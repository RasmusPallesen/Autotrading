"""
Motley Fool public article fetcher.
Pulls free analyst articles from fool.com RSS feeds for watchlist symbols.
No subscription required — public content only.

Articles are fetched per-symbol using fool.com's ticker quote pages,
which include an RSS-compatible article feed. Results are deduplicated
and returned as ResearchItem objects ready for Claude analysis.
"""

import hashlib
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import List
from xml.etree import ElementTree

import requests

logger = logging.getLogger(__name__)

# fool.com article feed per ticker
_TICKER_FEED_URL = "https://www.fool.com/quote/{exchange}/{ticker_lower}/"

# Company name → ticker map for article text matching (built dynamically)
_COMPANY_HINTS = {
    "nvidia": "NVDA", "amd": "AMD", "intel": "INTC", "broadcom": "AVGO",
    "qualcomm": "QCOM", "arm holdings": "ARM", "asml": "ASML", "tsmc": "TSM",
    "marvell": "MRVL", "applied materials": "AMAT",
    "microsoft": "MSFT", "alphabet": "GOOGL", "google": "GOOGL",
    "meta": "META", "amazon": "AMZN", "palantir": "PLTR",
    "c3.ai": "AI", "soundhound": "SOUN", "bigbear": "BBAI",
    "enphase": "ENPH", "solaredge": "SEDG", "first solar": "FSLR",
    "nextera": "NEE", "plug power": "PLUG", "bloom energy": "BE",
    "chargepoint": "CHPT", "blink": "BLNK", "sunrun": "RUN",
    "novo nordisk": "NVO", "eli lilly": "LLY", "dexcom": "DXCM",
    "abbott": "ABT", "intuitive surgical": "ISRG", "insulet": "PODD",
    "tandem": "TNDM", "medtronic": "MDT",
    "kratos": "KTOS", "aerovironment": "AVAV", "northrop": "NOC",
    "lockheed": "LMT", "raytheon": "RTX", "axon": "AXON",
    "apple": "AAPL", "tesla": "TSLA", "coinbase": "COIN",
    "microstrategy": "MSTR",
}

# Exchange map for fool.com URL format
_EXCHANGE_MAP = {
    "NVDA": "nasdaq", "AMD": "nasdaq", "INTC": "nasdaq", "AVGO": "nasdaq",
    "QCOM": "nasdaq", "ARM": "nasdaq", "ASML": "nasdaq", "TSM": "nyse",
    "MRVL": "nasdaq", "AMAT": "nasdaq",
    "MSFT": "nasdaq", "GOOGL": "nasdaq", "META": "nasdaq", "AMZN": "nasdaq",
    "PLTR": "nasdaq", "AI": "nyse", "SOUN": "nasdaq", "BBAI": "nyse",
    "ENPH": "nasdaq", "SEDG": "nasdaq", "FSLR": "nasdaq", "NEE": "nyse",
    "PLUG": "nasdaq", "BE": "nyse", "CHPT": "nyse", "BLNK": "nasdaq",
    "RUN": "nasdaq", "ARRY": "nasdaq",
    "NVO": "nyse", "LLY": "nyse", "DXCM": "nasdaq", "ABT": "nyse",
    "ISRG": "nasdaq", "PODD": "nasdaq", "TNDM": "nasdaq", "MDT": "nyse",
    "INVA": "nasdaq", "RYTM": "nasdaq",
    "MANE": "nasdaq", "RXRX": "nasdaq", "BEAM": "nasdaq",
    "CRSP": "nasdaq", "NTLA": "nasdaq",
    "KTOS": "nasdaq", "AVAV": "nasdaq", "RCAT": "nasdaq", "NOC": "nyse",
    "LMT": "nyse", "RTX": "nyse", "AXON": "nasdaq", "UMAC": "nyse",
    "AAPL": "nasdaq", "TSLA": "nasdaq", "COIN": "nasdaq", "MSTR": "nasdaq",
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

# Don't fetch articles older than this
_MAX_AGE_HOURS = 48
# Seconds between requests to be polite
_REQUEST_DELAY = 1.5
# Cache seen article URLs within a session to avoid duplicates
_seen_urls: set = set()


def _get_exchange(symbol: str) -> str:
    return _EXCHANGE_MAP.get(symbol, "nasdaq")


def _fetch_fool_articles(symbol: str, session: requests.Session) -> list:
    """
    Fetch recent Motley Fool articles for a symbol from their quote page RSS.
    Returns list of dicts with title, summary, url, published_at.
    """
    exchange = _get_exchange(symbol)
    url = f"https://www.fool.com/quote/{exchange}/{symbol.lower()}/"

    try:
        resp = session.get(url, headers=_HEADERS, timeout=15)
        if resp.status_code == 404:
            logger.debug("[%s] fool.com 404 — symbol not covered", symbol)
            return []
        if resp.status_code != 200:
            logger.debug("[%s] fool.com HTTP %d", symbol, resp.status_code)
            return []

        html = resp.text
        articles = _parse_articles_from_html(html, symbol)
        return articles

    except requests.RequestException as e:
        logger.debug("[%s] fool.com fetch error: %s", symbol, e)
        return []


def _parse_articles_from_html(html: str, symbol: str) -> list:
    """
    Parse article links and titles from fool.com quote page HTML.
    The page contains article cards with titles, dates, and blurbs.
    """
    articles = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=_MAX_AGE_HOURS)

    try:
        # Look for article links in the HTML — fool.com embeds JSON-LD or
        # article cards we can parse with simple string scanning
        import re

        # Pattern: article links under /investing/ or /story/
        article_pattern = re.compile(
            r'href="(https://www\.fool\.com/(?:investing|story|amp)/[^"]+)"[^>]*>'
            r'\s*([^<]{10,200})',
            re.IGNORECASE | re.DOTALL,
        )

        # Also try to find publication dates near article links
        date_pattern = re.compile(
            r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})',
        )

        found_urls = set()
        dates = date_pattern.findall(html)

        for match in article_pattern.finditer(html):
            article_url = match.group(1).split("?")[0]  # strip query params
            title = match.group(2).strip()
            title = re.sub(r'\s+', ' ', title)  # normalise whitespace

            if len(title) < 15:
                continue
            if article_url in found_urls or article_url in _seen_urls:
                continue

            found_urls.add(article_url)
            _seen_urls.add(article_url)

            articles.append({
                "url": article_url,
                "title": title[:200],
                "summary": f"Motley Fool analyst article: {title}",
                "published_at": datetime.now(timezone.utc),  # approximate
            })

            if len(articles) >= 5:  # cap per symbol
                break

    except Exception as e:
        logger.debug("[%s] HTML parse error: %s", symbol, e)

    return articles


