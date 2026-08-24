"""个股级方向命中率验证：新闻情绪方向 -> 次日涨跌。

用历史个股新闻（已带 symbols 标签）验证「情绪方向能否预测次日涨跌」，量化
匹配/预测的正确率，作为后续优化的评估基准（评估-迭代闭环）。

方法：
- 读 news/raw，取 kind=news 且 symbols 非空的条目（个股新闻/公告/8-K/Finnhub）。
- 情绪分：词典（score_text）或 LLM（news/llm_sentiment.json）。
- 方向：score >= +th 利好，<= -th 利空，其余中性（跳过）。
- 次日收益：新闻日 T 之后第一个交易日，T 收盘 -> T+1 收盘（含隔夜跳空）。
- 命中：利好且涨 / 利空且跌。
- 基准：随机方向命中率应为 50%；显著高于 50% 说明有预测力。

用法（项目根目录）：
    .venv/Scripts/python.exe scripts/stock_impact_validate.py
    .venv/Scripts/python.exe scripts/stock_impact_validate.py --backend llm --th 0.2
"""

import argparse
import glob
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from news_aggregator.sentiment import score_text  # noqa: E402

NEWS_DIR = ROOT / "news"
DATA_DIR = ROOT / "data"


def load_news() -> list:
    """读历史新闻（去重，只取带 symbols 的 news）。"""
    items = {}
    for f in glob.glob(str(NEWS_DIR / "raw" / "*.jsonl")):
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                it = json.loads(line)
            except json.JSONDecodeError:
                continue
            if it.get("kind") != "news":
                continue
            if not it.get("symbols"):
                continue
            items[it["id"]] = it
    return sorted(items.values(), key=lambda x: (x.get("date") or "", x.get("id") or ""))


def load_llm_scores() -> dict:
    p = NEWS_DIR / "llm_sentiment.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def load_bars(symbol: str) -> dict:
    """读 data/bars_{symbol}.csv，返回 {date_str: close}。"""
    p = DATA_DIR / f"bars_{symbol}.csv"
    if not p.exists():
        return {}
    bars = pd.read_csv(p, encoding="utf-8-sig")
    bars["date"] = pd.to_datetime(bars["date"]).dt.strftime("%Y-%m-%d")
    bars = bars.sort_values("date")
    return dict(zip(bars["date"], bars["close"]))


def next_day_ret(closes: dict, dates_sorted: list, news_date: str) -> float | None:
    """新闻日 T 之后第一个交易日的收益：T 收盘 -> T+1 收盘。"""
    import bisect
    i = bisect.bisect_right(dates_sorted, news_date)
    if i >= len(dates_sorted):
        return None
    t_close = closes.get(news_date)  # 新闻日当天收盘（若新闻日非交易日则用前一交易日）
    if t_close is None:
        # 新闻日可能是周末，取其前最后一个交易日收盘
        j = bisect.bisect_right(dates_sorted, news_date) - 1
        if j < 0:
            return None
        t_close = closes[dates_sorted[j]]
    t1_close = closes[dates_sorted[i]]
    if not t_close:
        return None
    return (float(t1_close) / float(t_close) - 1.0) * 100


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["dict", "llm"], default="dict")
    ap.add_argument("--th", type=float, default=0.1, help="方向阈值（|score|>=th 才计方向）")
    ap.add_argument("--window", choices=["next_day"], default="next_day")
    args = ap.parse_args()

    llm_scores = load_llm_scores()
    news = load_news()
    bars_cache = {}
    dates_cache = {}

    stats = []  # (symbol, direction, ret, hit)
    skipped_no_bars = 0
    skipped_neutral = 0

    for it in news:
        d = it.get("date") or ""
        text = f"{it.get('title', '')} {it.get('content', '')}"
        sc = llm_scores.get(it["id"]) if args.backend == "llm" else score_text(text)
        if sc is None:
            sc = score_text(text)
        if abs(sc) < args.th:
            skipped_neutral += 1
            continue
        direction = "利好" if sc > 0 else "利空"
        for sym in it.get("symbols", []):
            if sym not in bars_cache:
                closes = load_bars(sym)
                bars_cache[sym] = closes
                dates_cache[sym] = sorted(closes)
            closes = bars_cache[sym]
            if not closes:
                skipped_no_bars += 1
                continue
            ret = next_day_ret(closes, dates_cache[sym], d)
            if ret is None:
                continue
            hit = (direction == "利好" and ret > 0) or (direction == "利空" and ret < 0)
            stats.append((sym, direction, ret, hit))

    n = len(stats)
    if n == 0:
        print("无有效样本（检查 news/raw 与 data/bars_*.csv）")
        return 1

    pos = [s for s in stats if s[1] == "利好"]
    neg = [s for s in stats if s[1] == "利空"]
    hit_rate = sum(1 for s in stats if s[3]) / n * 100

    def group_rate(rows):
        return (sum(1 for s in rows if s[3]) / len(rows) * 100) if rows else 0.0

    def group_ret(rows):
        return (sum(s[2] for s in rows) / len(rows)) if rows else 0.0

    print("=" * 62)
    print(f"个股级方向命中率验证（backend={args.backend}, 阈值={args.th}）")
    print("=" * 62)
    print(f"总样本（新闻->股票）: {n}")
    print(f"  利好样本: {len(pos)}，命中率 {group_rate(pos):.1f}%，平均次日收益 {group_ret(pos):+.2f}%")
    print(f"  利空样本: {len(neg)}，命中率 {group_rate(neg):.1f}%，平均次日收益 {group_ret(neg):+.2f}%")
    print(f"  整体命中率: {hit_rate:.1f}%  （随机基准 50%）")
    print(f"  跳过（中性）: {skipped_neutral}，跳过（无行情）: {skipped_no_bars}")
    print("-" * 62)
    print("解读：命中率显著高于 50% 才说明情绪方向有预测力；")
    print("      若利好组平均收益为正、利空组为负，且各自命中率>50%，则方向信号有效。")

    # 按股票分组（前 8 只样本最多的）
    from collections import Counter
    by_sym = Counter(s[0] for s in stats)
    print("\n按股票分组（样本数 top 8）:")
    for sym, cnt in by_sym.most_common(8):
        rows = [s for s in stats if s[0] == sym]
        print(f"  {sym}: {cnt} 条, 命中率 {group_rate(rows):.1f}%, 平均次日收益 {group_ret(rows):+.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
