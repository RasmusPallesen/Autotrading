"""
Institutional filing monitor.
Tracks 13-F, 13-D, and 13-G SEC filings for watchlist symbols.

Caching strategy (mirrors collector.py filing_cache pattern):
- logs/institutional_cache.json  — seen accession numbers (never re-read)
- logs/institutional_13f.json    — previous quarter 13-F holdings per filer
                                   (used to delta-detect NEW positions only)

Cache rules:
- An accession number seen once is never re-fetched (filings are immutable)
- 13-F deltas are stored per filer per symbol — only new positions fire signals
- Cache survives restarts — no duplicate alerts across research cycles
- 13-F quarterly cache expires after 95 days (one full quarter + buffer)

Filing types monitored:
- 13-D  Activist investor crossing 5% with intent to influence (fires immediately)
- 13-G  Passive investor crossing 5% (fires immediately, lower urgency)
- 13-F  Quarterly institutional holdings (fires only for NEW positions vs prior quarter)
- SC13D/SC13G  Alternative form names for the same filings
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

EDGAR_HEADERS = {"User-Agent": "TradingAgent rasmus.pallesen@gmail.com"}

# ── Cache paths ────────────────────────────────────────────────────────────────
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOGS_DIR = os.path.join(_BASE_DIR, "logs")

_SEEN_CACHE_PATH = os.path.join(_LOGS_DIR, "institutional_cache.json")
_13F_DELTA_PATH  = os.path.join(_LOGS_DIR, "institutional_13f.json")

# How long to keep 13-F delta entries (one quarter + buffer)
_13F_CACHE_TTL_DAYS = 95


# ── Cache I/O ──────────────────────────────────────────────────────────────────

def _load_json(path: str) -> dict:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning("Could not load cache %s: %s", path, e)
    return {}


def _save_json(path: str, data: dict):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=None)
    except Exception as e:
        logger.warning("Could not save cache %s: %s", path, e)


# Load caches on module import — same pattern as collector.py
_SEEN_CACHE: dict = _load_json(_SEEN_CACHE_PATH)  # {accession: True}
_13F_CACHE:  dict = _load_json(_13F_DELTA_PATH)   # {filer_cik: {symbol: shares, cached_at}}

logger.debug(
    "Institutional monitor: %d seen accessions, %d 13-F filer records loaded",
    len(_SEEN_CACHE), len(_13F_CACHE),
)


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class InstitutionalSignal:
    symbol: str
    form_type: str          # "13-D" | "13-G" | "13-F"
    filer_name: str
    filer_cik: str
    ownership_pct: Optional[float]   # For 13-D/G
    shares_held: Optional[int]       # For 13-F
    market_value: Optional[float]    # For 13-F (USD)
    filing_date: str
    accession: str
    is_new_position: bool            # 13-F: True if not in prior quarter
    is_activist: bool                # 13-D = True, 13-G = False
    description: str                 # Parsed intent/purpose from filing
    url: str

    @property
    def urgency(self) -> str:
        """Signal urgency based on filing type and whether it's new."""
        if self.form_type == "13-D":
            return "HIGH"      # Activist — immediate price impact
        if self.form_type == "13-G" and self.is_new_position:
            return "MEDIUM"    # New passive large stake
        if self.form_type == "13-F" and self.is_new_position:
            return "MEDIUM"    # New institutional position
        return "LOW"

    def to_research_summary(self) -> str:
        lines = []

        if self.form_type == "13-D":
            lines += [
                f"ACTIVIST INVESTOR ALERT: {self.filer_name} has filed a 13-D on {self.symbol}.",
                f"Ownership: {self.ownership_pct:.1f}% of outstanding shares." if self.ownership_pct else "",
                f"Filing date: {self.filing_date}.",
                "13-D signals activist intent — the filer plans to engage management, "
                "push for strategic changes, board seats, buybacks, or a sale of the company. "
                "Historically one of the highest-conviction buy signals available from public data. "
                "Stock typically moves 5-20% on announcement and continues moving as the activist "
                "campaign develops. Monitor closely for follow-up 13-D/A amendments.",
            ]
            if self.description:
                lines.append(f"Stated purpose: {self.description[:300]}")

        elif self.form_type == "13-G":
            new_str = "NEW POSITION — " if self.is_new_position else ""
            lines += [
                f"INSTITUTIONAL STAKE: {new_str}{self.filer_name} holds "
                f"{self.ownership_pct:.1f}% of {self.symbol}." if self.ownership_pct else
                f"INSTITUTIONAL STAKE: {new_str}{self.filer_name} filed 13-G on {self.symbol}.",
                f"Filing date: {self.filing_date}.",
                "13-G indicates passive institutional ownership above 5%. "
                + ("This appears to be a new position — institution has recently crossed the 5% threshold. "
                   if self.is_new_position else
                   "Amended filing — institution has increased or decreased its position. "),
                "Large passive holders conduct extensive fundamental research before taking 5%+ stakes. "
                "Their presence validates the investment thesis.",
            ]

        elif self.form_type == "13-F":
            val_str = f"${self.market_value/1e6:.1f}M" if self.market_value else "undisclosed value"
            shr_str = f"{self.shares_held:,} shares" if self.shares_held else "undisclosed shares"
            lines += [
                f"NEW INSTITUTIONAL POSITION: {self.filer_name} added {self.symbol} "
                f"to their 13-F holdings — {shr_str} worth {val_str}.",
                f"Filing date: {self.filing_date} (covers prior quarter end).",
                "This is a NEW position — not held in the prior quarterly 13-F. "
                "Institutional 13-F additions indicate the fund has completed its analysis "
                "and is accumulating. Note: 13-F has a 45-day lag — institution may still "
                "be building the position.",
            ]

        return "\n".join(l for l in lines if l)


