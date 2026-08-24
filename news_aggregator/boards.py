"""板块名称解析 + 成分股缓存（运行时对齐，非致命）。

说明：
- 免费 AKShare 板块接口在部分网络环境不可用；本模块所有函数都容错，
  失败时返回空结果并打印告警，绝不影响新闻捕捉主流程。
- 板块名用「子串模式」匹配全量板块名，避免硬编码板名失效。
"""

import json
import pathlib
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
NEWS_DIR = ROOT / "news"
CACHE_PATH = NEWS_DIR / "boards_cache.json"


def _col(df, *names):
    """取 DataFrame 里第一个存在的列名；都不存在返回 None。"""
    if df is None or df.empty:
        return None
    for n in names:
        if n in df.columns:
            return n
    return None


def fetch_board_names(kind: str) -> list:
    """获取全量板块名。kind: concept | industry。"""
    import akshare as ak
    if kind == "concept":
        df = ak.stock_board_concept_name_em()
    else:
        df = ak.stock_board_industry_name_em()
    col = _col(df, "板块名称", "行业名称", "name")
    if not col:
        return []
    return [str(x) for x in df[col].tolist() if str(x).strip()]


def resolve_boards(patterns: list, kind: str) -> list:
    """按子串模式匹配出真实板块名。"""
    if not patterns:
        return []
    try:
        names = fetch_board_names(kind)
    except Exception as e:  # noqa: BLE001
        print(f"[boards] 获取{kind}板块名失败: {type(e).__name__}")
        return []
    out = []
    for pat in patterns:
        pat_l = str(pat).lower()
        hit = [n for n in names if pat_l in n.lower()]
        out.extend(hit)
    seen = set()
    res = []
    for n in out:
        if n not in seen:
            seen.add(n)
            res.append(n)
    return res


def fetch_constituents(board: str, kind: str, top_n: int) -> list:
    """获取板块成分股，按涨跌幅降序取前 top_n。返回 [{code,name,pct}]。"""
    import akshare as ak
    if kind == "concept":
        df = ak.stock_board_concept_cons_em(symbol=board)
    else:
        df = ak.stock_board_industry_cons_em(symbol=board)
    code_col = _col(df, "代码", "股票代码")
    name_col = _col(df, "名称", "股票名称")
    pct_col = _col(df, "涨跌幅")
    if not code_col or not name_col:
        return []
    rows = []
    for _, r in df.iterrows():
        try:
            pct = float(r[pct_col]) if pct_col else 0.0
        except (TypeError, ValueError):
            pct = 0.0
        rows.append({"code": str(r[code_col]), "name": str(r[name_col]), "pct": pct})
    rows.sort(key=lambda x: x["pct"], reverse=True)
    return rows[:top_n]


def build_board_cache(theme_names: dict, top_n: int = 5) -> tuple[dict, dict]:
    """按主题构建板块缓存。

    theme_names: {主题name: {concept_boards:[], industry_boards:[]}}
    返回 (boards, theme_boards)：
      boards:       {板块名: {kind, constituents}}
      theme_boards: {主题name: [板块名,...]}

    全量板块名在进程内只拉一次（旧实现对每个主题重复请求全量接口，
    接口慢/失败时构建时间随主题数线性膨胀）。
    """
    try:
        cnames = fetch_board_names("concept")
    except Exception as e:  # noqa: BLE001
        print(f"[boards] 获取concept板块名失败: {type(e).__name__}")
        cnames = []
    try:
        inames = fetch_board_names("industry")
    except Exception as e:  # noqa: BLE001
        print(f"[boards] 获取industry板块名失败: {type(e).__name__}")
        inames = []

    def resolve(patterns: list, names: list) -> list:
        out = []
        for pat in patterns:
            pl = str(pat).lower()
            out.extend(n for n in names if pl in n.lower())
        seen, res = set(), []
        for n in out:
            if n not in seen:
                seen.add(n)
                res.append(n)
        return res

    boards = {}
    theme_boards = {}
    for tname, t in theme_names.items():
        resolved = []
        for kind_key in ("concept_boards", "industry_boards"):
            kind = "concept" if kind_key == "concept_boards" else "industry"
            names = cnames if kind == "concept" else inames
            for b in resolve(t.get(kind_key) or [], names):
                resolved.append(b)
                if b in boards:
                    continue
                try:
                    cons = fetch_constituents(b, kind, top_n)
                except Exception as e:  # noqa: BLE001
                    print(f"[boards] {b} 成分股获取失败: {type(e).__name__}")
                    cons = []
                boards[b] = {"kind": kind, "constituents": cons}
                if cons:
                    print(f"[boards] {b}: {len(cons)} 只")
        theme_boards[tname] = resolved
    return boards, theme_boards


def load_cached() -> dict | None:
    if CACHE_PATH.exists():
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
    return None


def save_cached(cache: dict) -> None:
    NEWS_DIR.mkdir(exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def get_board_cache(theme_names: dict, top_n: int = 5, refresh_hours: int = 24) -> tuple[dict, dict]:
    """带过期判断的板块缓存。失败返回 ({}, {})，且不缓存失败结果。

    旧实现失败时也把空结果缓存 24h，导致网络恢复前一直拿不到板块数据。
    """
    cached = load_cached()
    if cached and cached.get("ts"):
        age_h = (time.time() - cached["ts"]) / 3600
        if age_h < refresh_hours:
            b = cached.get("boards") or {}
            tb = cached.get("theme_boards") or {}
            print(f"[boards] 使用缓存（{age_h:.1f}h 前，{len(b)} 板块）")
            return b, tb
    try:
        boards, theme_boards = build_board_cache(theme_names, top_n)
    except Exception as e:  # noqa: BLE001
        print(f"[boards] 板块缓存构建失败: {type(e).__name__}")
        return {}, {}
    if boards or theme_boards:
        save_cached({"ts": time.time(), "boards": boards, "theme_boards": theme_boards})
    return boards, theme_boards
