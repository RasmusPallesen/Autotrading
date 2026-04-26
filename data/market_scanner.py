"""
Market scanner: discovers opportunities across the broad market.
Uses Yahoo Finance (free, no API key required) to find:
- Top gainers today
- Unusual volume spikes
- Most active stocks
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

import requests

logger = logging.getLogger(__name__)

YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


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
    """Scans the broad market for unusual activity using Yahoo Finance."""

    def __init__(self, alpaca_api_key: str = "", alpaca_secret_key: str = "", paper: bool = True):
        # Alpaca credentials kept for future use but not required for scanning
        self.api_key = alpaca_api_key
        self.secret_key = alpaca_secret_key

    def scan(self, max_results: int = 20) -> List[ScannerHit]:
        """Run all scans and return deduplicated ranked hits."""
        all_hits: dict = {}

        for scan_fn in [
            self._scan_top_gainers,
            self._scan_most_active,
            self._scan_unusual_volume,
        ]:
            try:
                for hit in scan_fn():
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
                logger.warning("Scan error in %s: %s", scan_fn.__name__, e)

        ranked = sorted(all_hits.values(), key=lambda h: h.score, reverse=True)
        results = ranked[:max_results]

        logger.info("Market scanner found %d hits", len(results))
        for h in results[:5]:
            logger.info(
                "  [%s] %+.1f%% | vol %.1fx | score=%.2f | %s",
                h.symbol, h.change_pct, h.volume_ratio, h.score, h.reason,
            )
        return results

    def _fetch_yahoo_screener(self, screen_id: str, count: int = 25) -> List[dict]:
        """Fetch a Yahoo Finance predefined screener."""
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
        """Convert a Yahoo Finance quote dict to a ScannerHit."""
        symbol = q.get("symbol", "")
        change_pct = float(q.get("regularMarketChangePercent", 0))
        price = float(q.get("regularMarketPrice", 0))
        volume = float(q.get("regularMarketVolume", 0))
        avg_volume = float(q.get("averageDailyVolume3Month", 0) or volume or 1)
        vol_ratio = volume / avg_volume if avg_volume else 1.0

        if not symbol or price < 1.0:
            return None
        if abs(change_pct) < min_change:
            return None
        # Skip very large caps with tiny moves (just noise)
        market_cap = float(q.get("marketCap", 0) or 0)
        if market_cap > 2e12 and abs(change_pct) < 2.0:
            return None

        # Score: weighted combination of move size and volume ratio
        move_score = min(abs(change_pct) / 25.0, 1.0) * 0.6
        vol_score = min((vol_ratio - 1.0) / 4.0, 1.0) * 0.3 if vol_ratio > 1 else 0
        price_score = min(price / 500.0, 1.0) * 0.1
        score = min(move_score + vol_score + price_score, 1.0)

        direction = f"+{change_pct:.1f}%" if change_pct > 0 else f"{change_pct:.1f}%"
        reason = reason_template.format(
            change=direction,
            vol_ratio=vol_ratio,
            symbol=symbol,
        )

        return ScannerHit(
            symbol=symbol,
            company_name=q.get("shortName", symbol),
            price=price,
            change_pct=change_pct,
            volume=volume,
            avg_volume=avg_volume,
            volume_ratio=vol_ratio,
            reason=reason,
            score=score,
        )

    def _scan_top_gainers(self) -> List[ScannerHit]:
        """Top gaining stocks today."""
        quotes = self._fetch_yahoo_screener("day_gainers", 25)
        hits = []
        for q in quotes:
            hit = self._make_hit(q, "Top gainer {change} today", min_change=3.0)
            if hit:
                if hit.change_pct > 20:
                    hit.reason = f"EXPLOSIVE move {hit.change_pct:+.1f}% -- investigate catalyst"
                    hit.score = min(hit.score + 0.2, 1.0)
                elif hit.change_pct > 10:
                    hit.reason = f"Strong gainer {hit.change_pct:+.1f}% today"
                    hit.score = min(hit.score + 0.1, 1.0)
                hits.append(hit)
        logger.info("Top gainers scan: %d hits", len(hits))
        return hits

    def _scan_most_active(self) -> List[ScannerHit]:
        """Most actively traded stocks today."""
        quotes = self._fetch_yahoo_screener("most_actives", 25)
        hits = []
        for q in quotes:
            hit = self._make_hit(q, "Most active stock, {change} today with {vol_ratio:.1f}x avg volume", min_change=1.5)
            if hit:
                hits.append(hit)
        logger.info("Most active scan: %d hits", len(hits))
        return hits

    def _scan_unusual_volume(self) -> List[ScannerHit]:
        """Stocks with unusual volume — uses small_cap_gainers as proxy."""
        quotes = self._fetch_yahoo_screener("small_cap_gainers", 25)
        hits = []
        for q in quotes:
            hit = self._make_hit(q, "Small cap mover {change} today, {vol_ratio:.1f}x avg volume", min_change=2.0)
            if hit:
                hits.append(hit)
        logger.info("Small cap scan: %d hits", len(hits))
        return hits

    def get_symbol_detail(self, symbol: str) -> Optional[dict]:
        """Fetch basic company info for a discovered symbol."""
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