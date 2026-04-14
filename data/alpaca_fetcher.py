"""
Alpaca market data fetcher.
Retrieves bars (OHLCV), quotes, and account info from Alpaca's data API.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)


class AlpacaDataFetcher:
    """Fetches market data and account info from Alpaca."""

    def __init__(self, config):
        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame
            from alpaca.trading.client import TradingClient
            self._StockBarsRequest = StockBarsRequest
            self._TimeFrame = TimeFrame
            self.data_client = StockHistoricalDataClient(
                config.api_key, config.secret_key
            )
            self.trading_client = TradingClient(
                config.api_key, config.secret_key, paper=config.paper
            )
            logger.info("AlpacaDataFetcher initialised (paper=%s)", config.paper)
        except ImportError:
            raise ImportError("Install alpaca-py: pip install alpaca-py")

    def get_bars(
        self,
        symbols: List[str],
        lookback_bars: int = 50,
        timeframe: str = "1Min",
    ) -> Dict[str, pd.DataFrame]:
        """
        Returns a dict of {symbol: DataFrame} with OHLCV columns.
        DataFrame is sorted ascending by timestamp.
        """
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        from itertools import groupby

        tf_map = {
            "1Min": TimeFrame.Minute,
            "5Min": TimeFrame(5, TimeFrameUnit.Minute),
            "15Min": TimeFrame(15, TimeFrameUnit.Minute),
            "1Hour": TimeFrame.Hour,
            "1Day": TimeFrame.Day,
        }
        tf = tf_map.get(timeframe, TimeFrame.Minute)

        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=max(lookback_bars * 2, 120))

        request = self._StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=tf,
            start=start,
            end=end,
            limit=lookback_bars,
            feed="iex",
        )

        bars = self.data_client.get_stock_bars(request)
        result: Dict[str, pd.DataFrame] = {}

        # Handle both dict-style and list-style responses from alpaca-py
        data = bars.data if hasattr(bars, "data") else bars

        if isinstance(data, dict):
            items = list(data.items())
        else:
            sorted_bars = sorted(data, key=lambda b: b.symbol)
            items = [
                (symbol, list(group))
                for symbol, group in groupby(sorted_bars, key=lambda b: b.symbol)
            ]

        for symbol, bar_list in items:
            try:
                if hasattr(bar_list, "df"):
                    df = bar_list.df.copy()
                else:
                    df = pd.DataFrame([{
                        "timestamp": b.timestamp,
                        "open": float(b.open),
                        "high": float(b.high),
                        "low": float(b.low),
                        "close": float(b.close),
                        "volume": float(b.volume),
                    } for b in bar_list])
                    df = df.set_index("timestamp")

                df = df.sort_index().tail(lookback_bars)
                result[symbol] = df
                logger.debug("Fetched %d bars for %s", len(df), symbol)
            except Exception as e:
                logger.warning("Could not process bars for %s: %s", symbol, e)

        return result

    def get_account(self) -> dict:
        """Returns account info including equity, buying power, cash."""
        account = self.trading_client.get_account()
        return {
            "equity": float(account.equity),
            "cash": float(account.cash),
            "buying_power": float(account.buying_power),
            "portfolio_value": float(account.portfolio_value),
            "currency": account.currency,
        }

    def get_positions(self) -> List[dict]:
        """Returns current open positions."""
        positions = self.trading_client.get_all_positions()
        return [
            {
                "symbol": p.symbol,
                "qty": float(p.qty),
                "avg_entry_price": float(p.avg_entry_price),
                "current_price": float(p.current_price),
                "market_value": float(p.market_value),
                "unrealized_pl": float(p.unrealized_pl),
                "unrealized_plpc": float(p.unrealized_plpc),
                "side": p.side.value,
            }
            for p in positions
        ]

    def get_latest_price(self, symbol: str) -> Optional[float]:
        """Returns latest trade price for a symbol."""
        from alpaca.data.requests import StockLatestTradeRequest
        try:
            req = StockLatestTradeRequest(symbol_or_symbols=[symbol])
            trade = self.data_client.get_stock_latest_trade(req)
            return float(trade[symbol].price)
        except Exception as e:
            logger.warning("Could not fetch latest price for %s: %s", symbol, e)
            return None