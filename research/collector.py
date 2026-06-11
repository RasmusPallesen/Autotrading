"""
Data collector: fetches news, SEC filings (with actual content), and Reddit posts.
"""

import email.utils
import html
import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
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
# Evict empty cache entries — these are from previously failed fetches
# and should be retried rather than served as cache hits
_empty_keys = [k for k, v in _FILING_CACHE.items() if not v or len(str(v)) < 50]
if _empty_keys:
    for k in _empty_keys:
        del _FILING_CACHE[k]
    _save_cache(_FILING_CACHE)

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


# ── News fetching via Alpaca News API + Yahoo Finance RSS fallback ────────────
#
# Primary: Alpaca /v1beta1/news — returns Benzinga news, batched per call,
#   uses the same ALPACA_API_KEY already configured. Free tier included.
# Fallback: Yahoo Finance RSS and Google News RSS (work from most server IPs).

_NEWS_CUTOFF_HOURS   = 48
_NEWS_MAX_PER_SYMBOL = 5
_NEWS_BATCH_SIZE     = 10     # Alpaca allows up to ~50 symbols per request
_RSS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}


def _parse_iso_date(date_str: str) -> Optional[datetime]:
    """Parse ISO 8601 or RFC 2822 date string into a timezone-aware datetime."""
    if not date_str:
        return None
    try:
        # ISO 8601 (Alpaca format): 2024-06-10T14:30:00Z
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        try:
            return email.utils.parsedate_to_datetime(date_str.strip())
        except Exception:
            return None


def _fetch_alpaca_news(
    symbols: List[str],
    cutoff: datetime,
    alpaca_key: str,
    alpaca_secret: str,
) -> List[ResearchItem]:
    """
    Fetch news from Alpaca's /v1beta1/news endpoint (Benzinga source).
    Batches up to _NEWS_BATCH_SIZE symbols per request.
    """
    items: List[ResearchItem] = []
    seen_ids: set = set()
    start_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

    headers = {
        "APCA-API-KEY-ID":     alpaca_key,
        "APCA-API-SECRET-KEY": alpaca_secret,
    }
    base_url = "https://data.alpaca.markets/v1beta1/news"

    for i in range(0, len(symbols), _NEWS_BATCH_SIZE):
        batch = symbols[i : i + _NEWS_BATCH_SIZE]
        params = {
            "symbols":         ",".join(batch),
            "start":           start_str,
            "limit":           50,
            "include_content": "false",
            "sort":            "desc",
        }
        try:
            resp = requests.get(base_url, params=params, headers=headers, timeout=10)
            if resp.status_code == 403:
                logger.debug("[NEWS] Alpaca news: 403 (key missing or not whitelisted)")
                break
            if resp.status_code == 429:
                logger.warning("[NEWS] Alpaca news rate-limited — pausing 5s")
                time.sleep(5)
                continue
            resp.raise_for_status()
            data = resp.json()
            for article in data.get("news", []):
                article_id = str(article.get("id", ""))
                if article_id in seen_ids:
                    continue
                seen_ids.add(article_id)

                pub_dt = _parse_iso_date(article.get("created_at", ""))
                if pub_dt is None:
                    pub_dt = datetime.now(timezone.utc)
                if pub_dt < cutoff:
                    continue

                headline = html.unescape((article.get("headline") or "").strip())
                summary  = html.unescape((article.get("summary")  or headline).strip())
                url      = article.get("url", "")
                source   = article.get("source", "benzinga")
                tickers  = article.get("symbols", batch)

                age_hours = (datetime.now(timezone.utc) - pub_dt).total_seconds() / 3600
                for sym in tickers:
                    if sym in batch:
                        items.append(ResearchItem(
                            source="news",
                            symbol=sym,
                            title=headline[:200],
                            summary=summary[:600],
                            url=url,
                            published_at=pub_dt,
                            raw={"feed": source, "age_hours": round(age_hours, 1), "id": article_id},
                        ))
        except Exception as exc:
            logger.debug("[NEWS] Alpaca batch %s error: %s", batch[:3], exc)
        time.sleep(0.2)

    logger.info("[NEWS] Alpaca returned %d articles for %d symbols", len(items), len(symbols))
    return items


