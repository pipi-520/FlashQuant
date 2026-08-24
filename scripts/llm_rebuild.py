"""基于 LLM 情绪分（news/llm_sentiment.json）重建情绪历史。

与 news_aggregator.run.rebuild_history_from_raw 逻辑一致，区别在于：
每条新闻的情绪分优先取 llm_sentiment.json（LLM 打分），缺失时回退词典。
产出 news/sentiment_history.json，供 scripts/sentiment_score.py 消费。

用法（项目根目录）：
    .venv/Scripts/python.exe scripts/llm_rebuild.py
"""

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from news_aggregator.sentiment import score_text  # noqa: E402（词典兜底）

NEWS_DIR = ROOT / "news"


def load_llm_scores() -> dict:
    p = NEWS_DIR / "llm_sentiment.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def compute_daily(items: list, llm_scores: dict) -> tuple[dict, dict]:
    market: dict = {}
    per_sym: dict = {}
    n_llm = n_dict = 0
    for it in items:
        if it.get("kind") != "news":
            continue
        d = it["date"]
        sc = llm_scores.get(it["id"])
        if sc is None:
            sc = score_text(f"{it.get('title', '')} {it.get('content', '')}")
            n_dict += 1
        else:
            n_llm += 1
        market.setdefault(d, []).append(sc)
        for s in it.get("symbols", []):
            per_sym.setdefault(s, {}).setdefault(d, []).append(sc)
    market_out = {d: round(sum(v) / len(v), 4) for d, v in market.items()}
    sym_out = {
        s: {d: round(sum(v) / len(v), 4) for d, v in dd.items()}
        for s, dd in per_sym.items()
    }
    return market_out, sym_out, n_llm, n_dict


def main() -> int:
    llm_scores = load_llm_scores()
    print(f"LLM 情绪分 {len(llm_scores)} 条")
    h = {"market": {}, "symbols": {}}
    total_llm = total_dict = 0
    for path in sorted(NEWS_DIR.glob("raw/*.jsonl")):
        items = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if not items:
            continue
        seen, uniq = set(), []
        for it in items:
            if it["id"] in seen:
                continue
            seen.add(it["id"])
            uniq.append(it)
        market, sym_out, n_llm, n_dict = compute_daily(uniq, llm_scores)
        total_llm += n_llm
        total_dict += n_dict
        for d, v in market.items():
            h.setdefault("market", {})[d] = v
        for s, dd in sym_out.items():
            for d, v in dd.items():
                h.setdefault("symbols", {}).setdefault(s, {})[d] = v

    (NEWS_DIR / "sentiment_history.json").write_text(
        json.dumps(h, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"重建完成：市场 {len(h.get('market', {}))} 天 / 个股 {len(h.get('symbols', {}))} 只"
          f"（LLM 打分 {total_llm} 条，词典兜底 {total_dict} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
