"""历史新闻回填（可回填的历史新闻源）。

解决免费快讯源"只返回最近数日、无法回填历史"的问题：

- A股：东方财富资讯搜索接口，按股票代码/名称分页回填个股新闻（免 key，可回填较长时间）。
- 美股：SEC EDGAR 8-K 申报（免 key，全历史）+ Finnhub company-news（免费 key，约 2 周近期新闻，可选）。

产物：把归一化后的新闻追加写入 news/raw/{date}.jsonl，之后配合
news_aggregator.run.rebuild_history_from_raw() 即可重建跨年情绪历史。

本模块刻意不依赖 pandas/vnpy，便于单独测试与复用；网络请求惰性导入。
"""

from __future__ import annotations

import hashlib
import html
import json
import pathlib
import re
import time
from datetime import datetime, timedelta, timezone as _dt_timezone
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parents[1]
NEWS_DIR = ROOT / "news"
RAW_DIR = NEWS_DIR / "raw"
DATA_DIR = ROOT / "data"

try:
    TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # Windows 无 tzdata 时回退到固定 UTC+8（与上海无夏令时等价）
    TZ = _dt_timezone(timedelta(hours=8))
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}
# SEC EDGAR 要求请求头带联系方式
SEC_UA = "FlashQuant research contact@example.com"


# ---------- 时间 / 归一化工具（与 fetchers 保持同一 id 规则，跨源去重） ----------

def compact_ymd(s: str) -> str:
    """'20240101' -> '2024-01-01'；已带横线的原样截取前 10 位。"""
    s = str(s).strip()
    if "-" in s:
        return s[:10]
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def parse_dt(v):
    """解析常见时间格式为 tz-aware datetime，失败返回 None。"""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(int(v), tz=TZ)
    s = str(v).strip().replace("Z", "+00:00")
    dt = None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def mk(dt, source: str, title: str, content: str, url: str = "", lang: str = "zh") -> dict | None:
    """构造归一化 item（id 规则与 fetchers._mk 一致，保证跨源去重）。"""
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
        "kind": "news",
        "ticker": "",
        "politician": "",
    }


def parse_jsonp(text: str) -> dict:
    """去掉 JSONP 包装，返回解析后的 dict（失败返回 {}）。"""
    if not text:
        return {}
    s = text.find("{")
    e = text.rfind("}")
    if s < 0 or e <= s:
        return {}
    try:
        return json.loads(text[s:e + 1])
    except json.JSONDecodeError:
        return {}


# ---------- 东财个股新闻（A股，免 key） ----------

def extract_em_articles(obj) -> list:
    """从东财搜索接口响应里抽出文章列表（字段名做兼容）。"""
    if not obj:
        return []
    if isinstance(obj, list):
        return obj
    root = obj.get("result") or obj.get("data") or obj
    if isinstance(root, dict):
        for key in ("cmsArticleWebOld", "list", "articles", "items"):
            if isinstance(root.get(key), list):
                return root[key]
    if isinstance(root, list):
        return root
    return []


def _clean_html(s: str) -> str:
    """去掉 HTML 标签（东财高亮词带 <em>）并反转义实体。"""
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def _em_to_item(a: dict, symbols: list) -> dict | None:
    title = _clean_html(str(a.get("title") or a.get("标题") or ""))
    content = _clean_html(str(a.get("content") or a.get("summary") or a.get("摘要") or ""))
    if not title and not content:
        return None
    d = a.get("date") or a.get("showTime") or a.get("time") or a.get("发布时间")
    dt = parse_dt(d)
    if dt is None:
        return None
    media = _clean_html(str(a.get("mediaName") or a.get("source") or ""))
    url = str(a.get("url") or a.get("uniqueUrl") or "").strip()
    # 摘要缺失时用来源媒体名兜底，给情绪打分多一点素材
    if not content and media:
        content = f"来源：{media}"
    it = mk(dt, "东方财富个股新闻", title, content, url, lang="zh")
    if it:
        it["symbols"] = list(symbols)
    return it


