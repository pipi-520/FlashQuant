"""新闻 -> 个股影响映射。

输入一条新闻，用 LLM 从核心股票池（data/stock_universe.yaml）里判断哪些股票
受益/受损，输出结构化映射 [{name, code, direction, magnitude, reason}]。

direction: 利好 / 利空 / 中性；magnitude: 1~5 影响强度。
不依赖 akshare 实时板块接口，仅依赖 LLM + 静态股票池，稳定可复现。

用法：
    from news_aggregator.stock_impact import map_news_to_stocks
    map_news_to_stocks("美国FDA批准一款mRNA抗癌疫苗……")
"""

import json
import os
import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
UNIVERSE_PATH = ROOT / "data" / "stock_universe.yaml"

# 模块级缓存：monitor 每条命中新闻都会调用匹配/推理，避免重复读 CSV 和 .env
_STOCK_LIST_CACHE: dict | None = None
_ENV_CACHE: tuple | None = None


def load_universe() -> list:
    data = yaml.safe_load(UNIVERSE_PATH.read_text(encoding="utf-8"))
    return data.get("stocks") or []


def load_stock_list() -> dict:
    """加载全 A股 名称->代码 映射（data/stock_list.csv，akshare 缓存，模块级缓存）。"""
    import csv
    global _STOCK_LIST_CACHE
    if _STOCK_LIST_CACHE is not None:
        return _STOCK_LIST_CACHE
    p = ROOT / "data" / "stock_list.csv"
    name_to_code = {}
    if p.exists():
        with open(p, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                code = str(row.get("code") or "").strip()
                name = str(row.get("name") or "").strip()
                if code and name:
                    name_to_code[name] = code
    _STOCK_LIST_CACHE = name_to_code
    return name_to_code


def match_stocks_in_text(text: str) -> list:
    """精确匹配新闻里提到的 A股股票名，返回 [{name, code}]（按名称长度降序）。"""
    name_to_code = load_stock_list()
    if not name_to_code or not text:
        return []
    hits = []
    for name, code in name_to_code.items():
        if len(name) >= 3 and name in text:  # 只匹配 >=3 字，减少短名误匹配
            hits.append((name, code))
    hits.sort(key=lambda x: -len(x[0]))
    return [{"name": n, "code": c} for n, c in hits]


def _env() -> tuple[str, str, str]:
    global _ENV_CACHE
    if _ENV_CACHE is not None:
        return _ENV_CACHE
    try:
        with open(ROOT / ".env", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass
    _ENV_CACHE = (
        os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        os.environ.get("OPENAI_API_KEY", ""),
        os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
    )
    return _ENV_CACHE


def _llm_map(news_text: str, universe: list, max_stocks: int) -> list:
    """LLM 从股票池推理受影响股票（用于板块级新闻，没提具体股票时）。"""
    import requests

    pool = "\n".join(f"- {s['name']}({s['code']}): {s['business']}" for s in universe)
    prompt = (
        "你是资深A股分析师。给定一条新闻和一个股票池，判断新闻对哪些股票有显著影响。\n"
        "只从股票池里选，最多选 {n} 只受影响最明显的股票。\n"
        "只返回一个 JSON 数组，每项字段：name(股票名)、code(代码)、"
        "direction(利好/利空/中性)、magnitude(1~5 整数，影响强度)、reason(一句话理由)。\n"
        "不要输出任何其他文字。\n\n"
        f"新闻：{news_text}\n\n股票池：\n{pool}"
    ).format(n=max_stocks)

    base, key, model = _env()
    if not key:
        return []
    r = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "temperature": 0,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=30,
    )
    data = r.json()
    if "choices" not in data or not data["choices"]:
        raise RuntimeError(f"LLM 返回异常: {str(data)[:200]}")
    content = data["choices"][0]["message"]["content"]
    m = re.search(r"\[.*\]", content, re.S)
    if not m:
        raise RuntimeError(f"无法解析 JSON: {content[:200]}")
    out = json.loads(m.group(0))
    for item in out:
        d = str(item.get("direction") or "").strip()
        item["direction"] = "利好" if "利" in d and "空" not in d else (
            "利空" if "空" in d else "中性")
        try:
            item["magnitude"] = int(item.get("magnitude") or 1)
        except (TypeError, ValueError):
            item["magnitude"] = 1
    return sorted(out, key=lambda x: x.get("magnitude", 0), reverse=True)


def map_news_to_stocks(news_text: str, universe: list | None = None,
                       max_stocks: int = 8) -> list:
    """新闻 -> 个股影响映射（两层）。

    1. 精确匹配：新闻里明确提到的股票名（全 A股列表），直接映射。
       方向优先用「事件类型」判断（白名单事件有 alpha：处罚=利空、分红/中标/涨价=利好），
       否则回退到情绪分。
    2. LLM 推理：板块级新闻（没提具体股票）时，从核心股票池推理受益股补充。
    """
    from news_aggregator.sentiment import score_text
    from news_aggregator.event_filter import classify

    result = []
    # 事件类型方向（白名单事件自带方向，有 alpha）
    cls = classify(news_text)
    event_dir = cls[1] if cls and cls[2] else None
    event_type = cls[0] if cls else None

    # 第一层：精确匹配新闻里提到的股票
    sc = score_text(news_text)
    fallback = "利好" if sc > 0.05 else ("利空" if sc < -0.05 else "中性")
    for m in match_stocks_in_text(news_text)[:max_stocks]:
        direction = event_dir or fallback
        reason = f"事件[{event_type}]直接提及" if event_dir else "新闻直接提及"
        result.append({
            "name": m["name"], "code": m["code"],
            "direction": direction, "magnitude": 4,
            "reason": reason,
        })
    # 第二层：LLM 从股票池推理补充（去重）
    if len(result) < max_stocks:
        try:
            universe = universe or load_universe()
            existing = {r["code"] for r in result}
            for item in _llm_map(news_text, universe, max_stocks - len(result)):
                if item.get("code") not in existing:
                    result.append(item)
                    existing.add(item["code"])
        except Exception:  # noqa: BLE001
            pass
    return result
