"""扩展标的池回测 + 随机对照组（消除幸存者偏差的统计检验）。

背景：原 config.yaml 仅 4 只标的（2 只白酒 + AAPL/MSFT），存在选股幸存者偏差。
本脚本做三件事：
  1. 扩池回测：对 data/universe_expanded.yaml 的扩展池（A股30 + 美股12，行业分层，
     含下跌标的）逐只跑与 backtest.py 完全相同的引擎与参数；
  2. 随机标的对照：从池中无放回抽 4 只（原组合规模）等权组合，重复 K 次，
     得到「随机选股」的策略收益分布，看原组合落在分布什么位置（选股运气检验）；
  3. 随机信号对照：把每只标的的情绪序列随机打乱（日期不变）重跑 M 次，
     得到「随机择时」的收益分布，单侧 p 值检验真实情绪信号是否有 alpha。

用法（项目根目录）：
    .venv/Scripts/python.exe scripts/universe_expansion.py            # 全流程
    .venv/Scripts/python.exe scripts/universe_expansion.py --skip-data   # 数据已就绪，只跑回测+对照

输出：
    results/universe_report.md         汇总报告
    results/universe_backtest.csv      全池回测明细
    results/random_stock_control.csv   随机标的对照明细
    results/random_signal_control.csv  随机信号对照明细

仅用于学习研究，不构成投资建议。
"""

import argparse
import pathlib
import sys
from datetime import datetime

import numpy as np
import pandas as pd
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.download_data import fetch_bars, to_bar  # noqa: E402
from vnpy.trader.database import get_database  # noqa: E402
from scripts.backtest import run_engine, build_params, buy_hold_return  # noqa: E402
from scripts.sentiment_score import (  # noqa: E402
    load_history, load_single_daily, build_market_daily, fetch_symbol_daily,
    merge_score_dfs, reindex_to_trading_days, df_from_dict,
)

DATA_DIR = ROOT / "data"
RESULTS_DIR = ROOT / "results"
TMP_DIR = ROOT / ".tmp"
POOL_PATH = DATA_DIR / "universe_expanded.yaml"


