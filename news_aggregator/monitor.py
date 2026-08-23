"""实时事件监测器：秒级轮询快讯源 -> 事件词匹配 -> 影响分排序 -> 板块/个股映射 -> 多通道告警。

用法（在项目根目录执行）：
    .venv/Scripts/python.exe news_aggregator/monitor.py               # 常驻轮询
    .venv/Scripts/python.exe news_aggregator/monitor.py --once        # 只跑一轮即退（测试/定时）
    .venv/Scripts/python.exe news_aggregator/monitor.py --dry-run     # 只检测不推送
    .venv/Scripts/python.exe news_aggregator/monitor.py --interval 10  # 自定义轮询秒数
    .venv/Scripts/python.exe news_aggregator/monitor.py --no-boards    # 跳过板块缓存（更快/离线）

说明：
- 复用 news_aggregator/fetchers.py 的多源快讯、sentiment.py 情绪打分、
  impact.py 影响分排序、push.py 推送。
- seen.json 持久化去重：重启不重复告警；首次运行只建立基线、不告警历史旧闻。
- 新增：按影响分降序告警，低于 monitor.impact_min 的主题告警会被过滤。
- 只告警，不自动下单。
"""

import argparse
import json
import pathlib
import sys
import time

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from news_aggregator.fetchers import SOURCES, filter_recent, apply_primary_keys, load_env_file  # noqa: E402
from news_aggregator.sentiment import score_text, configure_backend  # noqa: E402
from news_aggregator.push import push_alert  # noqa: E402
from news_aggregator.run import compute_daily, load_history, save_history, upsert_history  # noqa: E402
from news_aggregator.boards import get_board_cache  # noqa: E402
from news_aggregator.impact import keyword_match, match_themes, compute_impact  # noqa: E402

# Windows 控制台编码兼容（避免 emoji 等字符导致 print 崩溃）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

NEWS_DIR = ROOT / "news"
SEEN_PATH = NEWS_DIR / "seen.json"
SEEN_MAX = 20000  # 只保留最近 N 条 id，防无限增长


def load_config() -> dict:
    with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_themes(path: str) -> list:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return (data or {}).get("themes") or []


