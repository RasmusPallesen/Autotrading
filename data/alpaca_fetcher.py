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
            from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
            from alpaca.trading.client import TradingClient
            self._StockBarsRequest = StockBarsRequest
            self._StockLatestTradeRequest = StockLatestTradeRequest
            self.data_client = StockHistoricalDataClient(
                config.api_key, config.secret_key
            )
            self.trading_client = TradingClient(
                config.api_key, config.secret_key, paper=config.paper
            )
            self._daily_atr_cache: Dict[str, tuple] = {}  # symbol -> (date, atr|None)
            logger.info("AlpacaDataFetcher initialised (paper=%s)", config.paper)
        except ImportError:
            raise ImportError("Install alpaca-py: pip install alpaca-py")

    def get_bars(
        self,
        symbols: List[str],
        lookback_bars: int = 50,
        timeframe: str = "1Min",
    ) -> Dict[str, pd.DataFrame]:
        """Fetch bars per symbol individually to work around IEX batch limitations."""
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        tf_map = {
            "1Min": TimeFrame.Minute,
            "5Min": TimeFrame(5, TimeFrameUnit.Minute),
            "15Min": TimeFrame(15, TimeFrameUnit.Minute),
            "1Hour": TimeFrame.Hour,
            "1Day": TimeFrame.Day,
        }
        tf = tf_map.get(timeframe, TimeFrame.Minute)

        # Size the lookback window by timeframe. The old hours-only heuristic
        # (~5 days) was far too short for daily/hourly bars — a request for 20
        # daily bars would silently return only ~4.
        end = datetime.now(timezone.utc)
        if timeframe == "1Day":
            # Calendar days must cover weekends/holidays to yield lookback_bars sessions.
            start = end - timedelta(days=lookback_bars * 2 + 15)
        elif timeframe == "1Hour":
            start = end - timedelta(days=max(lookback_bars // 6 + 5, 10))
        else:
            start = end - timedelta(hours=max(lookback_bars * 2, 120))

        result: Dict[str, pd.DataFrame] = {}

        for symbol in symbols:
            try:
                request = self._StockBarsRequest(
                    symbol_or_symbols=symbol,
                    timeframe=tf,
                    start=start,
                    end=end,
                    limit=lookback_bars,
                    feed="iex",
                )
                bars = self.data_client.get_stock_bars(request)
                data = bars.data if hasattr(bars, "data") else bars

                if not data:
                    logger.warning("No data returned for %s", symbol)
                    continue

                bar_list = data.get(symbol) if isinstance(data, dict) else data

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
                logger.warning("Could not fetch bars for %s: %s", symbol, e)

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
                "change_today": float(p.change_today or 0),
                "unrealized_intraday_plpc": float(p.unrealized_intraday_plpc or 0),
                "side": p.side.value,
            }
            for p in positions
        ]

    def get_latest_price(self, symbol: str) -> Optional[float]:
        """Returns latest trade price for a symbol."""
        try:
            req = self._StockLatestTradeRequest(
                symbol_or_symbols=[symbol],
                feed="iex",
            )
            trade = self.data_client.get_stock_latest_trade(req)
            return float(trade[symbol].price)
        except Exception as e:
            logger.warning("Could not fetch latest price for %s: %s", symbol, e)
            return None

    def get_premarket_move(self, symbol: str) -> Optional[float]:
        """
        Return the pre-market price move as a fraction vs previous close.
        Uses the Alpaca snapshot endpoint (prevDailyBar + latestTrade).
        Returns e.g. 1.12 for +112%, -0.05 for -5%, or None on failure.
        """
        try:
            from alpaca.data.requests import StockSnapshotRequest
            req = StockSnapshotRequest(symbol_or_symbols=[symbol], feed="iex")
            snaps = self.data_client.get_stock_snapshot(req)
            snap = snaps.get(symbol) if isinstance(snaps, dict) else snaps
            if snap is None:
                return None
            prev_close = float(snap.previous_daily_bar.close)
            latest = float(snap.latest_trade.price)
            if prev_close <= 0:
                return None
            return (latest - prev_close) / prev_close
        except Exception as e:
            logger.warning("Could not fetch pre-market move for %s: %s", symbol, e)
            return None

    def get_daily_atr(self, symbol: str, period: int = 14) -> Optional[float]:
        """
        Return the 14-period ATR computed on DAILY bars — the swing-trade
        volatility the risk manager's 2x/4x stop/target and position sizing were
        designed for. (Intraday 1-minute ATR is ~20x smaller and produces
        noise-level stops.) Cached once per symbol per UTC date. None on failure.
        """
        today = datetime.now(timezone.utc).date()
        cached = self._daily_atr_cache.get(symbol)
        if cached and cached[0] == today:
            return cached[1]

        atr_val: Optional[float] = None
        try:
            bars = self.get_bars([symbol], lookback_bars=period + 6, timeframe="1Day")
            df = bars.get(symbol)
            if df is not None and len(df) >= period + 1:
                high = df["high"].astype(float)
                low = df["low"].astype(float)
                close = df["close"].astype(float)
                prev_close = close.shift(1)
                tr = pd.concat([
                    high - low,
                    (high - prev_close).abs(),
                    (low - prev_close).abs(),
                ], axis=1).max(axis=1)
                atr_series = tr.rolling(window=period).mean()
                val = atr_series.iloc[-1]
                if val is not None and float(val) > 0:
                    atr_val = float(val)
        except Exception as e:
            logger.warning("Could not compute daily ATR for %s: %s", symbol, e)

        self._daily_atr_cache[symbol] = (today, atr_val)
        return atr_val