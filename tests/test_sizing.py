"""仓位计算单元测试：覆盖固定股数、风险预算、lot 取整、上限与边界。"""

import pytest

pytest.importorskip("vnpy_ctastrategy")

from strategies.news_sentiment_strategy import calc_size  # noqa: E402


def test_fixed_size_no_risk():
    assert calc_size(10.0, 100, 0.0, 0.05, 0.95, 100, 1_000_000) == 100


def test_risk_based_size():
    # 风险 1% of 1M = 1 万；每股风险 = 10 * 5% = 0.5 -> 2 万股
    assert calc_size(10.0, 100, 0.01, 0.05, 0.95, 100, 1_000_000) == 20000


def test_lot_rounding_down():
    # 固定 123 股，按 100 股整手向下取整 -> 100 股
    assert calc_size(10.0, 123, 0.0, 0.0, 0.95, 100, 1_000_000) == 100


def test_max_position_cap():
    # 风险预算 200000 股，被 95% 仓位上限(95000)截断
    assert calc_size(10.0, 100, 0.10, 0.05, 0.95, 100, 1_000_000) == 95000


def test_zero_price():
    assert calc_size(0.0, 100, 0.0, 0.0, 0.95, 100, 1_000_000) == 0


def test_us_lot_size_one():
    # 美股 lot_size=1，不取整
    assert calc_size(200.0, 100, 0.01, 0.05, 0.95, 1, 1_000_000) == 1000
