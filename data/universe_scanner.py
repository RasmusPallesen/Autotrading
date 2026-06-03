"""
Universe Scanner — intraday discovery of previously unknown momentum stocks.
Runs every 5 minutes during market hours as part of the research agent.

Two data sources (both free):
1. Alpaca most-actives endpoint — top stocks by volume updated continuously
2. Yahoo Finance movers — gainers, losers, most active

Pre-filter logic (Stage 1):
- Volume 3x+ average but price move < 1.5%  → accumulation before the move
- Price move > 3% with volume 2x+            → confirmed momentum
- Sector sympathy: if a major stock in watchlist moves >2%, flag sector peers

Returns a dynamic candidate list for the intraday monitor (Stage 2).
Candidates are scored and ranked — only top 15 passed to intraday monitor.
"""

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

ALPACA_HEADERS_TEMPLATE = {
    "APCA-API-KEY-ID": "",
    "APCA-API-SECRET-KEY": "",
}

YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

# Symbols to always exclude — too illiquid or problematic for intraday
_BLACKLIST = {"UVXY", "SQQQ", "SPXS", "TQQQ", "SPXU", "VIX"}

# Max candidates to return to intraday monitor
MAX_CANDIDATES = int(os.getenv("UNIVERSE_MAX_CANDIDATES", "15"))

# Minimum average daily volume — avoid micro-caps with no liquidity
MIN_AVG_VOLUME = int(os.getenv("UNIVERSE_MIN_AVG_VOLUME", "500000"))


@dataclass
class UniverseCandidate:
    symbol: str
    price: float
    change_pct: float           # % price change today
    volume: int                 # Today's volume so far
    avg_volume: Optional[int]   # 30-day average daily volume
    volume_ratio: float         # volume / avg_volume
    discovery_reason: str       # Why this was flagged
    sector: str = "UNKNOWN"
    market_cap: Optional[float] = None
    score: float = 0.0          # Combined score for ranking
    discovered_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @property
    def is_accumulation(self) -> bool:
        """High volume, low price move — institutional buying quietly."""
        return self.volume_ratio >= 3.0 and abs(self.change_pct) < 1.5

    @property
    def is_momentum(self) -> bool:
        """Price moving with volume confirmation."""
        return abs(self.change_pct) >= 2.0 and self.volume_ratio >= 1.5

    @property
    def is_breakout(self) -> bool:
        """Strong directional move with very high volume."""
        return abs(self.change_pct) >= 4.0 and self.volume_ratio >= 2.5

    def to_summary(self) -> str:
        signal_type = (
            "BREAKOUT" if self.is_breakout
            else "MOMENTUM" if self.is_momentum
            else "ACCUMULATION" if self.is_accumulation
            else "SCANNER"
        )
        return (
            f"[UNIVERSE DISCOVERY|{signal_type}] {self.symbol}: "
            f"{self.change_pct:+.1f}% today on {self.volume_ratio:.1f}x average volume. "
            f"Reason: {self.discovery_reason}. "
            f"Price: ${self.price:.2f}. "
            f"Signal type: {signal_type} — "
            + (
                "High volume without proportional price move suggests institutional "
                "accumulation. Watch for price breakout as buying pressure builds."
                if self.is_accumulation else
                "Strong directional move with volume confirmation. "
                "Momentum likely to continue short-term."
                if self.is_momentum else
                "Explosive move with very high volume — potential catalyst event. "
                "Verify fundamentals before entering."
                if self.is_breakout else
                "Flagged by market scanner for unusual activity."
            )
        )


