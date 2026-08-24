"""多源抓取器：一手新闻/政府源 + 社媒/另类数据 + 中文快讯（辅）。

归一化字段：id, ts, date, source, title, content, url, symbols,
            lang(en/zh), kind(news/congress_trade/insider_trade), ticker, politician
每个源独立容错，单个失败不影响整体。
"""

import hashlib
import html as _html
import os
import xml.etree.ElementTree as ET
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

TZ = ZoneInfo("Asia/Shanghai")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _parse_dt(v):
    """把多种时间格式解析为 tz-aware datetime，失败返回 None。"""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(int(v), tz=TZ)
        except (ValueError, OSError, OverflowError):
            return None
    s = str(v).strip()
    try:
        dt = pd.to_datetime(s, errors="coerce")
        if pd.isna(dt):
            return None
        dt = dt.to_pydatetime()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ)
    except (ValueError, TypeError):
        return None


def _mk(dt, source, title, content, url="", lang="zh", kind="news",
        ticker="", politician=""):
    if dt is None:
        return None
    title = (title or "").strip()
    content = (content or "").strip()
    key = f"{source}|{title or content[:40]}|{dt.isoformat()}"
    return {
        "id": hashlib.md5(key.encode("utf-8")).hexdigest()[:16],
        "ts": dt.isoformat(),
        "date": dt.strftime("%Y-%m-%d"),
        "source": source,
        "title": title,
        "content": content,
        "url": url or "",
        "symbols": [],
        "lang": lang,
        "kind": kind,
        "ticker": ticker or "",
        "politician": politician or "",
    }


def _rows_to_items(df, source, title_col, content_col, time_col, url_col=None):
    items = []
    if df is None or df.empty:
        return items
    for _, r in df.iterrows():
        try:
            dt = _parse_dt(r.get(time_col))
            title = str(r.get(title_col) or "") if title_col else ""
            content = str(r.get(content_col) or "") if content_col else ""
            url = str(r.get(url_col) or "") if url_col else ""
            it = _mk(dt, source, title, content, url)
            if it:
                items.append(it)
        except Exception:  # noqa: BLE001
            continue
    return items


def _fetch_rss(url, source, lang="en", params=None, strip_source=True):
    """通用 RSS 抓取。Google News 标题形如 '标题 - 来源'，默认去掉来源后缀。"""
    import requests
    r = requests.get(url, params=params or {}, headers=UA, timeout=15)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    items = []
    for node in root.findall(".//item"):
        title = _html.unescape(node.findtext("title") or "").strip()
        link = node.findtext("link") or ""
        desc = _html.unescape(node.findtext("description") or "").strip()
        pub = node.findtext("pubDate") or ""
        dt = _parse_dt(pub)
        if strip_source and " - " in title:
            title = title.rsplit(" - ", 1)[0].strip()
        obj = _mk(dt, source, title, desc, link, lang=lang)
        if obj:
            items.append(obj)
    return items


def _gnews_rss(query, source, lang="en", hl="en-US", gl="US", ceid="US:en"):
    url = "https://news.google.com/rss/search"
    params = {"q": query, "hl": hl, "gl": gl, "ceid": ceid}
    return _fetch_rss(url, source, lang=lang, params=params)


# ================= 一手新闻/政府源 =================

def fetch_ap():
    return _gnews_rss("site:apnews.com", "AP", "en")


def fetch_reuters():
    return _gnews_rss("source:Reuters", "Reuters", "en")


def fetch_afp():
    return _gnews_rss("source:AFP", "AFP", "en")


def fetch_bloomberg():
    return _fetch_rss("https://feeds.bloomberg.com/markets/news.rss", "彭博", "en")


def fetch_whitehouse():
    return _gnews_rss("site:whitehouse.gov", "白宫", "en")


def fetch_mfa():
    # 中国外交部例行记者会/发言人表态（中文）
    return _gnews_rss("外交部 例行记者会 OR 外交部 发言人", "中国外交部",
                      lang="zh", hl="zh-CN", gl="CN", ceid="CN:zh-Hans")


def fetch_congress():
    return _gnews_rss("congressional hearing", "国会听证会", "en")


# ================= 社媒/另类数据源 =================

def fetch_truthsocial():
    return _fetch_rss("https://trumpstruth.org/feed/", "特朗普 Truth Social", "en")


