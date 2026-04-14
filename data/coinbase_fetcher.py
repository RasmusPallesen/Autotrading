"""
Coinbase Advanced Trade data fetcher.
Retrieves candles and product info from Coinbase's REST API.
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

COINBASE_BASE = "https://api.coinbase.com/api/v3/brokerage"
COINBASE_SANDBOX = "https://api-public.sandbox.coinbase.com/api/v3/brokerage"

GRANULARITY_MAP = {
    "1Min": "ONE_MINUTE",
    "5Min": "FIVE_MINUTE",
    "15Min": "FIFTEEN_MINUTE",
    "1Hour": "ONE_HOUR",
    "1Day": "ONE_DAY",
}


class CoinbaseDataFetcher:
    """Fetches crypto market data from Coinbase Advanced Trade API."""

    def __init__(self, config):
        self.api_key = config.api_key
        self.api_secret = config.api_secret
        self.base_url = COINBASE_SANDBOX if config.sandbox else COINBASE_BASE
        logger.info("CoinbaseDataFetcher initialised (sandbox=%s)", config.sandbox)

    def _auth_headers(self, method: str, path: str, body: str = "") -> dict:
        """Generate JWT-based auth headers for Coinbase Advanced Trade API."""
        import hashlib
        import hmac

        timestamp = str(int(time.time()))
        message = f"{timestamp}{method}{path}{body}"
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            message.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).hexdigest()
        return {
            "CB-ACCESS-KEY": self.api_key,
            "CB-ACCESS-SIGN": signature,
            "CB-ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json",
        }

    def get_candles(
        self,
        product_ids: List[str],
        lookback_bars: int = 50,
        granularity: str = "1Min",
    ) -> Dict[str, pd.DataFrame]:
        """
        Returns dict of {product_id: DataFrame} with OHLCV columns.
        product_ids should be in Coinbase format e.g. 'BTC-USD'.
        """
        gran = GRANULARITY_MAP.get(granularity, "ONE_MINUTE")
        end = int(datetime.now(timezone.utc).timestamp())
        # Seconds per bar
        seconds = {"ONE_MINUTE": 60, "FIVE_MINUTE": 300, "FIFTEEN_MINUTE": 900,
                   "ONE_HOUR": 3600, "ONE_DAY": 86400}.get(gran, 60)
        start = end - (lookback_bars * seconds * 2)

        result: Dict[str, pd.DataFrame] = {}

        for product_id in product_ids:
            path = f"/products/{product_id}/candles"
            params = {
                "start": str(start),
                "end": str(end),
                "granularity": gran,
            }
            try:
                url = f"{self.base_url}{path}"
                headers = self._auth_headers("GET", f"/api/v3/brokerage{path}")
                resp = requests.get(url, headers=headers, params=params, timeout=10)
                resp.raise_for_status()
                candles = resp.json().get("candles", [])

                if not candles:
                    logger.warning("No candles returned for %s", product_id)
                    continue

                df = pd.DataFrame(candles, columns=["start", "low", "high", "open", "close", "volume"])
                df["timestamp"] = pd.to_datetime(df["start"].astype(int), unit="s", utc=True)
                df = df.set_index("timestamp").sort_index()
                df = df[["open", "high", "low", "close", "volume"]].astype(float)
                df = df.tail(lookback_bars)
                result[product_id] = df
                logger.debug("Fetched %d candles for %s", len(df), product_id)

            except Exception as e:
                logger.warning("Could not fetch candles for %s: %s", product_id, e)

        return result

    def get_latest_price(self, product_id: str) -> Optional[float]:
        """Returns latest best ask price for a product."""
        path = f"/best_bid_ask"
        try:
            url = f"{self.base_url}{path}"
            headers = self._auth_headers("GET", f"/api/v3/brokerage{path}")
            resp = requests.get(url, headers=headers, params={"product_ids": product_id}, timeout=10)
            resp.raise_for_status()
            pricebooks = resp.json().get("pricebooks", [])
            if pricebooks:
                return float(pricebooks[0].get("asks", [{}])[0].get("price", 0))
        except Exception as e:
            logger.warning("Could not fetch price for %s: %s", product_id, e)
        return None
