"""历史回填模块的纯函数单元测试（无网络、无 pandas/vnpy 依赖）。"""

from news_aggregator.backfill import (
    _em_to_item,
    cik10,
    compact_ymd,
    extract_em_articles,
    mk,
    parse_dt,
    parse_jsonp,
)


def test_compact_ymd():
    assert compact_ymd("20240101") == "2024-01-01"
    assert compact_ymd("2024-01-01") == "2024-01-01"
    assert compact_ymd("") == ""


def test_parse_dt():
    assert parse_dt("2024-01-05 10:00:00").strftime("%Y-%m-%d") == "2024-01-05"
    assert parse_dt("2024-01-05").strftime("%Y-%m-%d") == "2024-01-05"
    assert parse_dt(0) is not None
    assert parse_dt(None) is None
    assert parse_dt("garbage") is None


def test_parse_jsonp():
    assert parse_jsonp('jQuery123({"result":{"a":1}})') == {"result": {"a": 1}}
    assert parse_jsonp('{"code":0}') == {"code": 0}
    assert parse_jsonp("") == {}
    assert parse_jsonp("no json here") == {}


def test_extract_em_articles():
    assert extract_em_articles({}) == []
    assert extract_em_articles({"result": {"cmsArticleWebOld": [{"title": "x"}]}}) == [{"title": "x"}]
    assert extract_em_articles({"data": {"list": [{"title": "y"}]}}) == [{"title": "y"}]
    assert extract_em_articles([{"title": "z"}]) == [{"title": "z"}]


def test_cik10():
    assert cik10(320193) == "0000320193"
    assert cik10("320193") == "0000320193"


def test_mk_item():
    it = mk(parse_dt("2024-01-05"), "SEC 8-K", "AAPL 8-K 申报", "")
    assert it is not None
    assert it["date"] == "2024-01-05"
    assert it["source"] == "SEC 8-K"
    assert len(it["id"]) == 16
    assert it["symbols"] == []


def test_em_to_item():
    a = {"date": "2024-01-05 10:00:00", "title": "贵州茅台公告", "content": "", "mediaName": "证券时报"}
    it = _em_to_item(a, ["600519"])
    assert it is not None
    assert it["date"] == "2024-01-05"
    assert it["symbols"] == ["600519"]
    assert it["source"] == "东方财富个股新闻"
    # 缺标题缺内容 -> 丢弃
    assert _em_to_item({"date": "2024-01-05"}, ["600519"]) is None


def test_em_to_item_strips_html():
    """回归：东财高亮词带 <em> 标签，应被剥除。"""
    a = {"date": "2024-01-05 10:00:00", "title": "<em>贵州茅台</em>股价上涨", "content": "A股 &amp; 白酒"}
    it = _em_to_item(a, ["600519"])
    assert it is not None
    assert "<em>" not in it["title"]
    assert "贵州茅台" in it["title"]
    assert "&amp;" not in it["content"]
    assert "&" in it["content"]