def _quiver_items(dataset, source, kind):
    token = os.environ.get("QUIVER_TOKEN", "").strip()
    if not token:
        return []
    import requests
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://api.quiverquant.com/beta/live/{dataset}"
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code != 200:
            return []
        data = r.json()
        rows = data if isinstance(data, list) else (data.get("data") or data.get("results") or [])
    except Exception:  # noqa: BLE001
        return []
    items = []
    for rec in rows:
        ticker = str(rec.get("Ticker") or rec.get("ticker") or "").upper()
        pol = str(rec.get("Representative") or rec.get("Name")
                  or rec.get("politician") or "").strip()
        side = str(rec.get("Transaction") or rec.get("Type")
                   or rec.get("transaction_type") or "").strip()
        amount = rec.get("Amount") or rec.get("amount") or ""
        d = rec.get("Date") or rec.get("date") or rec.get("ReportDate") or rec.get("filed_date")
        dt = _parse_dt(d)
        title = f"{pol} {side} {ticker} ${amount}".strip() if ticker else f"{pol} {side}".strip()
        obj = _mk(dt, source, title, "", "", lang="en", kind=kind,
                  ticker=ticker, politician=pol)
        if obj:
            items.append(obj)
    return items


def fetch_quiver():
    items = _quiver_items("congresstrading", "Quiver国会交易", "congress_trade")
    items += _quiver_items("insidertrading", "Quiver内部人交易", "insider_trade")
    return items


def fetch_bargo():
    base = os.environ.get("BARGO_BASE_URL", "https://www.bargo.ai/free-apis/congress/v1").strip()
    key = os.environ.get("BARGO_API_KEY", "").strip()
    import requests
    headers = dict(UA)
    if key:
        headers["X-API-Key"] = key
    try:
        r = requests.get(f"{base.rstrip('/')}/trades", headers=headers,
                         params={"limit": 50}, timeout=15)
        if r.status_code != 200:
            return []
        data = r.json()
        rows = data.get("trades") or data.get("data") or []
    except Exception:  # noqa: BLE001
        return []
    items = []
    for rec in rows:
        ticker = str(rec.get("ticker") or "").upper()
        pol = str(rec.get("member") or "").strip()
        raw_type = str(rec.get("type") or "").lower()
        side = "买入" if raw_type == "purchase" else ("卖出" if raw_type == "sale" else raw_type)
        amount = rec.get("amount_range") or rec.get("amount") or ""
        d = rec.get("transaction_date") or rec.get("disclosure_date")
        dt = _parse_dt(d)
        title = f"{pol} {side} {ticker} {amount}".strip()
        obj = _mk(dt, "Bargo国会交易", title, "", "", lang="en",
                  kind="congress_trade", ticker=ticker, politician=pol)
        if obj:
            items.append(obj)
    return items
def fetch_fed():
    """美联储官方新闻/声明 RSS。"""
    return _fetch_rss("https://www.federalreserve.gov/feeds/press_all.xml", "美联储Fed", "en")


def fetch_sec_edgar():
    """SEC EDGAR 最新 8-K 申报（Atom）。"""
    import requests
    url = "https://www.sec.gov/cgi-bin/browse-edgar"
    params = {"action": "getcurrent", "type": "8-K", "dateb": "",
              "owner": "include", "count": "40", "output": "atom"}
    headers = {"User-Agent": "chaogu-research contact@example.com"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=15)
        r.raise_for_status()
        ns = {"a": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(r.content)
        items = []
        for e in root.findall("a:entry", ns):
            title = (e.findtext("a:title", default="") or "").strip()
            updated = e.findtext("a:updated", default="") or ""
            link = ""
            ln = e.find("a:link", ns)
            if ln is not None:
                link = ln.get("href") or ""
            dt = _parse_dt(updated)
            obj = _mk(dt, "SEC EDGAR", title, "", link, lang="en")
            if obj:
                items.append(obj)
        return items
    except Exception:  # noqa: BLE001
        return []


def fetch_fred():
    """FRED 宏观序列最新值：优先用 FRED_API_KEY，否则用免 key 的 CSV 端点。"""
    key = os.environ.get("FRED_API_KEY", "").strip()
    series = [("GDP", "GDP"), ("CPIAUCSL", "CPI"), ("PPIACO", "PPI"),
              ("UNRATE", "失业率"), ("PCE", "PCE"), ("DGS10", "10Y美债")]
    items = []
    if key:
        import requests
        for sid, label in series:
            try:
                r = requests.get("https://api.stlouisfed.org/fred/series/observations",
                                 params={"series_id": sid, "api_key": key,
                                         "file_type": "json", "sort_order": "desc", "limit": "1"},
                                 timeout=12)
                obs = r.json().get("observations", [])
                if obs:
                    val = obs[0].get("value")
                    d = obs[0].get("date")
                    dt = _parse_dt(d)
                    title = f"FRED {label}({sid}) 最新值 {val}"
                    obj = _mk(dt, "FRED宏观数据", title, "",
                              f"https://fred.stlouisfed.org/series/{sid}", lang="en")
                    if obj:
                        items.append(obj)
            except Exception:  # noqa: BLE001
                continue
        return items
    # 免 key CSV
    import requests
    for sid, label in series:
        try:
            r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv",
                             params={"id": sid}, headers=UA, timeout=15)
            lines = r.text.strip().splitlines()
            if len(lines) >= 2:
                last = lines[-1].split(",")
                d, val = last[0], last[1] if len(last) > 1 else ""
                dt = _parse_dt(d)
                title = f"FRED {label}({sid}) 最新值 {val}"
                obj = _mk(dt, "FRED宏观数据", title, "",
                          f"https://fred.stlouisfed.org/series/{sid}", lang="en")
                if obj:
                    items.append(obj)
        except Exception:  # noqa: BLE001
            continue
    return items
