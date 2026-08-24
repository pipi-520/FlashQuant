"""本地模拟盘（Paper Trading）引擎 —— 强化版 v2。

- 用真实情绪分（data/sentiment_{symbol}.csv，来自多源聚合器）驱动新闻情绪策略。
- 撮合规则：T-1 日情绪生成信号，T 日开盘成交（避免前视，与 vnpy 回测方向一致）。
- 风控：初始止损 / 止盈 / 移动止损（开盘价判断）。
- 仓位：risk_percent > 0 时按止损距离动态计算股数，向下取整到 lot_size。
- 持久化虚拟账户：paper/paper_state.json + paper/trades.csv；重复运行只处理新增交易日。
- 只做多头（allow_short 保持关闭），与 A 股普通账户一致。

用法（在项目根目录执行）：
    .venv/Scripts/python.exe scripts/paper_trade.py            # 用真实情绪分
    .venv/Scripts/python.exe scripts/paper_trade.py --synthetic  # 用合成情绪分演示
    .venv/Scripts/python.exe scripts/paper_trade.py --reset    # 重置虚拟账户
"""

import argparse
import json
import pathlib
import sys
from datetime import datetime

import pandas as pd
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from strategies.news_sentiment_strategy import calc_size, load_sentiment  # noqa: E402

DATA_DIR = ROOT / "data"
PAPER_DIR = ROOT / "paper"
STATE_PATH = PAPER_DIR / "paper_state.json"
TRADES_PATH = PAPER_DIR / "trades.csv"


def load_config() -> dict:
    with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state(capital: float) -> dict:
    if STATE_PATH.exists():
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"cash": capital, "positions": {}, "last_processed": {}, "trades": []}


