"""个人投资者视角的低频策略研究：周频/月频反转 + 20日动量。

背景：日频反转因子有显著 alpha（p=0.025），但散户级执行成本（佣金 0.1% +
滑点 0.05 元/股）下净收益转负。个人投资者没有机构级成本，但有另一个优势：
对换手率没有要求。本脚本验证：把信号频率降到周/月级后，成本敏感性与收益如何。

口径：与 reversal_study.py 一致（每账户 100 万、全仓、T+1 开盘成交、
含 A股印花税），但成本一律用「散户级」：佣金 0.1%（双边万 5）+ 滑点 0.05 元/股。
分割：固定日历 2025-08-01（样本内/外）。

用法：.venv/Scripts/python.exe scripts/lowfreq_study.py
输出：results/lowfreq_report.md
"""

import pathlib
import sys
from datetime import datetime

import numpy as np
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.event_strategy import (load_pool, load_stock_bars, align_dates, run_daily,
                                    STAMP_CN, combo_ret)

RESULTS_DIR = ROOT / "results"
SPLIT_DATE = "2025-08-01"
RETAIL_RATE = 0.001      # 散户佣金（双边万 5）
RETAIL_SLIP = 0.05       # 散户滑点（元/股）


def slice_bars(bars: pd.DataFrame, before: bool) -> pd.DataFrame:
    if before:
        return bars[bars["date"] < SPLIT_DATE].reset_index(drop=True)
    return bars[bars["date"] >= SPLIT_DATE].reset_index(drop=True)


def period_last_idx(dates: pd.Series) -> tuple:
    """每周/每月最后一个交易日的索引（按自然周/月分组，反向扫描取组内最后）。"""
    d = pd.to_datetime(dates)
    week = d.dt.to_period("W").astype(str)
    month = d.dt.to_period("M").astype(str)
    last_w, last_m = set(), set()
    out_w, out_m = [], []
    for i in range(len(d) - 1, -1, -1):
        if week.iloc[i] not in last_w:
            last_w.add(week.iloc[i])
            out_w.append(i)
        if month.iloc[i] not in last_m:
            last_m.add(month.iloc[i])
            out_m.append(i)
    return np.array(sorted(out_w)), np.array(sorted(out_m))


def weekly_reversal_signals(bars: pd.DataFrame, th: float, lookback: int = 5) -> set:
    """每周最后一个交易日：周收益 <= th -> 该日信号（次日开盘买入）。"""
    dates = bars["date"].tolist()
    closes = bars["close"].to_numpy(float)
    w_last, _ = period_last_idx(bars["date"])
    sig = set()
    for i in w_last:
        j = i - lookback
        if j >= 0 and closes[j] > 0:
            r = closes[i] / closes[j] - 1
            if r <= th:
                sig.add(dates[i])
    return sig


def monthly_reversal_signals(bars: pd.DataFrame, th: float, lookback: int = 21) -> set:
    """每月最后一个交易日：月收益 <= th -> 该日信号。"""
    dates = bars["date"].tolist()
    closes = bars["close"].to_numpy(float)
    _, m_last = period_last_idx(bars["date"])
    sig = set()
    for i in m_last:
        j = i - lookback
        if j >= 0 and closes[j] > 0:
            r = closes[i] / closes[j] - 1
            if r <= th:
                sig.add(dates[i])
    return sig


def run_signals(pool: list, sig_fn, th: float, hold: int, before: bool) -> dict:
    stats = []
    for item in pool:
        bars = load_stock_bars(item["symbol"])
        if bars.empty:
            continue
        bars = slice_bars(bars, before)
        if len(bars) < 40:
            continue
        dates, opens, closes = align_dates(bars)
        sig = sig_fn(bars, th)
        st = run_daily(dates, opens, closes, sig, set(), hold,
                       int(item.get("lot_size", 1)),
                       STAMP_CN if item["market"] == "cn" else 0.0,
                       rate=RETAIL_RATE, slippage=RETAIL_SLIP)
        stats.append(st)
    vals = [s for s in stats if s is not None]
    return {
        "ret": combo_ret(vals), "n_accts": len(vals),
        "trades": sum(s["trades"] for s in vals),
        "win": float(np.mean([s["win_rate"] for s in vals if s["win_rate"] is not None]) or 0),
    }