def fetch_northbound():
    """沪深港通北向资金（akshare，best-effort）。

    时间戳用数据里的交易日而非抓取时刻：否则每轮轮询都会生成新 id，
    导致 monitor 的去重失效、raw 归档无限重复追加。
    """
    import akshare as ak
    try:
        df = ak.stock_hsgt_fund_flow_summary_em()
        if df is None or df.empty:
            return []
        row = df.iloc[0]
        cols = {c: str(c) for c in df.columns}
        text = " | ".join(f"{k}: {row[k]}" for k in df.columns[:6])
        dt = None
        for k in ("交易日", "日期", "date", "Date"):
            if k in cols:
                dt = _parse_dt(str(row[k]))
                if dt is not None:
                    break
        if dt is None:
            # 兜底：抓取当日 15:00（收盘后），至少保证同一交易日内 id 稳定
            dt = datetime.now(TZ).replace(hour=15, minute=0, second=0, microsecond=0)
        obj = _mk(dt, "北向资金", "沪深港通资金流", text, "", lang="zh")
        return [obj] if obj else []
    except Exception:  # noqa: BLE001
        return []


def _make_gnews_fetcher(name, query, lang, hl, gl, ceid):
    def _f():
        return _gnews_rss(query, name, lang=lang, hl=hl, gl=gl, ceid=ceid)
    _f.__name__ = f"fetch_{name}"
    return _f


_GNEWS = [
    ("欧洲央行ECB", "European Central Bank rate decision", "en", "en-US", "US", "US:en"),
    ("日本央行BOJ", "Bank of Japan rate decision", "en", "en-US", "US", "US:en"),
    ("中国人民银行", "中国人民银行 LPR OR 降准 OR 降息 OR 社融", "zh", "zh-CN", "CN", "CN:zh-Hans"),
    ("英国央行BOE", "Bank of England rate decision", "en", "en-US", "US", "US:en"),
    ("美国非农/CPI", "nonfarm payrolls OR CPI inflation BLS", "en", "en-US", "US", "US:en"),
    ("美国GDP/PCE", "GDP OR PCE BEA", "en", "en-US", "US", "US:en"),
    ("ISM PMI", "ISM manufacturing PMI", "en", "en-US", "US", "US:en"),
    ("EIA原油库存", "EIA crude oil inventory", "en", "en-US", "US", "US:en"),
    ("OPEC/IEA", "OPEC OR IEA oil report", "en", "en-US", "US", "US:en"),
    ("中国宏观数据", "中国 GDP OR CPI OR PMI OR 进出口 OR 社融", "zh", "zh-CN", "CN", "CN:zh-Hans"),
    ("半导体行业", "SEMI semiconductor equipment OR WSTS", "en", "en-US", "US", "US:en"),
    ("航运BDI", "Baltic Dry Index shipping", "en", "en-US", "US", "US:en"),
    ("CFTC持仓", "CFTC commitments of traders", "en", "en-US", "US", "US:en"),
    ("VIX波动率", "VIX CBOE volatility", "en", "en-US", "US", "US:en"),
    ("AAII情绪", "AAII investor sentiment survey", "en", "en-US", "US", "US:en"),
    ("美国国务院", "site:state.gov", "en", "en-US", "US", "US:en"),
    ("IMF/世界银行", "IMF OR World Bank outlook", "en", "en-US", "US", "US:en"),
]

