"""回测脚本 v2：样本内/外分割 + 基准对比 + 参数稳健性扫描。

相对 v1 的增强：
- 前视已修复：策略内部用「上一交易日」情绪分生成信号。
- 样本内/外分割：前 60% 交易日为样本内（调参），后 40% 为样本外（验证）。
- 基准对比：同区间买入持有（Buy & Hold）收益。
- 参数稳健性：对 threshold × stop_loss_pct 做网格扫描，比较样本内/外表现。

用法（在项目根目录执行）：
    .venv/Scripts/python.exe scripts/backtest.py
输出：
    results/backtest_report.md    强化版汇总报告
    results/param_sweep.csv       参数扫描明细
"""

import pathlib
import sys
from datetime import datetime

import pandas as pd
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vnpy_ctastrategy.backtesting import BacktestingEngine  # noqa: E402
from vnpy.trader.constant import Interval  # noqa: E402

from strategies.news_sentiment_strategy import NewsSentimentStrategy  # noqa: E402

RESULTS_DIR = ROOT / "results"
DATA_DIR = ROOT / "data"


def fmt(v, digits: int = 2) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "-"
    return f"{f:,.{digits}f}"


def build_params(item: dict, sent: dict, bt: dict, overrides: dict | None = None) -> dict:
    """从 config 构造策略参数 dict。"""
    p = {
        "threshold": float(sent.get("threshold_long", 0.3)),
        "threshold_flat": float(sent.get("threshold_flat", -0.3)),
        "allow_short": bool(sent.get("allow_short", False)),
        "fixed_size": int(item.get("fixed_size", 100)),
        "risk_percent": float(sent.get("risk_percent", 0.0)),
        "max_position_pct": float(sent.get("max_position_pct", 0.95)),
        "lot_size": int(item.get("lot_size", 100)),
        "capital": float(bt.get("capital", 1_000_000)),
        "stop_loss_pct": float(sent.get("stop_loss_pct", 0.0)),
        "take_profit_pct": float(sent.get("take_profit_pct", 0.0)),
        "trailing_stop_pct": float(sent.get("trailing_stop_pct", 0.0)),
        "sentiment_path": "",
    }
    if overrides:
        p.update(overrides)
    return p


def run_engine(item: dict, cfg: dict, start: str, end: str,
               sentiment_path: str, params: dict) -> tuple[int, dict]:
    """跑一次 vnpy 回测，返回 (K线数, 统计dict)。"""
    bt = cfg["backtest"]
    engine = BacktestingEngine()
    engine.set_parameters(
        vt_symbol=f"{item['symbol']}.{item['exchange']}",
        interval=Interval.DAILY,
        start=datetime.strptime(start, "%Y%m%d"),
        end=datetime.strptime(end, "%Y%m%d"),
        rate=float(bt["rate"]),
        slippage=float(bt["slippage"]),
        size=1,
        pricetick=float(bt["pricetick"]),
        capital=int(bt["capital"]),
    )
    params["sentiment_path"] = sentiment_path
    engine.add_strategy(NewsSentimentStrategy, params)
    engine.load_data()
    n_bars = len(engine.history_data)
    engine.run_backtesting()
    df = engine.calculate_result()
    stats = engine.calculate_statistics(df, output=False)
    return n_bars, stats


def win_rate(stats: dict) -> float | None:
    pd_ = stats.get("profit_days", 0)
    ld_ = stats.get("loss_days", 0)
    if pd_ + ld_ > 0:
        return pd_ / (pd_ + ld_) * 100
    return None


def buy_hold_return(symbol: str, start: str, end: str) -> float | None:
    """区间买入持有收益率（%），区间首日收盘买入、末日收盘卖出。"""
    p = DATA_DIR / f"bars_{symbol}.csv"
    if not p.exists():
        return None
    bars = pd.read_csv(p, encoding="utf-8-sig")
    bars["date"] = pd.to_datetime(bars["date"])
    sd, ed = pd.to_datetime(start), pd.to_datetime(end)
    sub = bars[(bars["date"] >= sd) & (bars["date"] <= ed)]
    if sub.empty:
        return None
    return (float(sub.iloc[-1]["close"]) / float(sub.iloc[0]["close"]) - 1.0) * 100


def split_date(symbol: str, cfg: dict) -> str:
    """按交易日 60/40 分割，返回样本内截止日（YYYYMMDD）。"""
    start = cfg["backtest"]["start_date"]
    end = cfg["backtest"]["end_date"]
    p = DATA_DIR / f"bars_{symbol}.csv"
    bars = pd.read_csv(p, encoding="utf-8-sig")
    bars["date"] = pd.to_datetime(bars["date"])
    sd, ed = pd.to_datetime(start), pd.to_datetime(end)
    sub = bars[(bars["date"] >= sd) & (bars["date"] <= ed)].sort_values("date")
    if sub.empty:
        return start
    idx = int(len(sub) * 0.6) - 1
    idx = max(0, min(idx, len(sub) - 1))
    return sub.iloc[idx]["date"].strftime("%Y%m%d")


