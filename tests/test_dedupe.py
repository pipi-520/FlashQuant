"""去重逻辑单元测试。"""

from news_aggregator.run import dedupe


def test_dedupe_removes_duplicate_ids():
    items = [
        {"id": "a", "title": "1"},
        {"id": "b", "title": "2"},
        {"id": "a", "title": "1-dup"},
    ]
    out = dedupe(items)
    assert [it["id"] for it in out] == ["a", "b"]


def test_dedupe_empty():
    assert dedupe([]) == []
