"""
Massive.com Technical Indicators fetcher.
Fetches exchange-grade SMA, EMA, MACD, and RSI from Massive's free tier.

Endpoints (all free tier - Stocks Basic plan):
  GET /v1/indicators/sma/{ticker}
  GET /v1/indicators/ema/{ticker}
  GET /v1/indicators/macd/{ticker}
  GET /v1/indicators/rsi/{ticker}

Free tier provides end-of-day data with 2 years of history.
These values cross-validate the locally computed pandas-ta indicators.
"""

import logging
import os
from dataclasses import dataclass
from typing import Optional

import time
import requests

logger = logging.getLogger(__name__)

MASSIVE_BASE = "https://api.massive.com/v1/indicators"


@dataclass
class MassiveIndicators:
    symbol: str
    sma_20: Optional[float] = None
    sma_50: Optional[float] = None
    ema_9: Optional[float] = None
    ema_21: Optional[float] = None
    macd_value: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_histogram: Optional[float] = None
    rsi_14: Optional[float] = None

    def to_summary(self) -> str:
        """Format as concise text for AI prompt injection."""
        parts = [f"Massive.com indicators for {self.symbol} (end-of-day):"]
        if self.rsi_14 is not None:
            parts.append(f"  RSI(14): {self.rsi_14:.1f}")
        if self.ema_9 is not None and self.ema_21 is not None:
            cross = "BULLISH" if self.ema_9 > self.ema_21 else "BEARISH"
            parts.append(f"  EMA9: {self.ema_9:.2f} | EMA21: {self.ema_21:.2f} | Cross: {cross}")
        if self.sma_20 is not None and self.sma_50 is not None:
            trend = "ABOVE" if self.sma_20 > self.sma_50 else "BELOW"
            parts.append(f"  SMA20: {self.sma_20:.2f} | SMA50: {self.sma_50:.2f} | SMA20 {trend} SMA50")
        if self.macd_value is not None:
            parts.append(
                f"  MACD: {self.macd_value:.4f} | Signal: {self.macd_signal:.4f} | Hist: {self.macd_histogram:.4f}"
            )
        return "\n".join(parts)

    def conflicts_with_local(self, local_rsi: Optional[float], local_ema9: Optional[float]) -> bool:
        """
        Returns True if Massive indicators significantly disagree with local calculations.
        Useful for flagging data quality issues.
        """
        if self.rsi_14 and local_rsi:
            if abs(self.rsi_14 - local_rsi) > 15:
                return True
        if self.ema_9 and local_ema9:
            if abs(self.ema_9 - local_ema9) / local_ema9 > 0.05:
                return True
        return False


class MassiveIndicatorFetcher:
    """
    Fetches technical indicators from Massive.com free tier API.
    Uses end-of-day data — good for daily trend confirmation.
    """

    def __init__(self, api_key: str = ""):
        self.api_key = api_key or os.getenv("MASSIVE_API_KEY", "")
        if not self.api_key:
            logger.warning("MASSIVE_API_KEY not set — MassiveIndicatorFetcher disabled")

    def _get(self, endpoint: str, ticker: str, params: dict) -> Optional[dict]:
        """Make a GET request to a Massive indicator endpoint."""
        if not self.api_key:
            return None
        try:
            url = f"{MASSIVE_BASE}/{endpoint}/{ticker}"
            params["apiKey"] = self.api_key
            params.setdefault("timespan", "day")
            params.setdefault("series_type", "close")
            params.setdefault("order", "desc")
            params.setdefault("limit", 1)

            resp = requests.get(url, params=params, timeout=8)
            resp.raise_for_status()
            data = resp.json()

            values = data.get("results", {}).get("values", [])
            return values[0] if values else None

        except requests.HTTPError as e:
            if e.response.status_code == 403:
                logger.debug("Massive indicator %s/%s: plan does not cover this endpoint", endpoint, ticker)
            elif e.response.status_code == 404:
                logger.debug("Massive indicator %s/%s: ticker not found", endpoint, ticker)
            elif e.response.status_code == 429:
                logger.warning("Massive rate limit hit for %s/%s -- sleeping 5s", endpoint, ticker)
                time.sleep(5)
            else:
                logger.warning("Massive indicator %s/%s HTTP error: %s", endpoint, ticker, e)
        except Exception as e:
            logger.debug("Massive indicator %s/%s error: %s", endpoint, ticker, e)
        finally:
            time.sleep(1.5)  # Conservative delay for free tier
        return None

    def fetch_sma(self, ticker: str, window: int = 20) -> Optional[float]:
        """Fetch Simple Moving Average."""
        result = self._get("sma", ticker, {"window": window})
        return float(result["value"]) if result and "value" in result else None

    def fetch_ema(self, ticker: str, window: int = 9) -> Optional[float]:
        """Fetch Exponential Moving Average."""
        result = self._get("ema", ticker, {"window": window})
        return float(result["value"]) if result and "value" in result else None

    def fetch_macd(self, ticker: str) -> tuple:
        """
        Fetch MACD value, signal, and histogram.
        Returns (macd, signal, histogram) or (None, None, None).
        """
        result = self._get("macd", ticker, {
            "short_window": 12,
            "long_window": 26,
            "signal_window": 9,
        })
        if result:
            return (
                float(result.get("value", 0) or 0),
                float(result.get("signal", 0) or 0),
                float(result.get("histogram", 0) or 0),
            )
        return None, None, None

    def fetch_rsi(self, ticker: str, window: int = 14) -> Optional[float]:
        """Fetch Relative Strength Index."""
        result = self._get("rsi", ticker, {"window": window})
        return float(result["value"]) if result and "value" in result else None

    def fetch_all(self, ticker: str) -> MassiveIndicators:
        """
        Fetch key indicators for a ticker.
        Limited to RSI and EMA9 only to respect free tier rate limits.
        2 API calls per symbol instead of 6.
        """
        indicators = MassiveIndicators(symbol=ticker)

        # RSI — most valuable single indicator
        indicators.rsi_14 = self.fetch_rsi(ticker, window=14)

        # EMA9 — trend direction confirmation
        indicators.ema_9 = self.fetch_ema(ticker, window=9)

        fetched = sum(1 for v in [indicators.rsi_14, indicators.ema_9] if v is not None)
        logger.debug("Massive indicators for %s: %d/2 fetched", ticker, fetched)
        return indicators
