"""告警闭环回看：检验发出的告警「次日是否真的有大动静」。

monitor.py 实际推送时把告警写入 news/alerts.jsonl；本脚本回看这些告警：
- 告警日 -> 参考交易日 i（<= 告警日的最后交易日）-> 次日市场 |收益|（上证指数）；
- 若告警带个股标签，另统计该股次日 |收益|；
- 与「全样本基线」（P(|ret|>1.5%) / P(|ret|>3%)，见 impact_calibration.md）对比。

输出：results/alert_review.md；--push 时把摘要推送到企业微信。
建议每日收盘后跑一次（deploy/chaogu-alert-review.timer，北京时间 17:30）。

用法：
    .venv/Scripts/python.exe scripts/alert_review.py            # 回看全部已结算告警
    .venv/Scripts/python.exe scripts/alert_review.py --days 7   # 只看最近 7 天
    .venv/Scripts/python.exe scripts/alert_review.py --push     # 并推送到企业微信
"""

import argparse
import bisect
import json
import pathlib
import sys
from collections import defaultdict
from datetime import datetime, timedelta

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

NEWS_DIR = ROOT / "news"
DATA_DIR = ROOT / "data"
RESULTS_DIR = ROOT / "results"
ALERTS_PATH = NEWS_DIR / "alerts.jsonl"

BASELINE_TAIL = 0.361   # 全样本基线：P(次日|ret|>1.5%)，来自 impact_calibration.md
BASELINE_BIG = 0.136    # 全样本基线：P(次日|ret|>3%)


def load_alerts(days: int | None = None) -> list:
    if not ALERTS_PATH.exists():
        return []
    cutoff = None
    if days:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    alerts = []
    with open(ALERTS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                a = json.loads(line)
            except json.JSONDecodeError:
                continue
            if cutoff and (a.get("date") or "") < cutoff:
                continue
            alerts.append(a)
    return alerts


def load_index_ret() -> tuple:
    import akshare as ak
    df = ak.stock_zh_index_daily(symbol="sh000001")
    dates = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").tolist()
    closes = pd.to_numeric(df["close"], errors="coerce").tolist()
    ret = {}
    for i in range(len(dates) - 1):
        if closes[i] > 0:
            ret[dates[i]] = abs(closes[i + 1] / closes[i] - 1)
    return ret, dates


def stock_next_ret(sym: str) -> dict:
    p = DATA_DIR / f"bars_{sym}.csv"
    if not p.exists():
        return {}
    bars = pd.read_csv(p, encoding="utf-8-sig")
    dates = pd.to_datetime(bars["date"]).dt.strftime("%Y-%m-%d").tolist()
    closes = bars["close"].to_numpy(float)
    out = {}
    for i in range(len(dates) - 1):
        if closes[i] > 0:
            out[dates[i]] = abs(closes[i + 1] / closes[i] - 1)
    return out


def settle(alerts: list, idx_ret: dict, idx_dates: list, stock_rets: dict) -> list:
    """返回可结算（次日已收盘）的告警列表，附 next_abs 字段。"""
    out = []
    today = datetime.now().strftime("%Y-%m-%d")
    for a in alerts:
        d = a.get("date") or ""
        i = bisect.bisect_right(idx_dates, d) - 1
        if i < 0 or i >= len(idx_dates) - 1:
            continue
        ref = idx_dates[i]
        if ref >= today:
            continue  # 次日尚未收盘
        mkt = idx_ret.get(ref)
        stk = None
        for s in (a.get("symbols") or []):
            r = stock_rets.get(s)
            if r and ref in r:
                stk = r[ref]
                break
        a = dict(a)
        a["ref_date"] = ref
        a["next_abs"] = stk if stk is not None else mkt
        a["y_kind"] = "stock" if stk is not None else "market"
        out.append(a)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None, help="只看最近 N 天的告警")
    ap.add_argument("--push", action="store_true", help="推送到企业微信")
    args = ap.parse_args()

    alerts = load_alerts(args.days)
    if not alerts:
        print("[review] 暂无告警记录（news/alerts.jsonl）")
        return 0
    idx_ret, idx_dates = load_index_ret()
    import yaml
    pool = yaml.safe_load((DATA_DIR / "universe_expanded.yaml").read_text(encoding="utf-8"))["symbols"]
    stock_rets = {it["symbol"]: stock_next_ret(it["symbol"]) for it in pool}
    settled = settle(alerts, idx_ret, idx_dates, stock_rets)
    n_pending = len(alerts) - len(settled)
    print(f"[review] 告警 {len(alerts)} 条：已结算 {len(settled)} / 待次日数据 {n_pending}")

    if not settled:
        print("[review] 暂无已结算告警")
        return 0

    vals = [a["next_abs"] for a in settled if a["next_abs"] is not None]
    n_tail = sum(1 for v in vals if v > 0.015)
    n_big = sum(1 for v in vals if v > 0.03)

    by_type = defaultdict(list)
    for a in settled:
        by_type[a.get("event_type") or "其他"].append(a["next_abs"])
    type_rows = []
    for typ, vs in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
        vs = [v for v in vs if v is not None]
        if not vs:
            continue
        type_rows.append((typ, len(vs), sum(vs) / len(vs),
                          sum(1 for v in vs if v > 0.03) / len(vs)))

    lines = []
    lines.append("# 告警闭环回看报告")
    lines.append("")
    lines.append(f"> 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
    lines.append(f"> 已结算告警 {len(settled)} 条（{n_pending} 条等待次日数据）")
    lines.append("")
    lines.append("## 总体")
    lines.append("")
    if vals:
        lines.append(f"- 次日 |收益| 均值 **{sum(vals)/len(vals)*100:.2f}%**")
        lines.append(f"- P(>1.5%) = **{n_tail/len(vals)*100:.1f}%**（全样本基线 {BASELINE_TAIL*100:.1f}%）")
        lines.append(f"- P(>3%) = **{n_big/len(vals)*100:.1f}%**（全样本基线 {BASELINE_BIG*100:.1f}%）")
        better = (n_big / len(vals)) > BASELINE_BIG
        lines.append(f"- 判定：告警的「大动静」占比{'高于' if better else '低于'}基线——"
                     f"告警过滤{'有效' if better else '需要收紧'}。")
    lines.append("")
    lines.append("## 按事件类型")
    lines.append("")
    lines.append("| 事件类型 | 告警数 | 次日|ret|均值 | P(>3%) |")
    lines.append("|---|---|---|---|")
    for typ, n, mean_v, big_v in type_rows:
        lines.append(f"| {typ} | {n} | {mean_v*100:.2f}% | {big_v*100:.1f}% |")
    lines.append("")
    lines.append("- 对比 impact_calibration.md 的同类事件历史基线，持续积累后用于迭代告警阈值。")
    lines.append("")

    RESULTS_DIR.mkdir(exist_ok=True)
    (RESULTS_DIR / "alert_review.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"[review] 报告 -> results/alert_review.md")

    if args.push:
        from news_aggregator.push import push_alert
        import yaml as _yaml
        cfg = _yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        push_alert(cfg, "告警闭环回看", "\n".join(lines[:18]))
        print("[review] 已推送到企业微信")
    return 0


if __name__ == "__main__":
    sys.exit(main())
