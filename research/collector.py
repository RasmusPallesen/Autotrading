"""
Data collector: fetches news, SEC filings, and Reddit posts for the watchlist.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

import requests

logger = logging.getLogger(__name__)


@dataclass
class ResearchItem:
    source: str        # "news" | "sec" | "reddit"
    symbol: str        # Related ticker (best guess)
    title: str
    summary: str
    url: str
    published_at: datetime
    raw: dict


# ── News via NewsAPI ───────────────────────────────────────────────────────────

def fetch_news(symbols: List[str], api_key: str) -> List[ResearchItem]:
    """Fetch recent financial news for watchlist symbols via NewsAPI."""
    if not api_key:
        logger.warning("NEWSAPI_KEY not set — skipping news fetch")
        return []

    items = []
    query = " OR ".join(symbols[:5])  # NewsAPI free tier: keep query short

    try:
        resp = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": query,
                "language": "en",
                "sortBy": "publishedAt",
                "pageSize": 20,
                "apiKey": api_key,
            },
            timeout=10,
        )
        resp.raise_for_status()
        articles = resp.json().get("articles", [])

        for a in articles:
            # Guess which symbol this article is about
            title = a.get("title", "") or ""
            content = (a.get("description", "") or "") + title
            matched = next((s for s in symbols if s.lower() in content.lower()), symbols[0])

            items.append(ResearchItem(
                source="news",
                symbol=matched,
                title=title[:200],
                summary=(a.get("description", "") or "")[:500],
                url=a.get("url", ""),
                published_at=datetime.fromisoformat(
                    a["publishedAt"].replace("Z", "+00:00")
                ) if a.get("publishedAt") else datetime.now(timezone.utc),
                raw=a,
            ))
        logger.info("Fetched %d news articles", len(items))
    except Exception as e:
        logger.error("News fetch error: %s", e)

    return items


# ── SEC EDGAR filings ──────────────────────────────────────────────────────────

def fetch_sec_filings(symbols: List[str]) -> List[ResearchItem]:
    """
    Fetch recent SEC filings (8-K, 10-Q, 10-K) for watchlist symbols.
    Uses SEC EDGAR's free public API — no key required.
    """
    items = []

    # First resolve ticker -> CIK
    try:
        tickers_resp = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers={"User-Agent": "TradingAgent rasmus.pallesen@gmail.com"},
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
                headers={"User-Agent": "TradingAgent rasmus.pallesen@gmail.com"},
                timeout=10,
            )
            filings_resp.raise_for_status()
            data = filings_resp.json()
            recent = data.get("filings", {}).get("recent", {})

            forms = recent.get("form", [])
            dates = recent.get("filingDate", [])
            accessions = recent.get("accessionNumber", [])
            descriptions = recent.get("primaryDocument", [])

            for i, form in enumerate(forms[:20]):
                if form not in ("8-K", "10-Q", "10-K"):
                    continue
                accession = accessions[i].replace("-", "")
                url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}/{descriptions[i]}"

                items.append(ResearchItem(
                    source="sec",
                    symbol=symbol,
                    title=f"{symbol} {form} filing — {dates[i]}",
                    summary=f"SEC {form} filing for {symbol} submitted on {dates[i]}.",
                    url=url,
                    published_at=datetime.fromisoformat(dates[i]).replace(tzinfo=timezone.utc),
                    raw={"form": form, "date": dates[i], "cik": cik},
                ))

            logger.debug("Found %d SEC filings for %s", len([x for x in items if x.symbol == symbol]), symbol)
            time.sleep(0.15)  # Respect SEC rate limits

        except Exception as e:
            logger.warning("SEC fetch error for %s: %s", symbol, e)

    return items


# ── Reddit via PRAW ────────────────────────────────────────────────────────────

def fetch_reddit(symbols: List[str], client_id: str, client_secret: str) -> List[ResearchItem]:
    """
    Fetch top posts mentioning watchlist symbols from financial subreddits.
    Read-only — no posting.
    """
    if not client_id or not client_secret:
        logger.warning("REDDIT_CLIENT_ID or REDDIT_CLIENT_SECRET not set — skipping Reddit fetch")
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

                    matched = next((s for s in symbols if s.lower() in content.lower()), None)
                    if not matched:
                        continue

                    items.append(ResearchItem(
                        source="reddit",
                        symbol=matched,
                        title=title[:200],
                        summary=text[:500],
                        url=f"https://reddit.com{post.permalink}",
                        published_at=datetime.fromtimestamp(post.created_utc, tz=timezone.utc),
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
