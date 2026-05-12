"""
Earnings calendar monitor.
Primary source: Financial Modeling Prep (FMP) API.
Fallback: SEC EDGAR submissions data.
Yahoo Finance removed — rate-limited from server IPs.

FMP advantages over Yahoo:
- One API call fetches ALL symbols' earnings dates for a date range
- No IP-based rate limiting
- EPS estimates, actuals, and surprise data included
- Reliable from cloud server IPs (Hetzner, AWS etc.)

Setup:
1. Sign up at financialmodelingprep.com (free tier: 250 calls/day)
2. Add to .env_trading: FMP_API_KEY=your_key_here
3. Free tier is sufficient — we make ~2 calls per day total

Provides:
- Upcoming earnings dates per symbol
- Pre-earnings warning flags (within 48h of report)
- Post-earnings results (EPS beat/miss, revenue surprise)
- Strong beat/miss detection for cache invalidation
"""

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

FMP_API_KEY = os.getenv("FMP_API_KEY", "")
FMP_BASE    = "https://financialmodelingprep.com/api/v3"
EDGAR_HEADERS = {"User-Agent": "TradingAgent rasmus.pallesen@gmail.com"}

# How many days ahead to fetch earnings for
EARNINGS_LOOKAHEAD_DAYS = 30
# How many days back to check for recent earnings (post-earnings window)
EARNINGS_LOOKBACK_DAYS  = 7
# Strong beat/miss threshold for cache invalidation
STRONG_BEAT_THRESHOLD   = float(os.getenv("STRONG_BEAT_THRESHOLD_PCT", "10"))


@dataclass
class EarningsEvent:
    symbol: str
    company_name: str
    earnings_date: date
    confirmed: bool
    eps_estimate: Optional[float] = None
    eps_actual:   Optional[float] = None
    eps_surprise_pct: Optional[float] = None
    revenue_estimate: Optional[float] = None
    revenue_actual:   Optional[float] = None

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
        """True if earnings was in the last 7 days."""
        return -7 <= self.days_until < 0

    @property
    def beat_miss(self) -> Optional[str]:
        if self.eps_surprise_pct is None:
            return None
        if self.eps_surprise_pct > 3:
            return "BEAT"
        elif self.eps_surprise_pct < -3:
            return "MISS"
        return "IN-LINE"

    @property
    def is_strong_beat(self) -> bool:
        """True if post-earnings EPS beat >= STRONG_BEAT_THRESHOLD."""
        if not self.is_post_earnings or self.eps_surprise_pct is None:
            return False
        return self.eps_surprise_pct >= STRONG_BEAT_THRESHOLD

    @property
    def is_strong_miss(self) -> bool:
        """True if post-earnings EPS miss <= -STRONG_BEAT_THRESHOLD."""
        if not self.is_post_earnings or self.eps_surprise_pct is None:
            return False
        return self.eps_surprise_pct <= -STRONG_BEAT_THRESHOLD

    def to_prompt_text(self) -> str:
        if self.is_pre_earnings_window:
            return (
                f"EARNINGS WARNING: {self.symbol} reports earnings in "
                f"{self.days_until} day(s) "
                f"({'confirmed' if self.confirmed else 'estimated'} date: "
                f"{self.earnings_date}). "
                f"EPS estimate: "
                f"{'$'+str(self.eps_estimate) if self.eps_estimate else 'N/A'}. "
                f"CAUTION: Avoid adding to position before earnings — binary risk event. "
                f"Consider reducing position size or tightening stop-loss."
            )
        elif self.is_post_earnings and self.beat_miss:
            return (
                f"EARNINGS RESULT: {self.symbol} reported "
                f"{abs(self.days_until)} day(s) ago. "
                f"EPS {self.beat_miss}: actual=${self.eps_actual} vs "
                f"estimate=${self.eps_estimate} "
                f"(surprise: {self.eps_surprise_pct:+.1f}%). "
                f"{'Strong buy signal on beat.' if self.beat_miss == 'BEAT' else 'Caution — earnings miss may continue to weigh.'}"
            )
        else:
            return (
                f"UPCOMING EARNINGS: {self.symbol} reports in "
                f"{self.days_until} days ({self.earnings_date}). Plan accordingly."
            )


