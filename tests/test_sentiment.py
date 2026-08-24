"""情绪打分单元测试：覆盖中英文词典、否定窗口、边界与范围。"""

from news_aggregator.sentiment import score_text, score_text_en, score_text_zh


def test_positive_zh():
    assert score_text_zh("公司业绩大幅增长") > 0


def test_negative_zh():
    assert score_text_zh("公司亏损严重") < 0


def test_neg_growth_is_negative():
    """回归：负增长不应被当成正面（此前子串计数会把“增长”算正）。"""
    assert score_text_zh("公司业绩负增长") < 0


def test_not_growth_is_negative():
    assert score_text_zh("公司业绩没有增长") < 0


def test_avoid_risk_is_positive():
    """否定词反转：避免风险 = 正面。"""
    assert score_text_zh("公司有效避免风险") > 0


def test_future_growth_is_positive():
    """回归：未来增长不应因“未”字被误判为否定。"""
    assert score_text_zh("公司未来三年保持增长") > 0


def test_downslide_is_negative():
    assert score_text_zh("营收同比下滑") < 0


def test_score_range_zh():
    for t in ["利好大涨", "利空暴跌", "中性文本", ""]:
        assert -1.0 <= score_text_zh(t) <= 1.0


def test_english_negation():
    assert score_text_en("no growth expected") < 0
    assert score_text_en("strong growth expected") > 0


def test_score_text_routes():
    # 统一入口：中文走中文词典，英文走英文词典，空文本为 0
    assert score_text("") == 0.0
    assert -1.0 <= score_text("公司利好") <= 1.0
    assert -1.0 <= score_text("company beat estimates") <= 1.0
