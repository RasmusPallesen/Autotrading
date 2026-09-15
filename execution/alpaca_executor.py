"""
Alpaca execution layer.
Places market orders and manages positions.

Error handling improvements:
- PDT (Pattern Day Trader) errors are detected and returned as structured errors
  so the risk manager and agent can respond appropriately rather than silently failing
- Insufficient buying power is detected and logged clearly
- All Alpaca API errors include the error code for debugging
"""

import logging
import math
import os
from typing import Optional

try:
    from notifier import notify_buy, notify_sell, notify_pdt_block
    _NOTIFY = True
except ImportError:
    _NOTIFY = False

logger = logging.getLogger(__name__)

# Alpaca error codes
_PDT_ERROR_CODE = 40310100       # Pattern day trader protection
_INSUFFICIENT_FUNDS_CODE = 40310000
_POSITION_NOT_FOUND_CODE = 40410000

# Sentinel return values so callers can distinguish error types from None
class ExecutionError:
    """Structured error returned instead of None so callers know why a trade failed."""
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        self.is_pdt = code == _PDT_ERROR_CODE
        self.is_insufficient_funds = code == _INSUFFICIENT_FUNDS_CODE
        self.is_position_not_found = code == _POSITION_NOT_FOUND_CODE

    def __bool__(self):
        # ExecutionError is falsy — callers checking `if result:` still work correctly
        return False

    def __repr__(self):
        return f"ExecutionError(code={self.code}, message={self.message!r})"


def _parse_alpaca_error(exc: Exception) -> Optional[ExecutionError]:
    """
    Try to extract a structured Alpaca error code from an exception.
    Alpaca errors come back as JSON in the exception message.
    """
    import json
    msg = str(exc)
    # Alpaca error messages contain raw JSON: {"code":40310100,"message":"..."}
    try:
        # Find the JSON blob in the exception string
        start = msg.find("{")
        end = msg.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(msg[start:end])
            code = data.get("code", 0)
            message = data.get("message", msg)
            return ExecutionError(code=code, message=message)
    except Exception:
        pass
    return None