def load_pool() -> list:
    with open(POOL_PATH, "r", encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("symbols") or []


def load_config() -> dict:
    with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def ensure_data(pool: list, start: str, end: str) -> list:
    """下载池内缺失的行情并写入 vnpy 数据库；返回有数据的标的列表。"""
    database = get_database()
    ok_items = []
    for item in pool:
        sym = item["symbol"]
        csv_path = DATA_DIR / f"bars_{sym}.csv"
        if csv_path.exists():
            ok_items.append(item)
            continue
        print(f"[data] {item['name']}({sym}) 下载行情 ...")
        try:
            df = fetch_bars(item, start, end)
        except Exception as e:  # noqa: BLE001
            print(f"  !! 拉取失败: {type(e).__name__} {str(e)[:100]}")
            continue
        if df.empty:
            print(f"  !! 无数据，跳过")
            continue
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        bars = [to_bar(r, item) for r in df.to_dict("records")]
        database.save_bar_data(bars)
        ok_items.append(item)
        print(f"  OK {len(bars)} 根K线")
    return ok_items


def ensure_sentiment(items: list, start: str, end: str) -> list:
    """为池内标的生成真实情绪 CSV（个股情绪覆盖市场情绪，与原 pipeline 一致）。"""
    history = load_history()
    single = load_single_daily()
    market_daily = build_market_daily(history, single)
    ok_items = []
    for item in items:
        sym = item["symbol"]
        out = DATA_DIR / f"sentiment_{sym}.csv"
        try:
            sym_frames = []
            if history:
                sym_frames.append(df_from_dict((history.get("symbols") or {}).get(sym, {})))
            if single:
                sym_frames.append(df_from_dict((single.get("symbols") or {}).get(sym, {})))
            if item["market"] == "cn":
                per = fetch_symbol_daily(item["code"])
                if not per.empty:
                    sym_frames.append(per)
            sym_daily = merge_score_dfs(*sym_frames)
            daily = merge_score_dfs(market_daily, sym_daily)
            final = reindex_to_trading_days(daily, sym)
            final.to_csv(out, index=False, encoding="utf-8-sig")
            n_nz = int((final["score"] != 0).sum())
            print(f"[sentiment] {item['name']}({sym}): {len(final)} 天，非零 {n_nz}")
            ok_items.append(item)
        except Exception as e:  # noqa: BLE001
            print(f"  !! {sym} 情绪生成失败: {type(e).__name__} {str(e)[:100]}")
    return ok_items


def backtest_pool(items: list, cfg: dict) -> list:
    """逐只回测（真实情绪），返回 [{item, ret, bh, sharpe, n_bars, trades, n_nz}]。"""
    bt = cfg["backtest"]
    sent = cfg["sentiment"]
    rows = []
    for item in items:
        sym = item["symbol"]
        path = DATA_DIR / f"sentiment_{sym}.csv"
        if not path.exists():
            continue
        try:
            n, st = run_engine(item, cfg, bt["start_date"], bt["end_date"],
                               str(path), build_params(item, sent, bt))
        except Exception as e:  # noqa: BLE001
            print(f"  !! {sym} 回测失败: {type(e).__name__} {str(e)[:100]}")
            continue
        ret = st.get("total_return")
        bh = buy_hold_return(sym, bt["start_date"], bt["end_date"])
        sent_df = pd.read_csv(path, encoding="utf-8-sig")
        n_nz = int((sent_df["score"] != 0).sum())
        rows.append({
            "item": item,
            "sym": sym,
            "market": item["market"],
            "ret": ret,
            "bh": bh,
            "sharpe": st.get("sharpe_ratio"),
            "n_bars": n,
            "trades": st.get("total_trade_count", 0),
            "n_nz": n_nz,
        })
        print(f"[backtest] {item['name']}({sym}): 策略 {ret:+.2f}% / BH {bh:+.2f}% "
              f"/ 夏普 {st.get('sharpe_ratio')} / 成交 {st.get('total_trade_count', 0)}")
    return rows


def random_stock_control(rows: list, cfg: dict, orig_syms: list, k: int) -> dict:
    """随机标的对照：无放回抽 4 只等权组合 × K 次。

    rows 需包含 ret 与 bh 均为数值的标的。返回分布与分位信息。
    """
    pool = [r for r in rows if r["ret"] is not None and r["bh"] is not None]
    if len(pool) < 4:
        return {"ok": False}
    rng = np.random.default_rng(42)
    n_draw = len(orig_syms)
    orig_rows = [r for r in pool if r["sym"] in orig_syms]
    if len(orig_rows) < n_draw:
        return {"ok": False}
    orig_ret = float(np.mean([r["ret"] for r in orig_rows]))
    orig_bh = float(np.mean([r["bh"] for r in orig_rows]))
    orig_excess = orig_ret - orig_bh

    rets, bhs, excesses = [], [], []
    idx = np.arange(len(pool))
    for _ in range(k):
        pick = rng.choice(idx, size=n_draw, replace=False)
        rets.append(float(np.mean([pool[i]["ret"] for i in pick])))
        bhs.append(float(np.mean([pool[i]["bh"] for i in pick])))
        excesses.append(rets[-1] - bhs[-1])
    rets = np.array(rets)
    bhs = np.array(bhs)
    excesses = np.array(excesses)

    def pct_rank(arr, v):
        return float(np.mean(arr <= v) * 100)

    return {
        "ok": True,
        "k": k,
        "orig_syms": orig_syms,
        "orig_ret": orig_ret,
        "orig_bh": orig_bh,
        "orig_excess": orig_excess,
        "ret_dist": rets,
        "bh_dist": bhs,
        "excess_dist": excesses,
        "ret_rank": pct_rank(rets, orig_ret),
        "bh_rank": pct_rank(bhs, orig_bh),
        "excess_rank": pct_rank(excesses, orig_excess),
    }


def random_signal_control(rows: list, cfg: dict, m: int) -> list:
    """随机信号对照：对每只标的 shuffle 情绪序列 M 次，单侧 p 值检验。"""
    bt = cfg["backtest"]
    sent = cfg["sentiment"]
    TMP_DIR.mkdir(exist_ok=True)
    rng = np.random.default_rng(2024)
    out = []
    for r in rows:
        item, sym = r["item"], r["sym"]
        path = DATA_DIR / f"sentiment_{sym}.csv"
        df = pd.read_csv(path, encoding="utf-8-sig")
        scores = df["score"].to_numpy(dtype=float)
        real_ret = r["ret"]
        if real_ret is None:
            continue
        tmp_path = TMP_DIR / f"shuffled_{sym}.csv"
        rand_rets = []
        for i in range(m):
            shuffled = rng.permutation(scores)
            tmp_df = df.copy()
            tmp_df["score"] = shuffled
            tmp_df.to_csv(tmp_path, index=False, encoding="utf-8-sig")
            try:
                _, st = run_engine(item, cfg, bt["start_date"], bt["end_date"],
                                   str(tmp_path), build_params(item, sent, bt))
                rr = st.get("total_return")
                if rr is not None:
                    rand_rets.append(rr)
            except Exception:  # noqa: BLE001
                continue
        tmp_path.unlink(missing_ok=True)
        if not rand_rets:
            continue
        arr = np.array(rand_rets)
        p_value = float((np.sum(arr >= real_ret) + 1) / (len(arr) + 1))
        out.append({
            "sym": sym,
            "name": item.get("name", ""),
            "real_ret": real_ret,
            "rand_mean": float(np.mean(arr)),
            "rand_p5": float(np.percentile(arr, 5)),
            "rand_p95": float(np.percentile(arr, 95)),
            "p_value": p_value,
            "m": len(arr),
        })
        print(f"[shuffle] {item['name']}({sym}): real {real_ret:+.2f}% / "
              f"rand mean {np.mean(arr):+.2f}% / p={p_value:.3f}")
    return out


def write_report(cfg: dict, pool_rows: list, stock_ctrl: dict, signal_rows: list) -> None:
    bt = cfg["backtest"]
    lines = []
    lines.append("# 标的池扩展与随机对照报告")
    lines.append("")
    lines.append(f"> 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
    lines.append(f"> 回测区间：{bt['start_date']} ~ {bt['end_date']}，初始资金 {int(bt['capital']):,}（每标的独立账户）")
    lines.append("> 池规则：沪深300/标普500 代表性成分股按行业分层抽样（A股30 + 美股12），")
    lines.append("> 刻意纳入 2024-2026 期间下跌的标的（万科A/隆基绿能/牧原股份/PFE 等），消除选股幸存者偏差。")
    lines.append("> 策略与参数与 `scripts/backtest.py` 完全一致（真实情绪，T-1 信号、T 日成交，含成本）。")
    lines.append("> **仅用于学习研究，不构成投资建议。**")
    lines.append("")

    rows = [r for r in pool_rows if r["ret"] is not None]
    n_cn = sum(1 for r in rows if r["market"] == "cn")
    n_us = sum(1 for r in rows if r["market"] == "us")
    lines.append(f"## 一、全池回测（成功 {len(rows)} 只：A股 {n_cn} / 美股 {n_us}）")
    lines.append("")
    lines.append("| 标的 | 市场 | K线 | 情绪非零天 | 策略收益 | 买入持有 | 策略-BH | 夏普 | 成交 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in sorted(rows, key=lambda x: x["ret"], reverse=True):
        excess = r["ret"] - r["bh"] if r["bh"] is not None else None
        bh_str = f"{r['bh']:+.2f}%" if r["bh"] is not None else "-"
        lines.append(
            f"| {r['item']['name']}({r['sym']}) | {r['market']} | {r['n_bars']} | {r['n_nz']} | "
            f"{r['ret']:+.2f}% | {bh_str} | "
            f"{excess:+.2f}% | {r['sharpe']:.2f} | {r['trades']} |"
        )
    lines.append("")

    rets = np.array([r["ret"] for r in rows])
    bhs = np.array([r["bh"] for r in rows if r["bh"] is not None])
    orig = cfg["symbols"]
    orig_rets = [r["ret"] for r in rows if r["sym"] in [s["symbol"] for s in orig]]
    lines.append("**汇总**：")
    lines.append(f"- 池平均策略收益 **{rets.mean():+.2f}%**（中位 {np.median(rets):+.2f}%，"
                 f"正收益 {np.mean(rets > 0) * 100:.0f}% 的标的）")
    lines.append(f"- 池平均买入持有 {bhs.mean():+.2f}%（中位 {np.median(bhs):+.2f}%）")
    lines.append(f"- 原 4 只组合（等权）策略收益 **{np.mean(orig_rets):+.2f}%**")
    lines.append("")

    if stock_ctrl.get("ok"):
        lines.append(f"## 二、随机标的对照（选股运气检验，K={stock_ctrl['k']} 次无放回抽 4 只等权）")
        lines.append("")
        lines.append("| 指标 | 随机分布 5% | 中位 | 95% | 原组合 | 原组合分位 |")
        lines.append("|---|---|---|---|---|---|")
        for label, dist, orig_v, rank in [
            ("组合策略收益", stock_ctrl["ret_dist"], stock_ctrl["orig_ret"], stock_ctrl["ret_rank"]),
            ("组合买入持有", stock_ctrl["bh_dist"], stock_ctrl["orig_bh"], stock_ctrl["bh_rank"]),
            ("组合超额(策略-BH)", stock_ctrl["excess_dist"], stock_ctrl["orig_excess"], stock_ctrl["excess_rank"]),
        ]:
            lines.append(
                f"| {label} | {np.percentile(dist, 5):+.2f}% | {np.median(dist):+.2f}% | "
                f"{np.percentile(dist, 95):+.2f}% | {orig_v:+.2f}% | {rank:.0f}% |"
            )
        lines.append("")
        lines.append("- 分位 = 随机组合中 ≤ 原组合的比例。分位 > 97.5% 或 < 2.5% 才算选股显著偏离随机。")
        lines.append("- 若原组合收益处于随机分布中游，说明原 4 只标的没有可归因的选股优势（收益差异来自标的自身行情）。")
        lines.append("")

    if signal_rows:
        lines.append(f"## 三、随机信号对照（情绪信号 alpha 检验，每标 M 次打乱）")
        lines.append("")
        lines.append("| 标的 | 真实收益 | 随机均值 | 随机 5%~95% | p(随机≥真实) | 判断 |")
        lines.append("|---|---|---|---|---|---|")
        for r in sorted(signal_rows, key=lambda x: x["p_value"]):
            verdict = "显著" if r["p_value"] < 0.05 else ("边缘" if r["p_value"] < 0.10 else "不显著")
            lines.append(
                f"| {r['name']}({r['sym']}) | {r['real_ret']:+.2f}% | {r['rand_mean']:+.2f}% | "
                f"{r['rand_p5']:+.2f}%~{r['rand_p95']:+.2f}% | {r['p_value']:.3f} | {verdict} |"
            )
        n_sig = sum(1 for r in signal_rows if r["p_value"] < 0.05)
        lines.append("")
        lines.append(f"- 显著标的 {n_sig}/{len(signal_rows)}（p<0.05，未做多重比较校正；"
                     f"Bonferroni 阈值 = 0.05/{len(signal_rows)} = {0.05 / max(len(signal_rows), 1):.4f}）。")
        lines.append("- p(随机≥真实) 越小，说明真实情绪序列的打分顺序带来的收益越不可能来自随机。")
        lines.append("")

    lines.append("## 四、结论")
    lines.append("")
    lines.append("- 本报告回答两个问题：① 原 4 只手工标的相对随机选股是否有优势；② 情绪信号相对随机打乱是否有 alpha。")
    lines.append("- 若两个对照都不显著，说明策略的超额收益（如果有）主要来自标的自身 beta 与行情，而非新闻情绪信号；")
    lines.append("  下一步应优先转向事件驱动信号（predictive_power_report.md 显示主题事件次日超额为正）。")
    lines.append("")
    RESULTS_DIR.mkdir(exist_ok=True)
    (RESULTS_DIR / "universe_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-data", action="store_true", help="行情/情绪已就绪，跳过下载")
    ap.add_argument("--k-stock", type=int, default=300, help="随机标的抽样次数")
    ap.add_argument("--m-shuffle", type=int, default=200, help="每标的情绪打乱次数")
    args = ap.parse_args()

    cfg = load_config()
    pool = load_pool()
    start, end = cfg["data"]["start_date"], cfg["data"]["end_date"]
    print(f"[universe] 池 {len(pool)} 只，区间 {start}~{end}")

    if not args.skip_data:
        pool = ensure_data(pool, start, end)
        pool = ensure_sentiment(pool, start, end)
        print(f"[universe] 数据就绪 {len(pool)} 只")
    else:
        pool = [it for it in pool if (DATA_DIR / f"bars_{it['symbol']}.csv").exists()
                and (DATA_DIR / f"sentiment_{it['symbol']}.csv").exists()]
        print(f"[universe] 已有数据 {len(pool)} 只")

    rows = backtest_pool(pool, cfg)
    if not rows:
        print("[universe] 无可用回测结果，退出")
        return 1
    pd.DataFrame([
        {k: v for k, v in r.items() if k != "item"} for r in rows
    ]).to_csv(RESULTS_DIR / "universe_backtest.csv", index=False, encoding="utf-8-sig")

    orig_syms = [s["symbol"] for s in cfg["symbols"]]
    stock_ctrl = random_stock_control(rows, cfg, orig_syms, args.k_stock)
    if stock_ctrl.get("ok"):
        pd.DataFrame({
            "ret": stock_ctrl["ret_dist"],
            "bh": stock_ctrl["bh_dist"],
            "excess": stock_ctrl["excess_dist"],
        }).to_csv(RESULTS_DIR / "random_stock_control.csv", index=False, encoding="utf-8-sig")

    signal_rows = random_signal_control(rows, cfg, args.m_shuffle)
    if signal_rows:
        pd.DataFrame(signal_rows).to_csv(RESULTS_DIR / "random_signal_control.csv",
                                         index=False, encoding="utf-8-sig")

    write_report(cfg, rows, stock_ctrl, signal_rows)
    print("[universe] 报告 -> results/universe_report.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