def fetch_em_stock_news(keyword: str, symbol: str, start_date: str, max_pages: int = 30,
                        page_size: int = 100) -> list:
    """分页回填东财个股新闻，直到日期早于 start_date（YYYY-MM-DD）或翻页耗尽。

    keyword：搜索词（建议用中文简称/全称，命中质量更高）；symbol：打标签用的标的代码。
    实测东财搜索接口约能翻 29 页（~2900 条），深度因个股关注度而异（通常半年以上）。

    注意：本接口对 `requests` 库反爬（返回空或非资讯结果），故用 stdlib 的 urllib。
    """
    import urllib.parse
    import urllib.request

    items = []
    url = "https://search-api-web.eastmoney.com/search/jsonp"
    headers = {"User-Agent": UA["User-Agent"], "Referer": "https://so.eastmoney.com/"}
    for page in range(1, max_pages + 1):
        param = {
            "uid": "",
            "keyword": keyword,
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "clientVersion": "curr",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "default",
                    "pageIndex": page,
                    "pageSize": page_size,
                    "preTag": "",
                    "postTag": "",
                }
            },
        }
        cb = f"jQuery1124{int(time.time() * 1000)}_{int(time.time() * 1000)}"
        qs = urllib.parse.urlencode({"cb": cb, "param": json.dumps(param, ensure_ascii=False)})
        try:
            req = urllib.request.Request(url + "?" + qs, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as r:
                obj = parse_jsonp(r.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            print(f"  [backfill] 东财搜索第{page}页失败: {type(e).__name__}")
            break
        arts = extract_em_articles(obj)
        if not arts:
            break
        got = 0
        stop = False
        for a in arts:
            it = _em_to_item(a, [symbol])
            if not it:
                continue
            if it["date"] < start_date:
                stop = True
                break
            items.append(it)
            got += 1
        if stop or got < page_size:
            break
        time.sleep(0.5)  # 温和限速
    return items


def fetch_em_announcements(code: str, symbol: str, start_date: str, page_size: int = 50) -> list:
    """分页回填东财公告（np-anotice-stock），全历史，通常可回填十数年。

    公告（业绩预告/增减持/重大合同/分红/处罚等）比新闻更结构化、信号更明确，
    是 A股深度历史情绪的主要来源。按 notice_date 倒序翻页，直到早于 start_date。
    """
    import urllib.parse
    import urllib.request

    items = []
    url = "https://np-anotice-stock.eastmoney.com/api/security/ann"
    headers = {"User-Agent": UA["User-Agent"]}
    page = 1
    while True:
        params = {
            "sr": "-1",
            "page_size": str(page_size),
            "page_index": str(page),
            "ann_type": "A",
            "client_source": "web",
            "stock_list": code,
            "f_node": "0",
            "s_node": "0",
        }
        try:
            req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params), headers=headers)
            with urllib.request.urlopen(req, timeout=20) as r:
                obj = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            print(f"  [backfill] 东财公告第{page}页失败: {type(e).__name__}")
            break
        lst = (obj.get("data") or {}).get("list") or []
        if not lst:
            break
        got = 0
        stop = False
        for a in lst:
            title = _clean_html(str(a.get("title_ch") or a.get("title") or ""))
            if not title:
                continue
            dt = parse_dt(a.get("notice_date") or a.get("display_time") or "")
            if dt is None:
                continue
            if dt.strftime("%Y-%m-%d") < start_date:
                stop = True
                break
            art = str(a.get("art_code") or "")
            detail = f"https://data.eastmoney.com/notices/detail/{code}/{art}.html" if art else ""
            it = mk(dt, "东方财富公告", title, "", detail, lang="zh")
            if it:
                it["symbols"] = [symbol]
                items.append(it)
                got += 1
        if stop or got < page_size:
            break
        page += 1
        time.sleep(0.4)  # 温和限速
    return items


# ---------- SEC EDGAR 8-K（美股，免 key，全历史） ----------

_TICKERS_CACHE = DATA_DIR / "company_tickers.json"


def cik10(cik) -> str:
    """CIK 补零到 10 位（SEC submissions 接口要求）。"""
    return str(cik).zfill(10)


def lookup_cik(ticker: str) -> str:
    """由 ticker 查 CIK；company_tickers.json 本地缓存（SEC 官方映射）。"""
    import requests

    if not _TICKERS_CACHE.exists():
        _TICKERS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        r = requests.get("https://www.sec.gov/files/company_tickers.json",
                         headers={"User-Agent": SEC_UA}, timeout=30)
        r.raise_for_status()
        _TICKERS_CACHE.write_text(r.text, encoding="utf-8")
    data = json.loads(_TICKERS_CACHE.read_text(encoding="utf-8"))
    ticker = ticker.upper()
    for rec in data.values():
        if rec.get("ticker") == ticker:
            return str(rec.get("cik_str") or "")
    return ""


def _extract_8k_text(raw: str) -> tuple[str, str]:
    """从 8-K 文档提取 (简短标题, 正文文本)。

    去掉 SEC 表头样板，取第一个 Item 之后、Item 9.01（展品清单）之前的实质内容，
    作为情绪打分的素材（8-K 标题本身是通用文案，无情绪信息）。
    """
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    title = ""
    m = re.search(r"(Item\s+\d+\.\d+\s+[^.]{0,90}\.)", text)
    if m:
        title = m.group(1).strip()
        seg = text[m.start():]
    else:
        seg = text
    cut = re.search(r"Item\s+9\.01", seg)
    if cut:
        seg = seg[:cut.start()]
    return title, seg.strip()[:2500]


def _plain_text(raw: str, max_len: int = 3000) -> str:
    """去掉 HTML 标签，返回可见文本（用于展品/新闻稿）。"""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len]


