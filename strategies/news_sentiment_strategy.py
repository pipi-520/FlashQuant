"""新闻情绪驱动 CTA 策略（vnpy CtaTemplate）——强化版 v2。

相对 v1 的增强：
- 前视修复：用「上一交易日」的情绪分生成信号，避免使用当日尚未收盘的新闻（前视偏差）。
- 风控出场：初始止损 / 止盈 / 移动止损（收盘价判断）。
- 可选做空：allow_short=True 时，情绪分 <= -threshold 反向开空（A股普通账户不可融券，默认关）。
- 风险预算仓位：risk_percent > 0 时按止损距离动态计算股数，并向下取整到 lot_size。

情绪分由 scripts/sentiment_score.py 预计算，按日期写入 CSV（列：date, score）。
仅用于学习研究，不构成投资建议。
"""

import csv
import os

from vnpy_ctastrategy import CtaTemplate
from vnpy.trader.object import BarData


def load_sentiment(path: str) -> dict:
    """读取 sentiment CSV（列: date, score），返回 {date(str): score(float)}。"""
    result: dict = {}
    if not path or not os.path.exists(path):
        return result
    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            d = (row.get("date") or "").strip()
            try:
                result[d] = float(row.get("score") or 0.0)
            except (TypeError, ValueError):
                continue
    return result


def calc_size(price: float, fixed_size: int, risk_percent: float, stop_loss_pct: float,
              max_position_pct: float, lot_size: int, capital: float) -> int:
    """按风险预算计算开仓股数，取整到 lot_size、受仓位上限约束。

    回测策略（vnpy）与本地模拟盘（scripts/paper_trade.py）共用此函数，
    保证两处仓位计算完全一致（单一来源，避免漂移）。
    """
    if price <= 0:
        return 0
    if risk_percent > 0 and stop_loss_pct > 0:
        risk_amount = capital * risk_percent
        per_share_risk = price * stop_loss_pct
        size = int(risk_amount / per_share_risk)
    else:
        size = int(fixed_size)
    max_size = int(capital * max_position_pct / price)
    size = min(size, max_size)
    if lot_size > 1:
        size = size // lot_size * lot_size
    return max(0, size)


