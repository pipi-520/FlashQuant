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
from datetime import datetime

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from news_aggregator.fetchers import SOURCES, filter_recent, apply_primary_keys, load_env_file  # noqa: E402
from news_aggregator.sentiment import score_text, configure_backend  # noqa: E402
from news_aggregator.push import push_alert  # noqa: E402
from news_aggregator.run import compute_daily, dedupe, load_history, save_history, upsert_history, append_items_by_date  # noqa: E402
from news_aggregator.boards import get_board_cache  # noqa: E402
from news_aggregator.impact import keyword_match, match_themes, compute_impact  # noqa: E402
from news_aggregator.stock_impact import map_news_to_stocks  # noqa: E402
from news_aggregator.event_filter import classify as classify_event  # noqa: E402
from news_aggregator.event_filter import volatility_ref  # noqa: E402

# Windows 控制台编码兼容（避免 emoji 等字符导致 print 崩溃）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

NEWS_DIR = ROOT / "news"
SEEN_PATH = NEWS_DIR / "seen.json"
ALERTS_PATH = NEWS_DIR / "alerts.jsonl"
SEEN_MAX = 20000  # 只保留最近 N 条 id，防无限增长


def log_alert(alert: dict) -> None:
    """把实际发出的告警追加到 news/alerts.jsonl（供 alert_review.py 回看次日波动）。"""
    NEWS_DIR.mkdir(exist_ok=True)
    with open(ALERTS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(alert, ensure_ascii=False) + "\n")


def load_config() -> dict:
    with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_themes(path: str) -> list:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return (data or {}).get("themes") or []


