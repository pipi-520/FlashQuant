"""回测脚本 v3：样本内/外分割 + 基准对比 + 参数稳健性扫描 + Walk-forward。

相对 v2 的增强：
- 样本内/外分割与 Walk-forward 同时覆盖「真实情绪 + 合成情绪」。
- Walk-forward：固定训练段 + 滚动测试段，逐窗口训练段选优、测试段验证，
  判断信号是否随时间稳定（避免单次 60/40 分割的偶然性）。
- 前视已修复：策略内部用「上一交易日」情绪分生成信号。

用法（在项目根目录执行）：
    .venv/Scripts/python.exe scripts/backtest.py
输出：
    results/backtest_report.md    汇总报告（全区间/样本内外/参数扫描/Walk-forward）
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

# 行情缓存：walk_forward 网格扫描会数百次实例化引擎，
# engine.load_data() 每次都分批查 SQLite；改为全量加载一次后内存切片。
_BAR_CACHE: dict = {}


def _load_bars_cached(vt_symbol: str, interval) -> list:
    key = (vt_symbol, interval)
    bars = _BAR_CACHE.get(key)
    if bars is None:
        from vnpy.trader.database import get_database  # noqa: E402
        from vnpy.trader.constant import Exchange  # noqa: E402
        symbol, exchange = vt_symbol.split(".")
        db = get_database()
        bars = db.load_bar_data(symbol, Exchange(exchange), interval,
                                datetime(1990, 1, 1), datetime(2100, 1, 1))
        _BAR_CACHE[key] = bars
    return bars


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
    # 交易成本：vnpy 的 rate 为双边佣金率，不区分买卖方向；
    # A股另有卖出印花税 0.05%，近似摊到双边（+0.00025）。美股无印花税。
    rate = float(bt["rate"])
    if item.get("market") == "cn":
        rate += 0.00025
    engine = BacktestingEngine()
    engine.set_parameters(
        vt_symbol=f"{item['symbol']}.{item['exchange']}",
        interval=Interval.DAILY,
        start=datetime.strptime(start, "%Y%m%d"),
        end=datetime.strptime(end, "%Y%m%d"),
        rate=rate,
        slippage=float(bt["slippage"]),
        size=1,
        pricetick=float(bt["pricetick"]),
        capital=int(bt["capital"]),
    )
    params["sentiment_path"] = sentiment_path
    engine.add_strategy(NewsSentimentStrategy, params)
    # 用缓存行情替换 engine.load_data()：全量加载一次后按区间内存切片
    vt_symbol = f"{item['symbol']}.{item['exchange']}"
    bars = _load_bars_cached(vt_symbol, Interval.DAILY)
    tz = bars[0].datetime.tzinfo if bars else None
    sd = datetime.strptime(start, "%Y%m%d")
    ed = datetime.strptime(end, "%Y%m%d")
    if tz is not None:
        sd = sd.replace(tzinfo=tz)
        ed = ed.replace(tzinfo=tz)
    engine.history_data = [b for b in bars if sd <= b.datetime <= ed]
    n_bars = len(engine.history_data)
    engine.run_backtesting()
    df = engine.calculate_result()
    stats = engine.calculate_statistics(df, output=False)
    return n_bars, stats


def profit_day_pct(stats: dict) -> float | None:
    """盈利交易日占比（vnpy 统计不含逐笔交易明细，无法算真实交易胜率）。"""
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


def trading_days(symbol: str, cfg: dict) -> list:
    """返回回测区间内的交易日列表（YYYYMMDD 升序）。"""
    start = cfg["backtest"]["start_date"]
    end = cfg["backtest"]["end_date"]
    p = DATA_DIR / f"bars_{symbol}.csv"
    if not p.exists():
        return []
    bars = pd.read_csv(p, encoding="utf-8-sig")
    bars["date"] = pd.to_datetime(bars["date"])
    sd, ed = pd.to_datetime(start), pd.to_datetime(end)
    sub = bars[(bars["date"] >= sd) & (bars["date"] <= ed)].sort_values("date")
    return sub["date"].dt.strftime("%Y%m%d").tolist()


def split_date(symbol: str, cfg: dict) -> str:
    """按交易日 60/40 分割，返回样本内截止日（YYYYMMDD）。"""
    days = trading_days(symbol, cfg)
    if not days:
        return cfg["backtest"]["start_date"]
    idx = max(0, int(len(days) * 0.6) - 1)
    return days[idx]


def stat_row(name: str, symbol: str, market: str, n_bars: int, stats: dict) -> dict:
    return {
        "name": name, "symbol": symbol, "market": market, "n_bars": n_bars,
        "total_return": stats.get("total_return"),
        "annual_return": stats.get("annual_return"),
        "sharpe": stats.get("sharpe_ratio"),
        "max_dd": stats.get("max_ddpercent"),
        "win_rate": profit_day_pct(stats),
        "trades": stats.get("total_trade_count", 0),
        "end_balance": stats.get("end_balance"),
    }


def walk_forward(item: dict, cfg: dict, sent: dict, bt: dict, path: str,
                 thresholds: list, stop_losses: list,
                 train_days: int = 240, test_days: int = 60) -> list:
    """滚动窗口 walk-forward：固定训练段 + 滚动测试段。

    每个窗口：训练段跑 threshold×stop_loss 网格选「收益最高」的参数，
    再用该参数在测试段验证（测试段从未参与调参）。返回逐窗口明细。
    """
    sym = item["symbol"]
    days = trading_days(sym, cfg)
    results = []
    win = 0
    i = train_days
    while i + test_days <= len(days):
        train_start = days[max(0, i - train_days)]
        train_end = days[i - 1]
        test_start = days[i]
        test_end = days[i + test_days - 1]

        # 训练段网格选优
        best = None  # (th, sl, train_ret)
        for th in thresholds:
            for sl in stop_losses:
                overrides = {"threshold": th, "threshold_flat": -th, "stop_loss_pct": sl}
                try:
                    _, s_is = run_engine(item, cfg, train_start, train_end,
                                         path, build_params(item, sent, bt, overrides))
                except Exception:  # noqa: BLE001
                    continue
                ret = s_is.get("total_return")
                if ret is None:
                    continue
                if best is None or ret > best[2]:
                    best = (th, sl, ret)
        if best is None:
            i += test_days
            continue

        th, sl, _ = best
        overrides = {"threshold": th, "threshold_flat": -th, "stop_loss_pct": sl}
        try:
            _, s_test = run_engine(item, cfg, test_start, test_end,
                                   path, build_params(item, sent, bt, overrides))
        except Exception:  # noqa: BLE001
            i += test_days
            continue

        results.append({
            "symbol": sym, "window": win,
            "test_start": test_start, "test_end": test_end,
            "best_th": th, "best_sl": sl,
            "test_ret": s_test.get("total_return"),
            "test_sharpe": s_test.get("sharpe_ratio"),
            "test_trades": s_test.get("total_trade_count", 0),
        })
        win += 1
        i += test_days
    return results


def main() -> int:
    with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    RESULTS_DIR.mkdir(exist_ok=True)
    bt = cfg["backtest"]
    sent = cfg["sentiment"]
    start, end = bt["start_date"], bt["end_date"]

    symbols = cfg["symbols"]
    thresholds = [0.1, 0.2, 0.3, 0.4, 0.5]
    stop_losses = [0.0, 0.03, 0.05, 0.08, 0.10]
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

    # ---- 2) 样本内/外分割（真实 + 合成情绪，60/40 按交易日分割）----
    print("[backtest] 样本内/外分割（真实 + 合成情绪）...")
    oos_rows = []
    for item in symbols:
        sym = item["symbol"]
        sp = split_date(sym, cfg)
        for mode, fname in [("real", f"sentiment_{sym}.csv"),
                            ("synthetic", f"sentiment_synthetic_{sym}.csv")]:
            path = str(DATA_DIR / fname)
            try:
                params = build_params(item, sent, bt)
                n_is, s_is = run_engine(item, cfg, start, sp, path, params)
                n_oos, s_oos = run_engine(item, cfg, sp, end, path, params)
                bh_is = buy_hold_return(sym, start, sp)
                bh_oos = buy_hold_return(sym, sp, end)
                oos_rows.append({
                    "name": item["name"], "symbol": sym, "mode": mode, "split": sp,
                    "is_ret": s_is.get("total_return"), "oos_ret": s_oos.get("total_return"),
                    "is_trades": s_is.get("total_trade_count", 0), "oos_trades": s_oos.get("total_trade_count", 0),
                    "is_sharpe": s_is.get("sharpe_ratio"), "oos_sharpe": s_oos.get("sharpe_ratio"),
                    "bh_is": bh_is, "bh_oos": bh_oos,
                })
            except Exception as e:  # noqa: BLE001
                print(f"  !! {sym} {mode} 样本内外分割失败: {e}")

    # ---- 3) 参数稳健性扫描（合成情绪）----
    print("[backtest] 参数稳健性扫描（threshold × stop_loss_pct，合成情绪）...")
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

    # ---- 4) Walk-forward 滚动窗口（真实 + 合成情绪）----
    print("[backtest] Walk-forward 滚动窗口（真实 + 合成情绪）...")
    wf_rows = []
    for item in symbols:
        sym = item["symbol"]
        for mode, fname in [("real", f"sentiment_{sym}.csv"),
                            ("synthetic", f"sentiment_synthetic_{sym}.csv")]:
            path = str(DATA_DIR / fname)
            try:
                res = walk_forward(item, cfg, sent, bt, path, thresholds, stop_losses)
                rets = [r["test_ret"] for r in res if r["test_ret"] is not None]
                sharpes = [r["test_sharpe"] for r in res if r["test_sharpe"] is not None]
                wf_rows.append({
                    "name": item["name"], "symbol": sym, "mode": mode,
                    "windows": len(res),
                    "pos_windows": sum(1 for x in rets if x > 0),
                    "avg_ret": (sum(rets) / len(rets)) if rets else None,
                    "avg_sharpe": (sum(sharpes) / len(sharpes)) if sharpes else None,
                    "total_trades": sum(r["test_trades"] for r in res),
                })
            except Exception as e:  # noqa: BLE001
                print(f"  !! {sym} {mode} walk-forward 失败: {e}")

    # ---- 5) 写报告 ----
    lines = []
    lines.append("# 新闻情绪策略回测报告（强化版 v3）")
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
    lines.append("| 标的 | 情绪 | K线 | 策略收益 | 年化 | 夏普 | 最大回撤 | 盈利天数% | 成交 | 买入持有 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in summary_rows:
        lines.append(
            f"| {r['name']}({r['symbol']}) | {r['mode']} | {r['n_bars']} | "
            f"{fmt(r['total_return'])}% | {fmt(r['annual_return'])}% | {fmt(r['sharpe'])} | "
            f"{fmt(r['max_dd'])}% | {fmt(r['win_rate']) if r['win_rate'] is not None else '-'}% | "
            f"{r['trades']} | {fmt(r['buy_hold'])}% |"
        )
    lines.append("")
    lines.append("- `real` 为真实新闻/公告情绪（经 scripts/backfill_news.py 回填，覆盖约 2.5 年：A股东财新闻+公告、美股 SEC 8-K+Finnhub）。")
    lines.append("- `synthetic` 为确定性 AR(1) 合成情绪，用于演示完整策略机制，不代表真实可盈利。")
    lines.append("- `买入持有` = 同区间首日收盘买入、末日收盘卖出的收益（不计成本）。")
    lines.append("- `盈利天数%` = 盈利交易日占比（vnpy 统计口径，非逐笔交易胜率）。")
    lines.append("- 成本口径：佣金 rate 双边 + 每股滑点；A股另按 0.05% 印花税摊入双边（+0.00025）。")
    lines.append("")

    lines.append("## 二、样本内 / 样本外（真实 + 合成情绪，60/40 按交易日分割）")
    lines.append("")
    lines.append("| 标的 | 情绪 | 分割日 | 样本内收益 | 样本外收益 | 样本内夏普 | 样本外夏普 | 样本内BH | 样本外BH |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in oos_rows:
        lines.append(
            f"| {r['name']}({r['symbol']}) | {r['mode']} | {r['split']} | {fmt(r['is_ret'])}% | {fmt(r['oos_ret'])}% | "
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

    lines.append("## 四、Walk-forward 滚动窗口（真实 + 合成情绪）")
    lines.append("")
    lines.append("方法：固定训练段 240 交易日 + 滚动测试段 60 交易日；每个窗口在训练段用 threshold×stop_loss 网格选「收益最高」的参数，再用该参数跑测试段（测试段从未参与调参）。")
    lines.append("")
    lines.append("| 标的 | 情绪 | 窗口数 | 测试段为正窗口 | 平均测试收益 | 平均测试夏普 | 总测试成交 |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in wf_rows:
        lines.append(
            f"| {r['name']}({r['symbol']}) | {r['mode']} | {r['windows']} | "
            f"{r['pos_windows']}/{r['windows']} | {fmt(r['avg_ret'])}% | {fmt(r['avg_sharpe'])} | {r['total_trades']} |"
        )
    lines.append("")
    lines.append("- `测试段为正窗口` = 测试段收益 > 0 的窗口占比；占比越高说明信号随时间越稳定。")
    lines.append("- 若单次分割样本外为正、walk-forward 却普遍为负，说明单次分割的「正收益」是过拟合/偶然。")
    lines.append("")

    report = RESULTS_DIR / "backtest_report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"[backtest] 报告已写入 {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
