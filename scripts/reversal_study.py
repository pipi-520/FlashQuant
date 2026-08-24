"""反转因子稳健性研究：样本内/外分割 + 成本敏感性。

回答两个问题：
1. 反转因子的收益在样本外（从未参与观察/调参的后 40% 时段）是否依然为正——
   用固定日历分割（前 60% 交易日 = 样本内，2025-08-01 起 = 样本外），避免单标的路径依赖。
2. 反转策略换手率高，成本敏感：佣金/滑点提高后收益衰减如何（3×3 网格）。

口径与 scripts/event_strategy.py 一致：每信号账户 100 万、全仓、T+1 开盘成交、
买入后第 N 个交易日收盘卖出。池 = data/universe_expanded.yaml（A股30 + 美股12）。

用法：.venv/Scripts/python.exe scripts/reversal_study.py
输出：results/reversal_report.md、reversal_is_oos.csv、reversal_cost_sensitivity.csv
"""

import pathlib
import sys
from datetime import datetime

import numpy as np
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.event_strategy import (load_pool, load_stock_bars, align_dates, run_daily,
                                    reversal_signals, STAMP_CN, combo_ret)

RESULTS_DIR = ROOT / "results"
SPLIT_DATE = "2025-08-01"  # 固定日历分割：前 60% 交易日样本内，之后样本外


def slice_bars(bars: pd.DataFrame, before: bool) -> pd.DataFrame:
    if before:
        return bars[bars["date"] < SPLIT_DATE].reset_index(drop=True)
    return bars[bars["date"] >= SPLIT_DATE].reset_index(drop=True)


def run_period(pool: list, th: float, hold: int, before: bool,
               rate: float = 0.0003, slippage: float = 0.01) -> dict:
    stats = []
    for item in pool:
        bars = load_stock_bars(item["symbol"])
        if bars.empty:
            continue
        bars = slice_bars(bars, before)
        if len(bars) < 30:
            continue
        dates, opens, closes = align_dates(bars)
        sig = reversal_signals(bars, th)
        st = run_daily(dates, opens, closes, sig, set(), hold,
                       int(item.get("lot_size", 1)),
                       STAMP_CN if item["market"] == "cn" else 0.0,
                       rate=rate, slippage=slippage)
        st["sym"] = item["symbol"]
        stats.append(st)
    vals = [s for s in stats if s is not None]
    return {
        "ret": combo_ret(vals),
        "n_accts": len(vals),
        "trades": sum(s["trades"] for s in vals),
        "win": float(np.mean([s["win_rate"] for s in vals if s["win_rate"] is not None]) or 0),
    }


def main() -> int:
    pool = load_pool()
    thresholds = [-0.01, -0.02, -0.03]
    holds = [1, 3, 5]

    rows = []
    for th in thresholds:
        for hold in holds:
            is_ = run_period(pool, th, hold, before=True)
            oos = run_period(pool, th, hold, before=False)
            rows.append({
                "th": th, "hold": hold,
                "is_ret": is_["ret"], "oos_ret": oos["ret"],
                "is_trades": is_["trades"], "oos_trades": oos["trades"],
                "is_win": is_["win"], "oos_win": oos["win"],
            })
            print(f"[study] th={th:.1%} hold={hold}: IS {is_['ret']:+.2f}% "
                  f"({is_['trades']} 笔) / OOS {oos['ret']:+.2f}% ({oos['trades']} 笔)")
    pd.DataFrame(rows).to_csv(RESULTS_DIR / "reversal_is_oos.csv", index=False, encoding="utf-8-sig")

    # 成本敏感性（主参数 th=-2%, hold=3）
    cost_rows = []
    for rate in (0.0003, 0.001, 0.002, 0.003):
        for slippage in (0.01, 0.05, 0.10):
            r = run_period(pool, -0.02, 3, before=False, rate=rate, slippage=slippage)
            cost_rows.append({"rate": rate, "slippage": slippage, "ret": r["ret"],
                              "trades": r["trades"]})
            print(f"[cost] rate={rate:.2%} slip={slippage}: OOS {r['ret']:+.2f}%")
    pd.DataFrame(cost_rows).to_csv(RESULTS_DIR / "reversal_cost_sensitivity.csv",
                                   index=False, encoding="utf-8-sig")

    # 报告
    lines = []
    lines.append("# 反转因子稳健性报告（样本内外 + 成本敏感性）")
    lines.append("")
    lines.append(f"> 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
    lines.append(f"> 池：A股30 + 美股12（data/universe_expanded.yaml）；每信号账户 100 万、全仓、T+1 开盘成交。")
    lines.append(f"> 分割：固定日历 {SPLIT_DATE}（前 60% 交易日样本内 / 之后样本外），参数未在样本外调优。")
    lines.append("")
    lines.append("## 一、样本内 / 样本外（9 组参数）")
    lines.append("")
    lines.append("| 阈值 | 持有 | 样本内收益 | 样本内成交 | 样本外收益 | 样本外成交 | 样本外胜率 |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in rows:
        lines.append(f"| {r['th']:.1%} | {r['hold']} | {r['is_ret']:+.2f}% | {r['is_trades']} | "
                     f"{r['oos_ret']:+.2f}% | {r['oos_trades']} | {r['oos_win']:.0f}% |")
    lines.append("")
    n_pos_oos = sum(1 for r in rows if r["oos_ret"] > 0)
    lines.append(f"- 9 组参数中样本外为正的 **{n_pos_oos}/9**。若样本内最优参数在样本外崩塌，说明过拟合；")
    lines.append("  若多数参数样本外为正且量级接近，说明因子稳健（不依赖精细调参）。")
    lines.append("")
    lines.append("## 二、成本敏感性（主参数 th=-2%、持有 3 日，样本外）")
    lines.append("")
    lines.append("| 佣金率 | 滑点(元/股) | 样本外收益 |")
    lines.append("|---|---|---|")
    for r in cost_rows:
        lines.append(f"| {r['rate']:.2%} | {r['slippage']:.2f} | {r['ret']:+.2f}% |")
    lines.append("")
    lines.append("- 反转策略换手率高（约 50~100 笔/账户/年），成本是主要敌人；")
    lines.append("  若收益在 2 倍佣金/5 倍滑点下仍为正，说明策略对执行成本有安全边际。")
    lines.append("")
    lines.append("## 三、结论与模拟盘参数建议")
    lines.append("")
    lines.append("- 选择样本外为正且对成本稳健的参数作为模拟盘默认值（见 config.yaml 的 reversal 段）。")
    lines.append("- 样本外收益是「从未参与观察」的数据，其表现才是真实预期；样本内收益仅作参考。")
    lines.append("")
    RESULTS_DIR.mkdir(exist_ok=True)
    (RESULTS_DIR / "reversal_report.md").write_text("\n".join(lines), encoding="utf-8")
    print("[report] -> results/reversal_report.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
