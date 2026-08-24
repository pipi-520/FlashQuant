"""历史新闻回填 + 重建情绪历史。

为 config.yaml 里的每个标的回填可回填的历史新闻：
- A股：东方财富个股新闻分页（免 key）
- 美股：SEC EDGAR 8-K（免 key）+ Finnhub company-news（需 FINNHUB_API_KEY，可选）

回填结果归档到 news/raw/{date}.jsonl，随后从归档重建 news/sentiment_history.json，
供 scripts/sentiment_score.py 消费，让回测用上真实历史情绪。

用法（项目根目录）：
    .venv/Scripts/python.exe scripts/backfill_news.py                 # 回填全部标的并重建历史
    .venv/Scripts/python.exe scripts/backfill_news.py --market cn     # 只回填 A股
    .venv/Scripts/python.exe scripts/backfill_news.py --market us     # 只回填美股
    .venv/Scripts/python.exe scripts/backfill_news.py --no-rebuild    # 只回填归档，不重建历史
"""

import argparse
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from news_aggregator.backfill import (  # noqa: E402
    archive,
    compact_ymd,
    fetch_em_announcements,
    fetch_em_stock_news,
    fetch_finnhub_news,
    fetch_sec_8k,
)


def load_config() -> dict:
    with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_env_file(env_path) -> None:
    """把 KEY=VALUE 行载入 os.environ（已有环境变量优先，不覆盖）。"""
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip()
                if k and not os.environ.get(k):
                    os.environ[k] = v
    except FileNotFoundError:
        pass


def finnhub_token(cfg: dict) -> str:
    """读取 Finnhub key：config.primary.finnhub_api_key -> 环境变量 FINNHUB_API_KEY。"""
    return (str((cfg.get("primary") or {}).get("finnhub_api_key") or "").strip()
            or os.environ.get("FINNHUB_API_KEY", "").strip())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["cn", "us"], default=None, help="只回填某个市场")
    ap.add_argument("--no-rebuild", action="store_true", help="只归档，不重建情绪历史")
    args = ap.parse_args()

    load_env_file(ROOT / ".env")
    cfg = load_config()
    start_ymd = compact_ymd(cfg["data"]["start_date"])
    end_ymd = compact_ymd(cfg["data"]["end_date"])
    token = finnhub_token(cfg)

    all_items = []
    for item in cfg["symbols"]:
        market = item.get("market")
        if args.market and market != args.market:
            continue
        sym = item["symbol"]
        if market == "cn":
            print(f"[backfill] A股 {item['name']}({sym}) 东财新闻分页 + 公告回填 ...")
            keyword = str(item.get("name") or item.get("code"))
            items = fetch_em_stock_news(keyword, sym, start_ymd)
            items += fetch_em_announcements(str(item["code"]), sym, start_ymd)
        else:
            print(f"[backfill] 美股 {item['name']}({sym}) SEC 8-K + Finnhub ...")
            items = fetch_sec_8k(sym, start_ymd)
            items += fetch_finnhub_news(sym, start_ymd, end_ymd, token)
        print(f"  -> {len(items)} 条")
        all_items.extend(items)

    # 跨源去重（按 id）：同一条新闻被多只股票的关键词都命中时，合并 symbols，
    # 而不是丢弃（否则会让后命中的标的丢失该条情绪数据）。
    uniq = {}
    for it in all_items:
        if it["id"] in uniq:
            merged = set(uniq[it["id"]].get("symbols") or [])
            merged.update(it.get("symbols") or [])
            uniq[it["id"]]["symbols"] = sorted(merged)
        else:
            uniq[it["id"]] = it
    uniq = list(uniq.values())

    written = archive(uniq)
    print(f"[backfill] 归档新增 {written} 条（去重后 {len(uniq)} 条）")

    if not args.no_rebuild:
        from news_aggregator.run import rebuild_history_from_raw, save_history  # noqa: E402
        h = rebuild_history_from_raw()
        save_history(h)
        print(f"[backfill] 已重建历史情绪：市场 {len(h.get('market', {}))} 天 / "
              f"个股 {len(h.get('symbols', {}))} 只")
    return 0


if __name__ == "__main__":
    sys.exit(main())