def _fetch_rss_symbol(symbol: str, cutoff: datetime) -> List[ResearchItem]:
    """
    Fallback: fetch Yahoo Finance RSS then Google News RSS for a single symbol.
    Returns up to _NEWS_MAX_PER_SYMBOL items or empty list if both fail.
    """
    sources = [
        ("yahoo", f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"),
        ("google", f"https://news.google.com/rss/search?q={symbol}+stock+news&hl=en-US&gl=US&ceid=US:en"),
    ]
    for feed_name, url in sources:
        try:
            resp = requests.get(url, headers=_RSS_HEADERS, timeout=8)
            if resp.status_code in (403, 429):
                continue
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            items: List[ResearchItem] = []
            for item in root.iter("item"):
                title = html.unescape((item.findtext("title") or "").strip())
                link  = (item.findtext("link") or "").strip()
                desc  = html.unescape((item.findtext("description") or "").strip())
                pub_str = item.findtext("pubDate") or item.findtext("published") or ""
                pub_dt = _parse_iso_date(pub_str) or datetime.now(timezone.utc)
                if pub_dt < cutoff or not title:
                    continue
                age_h = (datetime.now(timezone.utc) - pub_dt).total_seconds() / 3600
                items.append(ResearchItem(
                    source="news", symbol=symbol,
                    title=title[:200],
                    summary=desc[:600] if desc else title,
                    url=link,
                    published_at=pub_dt,
                    raw={"feed": feed_name, "age_hours": round(age_h, 1)},
                ))
            if items:
                return items[:_NEWS_MAX_PER_SYMBOL]
        except Exception:
            continue
    return []


def fetch_news(symbols: List[str], api_key: str = "") -> List[ResearchItem]:
    """
    Fetch recent news articles for each symbol.

    Strategy (in order of preference):
    1. Alpaca /v1beta1/news (Benzinga) — uses ALPACA_API_KEY + ALPACA_SECRET_KEY env vars.
       Batched, efficient, returns high-quality financial news. Free tier included.
    2. Yahoo Finance RSS / Google News RSS — per-symbol fallback if Alpaca unavailable.

    Returns up to 5 articles per symbol from the last 48 hours.
    The api_key parameter is accepted for backward compatibility but ignored
    (Alpaca keys are read from environment variables directly).
    """
    import os
    cutoff      = datetime.now(timezone.utc) - timedelta(hours=_NEWS_CUTOFF_HOURS)
    all_items: List[ResearchItem] = []
    seen_urls:  set = set()

    alpaca_key    = os.getenv("ALPACA_API_KEY", "")
    alpaca_secret = os.getenv("ALPACA_SECRET_KEY", "")

    if alpaca_key and alpaca_secret:
        raw = _fetch_alpaca_news(symbols, cutoff, alpaca_key, alpaca_secret)
        # Dedup URLs and cap per symbol
        per_symbol: dict = {}
        for item in raw:
            if item.url in seen_urls:
                continue
            seen_urls.add(item.url)
            bucket = per_symbol.setdefault(item.symbol, [])
            if len(bucket) < _NEWS_MAX_PER_SYMBOL:
                bucket.append(item)
        for sym_items in per_symbol.values():
            all_items.extend(sym_items)
    else:
        # No Alpaca keys — fall back to RSS per symbol
        logger.info("[NEWS] No Alpaca keys — using RSS fallback for %d symbols", len(symbols))
        for symbol in symbols:
            items = _fetch_rss_symbol(symbol, cutoff)
            new_items = [i for i in items if i.url not in seen_urls]
            for i in new_items:
                seen_urls.add(i.url)
            all_items.extend(new_items)
            time.sleep(0.25)

    logger.info("[NEWS] Total: %d articles for %d symbols", len(all_items), len(symbols))
    return all_items


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

                # Fetch actual content for all filing types.
                # 8-K: material events (most time-sensitive)
                # 10-Q: quarterly earnings — richest financial data
                # 10-K: annual report — full business overview
                # Cache valid content to avoid re-reading same filing each cycle.
                # Empty cache entries are NOT served — always re-try failed fetches.
                content = ""
                if doc_url:
                    cached = _FILING_CACHE.get(accession_clean, "")
                    if cached:  # Only use cache if it has actual content
                        content = cached
                        logger.debug(
                            "Cache hit for %s %s filing (%s) -- skipping re-fetch",
                            symbol, form, accession_clean[:16],
                        )
                    else:
                        # Limit content size by form type
                        max_chars = {
                            "8-K":  5000,   # Material event — concise
                            "10-Q": 8000,   # Quarterly — more detail needed
                            "10-K": 6000,   # Annual — summary level
                        }.get(form, 5000)

                        logger.debug(
                            "Fetching %s content for %s: %s",
                            form, symbol, doc_url,
                        )
                        content = _fetch_filing_content(doc_url, max_chars=max_chars)
                        if content and len(content) > 200:  # Only cache substantial content
                            _FILING_CACHE[accession_clean] = content
                            logger.info(
                                "Read %d chars from %s %s filing (cached)",
                                len(content), symbol, form,
                            )
                            if len(_FILING_CACHE) > 500:
                                oldest = next(iter(_FILING_CACHE))
                                del _FILING_CACHE[oldest]
                            _save_cache(_FILING_CACHE)
                        elif not content:
                            logger.debug(
                                "Could not fetch content for %s %s — "
                                "will use filing metadata only",
                                symbol, form,
                            )

                # Build summary — use actual content if available,
                # otherwise provide a structured metadata summary that at least
                # tells Claude what type of filing this is and when it was filed.
                if content and len(content) > 200:
                    summary = content
                else:
                    form_descriptions = {
                        "8-K":  "material event or corporate announcement",
                        "10-Q": "quarterly financial report",
                        "10-K": "annual financial report",
                    }
                    summary = (
                        f"{symbol} filed a {form} ({form_descriptions.get(form, 'SEC filing')}) "
                        f"on {dates[i]}. "
                        f"Filing content could not be retrieved — "
                        f"refer to the SEC filing directly for details: {doc_url}. "
                        f"This filing should be reviewed for material information."
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
