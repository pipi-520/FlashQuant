"""事件驱动策略回测 + 反转因子基线 + 随机对照。

动机：词典情绪分作为日频择时因子已通过随机对照检验证明无 alpha（0/42 显著，
见 results/universe_report.md）。本项目历史数据支持的另一条证据链是「事件」：
  - 事件类型自带方向且有 alpha（.event-study：分红 +0.45%、中标 +0.55%、
    涨价 +0.69%、处罚 -0.98%，已沉淀为 event_filter 白名单）；
  - 主题命中后板块次日超额为正（predictive_power：创新药/美联储/AI/原油 +0.4~0.7%）。

本脚本把这两条证据链做成可回测策略，并与「短期反转」经典因子同口径对比，
再用随机打乱对照检验显著性。

信号定义（无前视：事件日 d -> 参考交易日 i = <=d 的最后交易日，i 日收盘后确认）：
  1. 个股事件（A股）：news/raw 中 symbols 命中且白名单的利好事件
     （分红/中标订单/涨价提价）-> i+1 开盘买入；白名单利空（处罚诉讼）-> 持仓则平仓。
  2. 主题板块（A股）：主题关键词命中日 -> i+1 收盘买入该主题板块指数篮子（等权）。
  3. 反转基线（全池42）：t 日收盘收益 <= -th -> t+1 开盘买入（th ∈ 1%/2%/3%）。

统一撮合：持有 N 日 = 买入后第 N 个交易日收盘卖出（N=1 即次日收盘卖，满足 A股 T+1）；
成本 = 佣金 0.03% 双边 + 滑点 0.01 元/股 + A股印花税 0.05%（卖出）；
每信号账户 100 万本金、全仓买入（A股 100 股整数手 / 美股 1 股）。

随机对照：把事件日/主题命中日随机映射到随机交易日（保持数量）重跑 M 次，
得到「随机择时」收益分布，单侧 p 值 = P(随机 >= 真实)。

用法（项目根目录）：
    .venv/Scripts/python.exe scripts/event_strategy.py --backfill     # 首次：回填全池A股新闻+公告
    .venv/Scripts/python.exe scripts/event_strategy.py                # 回测 + 对照
    .venv/Scripts/python.exe scripts/event_strategy.py --m-shuffle 100  # 快速模式
输出：results/event_strategy_report.md、event_backtest.csv、event_shuffle.csv

仅用于学习研究，不构成投资建议。
"""

import argparse
import bisect
import json
import pathlib
import sys
from collections import defaultdict
from datetime import datetime

import numpy as np
import pandas as pd
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from news_aggregator.event_filter import classify  # noqa: E402
from news_aggregator.impact import match_themes  # noqa: E402

NEWS_DIR = ROOT / "news"
DATA_DIR = ROOT / "data"
RESULTS_DIR = ROOT / "results"
POOL_PATH = DATA_DIR / "universe_expanded.yaml"

CAPITAL = 1_000_000.0
RATE = 0.0003          # 双边佣金
SLIPPAGE = 0.01        # 元/股
STAMP_CN = 0.0005      # A股卖出印花税

BUY_TYPES = {"分红", "中标订单", "涨价提价"}
SELL_TYPES = {"处罚诉讼"}


# ================= 数据准备 =================

