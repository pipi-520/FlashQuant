"""多源新闻/舆情聚合器主入口。

用法（在项目根目录执行）：
    .venv/Scripts/python.exe news_aggregator/run.py [--date YYYYMMDD] [--push] [--rebuild-history]

输出：
    news/raw/{date}.jsonl            原始新闻（每条一行 JSON，含 impact/sentiment）
    news/daily_sentiment.json        当日情绪分（市场 + 个股，快照）
    news/sentiment_history.json      历史情绪分（按日期累积，可回测）
    news/report/{date}.md            日报
"""

import argparse
import json
import pathlib
import sys
from datetime import date, datetime

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from news_aggregator.fetchers import SOURCES, fetch_symbol_news, filter_recent, apply_primary_keys, load_env_file  # noqa: E402
from news_aggregator.sentiment import score_text, configure_backend  # noqa: E402
from news_aggregator.tagger import tag  # noqa: E402
from news_aggregator.push import push_alert  # noqa: E402
from news_aggregator.impact import compute_impact, DEFAULT_WEIGHTS  # noqa: E402

# Windows 控制台编码兼容
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(errors='replace')

NEWS_DIR = ROOT / "news"
HISTORY_PATH = NEWS_DIR / "sentiment_history.json"


def load_config() -> dict:
    with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_themes(path: str) -> list:
    if not pathlib.Path(path).is_absolute():
        path = str(ROOT / path)
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return (data or {}).get("themes") or []


def fetch_all(enabled=None):
    """逐源抓取，单源失败不中断。返回 (items, stats)。"""
    items = []
    stats = {}
    for name, fn in SOURCES:
        if enabled and name not in enabled:
            continue
        try:
            got = fn() or []
            items.extend(got)
            stats[name] = len(got)
            print(f"[fetch] {name}: {len(got)} 条")
        except Exception as e:  # noqa: BLE001
            stats[name] = -1
            print(f"[fetch] {name}: 失败 ({type(e).__name__})")
    return items, stats


def dedupe(items):
    seen = set()
    out = []
    for it in items:
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        out.append(it)
    return out


def compute_daily(items):
    market = {}
    per_sym = {}
    for it in items:
        # 英文新闻同样纳入情绪（只排除非新闻类型：国会/内部人交易等）
        if it.get("kind") != "news":
            continue
        d = it["date"]
        sc = score_text(f"{it.get('title', '')} {it.get('content', '')}")
        market.setdefault(d, []).append(sc)
        for s in it.get("symbols", []):
            per_sym.setdefault(s, {}).setdefault(d, []).append(sc)
    market_out = {d: round(sum(v) / len(v), 4) for d, v in market.items()}
    sym_out = {
        s: {d: round(sum(v) / len(v), 4) for d, v in dd.items()}
        for s, dd in per_sym.items()
    }
    return market_out, sym_out


def load_history() -> dict:
    if HISTORY_PATH.exists():
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"market": {}, "symbols": {}}


def save_history(h: dict) -> None:
    NEWS_DIR.mkdir(exist_ok=True)
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False, indent=2)


def upsert_history(h: dict, market: dict, sym_out: dict) -> dict:
    """把一次聚合结果按日期并入历史（同日覆盖更新）。"""
    for d, v in market.items():
        h.setdefault("market", {})[d] = v
    for s, dd in sym_out.items():
        for d, v in dd.items():
            h.setdefault("symbols", {}).setdefault(s, {})[d] = v
    return h


def rebuild_history_from_raw() -> dict:
    """从 news/raw/*.jsonl 全量重算历史情绪（源数据为准）。"""
    h = {"market": {}, "symbols": {}}
    raw_dir = NEWS_DIR / "raw"
    if not raw_dir.exists():
        return h
    for path in sorted(raw_dir.glob("*.jsonl")):
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
        items = dedupe(items)
        market, sym_out = compute_daily(items)
        upsert_history(h, market, sym_out)
        print(f"[history] 重算 {path.name}: 市场 {len(market)} 天 / 个股 {len(sym_out)} 只")
    return h