# ── EDGAR helpers ──────────────────────────────────────────────────────────────

def _edgar_search(
    query: str,
    form_types: List[str],
    date_range_days: int = 90,
) -> List[dict]:
    """
    Search EDGAR full-text search for filings mentioning watchlist symbols.
    Returns list of filing dicts.
    """
    today = datetime.now(timezone.utc).date()
    start = (today - timedelta(days=date_range_days)).isoformat()
    end   = today.isoformat()

    try:
        resp = requests.get(
            "https://efts.sec.gov/LATEST/search-index",
            params={
                "q": f'"{query}"',
                "dateRange": "custom",
                "startdt": start,
                "enddt": end,
                "forms": ",".join(form_types),
            },
            headers=EDGAR_HEADERS,
            timeout=12,
        )
        if resp.status_code != 200:
            return []
        hits = resp.json().get("hits", {}).get("hits", [])
        return hits
    except Exception as e:
        logger.debug("EDGAR search error for '%s': %s", query, e)
        return []


def _get_company_filings(
    cik: str,
    form_types: List[str],
    max_filings: int = 10,
) -> List[dict]:
    """
    Get recent filings of specific types for a CIK from EDGAR submissions API.
    More reliable than full-text search for known CIKs.
    """
    try:
        resp = requests.get(
            f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json",
            headers=EDGAR_HEADERS,
            timeout=10,
        )
        if resp.status_code != 200:
            return []

        data  = resp.json()
        recent = data.get("filings", {}).get("recent", {})
        forms  = recent.get("form", [])
        dates  = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        docs   = recent.get("primaryDocument", [])

        results = []
        for i, form in enumerate(forms):
            if form not in form_types:
                continue
            results.append({
                "form": form,
                "date": dates[i] if i < len(dates) else "",
                "accession": accessions[i].replace("-", "") if i < len(accessions) else "",
                "doc": docs[i] if i < len(docs) else "",
                "cik": cik,
            })
            if len(results) >= max_filings:
                break

        return results
    except Exception as e:
        logger.debug("EDGAR submissions error for CIK %s: %s", cik, e)
        return []


