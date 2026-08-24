"""情绪后端对比：词典 vs LLM，谁更能预测财报后漂移。

在财报事件（美股 8-K Item 2.02 的新闻稿）上，分别用 lexicon 与 LLM 打分，
计算各自与「财报后 T+1→T+5 漂移」的相关系数。若 LLM 相关性显著更高，
说明「升级情绪模型」有价值；若两者都接近 0，说明情绪信号本身无 alpha。

用法（项目根目录）：
    .venv/Scripts/python.exe scripts/sentiment_backend_compare.py
"""

import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 加载 .env
for line in open(ROOT / ".env", encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

from scripts import earnings_study as es  # noqa: E402
from news_aggregator.sentiment import (  # noqa: E402
    configure_backend,
    score_text,
    _lexicon_score,
)


def load_earnings_content(ticker: str) -> dict:
    """返回 {date: content}，content 为财报新闻稿文本。"""
    out = {}
    for f in ROOT.glob("news/raw/*.jsonl"):
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                it = json.loads(line)
            except json.JSONDecodeError:
                continue
            if it.get("source") != "SEC 8-K":
                continue
            if ticker not in (it.get("symbols") or []):
                continue
            title = it.get("title") or ""
            content = (it.get("content") or "").lower()
            if "2.02" in title or ("quarter" in content and "financial results" in content):
                out[it["date"]] = (it.get("content") or title)[:800]
    return out


def main() -> int:
    configure_backend({"sentiment": {"backend": "llm"}})

    # 收集财报事件 + 漂移
    events = []  # [(ticker, date, drift, lex, llm)]
    for ticker in ("AAPL", "MSFT"):
        bars = es.load_bars(ticker)
        dates = bars["date"].tolist()
        contents = load_earnings_content(ticker)
        for ed, text in sorted(contents.items()):
            after = [i for i, d in enumerate(dates) if d > ed]
            if len(after) < 5:
                continue
            t1_open = float(bars.loc[after[0], "open"])
            t5_close = float(bars.loc[after[4], "close"])
            drift = (t5_close / t1_open - 1.0) * 100
            lex = _lexicon_score(text)
            llm = score_text(text)
            events.append((ticker, ed, drift, lex, llm))
            print(f"{ticker} {ed}: drift={drift:+.2f}% lex={lex:+.2f} llm={llm:+.2f}")

    # 相关系数
    def pearson(xs, ys):
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        vx = (sum((x - mx) ** 2 for x in xs)) ** 0.5
        vy = (sum((y - my) ** 2 for y in ys)) ** 0.5
        return cov / (vx * vy) if vx and vy else 0.0

    drifts = [e[2] for e in events]
    lexs = [e[3] for e in events]
    llms = [e[4] for e in events]
    print("\n样本数:", len(events))
    print(f"词典分 vs 漂移 相关系数: {pearson(lexs, drifts):+.3f}")
    print(f"LLM 分 vs 漂移 相关系数: {pearson(llms, drifts):+.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