_GNEWS_FETCHERS = {
    name: _make_gnews_fetcher(name, q, lang, hl, gl, ceid)
    for name, q, lang, hl, gl, ceid in _GNEWS
}

# ================= 中文快讯（辅） =================

def fetch_cls():
    import akshare as ak
    df = ak.stock_info_global_cls()
    items = []
    if df is not None and not df.empty:
        for _, r in df.iterrows():
            dt = _parse_dt(f"{r.get('发布日期', '')} {r.get('发布时间', '')}")
            it = _mk(dt, "财联社", r.get("标题", ""), r.get("内容", ""), "")
            if it:
                items.append(it)
    return items


def fetch_em():
    import akshare as ak
    df = ak.stock_info_global_em()
    return _rows_to_items(df, "东财全球资讯", "标题", "摘要", "发布时间", "链接")


def fetch_sina():
    import akshare as ak
    df = ak.stock_info_global_sina()
    items = []
    if df is not None and not df.empty:
        for _, r in df.iterrows():
            dt = _parse_dt(r.get("时间", ""))
            # 新浪快讯无标题：正文只放 content，避免 title==content 导致情绪/关键词/体积翻倍
            it = _mk(dt, "新浪7x24", "", r.get("内容", ""), "")
            if it:
                items.append(it)
    return items


def fetch_ths():
    import akshare as ak
    df = ak.stock_info_global_ths()
    return _rows_to_items(df, "同花顺7x24", "标题", "内容", "发布时间", "链接")


def fetch_futu():
    import akshare as ak
    df = ak.stock_info_global_futu()
    return _rows_to_items(df, "富途牛牛", "标题", "内容", "发布时间", "链接")


def fetch_wallstcn():
    import requests
    url = "https://api-one.wallstcn.com/apiv1/content/lives"
    r = requests.get(url, params={"channel": "global-channel", "limit": "50"}, timeout=12)
    r.raise_for_status()
    items = []
    for it in r.json().get("data", {}).get("items", []):
        content = it.get("content_text") or it.get("content") or ""
        title = it.get("title") or ""
        dt = _parse_dt(it.get("display_time"))
        uri = it.get("uri") or ""
        url = f"https://wallstreetcn.com{uri}" if uri else ""
        obj = _mk(dt, "华尔街见闻", title, content, url)
        if obj:
            items.append(obj)
    return items


def fetch_jin10():
    import requests
    headers = dict(UA)
    headers.update({"Referer": "https://www.jin10.com/",
                    "x-app-id": "bVBF4FyRTn5NJF5n", "x-version": "1.0.0"})
    url = "https://flash-api.jin10.com/get_flash_list"
    r = requests.get(url, params={"max_time": ""}, headers=headers, timeout=12)
    r.raise_for_status()
    items = []
    for it in r.json().get("data", []):
        body = it.get("data", {})
        content = body.get("content") or ""
        dt = _parse_dt(body.get("time") or it.get("time"))
        obj = _mk(dt, "金十快讯", "", content, "")
        if obj:
            items.append(obj)
    return items


def fetch_policy():
    import requests
    url = "https://www.gov.cn/zhengce/zuixin/rss.xml"
    r = requests.get(url, timeout=12)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    items = []
    for node in root.findall(".//item"):
        title = node.findtext("title") or ""
        desc = node.findtext("description") or ""
        link = node.findtext("link") or ""
        pub = node.findtext("pubDate") or ""
        dt = _parse_dt(pub)
        obj = _mk(dt, "政策公告", title, desc, link)
        if obj:
            items.append(obj)
    return items