def _fetch_filing_text(cik: str, accession: str, doc: str, max_chars: int = 3000) -> str:
    """Fetch plain text from a filing document. Returns empty string on failure."""
    if not accession or not doc:
        return ""

    # Check seen cache — never re-read a filing we've already processed
    if accession in _SEEN_CACHE:
        return _SEEN_CACHE.get(f"{accession}_text", "")

    url = (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
        f"{accession}/{doc}"
    )
    try:
        resp = requests.get(url, headers=EDGAR_HEADERS, timeout=15)
        if resp.status_code != 200:
            return ""

        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style"]):
                tag.decompose()
            text = soup.get_text(separator=" ", strip=True)
        except ImportError:
            import re
            text = re.sub(r"<[^>]+>", " ", resp.text)
            text = re.sub(r"\s+", " ", text).strip()

        return text[:max_chars]
    except Exception as e:
        logger.debug("Filing text fetch error: %s", e)
        return ""


def _parse_ownership_pct(text: str) -> Optional[float]:
    """Extract ownership percentage from 13-D/G filing text."""
    import re
    patterns = [
        r'(\d+\.?\d*)\s*%\s*of\s*(?:the\s+)?(?:outstanding|issued)',
        r'aggregate\s+(?:of\s+)?(\d+\.?\d*)\s*%',
        r'(\d+\.?\d*)\s*percent',
        r'(\d+\.?\d*)\s*%\s*(?:of|ownership)',
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            try:
                pct = float(m.group(1))
                if 1.0 <= pct <= 100.0:
                    return pct
            except ValueError:
                pass
    return None


def _parse_intent(text: str) -> str:
    """Extract the stated purpose/intent from a 13-D filing."""
    import re
    # Item 4 of 13-D contains the purpose
    m = re.search(
        r'(?:Item\s*4|Purpose\s*of\s*Transaction)[:\s]+(.{100,600}?)(?:Item\s*5|\Z)',
        text, re.IGNORECASE | re.DOTALL
    )
    if m:
        raw = m.group(1).strip()
        return re.sub(r"\s+", " ", raw)[:400]
    return ""


# ── 13-F delta logic ───────────────────────────────────────────────────────────

def _is_new_13f_position(filer_cik: str, symbol: str, shares: int) -> bool:
    """
    Returns True if this symbol is a NEW position for this filer
    (not present in our cached prior-quarter holdings).
    Updates the cache with current holdings.
    """
    global _13F_CACHE

    filer_data = _13F_CACHE.get(filer_cik, {})

    # Evict stale entries (older than one quarter + buffer)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_13F_CACHE_TTL_DAYS)).isoformat()
    cached_at = filer_data.get("_cached_at", "1970-01-01")
    if cached_at < cutoff:
        filer_data = {}  # Stale — treat everything as new this cycle

    prior_symbols = set(k for k in filer_data if not k.startswith("_"))
    is_new = symbol not in prior_symbols

    # Update cache with this position
    filer_data[symbol] = shares
    filer_data["_cached_at"] = datetime.now(timezone.utc).isoformat()
    _13F_CACHE[filer_cik] = filer_data
    _save_json(_13F_DELTA_PATH, _13F_CACHE)

    return is_new


# ── Main monitor class ─────────────────────────────────────────────────────────

class InstitutionalMonitor:
    """
    Monitors 13-D, 13-G, and 13-F filings for watchlist symbols.

    Caching:
    - Seen accessions: never re-processed (disk-backed, survives restarts)
    - 13-F delta: prior quarter holdings per filer (disk-backed, 95-day TTL)
    - In-memory rate limiting: min 0.2s between EDGAR requests
    """

    # Well-known institutional filers to monitor for 13-F positions
    # These are funds that commonly hold positions in your watchlist sectors
    WATCHED_FILERS_13F = {
        "0001166559": "Citadel Advisors",
        "0001364742": "Blackrock",
        "0000102909": "Vanguard",
        "0000093751": "State Street",
        "0001061219": "Berkshire Hathaway",
        "0001336528": "Point72 Asset Management",
        "0001603466": "Millennium Management",
        "0001655050": "RA Capital Management",  # Biotech specialist
        "0001418814": "Baker Bros Advisors",    # Biotech specialist
        "0001293310": "Perceptive Advisors",    # Biotech specialist
        "0001418814": "Dragoneer Investment",
        "0001655050": "RTW Investments",        # Biotech/healthcare
    }

    def __init__(self):
        self._last_request_ts = 0.0

    def _rate_limit(self):
        """Enforce minimum 0.2s between EDGAR requests."""
        elapsed = time.time() - self._last_request_ts
        if elapsed < 0.2:
            time.sleep(0.2 - elapsed)
        self._last_request_ts = time.time()

    def get_signals(
        self,
        symbols: List[str],
        ticker_map: Dict[str, str],  # {symbol: cik}
        days_back: int = 90,
    ) -> List[InstitutionalSignal]:
        """
        Fetch all institutional signals for the given symbols.
        Returns list of InstitutionalSignal objects, skipping already-seen accessions.
        """
        global _SEEN_CACHE

        signals = []

        # 1. 13-D and 13-G — scan by symbol CIK
        signals += self._fetch_13dg(symbols, ticker_map, days_back)

        # 2. 13-F — scan watched filers for new positions in our symbols
        signals += self._fetch_13f_new_positions(symbols, ticker_map)

        # Deduplicate by accession
        seen = set()
        unique = []
        for s in signals:
            if s.accession not in seen:
                seen.add(s.accession)
                unique.append(s)

        if unique:
            logger.info(
                "Institutional monitor: %d new signals (%d 13-D/G, %d 13-F)",
                len(unique),
                sum(1 for s in unique if s.form_type in ("13-D","13-G")),
                sum(1 for s in unique if s.form_type == "13-F"),
            )

        return unique

    def _fetch_13dg(
        self,
        symbols: List[str],
        ticker_map: Dict[str, str],
        days_back: int,
    ) -> List[InstitutionalSignal]:
        """Fetch recent 13-D and 13-G filings for all symbols."""
        signals = []
        form_types = ["SC 13D", "SC 13G", "SC 13D/A", "SC 13G/A",
                      "13D", "13G", "13D/A", "13G/A"]

        for symbol in symbols:
            cik = ticker_map.get(symbol)
            if not cik:
                continue

            self._rate_limit()
            filings = _get_company_filings(
                cik=cik,
                form_types=form_types,
                max_filings=5,
            )

            for filing in filings:
                accession = filing["accession"]
                if not accession:
                    continue

                # Skip already-seen accessions — core cache check
                if accession in _SEEN_CACHE:
                    logger.debug(
                        "[%s] 13-D/G accession %s already seen -- skipping",
                        symbol, accession[:16],
                    )
                    continue

                # Check date range
                try:
                    filing_dt = datetime.fromisoformat(filing["date"])
                    if (datetime.now() - filing_dt).days > days_back:
                        continue
                except Exception:
                    pass

                # Fetch filing text for ownership % and intent
                self._rate_limit()
                text = _fetch_filing_text(cik, accession, filing["doc"])
                ownership_pct = _parse_ownership_pct(text)
                intent = _parse_intent(text) if "13-D" in filing["form"].upper() else ""

                is_activist = "13-D" in filing["form"].upper()
                form_clean  = "13-D" if is_activist else "13-G"

                # Try to get filer name from EDGAR
                filer_name = self._get_filer_name(cik, accession) or "Unknown Filer"

                url = (
                    f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                    f"{accession}/{filing['doc']}"
                )

                signal = InstitutionalSignal(
                    symbol=symbol,
                    form_type=form_clean,
                    filer_name=filer_name,
                    filer_cik=cik,
                    ownership_pct=ownership_pct,
                    shares_held=None,
                    market_value=None,
                    filing_date=filing["date"],
                    accession=accession,
                    is_new_position=True,  # 13-D/G always treated as significant
                    is_activist=is_activist,
                    description=intent,
                    url=url,
                )
                signals.append(signal)

                # Mark as seen — never process this accession again
                _SEEN_CACHE[accession] = True
                if text:
                    _SEEN_CACHE[f"{accession}_text"] = text[:500]
                _save_json(_SEEN_CACHE_PATH, _SEEN_CACHE)

                logger.info(
                    "[%s] %s: %s owns %.1f%% (filed %s)",
                    symbol, form_clean, filer_name,
                    ownership_pct or 0, filing["date"],
                )
                time.sleep(0.15)

        return signals

    def _fetch_13f_new_positions(
        self,
        symbols: List[str],
        ticker_map: Dict[str, str],
    ) -> List[InstitutionalSignal]:
        """
        Check watched institutional filers' 13-F holdings for new positions
        in our watchlist symbols.
        Only fires for NEW positions (not in prior quarter's 13-F).
        """
        signals = []
        symbol_set = set(symbols)

        for filer_cik, filer_name in self.WATCHED_FILERS_13F.items():
            self._rate_limit()
            filings = _get_company_filings(
                cik=filer_cik,
                form_types=["13F-HR", "13F-HR/A"],
                max_filings=2,  # Only most recent filing
            )

            for filing in filings:
                accession = filing["accession"]
                if not accession:
                    continue

                # Skip already-processed 13-F filings
                if accession in _SEEN_CACHE:
                    logger.debug(
                        "[13-F] %s accession %s already processed",
                        filer_name, accession[:16],
                    )
                    continue

                # Fetch and parse holdings
                self._rate_limit()
                holdings = self._parse_13f_holdings(filer_cik, accession, filing["doc"])

                if not holdings:
                    # Mark as seen even if empty to avoid re-fetching
                    _SEEN_CACHE[accession] = True
                    _save_json(_SEEN_CACHE_PATH, _SEEN_CACHE)
                    continue

                logger.info(
                    "[13-F] %s: parsed %d holdings from %s",
                    filer_name, len(holdings), accession[:16],
                )

                for symbol, (shares, mkt_val) in holdings.items():
                    if symbol not in symbol_set:
                        continue

                    cik = ticker_map.get(symbol, "")
                    is_new = _is_new_13f_position(filer_cik, symbol, shares)

                    if not is_new:
                        logger.debug(
                            "[13-F] %s already held %s last quarter -- skipping",
                            filer_name, symbol,
                        )
                        continue

                    url = (
                        f"https://www.sec.gov/cgi-bin/browse-edgar"
                        f"?action=getcompany&CIK={filer_cik}&type=13F"
                    )

                    signal = InstitutionalSignal(
                        symbol=symbol,
                        form_type="13-F",
                        filer_name=filer_name,
                        filer_cik=filer_cik,
                        ownership_pct=None,
                        shares_held=shares,
                        market_value=mkt_val,
                        filing_date=filing["date"],
                        accession=accession,
                        is_new_position=True,
                        is_activist=False,
                        description="",
                        url=url,
                    )
                    signals.append(signal)

                    logger.info(
                        "[13-F] NEW: %s added %s — %s shares ($%.1fM)",
                        filer_name, symbol, f"{shares:,}",
                        (mkt_val or 0) / 1e6,
                    )

                # Mark entire 13-F filing as seen
                _SEEN_CACHE[accession] = True
                _save_json(_SEEN_CACHE_PATH, _SEEN_CACHE)
                time.sleep(0.2)

        return signals

    def _parse_13f_holdings(
        self,
        cik: str,
        accession: str,
        doc: str,
    ) -> Dict[str, Tuple[int, float]]:
        """
        Parse 13-F holdings XML/HTML into {symbol: (shares, market_value)}.
        13-F filings include an infotable XML with all positions.
        """
        import re

        holdings: Dict[str, Tuple[int, float]] = {}

        try:
            # 13-F filings include an infotable document
            # Try fetching the main index to find the infotable
            index_url = (
                f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                f"{accession}/{accession[:10]}-{accession[10:12]}-{accession[12:]}-index.htm"
            )
            self._rate_limit()
            resp = requests.get(index_url, headers=EDGAR_HEADERS, timeout=10)

            # Find the infotable document link
            infotable_url = None
            if resp.status_code == 200:
                links = re.findall(r'href="([^"]*infotable[^"]*)"', resp.text, re.IGNORECASE)
                if not links:
                    links = re.findall(r'href="([^"]*\.xml)"', resp.text, re.IGNORECASE)
                if links:
                    href = links[0]
                    infotable_url = (
                        f"https://www.sec.gov{href}"
                        if href.startswith("/")
                        else f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}/{href}"
                    )

            if not infotable_url:
                return {}

            self._rate_limit()
            resp = requests.get(infotable_url, headers=EDGAR_HEADERS, timeout=15)
            if resp.status_code != 200:
                return {}

            content = resp.text

            # Parse XML infotable — look for nameOfIssuer, cusip, value, sshPrnamt
            # Handle both namespaced and non-namespaced XML
            entries = re.findall(
                r'<infoTable>(.*?)</infoTable>',
                content, re.DOTALL | re.IGNORECASE
            )
            if not entries:
                # Try alternative tag patterns
                entries = re.findall(
                    r'<ns1:infoTable>(.*?)</ns1:infoTable>',
                    content, re.DOTALL | re.IGNORECASE
                )

            for entry in entries:
                name_m  = re.search(r'<nameOfIssuer[^>]*>(.*?)</nameOfIssuer>', entry, re.IGNORECASE)
                val_m   = re.search(r'<value[^>]*>(\d+)</value>', entry, re.IGNORECASE)
                shr_m   = re.search(r'<sshPrnamt[^>]*>(\d+)</sshPrnamt>', entry, re.IGNORECASE)

                if not (name_m and val_m and shr_m):
                    continue

                name  = name_m.group(1).strip().upper()
                value = int(val_m.group(1)) * 1000  # 13-F values are in thousands
                shares = int(shr_m.group(1))

                # Map company name to ticker — approximate matching
                # We use the reverse ticker map for this
                matched_symbol = _match_name_to_symbol(name)
                if matched_symbol:
                    holdings[matched_symbol] = (shares, float(value))

        except Exception as e:
            logger.debug("13-F parse error for CIK %s: %s", cik, e)

        return holdings

    def _get_filer_name(self, target_cik: str, accession: str) -> Optional[str]:
        """Try to extract the filer name from the EDGAR accession header."""
        try:
            url = (
                f"https://www.sec.gov/Archives/edgar/data/{int(target_cik)}/"
                f"{accession}/{accession[:10]}-{accession[10:12]}-{accession[12:]}-index.htm"
            )
            self._rate_limit()
            resp = requests.get(url, headers=EDGAR_HEADERS, timeout=8)
            if resp.status_code == 200:
                import re
                m = re.search(
                    r'<div class="companyInfo"><span[^>]*>(.*?)</span>',
                    resp.text, re.IGNORECASE
                )
                if m:
                    return m.group(1).strip()[:100]
        except Exception:
            pass
        return None


