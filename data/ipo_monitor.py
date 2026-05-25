"""
Upcoming IPO monitor using the Massive.com corporate-actions API.

Fetches IPOs expected in the next N days and caches the result to disk for
IPO_CACHE_TTL_HOURS (default 24) so the API is hit at most once per day.
The caller (research_agent) writes each IPO as a CATALYST signal directly to
research_store — no Claude call required.

Endpoint: GET https://api.massive.com/v1/stocks/corporate-actions/ipos
Auth: apiKey query parameter (MASSIVE_API_KEY env var)
"""

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List

import requests

logger = logging.getLogger(__name__)

_CACHE_PATH = Path(__file__).parent.parent / "logs" / "ipo_cache.json"
_ENDPOINT   = "https://api.massive.com/v1/stocks/corporate-actions/ipos"
_TTL_HOURS  = int(os.getenv("IPO_CACHE_TTL_HOURS", "24"))
_LOOKFORWARD_DAYS = int(os.getenv("IPO_LOOKFORWARD_DAYS", "14"))


class IpoMonitor:
    """
    Fetches upcoming IPOs from Massive.com, caching to disk to avoid
    re-fetching on every research cycle.
    """

    def __init__(self):
        self._api_key = os.getenv("MASSIVE_API_KEY", "")
        if not self._api_key:
            logger.warning("IpoMonitor: MASSIVE_API_KEY not set — IPO data unavailable")

    def fetch_upcoming(self, lookforward_days: int = _LOOKFORWARD_DAYS) -> List[dict]:
        """
        Return upcoming IPOs as a list of dicts.
        Serves from disk cache if fresh; calls Massive API otherwise.
        Returns [] if API key missing or endpoint unavailable.
        """
        cached = self._load_cache()
        if cached is not None:
            logger.debug("IPO cache HIT (age < %dh)", _TTL_HOURS)
            return cached

        if not self._api_key:
            return []

        return self._fetch_from_api(lookforward_days)

    # ── Cache helpers ─────────────────────────────────────────────────────────

    def _load_cache(self):
        """Return cached IPO list if fresh, else None."""
        if not _CACHE_PATH.exists():
            return None
        try:
            data = json.loads(_CACHE_PATH.read_text())
            cached_at = datetime.fromisoformat(data["cached_at"])
            age = datetime.now(timezone.utc) - cached_at
            if age < timedelta(hours=_TTL_HOURS):
                return data.get("ipos", [])
        except Exception as e:
            logger.debug("IPO cache read error: %s", e)
        return None

    def _save_cache(self, ipos: List[dict]):
        try:
            _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _CACHE_PATH.write_text(json.dumps({
                "cached_at": datetime.now(timezone.utc).isoformat(),
                "ipos": ipos,
            }, indent=2))
        except Exception as e:
            logger.warning("IPO cache write error: %s", e)

    # ── API fetch ─────────────────────────────────────────────────────────────

    def _fetch_from_api(self, lookforward_days: int) -> List[dict]:
        today = datetime.now(timezone.utc).date()
        to_date = today + timedelta(days=lookforward_days)
        params = {
            "apiKey": self._api_key,
            "from": today.isoformat(),
            "to": to_date.isoformat(),
        }
        logger.info("IpoMonitor: fetching from %s (from=%s to=%s)", _ENDPOINT, today, to_date)
        try:
            resp = requests.get(_ENDPOINT, params=params, timeout=10)
            if resp.status_code == 403:
                logger.debug(
                    "IpoMonitor: 403 from Massive — endpoint may require a higher plan tier. "
                    "URL attempted: %s", resp.url
                )
                self._save_cache([])
                return []
            if resp.status_code == 404:
                logger.debug(
                    "IpoMonitor: 404 from Massive — endpoint path may differ. "
                    "URL attempted: %s", resp.url
                )
                self._save_cache([])
                return []
            resp.raise_for_status()

            data = resp.json()
            # Massive typically wraps results under "results" or returns a list directly
            ipos = data if isinstance(data, list) else data.get("results", data.get("ipos", []))
            logger.info("IpoMonitor: %d upcoming IPOs fetched", len(ipos))
            self._save_cache(ipos)
            return ipos

        except requests.RequestException as e:
            logger.warning("IpoMonitor: API request failed: %s", e)
            self._save_cache([])
            return []
        except Exception as e:
            logger.warning("IpoMonitor: unexpected error: %s", e)
            return []
