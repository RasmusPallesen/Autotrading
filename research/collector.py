"""
Data collector: fetches news, SEC filings (with actual content), and Reddit posts.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

import requests

logger = logging.getLogger(__name__)


def _log_cache_status():
    """Log cache status once after logger is ready."""
    if _FILING_CACHE:
        logger.info("SEC filing cache: %d entries loaded from disk", len(_FILING_CACHE))

# Disk-backed cache for SEC filing content
# 8-K filings don't change once filed — persist across restarts
import json as _json
import os as _os

_CACHE_PATH = _os.path.join(
    _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
    "logs", "filing_cache.json"
)


def _load_cache() -> dict:
    """Load filing cache from disk."""
    try:
        if _os.path.exists(_CACHE_PATH):
            with open(_CACHE_PATH, "r", encoding="utf-8") as f:
                return _json.load(f)
    except Exception:
        pass
    return {}


def _save_cache(cache: dict):
    """Save filing cache to disk."""
    try:
        _os.makedirs(_os.path.dirname(_CACHE_PATH), exist_ok=True)
        with open(_CACHE_PATH, "w", encoding="utf-8") as f:
            _json.dump(cache, f, ensure_ascii=False)
    except Exception:
        pass


_FILING_CACHE: dict = _load_cache()

EDGAR_HEADERS = {"User-Agent": "TradingAgent rasmus.pallesen@gmail.com"}


@dataclass
class ResearchItem:
    source: str        # "news" | "sec" | "reddit" | "scanner"
    symbol: str
    title: str
    summary: str
    url: str
    published_at: datetime
    raw: dict


# ── News stub — returns empty (SEC filings handle primary research) ────────────

def fetch_news(symbols: List[str], api_key: str) -> List[ResearchItem]:
    """
    News fetching placeholder.
    Benzinga via Massive requires a paid plan ($99/mo).
    Primary research is handled by SEC 8-K filing content reading.
    Upgrade to Massive Benzinga plan to enable full article fetching.
    """
    if api_key:
        logger.info("News API key set but Benzinga endpoint requires paid Massive plan -- skipping")
    else:
        logger.debug("No news API key set -- skipping news fetch")
    return []


# ── SEC EDGAR filings with content fetching ────────────────────────────────────

def _fetch_filing_content(url: str, max_chars: int = 5000) -> str:
    """
    Fetch and extract plain text from an SEC filing HTML page.
    Returns empty string on failure.
    """
    try:
        resp = requests.get(url, headers=EDGAR_HEADERS, timeout=15)
        resp.raise_for_status()

        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "html.parser")

            # Remove script and style tags
            for tag in soup(["script", "style", "header", "footer", "nav"]):
                tag.decompose()

            text = soup.get_text(separator=" ", strip=True)
        except ImportError:
            # Fallback: basic tag stripping without BeautifulSoup
            import re
            text = re.sub(r"<[^>]+>", " ", resp.text)
            text = re.sub(r"\s+", " ", text).strip()

        # Clean up excessive whitespace
        import re
        text = re.sub(r"\s+", " ", text).strip()

        # Return first max_chars characters of meaningful content
        return text[:max_chars] if text else ""

    except Exception as e:
        logger.debug("Could not fetch filing content from %s: %s", url, e)
        return ""


def _get_filing_index_url(cik: str, accession: str) -> Optional[str]:
    """
    Get the index page URL for a filing to find the main document.
    """
    accession_dashed = f"{accession[:10]}-{accession[10:12]}-{accession[12:]}"
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}/{accession_dashed}-index.htm"


def _find_main_document(cik: str, accession: str, primary_doc: str) -> str:
    """
    Try to find the best URL for the main filing document.
    Prefers .htm files over .txt files.
    """
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}"

    # Try primary document first
    if primary_doc:
        url = f"{base}/{primary_doc}"
        try:
            resp = requests.head(url, headers=EDGAR_HEADERS, timeout=5)
            if resp.status_code == 200:
                return url
        except Exception:
            pass

    # Try fetching the index to find the main document
    try:
        index_url = f"{base}/{accession[:10]}-{accession[10:12]}-{accession[12:]}-index.htm"
        resp = requests.get(index_url, headers=EDGAR_HEADERS, timeout=10)
        resp.raise_for_status()

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")

        # Find links to .htm documents
        for link in soup.find_all("a", href=True):
            href = link["href"]
            if href.endswith(".htm") and "index" not in href.lower():
                return f"https://www.sec.gov{href}" if href.startswith("/") else f"{base}/{href}"
    except Exception:
        pass

    return f"{base}/{primary_doc}" if primary_doc else ""


def fetch_sec_filings(symbols: List[str]) -> List[ResearchItem]:
    """
    Fetch recent SEC filings for watchlist symbols.
    Reads actual filing content for 8-K and important filings.
    """
    _log_cache_status()
    items = []

    # Resolve tickers to CIKs
    try:
        tickers_resp = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers=EDGAR_HEADERS,
            timeout=10,
        )
        tickers_resp.raise_for_status()
        ticker_map = {
            v["ticker"].upper(): str(v["cik_str"]).zfill(10)
            for v in tickers_resp.json().values()
        }
    except Exception as e:
        logger.error("Could not fetch SEC ticker map: %s", e)
        return []

    for symbol in symbols:
        cik = ticker_map.get(symbol.upper())
        if not cik:
            logger.debug("No CIK found for %s", symbol)
            continue

        try:
            filings_resp = requests.get(
                f"https://data.sec.gov/submissions/CIK{cik}.json",
                headers=EDGAR_HEADERS,
                timeout=10,
            )
            filings_resp.raise_for_status()
            data = filings_resp.json()
            recent = data.get("filings", {}).get("recent", {})

            forms = recent.get("form", [])
            dates = recent.get("filingDate", [])
            accessions = recent.get("accessionNumber", [])
            descriptions = recent.get("primaryDocument", [])

            filings_processed = 0

            for i, form in enumerate(forms[:30]):
                if form not in ("8-K", "10-Q", "10-K"):
                    continue
                if filings_processed >= 3:  # Max 3 filings per symbol
                    break

                accession_clean = accessions[i].replace("-", "")
                primary_doc = descriptions[i] if i < len(descriptions) else ""

                # Build filing URL
                doc_url = _find_main_document(int(cik), accession_clean, primary_doc)

                # Fetch actual content for 8-K filings (most actionable)
                # Use cache to avoid re-reading the same filing each cycle
                content = ""
                if form == "8-K" and doc_url:
                    if accession_clean in _FILING_CACHE:
                        content = _FILING_CACHE[accession_clean]
                        logger.debug(
                            "Cache hit for %s %s filing (%s) -- skipping re-fetch",
                            symbol, form, accession_clean[:16],
                        )
                    else:
                        logger.debug("Fetching 8-K content for %s: %s", symbol, doc_url)
                        content = _fetch_filing_content(doc_url, max_chars=5000)
                        if content:
                            _FILING_CACHE[accession_clean] = content
                            logger.info(
                                "Read %d chars from %s %s filing (cached)",
                                len(content), symbol, form,
                            )
                            # Cap cache at 500 entries
                            if len(_FILING_CACHE) > 500:
                                oldest = next(iter(_FILING_CACHE))
                                del _FILING_CACHE[oldest]
                            # Persist to disk so cache survives restarts
                            _save_cache(_FILING_CACHE)

                # Fall back to generic summary if content fetch failed
                summary = content if content else (
                    f"SEC {form} filing for {symbol} submitted on {dates[i]}. "
                    f"Filing URL: {doc_url}"
                )

                items.append(ResearchItem(
                    source="sec",
                    symbol=symbol,
                    title=f"{symbol} {form} filing — {dates[i]}",
                    summary=summary[:5000],
                    url=doc_url or f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}",
                    published_at=datetime.fromisoformat(dates[i]).replace(tzinfo=timezone.utc),
                    raw={"form": form, "date": dates[i], "cik": cik},
                ))

                filings_processed += 1
                time.sleep(0.1)  # Respect SEC rate limits

            logger.debug("Processed %d SEC filings for %s", filings_processed, symbol)
            time.sleep(0.15)  # Rate limit between symbols

        except Exception as e:
            logger.warning("SEC fetch error for %s: %s", symbol, e)

    logger.info("Total SEC filing items: %d", len(items))
    return items


# ── Reddit via PRAW ────────────────────────────────────────────────────────────

def fetch_reddit(symbols: List[str], client_id: str, client_secret: str) -> List[ResearchItem]:
    """Fetch top posts mentioning watchlist symbols from financial subreddits."""
    if not client_id or not client_secret:
        logger.warning("Reddit credentials not set — skipping Reddit fetch")
        return []

    try:
        import praw
    except ImportError:
        logger.warning("praw not installed — run: pip install praw")
        return []

    items = []
    subreddits = ["wallstreetbets", "investing", "stocks", "SecurityAnalysis"]

    try:
        reddit = praw.Reddit(
            client_id=client_id,
            client_secret=client_secret,
            user_agent="TradingAgent/1.0 (read-only research bot)",
        )

        for sub in subreddits:
            try:
                for post in reddit.subreddit(sub).hot(limit=25):
                    title = post.title or ""
                    text = (post.selftext or "")[:1000]
                    content = title + " " + text

                    matched = next(
                        (s for s in symbols if s.lower() in content.lower()), None
                    )
                    if not matched:
                        continue

                    items.append(ResearchItem(
                        source="reddit",
                        symbol=matched,
                        title=title[:200],
                        summary=text[:500],
                        url=f"https://reddit.com{post.permalink}",
                        published_at=datetime.fromtimestamp(
                            post.created_utc, tz=timezone.utc
                        ),
                        raw={
                            "score": post.score,
                            "upvote_ratio": post.upvote_ratio,
                            "num_comments": post.num_comments,
                            "subreddit": sub,
                        },
                    ))
            except Exception as e:
                logger.warning("Reddit fetch error for r/%s: %s", sub, e)

        logger.info("Fetched %d Reddit posts", len(items))
    except Exception as e:
        logger.error("Reddit init error: %s", e)

    return items
