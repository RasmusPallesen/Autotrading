"""
Alpaca execution layer.
Places market orders and manages positions.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


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
            logger.info("AlpacaExecutor initialised (paper=%s)", config.paper)
        except ImportError:
            raise ImportError("Install alpaca-py: pip install alpaca-py")

    def buy(
        self,
        symbol: str,
        notional: float,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
    ) -> Optional[dict]:
        """
        Place a simple market buy order for a notional dollar amount.
        Fractional/notional orders cannot have bracket legs on Alpaca,
        so stop-loss and take-profit are tracked by the risk manager instead.
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
        """
        try:
            if close_all:
                response = self.client.close_position(symbol)
                logger.info("CLOSE POSITION %s | paper=%s", symbol, self.paper)
                return {"order_id": str(response.id), "symbol": symbol, "side": "SELL", "close_all": True}

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
            return {"order_id": str(order.id), "symbol": symbol, "side": "SELL", "qty": qty}

        except Exception as e:
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