def stat_row(name: str, symbol: str, market: str, n_bars: int, stats: dict) -> dict:
    return {
        "name": name, "symbol": symbol, "market": market, "n_bars": n_bars,
        "total_return": stats.get("total_return"),
        "annual_return": stats.get("annual_return"),
        "sharpe": stats.get("sharpe_ratio"),
        "max_dd": stats.get("max_ddpercent"),
        "win_rate": win_rate(stats),
        "trades": stats.get("total_trade_count", 0),
        "end_balance": stats.get("end_balance"),
    }


def main() -> int:
    with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    RESULTS_DIR.mkdir(exist_ok=True)
    bt = cfg["backtest"]
    sent = cfg["sentiment"]
    start, end = bt["start_date"], bt["end_date"]

    symbols = cfg["symbols"]
    summary_rows = []

    # ---- 1) 全区间回测：真实情绪 + 合成情绪 + Buy&Hold ----
    for item in symbols:
        sym = item["symbol"]
        print(f"[backtest] {item['name']}({sym}) 全区间 ...")
        bh = buy_hold_return(sym, start, end)

        for mode, fname in [("real", f"sentiment_{sym}.csv"), ("synthetic", f"sentiment_synthetic_{sym}.csv")]:
            path = str(DATA_DIR / fname)
            try:
                params = build_params(item, sent, bt)
                n, st = run_engine(item, cfg, start, end, path, params)
                r = stat_row(item["name"], sym, item["market"], n, st)
                r["mode"] = mode
                r["buy_hold"] = bh
                summary_rows.append(r)
            except Exception as e:  # noqa: BLE001
                print(f"  !! {mode} 回测失败: {e}")

    # ---- 2) 样本内/外分割（合成情绪，因真实情绪历史缺失）----
    print("[backtest] 样本内/外分割（合成情绪）...")
    oos_rows = []
    for item in symbols:
        sym = item["symbol"]
        sp = split_date(sym, cfg)
        path = str(DATA_DIR / f"sentiment_synthetic_{sym}.csv")
        try:
            params = build_params(item, sent, bt)
            n_is, s_is = run_engine(item, cfg, start, sp, path, params)
            n_oos, s_oos = run_engine(item, cfg, sp, end, path, params)
            bh_is = buy_hold_return(sym, start, sp)
            bh_oos = buy_hold_return(sym, sp, end)
            oos_rows.append({
                "name": item["name"], "symbol": sym, "split": sp,
                "is_ret": s_is.get("total_return"), "oos_ret": s_oos.get("total_return"),
                "is_trades": s_is.get("total_trade_count", 0), "oos_trades": s_oos.get("total_trade_count", 0),
                "is_sharpe": s_is.get("sharpe_ratio"), "oos_sharpe": s_oos.get("sharpe_ratio"),
                "bh_is": bh_is, "bh_oos": bh_oos,
            })
        except Exception as e:  # noqa: BLE001
            print(f"  !! {sym} 样本内外分割失败: {e}")

    # ---- 3) 参数稳健性扫描（合成情绪）----
    print("[backtest] 参数稳健性扫描（threshold × stop_loss_pct，合成情绪）...")
    thresholds = [0.1, 0.2, 0.3, 0.4, 0.5]
    stop_losses = [0.0, 0.03, 0.05, 0.08, 0.10]
    sweep_rows = []
    for item in symbols:
        sym = item["symbol"]
        sp = split_date(sym, cfg)
        path = str(DATA_DIR / f"sentiment_synthetic_{sym}.csv")
        for th in thresholds:
            for sl in stop_losses:
                try:
                    overrides = {"threshold": th, "threshold_flat": -th, "stop_loss_pct": sl}
                    params = build_params(item, sent, bt, overrides)
                    _, s_is = run_engine(item, cfg, start, sp, path, params)
                    _, s_oos = run_engine(item, cfg, sp, end, path, params)
                    sweep_rows.append({
                        "symbol": sym, "threshold": th, "stop_loss_pct": sl,
                        "is_ret": s_is.get("total_return"), "oos_ret": s_oos.get("total_return"),
                        "is_trades": s_is.get("total_trade_count", 0),
                        "oos_trades": s_oos.get("total_trade_count", 0),
                    })
                except Exception as e:  # noqa: BLE001
                    print(f"  !! {sym} th={th} sl={sl} 失败: {e}")

    sweep_df = pd.DataFrame(sweep_rows)
    if not sweep_df.empty:
        sweep_df.to_csv(RESULTS_DIR / "param_sweep.csv", index=False, encoding="utf-8-sig")

    # ---- 4) 写报告 ----
    lines = []
    lines.append("# 新闻情绪策略回测报告（强化版 v2）")
    lines.append("")
    lines.append(f"> 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
    lines.append(f"> 初始资金：{int(bt['capital']):,}")
    lines.append(f"> 回测区间：{start} ~ {end}")
    lines.append(f"> 情绪阈值：开多 >= {sent['threshold_long']}，平多 <= {sent['threshold_flat']}")
    lines.append(f"> 风控：止损 {sent.get('stop_loss_pct', 0)} / 止盈 {sent.get('take_profit_pct', 0)} / 移动止损 {sent.get('trailing_stop_pct', 0)}")
    lines.append(f"> 仓位：risk_percent={sent.get('risk_percent', 0)}（0=按各标的 fixed_size 股）")
    lines.append("> 撮合：vnpy BAR 回测；策略内部用「上一交易日」情绪分，T 日收盘成交（已修复前视偏差）。**本报告仅用于学习研究，不构成投资建议。**")
    lines.append("")

    lines.append("## 一、全区间回测汇总（策略 vs 买入持有）")
    lines.append("")
    lines.append("| 标的 | 情绪 | K线 | 策略收益 | 年化 | 夏普 | 最大回撤 | 胜率 | 成交 | 买入持有 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in summary_rows:
        lines.append(
            f"| {r['name']}({r['symbol']}) | {r['mode']} | {r['n_bars']} | "
            f"{fmt(r['total_return'])}% | {fmt(r['annual_return'])}% | {fmt(r['sharpe'])} | "
            f"{fmt(r['max_dd'])}% | {fmt(r['win_rate']) if r['win_rate'] is not None else '-'}% | "
            f"{r['trades']} | {fmt(r['buy_hold'])}% |"
        )
    lines.append("")
    lines.append("- `real` 为真实新闻情绪（仅覆盖最近数日，历史多为前向填充，故成交极少，仅作机制验证）。")
    lines.append("- `synthetic` 为确定性 AR(1) 合成情绪，用于演示完整策略机制，不代表真实可盈利。")
    lines.append("- `买入持有` = 同区间首日收盘买入、末日收盘卖出的收益。")
    lines.append("")

    lines.append("## 二、样本内 / 样本外（合成情绪，60/40 按交易日分割）")
    lines.append("")
    lines.append("| 标的 | 分割日 | 样本内收益 | 样本外收益 | 样本内夏普 | 样本外夏普 | 样本内BH | 样本外BH |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in oos_rows:
        lines.append(
            f"| {r['name']}({r['symbol']}) | {r['split']} | {fmt(r['is_ret'])}% | {fmt(r['oos_ret'])}% | "
            f"{fmt(r['is_sharpe'])} | {fmt(r['oos_sharpe'])} | {fmt(r['bh_is'])}% | {fmt(r['bh_oos'])}% |"
        )
    lines.append("")
    lines.append("- 样本外是「从未参与调参」的数据，其表现才代表策略的真实泛化能力。")
    lines.append("")

    lines.append("## 三、参数稳健性扫描（threshold × stop_loss_pct）")
    lines.append("")
    lines.append("判断标准：样本内表现好的参数组合，样本外是否依然为正。若样本内为正、样本外普遍为负，说明过拟合。")
    lines.append("")
    if not sweep_df.empty:
        for sym in sorted(sweep_df["symbol"].unique()):
            d = sweep_df[sweep_df["symbol"] == sym]
            pos_is = d[d["is_ret"] > 0]
            n_is = len(pos_is)
            n_oos_pos = int((pos_is["oos_ret"] > 0).sum()) if n_is else 0
            lines.append(f"### {sym}")
            lines.append("")
            lines.append(f"- 样本内为正的参数组合：{n_is}/{len(d)}；其中样本外仍为正：{n_oos_pos}/{n_is}。")
            top = d.sort_values("is_ret", ascending=False).head(5)
            lines.append("")
            lines.append("样本内收益最高的 5 组：")
            lines.append("")
            lines.append("| threshold | stop_loss | 样本内收益 | 样本外收益 | 样本内成交 | 样本外成交 |")
            lines.append("|---|---|---|---|---|---|")
            for _, row in top.iterrows():
                lines.append(
                    f"| {row['threshold']} | {row['stop_loss_pct']} | {fmt(row['is_ret'])}% | "
                    f"{fmt(row['oos_ret'])}% | {int(row['is_trades'])} | {int(row['oos_trades'])} |"
                )
            lines.append("")
    lines.append("（明细见 results/param_sweep.csv）")
    lines.append("")

    report = RESULTS_DIR / "backtest_report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"[backtest] 报告已写入 {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
