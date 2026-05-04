"""
Risk Manager.
Hard-coded guardrails that the agent cannot override.
Checks every trade decision before it reaches the execution layer.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from agent.decision_engine import TradeDecision
from risk.settlement_tracker import SettlementTracker

logger = logging.getLogger(__name__)


@dataclass
class RiskVerdict:
    approved: bool
    reason: str
    adjusted_notional: Optional[float] = None


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
        self.min_settled_cash_reserve = getattr(config, 'min_settled_cash_reserve', 30.0)

        self._daily_start_equity: Optional[float] = None
        self._killed = False
        self.settlement = SettlementTracker()

    def check(
        self,
        decision: TradeDecision,
        portfolio: dict,
        positions: list,
        min_confidence: float,
    ) -> RiskVerdict:
        """Validate a trade decision against all risk rules."""

        if self._killed:
            return RiskVerdict(False, "Kill switch is active -- no trading until reset.")

        # Daily drawdown kill switch
        equity = portfolio.get("equity", 0)
        if self._daily_start_equity is None:
            self._daily_start_equity = equity

        if self._daily_start_equity > 0:
            drawdown = (self._daily_start_equity - equity) / self._daily_start_equity
            if drawdown >= self.max_daily_drawdown_pct:
                self._killed = True
                logger.critical(
                    "Daily drawdown limit hit (%.2f%% >= %.2f%%). Kill switch activated.",
                    drawdown * 100, self.max_daily_drawdown_pct * 100,
                )
                return RiskVerdict(False, f"Daily drawdown limit hit ({drawdown*100:.2f}%). Agent shut down.")

        # HOLD passes through
        if decision.action == "HOLD":
            return RiskVerdict(True, "HOLD -- no trade to validate.")

        # SELL passes through with no notional check
        if decision.action == "SELL":
            if decision.confidence < min_confidence:
                return RiskVerdict(
                    False,
                    f"Confidence {decision.confidence:.2f} below threshold {min_confidence:.2f}",
                )
            return RiskVerdict(True, "SELL approved.", adjusted_notional=0)

        # BUY checks below
        if decision.confidence < min_confidence:
            return RiskVerdict(
                False,
                f"Confidence {decision.confidence:.2f} below threshold {min_confidence:.2f}",
            )

        # Max open positions
        current_symbols = {p["symbol"] for p in positions}
        is_new_position = decision.symbol not in current_symbols
        if is_new_position and len(current_symbols) >= self.max_open_positions:
            return RiskVerdict(
                False,
                f"Max open positions ({self.max_open_positions}) reached.",
            )

        # Position size
        requested_pct = min(decision.suggested_position_pct, self.max_position_pct)
        notional = equity * requested_pct

        # T+2 settlement check with urgency-aware reserve
        # HIGH urgency signals (RSI extremes, volume spikes) can access the
        # settled cash reserve. MEDIUM/LOW urgency signals cannot — the reserve
        # is held back specifically for high-conviction opportunities like the
        # PODD RSI-10.5 setup on 05/04 that was blocked by a $10.80 shortfall.
        total_cash = max(
            portfolio.get("cash", 0),
            portfolio.get("buying_power", 0),
        )
        urgency = getattr(decision, "urgency", "MEDIUM")
        is_high_urgency = urgency == "HIGH"

        settled = self.settlement.settled_cash(total_cash)
        usable = settled  # What's available for this trade

        if not is_high_urgency:
            # Non-HIGH trades must leave the reserve untouched
            usable = max(0.0, settled - self.min_settled_cash_reserve)
            if notional > usable:
                return RiskVerdict(
                    False,
                    f"T+2 settlement block: Insufficient settled cash (reserve protected). "
                    f"Requested: ${notional:,.2f} | "
                    f"Available after ${self.min_settled_cash_reserve:.0f} reserve: ${usable:,.2f} | "
                    f"Unsettled (T+2 pending): ${self.settlement.unsettled_amount():,.2f}"
                )
        else:
            # HIGH urgency can use full settled cash including reserve
            if notional > settled:
                return RiskVerdict(
                    False,
                    f"T+2 settlement block (HIGH urgency — reserve waived): "
                    f"Requested: ${notional:,.2f} | "
                    f"Settled: ${settled:,.2f} | "
                    f"Unsettled (T+2 pending): ${self.settlement.unsettled_amount():,.2f}"
                )
            logger.info(
                "[%s] HIGH urgency trade accessing settlement reserve "
                "(settled=$%.2f, reserve=$%.2f)",
                decision.symbol, settled, self.min_settled_cash_reserve,
            )

        can_buy, settlement_reason = self.settlement.can_buy(notional, total_cash)
        if not can_buy and is_high_urgency:
            # Redundant safety check — should not reach here
            return RiskVerdict(False, f"T+2 settlement block: {settlement_reason}")

        # Buying power cap
        # For HIGH urgency signals, require at least enough buying power to meet
        # the minimum trade size before reducing notional. This prevents the agent
        # from draining buying_power on MEDIUM trades and leaving HIGH urgency
        # signals (like DXCM RSI-15.7 on 05/04) with only $3.56 to work with.
        buying_power = portfolio.get("buying_power", 0)

        # Reserve buying power for HIGH urgency signals:
        # MEDIUM/LOW trades are blocked if buying power is below 2× minimum trade,
        # preserving at least $20 for any HIGH urgency signal arriving later this tick.
        min_trade = 10.0
        high_urgency_reserve = min_trade * 2  # $20 reserved for HIGH urgency

        if not is_high_urgency:
            effective_buying_power = max(0.0, buying_power - high_urgency_reserve)
            if notional > effective_buying_power:
                if effective_buying_power < min_trade:
                    return RiskVerdict(
                        False,
                        f"Insufficient buying power (reserving ${high_urgency_reserve:.0f} "
                        f"for HIGH urgency signals). "
                        f"Available: ${effective_buying_power:.2f} | "
                        f"Total buying power: ${buying_power:.2f}"
                    )
                notional = effective_buying_power * 0.95
                logger.warning(
                    "Notional reduced to fit buying power (after HIGH urgency reserve): $%.2f",
                    notional,
                )
        else:
            # HIGH urgency: use full buying power, no reserve deduction
            if notional > buying_power:
                notional = buying_power * 0.95
                if notional <= 0:
                    return RiskVerdict(False, "Insufficient buying power.")
                logger.warning(
                    "HIGH urgency notional reduced to fit buying power: $%.2f", notional
                )

        # Minimum trade size — applies to all urgency levels
        if notional < min_trade:
            return RiskVerdict(
                False,
                f"Trade notional ${notional:.2f} below minimum ${min_trade:.0f}. "
                f"{'(HIGH urgency — full buying power used)' if is_high_urgency else ''}"
            )

        return RiskVerdict(True, f"Approved -- notional=${notional:.2f}", adjusted_notional=notional)

    def record_sale(self, notional: float):
        """Record a sale for T+2 settlement tracking."""
        self.settlement.record_sale(notional)

    def compute_stop_and_target(self, current_price: float, decision: TradeDecision) -> tuple:
        """Compute stop-loss and take-profit prices."""
        sl_pct = max(0.01, min(decision.suggested_stop_loss_pct, self.stop_loss_pct))
        tp_pct = max(0.01, min(decision.suggested_take_profit_pct, self.take_profit_pct))
        stop_loss = current_price * (1 - sl_pct)
        take_profit = current_price * (1 + tp_pct)
        return stop_loss, take_profit

    def settlement_status(self) -> dict:
        """Returns current settlement tracker status."""
        return self.settlement.status()

    def reset_daily(self, equity: float):
        """Call at the start of each trading day."""
        self._daily_start_equity = equity
        self._killed = False
        logger.info("Risk manager daily reset. Starting equity: $%.2f", equity)

    def activate_kill_switch(self):
        self._killed = True
        logger.warning("Kill switch manually activated.")

    def deactivate_kill_switch(self):
        self._killed = False
        logger.warning("Kill switch manually deactivated.")

    @property
    def is_killed(self) -> bool:
        return self._killed