class EarningsCalendar:
    """
    Fetches and caches earnings dates for watchlist symbols.
    Primary: FMP API (one bulk call per day for all symbols).
    Fallback: SEC EDGAR (estimated dates from filing history).
    """

    def __init__(self):
        self._cache: Dict[str, EarningsEvent] = {}
        self._last_refresh: Optional[datetime] = None
        self._refresh_interval_hours = 12

        if not FMP_API_KEY:
            logger.warning(
                "FMP_API_KEY not set — earnings calendar will use EDGAR estimates only. "
                "Sign up free at financialmodelingprep.com and add FMP_API_KEY to .env_trading"
            )

    # ── Public interface (unchanged from original) ─────────────────────────────

    def get_events(self, symbols: List[str]) -> Dict[str, EarningsEvent]:
        if self._should_refresh():
            self._refresh(symbols)
        return {
            sym: event
            for sym, event in self._cache.items()
            if sym in symbols and (
                event.is_pre_earnings_window or
                event.is_post_earnings or
                event.days_until <= 7 or
                event.is_strong_beat or
                event.is_strong_miss
            )
        }

    def get_pre_earnings_symbols(self, symbols: List[str]) -> List[str]:
        return [s for s, ev in self.get_events(symbols).items()
                if ev.is_pre_earnings_window]

    def get_post_earnings_symbols(self, symbols: List[str]) -> List[str]:
        return [s for s, ev in self.get_events(symbols).items()
                if ev.is_post_earnings]

    def get_strong_beat_symbols(self, symbols: List[str]) -> List[str]:
        return [s for s, ev in self.get_events(symbols).items()
                if ev.is_strong_beat]

    def get_strong_miss_symbols(self, symbols: List[str]) -> List[str]:
        return [s for s, ev in self.get_events(symbols).items()
                if ev.is_strong_miss]

    # ── Refresh logic ──────────────────────────────────────────────────────────

    def _should_refresh(self) -> bool:
        if not self._last_refresh:
            return True
        age = datetime.now(timezone.utc) - self._last_refresh
        return age.total_seconds() > self._refresh_interval_hours * 3600

    def _refresh(self, symbols: List[str]):
        logger.info(
            "Refreshing earnings calendar for %d symbols via %s",
            len(symbols),
            "FMP" if FMP_API_KEY else "EDGAR",
        )
        fetched = 0

        if FMP_API_KEY:
            # FMP: one bulk call covers all symbols for the date range
            fmp_events = self._fetch_fmp_bulk(symbols)
            for sym, event in fmp_events.items():
                self._cache[sym] = event
                fetched += 1

            # For symbols not covered by FMP, fall back to EDGAR
            missing = [s for s in symbols if s not in self._cache]
            if missing:
                logger.debug(
                    "FMP missing %d symbols — trying EDGAR: %s",
                    len(missing), missing[:5]
                )
                for sym in missing:
                    event = self._fetch_edgar_earnings(sym)
                    if event:
                        self._cache[sym] = event
                        fetched += 1
        else:
            # No FMP key — use EDGAR for all symbols
            for sym in symbols:
                event = self._fetch_edgar_earnings(sym)
                if event:
                    self._cache[sym] = event
                    fetched += 1

        self._last_refresh = datetime.now(timezone.utc)
        logger.info(
            "Earnings calendar refreshed: %d/%d symbols have upcoming/recent earnings",
            fetched, len(symbols),
        )

    # ── FMP bulk fetch (replaces 55 Yahoo calls with 2 FMP calls) ─────────────

    def _fetch_fmp_bulk(self, symbols: List[str]) -> Dict[str, EarningsEvent]:
        """
        Fetch earnings for all symbols in ONE API call using FMP's date-range endpoint.
        Covers both upcoming (next 30 days) and recent (last 7 days) earnings.
        Costs 2 API calls per refresh (upcoming + recent) regardless of watchlist size.
        """
        events: Dict[str, EarningsEvent] = {}
        symbol_set = {s.upper() for s in symbols}
        today = date.today()

        # Call 1: upcoming earnings (next 30 days)
        upcoming = self._fmp_earnings_range(
            from_date=today,
            to_date=today + timedelta(days=EARNINGS_LOOKAHEAD_DAYS),
        )
        for item in upcoming:
            sym = item.get("symbol", "").upper()
            if sym not in symbol_set:
                continue
            event = self._parse_fmp_item(item)
            if event:
                events[sym] = event

        # Call 2: recent earnings (last 7 days) for beat/miss detection
        recent = self._fmp_earnings_range(
            from_date=today - timedelta(days=EARNINGS_LOOKBACK_DAYS),
            to_date=today - timedelta(days=1),
        )
        for item in recent:
            sym = item.get("symbol", "").upper()
            if sym not in symbol_set:
                continue
            # Only add if not already covered by upcoming (prefer upcoming)
            if sym not in events:
                event = self._parse_fmp_item(item)
                if event:
                    events[sym] = event

        logger.info(
            "FMP earnings: %d symbols with events in -%dd to +%dd window",
            len(events), EARNINGS_LOOKBACK_DAYS, EARNINGS_LOOKAHEAD_DAYS,
        )
        return events

    def _fmp_earnings_range(
        self, from_date: date, to_date: date
    ) -> List[dict]:
        """Fetch FMP earnings calendar for a date range."""
        try:
            resp = requests.get(
                f"{FMP_BASE}/earnings-calendar",
                params={
                    "from":   from_date.isoformat(),
                    "to":     to_date.isoformat(),
                    "apikey": FMP_API_KEY,
                },
                timeout=12,
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    return data
                # FMP returns error dict on bad key
                if isinstance(data, dict) and "Error Message" in data:
                    logger.error("FMP API error: %s", data["Error Message"])
            elif resp.status_code == 403:
                logger.error(
                    "FMP API key invalid or plan limit reached (403). "
                    "Check FMP_API_KEY in .env_trading."
                )
            else:
                logger.warning("FMP earnings API HTTP %d", resp.status_code)
        except Exception as e:
            logger.warning("FMP earnings fetch error: %s", e)
        return []

    def _parse_fmp_item(self, item: dict) -> Optional[EarningsEvent]:
        """Parse a single FMP earnings calendar item into an EarningsEvent."""
        try:
            sym          = item.get("symbol", "").upper()
            date_str     = item.get("date", "")
            eps_est      = item.get("epsEstimated")
            eps_act      = item.get("eps")
            rev_est      = item.get("revenueEstimated")
            rev_act      = item.get("revenue")
            time_of_day  = item.get("time", "")  # "bmo" (before open) or "amc" (after close)

            if not (sym and date_str):
                return None

            earnings_date = datetime.fromisoformat(date_str).date()

            # Compute EPS surprise
            eps_surprise_pct = None
            if eps_act is not None and eps_est and eps_est != 0:
                try:
                    eps_surprise_pct = (
                        (float(eps_act) - float(eps_est)) / abs(float(eps_est))
                    ) * 100
                except (TypeError, ZeroDivisionError):
                    pass

            return EarningsEvent(
                symbol=sym,
                company_name=item.get("name", sym),
                earnings_date=earnings_date,
                confirmed=True,   # FMP data is confirmed from company filings
                eps_estimate=float(eps_est) if eps_est is not None else None,
                eps_actual=float(eps_act) if eps_act is not None else None,
                eps_surprise_pct=eps_surprise_pct,
                revenue_estimate=float(rev_est) if rev_est is not None else None,
                revenue_actual=float(rev_act) if rev_act is not None else None,
            )
        except Exception as e:
            logger.debug("FMP item parse error: %s", e)
            return None

    # ── EDGAR fallback (no API key needed, estimated dates) ───────────────────

    def _fetch_edgar_earnings(self, symbol: str) -> Optional[EarningsEvent]:
        """
        Estimate next earnings date from SEC EDGAR filing history.
        Uses 10-Q/10-K report dates + 91 days as estimate.
        No API key required, no rate limits, works from any IP.
        Confirmed=False since dates are estimated.
        """
        try:
            # Get CIK for symbol
            tickers_resp = requests.get(
                "https://www.sec.gov/files/company_tickers.json",
                headers=EDGAR_HEADERS,
                timeout=8,
            )
            if tickers_resp.status_code != 200:
                return None

            ticker_map = {
                v["ticker"].upper(): str(v["cik_str"]).zfill(10)
                for v in tickers_resp.json().values()
            }
            cik = ticker_map.get(symbol.upper())
            if not cik:
                return None

            # Get filing history
            sub_resp = requests.get(
                f"https://data.sec.gov/submissions/CIK{cik}.json",
                headers=EDGAR_HEADERS,
                timeout=8,
            )
            if sub_resp.status_code != 200:
                return None

            data = sub_resp.json()
            recent = data.get("filings", {}).get("recent", {})
            forms        = recent.get("form", [])
            report_dates = recent.get("reportDate", [])

            # Find most recent 10-Q or 10-K report date
            last_report_date = None
            for i, form in enumerate(forms[:20]):
                if form in ("10-Q", "10-K"):
                    try:
                        d_str = report_dates[i] if i < len(report_dates) else ""
                        if d_str:
                            d = datetime.fromisoformat(d_str).date()
                            if last_report_date is None or d > last_report_date:
                                last_report_date = d
                    except Exception:
                        pass

            if not last_report_date:
                return None

            # Estimate next earnings ~91 days after last report
            estimated_next = last_report_date + timedelta(days=91)
            today = date.today()
            days_diff = (estimated_next - today).days

            # Only return if the estimated date is relevant
            if days_diff < -EARNINGS_LOOKBACK_DAYS or days_diff > EARNINGS_LOOKAHEAD_DAYS:
                return None

            logger.debug(
                "[%s] EDGAR estimated earnings: %s (%+d days)",
                symbol, estimated_next, days_diff,
            )
            return EarningsEvent(
                symbol=symbol,
                company_name=data.get("name", symbol),
                earnings_date=estimated_next,
                confirmed=False,
                eps_estimate=None,
                eps_actual=None,
                eps_surprise_pct=None,
            )
        except Exception as e:
            logger.debug("EDGAR earnings fetch failed for %s: %s", symbol, e)
            return None