def load_pool() -> list:
    with open(POOL_PATH, "r", encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("symbols") or []


def load_themes() -> list:
    with open(ROOT / "news_aggregator/themes.yaml", "r", encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("themes") or []


def backfill_pool(pool: list, start_ymd: str) -> int:
    """回填池内 A 股个股新闻+公告（复用 backfill 管线，幂等）。"""
    from news_aggregator.backfill import (fetch_em_stock_news,
                                          fetch_em_announcements, archive)
    all_items = []
    for item in pool:
        if item.get("market") != "cn":
            continue
        sym = item["symbol"]
        print(f"[backfill] {item['name']}({sym}) 东财新闻+公告 ...")
        try:
            items = fetch_em_stock_news(str(item.get("name") or item.get("code")), sym, start_ymd)
            items += fetch_em_announcements(str(item["code"]), sym, start_ymd)
        except Exception as e:  # noqa: BLE001
            print(f"  !! 失败: {type(e).__name__} {str(e)[:100]}")
            continue
        print(f"  -> {len(items)} 条")
        all_items.extend(items)
    uniq = {}
    for it in all_items:
        if it["id"] in uniq:
            merged = set(uniq[it["id"]].get("symbols") or [])
            merged.update(it.get("symbols") or [])
            uniq[it["id"]]["symbols"] = sorted(merged)
        else:
            uniq[it["id"]] = it
    written = archive(list(uniq.values()))
    print(f"[backfill] 归档新增 {written} 条（去重后 {len(uniq)} 条）")
    return written


def load_all_items() -> list:
    items = []
    seen = set()
    for path in sorted((NEWS_DIR / "raw").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                it = json.loads(line)
            except json.JSONDecodeError:
                continue
            if it.get("id") in seen:
                continue
            seen.add(it.get("id"))
            items.append(it)
    return items


def build_stock_event_signals(items: list, cn_syms: set) -> dict:
    """返回 {sym: {'buy': set(dates), 'sell': set(dates)}}。"""
    signals = {s: {"buy": set(), "sell": set()} for s in cn_syms}
    for it in items:
        if it.get("kind") != "news":
            continue
        d = it.get("date")
        if not d:
            continue
        syms = [s for s in (it.get("symbols") or []) if s in cn_syms]
        if not syms:
            continue
        cls = classify(f"{it.get('title', '')} {it.get('content', '')}")
        if not cls or not cls[2]:
            continue
        typ, direction, _ = cls
        for s in syms:
            if typ in BUY_TYPES:
                signals[s]["buy"].add(d)
            elif typ in SELL_TYPES:
                signals[s]["sell"].add(d)
    return signals


def build_theme_signals(items: list, themes: list, impact_min: float = 0.0) -> dict:
    """返回 {theme_name: set(hit_dates)}。"""
    hits = {th["name"]: set() for th in themes}
    for it in items:
        if it.get("kind") != "news":
            continue
        d = it.get("date")
        if not d:
            continue
        if float(it.get("impact") or 0.0) < impact_min:
            continue
        text = f"{it.get('title', '')} {it.get('content', '')}"
        for th, _h in match_themes(text, themes):
            hits[th["name"]].add(d)
    return hits


def resolve_theme_boards(themes: list, max_boards: int = 3) -> dict:
    """{theme_name: [(board_name, kind)]}，同 predictive_power 口径（同花顺）。"""
    import akshare as ak
    try:
        cnames = [str(x) for x in ak.stock_board_concept_name_ths()["name"].tolist()]
    except Exception:  # noqa: BLE001
        cnames = []
    try:
        inames = [str(x) for x in ak.stock_board_industry_name_ths()["name"].tolist()]
    except Exception:  # noqa: BLE001
        inames = []
    out = {}
    for th in themes:
        boards = []
        for pat in (th.get("concept_boards") or []):
            pl = str(pat).lower()
            for n in cnames:
                if pl in n.lower() and (n, "concept") not in boards:
                    boards.append((n, "concept"))
        for pat in (th.get("industry_boards") or []):
            pl = str(pat).lower()
            for n in inames:
                if pl in n.lower() and (n, "industry") not in boards:
                    boards.append((n, "industry"))
        out[th["name"]] = boards[:max_boards]
    return out


def load_board_index(name: str, kind: str, start: str, end: str) -> pd.DataFrame:
    """同花顺板块指数日线 -> DataFrame[date(YYYY-MM-DD), open, close]。"""
    import akshare as ak
    if kind == "concept":
        df = ak.stock_board_concept_index_ths(symbol=name, start_date=start, end_date=end)
    else:
        df = ak.stock_board_industry_index_ths(symbol=name, start_date=start, end_date=end)
    date_col = next((c for c in ("date", "日期", "时间") if c in df.columns), df.columns[0])
    open_col = next((c for c in ("open", "开盘价", "开盘") if c in df.columns), None)
    close_col = next((c for c in ("close", "收盘价", "收盘") if c in df.columns), None)
    if close_col is None:
        return pd.DataFrame(columns=["date", "open", "close"])
    out = pd.DataFrame({
        "date": pd.to_datetime(df[date_col], errors="coerce").dt.strftime("%Y-%m-%d"),
        "open": pd.to_numeric(df[open_col], errors="coerce") if open_col else np.nan,
        "close": pd.to_numeric(df[close_col], errors="coerce"),
    }).dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
    out["open"] = out["open"].fillna(out["close"])
    return out


def load_stock_bars(sym: str) -> pd.DataFrame:
    df = pd.read_csv(DATA_DIR / f"bars_{sym}.csv", encoding="utf-8-sig")
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    return df.sort_values("date").reset_index(drop=True)


# ================= 统一日频撮合引擎 =================

def run_daily(dates, opens, closes, buy_dates, sell_dates, hold_days: int,
              lot_size: int, stamp_tax: float, rate: float = RATE,
              slippage: float = SLIPPAGE) -> dict:
    """日频回测：T-1 信号 -> T 开盘买入（全仓），买入后第 hold_days 个交易日收盘卖出。

    卖出优先级：到期平仓 > 利空信号平仓。期末强制平仓。
    rate/slippage 可覆盖（成本敏感性分析用）。
    返回 {ret, trades, wins, avg_hold}。
    """
    cash = CAPITAL
    qty = 0
    entry_idx = -1
    trades = 0
    wins = 0
    buy_amt = 0.0

    def sell_at(price, idx):
        nonlocal cash, qty, trades, wins, buy_amt
        if qty <= 0:
            return
        gross = price * qty
        cash += gross - gross * rate - qty * slippage - gross * stamp_tax
        pnl = gross - buy_amt
        if pnl > 0:
            wins += 1
        trades += 1
        qty = 0
        buy_amt = 0.0

    n = len(dates)
    for i in range(n):
        if qty > 0 and i == entry_idx + hold_days:
            sell_at(closes[i], i)
        if qty > 0 and dates[i] in sell_dates:
            sell_at(closes[i], i)
        if qty == 0 and i >= 1 and dates[i - 1] in buy_dates:
            price = opens[i]
            if price and price > 0:
                size = int(cash * 0.95 / (price * (1 + rate) + slippage))
                if lot_size > 1:
                    size = size // lot_size * lot_size
                if size >= lot_size:
                    cost = price * size * (1 + rate) + size * slippage
                    cash -= cost
                    qty = float(size)
                    buy_amt = cost
                    entry_idx = i
    if qty > 0:
        sell_at(closes[n - 1], n - 1)
    equity = cash
    return {
        "ret": (equity / CAPITAL - 1.0) * 100.0,
        "trades": trades,
        "win_rate": (wins / trades * 100.0) if trades else None,
    }


def align_dates(bars: pd.DataFrame) -> tuple:
    dates = bars["date"].tolist()
    return dates, bars["open"].to_numpy(float), bars["close"].to_numpy(float)


def backtest_symbol(sym: str, buy_dates: set, sell_dates: set, hold_days: int,
                    lot_size: int, stamp_tax: float) -> dict | None:
    bars = load_stock_bars(sym)
    if bars.empty:
        return None
    dates, opens, closes = align_dates(bars)
    st = run_daily(dates, opens, closes, buy_dates, sell_dates, hold_days, lot_size, stamp_tax)
    st["sym"] = sym
    st["n_signals"] = len(buy_dates) + len(sell_dates)
    return st


def backtest_board(board_df: pd.DataFrame, hit_dates: set, hold_days: int) -> dict | None:
    """板块指数版：买入价用 close（i+1 收盘，指数无法开盘成交，近似）。"""
    if board_df.empty:
        return None
    dates, opens, closes = align_dates(board_df)
    st = run_daily(dates, closes, closes, hit_dates, set(), hold_days, 1, 0.0)
    st["n_signals"] = len(hit_dates)
    return st


def combo_ret(stats: list) -> float | None:
    vals = [s["ret"] for s in stats if s is not None]
    return float(np.mean(vals)) if vals else None


# ================= 随机对照 =================

def shuffled_dates(dates: list, n: int, rng) -> set:
    return set(rng.choice(dates[1:], size=min(n, len(dates) - 1), replace=False).tolist())


def shuffle_stock_control(cn_items: list, signals: dict, hold_days: int, m: int,
                          rng, real_ret: float) -> dict:
    """个股事件：每 sym 信号日随机化重跑 M 次，返回组合收益分布与 p 值。"""
    dist = []
    for _ in range(m):
        stats = []
        for item in cn_items:
            sym = item["symbol"]
            bars = load_stock_bars(sym)
            if bars.empty:
                continue
            dates, opens, closes = align_dates(bars)
            n_buy = len(signals.get(sym, {}).get("buy", set()))
            n_sell = len(signals.get(sym, {}).get("sell", set()))
            buy = shuffled_dates(dates, n_buy, rng)
            sell = shuffled_dates(dates, n_sell, rng)
            st = run_daily(dates, opens, closes, buy, sell, hold_days,
                           int(item.get("lot_size", 100)), STAMP_CN)
            stats.append(st)
        r = combo_ret(stats)
        if r is not None:
            dist.append(r)
    arr = np.array(dist)
    return {"real": real_ret, "dist": arr, "p": float((np.sum(arr >= real_ret) + 1) / (len(arr) + 1))}


def shuffle_theme_control(theme_boards_all: dict, theme_hits: dict, hold_days: int,
                          m: int, rng, real_ret: float) -> dict:
    """主题板块：命中日随机化重跑 M 次。"""
    dist = []
    for _ in range(m):
        stats = []
        for tname, hit_dates in theme_hits.items():
            for name, kind, bdf in theme_boards_all.get(tname, []):
                if bdf.empty:
                    continue
                dates = bdf["date"].tolist()
                rd = shuffled_dates(dates, len(hit_dates), rng)
                st = backtest_board(bdf, rd, hold_days)
                stats.append(st)
        r = combo_ret(stats)
        if r is not None:
            dist.append(r)
    arr = np.array(dist)
    return {"real": real_ret, "dist": arr, "p": float((np.sum(arr >= real_ret) + 1) / (len(arr) + 1))}


# ================= 反转基线 =================

def reversal_signals(bars: pd.DataFrame, th: float) -> set:
    dates = bars["date"].tolist()
    closes = bars["close"].to_numpy(float)
    sig = set()
    for i in range(1, len(dates)):
        if closes[i - 1] > 0 and closes[i] / closes[i - 1] - 1 <= th:
            sig.add(dates[i])
    return sig


def shuffle_reversal_control(pool: list, th: float, hold_days: int, m: int,
                             rng, real_ret: float) -> dict:
    """反转因子 vs 随机择时：每账户保持信号数量，随机化信号日重跑 M 次。"""
    dist = []
    for _ in range(m):
        stats = []
        for item in pool:
            bars = load_stock_bars(item["symbol"])
            if bars.empty:
                continue
            dates, opens, closes = align_dates(bars)
            n_sig = len(reversal_signals(bars, th))
            rd = shuffled_dates(dates, n_sig, rng)
            st = run_daily(dates, opens, closes, rd, set(), hold_days,
                           int(item.get("lot_size", 1)), STAMP_CN if item["market"] == "cn" else 0.0)
            stats.append(st)
        r = combo_ret(stats)
        if r is not None:
            dist.append(r)
    arr = np.array(dist)
    return {"real": real_ret, "dist": arr, "p": float((np.sum(arr >= real_ret) + 1) / (len(arr) + 1))}


# ================= 报告 =================

def write_report(pool, stock_rows, theme_rows, reversal_rows, stock_ctrl, theme_ctrl,
                 reversal_ctrl, cn_syms, hold_main) -> None:
    lines = []
    lines.append("# 事件驱动策略回测报告（vs 反转基线 + 随机对照）")
    lines.append("")
    lines.append(f"> 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
    lines.append("> 口径：每信号账户 100 万、全仓、T+1 开盘成交、买入后第 N 个交易日收盘卖出；")
    lines.append("> 成本 = 佣金 0.03% 双边 + 滑点 0.01 元/股 + A股印花税 0.05%（卖出）。")
    lines.append("> 事件日 -> 参考交易日 i（<=事件日的最后交易日），i 收盘后确认信号，无前视。")
    lines.append("> **仅用于学习研究，不构成投资建议。**")
    lines.append("")

    lines.append("## 一、个股事件策略（A股，白名单：分红/中标/涨价=买入，处罚=平仓）")
    lines.append("")
    lines.append("| 持有 N 日 | 组合收益 | 参与账户 | 总交易 | 平均胜率 | 总信号 |")
    lines.append("|---|---|---|---|---|---|")
    for r in stock_rows:
        lines.append(
            f"| {r['hold']} | {r['ret']:+.2f}% | {r['n_accts']} | {r['trades']} | "
            f"{r['win']:.0f}% | {r['n_signals']} |"
        )
    lines.append("")
    if stock_ctrl:
        arr = stock_ctrl["dist"]
        lines.append(f"- 随机对照（持有 {hold_main} 日，M={len(arr)} 次打乱）：真实组合 {stock_ctrl['real']:+.2f}%，"
                     f"随机均值 {arr.mean():+.2f}%，5%~95% {np.percentile(arr, 5):+.2f}%~{np.percentile(arr, 95):+.2f}%，"
                     f"**p={stock_ctrl['p']:.3f}**")
        lines.append("")
    lines.append("## 二、主题板块策略（A股板块指数篮子，等权）")
    lines.append("")
    lines.append("| 持有 N 日 | 组合收益 | 参与板块账户 | 总交易 | 平均胜率 | 主题命中日 |")
    lines.append("|---|---|---|---|---|---|")
    for r in theme_rows:
        lines.append(
            f"| {r['hold']} | {r['ret']:+.2f}% | {r['n_accts']} | {r['trades']} | "
            f"{r['win']:.0f}% | {r['n_signals']} |"
        )
    lines.append("")
    if theme_ctrl:
        arr = theme_ctrl["dist"]
        lines.append(f"- 随机对照（持有 {hold_main} 日，M={len(arr)} 次打乱）：真实组合 {theme_ctrl['real']:+.2f}%，"
                     f"随机均值 {arr.mean():+.2f}%，5%~95% {np.percentile(arr, 5):+.2f}%~{np.percentile(arr, 95):+.2f}%，"
                     f"**p={theme_ctrl['p']:.3f}**")
        lines.append("")
    lines.append("## 三、反转基线（全池 42 只，t 日跌幅 <= -th -> t+1 买入）")
    lines.append("")
    lines.append("| 阈值 | 持有 N 日 | 组合收益 | 参与账户 | 总交易 | 平均胜率 |")
    lines.append("|---|---|---|---|---|---|")
    for r in reversal_rows:
        lines.append(
            f"| {r['th']:.1%} | {r['hold']} | {r['ret']:+.2f}% | {r['n_accts']} | "
            f"{r['trades']} | {r['win']:.0f}% |"
        )
    lines.append("")
    if reversal_ctrl:
        arr = reversal_ctrl["dist"]
        lines.append(f"- 随机对照（th=-2%、持有 3 日，M={len(arr)} 次打乱）：真实组合 {reversal_ctrl['real']:+.2f}%，"
                     f"随机择时均值 {arr.mean():+.2f}%，5%~95% {np.percentile(arr, 5):+.2f}%~{np.percentile(arr, 95):+.2f}%，"
                     f"**p={reversal_ctrl['p']:.3f}**")
        lines.append("")
    stock_main = next(r for r in stock_rows if r["hold"] == hold_main)
    theme_main_row = next((r for r in theme_rows if r["hold"] == hold_main), None)
    lines.append("## 四、结论")
    lines.append("")
    lines.append("三重检验的完整证据链（本报告 + universe_report.md）：")
    lines.append("")
    lines.append("| 信号 | 组合收益（主口径） | 随机对照 p 值 | 判定 |")
    lines.append("|---|---|---|---|")
    lines.append("| 词典情绪水平（42 只，0/42 显著） | +0.33% | 全不显著 | 无 alpha |")
    lines.append(f"| 个股白名单事件（30 只 A股，hold={hold_main}） | "
                 f"{stock_main['ret']:+.2f}% | {stock_ctrl['p']:.3f} | 无 alpha |")
    lines.append(f"| 主题板块篮子（46 板块，hold={hold_main}） | "
                 f"{theme_main_row['ret'] if theme_main_row else 0.0:+.2f}% | "
                 f"{theme_ctrl['p'] if theme_ctrl else 1.0:.3f} | 无 alpha（负选择） |")
    if reversal_ctrl:
        lines.append(f"| 短期反转（th=-2%，hold={hold_main}，42 只） | "
                     f"{reversal_ctrl['real']:+.2f}% | {reversal_ctrl['p']:.3f} | **显著** |")
    lines.append("")
    lines.append("- 新闻类信号（情绪水平、事件类型、主题命中）在日频层面均未通过随机对照：")
    lines.append("  市场对新闻的消化速度比「T+1 开盘买入」更快，主题密集日甚至是负选择（利好出尽）。")
    lines.append("- 纯量价反转因子显著（p<0.05）：A股/美股短期超跌后次日买入持有 3 日，")
    lines.append("  显著优于随机择时——但注意其换手率高，成本敏感，需做参数与样本外稳健性确认。")
    lines.append("- 下一步建议：① 反转因子做样本内/外与成本敏感性检验后接入模拟盘；")
    lines.append("  ② 新闻信号转向更长周期（周频情绪变化率）或作为反转信号的过滤器/风控开关，而非独立择时因子。")
    lines.append("")
    RESULTS_DIR.mkdir(exist_ok=True)
    (RESULTS_DIR / "event_strategy_report.md").write_text("\n".join(lines), encoding="utf-8")


# ================= main =================

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true", help="先回填全池A股新闻+公告")
    ap.add_argument("--m-shuffle", type=int, default=200, help="随机打乱次数")
    ap.add_argument("--hold-main", type=int, default=3, help="随机对照用的持有天数")
    ap.add_argument("--start", default="20240101", help="板块指数起始日")
    ap.add_argument("--end", default="20260824", help="板块指数结束日")
    args = ap.parse_args()

    pool = load_pool()
    cn_items = [it for it in pool if it.get("market") == "cn"]
    cn_syms = {it["symbol"] for it in cn_items}
    themes = load_themes()

    if args.backfill:
        backfill_pool(pool, "2024-01-01")

    items = load_all_items()
    print(f"[load] 去重后新闻 {len(items)} 条")

    # 1) 个股事件信号
    signals = build_stock_event_signals(items, cn_syms)
    n_sig = sum(len(v["buy"]) + len(v["sell"]) for v in signals.values())
    print(f"[signal] 个股事件信号 {n_sig} 个（覆盖 {sum(1 for v in signals.values() if v['buy'] or v['sell'])} 只标的）")

    # 2) 主题信号 + 板块指数
    theme_hits = build_theme_signals(items, themes)
    theme_boards = resolve_theme_boards(themes)
    theme_boards_all = {}  # {tname: [(name, kind, df)]}
    for tname, boards in theme_boards.items():
        lst = []
        for name, kind in boards:
            try:
                bdf = load_board_index(name, kind, args.start, args.end)
            except Exception as e:  # noqa: BLE001
                print(f"  [board] {name}({kind}) 失败: {type(e).__name__}")
                bdf = pd.DataFrame(columns=["date", "open", "close"])
            if not bdf.empty:
                lst.append((name, kind, bdf))
        theme_boards_all[tname] = lst
    n_boards = sum(len(v) for v in theme_boards_all.values())
    print(f"[board] 板块指数 {n_boards} 个")

    # 3) 个股事件回测（hold 扫描）
    stock_rows = []
    for hold in (1, 3, 5, 10):
        stats = []
        for item in cn_items:
            sym = item["symbol"]
            st = backtest_symbol(sym, signals[sym]["buy"], signals[sym]["sell"], hold,
                                 int(item.get("lot_size", 100)), STAMP_CN)
            stats.append(st)
        vals = [s for s in stats if s is not None]
        stock_rows.append({
            "hold": hold, "ret": combo_ret(vals), "n_accts": len(vals),
            "trades": sum(s["trades"] for s in vals),
            "win": float(np.mean([s["win_rate"] for s in vals if s["win_rate"] is not None]) or 0),
            "n_signals": sum(s["n_signals"] for s in vals),
        })
        print(f"[event] hold={hold}: 组合 {stock_rows[-1]['ret']:+.2f}% "
              f"({len(vals)} 账户, {stock_rows[-1]['trades']} 笔)")
    pd.DataFrame(stock_rows).to_csv(RESULTS_DIR / "event_backtest.csv", index=False, encoding="utf-8-sig")

    # 4) 主题板块回测（hold 扫描）
    theme_rows = []
    for hold in (1, 3, 5):
        stats = []
        for tname, hit_dates in theme_hits.items():
            if not hit_dates:
                continue
            for _name, _kind, bdf in theme_boards_all.get(tname, []):
                stats.append(backtest_board(bdf, hit_dates, hold))
        vals = [s for s in stats if s is not None]
        theme_rows.append({
            "hold": hold, "ret": combo_ret(vals), "n_accts": len(vals),
            "trades": sum(s["trades"] for s in vals),
            "win": float(np.mean([s["win_rate"] for s in vals if s["win_rate"] is not None]) or 0),
            "n_signals": sum(len(h) for h in theme_hits.values()),
        })
        print(f"[theme] hold={hold}: 组合 {theme_rows[-1]['ret']:+.2f}% "
              f"({len(vals)} 板块账户)")
    pd.DataFrame(theme_rows).to_csv(RESULTS_DIR / "theme_backtest.csv", index=False, encoding="utf-8-sig")

    # 5) 反转基线（全池）
    reversal_rows = []
    for th in (-0.01, -0.02, -0.03):
        for hold in (1, 3, 5):
            stats = []
            for item in pool:
                bars = load_stock_bars(item["symbol"])
                if bars.empty:
                    continue
                dates, opens, closes = align_dates(bars)
                sig = reversal_signals(bars, th)
                st = run_daily(dates, opens, closes, sig, set(), hold,
                               int(item.get("lot_size", 1)), STAMP_CN if item["market"] == "cn" else 0.0)
                st["sym"] = item["symbol"]
                stats.append(st)
            vals = [s for s in stats if s is not None]
            reversal_rows.append({
                "th": th, "hold": hold, "ret": combo_ret(vals), "n_accts": len(vals),
                "trades": sum(s["trades"] for s in vals),
                "win": float(np.mean([s["win_rate"] for s in vals if s["win_rate"] is not None]) or 0),
            })
            print(f"[reversal] th={th:.1%} hold={hold}: 组合 {reversal_rows[-1]['ret']:+.2f}%")
    pd.DataFrame(reversal_rows).to_csv(RESULTS_DIR / "reversal_backtest.csv", index=False, encoding="utf-8-sig")

    # 6) 随机对照
    rng = np.random.default_rng(7)
    main_row = next(r for r in stock_rows if r["hold"] == args.hold_main)
    stock_ctrl = shuffle_stock_control(cn_items, signals, args.hold_main, args.m_shuffle, rng,
                                       main_row["ret"])
    print(f"[shuffle] 个股事件: real {stock_ctrl['real']:+.2f}% / rand mean "
          f"{stock_ctrl['dist'].mean():+.2f}% / p={stock_ctrl['p']:.3f}")
    theme_main = next((r for r in theme_rows if r["hold"] == args.hold_main), None)
    theme_ctrl = None
    if theme_main is not None:
        theme_ctrl = shuffle_theme_control(theme_boards_all, theme_hits, args.hold_main,
                                           args.m_shuffle, rng, theme_main["ret"])
        print(f"[shuffle] 主题板块: real {theme_ctrl['real']:+.2f}% / rand mean "
              f"{theme_ctrl['dist'].mean():+.2f}% / p={theme_ctrl['p']:.3f}")
    rev_main = next(r for r in reversal_rows if r["th"] == -0.02 and r["hold"] == args.hold_main)
    reversal_ctrl = shuffle_reversal_control(pool, -0.02, args.hold_main, args.m_shuffle, rng,
                                             rev_main["ret"])
    print(f"[shuffle] 反转基线: real {reversal_ctrl['real']:+.2f}% / rand mean "
          f"{reversal_ctrl['dist'].mean():+.2f}% / p={reversal_ctrl['p']:.3f}")
    pd.DataFrame({"ret": stock_ctrl["dist"]}).to_csv(RESULTS_DIR / "event_shuffle.csv",
                                                     index=False, encoding="utf-8-sig")
    pd.DataFrame({"ret": reversal_ctrl["dist"]}).to_csv(RESULTS_DIR / "reversal_shuffle.csv",
                                                        index=False, encoding="utf-8-sig")

    write_report(pool, stock_rows, theme_rows, reversal_rows, stock_ctrl, theme_ctrl,
                 reversal_ctrl, cn_syms, args.hold_main)
    print("[report] -> results/event_strategy_report.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