# ── Name-to-ticker mapping ─────────────────────────────────────────────────────

# Approximate name fragments to tickers for 13-F parsing.
# 13-F uses company names not tickers — we match on key words.
_NAME_MAP = {
    "NVIDIA":          "NVDA",
    "ADVANCED MICRO":  "AMD",
    "INTEL":           "INTC",
    "BROADCOM":        "AVGO",
    "QUALCOMM":        "QCOM",
    "ARM HOLDINGS":    "ARM",
    "ASML":            "ASML",
    "TAIWAN SEMI":     "TSM",
    "MARVELL":         "MRVL",
    "APPLIED MATERIAL":"AMAT",
    "MICROSOFT":       "MSFT",
    "ALPHABET":        "GOOGL",
    "META PLATFORMS":  "META",
    "AMAZON":          "AMZN",
    "PALANTIR":        "PLTR",
    "C3.AI":           "AI",
    "SOUNDHOUND":      "SOUN",
    "BIGBEAR":         "BBAI",
    "ENPHASE":         "ENPH",
    "SOLAREDGE":       "SEDG",
    "FIRST SOLAR":     "FSLR",
    "NEXTERA":         "NEE",
    "PLUG POWER":      "PLUG",
    "BLOOM ENERGY":    "BE",
    "CHARGEPOINT":     "CHPT",
    "BLINK CHARGING":  "BLNK",
    "SUNRUN":          "RUN",
    "ARRAY TECHNOLOG": "ARRY",
    "NOVO NORDISK":    "NVO",
    "ELI LILLY":       "LLY",
    "DEXCOM":          "DXCM",
    "ABBOTT":          "ABT",
    "INTUITIVE SURG":  "ISRG",
    "INSULET":         "PODD",
    "TANDEM DIABETES": "TNDM",
    "MEDTRONIC":       "MDT",
    "INVACARE":        "INVA",
    "RHYTHM PHARMA":   "RYTM",
    "VERADERMICS":     "MANE",
    "RECURSION":       "RXRX",
    "BEAM THERAPEUT":  "BEAM",
    "CRISPR THERAP":   "CRSP",
    "INTELLIA":        "NTLA",
    "KRATOS":          "KTOS",
    "AEROVIRONMENT":   "AVAV",
    "RED CAT":         "RCAT",
    "NORTHROP":        "NOC",
    "LOCKHEED":        "LMT",
    "RAYTHEON":        "RTX",
    "AXON":            "AXON",
    "UNUSUAL MACHINE": "UMAC",
    "APPLE":           "AAPL",
    "TESLA":           "TSLA",
    "COINBASE":        "COIN",
    "MICROSTRATEGY":   "MSTR",
}


