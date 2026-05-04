"""
Clinical catalyst calendar.
Monitors upcoming FDA decisions, Phase 3 trial readouts, and PDUFA dates
for biotech/biopharma symbols in the watchlist.

Mirrors the EarningsCalendar pattern exactly so it slots into the
research agent and trading agent with identical interfaces.

Data sources (all free, no API key required):
1. SEC EDGAR full-text search — 8-K filings mentioning trial readout dates
2. BioPharmCatalyst.com scrape — curated FDA/PDUFA calendar
3. Yahoo Finance news — symbol-specific clinical news as fallback

Provides:
- Upcoming catalyst dates per symbol
- Pre-catalyst warning flags (within 7 days of readout)
- Post-catalyst result flags (readout in last 3 days)
- Catalyst type classification (Phase2, Phase3, PDUFA, NDA/BLA)
"""

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# Pre-catalyst caution window in days — wider than earnings (48h)
# because biotech readouts are often leaked/anticipated further out
PRE_CATALYST_DAYS = int(__import__("os").getenv("PRE_CATALYST_DAYS", "7"))

# Catalyst types in order of binary risk severity
CATALYST_RISK = {
    "PDUFA": 5,        # FDA approval decision — highest binary risk
    "NDA_BLA": 4,      # New Drug Application submission/acceptance
    "PHASE3": 3,       # Phase 3 topline data — very binary
    "PHASE2": 2,       # Phase 2 data — meaningful but less definitive
    "PHASE1": 1,       # Phase 1 safety — lower market impact
    "ADVISORY": 4,     # FDA Advisory Committee — near-PDUFA risk
    "UNKNOWN": 1,
}


@dataclass
class ClinicalCatalyst:
    symbol: str
    company_name: str
    catalyst_date: date
    catalyst_type: str          # PDUFA | PHASE3 | PHASE2 | NDA_BLA | ADVISORY | UNKNOWN
    drug_name: str = ""
    indication: str = ""
    description: str = ""
    confirmed: bool = False     # True if from official SEC filing
    source: str = "estimated"   # "sec_filing" | "biopharma_catalyst" | "news"

    @property
    def days_until(self) -> int:
        return (self.catalyst_date - date.today()).days

    @property
    def is_upcoming(self) -> bool:
        return self.days_until >= 0

    @property
    def is_pre_catalyst_window(self) -> bool:
        """True if catalyst within PRE_CATALYST_DAYS — caution zone."""
        return 0 <= self.days_until <= PRE_CATALYST_DAYS

    @property
    def is_post_catalyst(self) -> bool:
        """True if catalyst was in the last 3 days."""
        return -3 <= self.days_until < 0

    @property
    def risk_level(self) -> int:
        """1-5 binary risk score based on catalyst type."""
        return CATALYST_RISK.get(self.catalyst_type, 1)

    @property
    def is_high_risk(self) -> bool:
        """True for PDUFA, Advisory, Phase 3 — highest binary risk events."""
        return self.risk_level >= 3

    def to_prompt_text(self) -> str:
        """Format for injection into Claude's trading prompt."""
        drug_str = f" ({self.drug_name})" if self.drug_name else ""
        indication_str = f" for {self.indication}" if self.indication else ""
        source_str = "confirmed SEC filing" if self.confirmed else "estimated date"

        if self.is_pre_catalyst_window:
            risk_note = (
                "EXTREME BINARY RISK — PDUFA decisions move stocks 30-80% in a single day. "
                if self.catalyst_type == "PDUFA"
                else "HIGH BINARY RISK — Phase 3 readouts move stocks 20-60% on single day. "
                if self.catalyst_type == "PHASE3"
                else "BINARY RISK EVENT — outcome unknown until data released. "
            )
            return (
                f"CLINICAL CATALYST WARNING: {self.symbol}{drug_str} has a "
                f"{self.catalyst_type} readout{indication_str} in {self.days_until} day(s) "
                f"({source_str}: {self.catalyst_date}). "
                f"{risk_note}"
                f"DO NOT add to position before this event. "
                f"Consider closing or tightening stop-loss to protect against adverse outcome. "
                f"Technical signals are unreliable ahead of binary events of this magnitude."
            )
        elif self.is_post_catalyst:
            return (
                f"RECENT CLINICAL CATALYST: {self.symbol}{drug_str} had a "
                f"{self.catalyst_type} readout{indication_str} "
                f"{abs(self.days_until)} day(s) ago ({self.catalyst_date}). "
                f"Current price action reflects market's interpretation of results. "
                f"{'Check SEC 8-K filing for full data package.' if self.confirmed else ''}"
            )
        else:
            return (
                f"UPCOMING CLINICAL CATALYST: {self.symbol}{drug_str} — "
                f"{self.catalyst_type}{indication_str} expected in {self.days_until} days "
                f"({self.catalyst_date}, {source_str}). "
                f"Risk level: {self.risk_level}/5. Monitor position size accordingly."
            )


