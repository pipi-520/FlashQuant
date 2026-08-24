"""用 LLM（DeepSeek）批量重打新闻情绪分，覆盖词典分数。

读 news/raw/*.jsonl 的所有新闻（去重），分批调用 LLM 批量打分，
结果写入 news/llm_sentiment.json（{id: score}）。支持断点续跑。
随后用 scripts/llm_rebuild.py 基于该分数重建情绪历史。

用法（项目根目录）：
    .venv/Scripts/python.exe scripts/llm_rescore.py [--batch 20] [--limit 0]
"""

import argparse
import glob
import json
import os
import pathlib
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 加载 .env
for line in open(ROOT / ".env", encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

import requests  # noqa: E402

API_BASE = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
API_KEY = os.environ.get("OPENAI_API_KEY", "")
MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
SCORED_PATH = ROOT / "news" / "llm_sentiment.json"


# 只重打这些「事件级 / 美股新闻」源（有实质内容、最可能从 LLM 受益）；
# A股快讯与聚合器历史快讯量大、信号弱，保持词典打分。
RESCORE_SOURCES = {"东方财富公告", "SEC 8-K", "Finnhub"}


def load_all_items() -> list:
    """读指定源的新闻（去重，按 date 排序）。"""
    items = {}
    for f in glob.glob(str(ROOT / "news" / "raw" / "*.jsonl")):
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                it = json.loads(line)
            except json.JSONDecodeError:
                continue
            if it.get("kind") != "news":
                continue
            if it.get("source") not in RESCORE_SOURCES:
                continue
            items[it["id"]] = it
    return sorted(items.values(), key=lambda x: (x.get("date") or "", x.get("id") or ""))


def load_scored() -> dict:
    if SCORED_PATH.exists():
        try:
            return json.loads(SCORED_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_scored(d: dict) -> None:
    SCORED_PATH.parent.mkdir(exist_ok=True)
    SCORED_PATH.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


def text_of(it: dict) -> str:
    t = f"{it.get('title', '')} {it.get('content', '')}".replace("\n", " ").strip()
    return t[:220]


def llm_score_batch(texts: list, ids: list) -> dict:
    """调用 LLM 批量打分，返回 {id: score}。失败抛异常。"""
    n = len(texts)
    news = "\n".join(f"{i}. {t}" for i, t in enumerate(texts))
    prompt = (
        "你是金融新闻情绪分析师。对下面每条新闻分别打分，情绪分范围 [-1,1]："
        "正值=利好，负值=利空，0=中性。\n"
        f"只返回一个 JSON 数组，长度必须等于 {n}，按输入顺序依次给出情绪分，"
        "不要输出任何其他文字。\n\n" + news
    )
    r = requests.post(
        f"{API_BASE}/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={
            "model": MODEL,
            "temperature": 0,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=90,
    )
    data = r.json()
    if "choices" not in data or not data["choices"]:
        raise RuntimeError(f"LLM 返回异常: {str(data)[:200]}")
    content = data["choices"][0]["message"]["content"]
    m = re.search(r"\[.*\]", content, re.S)
    if not m:
        raise RuntimeError(f"无法解析 JSON 数组: {content[:200]}")
    arr = json.loads(m.group(0))
    out = {}
    for i, s in enumerate(arr):
        if i < len(ids):
            try:
                out[ids[i]] = max(-1.0, min(1.0, float(s)))
            except (TypeError, ValueError):
                continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=20)
    ap.add_argument("--workers", type=int, default=4, help="并发线程数")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条（0=全部）")
    args = ap.parse_args()

    if not API_KEY:
        print("未配置 OPENAI_API_KEY，退出")
        return 1

    items = load_all_items()
    if args.limit > 0:
        items = items[:args.limit]
    scored = load_scored()
    todo = [it for it in items if it["id"] not in scored]
    print(f"总新闻 {len(items)} 条，已打分 {len(scored)}，待打分 {len(todo)}，"
          f"model={MODEL}，workers={args.workers}")

    batch = args.batch
    chunks = [todo[start:start + batch] for start in range(0, len(todo), batch)]

    def process(chunk):
        texts = [text_of(it) for it in chunk]
        ids = [it["id"] for it in chunk]
        for attempt in range(3):
            try:
                return llm_score_batch(texts, ids)
            except Exception as e:  # noqa: BLE001
                print(f"  批失败(第{attempt + 1}次): {type(e).__name__} {e}")
                time.sleep(2 * (attempt + 1))
        return {}

    lock = threading.Lock()
    done_batches = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(process, c) for c in chunks]
        for fut in as_completed(futures):
            got = fut.result()
            with lock:
                scored.update(got)
                done_batches += 1
                if done_batches % 5 == 0:
                    save_scored(scored)
                    print(f"  进度 {done_batches}/{len(chunks)} 批，已存 {len(scored)} 条")

    save_scored(scored)
    print(f"完成：共打分 {len(scored)} 条 -> {SCORED_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
