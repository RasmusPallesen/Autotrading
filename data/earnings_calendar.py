"""
Earnings calendar monitor.
Fetches upcoming and recent earnings dates for watchlist symbols.
Uses Yahoo Finance (free, no API key required).

Provides:
- Upcoming earnings dates per symbol
- Pre-earnings warning flags (within 48h of report)
- Post-earnings results (EPS beat/miss, revenue surprise)
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


@dataclass
class EarningsEvent:
    symbol: str
    company_name: str
    earnings_date: date
    confirmed: bool
    eps_estimate: Optional[float] = None
    eps_actual: Optional[float] = None
    eps_surprise_pct: Optional[float] = None
    revenue_estimate: Optional[float] = None
    revenue_actual: Optional[float] = None

    @property
    def days_until(self) -> int:
        return (self.earnings_date - date.today()).days

    @property
    def is_upcoming(self) -> bool:
        return self.days_until >= 0

    @property
    def is_pre_earnings_window(self) -> bool:
        """True if earnings within next 48 hours — caution zone."""
        return 0 <= self.days_until <= 2

    @property
    def is_post_earnings(self) -> bool:
        """True if earnings was in the last 2 days."""
        return -2 <= self.days_until < 0

    @property
    def beat_miss(self) -> Optional[str]:
        """Returns BEAT, MISS, or IN-LINE based on EPS surprise."""
        if self.eps_surprise_pct is None:
            return None
        if self.eps_surprise_pct > 3:
            return "BEAT"
        elif self.eps_surprise_pct < -3:
            return "MISS"
        return "IN-LINE"

    def to_prompt_text(self) -> str:
        """Format for injection into Claude's trading prompt."""
        if self.is_pre_earnings_window:
            return (
                f"EARNINGS WARNING: {self.symbol} reports earnings in {self.days_until} day(s) "
                f"({'confirmed' if self.confirmed else 'estimated'} date: {self.earnings_date}). "
                f"EPS estimate: {'$'+str(self.eps_estimate) if self.eps_estimate else 'N/A'}. "
                f"CAUTION: Avoid adding to position before earnings — binary risk event. "
                f"Consider reducing position size or tightening stop-loss."
            )
        elif self.is_post_earnings and self.beat_miss:
            return (
                f"EARNINGS RESULT: {self.symbol} reported {self.days_until * -1} day(s) ago. "
                f"EPS {self.beat_miss}: actual=${self.eps_actual} vs estimate=${self.eps_estimate} "
                f"(surprise: {self.eps_surprise_pct:+.1f}%). "
                f"{'Strong buy signal on beat.' if self.beat_miss == 'BEAT' else 'Caution — earnings miss may continue to weigh.'}"
            )
        else:
            return (
                f"UPCOMING EARNINGS: {self.symbol} reports in {self.days_until} days "
                f"({self.earnings_date}). Plan accordingly."
            )


class EarningsCalendar:
    """Fetches and caches earnings dates for watchlist symbols."""

    def __init__(self):
        self._cache: Dict[str, EarningsEvent] = {}
        self._last_refresh: Optional[datetime] = None
        self._refresh_interval_hours = 12  # Refresh twice daily

    def get_events(self, symbols: List[str]) -> Dict[str, EarningsEvent]:
        """
        Get earnings events for symbols.
        Returns dict of {symbol: EarningsEvent} for symbols with upcoming
        or recent earnings only.
        """
        # Refresh cache if stale
        if self._should_refresh():
            self._refresh(symbols)

        return {
            sym: event
            for sym, event in self._cache.items()
            if sym in symbols and (event.is_pre_earnings_window or
                                   event.is_post_earnings or
                                   event.days_until <= 7)
        }

    def get_pre_earnings_symbols(self, symbols: List[str]) -> List[str]:
        """Returns symbols with earnings in the next 48 hours."""
        events = self.get_events(symbols)
        return [sym for sym, ev in events.items() if ev.is_pre_earnings_window]

    def get_post_earnings_symbols(self, symbols: List[str]) -> List[str]:
        """Returns symbols that reported earnings in the last 2 days."""
        events = self.get_events(symbols)
        return [sym for sym, ev in events.items() if ev.is_post_earnings]

    def _should_refresh(self) -> bool:
        if not self._last_refresh:
            return True
        age = datetime.now(timezone.utc) - self._last_refresh
        return age.total_seconds() > self._refresh_interval_hours * 3600

    def _refresh(self, symbols: List[str]):
        """Fetch earnings data for all symbols from Yahoo Finance."""
        logger.info("Refreshing earnings calendar for %d symbols", len(symbols))
        fetched = 0

        for symbol in symbols:
            event = self._fetch_yahoo_earnings(symbol)
            if event:
                self._cache[symbol] = event
                fetched += 1

        self._last_refresh = datetime.now(timezone.utc)
        logger.info(
            "Earnings calendar refreshed: %d/%d symbols have upcoming/recent earnings",
            fetched, len(symbols),
        )

    def _fetch_yahoo_earnings(self, symbol: str) -> Optional[EarningsEvent]:
        """Fetch earnings data for a single symbol from Yahoo Finance."""
        try:
            resp = requests.get(
                f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}",
                params={"modules": "calendarEvents,earnings"},
                headers=YAHOO_HEADERS,
                timeout=8,
            )
            resp.raise_for_status()
            data = resp.json().get("quoteSummary", {}).get("result", [{}])[0]

            # Upcoming earnings date
            calendar = data.get("calendarEvents", {})
            earnings_dates = calendar.get("earnings", {}).get("earningsDate", [])

            earnings_date = None
            confirmed = False
            if earnings_dates:
                ts = earnings_dates[0].get("raw", 0)
                if ts:
                    earnings_date = datetime.fromtimestamp(ts, tz=timezone.utc).date()
                    # Consider confirmed if within 30 days and second date within 5 days
                    if len(earnings_dates) > 1:
                        ts2 = earnings_dates[1].get("raw", 0)
                        d2 = datetime.fromtimestamp(ts2, tz=timezone.utc).date()
                        confirmed = abs((d2 - earnings_date).days) <= 5

            if not earnings_date:
                return None

            # EPS estimates and actuals from earnings history
            earnings_hist = data.get("earnings", {})
            quarterly = earnings_hist.get("earningsChart", {}).get("quarterly", [])

            eps_estimate = None
            eps_actual = None
            eps_surprise_pct = None
            rev_estimate = None
            rev_actual = None

            # Get most recent quarter data
            if quarterly:
                last = quarterly[-1]
                eps_actual = last.get("actual", {}).get("raw")
                eps_estimate = last.get("estimate", {}).get("raw")
                if eps_actual is not None and eps_estimate and eps_estimate != 0:
                    eps_surprise_pct = ((eps_actual - eps_estimate) / abs(eps_estimate)) * 100

            # Revenue from financials
            fin_data = earnings_hist.get("financialsChart", {})
            quarterly_fin = fin_data.get("quarterly", [])
            if quarterly_fin:
                last_fin = quarterly_fin[-1]
                rev_actual = last_fin.get("revenue", {}).get("raw")

            return EarningsEvent(
                symbol=symbol,
                company_name=symbol,
                earnings_date=earnings_date,
                confirmed=confirmed,
                eps_estimate=eps_estimate,
                eps_actual=eps_actual,
                eps_surprise_pct=eps_surprise_pct,
                revenue_estimate=rev_estimate,
                revenue_actual=rev_actual,
            )

        except Exception as e:
            logger.debug("Could not fetch earnings for %s: %s", symbol, e)
            return None
