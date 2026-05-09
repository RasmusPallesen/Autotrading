"""
Yahoo Finance crumb helper.
Shared across earnings_calendar.py, iv_monitor.py, and market_scanner.py.

Yahoo Finance changed their API in 2024-2025 to require a session crumb
token with every request. Without it all requests return:
  {"finance":{"result":null,"error":{"code":"Unauthorized","description":"Invalid Crumb"}}}

Usage:
    from data.yahoo_helper import get_yahoo_session
    session, crumb = get_yahoo_session()
    resp = session.get(url, params={"crumb": crumb, ...})
"""

import logging
import threading
import time
from typing import Optional, Tuple

import requests

logger = logging.getLogger(__name__)

_LOCK   = threading.Lock()
_SESSION: Optional[requests.Session] = None
_CRUMB:   Optional[str] = None
_CRUMB_TS: float = 0.0
_CRUMB_TTL = 3600  # Refresh crumb every hour

YAHOO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://finance.yahoo.com/",
}


def get_yahoo_session() -> Tuple[requests.Session, Optional[str]]:
    """
    Returns a (session, crumb) tuple ready for Yahoo Finance API calls.
    Session has the required cookies; crumb must be passed as ?crumb=xxx.
    Thread-safe — uses a module-level lock.
    Refreshes automatically when crumb is older than 1 hour.
    """
    global _SESSION, _CRUMB, _CRUMB_TS

    with _LOCK:
        now = time.time()
        if _CRUMB and _SESSION and (now - _CRUMB_TS) < _CRUMB_TTL:
            return _SESSION, _CRUMB

        logger.info("Fetching fresh Yahoo Finance crumb...")
        session, crumb = _fetch_crumb()
        if session and crumb:
            _SESSION  = session
            _CRUMB    = crumb
            _CRUMB_TS = now
            logger.info("Yahoo Finance crumb acquired successfully")
        else:
            logger.warning(
                "Could not acquire Yahoo Finance crumb -- "
                "API calls may return Unauthorized errors"
            )
            # Return existing session/crumb if available, even if stale
            if _SESSION:
                return _SESSION, _CRUMB
            # Last resort: return a plain session with no crumb
            _SESSION = requests.Session()
            _SESSION.headers.update(YAHOO_HEADERS)
            _CRUMB = None

        return _SESSION, _CRUMB


def _fetch_crumb() -> Tuple[Optional[requests.Session], Optional[str]]:
    """
    Fetch a Yahoo Finance session cookie and crumb token.
    Returns (session, crumb) or (None, None) on failure.
    """
    session = requests.Session()
    session.headers.update(YAHOO_HEADERS)

    # Strategy 1: Standard crumb endpoint
    try:
        # Step 1 — hit consent/fc page to get initial cookies
        session.get("https://fc.yahoo.com", timeout=8)

        # Step 2 — hit Yahoo Finance to get session cookies
        session.get("https://finance.yahoo.com", timeout=8)

        # Step 3 — fetch crumb
        resp = session.get(
            "https://query1.finance.yahoo.com/v1/test/getcrumb",
            timeout=8,
        )
        if resp.status_code == 200 and resp.text.strip():
            crumb = resp.text.strip()
            if len(crumb) > 3:  # Valid crumb is typically 11 chars
                logger.debug("Crumb fetched via strategy 1: %s", crumb[:6] + "...")
                return session, crumb
    except Exception as e:
        logger.debug("Crumb strategy 1 failed: %s", e)

    # Strategy 2: Try alternate crumb endpoint
    try:
        session2 = requests.Session()
        session2.headers.update(YAHOO_HEADERS)
        resp = session2.get(
            "https://query2.finance.yahoo.com/v1/test/getcrumb",
            timeout=8,
        )
        if resp.status_code == 200 and resp.text.strip():
            crumb = resp.text.strip()
            if len(crumb) > 3:
                logger.debug("Crumb fetched via strategy 2")
                return session2, crumb
    except Exception as e:
        logger.debug("Crumb strategy 2 failed: %s", e)

    # Strategy 3: Extract crumb from Yahoo Finance page HTML
    try:
        session3 = requests.Session()
        session3.headers.update(YAHOO_HEADERS)
        resp = session3.get("https://finance.yahoo.com/quote/AAPL", timeout=10)
        import re
        # Yahoo embeds the crumb in the page JS
        match = re.search(r'"crumb":"([^"]+)"', resp.text)
        if match:
            crumb = match.group(1).replace("\\u002F", "/")
            logger.debug("Crumb extracted from page HTML")
            return session3, crumb
    except Exception as e:
        logger.debug("Crumb strategy 3 failed: %s", e)

    return None, None


def yahoo_get(url: str, params: dict = None, timeout: int = 10) -> Optional[dict]:
    """
    Convenience wrapper: GET a Yahoo Finance API URL with crumb injected.
    Returns parsed JSON dict or None on failure.
    """
    session, crumb = get_yahoo_session()
    p = dict(params or {})
    if crumb:
        p["crumb"] = crumb
    try:
        resp = session.get(url, params=p, timeout=timeout)
        if resp.status_code == 200:
            return resp.json()
        elif resp.status_code == 401:
            # Crumb expired — force refresh on next call
            global _CRUMB_TS
            _CRUMB_TS = 0.0
            logger.warning("Yahoo Finance crumb expired (401) -- will refresh on next call")
        else:
            logger.debug("Yahoo Finance HTTP %d for %s", resp.status_code, url)
    except Exception as e:
        logger.debug("Yahoo Finance request error: %s", e)
    return None