def load_seen() -> set:
    if SEEN_PATH.exists():
        try:
            with open(SEEN_PATH, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except (json.JSONDecodeError, OSError):
            return set()
    return set()


def save_seen(seen: set) -> None:
    NEWS_DIR.mkdir(exist_ok=True)
    # 只保留最近 SEEN_MAX 条
    lst = list(seen)[-SEEN_MAX:]
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(lst, f, ensure_ascii=False)


def fetch_new_items(enabled_sources: list | None) -> list:
    """逐源抓取，返回 id 未在 seen 中的新条目；单源失败不中断。"""
    items = []
    for name, fn in SOURCES:
        if enabled_sources and name not in enabled_sources:
            continue
        try:
            got = fn() or []
        except Exception as e:  # noqa: BLE001
            print(f"[monitor] {name}: 失败 ({type(e).__name__})")
            continue
        items.extend(got)
    return items


def append_raw(items: list) -> None:
    """按日期追加写原始新闻（幂等：跳过文件里已存在的 id）。"""
    raw_dir = NEWS_DIR / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    by_day = {}
    for it in items:
        by_day.setdefault(it.get("date", "unknown"), []).append(it)
    for day, lst in by_day.items():
        path = raw_dir / f"{day.replace('-', '')}.jsonl"
        existing = set()
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    existing.add(json.loads(line).get("id"))
                except json.JSONDecodeError:
                    continue
        with open(path, "a", encoding="utf-8") as f:
            for it in lst:
                if it.get("id") in existing:
                    continue
                f.write(json.dumps(it, ensure_ascii=False) + "\n")


def update_history(items: list) -> None:
    """把新条目并入历史情绪库（同日覆盖）。"""
    if not items:
        return
    market, sym_out = compute_daily(items)
    h = load_history()
    h = upsert_history(h, market, sym_out)
    save_history(h)


def fmt_boards(theme, hits, boards, theme_boards, top_n: int) -> list:
    """组装板块展示行。"""
    lines = []
    for bname in (theme_boards or {}).get(theme["name"], []):
        info = (boards or {}).get(bname)
        cons = (info or {}).get("constituents") or []
        if cons:
            detail = "、".join(f"{c['name']} {c['pct']:+.1f}%" for c in cons[:top_n])
        else:
            detail = "(成分股未获取)"
        kind = (info or {}).get("kind") or "concept"
        kind_cn = "概念" if kind == "concept" else "行业"
        lines.append(f"- {bname}({kind_cn})：{detail}")
    return lines


def build_alert(item: dict, score: float, theme: dict, hits: list,
                boards: dict, theme_boards: dict, top_n: int) -> tuple[str, str]:
    title = f"[事件告警] {theme['name']}"
    src = item.get("source") or ""
    ts = item.get("ts") or item.get("date") or ""
    text = (item.get("title") or item.get("content") or "").strip()
    if len(text) > 160:
        text = text[:160] + "…"

    lines = []
    lines.append(f"**主题**：{theme['name']}")
    lines.append(f"**命中关键词**：{' / '.join(hits)}")
    lines.append(f"**影响分**：{item.get('impact', 0.0):.3f}")
    lines.append(f"**情绪分**：{score:+.3f}")
    lines.append(f"**来源**：{src}　**时间**：{ts}")
    lines.append(f"**原文**：{text}")
    if theme.get("macro"):
        lines.append("**类型**：宏观事件（关注利率/避险相关板块）")
    bl = fmt_boards(theme, hits, boards, theme_boards, top_n)
    if bl:
        lines.append("**相关板块**：")
        lines.extend(bl)
    return title, "\n".join(lines)


def _politician_allowed(pol: str, cfg: dict) -> bool:
    watch = (cfg.get("politicians") or [])
    if not watch:
        return True
    pol = (pol or "").lower()
    return any(str(w).lower() in pol for w in watch)


def build_ticker_alert(item: dict) -> tuple[str, str]:
    kind_label = "国会交易" if item.get("kind") == "congress_trade" else "内部人交易"
    pol = item.get("politician") or "未知"
    ticker = item.get("ticker") or "-"
    title = f"[{kind_label}] {pol} {ticker}"
    lines = [
        f"**类型**：{kind_label}",
        f"**人物**：{pol}",
        f"**标的**：{ticker}",
        f"**内容**：{item.get('title') or ''}",
        f"**日期**：{item.get('date') or ''}",
        f"**来源**：{item.get('source') or ''}",
    ]
    return title, "\n".join(lines)


def run_once(cfg: dict, themes: list, seen: set, cold_start: bool,
             dry_run: bool, boards: dict, theme_boards: dict, top_n: int) -> int:
    """一轮：抓取 -> 去重 -> 影响分排序 -> 匹配 -> 告警。返回本轮新条目数。"""
    enabled = ((cfg.get("monitor") or {}).get("enabled_sources")
               or (cfg.get("news") or {}).get("enabled_sources") or None)

    all_items = fetch_new_items(enabled)
    # 去重
    uniq = {}
    for it in all_items:
        uniq.setdefault(it["id"], it)
    new_items = filter_recent([it for it in uniq.values() if it["id"] not in seen], days=30)

    if cold_start and new_items:
        print(f"[monitor] 首次运行：仅建立基线，跳过 {len(new_items)} 条历史消息，不告警")
        for it in new_items:
            seen.add(it["id"])
        save_seen(seen)
        append_raw(new_items)
        return len(new_items)

    # 影响分排序
    imp_cfg = cfg.get("impact") or {}
    weights = imp_cfg.get("weights") or None
    window_minutes = int(imp_cfg.get("window_minutes", 60))
    burst_cap = int(imp_cfg.get("burst_cap", 4))
    impact_min = float((cfg.get("monitor") or {}).get("impact_min", 0.0))
    new_items = compute_impact(new_items, themes, weights, window_minutes, burst_cap)

    alerts = 0
    for it in new_items:
        seen.add(it["id"])
        # 另类数据（国会/内部人交易）→ ticker 告警，不参与主题词匹配
        kind = it.get("kind")
        if kind in ("congress_trade", "insider_trade") and it.get("ticker"):
            if _politician_allowed(it.get("politician"), cfg):
                alerts += 1
                title, content = build_ticker_alert(it)
                print("\n" + "=" * 60)
                print(title)
                print(content)
                print("=" * 60 + "\n")
                if not dry_run:
                    push_alert(cfg, title, content)
            continue
        text = f"{it.get('title', '')} {it.get('content', '')}"
        score = it.get("sentiment", score_text(text))
        matched = match_themes(text, themes)
        for theme, hits in matched:
            imp = it.get("impact", 0.0)
            if impact_min > 0 and imp < impact_min:
                continue
            alerts += 1
            title, content = build_alert(it, score, theme, hits, boards, theme_boards, top_n)
            print("\n" + "=" * 60)
            print(title)
            print(content)
            print("=" * 60 + "\n")
            if not dry_run:
                push_alert(cfg, title, content)

    if new_items:
        append_raw(new_items)
        update_history(new_items)
        save_seen(seen)
        print(f"[monitor] 本轮新增 {len(new_items)} 条，命中告警 {alerts} 条"
              f"{'（dry-run 未推送）' if dry_run else ''}")
    else:
        print("[monitor] 本轮无新增")
    return len(new_items)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只跑一轮即退")
    ap.add_argument("--interval", type=int, default=None, help="轮询秒数")
    ap.add_argument("--dry-run", action="store_true", help="只检测不推送")
    ap.add_argument("--no-boards", action="store_true", help="跳过板块缓存构建")
    args = ap.parse_args()

    load_env_file(ROOT / ".env")
    cfg = load_config()
    apply_primary_keys(cfg.get("primary") or {})
    configure_backend(cfg)
    mon = cfg.get("monitor") or {}
    interval = args.interval or int(mon.get("poll_interval", 15))
    top_n = int(mon.get("top_constituents", 5))
    refresh_hours = int(mon.get("board_cache_refresh_hours", 24))
    impact_min = float(mon.get("impact_min", 0.0))
    themes_path = mon.get("themes_path", "news_aggregator/themes.yaml")
    if not pathlib.Path(themes_path).is_absolute():
        themes_path = str(ROOT / themes_path)

    themes = load_themes(themes_path)
    print(f"[monitor] 载入主题 {len(themes)} 个，impact_min={impact_min}")

    seen = load_seen()
    cold_start = not SEEN_PATH.exists()

    boards, theme_boards = {}, {}
    if not args.no_boards:
        try:
            theme_names = {
                th["name"]: {
                    "concept_boards": th.get("concept_boards") or [],
                    "industry_boards": th.get("industry_boards") or [],
                }
                for th in themes
            }
            boards, theme_boards = get_board_cache(theme_names, top_n, refresh_hours)
        except Exception as e:  # noqa: BLE001
            print(f"[monitor] 板块缓存不可用（不影响告警）: {type(e).__name__}")

    print(f"[monitor] 启动：interval={interval}s dry_run={args.dry_run} "
          f"cold_start={cold_start} boards={len(boards)}")

    if args.once:
        run_once(cfg, themes, seen, cold_start, args.dry_run, boards, theme_boards, top_n)
        return 0

    first = True
    while True:
        try:
            run_once(cfg, themes, seen, cold_start and first, args.dry_run, boards, theme_boards, top_n)
            first = False
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\n[monitor] 收到中断，保存状态后退出")
            save_seen(seen)
            return 0
        except Exception as e:  # noqa: BLE001
            print(f"[monitor] 本轮异常: {type(e).__name__}: {e}")
            time.sleep(max(interval, 5))


if __name__ == "__main__":
    sys.exit(main())