def momentum_run(pool: list, lookback: int, th_buy: float, th_sell: float,
                 before: bool) -> dict:
    """20日动量状态策略：T-1 动量 >= th_buy 持仓（空仓则 T 开盘买），< th_sell 空仓（持仓则 T 收盘卖）。"""
    stats = []
    for item in pool:
        bars = load_stock_bars(item["symbol"])
        if bars.empty:
            continue
        bars = slice_bars(bars, before)
        if len(bars) < lookback + 10:
            continue
        dates = bars["date"].tolist()
        opens = bars["open"].to_numpy(float)
        closes = bars["close"].to_numpy(float)
        stamp = STAMP_CN if item["market"] == "cn" else 0.0
        lot = int(item.get("lot_size", 1))
        cash = 1_000_000.0
        qty = 0.0
        buy_amt = 0.0
        trades = wins = 0
        for i in range(lookback, len(dates)):
            mom = closes[i - 1] / closes[i - 1 - lookback] - 1 if closes[i - 1 - lookback] > 0 else 0.0
            if qty > 0 and mom < th_sell:
                gross = closes[i] * qty
                cash += gross - gross * RETAIL_RATE - qty * RETAIL_SLIP - gross * stamp
                if gross - buy_amt > 0:
                    wins += 1
                trades += 1
                qty = 0.0
            elif qty == 0 and mom >= th_buy and opens[i] > 0:
                size = int(cash * 0.95 / (opens[i] * (1 + RETAIL_RATE) + RETAIL_SLIP))
                if lot > 1:
                    size = size // lot * lot
                if size >= lot:
                    cost = opens[i] * size * (1 + RETAIL_RATE) + size * RETAIL_SLIP
                    cash -= cost
                    qty = float(size)
                    buy_amt = cost
        if qty > 0:
            gross = closes[-1] * qty
            cash += gross - gross * RETAIL_RATE - qty * RETAIL_SLIP - gross * stamp
            if gross - buy_amt > 0:
                wins += 1
            trades += 1
        stats.append({"ret": (cash / 1_000_000.0 - 1) * 100, "trades": trades,
                      "win_rate": (wins / trades * 100) if trades else None})
    vals = [s for s in stats if s is not None]
    return {"ret": combo_ret(vals), "n_accts": len(vals),
            "trades": sum(s["trades"] for s in vals),
            "win": float(np.mean([s["win_rate"] for s in vals if s["win_rate"] is not None]) or 0)}


def main() -> int:
    pool = load_pool()
    rows = []

    # 周频反转（周跌幅 <= -3%/-5% -> 持有 1/2/4 周 = 5/10/20 交易日）
    for th in (-0.03, -0.05):
        for hold, label in ((5, "1周"), (10, "2周"), (20, "4周")):
            is_ = run_signals(pool, weekly_reversal_signals, th, hold, before=True)
            oos = run_signals(pool, weekly_reversal_signals, th, hold, before=False)
            rows.append({"name": f"周频反转 -{abs(th):.0%} 持有{label}",
                         "is_ret": is_["ret"], "oos_ret": oos["ret"],
                         "trades_oos": oos["trades"], "win_oos": oos["win"]})
            print(f"[weekly] th={th:.0%} hold={label}: IS {is_['ret']:+.2f}% / OOS {oos['ret']:+.2f}% ({oos['trades']} 笔)")

    # 月频反转（月跌幅 <= -5%/-8% -> 持有 1/2/3 个月 = 21/42/63 交易日）
    for th in (-0.05, -0.08):
        for hold, label in ((21, "1月"), (42, "2月"), (63, "3月")):
            is_ = run_signals(pool, monthly_reversal_signals, th, hold, before=True)
            oos = run_signals(pool, monthly_reversal_signals, th, hold, before=False)
            rows.append({"name": f"月频反转 -{abs(th):.0%} 持有{label}",
                         "is_ret": is_["ret"], "oos_ret": oos["ret"],
                         "trades_oos": oos["trades"], "win_oos": oos["win"]})
            print(f"[monthly] th={th:.0%} hold={label}: IS {is_['ret']:+.2f}% / OOS {oos['ret']:+.2f}% ({oos['trades']} 笔)")

    # 20日动量（趋势跟随）
    for th_buy, th_sell in ((0.05, 0.0), (0.10, 0.0), (0.05, -0.05)):
        is_ = momentum_run(pool, 20, th_buy, th_sell, before=True)
        oos = momentum_run(pool, 20, th_buy, th_sell, before=False)
        rows.append({"name": f"20日动量 {th_buy:.0%}/{th_sell:.0%}",
                     "is_ret": is_["ret"], "oos_ret": oos["ret"],
                     "trades_oos": oos["trades"], "win_oos": oos["win"]})
        print(f"[momentum] {th_buy:.0%}/{th_sell:.0%}: IS {is_['ret']:+.2f}% / OOS {oos['ret']:+.2f}% ({oos['trades']} 笔)")

    pd.DataFrame(rows).to_csv(RESULTS_DIR / "lowfreq_backtest.csv", index=False, encoding="utf-8-sig")

    lines = []
    lines.append("# 个人投资者低频策略研究（散户成本口径）")
    lines.append("")
    lines.append(f"> 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
    lines.append("> 成本：散户级佣金 0.1%（双边万 5）+ 滑点 0.05 元/股 + A股印花税（卖出）。")
    lines.append(f"> 分割：固定日历 {SPLIT_DATE}（样本内/外），每账户 100 万、全仓、42 只池。")
    lines.append("> 对照：日频反转同成本下样本外约 -1.6%（见 reversal_report.md 成本表）。")
    lines.append("")
    lines.append("| 策略 | 样本内收益 | 样本外收益 | 样本外成交 | 样本外胜率 |")
    lines.append("|---|---|---|---|---|")
    for r in rows:
        lines.append(f"| {r['name']} | {r['is_ret']:+.2f}% | {r['oos_ret']:+.2f}% | "
                     f"{r['trades_oos']} | {r['win_oos']:.0f}% |")
    lines.append("")
    lines.append("## 结论")
    lines.append("")
    lines.append("- 判定标准：样本外为正且成交数明显低于日频（约 800+ 笔/样本外）才算「个人可用」。")
    lines.append("- 若低频变体在散户成本下样本外为正，说明降低换手是对冲个人成本劣势的有效手段；")
    lines.append("  若仍为负，说明该信号本身在样本外已衰减，与成本无关。")
    lines.append("")
    (RESULTS_DIR / "lowfreq_report.md").write_text("\n".join(lines), encoding="utf-8")
    print("[report] -> results/lowfreq_report.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