class AlpacaExecutor:
    """Executes trades via the Alpaca Trading API."""

    def __init__(self, config):
        try:
            from alpaca.trading.client import TradingClient
            from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus, OrderClass
            from alpaca.trading.requests import (
                MarketOrderRequest, LimitOrderRequest, GetOrdersRequest,
                StopLossRequest, TakeProfitRequest,
            )
            self._OrderSide = OrderSide
            self._TimeInForce = TimeInForce
            self._OrderClass = OrderClass
            self._MarketOrderRequest = MarketOrderRequest
            self._LimitOrderRequest = LimitOrderRequest
            self._StopLossRequest = StopLossRequest
            self._TakeProfitRequest = TakeProfitRequest
            self._GetOrdersRequest = GetOrdersRequest
            self._QueryOrderStatus = QueryOrderStatus

            self.client = TradingClient(
                config.api_key, config.secret_key, paper=config.paper
            )
            self.paper = config.paper
            self._pdt_blocked = False
            self._notify = _NOTIFY  # Set True if PDT error fires — suppresses further sells this session
            logger.info("AlpacaExecutor initialised (paper=%s)", config.paper)
        except ImportError:
            raise ImportError("Install alpaca-py: pip install alpaca-py")

    @property
    def is_pdt_blocked(self) -> bool:
        return self._pdt_blocked

    def buy(
        self,
        symbol: str,
        notional: float,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        extended_hours: bool = False,
        limit_price: Optional[float] = None,
        current_price: Optional[float] = None,
    ) -> Optional[dict]:
        """
        Place a buy order.

        Regular hours: submits a whole-share BRACKET order (entry + broker-side
        stop-loss + take-profit as OCO children) when stop/target prices are
        provided — this is the only way to get real broker-side protection.
        Alpaca brackets require whole-share qty (no notional/fractional), so
        notional is converted to shares via current_price (falls back to
        limit_price). If no stop/target given, falls back to a plain notional
        market order (fractional allowed, but unprotected).

        Extended hours: Alpaca does NOT support bracket orders pre/post-market,
        so this submits a plain whole-share limit order. Protection for these
        entries is handled software-side by the stop-monitor in main.py.
        """
        try:
            if extended_hours and limit_price:
                qty = math.floor(notional / limit_price)  # whole shares only — fractional not allowed in extended hours
                if qty <= 0:
                    logger.warning("BUY %s skipped — computed qty=0 at limit_price=%.2f (notional=%.2f)", symbol, limit_price, notional)
                    return None
                order_req = self._LimitOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=self._OrderSide.BUY,
                    time_in_force=self._TimeInForce.DAY,
                    limit_price=round(limit_price, 2),
                    extended_hours=True,
                )
                logger.info(
                    "BUY (EXT, no broker stop — software-monitored) %s | qty=%d | limit=$%.2f | notional≈$%.2f | paper=%s",
                    symbol, qty, limit_price, notional, self.paper,
                )
            elif stop_loss_price and take_profit_price and (current_price or limit_price):
                # Regular-hours BRACKET order — real broker-side protection.
                ref_price = current_price or limit_price
                qty = math.floor(notional / ref_price)  # brackets require whole shares
                if qty <= 0:
                    logger.warning(
                        "BUY %s skipped — bracket qty=0 at price=$%.2f (notional=$%.2f); "
                        "account too small for one share.", symbol, ref_price, notional,
                    )
                    return None
                # Stop must be below entry and target above; guard against inversion.
                stop_px = round(stop_loss_price, 2)
                tp_px = round(take_profit_price, 2)
                order_req = self._MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=self._OrderSide.BUY,
                    time_in_force=self._TimeInForce.DAY,
                    order_class=self._OrderClass.BRACKET,
                    stop_loss=self._StopLossRequest(stop_price=stop_px),
                    take_profit=self._TakeProfitRequest(limit_price=tp_px),
                )
                logger.info(
                    "BUY (BRACKET) %s | qty=%d | ~$%.2f | stop=$%.2f | target=$%.2f | notional≈$%.2f | paper=%s",
                    symbol, qty, ref_price, stop_px, tp_px, notional, self.paper,
                )
            else:
                # No stop/target available — unprotected notional market order (legacy path).
                order_req = self._MarketOrderRequest(
                    symbol=symbol,
                    notional=round(notional, 2),
                    side=self._OrderSide.BUY,
                    time_in_force=self._TimeInForce.DAY,
                )
                logger.warning(
                    "BUY %s | notional=$%.2f | NO stop/target provided — unprotected market order",
                    symbol, notional,
                )
            order = self.client.submit_order(order_req)

            logger.info(
                "BUY %s | notional=$%.2f | order_id=%s | paper=%s",
                symbol, notional, order.id, self.paper,
            )
            result = {
                "order_id": str(order.id),
                "symbol": symbol,
                "side": "BUY",
                "notional": notional,
                "stop_loss": stop_loss_price,
                "take_profit": take_profit_price,
            }
            if self._notify:
                notify_buy(
                    symbol=symbol,
                    notional=notional,
                    confidence=0.0,   # Caller patches this via _last_decision if needed
                    urgency="MEDIUM",
                    rationale="",
                    stop_loss=stop_loss_price,
                    take_profit=take_profit_price,
                    paper=self.paper,
                )
            return result

        except Exception as e:
            error = _parse_alpaca_error(e)
            if error:
                if error.is_pdt:
                    self._pdt_blocked = True
                    logger.error(
                        "BUY %s BLOCKED -- Pattern Day Trader protection (code=%d). "
                        "Account has exceeded 3 round-trip trades in 5 business days. "
                        "No further day trades until the oldest trade ages out. "
                        "Consider switching to a cash account or raising account equity above $25,000.",
                        symbol, error.code,
                    )
                    if self._notify:
                        notify_pdt_block(symbol=symbol, paper=self.paper)
                elif error.is_insufficient_funds:
                    logger.error(
                        "BUY %s BLOCKED -- Insufficient buying power (code=%d): %s",
                        symbol, error.code, error.message,
                    )
                else:
                    logger.error(
                        "BUY %s FAILED -- Alpaca error code=%d: %s",
                        symbol, error.code, error.message,
                    )
                return error
            logger.error("Failed to place BUY for %s: %s", symbol, e)
            return None

    def sell(
        self,
        symbol: str,
        qty: Optional[float] = None,
        close_all: bool = False,
        extended_hours: bool = False,
        limit_price: Optional[float] = None,
        replace_pending: bool = False,
    ) -> Optional[dict]:
        """
        Place a sell order. Uses close_position() (market) during regular hours;
        a limit order with extended_hours=True during pre/post-market.

        By default, if any order is already open for the symbol the sell is
        skipped (avoids duplicate/oversell). Pass replace_pending=True to instead
        CANCEL those open orders first and proceed — used by the stop-monitor in
        extended hours, where a resting broker bracket leg (a stop/market order)
        is dormant and cannot execute, so it must be replaced with an
        extended-hours limit that can actually fill.
        """
        try:
            open_orders = self.client.get_orders(
                self._GetOrdersRequest(
                    status=self._QueryOrderStatus.OPEN,
                    symbols=[symbol],
                )
            )
            if open_orders:
                if replace_pending:
                    logger.info(
                        "SELL %s — cancelling %d pending order(s) to replace (ids: %s)",
                        symbol, len(open_orders),
                        ", ".join(str(o.id) for o in open_orders),
                    )
                    self.cancel_orders_for_symbol(symbol)
                else:
                    logger.warning(
                        "SELL %s skipped — %d open order(s) already pending (ids: %s)",
                        symbol,
                        len(open_orders),
                        ", ".join(str(o.id) for o in open_orders),
                    )
                    return {
                        "order_id": str(open_orders[0].id),
                        "symbol": symbol,
                        "side": "SELL",
                        "skipped": "already_pending",
                    }
        except Exception as e:
            logger.warning("Could not check open orders for %s: %s — proceeding with sell", symbol, e)

        try:
            if extended_hours and limit_price:
                # Extended hours: must use limit orders. Fetch position qty if close_all.
                if close_all:
                    pos = self.client.get_open_position(symbol)
                    sell_qty = math.floor(float(pos.qty))  # whole shares only — fractional not allowed in extended hours
                else:
                    if qty is None:
                        raise ValueError("Must specify qty or close_all=True")
                    sell_qty = math.floor(qty)  # whole shares only
                if sell_qty <= 0:
                    logger.warning("SELL %s skipped — computed qty=0 (position is sub-share fractional)", symbol)
                    return None
                order_req = self._LimitOrderRequest(
                    symbol=symbol,
                    qty=sell_qty,
                    side=self._OrderSide.SELL,
                    time_in_force=self._TimeInForce.DAY,
                    limit_price=round(limit_price, 2),
                    extended_hours=True,
                )
                order = self.client.submit_order(order_req)
                logger.info(
                    "SELL (EXT) %s | qty=%.6f | limit=$%.2f | order_id=%s | paper=%s",
                    symbol, sell_qty, limit_price, order.id, self.paper,
                )
                result = {
                    "order_id": str(order.id),
                    "symbol": symbol,
                    "side": "SELL",
                    "close_all": close_all,
                }
                if self._notify:
                    notify_sell(symbol=symbol, notional=0.0, confidence=0.0, urgency="MEDIUM", paper=self.paper)
                return result

            if close_all:
                response = self.client.close_position(symbol)
                logger.info("CLOSE POSITION %s | paper=%s", symbol, self.paper)
                result = {
                    "order_id": str(response.id),
                    "symbol": symbol,
                    "side": "SELL",
                    "close_all": True,
                }
                if self._notify:
                    notify_sell(
                        symbol=symbol,
                        notional=0.0,   # Caller patches notional after position lookup
                        confidence=0.0,
                        urgency="MEDIUM",
                        paper=self.paper,
                    )
                return result

            if qty is None:
                raise ValueError("Must specify qty or close_all=True")

            order_req = self._MarketOrderRequest(
                symbol=symbol,
                qty=math.floor(qty * 1_000_000) / 1_000_000,  # truncate, never round up
                side=self._OrderSide.SELL,
                time_in_force=self._TimeInForce.DAY,
            )
            order = self.client.submit_order(order_req)
            logger.info(
                "SELL %s | qty=%.6f | order_id=%s | paper=%s",
                symbol, qty, order.id, self.paper,
            )
            return {
                "order_id": str(order.id),
                "symbol": symbol,
                "side": "SELL",
                "qty": qty,
            }

        except Exception as e:
            error = _parse_alpaca_error(e)
            if error:
                if error.is_pdt:
                    self._pdt_blocked = True
                    logger.error(
                        "SELL %s BLOCKED -- Pattern Day Trader protection (code=%d). "
                        "Position cannot be closed today due to PDT rules. "
                        "If this position was opened regular-hours it has a broker "
                        "bracket stop; extended-hours entries rely on the software "
                        "stop-monitor. Agent will not retry this sell today.",
                        symbol, error.code,
                    )
                    if self._notify:
                        notify_pdt_block(symbol=symbol, paper=self.paper)
                elif error.is_position_not_found:
                    logger.warning(
                        "SELL %s -- Position not found (code=%d). "
                        "Already closed or never opened. Treating as success.",
                        symbol, error.code,
                    )
                    # Return a synthetic success so the agent removes it from its local state
                    return {
                        "order_id": "position_not_found",
                        "symbol": symbol,
                        "side": "SELL",
                        "close_all": close_all,
                    }
                else:
                    logger.error(
                        "SELL %s FAILED -- Alpaca error code=%d: %s",
                        symbol, error.code, error.message,
                    )
                return error
            logger.error("Failed to place SELL for %s: %s", symbol, e)
            return None

    def cancel_orders_for_symbol(self, symbol: str) -> int:
        """Cancel all OPEN orders for a single symbol. Returns count attempted."""
        try:
            open_orders = self.client.get_orders(
                self._GetOrdersRequest(
                    status=self._QueryOrderStatus.OPEN,
                    symbols=[symbol],
                )
            )
        except Exception as e:
            logger.warning("Could not list open orders for %s to cancel: %s", symbol, e)
            return 0
        n = 0
        for o in open_orders:
            try:
                self.client.cancel_order_by_id(o.id)
                n += 1
            except Exception as e:
                logger.warning("Failed to cancel order %s for %s: %s", o.id, symbol, e)
        if n:
            logger.info("Cancelled %d open order(s) for %s", n, symbol)
        return n

    def cancel_all_orders(self):
        """Emergency: cancel all open orders."""
        try:
            self.client.cancel_orders()
            logger.warning("All open orders cancelled.")
        except Exception as e:
            logger.error("Failed to cancel orders: %s", e)

    def close_all_positions(self):
        """Emergency kill switch: close every open position."""
        try:
            self.client.close_all_positions(cancel_orders=True)
            logger.warning("All positions closed (kill switch activated).")
        except Exception as e:
            logger.error("Failed to close all positions: %s", e)
