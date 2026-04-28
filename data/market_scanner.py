"""
Market scanner: discovers opportunities using Massive.com free tier APIs.
Uses exchange-grade data co-located at NYSE/NASDAQ — far superior to Yahoo Finance.

Endpoints used (all free tier):
- /stocks/snapshots/top-market-movers  — top 20 gainers/losers by % change
- /stocks/snapshots/unified-snapshot   — price snapshot for specific tickers

Falls back to Yahoo Finance if Massive API key is not set.
"""

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

import requests

logger = logging.getLogger(__name__)

MASSIVE_BASE = "https://api.massive.com/v3"
YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


@dataclass
class ScannerHit:
    symbol: str
    company_name: str
    price: float
    change_pct: float
    volume: float
    avg_volume: float
    volume_ratio: float
    reason: str
    score: float


class MarketScanner:
    """
    Scans the broad market for unusual activity.
    Uses Massive.com free tier when API key is available,
    falls back to Yahoo Finance otherwise.
    """

    def __init__(self, alpaca_api_key: str = "", alpaca_secret_key: str = "", paper: bool = True):
        self.massive_key = os.getenv("MASSIVE_API_KEY", "")
        self.alpaca_api_key = alpaca_api_key

    def _massive_params(self, extra: dict = None) -> dict:
        params = {"apiKey": self.massive_key}
        if extra:
            params.update(extra)
        return params

    def scan(self, max_results: int = 20) -> List[ScannerHit]:
        """Run all scans and return deduplicated ranked hits."""
        if self.massive_key:
            return self._scan_massive(max_results)
        else:
            logger.info("No MASSIVE_API_KEY — falling back to Yahoo Finance scanner")
            return self._scan_yahoo(max_results)

    # ── Massive.com scanning ──────────────────────────────────────────────────

    def _scan_massive(self, max_results: int) -> List[ScannerHit]:
        """Use Massive.com free tier endpoints for exchange-grade scanning."""
        all_hits: dict = {}

        # Top gainers
        for hit in self._massive_top_movers("gainers"):
            all_hits[hit.symbol] = hit

        # Top losers (mean reversion opportunities)
        for hit in self._massive_top_movers("losers"):
            if hit.symbol not in all_hits:
                all_hits[hit.symbol] = hit

        ranked = sorted(all_hits.values(), key=lambda h: h.score, reverse=True)
        results = ranked[:max_results]

        logger.info("Massive scanner found %d hits", len(results))
        for h in results[:5]:
            logger.info(
                "  [%s] %+.1f%% | score=%.2f | %s",
                h.symbol, h.change_pct, h.score, h.reason,
            )
        return results

    def _massive_top_movers(self, direction: str = "gainers") -> List[ScannerHit]:
        """
        Fetch top 20 gainers or losers from Massive.com.
        direction: "gainers" or "losers"
        """
        hits = []
        try:
            resp = requests.get(
                f"{MASSIVE_BASE}/stocks/snapshots/gainers-losers",
                params=self._massive_params({"direction": direction}),
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            tickers = data.get("results", data if isinstance(data, list) else [])

            for item in tickers:
                symbol = item.get("ticker", item.get("symbol", ""))
                if not symbol:
                    continue

                # Extract from day bar
                day = item.get("day", item.get("todaysChangePerc", {}))
                if isinstance(day, dict):
                    change_pct = float(day.get("change_percent", day.get("changePercent", 0)))
                    volume = float(day.get("volume", 0))
                    price = float(day.get("close", day.get("c", 0)))
                else:
                    change_pct = float(item.get("todaysChangePerc", item.get("change_percent", 0)))
                    volume = float(item.get("volume", 0))
                    price = float(item.get("lastTrade", {}).get("p", item.get("price", 0)))

                prev = item.get("prevDay", {})
                avg_volume = float(prev.get("volume", volume) or volume)
                vol_ratio = volume / avg_volume if avg_volume > 0 else 1.0

                if price < 1.0:
                    continue
                if abs(change_pct) < 3.0 and direction == "gainers":
                    continue
                if abs(change_pct) < 3.0 and direction == "losers":
                    continue

                move_score = min(abs(change_pct) / 25.0, 1.0) * 0.6
                vol_score = min((vol_ratio - 1.0) / 4.0, 1.0) * 0.3 if vol_ratio > 1 else 0
                price_score = min(price / 500.0, 1.0) * 0.1
                score = min(move_score + vol_score + price_score, 1.0)

                if direction == "gainers":
                    if change_pct > 20:
                        reason = f"EXPLOSIVE gain +{change_pct:.1f}% — investigate catalyst"
                        score = min(score + 0.2, 1.0)
                    elif change_pct > 10:
                        reason = f"Strong gainer +{change_pct:.1f}% today"
                        score = min(score + 0.1, 1.0)
                    else:
                        reason = f"Top gainer +{change_pct:.1f}% today"
                else:
                    reason = f"Top loser {change_pct:.1f}% — mean reversion candidate"

                hits.append(ScannerHit(
                    symbol=symbol,
                    company_name=item.get("name", symbol),
                    price=price,
                    change_pct=change_pct,
                    volume=volume,
                    avg_volume=avg_volume,
                    volume_ratio=vol_ratio,
                    reason=reason,
                    score=score,
                ))

            logger.info("Massive %s scan: %d hits", direction, len(hits))

        except Exception as e:
            logger.warning("Massive %s scan failed: %s — falling back to Yahoo", direction, e)
            hits = self._scan_yahoo_gainers() if direction == "gainers" else []

        return hits

    def get_symbol_detail(self, symbol: str) -> Optional[dict]:
        """Fetch snapshot detail for a discovered symbol."""
        if self.massive_key:
            return self._massive_snapshot(symbol)
        return self._yahoo_detail(symbol)

    def _massive_snapshot(self, symbol: str) -> Optional[dict]:
        """Get unified snapshot for a symbol from Massive."""
        try:
            resp = requests.get(
                f"{MASSIVE_BASE}/stocks/snapshots",
                params=self._massive_params({"tickers": symbol}),
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            if results:
                item = results[0]
                return {
                    "sector": item.get("sector", "Unknown"),
                    "industry": item.get("industry", "Unknown"),
                    "description": item.get("description", ""),
                    "market_cap": item.get("market_cap", 0),
                    "company_name": item.get("name", symbol),
                }
        except Exception as e:
            logger.debug("Massive snapshot failed for %s: %s", symbol, e)
        return self._yahoo_detail(symbol)

    # ── Yahoo Finance fallback ────────────────────────────────────────────────

    def _scan_yahoo(self, max_results: int) -> List[ScannerHit]:
        """Full Yahoo Finance scan as fallback."""
        all_hits: dict = {}
        for fn in [self._scan_yahoo_gainers, self._scan_yahoo_actives, self._scan_yahoo_smallcap]:
            try:
                for hit in fn():
                    if hit.symbol not in all_hits:
                        all_hits[hit.symbol] = hit
                    else:
                        existing = all_hits[hit.symbol]
                        all_hits[hit.symbol] = ScannerHit(
                            symbol=existing.symbol,
                            company_name=existing.company_name,
                            price=existing.price,
                            change_pct=existing.change_pct,
                            volume=existing.volume,
                            avg_volume=existing.avg_volume,
                            volume_ratio=existing.volume_ratio,
                            reason=existing.reason + " + " + hit.reason,
                            score=min(existing.score + 0.15, 1.0),
                        )
            except Exception as e:
                logger.warning("Yahoo scan error: %s", e)

        ranked = sorted(all_hits.values(), key=lambda h: h.score, reverse=True)
        return ranked[:max_results]

    def _fetch_yahoo_screener(self, screen_id: str, count: int = 25) -> List[dict]:
        try:
            resp = requests.get(
                "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved",
                params={"scrIds": screen_id, "count": count, "formatted": "false"},
                headers=YAHOO_HEADERS,
                timeout=10,
            )
            resp.raise_for_status()
            results = resp.json().get("finance", {}).get("result", [])
            if results:
                return results[0].get("quotes", [])
        except Exception as e:
            logger.warning("Yahoo screener %s failed: %s", screen_id, e)
        return []

    def _make_hit(self, q: dict, reason_template: str, min_change: float = 0.0) -> Optional[ScannerHit]:
        symbol = q.get("symbol", "")
        change_pct = float(q.get("regularMarketChangePercent", 0))
        price = float(q.get("regularMarketPrice", 0))
        volume = float(q.get("regularMarketVolume", 0))
        avg_volume = float(q.get("averageDailyVolume3Month", 0) or volume or 1)
        vol_ratio = volume / avg_volume if avg_volume else 1.0

        if not symbol or price < 1.0 or abs(change_pct) < min_change:
            return None

        move_score = min(abs(change_pct) / 25.0, 1.0) * 0.6
        vol_score = min((vol_ratio - 1.0) / 4.0, 1.0) * 0.3 if vol_ratio > 1 else 0
        price_score = min(price / 500.0, 1.0) * 0.1
        score = min(move_score + vol_score + price_score, 1.0)

        return ScannerHit(
            symbol=symbol,
            company_name=q.get("shortName", symbol),
            price=price,
            change_pct=change_pct,
            volume=volume,
            avg_volume=avg_volume,
            volume_ratio=vol_ratio,
            reason=reason_template.format(change=f"{change_pct:+.1f}%", vol_ratio=vol_ratio),
            score=score,
        )

    def _scan_yahoo_gainers(self) -> List[ScannerHit]:
        quotes = self._fetch_yahoo_screener("day_gainers", 25)
        return [h for q in quotes if (h := self._make_hit(q, "Top gainer {change} today", 3.0))]

    def _scan_yahoo_actives(self) -> List[ScannerHit]:
        quotes = self._fetch_yahoo_screener("most_actives", 25)
        return [h for q in quotes if (h := self._make_hit(q, "Most active {change} today, {vol_ratio:.1f}x avg vol", 1.5))]

    def _scan_yahoo_smallcap(self) -> List[ScannerHit]:
        quotes = self._fetch_yahoo_screener("small_cap_gainers", 25)
        return [h for q in quotes if (h := self._make_hit(q, "Small cap mover {change} today", 2.0))]

    def _yahoo_detail(self, symbol: str) -> Optional[dict]:
        try:
            resp = requests.get(
                f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}",
                params={"modules": "summaryProfile,price"},
                headers=YAHOO_HEADERS,
                timeout=5,
            )
            resp.raise_for_status()
            result = resp.json().get("quoteSummary", {}).get("result", [{}])[0]
            profile = result.get("summaryProfile", {})
            price_data = result.get("price", {})
            return {
                "sector": profile.get("sector", "Unknown"),
                "industry": profile.get("industry", "Unknown"),
                "description": profile.get("longBusinessSummary", "")[:300],
                "market_cap": price_data.get("marketCap", {}).get("raw", 0),
                "company_name": price_data.get("longName", symbol),
            }
        except Exception:
            return None
