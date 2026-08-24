"""影响分模型：量化一条新闻对市场情绪的影响强度。

impact = w_authority * 来源权威度
       + w_burst     * 爆发系数（时间窗内多源重复报道）
       + w_intensity * 情绪强度 |score|
       + w_relevance * 持仓/关注相关度
       + w_theme     * 主题热度权重

权重可在 config.yaml 的 impact 段覆盖。
输出：为每条 item 增加 impact / impact_parts 字段，并按 impact 降序返回。
"""

import re
from datetime import datetime, timedelta, timezone

from news_aggregator.sentiment import score_text, score_batch

# ---------- 主题匹配（与 monitor 共用，集中在此，避免两处漂移） ----------

def keyword_match(text: str, kw: str) -> bool:
    """中文子串匹配；纯 ASCII 关键词用「仅限 ASCII 字母数字」的边界匹配。"""
    if not kw:
        return False
    if kw.isascii():
        pat = r"(?<![A-Za-z0-9])" + re.escape(kw) + r"(?![A-Za-z0-9])"
        return re.search(pat, text, re.I) is not None
    return kw in text


def match_themes(text: str, themes: list) -> list:
    """返回 [(theme, hits)]，hits 为命中的关键词列表。"""
    out = []
    for th in (themes or []):
        hits = [kw for kw in (th.get("keywords") or []) if keyword_match(text, kw)]
        if hits:
            out.append((th, hits))
    return out


# ---------- 来源权威度（0-1） ----------
# 央行/政府/监管/宏观数据发布方最高，通讯社次之，社媒/快讯中等，个股新闻最低。
SOURCE_AUTHORITY = {
    "美联储Fed": 1.0, "白宫": 1.0, "中国人民银行": 1.0,
    "欧洲央行ECB": 0.98, "日本央行BOJ": 0.98, "英国央行BOE": 0.98,
    "美国国务院": 0.95, "中国外交部": 0.95, "国会听证会": 0.9,
    "SEC EDGAR": 0.9, "FRED宏观数据": 0.95, "美国非农/CPI": 0.9,
    "美国GDP/PCE": 0.9, "ISM PMI": 0.9, "EIA原油库存": 0.9,
    "OPEC/IEA": 0.9, "CFTC持仓": 0.9, "VIX波动率": 0.85, "AAII情绪": 0.85,
    "中国宏观数据": 0.9, "IMF/世界银行": 0.9, "政策公告": 0.9,
    "AP": 0.85, "Reuters": 0.85, "AFP": 0.85, "彭博": 0.85,
    "特朗普 Truth Social": 0.85, "北向资金": 0.85,
    "Quiver另类数据": 0.9, "Quiver国会交易": 0.9, "Quiver内部人交易": 0.9,
    "Bargo国会交易": 0.9,
    "财联社": 0.8, "华尔街见闻": 0.75, "东财全球资讯": 0.75,
    "金十快讯": 0.75, "新浪7x24": 0.7, "同花顺7x24": 0.7, "富途牛牛": 0.7,
    "半导体行业": 0.7, "航运BDI": 0.7, "个股新闻": 0.5,
}
DEFAULT_AUTHORITY = 0.6

DEFAULT_WEIGHTS = {
    "authority": 0.20,
    "burst": 0.20,
    "intensity": 0.25,
    "relevance": 0.20,
    "theme": 0.15,
}

BURST_CAP = 4          # 相似条数达到该值即 burst=1
SIM_THRESHOLD = 0.6    # 标题相似度阈值
WINDOW_MINUTES = 60    # 突发检测时间窗
DEFAULT_THEME_WEIGHT = 0.5

_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is",
         "are", "as", "at", "by", "be", "with", "from", "says", "say", "said",
         "reports", "report", "will", "after", "over", "this", "that", "its"}


