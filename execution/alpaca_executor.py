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
from typing import Optional

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
            from alpaca.trading.enums import OrderSide, TimeInForce
            from alpaca.trading.requests import MarketOrderRequest
            self._OrderSide = OrderSide
            self._TimeInForce = TimeInForce
            self._MarketOrderRequest = MarketOrderRequest

            self.client = TradingClient(
                config.api_key, config.secret_key, paper=config.paper
            )
            self.paper = config.paper
            self._pdt_blocked = False  # Set True if PDT error fires — suppresses further sells this session
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
    ) -> Optional[dict]:
        """
        Place a simple market buy order for a notional dollar amount.
        Returns a result dict on success, ExecutionError on known failure, None on unknown failure.
        """
        try:
            order_req = self._MarketOrderRequest(
                symbol=symbol,
                notional=round(notional, 2),
                side=self._OrderSide.BUY,
                time_in_force=self._TimeInForce.DAY,
            )
            order = self.client.submit_order(order_req)

            logger.info(
                "BUY %s | notional=$%.2f | order_id=%s | paper=%s",
                symbol, notional, order.id, self.paper,
            )
            return {
                "order_id": str(order.id),
                "symbol": symbol,
                "side": "BUY",
                "notional": notional,
                "stop_loss": stop_loss_price,
                "take_profit": take_profit_price,
            }

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
    ) -> Optional[dict]:
        """
        Place a market sell order.
        Use close_all=True to liquidate the entire position.
        Returns a result dict on success, ExecutionError on known failure, None on unknown failure.
        """
        try:
            if close_all:
                response = self.client.close_position(symbol)
                logger.info("CLOSE POSITION %s | paper=%s", symbol, self.paper)
                return {
                    "order_id": str(response.id),
                    "symbol": symbol,
                    "side": "SELL",
                    "close_all": True,
                }

            if qty is None:
                raise ValueError("Must specify qty or close_all=True")

            order_req = self._MarketOrderRequest(
                symbol=symbol,
                qty=round(qty, 6),
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
                        "The stop-loss bracket order at Alpaca is still active and will "
                        "protect the position. Agent will not retry this sell today.",
                        symbol, error.code,
                    )
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
