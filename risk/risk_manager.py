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
        atr: float = None,
        current_price: float = None,
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
                try:
                    from notifier import notify_kill_switch
                    notify_kill_switch(
                        reason=f"Daily drawdown {drawdown*100:.2f}% exceeded limit {self.max_daily_drawdown_pct*100:.2f}%",
                        equity=equity,
                        drawdown_pct=drawdown * 100,
                    )
                except Exception:
                    pass
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

        # Position size — volatility-adaptive when ATR + price are available
        requested_pct = min(decision.suggested_position_pct, self.max_position_pct)
        if atr and atr > 0 and current_price and current_price > 0:
            # Risk 1% of equity per trade, sized so the ATR×2 stop costs exactly that
            dollar_risk = equity * 0.01
            shares = dollar_risk / (atr * 2.0)
            notional = shares * current_price
            notional = min(notional, equity * self.max_position_pct)  # cap at max position
            logger.debug(
                "[%s] ATR sizing: equity=%.2f atr=%.4f shares=%.2f notional=%.2f",
                decision.symbol, equity, atr, shares, notional,
            )
        else:
            notional = equity * requested_pct

        # Aggregate position cap — existing + new must not exceed max_position_pct
        existing_position = next((p for p in positions if p["symbol"] == decision.symbol), None)
        existing_notional = float(existing_position.get("market_value", 0)) if existing_position else 0.0
        if existing_notional > 0:
            max_symbol_notional = equity * self.max_position_pct
            headroom = max(0.0, max_symbol_notional - existing_notional)
            if headroom <= 0:
                return RiskVerdict(
                    False,
                    f"Position already at max allocation "
                    f"(existing=${existing_notional:,.2f}, cap=${max_symbol_notional:,.2f}). "
                    f"No headroom for add-on.",
                )
            notional = min(notional, headroom)
            logger.info(
                "[%s] Add-on BUY: existing=$%.2f headroom=$%.2f new_notional=$%.2f",
                decision.symbol, existing_notional, headroom, notional,
            )

        # Early exit: if notional is already below minimum before any adjustments,
        # skip all the settlement and buying power checks — the trade is impossible
        # regardless of cash position. This avoids misleading block reasons like
        # "reserve protected" when the real issue is the account is too small.
        min_trade = 10.0
        if notional < min_trade:
            return RiskVerdict(
                False,
                f"Trade notional ${notional:.2f} below minimum ${min_trade:.0f} "
                f"(equity=${equity:.2f} x {requested_pct:.0%} = ${notional:.2f}). "
                f"Account equity too low to trade at current position sizing."
            )

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

        # Scale reserve with equity — 5% of equity, min $10, max configured value.
        # A fixed $30 reserve on a $62 account is 48% — too aggressive.
        scaled_reserve = max(10.0, min(self.min_settled_cash_reserve, equity * 0.05))

        if not is_high_urgency:
            # Non-HIGH trades must leave the scaled reserve untouched
            usable = max(0.0, settled - scaled_reserve)
            if notional > usable:
                if usable < min_trade:
                    return RiskVerdict(
                        False,
                        f"T+2 settlement block: Settled cash after ${scaled_reserve:.0f} reserve "
                        f"(${usable:.2f}) below minimum trade size (${min_trade:.0f}). "
                        f"Unsettled (T+2 pending): ${self.settlement.unsettled_amount():,.2f}"
                    )
                notional = usable
                logger.info(
                    "[%s] Notional scaled to $%.2f to fit settled cash "
                    "(after $%.0f reserve; was $%.2f)",
                    decision.symbol, notional, scaled_reserve, equity * requested_pct,
                )
        else:
            # HIGH urgency can use full settled cash including reserve
            if notional > settled:
                if settled < min_trade:
                    return RiskVerdict(
                        False,
                        f"T+2 settlement block (HIGH urgency): "
                        f"Settled cash ${settled:.2f} below minimum trade size (${min_trade:.0f}). "
                        f"Unsettled (T+2 pending): ${self.settlement.unsettled_amount():,.2f}"
                    )
                notional = settled * 0.95
                logger.info(
                    "[%s] HIGH urgency notional scaled to $%.2f to fit settled cash",
                    decision.symbol, notional,
                )
            else:
                logger.info(
                    "[%s] HIGH urgency trade accessing settlement reserve "
                    "(settled=$%.2f, scaled_reserve=$%.2f)",
                    decision.symbol, settled, scaled_reserve,
                )

        can_buy, settlement_reason = self.settlement.can_buy(notional, total_cash)
        if not can_buy and is_high_urgency:
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

    def compute_stop_and_target(
        self, current_price: float, decision: TradeDecision, atr: float = None
    ) -> tuple:
        """Compute stop-loss and take-profit prices. Uses ATR-based levels when available."""
        if atr and atr > 0:
            # ATR-based swing-trade stops: 2× ATR stop, 4× ATR target (2:1 reward/risk)
            stop_loss = current_price - atr * 2.0
            take_profit = current_price + atr * 4.0
            logger.debug(
                "[%s] ATR stops: price=%.2f atr=%.4f stop=%.2f target=%.2f",
                decision.symbol, current_price, atr, stop_loss, take_profit,
            )
        else:
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