SOURCES = [
    # 一手新闻社 / 政府官网
    ("AP", fetch_ap),
    ("Reuters", fetch_reuters),
    ("AFP", fetch_afp),
    ("彭博", fetch_bloomberg),
    ("白宫", fetch_whitehouse),
    ("中国外交部", fetch_mfa),
    ("国会听证会", fetch_congress),
    ("特朗普 Truth Social", fetch_truthsocial),
    # 另类数据
    ("Quiver另类数据", fetch_quiver),
    ("Bargo国会交易", fetch_bargo),
    # 央行与货币政策
    ("美联储Fed", fetch_fed),
    ("欧洲央行ECB", _GNEWS_FETCHERS["欧洲央行ECB"]),
    ("日本央行BOJ", _GNEWS_FETCHERS["日本央行BOJ"]),
    ("中国人民银行", _GNEWS_FETCHERS["中国人民银行"]),
    ("英国央行BOE", _GNEWS_FETCHERS["英国央行BOE"]),
    # 宏观经济数据
    ("FRED宏观数据", fetch_fred),
    ("美国非农/CPI", _GNEWS_FETCHERS["美国非农/CPI"]),
    ("美国GDP/PCE", _GNEWS_FETCHERS["美国GDP/PCE"]),
    ("ISM PMI", _GNEWS_FETCHERS["ISM PMI"]),
    ("EIA原油库存", _GNEWS_FETCHERS["EIA原油库存"]),
    ("OPEC/IEA", _GNEWS_FETCHERS["OPEC/IEA"]),
    ("中国宏观数据", _GNEWS_FETCHERS["中国宏观数据"]),
    # 行业/商品
    ("半导体行业", _GNEWS_FETCHERS["半导体行业"]),
    ("航运BDI", _GNEWS_FETCHERS["航运BDI"]),
    # 公司财报/公告
    ("SEC EDGAR", fetch_sec_edgar),
    ("北向资金", fetch_northbound),
    # 市场内部
    ("CFTC持仓", _GNEWS_FETCHERS["CFTC持仓"]),
    ("VIX波动率", _GNEWS_FETCHERS["VIX波动率"]),
    ("AAII情绪", _GNEWS_FETCHERS["AAII情绪"]),
    # 地缘/国际组织
    ("美国国务院", _GNEWS_FETCHERS["美国国务院"]),
    ("IMF/世界银行", _GNEWS_FETCHERS["IMF/世界银行"]),
    # 中文快讯（辅）
    ("财联社", fetch_cls),
    ("东财全球资讯", fetch_em),
    ("新浪7x24", fetch_sina),
    ("同花顺7x24", fetch_ths),
    ("富途牛牛", fetch_futu),
    ("华尔街见闻", fetch_wallstcn),
    ("金十快讯", fetch_jin10),
    ("政策公告", fetch_policy),
]


# ---- 个股新闻（为每只 A股抓取并预打标签） ----
def fetch_symbol_news(symbols_cfg):
    import akshare as ak
    items = []
    for s in symbols_cfg:
        if s.get("market") != "cn":
            continue
        code = str(s.get("code") or "")
        if not code:
            continue
        try:
            df = ak.stock_news_em(symbol=code)
        except Exception:  # noqa: BLE001
            continue
        if df is None or df.empty:
            continue
        for _, r in df.iterrows():
            dt = _parse_dt(r.get("发布时间", ""))
            it = _mk(dt, "个股新闻", r.get("新闻标题", ""), r.get("新闻内容", ""),
                     r.get("新闻链接", ""))
            if it:
                it["symbols"] = [s["symbol"]]
                items.append(it)
    return items

def filter_recent(items, days: int = 30):
    """只保留近 N 天条目，过滤 Google News RSS 返回的历史旧闻。"""
    from datetime import timedelta
    cutoff = (datetime.now(TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
    return [it for it in items if (it.get("date") or "") >= cutoff]




def apply_primary_keys(primary: dict) -> None:
    """把 config['primary'] 里的密钥注入环境变量（env 已存在则优先，不覆盖）。

    供 run.py / monitor.py 在 load_config 后调用，使 config.yaml 里的
    AP/国会/Quiver/Bargo/FRED 密钥无需额外设环境变量即可生效。
    """
    mapping = {
        "ap_api_key": "AP_API_KEY",
        "congress_api_key": "CONGRESS_API_KEY",
        "quiver_token": "QUIVER_TOKEN",
        "bargo_base_url": "BARGO_BASE_URL",
        "bargo_api_key": "BARGO_API_KEY",
        "fred_api_key": "FRED_API_KEY",
        "finnhub_api_key": "FINNHUB_API_KEY",
    }
    for k, env in mapping.items():
        v = str(primary.get(k) or "").strip()
        if v and not os.environ.get(env):
            os.environ[env] = v


def load_env_file(env_path) -> None:
    """Load KEY=VALUE lines from a .env file into os.environ (existing env wins)."""
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip()
                if k and not os.environ.get(k):
                    os.environ[k] = v
    except FileNotFoundError:
        pass