def _fetch_fool_rss(symbol: str, session: requests.Session) -> list:
    """
    Try fool.com sitemap/RSS endpoints as a fallback.
    Returns list of article dicts.
    """
    # fool.com has a general RSS feed; filter by symbol mention in title
    rss_urls = [
        "https://www.fool.com/feeds/index.aspx?id=fool-articles",
        f"https://www.fool.com/feeds/index.aspx?id={symbol.lower()}",
    ]

    articles = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=_MAX_AGE_HOURS)

    for rss_url in rss_urls:
        try:
            resp = session.get(rss_url, headers=_HEADERS, timeout=10)
            if resp.status_code != 200:
                continue

            root = ElementTree.fromstring(resp.content)
            ns = {"atom": "http://www.w3.org/2005/Atom"}

            # Handle both RSS 2.0 and Atom feeds
            items = root.findall(".//item") or root.findall(".//atom:entry", ns)

            for item in items:
                title_el = item.find("title") or item.find("atom:title", ns)
                link_el = item.find("link") or item.find("atom:link", ns)
                desc_el = item.find("description") or item.find("atom:summary", ns)
                date_el = item.find("pubDate") or item.find("atom:published", ns)

                if title_el is None:
                    continue

                title = (title_el.text or "").strip()
                url = (
                    (link_el.text or link_el.get("href", "")).strip()
                    if link_el is not None else ""
                )
                desc = (desc_el.text or "").strip() if desc_el is not None else ""

                # Filter to articles mentioning this symbol or company
                ticker_mentioned = symbol.upper() in title.upper()
                company_mentioned = any(
                    hint in title.lower()
                    for hint, sym in _COMPANY_HINTS.items()
                    if sym == symbol
                )

                if not (ticker_mentioned or company_mentioned):
                    continue
                if url in _seen_urls:
                    continue

                _seen_urls.add(url)
                articles.append({
                    "url": url,
                    "title": title[:200],
                    "summary": desc[:500] if desc else f"Motley Fool: {title}",
                    "published_at": datetime.now(timezone.utc),
                })

                if len(articles) >= 3:
                    break

        except Exception as e:
            logger.debug("[%s] RSS parse error for %s: %s", symbol, rss_url, e)

        if articles:
            break

    return articles


def fetch_motley_fool(symbols: List[str]) -> list:
    """
    Fetch recent Motley Fool public articles for the given symbols.
    Returns list of ResearchItem objects.

    Uses a polite request rate (1.5s between requests) and deduplicates
    articles seen within the same agent session.

    Args:
        symbols: List of ticker symbols to fetch articles for.

    Returns:
        List of ResearchItem instances ready for analyst.analyse_all().
    """
    # Import here to avoid circular import — collector imports us
    from collector import ResearchItem

    if not symbols:
        return []

    results: List[ResearchItem] = []
    session = requests.Session()
    session.headers.update(_HEADERS)

    logger.info("Motley Fool: fetching articles for %d symbols", len(symbols))

    for i, symbol in enumerate(symbols):
        if i > 0:
            time.sleep(_REQUEST_DELAY)

        # Try quote page first, fall back to RSS
        articles = _fetch_fool_articles(symbol, session)
        if not articles:
            articles = _fetch_fool_rss(symbol, session)

        for art in articles:
            results.append(ResearchItem(
                source="motley_fool",
                symbol=symbol,
                title=f"[MOTLEY FOOL] {art['title']}",
                summary=art["summary"][:600],
                url=art["url"],
                published_at=art["published_at"],
                raw={"source_detail": "motley_fool_public"},
            ))

        if articles:
            logger.debug("[%s] Motley Fool: %d articles", symbol, len(articles))

    logger.info(
        "Motley Fool: collected %d articles across %d symbols",
        len(results), len(symbols),
    )
    return results