def _norm_tokens(text: str) -> set:
    """ASCII 单词（去停用词）+ CJK 字符 bigram，组成相似度 token 集合。"""
    t = (text or "").lower()
    words = [w for w in re.findall(r"[a-z]+", t)
             if w not in _STOP and len(w) > 1]
    cjk = re.findall(r"[\u4e00-\u9fff]", t)
    words += [a + b for a, b in zip(cjk, cjk[1:])]
    return set(words)


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _ts_epoch(item: dict):
    ts = item.get("ts") or ""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def authority(source: str) -> float:
    return SOURCE_AUTHORITY.get(source or "", DEFAULT_AUTHORITY)


def _burst_counts(items: list, epochs: list, window_sec: int) -> list:
    """返回每条 item 的相似计数（时间窗内、标题相似度超阈值的近重复条数）。"""
    n = len(items)
    counts = [0] * n
    if n < 2:
        return counts
    toks = [_norm_tokens(f"{it.get('title', '')} {it.get('content', '')}") for it in items]
    order = sorted(range(n), key=lambda i: (epochs[i] if epochs[i] is not None else 1e18, i))
    # 双指针：对每个 i，只和其后的 item 比较，超过时间窗即停
    for pos_i, i in enumerate(order):
        if epochs[i] is None:
            continue
        src_i = items[i].get("source")
        for j in order[pos_i + 1:]:
            if epochs[j] is None:
                continue
            if epochs[j] - epochs[i] > window_sec:
                break
            if items[j].get("source") == src_i:
                continue  # 同源重复不计为多源爆发
            inter = toks[i] & toks[j]
            if len(inter) >= 3 and len(inter) / len(toks[i] | toks[j]) >= SIM_THRESHOLD:
                counts[i] += 1
                counts[j] += 1
    return counts


def compute_impact(items: list, themes: list | None = None,
                   weights: dict | None = None,
                   window_minutes: int = WINDOW_MINUTES,
                   burst_cap: int = BURST_CAP) -> list:
    """为 items 计算影响分并降序返回（原地写入 impact / impact_parts）。

    themes: 主题列表（同 themes.yaml 的 themes 数组），可为 None 表示不做主题匹配。
    weights: 覆盖 DEFAULT_WEIGHTS。
    """
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update({k: float(v) for k, v in weights.items() if k in w})
    # 归一化权重
    total = sum(w.values()) or 1.0
    w = {k: v / total for k, v in w.items()}

    themes = themes or []
    epochs = [_ts_epoch(it) for it in items]
    window_sec = window_minutes * 60
    counts = _burst_counts(items, epochs, window_sec)

    texts = [f"{it.get('title', '')} {it.get('content', '')}" for it in items]
    scores = score_batch(texts)

    for idx, (it, cnt) in enumerate(zip(items, counts)):
        text = texts[idx]
        sc = scores[idx]
        matched = match_themes(text, themes)
        theme_w = 0.0
        if matched:
            theme_w = max((th.get("weight") or DEFAULT_THEME_WEIGHT) for th, _ in matched)
        parts = {
            "authority": authority(it.get("source")),
            "burst": min(1.0, cnt / max(1, burst_cap)),
            "intensity": abs(sc),
            "relevance": 1.0 if it.get("symbols") else 0.2,
            "theme": min(1.0, theme_w),
        }
        impact = sum(w[k] * parts[k] for k in parts)
        it["impact"] = round(impact, 4)
        it["impact_parts"] = {k: round(v, 4) for k, v in parts.items()}
        it["sentiment"] = round(sc, 4)

    return sorted(items, key=lambda x: x.get("impact", 0.0), reverse=True)


def rank_items(items: list, themes: list | None = None, weights: dict | None = None,
               window_minutes: int = WINDOW_MINUTES, burst_cap: int = BURST_CAP) -> list:
    """compute_impact 的别名（不修改入参列表顺序的语义由返回列表体现）。"""
    return compute_impact(items, themes, weights, window_minutes, burst_cap)



