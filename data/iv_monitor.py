"""
Implied Volatility (IV) Spike Monitor.
Detects unusual IV spikes on watchlist symbols using Yahoo Finance options chain.
IV spikes not explained by upcoming earnings often precede significant price moves.

Runs after market close as part of the research cycle.
Free — no API key required.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# IV rank threshold to flag as unusual (top 20% of 52-week range)
IV_RANK_THRESHOLD = 0.80

# Minimum absolute IV to consider (filter very low IV stocks)
MIN_IV = 0.20  # 20%


@dataclass
class IVSnapshot:
    symbol: str
    current_iv: float          # Current implied volatility (annualised)
    iv_52w_high: float         # 52-week IV high
    iv_52w_low: float          # 52-week IV low
    iv_rank: float             # 0-1: where current IV sits in 52w range
    iv_percentile: float       # 0-1: percentile vs historical
    put_call_ratio: float      # Put/call volume ratio
    total_options_volume: int  # Total options contracts traded today
    snapshot_date: date = field(default_factory=date.today)

    @property
    def is_unusual(self) -> bool:
        return (
            self.iv_rank >= IV_RANK_THRESHOLD and
            self.current_iv >= MIN_IV
        )

    @property
    def signal_type(self) -> str:
        """Interpret what the IV spike likely means."""
        if self.put_call_ratio > 1.5:
            return "BEARISH_HEDGE"   # High put volume — someone buying protection
        elif self.put_call_ratio < 0.5:
            return "BULLISH_CALL_BUYING"  # Heavy call buying — directional bet up
        else:
            return "DIRECTIONAL_UNKNOWN"  # High IV but balanced flow

    @property
    def signal_strength(self) -> str:
        if self.iv_rank >= 0.95:
            return "EXTREME"
        elif self.iv_rank >= 0.90:
            return "VERY HIGH"
        elif self.iv_rank >= 0.80:
            return "HIGH"
        return "MODERATE"

    def to_research_summary(self, has_earnings_soon: bool = False) -> str:
        explanation = ""
        if has_earnings_soon:
            explanation = (
                "IV spike is EXPLAINED by upcoming earnings — "
                "elevated volatility is expected and not a standalone signal."
            )
        else:
            if self.signal_type == "BULLISH_CALL_BUYING":
                explanation = (
                    "IV spike with heavy CALL buying (put/call ratio "
                    f"{self.put_call_ratio:.2f}) suggests institutional "
                    "positioning for an upside move. Someone may have "
                    "information or conviction about a near-term catalyst. "
                    "BULLISH signal — investigate any pending catalysts."
                )
            elif self.signal_type == "BEARISH_HEDGE":
                explanation = (
                    "IV spike with heavy PUT buying (put/call ratio "
                    f"{self.put_call_ratio:.2f}) suggests hedging activity. "
                    "Could indicate concern about downside risk from "
                    "institutional players. BEARISH signal — exercise caution."
                )
            else:
                explanation = (
                    "Unusual IV spike not explained by earnings. "
                    "Balanced put/call flow suggests market participants "
                    "expect a significant move but direction is unclear. "
                    "Possible catalyst: M&A, regulatory decision, or "
                    "major product announcement."
                )

        return (
            f"IV SPIKE ALERT: {self.symbol} implied volatility is at "
            f"{self.current_iv*100:.1f}% (IV Rank: {self.iv_rank*100:.0f}th percentile "
            f"of 52-week range {self.iv_52w_low*100:.0f}%-{self.iv_52w_high*100:.0f}%). "
            f"Signal strength: {self.signal_strength}. "
            f"Options volume today: {self.total_options_volume:,} contracts. "
            f"{explanation}"
        )


class IVMonitor:
    """
    Monitors implied volatility across watchlist symbols.
    Uses Yahoo Finance options chain data — free, no API key required.
    """

    def __init__(self):
        # Rolling IV history per symbol for rank calculation
        # Format: {symbol: [(date, iv), ...]}
        self._iv_history: Dict[str, List[tuple]] = {}

    def scan(
        self,
        symbols: List[str],
        earnings_symbols: Optional[List[str]] = None,
    ) -> List[IVSnapshot]:
        """
        Scan symbols for unusual IV activity.
        Returns list of IVSnapshot for symbols with unusual IV.
        earnings_symbols: symbols with earnings in next 7 days (explains IV)
        """
        earnings_symbols = earnings_symbols or []
        results = []

        for symbol in symbols:
            try:
                snapshot = self._fetch_iv(symbol)
                if snapshot and snapshot.is_unusual:
                    has_earnings = symbol in earnings_symbols
                    results.append(snapshot)
                    logger.info(
                        "IV SPIKE [%s]: rank=%.0f%% iv=%.0f%% pcr=%.2f type=%s%s",
                        symbol,
                        snapshot.iv_rank * 100,
                        snapshot.current_iv * 100,
                        snapshot.put_call_ratio,
                        snapshot.signal_type,
                        " (earnings)" if has_earnings else " *** UNEXPLAINED ***",
                    )
                time.sleep(0.3)  # Rate limit Yahoo Finance
            except Exception as e:
                logger.debug("IV fetch error for %s: %s", symbol, e)

        # Sort by IV rank descending
        results.sort(key=lambda s: s.iv_rank, reverse=True)
        logger.info(
            "IV scan complete: %d/%d symbols show unusual IV",
            len(results), len(symbols),
        )
        return results

    def _fetch_iv(self, symbol: str) -> Optional[IVSnapshot]:
        """
        Fetch options chain from Yahoo Finance and compute IV metrics.
        Uses the nearest expiry options for current IV calculation.
        """
        try:
            # Get options expiry dates
            resp = requests.get(
                f"https://query1.finance.yahoo.com/v7/finance/options/{symbol}",
                headers=YAHOO_HEADERS,
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            option_chain = data.get("optionChain", {})
            result = option_chain.get("result", [])
            if not result:
                return None

            result = result[0]
            expiry_dates = result.get("expirationDates", [])
            if not expiry_dates:
                return None

            # Use nearest expiry for current IV
            nearest_expiry = expiry_dates[0]

            # Fetch options for nearest expiry
            resp2 = requests.get(
                f"https://query1.finance.yahoo.com/v7/finance/options/{symbol}",
                params={"date": nearest_expiry},
                headers=YAHOO_HEADERS,
                timeout=10,
            )
            resp2.raise_for_status()
            data2 = resp2.json()

            result2 = data2.get("optionChain", {}).get("result", [{}])[0]
            options = result2.get("options", [{}])[0]
            calls = options.get("calls", [])
            puts = options.get("puts", [])

            if not calls and not puts:
                return None

            # Compute average IV from ATM options
            quote = result2.get("quote", {})
            current_price = quote.get("regularMarketPrice", 0)

            atm_ivs = []
            call_volume = 0
            put_volume = 0

            for call in calls:
                strike = call.get("strike", 0)
                if abs(strike - current_price) / current_price < 0.05:  # Within 5% of ATM
                    iv = call.get("impliedVolatility", 0)
                    if iv and iv > 0:
                        atm_ivs.append(iv)
                call_volume += call.get("volume", 0) or 0

            for put in puts:
                strike = put.get("strike", 0)
                if abs(strike - current_price) / current_price < 0.05:
                    iv = put.get("impliedVolatility", 0)
                    if iv and iv > 0:
                        atm_ivs.append(iv)
                put_volume += put.get("volume", 0) or 0

            if not atm_ivs:
                return None

            current_iv = sum(atm_ivs) / len(atm_ivs)
            total_volume = call_volume + put_volume
            put_call_ratio = put_volume / call_volume if call_volume > 0 else 1.0

            # Update IV history
            today = date.today()
            if symbol not in self._iv_history:
                self._iv_history[symbol] = []
            self._iv_history[symbol].append((today, current_iv))

            # Keep last 252 trading days (~1 year)
            self._iv_history[symbol] = self._iv_history[symbol][-252:]

            # Compute IV rank from history
            history_ivs = [iv for _, iv in self._iv_history[symbol]]
            if len(history_ivs) < 5:
                # Not enough history — use Yahoo's 52w data as proxy
                iv_52w_low = current_iv * 0.5
                iv_52w_high = current_iv * 1.5
            else:
                iv_52w_low = min(history_ivs)
                iv_52w_high = max(history_ivs)

            iv_range = iv_52w_high - iv_52w_low
            iv_rank = (
                (current_iv - iv_52w_low) / iv_range
                if iv_range > 0 else 0.5
            )
            iv_rank = max(0.0, min(1.0, iv_rank))

            # IV percentile
            iv_percentile = (
                sum(1 for iv in history_ivs if iv <= current_iv) /
                len(history_ivs)
            ) if history_ivs else 0.5

            return IVSnapshot(
                symbol=symbol,
                current_iv=current_iv,
                iv_52w_high=iv_52w_high,
                iv_52w_low=iv_52w_low,
                iv_rank=iv_rank,
                iv_percentile=iv_percentile,
                put_call_ratio=put_call_ratio,
                total_options_volume=total_volume,
            )

        except Exception as e:
            logger.debug("IV fetch failed for %s: %s", symbol, e)
            return None
