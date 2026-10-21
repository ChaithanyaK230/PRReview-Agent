from decimal import Decimal

import pytest

from core.cost import CostMeter
from core.errors import BudgetExceeded


def test_under_cap_allows_calls():
    meter = CostMeter(Decimal("0.50"))
    meter.add(0.10)
    meter.check()  # must not raise
    assert meter.remaining == Decimal("0.40")


def test_reaching_cap_blocks_next_call():
    meter = CostMeter(Decimal("0.50"))
    meter.add(0.30)
    meter.add(0.20)
    with pytest.raises(BudgetExceeded):
        meter.check()


def test_float_costs_do_not_drift():
    meter = CostMeter(Decimal("1"))
    for _ in range(10):
        meter.add(0.1)
    assert meter.spent == Decimal("1.0")