def load_seen() -> list:
    """返回按加入顺序的 id 列表（seen.json 为 JSON 数组，天然有序）。

    返回 list 而非 set：裁剪时按「先到先淘汰」保留最近 SEEN_MAX 条。
    """
    if SEEN_PATH.exists():
        try:
            with open(SEEN_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []
    return []


def save_seen(seen: list) -> None:
    NEWS_DIR.mkdir(exist_ok=True)
    # 按加入顺序保留最近 SEEN_MAX 条（旧 set 实现随机丢弃，可能丢新留旧）
    tmp = SEEN_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(seen[-SEEN_MAX:], f, ensure_ascii=False)
    tmp.replace(SEEN_PATH)  # 原子替换：写入中断不会损坏 seen.json


def fetch_new_items(enabled_sources: list | None, max_workers: int = 4,
                    timeout: float = 90.0) -> list:
    """并发抓取全部源；单个源失败/超时不中断。整体软超时后使用已返回的结果。

    旧实现串行抓 40+ 源（Google News RSS 23 次 + akshare 快讯），一轮可长达数分钟；
    并发后一轮时间 ≈ 最慢几个源的时间。超时的源在其线程内自然结束（requests 有超时）。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    jobs = [(name, fn) for name, fn in SOURCES
            if not enabled_sources or name in enabled_sources]

    def _safe(name, fn):
        try:
            return name, fn() or []
        except Exception as e:  # noqa: BLE001
            return name, None

    items = []
    ex = ThreadPoolExecutor(max_workers=max_workers)
    try:
        futs = {ex.submit(_safe, n, f): n for n, f in jobs}
        for fut in as_completed(futs, timeout=timeout):
            name, got = fut.result()
            if got is None:
                print(f"[monitor] {name}: 失败")
            else:
                items.extend(got)
    except TimeoutError:
        print(f"[monitor] 抓取超过 {timeout:.0f}s，使用已返回的 {len(items)} 条结果继续")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return items


def append_raw(items: list) -> None:
    """按日期追加写原始新闻（复用 run.append_items_by_date，保证两处归档规则一致）。"""
    append_items_by_date(items, NEWS_DIR / "raw")


def update_history(items: list) -> None:
    """把新条目并入历史情绪库。

    注意：不能用「本轮增量」的均值直接覆盖当日值——每轮增量只是全天的一部分，
    会让历史情绪被最后一批增量稀释失真。正确做法：先归档（调用方已 append_raw），
    再从 raw 文件重算受影响日期的全部新闻。
    """
    if not items:
        return
    days = sorted({str(it.get("date") or "").replace("-", "") for it in items})
    days = [d for d in days if d and d != "unknown"]
    if not days:
        return
    h = load_history()
    raw_dir = NEWS_DIR / "raw"
    for day in days:
        path = raw_dir / f"{day}.jsonl"
        if not path.exists():
            continue
        day_items = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                day_items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if not day_items:
            continue
        market, sym_out = compute_daily(dedupe(day_items))
        h = upsert_history(h, market, sym_out)
    save_history(h)


def fmt_boards(themes_list: list, hits, boards, theme_boards, top_n: int) -> list:
    """组装板块展示行（合并告警时遍历多个主题）。"""
    lines = []
    for th in themes_list:
        for bname in (theme_boards or {}).get(th["name"], []):
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
                boards: dict, theme_boards: dict, top_n: int,
                stock_map: list | None = None, event_note: str = "",
                vol_ref: dict | None = None, themes_list: list | None = None) -> tuple[str, str]:
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
    if event_note:
        lines.append(f"**事件类型**：{event_note}")
    if vol_ref:
        lines.append(
            f"**历史波动参考**：同类「{vol_ref['typ']}」事件次日平均波动 "
            f"{vol_ref['mean_abs']*100:.1f}%，次日|波动|>3% 的概率 {vol_ref['big_rate']*100:.0f}%"
            f"（全样本校准，仅衡量波动大小、不预测方向）")
    lines.append(f"**来源**：{src}　**时间**：{ts}")
    lines.append(f"**原文**：{text}")
    if theme.get("macro"):
        lines.append("**类型**：宏观事件（关注利率/避险相关板块）")
    if stock_map:
        lines.append("**个股影响（LLM 判断）**：")
        for r in stock_map:
            arrow = {"利好": "↑", "利空": "↓", "中性": "→"}.get(r.get("direction"), "")
            lines.append(
                f"- {r.get('name')}({r.get('code')}) {r.get('direction')}{arrow}"
                f"（强度{r.get('magnitude')}）：{r.get('reason')}")
    bl = fmt_boards(themes_list or [theme], hits, boards, theme_boards, top_n)
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


def run_once(cfg: dict, themes: list, seen: list, cold_start: bool,
             dry_run: bool, boards: dict, theme_boards: dict, top_n: int,
             no_stock_impact: bool = False) -> int:
    """一轮：抓取 -> 去重 -> 影响分排序 -> 匹配 -> 告警。返回本轮新条目数。

    seen 为按加入顺序的 id 列表；函数内用 seen_set 保证 O(1) 查重，
    同时按顺序 append 以支持 save_seen 的「先到先淘汰」裁剪。
    """
    seen_set = set(seen)
    enabled = ((cfg.get("monitor") or {}).get("enabled_sources")
               or (cfg.get("news") or {}).get("enabled_sources") or None)

    all_items = fetch_new_items(enabled)
    # 去重
    uniq = {}
    for it in all_items:
        uniq.setdefault(it["id"], it)
    new_items = filter_recent([it for it in uniq.values() if it["id"] not in seen_set], days=30)

    if cold_start and new_items:
        print(f"[monitor] 首次运行：仅建立基线，跳过 {len(new_items)} 条历史消息，不告警")
        for it in new_items:
            seen.append(it["id"])
            seen_set.add(it["id"])
        save_seen(seen)
        append_raw(new_items)
        return len(new_items)

    # 影响分排序
    imp_cfg = cfg.get("impact") or {}
    weights = imp_cfg.get("weights") or None
    window_minutes = int(imp_cfg.get("window_minutes", 60))
    burst_cap = int(imp_cfg.get("burst_cap", 4))
    impact_min = float((cfg.get("monitor") or {}).get("impact_min", 0.0))
    si_cfg = (cfg.get("monitor") or {}).get("stock_impact") or {}
    si_enabled = bool(si_cfg.get("enabled", False)) and not no_stock_impact
    si_max = int(si_cfg.get("max_stocks", 6))
    new_items = compute_impact(new_items, themes, weights, window_minutes, burst_cap)

    alerts = 0
    for it in new_items:
        seen.append(it["id"])
        seen_set.add(it["id"])
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
        # 事件类型标注（白名单=有 alpha，非白名单=无 alpha/反向）
        ev = classify_event(text)
        event_note = ""
        if ev:
            typ, direction, whitelisted = ev
            event_note = f"{typ}({direction}，{'有alpha' if whitelisted else '无alpha'})"
        # 历史波动参考（影响分校准：同类事件次日平均波动与大动静概率）
        vol_ref = volatility_ref(text)
        # 命中主题时可选：LLM 个股影响映射（每条新闻只调一次，多主题复用；失败不阻塞告警）
        stock_map = []
        if si_enabled and matched:
            try:
                stock_map = map_news_to_stocks(text, max_stocks=si_max)
            except Exception as e:  # noqa: BLE001
                print(f"  [monitor] 个股映射失败: {type(e).__name__}")
        if not matched:
            continue
        imp = it.get("impact", 0.0)
        if impact_min > 0 and imp < impact_min:
            continue
        alerts += 1
        # 合并告警：一条新闻只推一次，多主题合并展示（旧行为每主题各推一条，刷屏严重）
        merged_theme = {
            "name": "、".join(th["name"] for th, _ in matched),
            "macro": any(th.get("macro") for th, _ in matched),
        }
        merged_hits = []
        seen_h = set()
        for _th, hs in matched:
            for h in hs:
                if h not in seen_h:
                    seen_h.add(h)
                    merged_hits.append(h)
        title, content = build_alert(it, score, merged_theme, merged_hits, boards,
                                     theme_boards, top_n, stock_map, event_note,
                                     vol_ref, [th for th, _ in matched])
        print("\n" + "=" * 60)
        print(title)
        print(content)
        print("=" * 60 + "\n")
        if not dry_run:
            push_alert(cfg, title, content)
            # 告警闭环：记录实际发出的告警，供 scripts/alert_review.py 回看次日波动
            log_alert({
                "news_id": it.get("id"),
                "ts": datetime.now().isoformat(timespec="seconds"),
                "date": it.get("date") or "",
                "theme": merged_theme["name"],
                "event_type": (vol_ref or {}).get("typ", ""),
                "impact": float(it.get("impact", 0.0)),
                "score": float(score),
                "source": it.get("source") or "",
                "symbols": it.get("symbols") or [],
            })

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
    ap.add_argument("--no-stock-impact", action="store_true",
                    help="跳过 LLM 个股影响映射（API 不可用时避免每条新闻挂 90s）")
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
        run_once(cfg, themes, seen, cold_start, args.dry_run, boards, theme_boards,
                 top_n, args.no_stock_impact)
        return 0

    first = True
    while True:
        try:
            run_once(cfg, themes, seen, cold_start and first, args.dry_run, boards,
                     theme_boards, top_n, args.no_stock_impact)
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


