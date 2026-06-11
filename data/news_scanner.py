"""
NewsScanner: fast keyword-based scoring of news headlines to surface hot symbols.

Runs every 5 minutes (no Claude). Symbols scoring ±4 or higher are flagged for
priority analysis in the next 30-minute deep research cycle.
"""

import logging
import sys
import os
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger(__name__)

# ── Keyword scoring tables ────────────────────────────────────────────────────

_POSITIVE = {
    # +4: strong catalysts — expect 5%+ move
    "fda approval": 4, "fda approves": 4, "fda approved": 4,
    "pdufa": 4, "breakthrough therapy": 4,
    "earnings beat": 4, "beats estimates": 4, "beats expectations": 4,
    "record revenue": 4, "record earnings": 4, "record quarter": 4,
    "guidance raised": 4, "raises guidance": 4, "raised outlook": 4,
    "major contract": 4, "major partnership": 4,
    "acquisition announced": 4, "to acquire": 4,
    "buyout": 4, "takeover bid": 4, "merger agreement": 4,
    # +2: moderate catalysts
    "partnership": 2, "collaboration": 2, "contract win": 2, "wins contract": 2,
    "analyst upgrade": 2, "upgraded to buy": 2, "price target raised": 2,
    "share buyback": 2, "buyback program": 2, "dividend increase": 2,
    "new product launch": 2, "launches": 2, "expansion": 2,
    "strong results": 2, "solid results": 2, "revenue growth": 2,
    "market share gain": 2, "positive data": 2, "clinical success": 2,
    "regulatory approval": 2, "cleared by": 2,
    # +1: weak positive signals
    "growth": 1, "momentum": 1, "surges": 1, "rally": 1,
    "positive": 1, "outperform": 1, "innovation": 1, "milestone": 1,
}

_NEGATIVE = {
    # -4: strong negative catalysts
    "sec investigation": 4, "sec charges": 4, "sec subpoena": 4,
    "fda rejection": 4, "fda rejects": 4, "complete response letter": 4,
    "earnings miss": 4, "misses estimates": 4, "misses expectations": 4,
    "guidance cut": 4, "cuts guidance": 4, "lowered outlook": 4,
    "guidance withdrawn": 4, "withdraws guidance": 4,
    "bankruptcy": 4, "chapter 11": 4, "insolvency": 4,
    "class action": 4, "securities fraud": 4,
    "product recall": 4, "safety recall": 4,
    "delisted": 4, "nasdaq delisting": 4,
    # -2: moderate negatives
    "downgrade": 2, "downgraded to sell": 2, "price target cut": 2,
    "recall": 2, "investigation": 2, "probe": 2, "scrutiny": 2,
    "cfo resigns": 2, "ceo resigns": 2, "executive departure": 2,
    "debt warning": 2, "credit downgrade": 2, "covenant breach": 2,
    "lawsuit": 2, "litigation": 2,
    "revenue miss": 2, "disappointing results": 2, "weak demand": 2,
    "inventory build": 2, "supply chain issues": 2,
    # -1: weak negative signals
    "concern": 1, "risk": 1, "warning": 1, "delay": 1,
    "loss": 1, "decline": 1, "disappoints": 1, "falls short": 1,
    "volatile": 1, "uncertainty": 1,
}

_MIN_HOT_SCORE = 4   # abs(score) threshold to be considered "hot"
_SEEN_URL_MAX  = 6000


@dataclass
class NewsHit:
    symbol: str
    score: int            # net keyword score (positive = bullish, negative = bearish)
    sentiment: str        # "BULLISH" | "BEARISH" | "NEUTRAL"
    top_headline: str
    top_headline_url: str
    article_count: int    # new articles contributed to this score
    published_at: datetime
    scanned_at: datetime


def _score_text(text: str) -> int:
    """Return net keyword score for a headline/summary string."""
    lower = text.lower()
    score = 0
    for phrase, pts in _POSITIVE.items():
        if phrase in lower:
            score += pts
    for phrase, pts in _NEGATIVE.items():
        if phrase in lower:
            score -= pts
    return max(-12, min(12, score))  # cap at ±12


class NewsScanner:
    """
    Lightweight news scanner: scores RSS headlines with keyword rules (no Claude).
    Identifies symbols with significant news momentum for priority deep analysis.
    """

    def __init__(self):
        self._seen_urls: set = set()

    def scan(self, news_items) -> List[NewsHit]:
        """
        Score a list of ResearchItem news objects and return hot symbols.
        Deduplicates by URL so the same article is never double-counted.
        """
        now = datetime.now(timezone.utc)

        # Group by symbol, dedup URLs
        symbol_scores: dict = {}   # symbol -> {"score": int, "best_score": int, "headline": str, "url": str, "pub": dt, "count": int}

        for item in news_items:
            if item.source != "news":
                continue
            if item.url in self._seen_urls:
                continue

            text  = f"{item.title} {item.summary}"
            score = _score_text(text)
            sym   = item.symbol

            if sym not in symbol_scores:
                symbol_scores[sym] = {
                    "score": 0, "best_abs": 0,
                    "headline": "", "url": "", "pub": now, "count": 0,
                }

            entry = symbol_scores[sym]
            entry["score"] += score
            entry["count"] += 1

            if abs(score) > entry["best_abs"]:
                entry["best_abs"] = abs(score)
                entry["headline"] = item.title
                entry["url"]      = item.url
                entry["pub"]      = item.published_at

        # Update seen URLs
        for item in news_items:
            if item.source == "news":
                self._seen_urls.add(item.url)

        # Evict oldest entries if cache grows too large (simple size cap)
        if len(self._seen_urls) > _SEEN_URL_MAX:
            evict_n = len(self._seen_urls) - _SEEN_URL_MAX
            for url in list(self._seen_urls)[:evict_n]:
                self._seen_urls.discard(url)

        # Build NewsHit list for symbols above threshold
        hits: List[NewsHit] = []
        for sym, entry in symbol_scores.items():
            net = max(-12, min(12, entry["score"]))
            if abs(net) < _MIN_HOT_SCORE:
                continue
            sentiment = "BULLISH" if net > 0 else "BEARISH" if net < 0 else "NEUTRAL"
            hits.append(NewsHit(
                symbol=sym,
                score=net,
                sentiment=sentiment,
                top_headline=entry["headline"],
                top_headline_url=entry["url"],
                article_count=entry["count"],
                published_at=entry["pub"],
                scanned_at=now,
            ))

        hits.sort(key=lambda h: abs(h.score), reverse=True)
        logger.info(
            "[NewsScanner] %d symbols scanned → %d hot (%s)",
            len(symbol_scores), len(hits),
            [(h.symbol, h.score) for h in hits[:5]],
        )
        return hits

    def get_hot_symbols(self, hits: List[NewsHit], min_score: int = _MIN_HOT_SCORE) -> List[str]:
        """Return symbol names for hits meeting the score threshold."""
        return [h.symbol for h in hits if abs(h.score) >= min_score]
