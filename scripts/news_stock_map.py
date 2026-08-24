"""新闻 -> 个股影响映射（CLI 演示）。

输入一条新闻，输出：
1. 命中的主题（themes.yaml 关键词匹配）。
2. LLM 判断的个股影响映射（受益/受损股票 + 方向 + 强度 + 理由）。

用法（项目根目录）：
    .venv/Scripts/python.exe scripts/news_stock_map.py "美国FDA批准mRNA抗癌疫苗三期临床成功"
    .venv/Scripts/python.exe scripts/news_stock_map.py --demo          # 用内置示例
    .venv/Scripts/python.exe scripts/news_stock_map.py --file 新闻.txt
"""

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from news_aggregator.impact import match_themes  # noqa: E402
from news_aggregator.stock_impact import load_universe, map_news_to_stocks  # noqa: E402

DEMO = "美国FDA批准一款mRNA抗癌疫苗三期临床成功，有望年内上市，市场预期将带动创新药板块行情"


def load_themes() -> list:
    return (yaml.safe_load((ROOT / "news_aggregator/themes.yaml").read_text(encoding="utf-8"))
            .get("themes") or [])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("text", nargs="?", help="新闻文本")
    ap.add_argument("--demo", action="store_true", help="使用内置示例")
    ap.add_argument("--file", help="从文件读取新闻文本")
    args = ap.parse_args()

    if args.file:
        news = pathlib.Path(args.file).read_text(encoding="utf-8").strip()
    elif args.demo or not args.text:
        news = DEMO
    else:
        news = args.text

    print("=" * 60)
    print("新闻：", news)
    print("=" * 60)

    # 1) 主题命中
    themes = load_themes()
    matched = match_themes(news, themes)
    print("\n[主题命中]")
    if matched:
        for th, hits in matched:
            print(f"  {th['name']} <- {hits}")
    else:
        print("  （未命中任何主题）")

    # 2) 个股影响映射
    print("\n[个股影响映射（LLM）]")
    try:
        universe = load_universe()
        results = map_news_to_stocks(news, universe)
        if not results:
            print("  （无结果）")
        else:
            print(f"{'方向':<4} {'强度':<4} {'代码':<8} {'名称':<8} 理由")
            print("-" * 60)
            for r in results:
                arrow = {"利好": "↑", "利空": "↓", "中性": "→"}.get(r["direction"], "?")
                print(f"{r['direction']}{arrow:<2} {r['magnitude']:<4} "
                      f"{r['code']:<8} {r['name']:<8} {r['reason']}")
        print("\n[JSON]")
        print(json.dumps(results, ensure_ascii=False, indent=2))
    except Exception as e:  # noqa: BLE001
        print(f"  个股映射失败: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