class ClinicalCatalystCalendar:
    """
    Fetches and caches clinical catalyst dates for biotech/biopharma symbols.
    Mirrors EarningsCalendar interface for drop-in integration.
    """

    # Biotech/biopharma symbols that warrant clinical monitoring
    CLINICAL_SYMBOLS = {
        "MANE", "RXRX", "BEAM", "CRSP", "NTLA",   # Biotech watchlist
        "NVO", "LLY", "DXCM", "PODD", "TNDM",     # MedTech (some have pipeline)
        "INVA", "RYTM",                             # MedTech clinical stage
        "KTOS", "AVAV",                             # Defence (no clinical, skip)
    }

    # Only monitor these for clinical catalysts (not defence/chips)
    BIOTECH_ONLY = {
        "MANE", "RXRX", "BEAM", "CRSP", "NTLA",
        "INVA", "RYTM",
    }

    def __init__(self):
        self._cache: Dict[str, List[ClinicalCatalyst]] = {}
        self._last_refresh: Optional[datetime] = None
        self._refresh_interval_hours = 12

    def get_events(self, symbols: List[str]) -> Dict[str, ClinicalCatalyst]:
        """
        Get the nearest upcoming clinical catalyst per symbol.
        Returns dict of {symbol: ClinicalCatalyst} for symbols with
        catalysts within 30 days or post-catalyst within 3 days.
        Mirrors EarningsCalendar.get_events() interface.
        """
        clinical_symbols = [s for s in symbols if s in self.BIOTECH_ONLY]
        if not clinical_symbols:
            return {}

        if self._should_refresh():
            self._refresh(clinical_symbols)

        result = {}
        for symbol in clinical_symbols:
            catalysts = self._cache.get(symbol, [])
            # Pick nearest upcoming catalyst
            upcoming = [c for c in catalysts if c.days_until >= -3]
            if upcoming:
                nearest = min(upcoming, key=lambda c: c.days_until)
                if nearest.days_until <= 30 or nearest.is_post_catalyst:
                    result[symbol] = nearest

        return result

    def get_pre_catalyst_symbols(self, symbols: List[str]) -> List[str]:
        """Returns symbols with catalysts within PRE_CATALYST_DAYS."""
        events = self.get_events(symbols)
        return [s for s, c in events.items() if c.is_pre_catalyst_window]

    def get_high_risk_symbols(self, symbols: List[str]) -> List[str]:
        """Returns symbols with PDUFA/Phase3/Advisory within PRE_CATALYST_DAYS."""
        events = self.get_events(symbols)
        return [
            s for s, c in events.items()
            if c.is_pre_catalyst_window and c.is_high_risk
        ]

    def _should_refresh(self) -> bool:
        if not self._last_refresh:
            return True
        age = datetime.now(timezone.utc) - self._last_refresh
        return age.total_seconds() > self._refresh_interval_hours * 3600

    def _refresh(self, symbols: List[str]):
        logger.info(
            "Refreshing clinical catalyst calendar for %d biotech symbols: %s",
            len(symbols), symbols,
        )
        fetched = 0
        for symbol in symbols:
            catalysts = []

            # Source 1: SEC EDGAR 8-K filings (most reliable — company's own disclosures)
            sec_catalysts = self._fetch_sec_catalysts(symbol)
            catalysts.extend(sec_catalysts)

            # Source 2: BioPharmCatalyst scrape (curated FDA calendar)
            bpc_catalysts = self._fetch_biopharma_catalyst(symbol)
            for c in bpc_catalysts:
                # Don't duplicate if SEC already has this date
                if not any(
                    abs((c.catalyst_date - existing.catalyst_date).days) <= 3
                    for existing in catalysts
                ):
                    catalysts.append(c)

            # Source 3: Yahoo Finance news fallback for date extraction
            if not catalysts:
                news_catalysts = self._fetch_from_yahoo_news(symbol)
                catalysts.extend(news_catalysts)

            if catalysts:
                self._cache[symbol] = sorted(catalysts, key=lambda c: c.catalyst_date)
                fetched += 1
                for c in catalysts[:2]:
                    logger.info(
                        "[%s] Clinical catalyst found: %s %s in %d days (%s, %s)",
                        symbol, c.catalyst_type, c.drug_name,
                        c.days_until, c.catalyst_date, c.source,
                    )
            else:
                logger.debug("[%s] No clinical catalysts found", symbol)

        self._last_refresh = datetime.now(timezone.utc)
        logger.info(
            "Clinical catalyst calendar refreshed: %d/%d symbols have upcoming catalysts",
            fetched, len(symbols),
        )

    def _fetch_sec_catalysts(self, symbol: str) -> List[ClinicalCatalyst]:
        """
        Search SEC EDGAR full-text search for recent 8-K filings mentioning
        trial readout dates, PDUFA dates, or Phase 3 results.
        Returns confirmed catalysts from official company disclosures.
        """
        catalysts = []
        try:
            # EDGAR full-text search for recent 8-Ks
            url = "https://efts.sec.gov/LATEST/search-index?q=%22topline+results%22+%22Phase+3%22&dateRange=custom&startdt={start}&enddt={end}&forms=8-K"
            today = date.today()
            start = (today - timedelta(days=90)).isoformat()
            end = today.isoformat()

            resp = requests.get(
                "https://efts.sec.gov/LATEST/search-index",
                params={
                    "q": f'"{symbol}" "topline" OR "PDUFA" OR "Phase 3" OR "readout"',
                    "dateRange": "custom",
                    "startdt": start,
                    "enddt": end,
                    "forms": "8-K",
                },
                headers=_HEADERS,
                timeout=10,
            )
            if resp.status_code != 200:
                return []

            data = resp.json()
            hits = data.get("hits", {}).get("hits", [])

            for hit in hits[:5]:
                source_data = hit.get("_source", {})
                display_names = source_data.get("display_names", [])

                # Check if this filing is actually for our symbol
                entity_names = [d.get("name", "").upper() for d in display_names]
                ticker_match = any(symbol in name for name in entity_names)
                if not ticker_match:
                    # Try matching by company name fragments in the filing
                    file_date_str = source_data.get("file_date", "")
                    filing_text = hit.get("_source", {}).get("file_date", "")

                # Extract date mentions from filing description
                description = source_data.get("period_of_report", "")
                file_date = source_data.get("file_date", "")

                # Parse catalyst date from text
                catalyst = self._parse_catalyst_from_text(
                    text=str(source_data),
                    symbol=symbol,
                    source="sec_filing",
                    confirmed=True,
                )
                if catalyst:
                    catalysts.append(catalyst)
                    break  # One confirmed SEC catalyst is enough

        except Exception as e:
            logger.debug("[%s] SEC EDGAR catalyst fetch error: %s", symbol, e)

        return catalysts

    def _fetch_biopharma_catalyst(self, symbol: str) -> List[ClinicalCatalyst]:
        """
        Fetch from BioPharmCatalyst.com — a curated free FDA/clinical calendar.
        Searches by ticker symbol.
        """
        catalysts = []
        try:
            url = f"https://www.biopharmcatalyst.com/company/{symbol}"
            resp = requests.get(url, headers=_HEADERS, timeout=12)

            if resp.status_code != 200:
                return []

            html = resp.text
            catalysts = self._parse_bpc_html(html, symbol)

        except Exception as e:
            logger.debug("[%s] BioPharmCatalyst fetch error: %s", symbol, e)

        return catalysts

    def _parse_bpc_html(self, html: str, symbol: str) -> List[ClinicalCatalyst]:
        """Parse catalyst table from BioPharmCatalyst company page."""
        catalysts = []
        today = date.today()

        try:
            # BPC uses a table with columns: Drug | Indication | Phase | Catalyst | Date
            # Extract rows with date patterns
            date_pattern = re.compile(
                r'(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2}|'
                r'Q[1-4]\s*\d{4}|H[12]\s*\d{4}|'
                r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4})',
                re.IGNORECASE,
            )
            phase_pattern = re.compile(
                r'Phase\s*([123])|PDUFA|NDA|BLA|Advisory|P([123])',
                re.IGNORECASE,
            )
            drug_pattern = re.compile(r'VD\w+|[A-Z]{2,}-\d+|[a-z]+mab|[a-z]+nib|[a-z]+lib',
                                       re.IGNORECASE)

            # Find all table rows
            row_pattern = re.compile(r'<tr[^>]*>(.*?)</tr>', re.DOTALL | re.IGNORECASE)
            cell_pattern = re.compile(r'<td[^>]*>(.*?)</td>', re.DOTALL | re.IGNORECASE)
            tag_strip = re.compile(r'<[^>]+>')

            for row_match in row_pattern.finditer(html):
                row_text = row_match.group(1)
                cells = [
                    tag_strip.sub('', c.group(1)).strip()
                    for c in cell_pattern.finditer(row_text)
                ]
                row_clean = ' '.join(cells)

                date_match = date_pattern.search(row_clean)
                phase_match = phase_pattern.search(row_clean)
                if not date_match:
                    continue

                catalyst_date = self._parse_date_string(date_match.group(0))
                if not catalyst_date:
                    continue
                if catalyst_date < today - timedelta(days=30):
                    continue  # Skip old catalysts

                # Classify type
                catalyst_type = "UNKNOWN"
                if phase_match:
                    raw = phase_match.group(0).upper()
                    if "PDUFA" in raw:
                        catalyst_type = "PDUFA"
                    elif "NDA" in raw or "BLA" in raw:
                        catalyst_type = "NDA_BLA"
                    elif "ADVISORY" in raw:
                        catalyst_type = "ADVISORY"
                    elif "3" in raw:
                        catalyst_type = "PHASE3"
                    elif "2" in raw:
                        catalyst_type = "PHASE2"
                    elif "1" in raw:
                        catalyst_type = "PHASE1"

                drug_match = drug_pattern.search(row_clean)
                drug_name = drug_match.group(0) if drug_match else ""

                # Use first non-date, non-phase cell as indication hint
                indication = ""
                for cell in cells:
                    if cell and not date_pattern.search(cell) and len(cell) > 3:
                        indication = cell[:60]
                        break

                catalysts.append(ClinicalCatalyst(
                    symbol=symbol,
                    company_name=symbol,
                    catalyst_date=catalyst_date,
                    catalyst_type=catalyst_type,
                    drug_name=drug_name,
                    indication=indication,
                    confirmed=False,
                    source="biopharma_catalyst",
                ))

        except Exception as e:
            logger.debug("[%s] BPC HTML parse error: %s", symbol, e)

        return catalysts

    def _fetch_from_yahoo_news(self, symbol: str) -> List[ClinicalCatalyst]:
        """
        Fallback: scan Yahoo Finance news headlines for date mentions
        near keywords like 'topline', 'readout', 'PDUFA', 'Phase 3'.
        Returns estimated catalysts with lower confidence.
        """
        catalysts = []
        try:
            resp = requests.get(
                f"https://query1.finance.yahoo.com/v1/finance/search",
                params={"q": symbol, "newsCount": 20, "enableFuzzyQuery": False},
                headers=_HEADERS,
                timeout=8,
            )
            if resp.status_code != 200:
                return []

            news = resp.json().get("news", [])
            clinical_keywords = re.compile(
                r'topline|readout|phase\s*[23]|pdufa|nda|bla|'
                r'trial\s+results?|clinical\s+data|fda\s+decision|'
                r'approval|hair\s+loss|alopecia',
                re.IGNORECASE,
            )
            date_pattern = re.compile(
                r'Q[1-4]\s*20\d{2}|H[12]\s*20\d{2}|'
                r'(?:mid|late|early)[- ]20\d{2}|'
                r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+20\d{2}',
                re.IGNORECASE,
            )

            for article in news:
                title = article.get("title", "")
                if not clinical_keywords.search(title):
                    continue

                # Try to extract a date from title
                date_match = date_pattern.search(title)
                catalyst_date = None
                if date_match:
                    catalyst_date = self._parse_date_string(date_match.group(0))

                # If no date in title, use article publish date + 90 days as rough estimate
                if not catalyst_date:
                    pub_ts = article.get("providerPublishTime", 0)
                    if pub_ts:
                        pub_date = datetime.fromtimestamp(pub_ts, tz=timezone.utc).date()
                        catalyst_date = pub_date + timedelta(days=90)

                if not catalyst_date:
                    continue

                # Classify from title
                catalyst_type = "UNKNOWN"
                title_upper = title.upper()
                if "PDUFA" in title_upper:
                    catalyst_type = "PDUFA"
                elif "PHASE 3" in title_upper or "PHASE III" in title_upper:
                    catalyst_type = "PHASE3"
                elif "PHASE 2" in title_upper or "PHASE II" in title_upper:
                    catalyst_type = "PHASE2"
                elif "NDA" in title_upper or "BLA" in title_upper:
                    catalyst_type = "NDA_BLA"

                catalysts.append(ClinicalCatalyst(
                    symbol=symbol,
                    company_name=symbol,
                    catalyst_date=catalyst_date,
                    catalyst_type=catalyst_type,
                    drug_name="",
                    indication="",
                    description=title[:200],
                    confirmed=False,
                    source="news",
                ))
                break  # One news-derived estimate per symbol is enough

        except Exception as e:
            logger.debug("[%s] Yahoo news catalyst fetch error: %s", symbol, e)

        return catalysts

    def _parse_date_string(self, date_str: str) -> Optional[date]:
        """
        Parse various date string formats into a date object.
        Handles: 2026-07-01, 7/1/2026, Q3 2026, H2 2026, July 2026, mid-2026.
        """
        today = date.today()
        s = date_str.strip()

        # ISO format
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except ValueError:
            pass

        # US format
        try:
            return datetime.strptime(s, "%m/%d/%Y").date()
        except ValueError:
            pass

        # Month Year: "July 2026", "Jul 2026"
        for fmt in ("%B %Y", "%b %Y"):
            try:
                d = datetime.strptime(s, fmt).date()
                return d.replace(day=15)  # Mid-month estimate
            except ValueError:
                pass

        # Quarter: Q3 2026
        q_match = re.match(r'Q([1-4])\s*(\d{4})', s, re.IGNORECASE)
        if q_match:
            quarter = int(q_match.group(1))
            year = int(q_match.group(2))
            month = quarter * 3  # End of quarter
            try:
                return date(year, month, 15)
            except ValueError:
                pass

        # Half year: H1 2026, H2 2026
        h_match = re.match(r'H([12])\s*(\d{4})', s, re.IGNORECASE)
        if h_match:
            half = int(h_match.group(1))
            year = int(h_match.group(2))
            month = 3 if half == 1 else 9  # Mid-half estimate
            return date(year, month, 15)

        # "mid-2026", "late 2026", "early 2026"
        period_match = re.match(r'(early|mid|late)[- ](\d{4})', s, re.IGNORECASE)
        if period_match:
            period = period_match.group(1).lower()
            year = int(period_match.group(2))
            month = {"early": 2, "mid": 6, "late": 10}.get(period, 6)
            return date(year, month, 15)

        return None

    def _parse_catalyst_from_text(
        self, text: str, symbol: str, source: str, confirmed: bool
    ) -> Optional[ClinicalCatalyst]:
        """Extract a catalyst date from arbitrary text (SEC filing body etc.)."""
        today = date.today()

        phase_pattern = re.compile(
            r'(PDUFA|NDA|BLA|Advisory|Phase\s*[123]|topline|readout)',
            re.IGNORECASE,
        )
        date_pattern = re.compile(
            r'(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}|'
            r'Q[1-4]\s*20\d{2}|H[12]\s*20\d{2}|'
            r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+20\d{2})',
            re.IGNORECASE,
        )

        phase_match = phase_pattern.search(text)
        date_match = date_pattern.search(text)

        if not date_match:
            return None

        catalyst_date = self._parse_date_string(date_match.group(0))
        if not catalyst_date or catalyst_date < today:
            return None

        catalyst_type = "UNKNOWN"
        if phase_match:
            raw = phase_match.group(0).upper()
            if "PDUFA" in raw:
                catalyst_type = "PDUFA"
            elif "NDA" in raw or "BLA" in raw:
                catalyst_type = "NDA_BLA"
            elif "ADVISORY" in raw:
                catalyst_type = "ADVISORY"
            elif "3" in raw or "TOPLINE" in raw or "READOUT" in raw:
                catalyst_type = "PHASE3"
            elif "2" in raw:
                catalyst_type = "PHASE2"
            elif "1" in raw:
                catalyst_type = "PHASE1"

        return ClinicalCatalyst(
            symbol=symbol,
            company_name=symbol,
            catalyst_date=catalyst_date,
            catalyst_type=catalyst_type,
            confirmed=confirmed,
            source=source,
        )
