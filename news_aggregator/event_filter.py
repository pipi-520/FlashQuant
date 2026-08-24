"""事件类型过滤层：只保留有 alpha 的事件，过滤无 alpha/反向事件。

基于 scripts/event_type_study.py 的历史验证结论（次日收益/命中率）：
- 白名单（有 alpha）：
    处罚诉讼 -> 利空（次日 -0.98%，命中率 66.7%）
    分红     -> 利好（次日 +0.45%，命中率 72.4%）
    中标订单 -> 利好（次日 +0.55%）
    涨价提价 -> 利好（次日 +0.69%）
- 非白名单（无 alpha 或反向）：
    财报预增 -> 利好出尽（次日 -0.79%）
    增持回购 -> 反向（次日 -0.38%）
    减持 / 财报预减 -> 无 alpha

用法：
    from news_aggregator.event_filter import classify
    typ, direction, whitelisted = classify("公司中标5亿元订单")  # ("中标订单","利好",True)
"""

# (事件类型, 方向, 关键词, 是否白名单)。顺序即优先级（白名单靠前）。
EVENT_TYPES = [
    ("处罚诉讼", "利空", ["处罚", "立案", "违规", "诉讼", "调查"], True),
    ("分红", "利好", ["分红", "派息", "送转", "红利", "派现"], True),
    ("中标订单", "利好", ["中标", "签约", "订单"], True),
    ("涨价提价", "利好", ["涨价", "提价", "价格上调"], True),
    ("减持", "利空", ["减持"], False),
    ("增持回购", "利好", ["增持", "回购"], False),
    ("财报预增", "利好", ["预增", "业绩预增", "净利润增长", "同比增长", "扭亏", "超预期"], False),
    ("财报预减", "利空", ["预减", "业绩预减", "净利润下降", "同比下滑", "亏损", "不及预期"], False),
]


def classify(text: str) -> tuple[str, str, bool] | None:
    """返回 (事件类型, 方向, 是否白名单)；未命中任何事件类型返回 None。"""
    if not text:
        return None
    for typ, direction, kws, whitelisted in EVENT_TYPES:
        for kw in kws:
            if kw in text:
                return typ, direction, whitelisted
    return None


def whitelisted(text: str) -> bool:
    """是否属于有 alpha 的事件类型（白名单）。"""
    cls = classify(text)
    return bool(cls and cls[2])


# 事件类型 -> 历史次日波动参考（scripts/impact_calibration.py 全样本 36939 条校准，
# 2026-08-24 生成，结果见 results/impact_calibration.md）。
# mean_abs = 次日 |收益| 均值；big_rate = P(次日 |收益| > 3%)。
# 用途：告警文案附带的「同类事件历史波动」参考 + 告警优先级排序。
EVENT_VOLATILITY = {
    "财报预增": {"mean_abs": 0.021, "big_rate": 0.265},
    "涨价提价": {"mean_abs": 0.019, "big_rate": 0.196},
    "增持回购": {"mean_abs": 0.019, "big_rate": 0.175},
    "处罚诉讼": {"mean_abs": 0.015, "big_rate": 0.175},
    "分红":     {"mean_abs": 0.015, "big_rate": 0.125},
    "中标订单": {"mean_abs": 0.015, "big_rate": 0.116},
    "减持":     {"mean_abs": 0.016, "big_rate": 0.095},
    "财报预减": {"mean_abs": 0.013, "big_rate": 0.060},
    "其他":     {"mean_abs": 0.015, "big_rate": 0.129},
}
# 基线（全部新闻）：mean_abs 1.55%，P(>3%) 13.6%。
EVENT_VOLATILITY_BASELINE = {"mean_abs": 0.0155, "big_rate": 0.136}


def volatility_ref(text: str) -> dict | None:
    """返回新闻命中事件类型的历史波动参考 {typ, mean_abs, big_rate}；未命中返回 None。

    与 classify 不同：classify 按白名单优先级返回单一类型（用于方向判断）；
    本函数扫描全部命中类型，取历史大动静概率（big_rate）最高的一个——
    因为「财报预增」的波动参考（26.5%）比「分红」（12.5%）更有告警价值，
    而 classify 的优先级会先命中分红。
    """
    if not text:
        return None
    hits = [(typ, direction, whitelisted)
            for typ, direction, kws, whitelisted in EVENT_TYPES
            if any(kw in text for kw in kws)]
    if not hits:
        return None
    best = max(hits, key=lambda t: EVENT_VOLATILITY.get(t[0], EVENT_VOLATILITY["其他"])["big_rate"])
    ref = EVENT_VOLATILITY.get(best[0]) or EVENT_VOLATILITY["其他"]
    return {"typ": best[0], "mean_abs": ref["mean_abs"], "big_rate": ref["big_rate"]}