def _fetch_8k_exhibit(cik: int, acc: str) -> str:
    """抓取 8-K 的展品（通常 ex99 = 新闻稿），返回可见文本。失败返回空串。"""
    import requests

    idx_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/index.json"
    try:
        r = requests.get(idx_url, headers={"User-Agent": SEC_UA}, timeout=20)
        r.raise_for_status()
        items = r.json().get("directory", {}).get("item", [])
    except Exception:  # noqa: BLE001
        return ""
    ex = None
    for it in items:
        name = str(it.get("name") or "")
        if re.search(r"ex[-_]?\d", name, re.I):
            if re.search(r"ex[-_]?99", name, re.I):
                ex = name
                break
            ex = ex or name
    if not ex:
        return ""
    try:
        r = requests.get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{ex}",
                         headers={"User-Agent": SEC_UA}, timeout=20)
        return _plain_text(r.text)
    except Exception:  # noqa: BLE001
        return ""


def fetch_sec_8k(ticker: str, start_date: str, max_items: int = 1000) -> list:
    """拉取某只美股的历史 8-K 申报（SEC submissions）。

    `filings.recent` 已覆盖最近约 1000 条申报（8-K 通常回溯 10 年以上），
    对 2024+ 的回测区间绰绰有余；更早需再翻 `filings.files`（暂不需要）。
    每条 8-K 抓取正文 + 新闻稿展品（ex99），提取文本供情绪打分。
    """
    import requests

    cik = lookup_cik(ticker)
    if not cik:
        print(f"  [backfill] {ticker} 未在 SEC 映射中找到 CIK，跳过 8-K")
        return []
    url = f"https://data.sec.gov/submissions/CIK{cik10(cik)}.json"
    try:
        r = requests.get(url, headers={"User-Agent": SEC_UA}, timeout=20)
        r.raise_for_status()
        fil = r.json().get("filings", {}).get("recent", {})
    except Exception as e:  # noqa: BLE001
        print(f"  [backfill] {ticker} SEC 申报获取失败: {type(e).__name__}")
        return []

    forms = fil.get("form", [])
    dates = fil.get("filingDate", [])
    accs = fil.get("accessionNumber", [])
    docs = fil.get("primaryDocument", [])
    cik_int = int(cik)

    items = []
    for i in range(min(len(forms), max_items)):
        if forms[i] != "8-K":
            continue
        d = str(dates[i])[:10]
        if d < start_date:
            continue
        acc = str(accs[i]).replace("-", "")
        doc = str(docs[i]) if i < len(docs) else ""
        url_ = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc}/{doc}" if doc else ""
        hint, body = "", ""
        if doc:
            try:
                raw = requests.get(url_, headers={"User-Agent": SEC_UA}, timeout=20).text
                hint, body = _extract_8k_text(raw)
            except Exception as e:  # noqa: BLE001
                print(f"  [backfill] {ticker} 8-K 正文获取失败({d}): {type(e).__name__}")
            # 新闻稿展品（ex99）文本比 8-K 正文更丰富，优先作为情绪素材
            exhibit = _fetch_8k_exhibit(cik_int, acc) if acc else ""
            if exhibit:
                body = exhibit
        title = f"{ticker} 8-K {hint}".strip() if hint else f"{ticker} 8-K 申报"
        it = mk(parse_dt(d), "SEC 8-K", title, body, url_, lang="en")
        if it:
            it["symbols"] = [ticker]
            items.append(it)
        time.sleep(0.2)  # SEC 限速（10 req/s 上限，留足余量）
    return items


# ---------- Finnhub company-news（美股，免费 key，约 1 年） ----------

def fetch_finnhub_news(ticker: str, start_date: str, end_date: str, token: str) -> list:
    """拉取 Finnhub 公司新闻（免费 tier 约回填 1 年）。token 为空则跳过。"""
    if not token:
        return []
    import requests

    url = "https://finnhub.io/api/v1/company-news"
    try:
        r = requests.get(url, params={
            "symbol": ticker, "from": start_date, "to": end_date, "token": token,
        }, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # noqa: BLE001
        print(f"  [backfill] {ticker} Finnhub 新闻获取失败: {type(e).__name__}")
        return []

    items = []
    for a in data or []:
        ts = a.get("datetime")
        dt = parse_dt(ts)
        head = str(a.get("headline") or "").strip()
        summ = str(a.get("summary") or "").strip()
        if dt is None or not head:
            continue
        it = mk(dt, "Finnhub", head, summ, str(a.get("url") or ""), lang="en")
        if it:
            it["symbols"] = [ticker]
            items.append(it)
    return items


# ---------- 归档 ----------

def archive(items: list) -> int:
    """按日期追加写入 news/raw/{date}.jsonl（幂等：跳过文件里已存在的 id）。"""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    by_day: dict = {}
    for it in items:
        by_day.setdefault(it.get("date", "unknown"), []).append(it)
    written = 0
    for day, lst in by_day.items():
        path = RAW_DIR / f"{day.replace('-', '')}.jsonl"
        existing = set()
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    existing.add(json.loads(line).get("id"))
                except json.JSONDecodeError:
                    continue
        with open(path, "a", encoding="utf-8") as f:
            for it in lst:
                if it.get("id") in existing:
                    continue
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
                written += 1
    return written