def _match_name_to_symbol(name: str) -> Optional[str]:
    """Match a company name from 13-F to a watchlist ticker."""
    name_upper = name.upper()
    for fragment, ticker in _NAME_MAP.items():
        if fragment in name_upper:
            return ticker
    return None


# ── Ticker-to-CIK resolver ─────────────────────────────────────────────────────

_TICKER_CIK_CACHE: Dict[str, str] = {}
_TICKER_CIK_LOADED = False


def get_ticker_cik_map(symbols: List[str]) -> Dict[str, str]:
    """
    Resolve ticker symbols to SEC CIK numbers.
    Cached in memory for the session — CIKs never change.
    """
    global _TICKER_CIK_CACHE, _TICKER_CIK_LOADED

    if _TICKER_CIK_LOADED:
        return {s: _TICKER_CIK_CACHE[s] for s in symbols if s in _TICKER_CIK_CACHE}

    try:
        resp = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers=EDGAR_HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        for v in resp.json().values():
            ticker = v["ticker"].upper()
            cik    = str(v["cik_str"]).zfill(10)
            _TICKER_CIK_CACHE[ticker] = cik
        _TICKER_CIK_LOADED = True
        logger.debug("Ticker→CIK map loaded: %d entries", len(_TICKER_CIK_CACHE))
    except Exception as e:
        logger.warning("Could not load ticker→CIK map: %s", e)

    return {s: _TICKER_CIK_CACHE[s] for s in symbols if s in _TICKER_CIK_CACHE}