class UniverseScanner:
    """
    Scans the full US market for intraday momentum candidates.
    Runs every 5 minutes — much faster than the 15-minute research cycle.
    """

    def __init__(self, alpaca_api_key: str = "", alpaca_secret_key: str = "",
                 paper: bool = True):
        self._api_key    = alpaca_api_key or os.getenv("ALPACA_API_KEY", "")
        self._secret_key = alpaca_secret_key or os.getenv("ALPACA_SECRET_KEY", "")
        self._paper      = paper
        self._base_url   = (
            "https://paper-api.alpaca.markets"
            if paper else
            "https://api.alpaca.markets"
        )
        self._data_url   = "https://data.alpaca.markets"

        # Cache to avoid re-flagging the same symbol every 5 minutes
        # {symbol: last_flagged_at}
        self._flagged_cache: Dict[str, datetime] = {}
        self._flag_cooldown_minutes = int(
            os.getenv("UNIVERSE_COOLDOWN_MINUTES", "30")
        )

        # Sector sympathy map — major stock → peers to watch
        self._sympathy_map = {
            "NVDA": ["AMD", "INTC", "AVGO", "QCOM", "MRVL", "ARM", "AMAT"],
            "AMD":  ["NVDA", "INTC", "AVGO", "QCOM"],
            "TSLA": ["RIVN", "LCID", "NIO", "F", "GM"],
            "PLTR": ["AI", "BBAI", "SOUN", "RXRX"],
            "NVO":  ["LLY", "DXCM", "PODD", "TNDM"],
            "LLY":  ["NVO", "DXCM", "PODD"],
            "KTOS": ["AVAV", "RCAT", "NOC", "LMT", "RTX"],
            "AVAV": ["KTOS", "RCAT", "UMAC"],
            "MANE": ["RXRX", "BEAM", "CRSP", "NTLA", "RYTM"],
        }

    @property
    def _alpaca_headers(self) -> dict:
        return {
            "APCA-API-KEY-ID": self._api_key,
            "APCA-API-SECRET-KEY": self._secret_key,
        }

    def scan(self, existing_watchlist: List[str]) -> List[UniverseCandidate]:
        """
        Run all scanner sources and return ranked candidates.
        Excludes symbols already on the existing watchlist.
        Applies cooldown to avoid re-flagging same symbol repeatedly.
        """
        candidates: Dict[str, UniverseCandidate] = {}

        # Source 1: Alpaca most-actives
        logger.info("[SCAN] Fetching Alpaca most-actives...")
        for c in self._fetch_alpaca_most_actives():
            if self._should_include(c, existing_watchlist):
                candidates[c.symbol] = c
        logger.info("[SCAN] Alpaca most-actives done (%d candidates so far)", len(candidates))

        # Source 2: Yahoo Finance movers
        logger.info("[SCAN] Fetching Yahoo Finance movers...")
        for c in self._fetch_yahoo_movers():
            if c.symbol not in candidates and self._should_include(c, existing_watchlist):
                candidates[c.symbol] = c
        logger.info("[SCAN] Yahoo movers done (%d candidates so far)", len(candidates))

        # Source 3: Sector sympathy
        logger.info("[SCAN] Checking sector sympathy...")
        for c in self._check_sector_sympathy(existing_watchlist):
            if c.symbol not in candidates and self._should_include(c, existing_watchlist):
                candidates[c.symbol] = c
        logger.info("[SCAN] Sector sympathy done (%d candidates total)", len(candidates))

        # Score and rank
        ranked = self._rank(list(candidates.values()))

        # Update flagged cache
        now = datetime.now(timezone.utc)
        for c in ranked[:MAX_CANDIDATES]:
            self._flagged_cache[c.symbol] = now

        # Evict old cache entries
        cutoff = now - timedelta(minutes=self._flag_cooldown_minutes * 2)
        self._flagged_cache = {
            k: v for k, v in self._flagged_cache.items() if v > cutoff
        }

        if ranked:
            logger.info(
                "Universe scanner: %d candidates found (top: %s)",
                len(ranked),
                ", ".join(f"{c.symbol}({c.change_pct:+.1f}%,{c.volume_ratio:.1f}x)"
                          for c in ranked[:5]),
            )

        return ranked[:MAX_CANDIDATES]

    def _should_include(self, c: UniverseCandidate, watchlist: List[str]) -> bool:
        """Apply filters — blacklist, liquidity, cooldown."""
        if c.symbol in _BLACKLIST:
            return False
        if c.symbol in watchlist:
            return False  # Already covered by main watchlist
        if c.avg_volume and c.avg_volume < MIN_AVG_VOLUME:
            return False  # Too illiquid
        # Cooldown check
        last_flagged = self._flagged_cache.get(c.symbol)
        if last_flagged:
            age_minutes = (datetime.now(timezone.utc) - last_flagged).total_seconds() / 60
            if age_minutes < self._flag_cooldown_minutes:
                return False
        return True

    def _rank(self, candidates: List[UniverseCandidate]) -> List[UniverseCandidate]:
        """Score candidates — higher is better."""
        for c in candidates:
            score = 0.0
            # Volume ratio is the strongest signal
            score += min(c.volume_ratio, 10.0) * 0.4
            # Price move magnitude (absolute)
            score += min(abs(c.change_pct), 10.0) * 0.3
            # Bonus for accumulation pattern (high volume, low price move)
            if c.is_accumulation:
                score += 2.0
            # Bonus for confirmed breakout
            if c.is_breakout:
                score += 1.5
            c.score = score
        return sorted(candidates, key=lambda c: c.score, reverse=True)

    def _fetch_alpaca_most_actives(self) -> List[UniverseCandidate]:
        """
        Fetch top stocks by volume from Alpaca's screener endpoint.
        Returns up to 20 most active stocks updated continuously during market hours.
        """
        candidates = []
        try:
            resp = requests.get(
                f"{self._data_url}/v1beta1/screener/stocks/most-actives",
                params={"by": "volume", "top": 20},
                headers=self._alpaca_headers,
                timeout=8,
            )
            if resp.status_code != 200:
                logger.debug("Alpaca most-actives: HTTP %d", resp.status_code)
                return []

            data = resp.json()
            most_actives = data.get("most_actives", [])

            for item in most_actives:
                symbol = item.get("symbol", "")
                if not symbol:
                    continue

                price      = float(item.get("price", 0))
                change_pct = float(item.get("change_percent", 0))
                volume     = int(item.get("volume", 0))
                avg_volume = int(item.get("avg_vol_30d", 0)) or None
                vol_ratio  = (volume / avg_volume) if avg_volume else 1.0

                if price <= 0 or volume <= 0:
                    continue

                candidates.append(UniverseCandidate(
                    symbol=symbol,
                    price=price,
                    change_pct=change_pct,
                    volume=volume,
                    avg_volume=avg_volume,
                    volume_ratio=vol_ratio,
                    discovery_reason=f"Alpaca most-active: {vol_ratio:.1f}x avg volume",
                    score=0.0,
                ))

        except Exception as e:
            logger.debug("Alpaca most-actives error: %s", e)

        return candidates

    def _fetch_yahoo_movers(self) -> List[UniverseCandidate]:
        """
        Fetch Yahoo Finance gainers, losers, and most-active stocks.
        Parses the finance.yahoo.com/markets/stocks/ endpoints.
        """
        candidates = []
        endpoints = [
            ("https://finance.yahoo.com/markets/stocks/gainers/", "gainer"),
            ("https://finance.yahoo.com/markets/stocks/losers/",  "loser"),
            ("https://finance.yahoo.com/markets/stocks/most-active/", "most-active"),
        ]

        for url, reason_tag in endpoints:
            try:
                resp = requests.get(url, headers=YAHOO_HEADERS, timeout=10)
                if resp.status_code != 200:
                    continue

                import re
                # Extract symbol, price, change% from Yahoo Finance table rows
                # Yahoo embeds data in JSON within the page
                json_match = re.search(
                    r'"finance":\s*\{.*?"result":\s*\[(.*?)\]\s*\}',
                    resp.text, re.DOTALL
                )

                # Simpler: parse visible table data
                # Find rows with ticker pattern
                row_pattern = re.compile(
                    r'"symbol":"([A-Z]{1,5})".*?"regularMarketPrice":\{"raw":([\d.]+)'
                    r'.*?"regularMarketChangePercent":\{"raw":(-?[\d.]+)'
                    r'.*?"regularMarketVolume":\{"raw":(\d+)',
                    re.DOTALL
                )

                for m in row_pattern.finditer(resp.text):
                    try:
                        symbol     = m.group(1)
                        price      = float(m.group(2))
                        change_pct = float(m.group(3))
                        volume     = int(m.group(4))

                        if price <= 0:
                            continue

                        candidates.append(UniverseCandidate(
                            symbol=symbol,
                            price=price,
                            change_pct=change_pct,
                            volume=volume,
                            avg_volume=None,
                            volume_ratio=1.0,  # Unknown without avg
                            discovery_reason=f"Yahoo Finance {reason_tag}: {change_pct:+.1f}%",
                            score=0.0,
                        ))
                    except (ValueError, IndexError):
                        continue

                time.sleep(0.3)

            except Exception as e:
                logger.debug("Yahoo movers error (%s): %s", reason_tag, e)

        return candidates

    def _check_sector_sympathy(
        self, watchlist: List[str]
    ) -> List[UniverseCandidate]:
        """
        If a major watchlist stock moves >2% intraday, flag its sector peers
        that haven't moved yet — they often follow within 30-60 minutes.
        """
        candidates = []
        if not self._api_key:
            return []

        # Check prices for sympathy trigger symbols
        trigger_symbols = [s for s in self._sympathy_map if s in watchlist]
        if not trigger_symbols:
            return []

        try:
            resp = requests.get(
                f"{self._data_url}/v2/stocks/snapshots",
                params={"symbols": ",".join(trigger_symbols), "feed": "iex"},
                headers=self._alpaca_headers,
                timeout=8,
            )
            if resp.status_code != 200:
                return []

            snapshots = resp.json()

            for trigger_sym, peers in self._sympathy_map.items():
                if trigger_sym not in snapshots:
                    continue

                snap = snapshots[trigger_sym]
                daily_bar = snap.get("dailyBar", {})
                prev_close = snap.get("prevDailyBar", {}).get("c", 0)
                current    = daily_bar.get("c", 0)

                if not (prev_close and current):
                    continue

                trigger_change = ((current - prev_close) / prev_close) * 100

                # Only fire sympathy if trigger moved significantly
                if abs(trigger_change) < 2.0:
                    continue

                direction = "up" if trigger_change > 0 else "down"
                logger.info(
                    "Sector sympathy trigger: %s %s%.1f%% — checking peers %s",
                    trigger_sym, "+" if trigger_change > 0 else "",
                    trigger_change, peers,
                )

                # Check which peers haven't moved yet
                for peer in peers:
                    if peer in watchlist:
                        continue
                    try:
                        peer_resp = requests.get(
                            f"{self._data_url}/v2/stocks/{peer}/snapshot",
                            params={"feed": "iex"},
                            headers=self._alpaca_headers,
                            timeout=5,
                        )
                        if peer_resp.status_code != 200:
                            continue

                        peer_snap = peer_resp.json()
                        peer_bar  = peer_snap.get("dailyBar", {})
                        peer_prev = peer_snap.get("prevDailyBar", {}).get("c", 0)
                        peer_curr = peer_bar.get("c", 0)
                        peer_vol  = peer_bar.get("v", 0)

                        if not (peer_prev and peer_curr):
                            continue

                        peer_change = ((peer_curr - peer_prev) / peer_prev) * 100

                        # Flag peers that haven't moved yet — sympathy opportunity
                        if abs(peer_change) < abs(trigger_change) * 0.5:
                            candidates.append(UniverseCandidate(
                                symbol=peer,
                                price=peer_curr,
                                change_pct=peer_change,
                                volume=int(peer_vol),
                                avg_volume=None,
                                volume_ratio=1.0,
                                discovery_reason=(
                                    f"Sector sympathy: {trigger_sym} moved "
                                    f"{trigger_change:+.1f}% {direction} — "
                                    f"{peer} hasn't followed yet"
                                ),
                                score=0.0,
                            ))
                        time.sleep(0.1)

                    except Exception as e:
                        logger.debug("Sympathy peer check error %s: %s", peer, e)

        except Exception as e:
            logger.debug("Sector sympathy error: %s", e)

        return candidates
