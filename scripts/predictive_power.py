"""预测力回看：命中事件 -> 次日板块超额收益。

回答一个问题：某个主题的关键词命中新闻后，对应 A 股板块下一个交易日是否跑赢大盘？

数据源：
- 新闻：news/raw/*.jsonl（多源聚合产物，含 date/source/title/content/impact）
- 板块：同花顺概念/行业板块指数（stock_board_concept_index_ths / stock_board_industry_index_ths）
- 基准：上证指数（stock_zh_index_daily, sh000001）

方法：
- 主题命中：用 themes.yaml 关键词匹配每条新闻，按日期聚合命中日。
- 板块篮子：主题解析出的前 max_boards 个板块，按等权合成日收益（日收益率再平均）。
- 事件研究：命中日 d -> 参考交易日 i（<=d 的最后一个交易日），
  次日超额 = 篮子 close[i+1]/close[i]-1 - 指数 close[i+1]/close[i]-1。
- 基准：全期所有交易日的次日超额均值（背景值），edge = 信号均值 - 背景均值。

用法：
    .venv/Scripts/python.exe scripts/predictive_power.py [--impact-min 0.0] [--max-boards 3] [--no-cache]
"""

import argparse
import bisect
import json
import pathlib
import sys

import pandas as pd
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from news_aggregator.impact import match_themes, compute_impact, DEFAULT_WEIGHTS  # noqa: E402

NEWS_DIR = ROOT / "news"
DATA_DIR = ROOT / "data"
RESULTS_DIR = ROOT / "results"
CACHE_PATH = DATA_DIR / "board_index_cache.json"

START = "20260715"
END = "20260827"


