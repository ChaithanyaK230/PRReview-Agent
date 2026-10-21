from decimal import Decimal

from core.errors import BudgetExceeded


class CostMeter:
    """Tracks LLM spend for one job and blocks new calls once the cap is reached."""

    def __init__(self, cap_usd: Decimal) -> None:
        self.cap = cap_usd
        self.spent = Decimal("0")

    @property
    def remaining(self) -> Decimal:
        return self.cap - self.spent

    def check(self) -> None:
        """Call before every LLM request."""
        if self.spent >= self.cap:
            raise BudgetExceeded(f"spent ${self.spent} of ${self.cap} budget")

    def add(self, cost_usd: float | Decimal) -> None:
        """Call after every LLM request with the cost OpenRouter reported."""
        # str() first so a float like 0.1 doesn't become 0.1000000000000000055...
        self.spent += Decimal(str(cost_usd))