"""财报事件研究：财报后 T+1~T+5 漂移（针对美股 8-K Item 2.02 财报事件）。

背景：回测显示美股（尤其苹果）的真实情绪有正信号，且主要来自 8-K 财报事件。
本脚本把「财报发布」单独拎出来，量化财报后的漂移（drift），判断信号来源。

方法：
- 财报日 T = SEC 8-K「Item 2.02 Results of Operations」的发布日期（财报通常盘后发布）。
- 信号收益：T+1 开盘买入 -> T+5 收盘卖出（不含财报次日开盘的跳空）。
- 即时反应：T 收盘 -> T+1 收盘（含跳空 gap）。
- 背景基准：全期所有「5 日持有窗口」的平均收益，用于对比财报窗口是否异常。

用法（项目根目录）：
    .venv/Scripts/python.exe scripts/earnings_study.py
输出：
    results/earnings_study_report.md
"""

import glob
import json
import pathlib

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
NEWS_DIR = ROOT / "news"
DATA_DIR = ROOT / "data"
RESULTS_DIR = ROOT / "results"


def load_bars(ticker: str) -> pd.DataFrame:
    df = pd.read_csv(DATA_DIR / f"bars_{ticker}.csv", encoding="utf-8-sig")
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    return df.sort_values("date").reset_index(drop=True)


def earnings_events(ticker: str) -> list:
    """从归档筛出该 ticker 的财报事件日期。

    财报 8-K = 标题含 Item 2.02（AAPL），或正文新闻稿含财报特征词（MSFT，
    其 8-K 正文的 Item 标题未被提取，但展品 99.1 是财报新闻稿）。
    """
    dates = []
    for f in glob.glob(str(NEWS_DIR / "raw" / "*.jsonl")):
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                it = json.loads(line)
            except json.JSONDecodeError:
                continue
            if it.get("source") != "SEC 8-K":
                continue
            if ticker not in (it.get("symbols") or []):
                continue
            title = it.get("title") or ""
            content = (it.get("content") or "").lower()
            if "2.02" in title or ("results of operations" in content) or (
                    "quarter" in content and "financial results" in content):
                dates.append(it["date"])
    return sorted(set(dates))


def background_5d(bars: pd.DataFrame) -> float:
    """全期所有 5 日持有窗口（open[i] -> close[i+4]）的平均收益（%）。"""
    rets = []
    for i in range(len(bars) - 4):
        rets.append((float(bars.loc[i + 4, "close"]) / float(bars.loc[i, "open"]) - 1.0) * 100)
    return sum(rets) / len(rets) if rets else 0.0


def study(ticker: str) -> dict:
    bars = load_bars(ticker)
    dates = bars["date"].tolist()
    events = earnings_events(ticker)
    rows = []
    for ed in events:
        after = [i for i, d in enumerate(dates) if d > ed]
        if len(after) < 5:
            continue
        idxs = after[:5]  # T+1 .. T+5
        t1_open = float(bars.loc[idxs[0], "open"])
        t1_close = float(bars.loc[idxs[0], "close"])
        t5_close = float(bars.loc[idxs[4], "close"])
        day1 = (t1_close / t1_open - 1.0) * 100
        drift = (t5_close / t1_open - 1.0) * 100
        # 财报日 T 收盘 -> T+1 收盘（含跳空）
        gap = None
        for i, d in enumerate(dates):
            if d == ed and i + 1 < len(dates):
                gap = (float(bars.loc[i + 1, "close"]) / float(bars.loc[i, "close"]) - 1.0) * 100
                break
        rows.append({
            "date": ed,
            "gap_t0_t1": gap,
            "day1_ret": day1,
            "t1_to_t5": drift,
        })
    return {"ticker": ticker, "rows": rows, "bg_5d": background_5d(bars)}


def main() -> int:
    RESULTS_DIR.mkdir(exist_ok=True)
    lines = ["# 财报事件研究：财报后 T+1~T+5 漂移", ""]
    lines.append("> 财报日 T = SEC 8-K「Item 2.02 Results of Operations」发布日（盘后）。")
    lines.append("> 信号收益 = T+1 开盘买入 -> T+5 收盘卖出；即时反应 = T 收盘 -> T+1 收盘（含跳空）。")
    lines.append("> **本报告仅用于学习研究，不构成投资建议。**")
    lines.append("")

    for ticker in ("AAPL", "MSFT"):
        res = study(ticker)
        rows = res["rows"]
        lines.append(f"## {ticker}（财报事件 {len(rows)} 个）")
        lines.append("")
        if not rows:
            lines.append("（无财报事件数据）")
            lines.append("")
            continue
        drifts = [r["t1_to_t5"] for r in rows]
        day1s = [r["day1_ret"] for r in rows]
        gaps = [r["gap_t0_t1"] for r in rows if r["gap_t0_t1"] is not None]
        pos = sum(1 for x in drifts if x > 0)
        lines.append(f"- 背景基准（全期 5 日持有均值）：**{res['bg_5d']:+.2f}%**")
        lines.append(f"- 财报后 T+1→T+5 漂移：均值 **{sum(drifts)/len(drifts):+.2f}%**，"
                     f"中位数 {sorted(drifts)[len(drifts)//2]:+.2f}%，胜率 **{pos}/{len(drifts)}**")
        lines.append(f"- 财报次日（T+1）单日：均值 {sum(day1s)/len(day1s):+.2f}%")
        lines.append(f"- 财报跳空（T→T+1 收盘）：均值 {sum(gaps)/len(gaps):+.2f}%")
        lines.append("")
        lines.append("| 财报日 | 跳空(T→T+1) | T+1 单日 | T+1→T+5 漂移 |")
        lines.append("|---|---|---|---|")
        for r in rows:
            lines.append(
                f"| {r['date']} | {(f'{r['gap_t0_t1']:+.2f}%' if r['gap_t0_t1'] is not None else '-')} "
                f"| {r['day1_ret']:+.2f}% | {r['t1_to_t5']:+.2f}% |"
            )
        lines.append("")

    report = RESULTS_DIR / "earnings_study_report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"[earnings] 报告已写入 {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