class NewsSentimentStrategy(CtaTemplate):
    """新闻情绪驱动策略（多空可选 + 风控 + 风险仓位）。"""

    author: str = "FlashQuant"

    # ---- 信号参数 ----
    threshold: float = 0.3          # 开多阈值（做空为 -threshold）
    threshold_flat: float = -0.3    # 平多阈值（做空平仓为 -threshold_flat）
    allow_short: bool = False       # 是否允许做空（A股普通账户不可融券，默认关）

    # ---- 仓位参数 ----
    fixed_size: int = 100           # 固定股数（risk_percent=0 时生效）
    risk_percent: float = 0.0       # 单笔风险占初始资金比例（>0 覆盖 fixed_size）
    max_position_pct: float = 0.95  # 单标的仓位上限（占初始资金比例）
    lot_size: int = 100             # 最小交易单位（A股=100，美股=1）
    capital: float = 1_000_000.0    # 初始资金（用于风险仓位估算）

    # ---- 出场 / 风控参数 ----
    stop_loss_pct: float = 0.05     # 初始止损（0=关闭）
    take_profit_pct: float = 0.0    # 止盈（0=关闭）
    trailing_stop_pct: float = 0.0  # 移动止损（0=关闭）

    sentiment_path: str = ""

    parameters: list = [
        "threshold", "threshold_flat", "allow_short",
        "fixed_size", "risk_percent", "max_position_pct", "lot_size", "capital",
        "stop_loss_pct", "take_profit_pct", "trailing_stop_pct",
        "sentiment_path",
    ]
    variables: list = ["score", "entry_price", "highest_price", "lowest_price", "_last_date"]

    def on_init(self) -> None:
        self.sentiment: dict = load_sentiment(self.sentiment_path)
        self.score: float = 0.0
        self.entry_price: float = 0.0
        self.highest_price: float = 0.0
        self.lowest_price: float = 0.0
        self._last_date = None
        self.write_log(f"载入情绪数据 {len(self.sentiment)} 天")

    def on_start(self) -> None:
        # vnpy 在 on_init 之后才从 strategy_data 恢复 variables，
        # 因此状态一致性检查放在 on_start（此时 pos/entry_price 已恢复）：
        # 空仓却残留入场价时，止损/移动止损会基于失效价格判断，必须清理。
        if self.pos == 0 and self.entry_price > 0:
            self.write_log("检测到空仓但残留入场价，重置风控状态")
            self.entry_price = 0.0
            self.highest_price = 0.0
            self.lowest_price = 0.0
        self.write_log("策略启动（新闻情绪驱动 v2：风控+风险仓位+可选做空）")

    def on_stop(self) -> None:
        self.write_log("策略停止")

    # ---------- 工具 ----------
    def _calc_size(self, price: float) -> int:
        return calc_size(price, self.fixed_size, self.risk_percent, self.stop_loss_pct,
                         self.max_position_pct, self.lot_size, self.capital)

    def _open_long(self, price: float) -> None:
        size = self._calc_size(price)
        if size <= 0:
            return
        self.buy(price, float(size))
        self.entry_price = price
        self.highest_price = price

    def _open_short(self, price: float) -> None:
        size = self._calc_size(price)
        if size <= 0:
            return
        self.short(price, float(size))
        self.entry_price = price
        self.lowest_price = price

    def _reset_position(self) -> None:
        self.entry_price = 0.0
        self.highest_price = 0.0
        self.lowest_price = 0.0

    def _check_exit(self, bar: BarData) -> bool:
        """检查止损/止盈/移动止损，触发则平仓并返回 True（收盘价判断）。"""
        price = bar.close_price
        if self.pos > 0:
            self.highest_price = max(self.highest_price, price)
            stop = self.entry_price * (1.0 - self.stop_loss_pct) if self.stop_loss_pct > 0 else None
            if self.trailing_stop_pct > 0:
                trail = self.highest_price * (1.0 - self.trailing_stop_pct)
                if stop is None or trail > stop:
                    stop = trail
            take = self.entry_price * (1.0 + self.take_profit_pct) if self.take_profit_pct > 0 else None
            if stop is not None and price <= stop:
                self.sell(price, abs(self.pos))
                self._reset_position()
                return True
            if take is not None and price >= take:
                self.sell(price, abs(self.pos))
                self._reset_position()
                return True
        elif self.pos < 0:
            self.lowest_price = min(self.lowest_price, price)
            stop = self.entry_price * (1.0 + self.stop_loss_pct) if self.stop_loss_pct > 0 else None
            if self.trailing_stop_pct > 0:
                trail = self.lowest_price * (1.0 + self.trailing_stop_pct)
                if stop is None or trail < stop:
                    stop = trail
            take = self.entry_price * (1.0 - self.take_profit_pct) if self.take_profit_pct > 0 else None
            if stop is not None and price >= stop:
                self.cover(price, abs(self.pos))
                self._reset_position()
                return True
            if take is not None and price <= take:
                self.cover(price, abs(self.pos))
                self._reset_position()
                return True
        return False

    def on_bar(self, bar: BarData) -> None:
        """逐日 K 线回调：T-1 情绪 -> T 日交易（避免前视）。"""
        # 1) 上一交易日情绪分
        if self._last_date is not None:
            self.score = float(self.sentiment.get(self._last_date, 0.0))
        else:
            self.score = 0.0

        # 2) 已有持仓：先做风控出场
        if self.pos != 0 and self._check_exit(bar):
            self._last_date = bar.datetime.strftime("%Y-%m-%d")
            return

        # 3) 空仓：按信号开仓
        if self.pos == 0:
            if self.score >= self.threshold:
                self._open_long(bar.close_price)
            elif self.allow_short and self.score <= -self.threshold:
                self._open_short(bar.close_price)
        # 4) 持仓中的情绪反转出场
        elif self.pos > 0 and self.score <= self.threshold_flat:
            self.sell(bar.close_price, abs(self.pos))
            self._reset_position()
        elif self.pos < 0 and self.score >= -self.threshold_flat:
            self.cover(bar.close_price, abs(self.pos))
            self._reset_position()

        self._last_date = bar.datetime.strftime("%Y-%m-%d")
