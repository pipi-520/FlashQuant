"""事件类型 alpha 扫描：不同事件类型的次日收益是否有方向性。

情绪方向已被验证无 alpha（命中率 <50%）。本脚本改用「事件类型」作为信号：
- 事件类型自带方向（财报预增=利好、减持=利空…），无需情绪分。
- 对每个事件类型，统计「事件方向 vs 次日实际涨跌」的命中率与平均次日收益。
- 找出真正有 alpha 的类型（利好类型次日平均涨 / 利空类型次日平均跌）。

用法（项目根目录）：
    .venv/Scripts/python.exe scripts/event_type_study.py
"""

import glob
import json
import pathlib
import sys
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

NEWS_DIR = ROOT / "news"
DATA_DIR = ROOT / "data"

# 事件类型 -> (方向, 关键词列表)。顺序即优先级（一条新闻归入第一个命中的类型）。
EVENT_TYPES = [
    ("财报预增", "利好", ["预增", "业绩预增", "净利润增长", "同比增长", "扭亏", "超预期", "创新高"]),
    ("财报预减", "利空", ["预减", "业绩预减", "净利润下降", "同比下滑", "亏损", "不及预期", "爆雷"]),
    ("减持", "利空", ["减持"]),
    ("处罚诉讼", "利空", ["处罚", "立案", "违规", "诉讼", "调查", "冻结", "质押"]),
    ("增持回购", "利好", ["增持", "回购"]),
    ("中标订单", "利好", ["中标", "签约", "订单", "中标候选人", "合同"]),
    ("获批", "利好", ["获批", "批准", "FDA", "上市许可", "临床成功"]),
    ("涨价提价", "利好", ["涨价", "提价", "上调价格", "价格上调"]),
    ("分红", "利好", ["分红", "派息", "送转"]),
]


def load_news() -> list:
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


def classify(text: str) -> tuple[str, str] | None:
    """按优先级返回 (事件类型, 方向)，未命中返回 None。"""
    for typ, direction, kws in EVENT_TYPES:
        for kw in kws:
            if kw in text:
                return typ, direction
    return None


def load_bars(symbol: str) -> dict:
    p = DATA_DIR / f"bars_{symbol}.csv"
    if not p.exists():
        return {}
    bars = pd.read_csv(p, encoding="utf-8-sig")
    bars["date"] = pd.to_datetime(bars["date"]).dt.strftime("%Y-%m-%d")
    bars = bars.sort_values("date")
    return dict(zip(bars["date"], bars["close"]))


def next_day_ret(closes: dict, dates_sorted: list, news_date: str) -> float | None:
    import bisect
    i = bisect.bisect_right(dates_sorted, news_date)
    if i >= len(dates_sorted):
        return None
    j = bisect.bisect_right(dates_sorted, news_date) - 1
    if j < 0:
        return None
    t_close = closes[dates_sorted[j]]
    t1_close = closes[dates_sorted[i]]
    if not t_close:
        return None
    return (float(t1_close) / float(t_close) - 1.0) * 100


def main() -> int:
    news = load_news()
    bars_cache, dates_cache = {}, {}

    # stats[event_type] = [direction, [rets], [hits]]
    stats = defaultdict(lambda: ["", [], []])
    for it in news:
        text = f"{it.get('title', '')} {it.get('content', '')}"
        cls = classify(text)
        if not cls:
            continue
        typ, direction = cls
        for sym in it.get("symbols", []):
            if sym not in bars_cache:
                closes = load_bars(sym)
                bars_cache[sym] = closes
                dates_cache[sym] = sorted(closes)
            closes = bars_cache[sym]
            if not closes:
                continue
            ret = next_day_ret(closes, dates_cache[sym], it.get("date") or "")
            if ret is None:
                continue
            hit = (direction == "利好" and ret > 0) or (direction == "利空" and ret < 0)
            stats[typ][0] = direction
            stats[typ][1].append(ret)
            stats[typ][2].append(1 if hit else 0)

    print("=" * 78)
    print("事件类型 alpha 扫描（事件自带方向 vs 次日实际涨跌）")
    print("=" * 78)
    print(f"{'事件类型':<10} {'方向':<4} {'样本':>5} {'命中率':>7} {'平均次日收益':>12}  判定")
    print("-" * 78)

    rows = []
    for typ, (direction, rets, hits) in stats.items():
        n = len(rets)
        if n < 10:
            continue
        hit_rate = sum(hits) / n * 100
        avg_ret = sum(rets) / n
        # 判定：利好且平均收益为正 / 利空且平均收益为负，且命中率偏离 50%
        edge = avg_ret if direction == "利好" else -avg_ret  # 正 = 有 alpha
        if direction == "利好" and avg_ret > 0.3:
            verdict = "★ 利好有效"
        elif direction == "利空" and avg_ret < -0.3:
            verdict = "★ 利空有效"
        elif hit_rate > 55 or hit_rate < 45:
            verdict = "? 命中率偏离"
        else:
            verdict = "无 alpha"
        rows.append((typ, direction, n, hit_rate, avg_ret, verdict))
        print(f"{typ:<10} {direction:<4} {n:>5} {hit_rate:>6.1f}% {avg_ret:>+11.2f}%  {verdict}")

    if not rows:
        print("（无样本）")
    print("-" * 78)
    print("★ = 事件方向与次日收益一致（有 alpha），可作为过滤保留的信号；")
    print("   其余类型（尤其财报预增/预减）若无 alpha，说明情绪/事件已被 price in。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