def build_report(day: str, items, stats, market, sym_out) -> str:
    lines = []
    lines.append(f"# 财经新闻舆情日报 {day}")
    lines.append("")
    lines.append(f"> 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
    lines.append("> 数据来源：多源快讯 + 英文一手源（按影响分排序）")
    lines.append("")
    lines.append("## 数据源统计")
    lines.append("")
    lines.append("| 来源 | 条数 |")
    lines.append("|---|---|")
    for name, n in stats.items():
        lines.append(f"| {name} | {n if n >= 0 else '失败'} |")
    lines.append("")
    lines.append(f"**去重后总计：{len(items)} 条**")
    lines.append("")

    # 市场情绪
    lines.append("## 市场情绪（最近）")
    lines.append("")
    if market:
        lines.append("| 日期 | 情绪分 |")
        lines.append("|---|---|")
        for d in sorted(market, reverse=True)[:5]:
            lines.append(f"| {d} | {market[d]:+.3f} |")
    else:
        lines.append("（无数据）")
    lines.append("")

    # 个股情绪
    if sym_out:
        lines.append("## 个股情绪（最新）")
        lines.append("")
        lines.append("| 股票 | 最新情绪分 |")
        lines.append("|---|---|")
        for s, dd in sorted(sym_out.items()):
            latest = sorted(dd)[-1] if dd else "-"
            lines.append(f"| {s} | {dd[latest]:+.3f} ({latest}) |")
        lines.append("")

    # 影响最强新闻（按影响分排序，已在 main 中计算好）
    ranked = sorted(items, key=lambda x: x.get("impact", 0.0), reverse=True)
    lines.append("## 影响最强新闻 Top 15")
    lines.append("")
    lines.append("| 影响分 | 情绪 | 来源 | 标题 |")
    lines.append("|---|---|---|---|")
    for it in ranked[:15]:
        text = (it.get("title") or it.get("content") or "").strip()
        text = text[:60].replace("|", "\\|") + ("…" if len(text) > 60 else "")
        imp = it.get("impact", 0.0)
        sc = it.get("sentiment", score_text(f"{it.get('title', '')} {it.get('content', '')}"))
        lines.append(f"| {imp:.3f} | {sc:+.2f} | {it.get('source')} | {text} |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().strftime("%Y%m%d"))
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--rebuild-history", action="store_true",
                    help="从 news/raw/*.jsonl 全量重算历史情绪，不重新抓取")
    args = ap.parse_args()

    if args.rebuild_history:
        h = rebuild_history_from_raw()
        save_history(h)
        n_mkt = len(h.get("market", {}))
        n_sym = len(h.get("symbols", {}))
        print(f"[history] 已重建: 市场 {n_mkt} 天 / 个股 {n_sym} 只 -> {HISTORY_PATH}")
        return 0

    load_env_file(ROOT / ".env")
    cfg = load_config()
    apply_primary_keys(cfg.get("primary") or {})
    configure_backend(cfg)
    enabled = (cfg.get("news") or {}).get("enabled_sources") or None
    impact_cfg = (cfg.get("impact") or {})
    weights = impact_cfg.get("weights") or DEFAULT_WEIGHTS
    window_minutes = int(impact_cfg.get("window_minutes", 60))
    burst_cap = int(impact_cfg.get("burst_cap", 4))
    themes_path = impact_cfg.get("themes_path", "news_aggregator/themes.yaml")

    themes = load_themes(themes_path)

    items, stats = fetch_all(enabled)

    # 个股新闻（为每只 A股抓取并预打标签）
    try:
        sym_items = fetch_symbol_news(cfg["symbols"])
        stats["个股新闻"] = len(sym_items)
        items.extend(sym_items)
        print(f"[fetch] 个股新闻: {len(sym_items)} 条")
    except Exception as e:  # noqa: BLE001
        stats["个股新闻"] = -1
        print(f"[fetch] 个股新闻: 失败 ({type(e).__name__})")

    items = dedupe(items)
    items = filter_recent(items, days=30)
    items = tag(items, cfg["symbols"])
    items = compute_impact(items, themes, weights, window_minutes, burst_cap)
    print(f"[agg] 去重后 {len(items)} 条，已按影响分排序")

    NEWS_DIR.mkdir(exist_ok=True)
    raw_dir = NEWS_DIR / "raw"
    report_dir = NEWS_DIR / "report"
    raw_dir.mkdir(exist_ok=True)
    report_dir.mkdir(exist_ok=True)

    raw_path = raw_dir / f"{args.date}.jsonl"
    with open(raw_path, "w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")

    market, sym_out = compute_daily(items)
    daily = {
        "generated_at": datetime.now().isoformat(),
        "date": args.date,
        "sources": {k: v for k, v in stats.items()},
        "market": market,
        "symbols": sym_out,
    }
    with open(NEWS_DIR / "daily_sentiment.json", "w", encoding="utf-8") as f:
        json.dump(daily, f, ensure_ascii=False, indent=2)

    # 累积历史情绪
    history = load_history()
    history = upsert_history(history, market, sym_out)
    save_history(history)
    print(f"[history] 累积历史 -> {HISTORY_PATH}"
          f"（市场 {len(history.get('market', {}))} 天 / 个股 {len(history.get('symbols', {}))} 只）")

    md = build_report(args.date, items, stats, market, sym_out)
    report_path = report_dir / f"{args.date}.md"
    report_path.write_text(md, encoding="utf-8")
    print(f"[agg] raw -> {raw_path}")
    print(f"[agg] report -> {report_path}")
    print(f"[agg] daily_sentiment -> {NEWS_DIR / 'daily_sentiment.json'}")

    if args.push:
        push_alert(cfg, "财经新闻舆情日报", md)

    return 0


if __name__ == "__main__":
    sys.exit(main())


