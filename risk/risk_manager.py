"""
Risk Manager.
Hard-coded guardrails that the agent cannot override.
Checks every trade decision before it reaches the execution layer.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from agent.decision_engine import TradeDecision

logger = logging.getLogger(__name__)


@dataclass
class RiskVerdict:
    approved: bool
    reason: str
    adjusted_notional: Optional[float] = None  # May be reduced from requested


class RiskManager:
    """
    Enforces risk rules on every trade decision.
    Returns a RiskVerdict — only execute if approved=True.
    """

    def __init__(self, config):
        self.max_position_pct = config.max_position_pct
        self.stop_loss_pct = config.stop_loss_pct
        self.take_profit_pct = config.take_profit_pct
        self.max_daily_drawdown_pct = config.max_daily_drawdown_pct
        self.max_open_positions = config.max_open_positions

        self._daily_start_equity: Optional[float] = None
        self._killed = False  # Kill switch state

    def check(
        self,
        decision: TradeDecision,
        portfolio: dict,
        positions: list,
        min_confidence: float,
    ) -> RiskVerdict:
        """
        Validate a trade decision against all risk rules.
        Returns RiskVerdict with approved=True only if all checks pass.
        """
        if self._killed:
            return RiskVerdict(False, "Kill switch is active — no trading until reset.")

        # --- Kill switch: daily drawdown ---
        equity = portfolio.get("equity", 0)
        if self._daily_start_equity is None:
            self._daily_start_equity = equity

        if self._daily_start_equity > 0:
            drawdown = (self._daily_start_equity - equity) / self._daily_start_equity
            if drawdown >= self.max_daily_drawdown_pct:
                self._killed = True
                logger.critical(
                    "Daily drawdown limit hit (%.2f%% >= %.2f%%). Kill switch activated.",
                    drawdown * 100,
                    self.max_daily_drawdown_pct * 100,
                )
                return RiskVerdict(False, f"Daily drawdown limit hit ({drawdown*100:.2f}%). Agent shut down.")

        # --- HOLD decisions pass through (no execution needed) ---
        if decision.action == "HOLD":
            return RiskVerdict(True, "HOLD — no trade to validate.")

        # --- Confidence threshold ---
        if decision.confidence < min_confidence:
            return RiskVerdict(
                False,
                f"Confidence {decision.confidence:.2f} below threshold {min_confidence:.2f}",
            )

        # --- Position count limit ---
        current_symbols = {p["symbol"] for p in positions}
        is_new_position = decision.symbol not in current_symbols
        if decision.action == "BUY" and is_new_position:
            if len(current_symbols) >= self.max_open_positions:
                return RiskVerdict(
                    False,
                    f"Max open positions ({self.max_open_positions}) reached. Close existing positions first.",
                )

        # --- Position size cap ---
        requested_pct = min(decision.suggested_position_pct, self.max_position_pct)
        notional = equity * requested_pct

        buying_power = portfolio.get("buying_power", 0)
        if decision.action == "BUY" and notional > buying_power:
            notional = buying_power * 0.95  # Use up to 95% of available BP
            if notional <= 0:
                return RiskVerdict(False, "Insufficient buying power.")
            logger.warning("Notional reduced to fit buying power: $%.2f", notional)

        # --- Minimum trade size ($10 minimum) ---
        if notional < 10:
            return RiskVerdict(False, f"Trade notional ${notional:.2f} below minimum $10.")

        return RiskVerdict(True, f"Approved — notional=${notional:.2f}", adjusted_notional=notional)

    def compute_stop_and_target(self, current_price: float, decision: TradeDecision) -> tuple:
        """
        Compute stop-loss and take-profit prices from decision suggestions,
        clamped to configured limits.
        """
        sl_pct = max(0.01, min(decision.suggested_stop_loss_pct, self.stop_loss_pct))
        tp_pct = max(0.01, min(decision.suggested_take_profit_pct, self.take_profit_pct))

        stop_loss = current_price * (1 - sl_pct)
        take_profit = current_price * (1 + tp_pct)
        return stop_loss, take_profit

    def reset_daily(self, equity: float):
        """Call at the start of each trading day to reset daily drawdown tracking."""
        self._daily_start_equity = equity
        self._killed = False
        logger.info("Risk manager daily reset. Starting equity: $%.2f", equity)

    def activate_kill_switch(self):
        """Manually activate the kill switch."""
        self._killed = True
        logger.warning("Kill switch manually activated.")

    def deactivate_kill_switch(self):
        """Manually deactivate (use with caution)."""
        self._killed = False
        logger.warning("Kill switch manually deactivated.")

    @property
    def is_killed(self) -> bool:
        return self._killed
