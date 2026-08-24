"""影响分模型单元测试：权威度映射、排序、字段与权重归一化。"""

from news_aggregator.impact import DEFAULT_WEIGHTS, authority, compute_impact


def test_authority_quiver_mapping():
    """回归：Quiver 抓取的 source 名应命中权威度表，而不是落到默认 0.6。"""
    assert authority("Quiver国会交易") == 0.9
    assert authority("Quiver内部人交易") == 0.9
    assert authority("美联储Fed") == 1.0
    assert authority("未知来源") == 0.6


def test_compute_impact_sorts_and_fields():
    items = [
        {"id": "1", "ts": "2026-08-01T00:00:00+00:00", "source": "美联储Fed",
         "title": "美联储宣布降息", "content": "降息 降准 利好", "symbols": ["600519"]},
        {"id": "2", "ts": "2026-08-01T00:01:00+00:00", "source": "个股新闻",
         "title": "公司发布公告", "content": "", "symbols": []},
    ]
    out = compute_impact(items)
    assert out[0]["impact"] >= out[1]["impact"]
    for it in out:
        assert "impact" in it and "sentiment" in it and "impact_parts" in it
        assert 0.0 <= it["impact"] <= 1.0


def test_weights_normalized():
    # 权重和为 5 时会被归一化，最终 impact 仍落在 [0,1]
    weights = {"authority": 1.0, "burst": 1.0, "intensity": 1.0,
               "relevance": 1.0, "theme": 1.0}
    items = [{"id": "x", "ts": "2026-08-01T00:00:00+00:00", "source": "美联储Fed",
              "title": "降息", "content": "", "symbols": []}]
    compute_impact(items, weights=weights)
    assert 0.0 <= items[0]["impact"] <= 1.0


def test_default_weights_sum_to_one():
    assert abs(sum(DEFAULT_WEIGHTS.values()) - 1.0) < 1e-9