def save_state(state: dict) -> None:
    PAPER_DIR.mkdir(exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_series(symbol: str, synthetic: bool) -> tuple:
    """返回 (bars DataFrame[date,open,close], sentiment dict[date->score])。"""
    bars = pd.read_csv(DATA_DIR / f"bars_{symbol}.csv", encoding="utf-8-sig")
    bars["date"] = pd.to_datetime(bars["date"]).dt.strftime("%Y-%m-%d")
    bars = bars.sort_values("date").reset_index(drop=True)
    fname = f"sentiment_synthetic_{symbol}.csv" if synthetic else f"sentiment_{symbol}.csv"
    sent = load_sentiment(str(DATA_DIR / fname))
    return bars, sent


def process_symbol(sym: str, item: dict, sent_cfg: dict, bt: dict, state: dict, synthetic: bool):
    th = float(sent_cfg["threshold_long"])
    tf = float(sent_cfg["threshold_flat"])
    stop_loss_pct = float(sent_cfg.get("stop_loss_pct", 0.0))
    take_profit_pct = float(sent_cfg.get("take_profit_pct", 0.0))
    trailing_stop_pct = float(sent_cfg.get("trailing_stop_pct", 0.0))
    risk_percent = float(sent_cfg.get("risk_percent", 0.0))
    max_position_pct = float(sent_cfg.get("max_position_pct", 0.95))
    lot_size = int(item.get("lot_size", 100))
    fixed_size = int(item.get("fixed_size", 100))
    capital = float(bt["capital"])
    # 交易成本：与回测口径一致（佣金按成交额双边 + 每股滑点；A股另加卖出印花税 0.05%）
    rate = float(bt.get("rate", 0.0003))
    slippage = float(bt.get("slippage", 0.0))
    stamp_tax = 0.0005 if item.get("market") == "cn" else 0.0

    def buy_cost(price: float, qty: float) -> float:
        gross = price * qty
        return gross + gross * rate + qty * slippage

    def sell_proceeds(price: float, qty: float) -> float:
        gross = price * qty
        return gross - gross * rate - qty * slippage - gross * stamp_tax

    bars, sent = load_series(sym, synthetic)
    if bars.empty:
        return
    last = state["last_processed"].get(sym)
    pos = state["positions"].get(sym, {})
    qty = float(pos.get("qty", 0.0))
    avg_cost = float(pos.get("avg_cost", 0.0))
    highest_price = float(pos.get("highest_price", avg_cost))

    # 起点：上次已处理日之后；首次从第一条开始（信号需要前一日，因此 i 从 1 开始）
    start_idx = 0
    if last is not None:
        idx = bars.index[bars["date"] == last].tolist()
        if idx:
            start_idx = idx[0] + 1

    for i in range(max(start_idx, 0), len(bars)):
        if i == 0:
            continue  # 第一条仅作为首个信号日
        sig_date = bars.loc[i - 1, "date"]
        trade_date = bars.loc[i, "date"]
        open_price = float(bars.loc[i, "open"])
        score = float(sent.get(sig_date, 0.0))

        exited = False

        # 1) 已有持仓：先做风控出场（T 日开盘价判断）
        if qty > 0:
            highest_price = max(highest_price, open_price)
            stop = avg_cost * (1.0 - stop_loss_pct) if stop_loss_pct > 0 else None
            if trailing_stop_pct > 0:
                trail = highest_price * (1.0 - trailing_stop_pct)
                if stop is None or trail > stop:
                    stop = trail
            take = avg_cost * (1.0 + take_profit_pct) if take_profit_pct > 0 else None

            reason = None
            if stop is not None and open_price <= stop:
                reason = "stop_loss"
            elif take is not None and open_price >= take:
                reason = "take_profit"
            if reason:
                state["cash"] += sell_proceeds(open_price, qty)
                state["trades"].append({
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "signal_date": sig_date,
                    "date": trade_date,
                    "symbol": sym,
                    "side": "SELL",
                    "reason": reason,
                    "qty": qty,
                    "price": round(open_price, 4),
                    "cash_after": round(state["cash"], 2),
                })
                qty = 0.0
                avg_cost = 0.0
                highest_price = 0.0
                exited = True

        # 2) 开仓 / 情绪反转出场
        if not exited:
            if qty == 0 and score >= th:
                size = calc_size(open_price, fixed_size, risk_percent, stop_loss_pct,
                                 max_position_pct, lot_size, capital)
                cost = buy_cost(open_price, size)
                if size > 0 and cost <= state["cash"]:
                    state["cash"] -= cost
                    avg_cost = open_price
                    qty = float(size)
                    highest_price = open_price
                    state["trades"].append({
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "signal_date": sig_date,
                        "date": trade_date,
                        "symbol": sym,
                        "side": "BUY",
                        "reason": "signal",
                        "qty": size,
                        "price": round(open_price, 4),
                        "cash_after": round(state["cash"], 2),
                    })
            elif qty > 0 and score <= tf:
                state["cash"] += sell_proceeds(open_price, qty)
                state["trades"].append({
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "signal_date": sig_date,
                    "date": trade_date,
                    "symbol": sym,
                    "side": "SELL",
                    "reason": "signal",
                    "qty": qty,
                    "price": round(open_price, 4),
                    "cash_after": round(state["cash"], 2),
                })
                qty = 0.0
                avg_cost = 0.0
                highest_price = 0.0

        state["last_processed"][sym] = trade_date

    # 估值价取「最后处理到的那天」的收盘价，避免用到 bars 里尚未处理的新数据（未来数据）。
    last_date = state["last_processed"].get(sym)
    last_close = float(bars["close"].iloc[-1])
    if last_date:
        m = bars.index[bars["date"] == last_date].tolist()
        if m:
            last_close = float(bars.loc[m[0], "close"])

    state["positions"][sym] = {
        "qty": qty,
        "avg_cost": round(avg_cost, 4),
        "highest_price": round(highest_price, 4),
        "last_close": last_close,
    }


def write_trades(state: dict) -> None:
    if state["trades"]:
        df = pd.DataFrame(state["trades"])
        PAPER_DIR.mkdir(exist_ok=True)
        df.to_csv(TRADES_PATH, index=False, encoding="utf-8-sig")


# ================= 反转因子模式（--reversal） =================
# 信号：T-1 日收盘跌幅 <= threshold -> T 日开盘全仓买入 -> 买入后第 hold_days 个交易日收盘卖出。
# 每标的独立 100 万虚拟账户（与 results/reversal_report.md 的研究口径一致）。
# 状态与情绪模式隔离：paper/reversal_state.json + paper/reversal_trades.csv。

REV_STATE_PATH = PAPER_DIR / "reversal_state.json"
REV_TRADES_PATH = PAPER_DIR / "reversal_trades.csv"
REV_CAPITAL = 1_000_000.0


def load_rev_state() -> dict:
    if REV_STATE_PATH.exists():
        with open(REV_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"accounts": {}, "last_processed": {}, "trades": []}


def save_rev_state(state: dict) -> None:
    PAPER_DIR.mkdir(exist_ok=True)
    tmp = REV_STATE_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(REV_STATE_PATH)


def process_reversal_symbol(sym: str, item: dict, rev: dict, state: dict) -> None:
    th = float(rev.get("threshold", -0.02))
    hold = int(rev.get("hold_days", 3))
    maxp = float(rev.get("max_position_pct", 0.95))
    rate = float(rev.get("rate", 0.0003))
    slippage = float(rev.get("slippage", 0.01))
    stamp = 0.0005 if item.get("market") == "cn" else 0.0
    lot = int(item.get("lot_size", 100))

    bars = pd.read_csv(DATA_DIR / f"bars_{sym}.csv", encoding="utf-8-sig")
    bars["date"] = pd.to_datetime(bars["date"]).dt.strftime("%Y-%m-%d")
    bars = bars.sort_values("date").reset_index(drop=True)
    if bars.empty:
        return

    acct = state["accounts"].setdefault(
        sym, {"cash": REV_CAPITAL, "qty": 0.0, "avg_cost": 0.0,
              "entry_date": None, "last_close": 0.0})
    cash = float(acct["cash"])
    qty = float(acct["qty"])
    entry_date = acct.get("entry_date")
    last = state["last_processed"].get(sym)

    start_idx = 0
    if last is not None:
        idx = bars.index[bars["date"] == last].tolist()
        if idx:
            start_idx = idx[0] + 1

    for i in range(max(start_idx, 0), len(bars)):
        if i < 2:
            state["last_processed"][sym] = bars.loc[i, "date"]
            continue
        d = bars.loc[i, "date"]
        close = float(bars.loc[i, "close"])
        open_ = float(bars.loc[i, "open"])

        # 1) 到期平仓（收盘价）：买入日 entry 起第 hold 个交易日收盘卖
        if qty > 0 and entry_date is not None:
            eidx = bars.index[bars["date"] == entry_date].tolist()
            if eidx and i == eidx[0] + hold:
                gross = close * qty
                cash += gross - gross * rate - qty * slippage - gross * stamp
                state["trades"].append({
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "date": d, "symbol": sym, "side": "SELL", "reason": "expire",
                    "qty": qty, "price": round(close, 4),
                    "cash_after": round(cash, 2),
                })
                qty = 0.0
                entry_date = None

        # 2) 开仓信号：T-1 vs T-2 收盘跌幅 <= th -> T 日开盘全仓买入
        if qty == 0:
            prev_close = float(bars.loc[i - 1, "close"])
            prev2_close = float(bars.loc[i - 2, "close"])
            if prev2_close > 0 and prev_close / prev2_close - 1 <= th and open_ > 0:
                size = int(cash * maxp / (open_ * (1 + rate) + slippage))
                if lot > 1:
                    size = size // lot * lot
                if size >= lot:
                    cost = open_ * size * (1 + rate) + size * slippage
                    cash -= cost
                    qty = float(size)
                    entry_date = d
                    state["trades"].append({
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "date": d, "symbol": sym, "side": "BUY", "reason": "reversal",
                        "qty": size, "price": round(open_, 4),
                        "cash_after": round(cash, 2),
                    })
        state["last_processed"][sym] = d

    last_date = state["last_processed"].get(sym)
    last_close = float(bars["close"].iloc[-1])
    if last_date:
        m = bars.index[bars["date"] == last_date].tolist()
        if m:
            last_close = float(bars.loc[m[0], "close"])
    acct.update({"cash": cash, "qty": qty, "avg_cost": 0.0 if qty == 0 else acct["avg_cost"],
                 "entry_date": entry_date, "last_close": last_close})


def run_reversal(cfg: dict, reset: bool) -> None:
    rev = cfg.get("reversal") or {}
    if reset and REV_STATE_PATH.exists():
        REV_STATE_PATH.unlink()
        print("[reversal] 已重置虚拟账户")
    state = load_rev_state()

    # 标的池：config.reversal.universe（默认扩展池 42 只）
    universe_path = str(rev.get("universe", "data/universe_expanded.yaml"))
    if not pathlib.Path(universe_path).is_absolute():
        universe_path = str(ROOT / universe_path)
    if pathlib.Path(universe_path).exists():
        with open(universe_path, "r", encoding="utf-8") as f:
            pool = (yaml.safe_load(f) or {}).get("symbols") or []
    else:
        pool = cfg["symbols"]

    for item in pool:
        sym = item["symbol"]
        if not (DATA_DIR / f"bars_{sym}.csv").exists():
            continue
        try:
            process_reversal_symbol(sym, item, rev, state)
        except Exception as e:  # noqa: BLE001
            print(f"  !! {sym} 处理失败: {e}")
    save_rev_state(state)

    if state["trades"]:
        df = pd.DataFrame(state["trades"])
        df.to_csv(REV_TRADES_PATH, index=False, encoding="utf-8-sig")

    rets = []
    print("\n==== 反转策略虚拟账户概览（每标的独立 100 万） ====")
    for sym in sorted(state["accounts"]):
        a = state["accounts"][sym]
        val = float(a.get("qty", 0)) * float(a.get("last_close", 0))
        equity = float(a.get("cash", 0)) + val
        ret = (equity / REV_CAPITAL - 1.0) * 100.0
        rets.append(ret)
        print(f"  {sym}: 总资产 {equity:,.0f}（收益 {ret:+.2f}%）"
              f"{' [持仓]' if float(a.get('qty', 0)) > 0 else ''}")
    if rets:
        print(f"组合等权收益：{sum(rets)/len(rets):+.2f}%（{len(rets)} 账户）")
    print(f"累计成交：{len(state['trades'])} 笔")
    print("（T-1 跌幅信号、T 日开盘成交、持有 N 日收盘卖；仅作模拟，不构成投资建议）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", action="store_true", help="用合成情绪分演示")
    ap.add_argument("--reset", action="store_true", help="重置虚拟账户")
    ap.add_argument("--reversal", action="store_true",
                    help="反转因子模式（T-1 跌幅信号，独立账户与状态文件）")
    args = ap.parse_args()

    cfg = load_config()

    if args.reversal:
        run_reversal(cfg, args.reset)
        return 0

    sent_cfg = cfg["sentiment"]
    bt = cfg["backtest"]
    capital = float(bt["capital"])

    if args.reset and STATE_PATH.exists():
        STATE_PATH.unlink()
        print("[paper] 已重置虚拟账户")

    state = load_state(capital)
    for item in cfg["symbols"]:
        sym = item["symbol"]
        print(f"[paper] {item['name']}({sym}) ...")
        try:
            process_symbol(sym, item, sent_cfg, bt, state, args.synthetic)
        except Exception as e:  # noqa: BLE001
            print(f"  !! 处理失败: {e}")

    save_state(state)
    write_trades(state)

    # 汇总
    equity = state["cash"]
    print("\n==== 虚拟账户概览 ====")
    print(f"现金：{state['cash']:,.2f}")
    print("持仓：")
    for sym, pos in state["positions"].items():
        val = pos.get("qty", 0) * pos.get("last_close", 0)
        equity += val
        print(f"  {sym}: {pos.get('qty', 0):,.0f} 股 @ 成本 {pos.get('avg_cost', 0):.4f}"
              f" / 最新价 {pos.get('last_close', 0):.4f} / 市值 {val:,.2f}")
    print(f"总资产（现金+持仓市值）：{equity:,.2f}")
    print(f"累计成交：{len(state['trades'])} 笔")
    print("（撮合规则：T-1 日信号，T 日开盘成交；仅作模拟，不构成投资建议）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