def load_themes() -> list:
    with open(ROOT / "news_aggregator/themes.yaml", "r", encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("themes") or []


def load_raw_items() -> list:
    items = []
    seen = set()
    raw_dir = NEWS_DIR / "raw"
    if not raw_dir.exists():
        return items
    for path in sorted(raw_dir.glob("*.jsonl")):
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


def _date_col(df):
    for c in ("date", "日期", "时间"):
        if c in df.columns:
            return c
    return df.columns[0]


def _close_col(df):
    for c in ("close", "收盘价"):
        if c in df.columns:
            return c
    return None


def normalize_index(df) -> pd.DataFrame:
    """统一为 [date, close]，date 为 date 类型。"""
    d = _date_col(df)
    c = _close_col(df)
    if c is None:
        return pd.DataFrame(columns=["date", "close"])
    out = pd.DataFrame({
        "date": pd.to_datetime(df[d], errors="coerce").dt.date,
        "close": pd.to_numeric(df[c], errors="coerce"),
    }).dropna()
    return out.sort_values("date").reset_index(drop=True)


def fetch_board_index(name: str, kind: str, start: str, end: str) -> pd.DataFrame:
    import akshare as ak
    if kind == "concept":
        df = ak.stock_board_concept_index_ths(symbol=name, start_date=start, end_date=end)
    else:
        df = ak.stock_board_industry_index_ths(symbol=name, start_date=start, end_date=end)
    return normalize_index(df)


def resolve_boards(themes: list, max_boards: int) -> dict:
    """返回 {theme_name: [(board_name, kind)]}。"""
    import akshare as ak
    try:
        cnames = [str(x) for x in ak.stock_board_concept_name_ths()["name"].tolist()]
    except Exception as e:  # noqa: BLE001
        print(f"[boards] 概念板块名获取失败: {type(e).__name__}")
        cnames = []
    try:
        inames = [str(x) for x in ak.stock_board_industry_name_ths()["name"].tolist()]
    except Exception as e:  # noqa: BLE001
        print(f"[boards] 行业板块名获取失败: {type(e).__name__}")
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
        boards = boards[:max_boards]
        out[th["name"]] = boards
    return out


def load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_cache(cache: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def board_daily(name: str, kind: str, cache: dict, use_cache: bool, start: str, end: str) -> dict | None:
    """返回 {date_str: close} 或 None。带缓存。"""
    key = f"{kind}:{name}"
    if use_cache and key in cache and cache[key].get("dates"):
        return cache[key]
    try:
        df = fetch_board_index(name, kind, start, end)
    except Exception as e:  # noqa: BLE001
        print(f"  [skip] {name}({kind}) 获取失败: {type(e).__name__}")
        return None
    if df.empty:
        return None
    rec = {
        "dates": [d.strftime("%Y-%m-%d") for d in df["date"].tolist()],
        "closes": [float(x) for x in df["close"].tolist()],
    }
    cache[key] = rec
    return rec


def build_ret_maps(dates: list, closes: list) -> dict:
    """date(str) -> {'f1': next-day ret, 'f3': 3-day cum ret}。"""
    ret = {}
    n = len(dates)
    for i in range(n - 1):
        c0 = closes[i]
        f1 = closes[i + 1] / c0 - 1 if c0 else 0.0
        f3 = closes[i + 3] / c0 - 1 if i + 3 < n and c0 else None
        ret[dates[i]] = {"f1": f1, "f3": f3}
    return ret


def mean_of(values) -> float | None:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--impact-min", type=float, default=0.0, help="只统计影响分>=该值的命中")
    ap.add_argument("--max-boards", type=int, default=3, help="每个主题最多取几个板块")
    ap.add_argument("--no-cache", action="store_true", help="忽略板块缓存强制重取")
    ap.add_argument("--start", default=START, help="板块指数起始日 YYYYMMDD")
    ap.add_argument("--end", default=END, help="板块指数结束日 YYYYMMDD")
    args = ap.parse_args()

    themes = load_themes()
    print(f"[load] 主题 {len(themes)} 个")
    items = load_raw_items()
    print(f"[load] 去重后新闻 {len(items)} 条")

    # 重算 impact（补齐旧数据）
    imp_cfg = {}
    cfg_path = ROOT / "config.yaml"
    if cfg_path.exists():
        imp_cfg = (yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}).get("impact") or {}
    weights = imp_cfg.get("weights") or DEFAULT_WEIGHTS
    window_minutes = int(imp_cfg.get("window_minutes", 60))
    burst_cap = int(imp_cfg.get("burst_cap", 4))
    items = compute_impact(items, themes, weights, window_minutes, burst_cap)

    # 板块解析
    print("[boards] 解析主题 -> 板块 ...")
    theme_boards = resolve_boards(themes, args.max_boards)

    # 拉取板块指数
    cache = load_cache()
    use_cache = not args.no_cache
    all_daily = {}  # (name,kind) -> {date_str: close}
    for th in themes:
        for name, kind in theme_boards.get(th["name"], []):
            if (name, kind) in all_daily:
                continue
            rec = board_daily(name, kind, cache, use_cache, args.start, args.end)
            if rec:
                all_daily[(name, kind)] = rec
            sys.stdout.write(".")
            sys.stdout.flush()
    print(f"\n[boards] 成功拉取 {len(all_daily)} 个板块指数")
    save_cache(cache)

    # 基准：上证指数
    import akshare as ak
    try:
        idx = normalize_index(ak.stock_zh_index_daily(symbol="sh000001"))
    except Exception as e:  # noqa: BLE001
        print(f"[index] 上证指数获取失败: {type(e).__name__}")
        return 1
    idx_dates = [d.strftime("%Y-%m-%d") for d in idx["date"].tolist()]
    idx_closes = [float(x) for x in idx["close"].tolist()]
    idx_ret = build_ret_maps(idx_dates, idx_closes)
    print(f"[index] 上证指数 {len(idx_dates)} 个交易日")

    # 每条新闻的主题命中（按日期）
    # 命中：某日存在一条匹配该主题、影响分>=阈值的新闻
    theme_hit_dates = {th["name"]: set() for th in themes}
    theme_hit_count = {th["name"]: 0 for th in themes}
    for it in items:
        d = it.get("date")
        if not d:
            continue
        imp = float(it.get("impact") or 0.0)
        if imp < args.impact_min:
            continue
        text = f"{it.get('title', '')} {it.get('content', '')}"
        for th, _hits in match_themes(text, themes):
            theme_hit_dates[th["name"]].add(d)
            theme_hit_count[th["name"]] += 1

    # 事件研究
    rows = []
    for th in themes:
        boards = theme_boards.get(th["name"], [])
        if not boards:
            rows.append({"name": th["name"], "boards": 0, "events": 0, "n_items": 0,
                         "avg_f1": None, "med_f1": None, "hit_rate": None,
                         "baseline": None, "edge": None, "avg_f3": None})
            continue
        # 合成篮子：每个交易日对所有板块的 f1/f3 取均值
        basket_f1 = {}
        basket_f3 = {}
        for name, kind in boards:
            rec = all_daily.get((name, kind))
            if not rec:
                continue
            rm = build_ret_maps(rec["dates"], rec["closes"])
            for d, r in rm.items():
                basket_f1.setdefault(d, []).append(r["f1"])
                basket_f3.setdefault(d, []).append(r["f3"])
        basket_f1 = {d: mean_of(v) for d, v in basket_f1.items()}
        basket_f3 = {d: mean_of(v) for d, v in basket_f3.items()}

        # 背景基准：全期所有交易日的次日超额
        all_excess = []
        for d in idx_dates:
            bf = basket_f1.get(d)
            ix = idx_ret.get(d, {}).get("f1")
            if bf is not None and ix is not None:
                all_excess.append(bf - ix)
        baseline = mean_of(all_excess)

        # 命中日
        hit_dates = sorted(theme_hit_dates[th["name"]])
        event_excess = []
        event_f3 = []
        for d in hit_dates:
            i = bisect.bisect_right(idx_dates, d) - 1  # 参考交易日
            if i < 0 or i >= len(idx_dates) - 1:
                continue
            ref = idx_dates[i]
            bf = basket_f1.get(ref)
            ix = idx_ret.get(ref, {}).get("f1")
            if bf is None or ix is None:
                continue
            event_excess.append(bf - ix)
            bf3 = basket_f3.get(ref)
            ix3 = idx_ret.get(ref, {}).get("f3")
            if bf3 is not None and ix3 is not None:
                event_f3.append(bf3 - ix3)

        n_ev = len(event_excess)
        avg_f1 = mean_of(event_excess)
        med_f1 = float(sorted(event_excess)[n_ev // 2]) if n_ev else None
        hit_rate = (sum(1 for x in event_excess if x > 0) / n_ev) if n_ev else None
        avg_f3 = mean_of(event_f3)

        rows.append({"name": th["name"], "boards": len(boards), "events": n_ev,
                     "n_items": theme_hit_count[th["name"]],
                     "avg_f1": avg_f1, "med_f1": med_f1, "hit_rate": hit_rate,
                     "baseline": baseline, "edge": (avg_f1 - baseline) if avg_f1 is not None and baseline is not None else None,
                     "avg_f3": avg_f3})

    # 排序：有 edge 的按 edge 降序，其余排后
    rows.sort(key=lambda r: (r["edge"] is not None, r["edge"] if r["edge"] is not None else -9), reverse=True)

    # 写报告
    def pct(v, digits=2):
        return "-" if v is None else f"{v*100:+.{digits}f}%"

    lines = []
    lines.append("# 新闻事件预测力回看报告")
    lines.append("")
    lines.append(f"> 生成时间：{pd.Timestamp.now():%Y-%m-%d %H:%M:%S}")
    lines.append(f"> 新闻范围：{min((it.get('date') or '9999') for it in items)} ~ {max((it.get('date') or '0000') for it in items)}，去重 {len(items)} 条")
    lines.append(f"> 影响分阈值：impact >= {args.impact_min}；每个主题最多 {args.max_boards} 个板块")
    lines.append("> 方法：命中日参考交易日 i（<=命中日的最后一个交易日），次日超额 = 板块篮子(i+1 日收益) - 上证指数(i+1 日收益)。")
    lines.append("> **样本仅约一个月，命中次数少，结果仅供方向参考，不具备统计显著性。**")
    lines.append("")
    lines.append("## 汇总（按 edge 降序）")
    lines.append("")
    lines.append("| 主题 | 板块数 | 命中日 | 命中条数 | 次日超额均值 | 中位数 | 命中率(>0) | 背景基准 | edge | 3日累计超额 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        lines.append(
            f"| {r['name']} | {r['boards']} | {r['events']} | {r['n_items']} | "
            f"{pct(r['avg_f1'])} | {pct(r['med_f1'])} | "
            f"{('- ' if r['hit_rate'] is None else f'{r['hit_rate']*100:.0f}%')} | "
            f"{pct(r['baseline'])} | {pct(r['edge'])} | {pct(r['avg_f3'])} |"
        )
    lines.append("")
    lines.append("## 说明")
    lines.append("")
    lines.append("- `edge = 次日超额均值 - 背景基准`：正值表示该主题命中后，板块次日跑赢大盘的程度高于随机交易日。")
    lines.append("- 命中率 = 命中日中次日超额 > 0 的占比。")
    lines.append("- 板块篮子为等权：把主题解析出的多个板块的日收益取平均，再与上证指数比较。")
    lines.append("- 未计入手续费/滑点，且 A 股 T+1 下实际可操作性另需回测验证。")
    lines.append("")

    RESULTS_DIR.mkdir(exist_ok=True)
    report = RESULTS_DIR / "predictive_power_report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n[report] 已写入 {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
