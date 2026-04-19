"""
T+2 Settlement Tracker for cash accounts.
Tracks when sold positions will have settled funds available.
US markets settle on T+2 (trade date + 2 business days).
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Dict

logger = logging.getLogger(__name__)

# US market holidays 2025-2026 (NYSE observed holidays)
US_MARKET_HOLIDAYS = {
    date(2025, 1, 1),   # New Year's Day
    date(2025, 1, 20),  # MLK Day
    date(2025, 2, 17),  # Presidents' Day
    date(2025, 4, 18),  # Good Friday
    date(2025, 5, 26),  # Memorial Day
    date(2025, 6, 19),  # Juneteenth
    date(2025, 7, 4),   # Independence Day
    date(2025, 9, 1),   # Labor Day
    date(2025, 11, 27), # Thanksgiving
    date(2025, 12, 25), # Christmas
    date(2026, 1, 1),   # New Year's Day
    date(2026, 1, 19),  # MLK Day
    date(2026, 2, 16),  # Presidents' Day
    date(2026, 4, 3),   # Good Friday
    date(2026, 5, 25),  # Memorial Day
    date(2026, 6, 19),  # Juneteenth
    date(2026, 7, 3),   # Independence Day (observed)
    date(2026, 9, 7),   # Labor Day
    date(2026, 11, 26), # Thanksgiving
    date(2026, 12, 25), # Christmas
}


def _is_business_day(d: date) -> bool:
    """Returns True if date is a NYSE trading day."""
    return d.weekday() < 5 and d not in US_MARKET_HOLIDAYS


def _add_business_days(start: date, days: int) -> date:
    """Add N business days to a date, skipping weekends and holidays."""
    current = start
    added = 0
    while added < days:
        current += timedelta(days=1)
        if _is_business_day(current):
            added += 1
    return current


def settlement_date(trade_date: date) -> date:
    """Returns the T+2 settlement date for a trade."""
    return _add_business_days(trade_date, 2)


class SettlementTracker:
    """
    Tracks unsettled sale proceeds for a cash account.
    Blocks buys if insufficient settled cash is available.
    """

    def __init__(self):
        # Maps settlement_date -> amount that settles on that date
        self._pending: Dict[date, float] = {}
        logger.info("SettlementTracker initialised (T+2 cash account mode)")

    def record_sale(self, notional: float, trade_date: date = None):
        """
        Record a sale. The proceeds will be available after T+2.
        Call this every time a SELL order is executed.
        """
        if trade_date is None:
            trade_date = datetime.now(timezone.utc).date()

        settle_on = settlement_date(trade_date)
        self._pending[settle_on] = self._pending.get(settle_on, 0.0) + notional

        logger.info(
            "Sale of $%.2f recorded. Funds settle on %s (T+2).",
            notional, settle_on.isoformat(),
        )

    def unsettled_amount(self) -> float:
        """Returns total amount of unsettled sale proceeds."""
        today = datetime.now(timezone.utc).date()
        self._clear_settled(today)
        return sum(v for k, v in self._pending.items() if k > today)

    def settled_cash(self, total_cash: float) -> float:
        """
        Returns how much of the total cash balance is settled and usable.
        total_cash: the full cash balance from the broker.
        """
        usable = total_cash - self.unsettled_amount()
        return max(usable, 0.0)

    def can_buy(self, notional: float, total_cash: float) -> tuple:
        """
        Returns (allowed: bool, reason: str).
        Checks whether a buy of `notional` can be made with settled funds.
        """
        usable = self.settled_cash(total_cash)
        if notional <= usable:
            return True, f"Settled cash available: ${usable:,.2f}"
        return False, (
            f"Insufficient settled cash. "
            f"Requested: ${notional:,.2f} | "
            f"Settled: ${usable:,.2f} | "
            f"Unsettled (T+2 pending): ${self.unsettled_amount():,.2f}"
        )

    def _clear_settled(self, today: date):
        """Remove entries that have already settled."""
        settled_keys = [k for k in self._pending if k <= today]
        for k in settled_keys:
            logger.info("$%.2f settled on %s — now available.", self._pending[k], k)
            del self._pending[k]

    def status(self) -> dict:
        """Returns a summary of pending settlements."""
        today = datetime.now(timezone.utc).date()
        self._clear_settled(today)
        return {
            "unsettled_total": self.unsettled_amount(),
            "pending_settlements": {
                k.isoformat(): v for k, v in sorted(self._pending.items())
            },
        }
