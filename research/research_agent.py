"""
Research agent main loop.
Collects data, scans market for opportunities, analyses, writes signals to DB, sends alerts.
"""

import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from collector import fetch_news, fetch_sec_filings, fetch_reddit
from analyst import ResearchAnalyst
from emailer import send_alert
from storage.research_store import ResearchStore
from data.market_scanner import MarketScanner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "research.log"),
            encoding="utf-8"
        ),
    ],
)
logger = logging.getLogger("research_agent")

RESEARCH_INTERVAL  = int(os.getenv("RESEARCH_INTERVAL_SECONDS", "900"))
CONVICTION_THRESHOLD = float(os.getenv("CONVICTION_THRESHOLD", "0.70"))
SCANNER_MIN_SCORE  = float(os.getenv("SCANNER_MIN_SCORE", "0.50"))
SCANNER_MAX_HITS   = int(os.getenv("SCANNER_MAX_HITS", "10"))


def run_research_cycle(analyst: ResearchAnalyst, store: ResearchStore, scanner: MarketScanner):
    logger.info("=== Research cycle starting ===")

    # 1. Base watchlist
    base_symbols = config.watchlist.all_symbols
    logger.info("Base watchlist: %d symbols", len(base_symbols))

    # 2. Market scanner — discover new opportunities
    discovered_symbols = []
    scanner_hits = []
    try:
        scanner_hits = scanner.scan(max_results=SCANNER_MAX_HITS)
        discovered_symbols = [
            h.symbol for h in scanner_hits
            if h.score >= SCANNER_MIN_SCORE and h.symbol not in base_symbols
        ]
        if discovered_symbols:
            logger.info(
                "Scanner discovered %d new symbols: %s",
                len(discovered_symbols), discovered_symbols,
            )
    except Exception as e:
        logger.warning("Market scanner error: %s", e)

    # Combined symbol list: base + discovered
    all_symbols = list(dict.fromkeys(base_symbols + discovered_symbols))
    logger.info("Total symbols to research this cycle: %d", len(all_symbols))

    # 3. Collect from all sources
    news_items    = fetch_news(all_symbols, api_key=os.getenv("NEWSAPI_KEY", ""))
    sec_items     = fetch_sec_filings(all_symbols)
    reddit_items  = fetch_reddit(
        all_symbols,
        client_id=os.getenv("REDDIT_CLIENT_ID", ""),
        client_secret=os.getenv("REDDIT_CLIENT_SECRET", ""),
    )

    # Add scanner hits as synthetic research items
    from collector import ResearchItem
    scanner_items = []
    for hit in scanner_hits:
        if hit.score >= SCANNER_MIN_SCORE:
            # Optionally enrich with Yahoo detail
            detail = scanner.get_symbol_detail(hit.symbol) or {}
            summary = (
                f"{detail.get('company_name', hit.symbol)} ({detail.get('sector','?')} / "
                f"{detail.get('industry','?')}): {hit.reason}. "
                f"Price ${hit.price:.2f}, change {hit.change_pct:+.1f}% today. "
                f"{detail.get('description', '')}"
            )
            scanner_items.append(ResearchItem(
                source="scanner",
                symbol=hit.symbol,
                title=f"[SCANNER] {hit.symbol}: {hit.reason}",
                summary=summary[:500],
                url=f"https://finance.yahoo.com/quote/{hit.symbol}",
                published_at=datetime.now(timezone.utc),
                raw={"score": hit.score, "change_pct": hit.change_pct},
            ))

    all_items = news_items + sec_items + reddit_items + scanner_items
    logger.info(
        "Collected %d items -- news=%d, SEC=%d, Reddit=%d, Scanner=%d",
        len(all_items), len(news_items), len(sec_items), len(reddit_items), len(scanner_items),
    )

    if not all_items:
        logger.warning("No research items collected this cycle")
        return

    # 4. Analyse with Claude
    reports = analyst.analyse_all(all_items, all_symbols)
    high_conviction = [r for r in reports if r.conviction >= CONVICTION_THRESHOLD]

    logger.info(
        "Analysis complete -- %d reports, %d high-conviction",
        len(reports), len(high_conviction),
    )

    # 5. Write signals to DB
    for r in reports:
        # Give scanner-discovered stocks a slightly shorter TTL
        ttl = 2 if r.symbol in discovered_symbols else 4
        logger.info(
            "[%s] %s | conviction=%.0f%% | action=%s | %s",
            r.symbol, r.overall_sentiment, r.conviction * 100,
            r.recommended_action, r.summary[:300],
        )
        store.write_signal(
            symbol=r.symbol,
            sentiment=r.overall_sentiment,
            conviction=r.conviction,
            recommended_action=r.recommended_action,
            summary=r.summary,
            key_points=r.key_points,
            risk_factors=r.risk_factors,
            sources_used=r.sources_used,
            ttl_hours=ttl,
        )

    # 6. Email high-conviction signals
    if high_conviction:
        send_alert(high_conviction)
        logger.info("Alert sent for: %s", [r.symbol for r in high_conviction])

    # 7. Log scanner summary
    if scanner_hits:
        logger.info("=== Scanner summary ===")
        for h in scanner_hits[:5]:
            logger.info(
                "  [%s] %+.1f%% | score=%.2f | %s",
                h.symbol, h.change_pct, h.score, h.reason,
            )

    logger.info("=== Research cycle complete ===")


def main():
    logger.info("Research Agent starting up")
    logger.info("  Interval: %ds (%d min)", RESEARCH_INTERVAL, RESEARCH_INTERVAL // 60)
    logger.info("  Conviction threshold: %.0f%%", CONVICTION_THRESHOLD * 100)
    logger.info("  Scanner min score: %.0f%%", SCANNER_MIN_SCORE * 100)
    logger.info("  Base watchlist: %d symbols", len(config.watchlist.all_symbols))

    analyst = ResearchAnalyst(config.anthropic)
    store   = ResearchStore()
    scanner = MarketScanner(
        alpaca_api_key=os.getenv("ALPACA_API_KEY", ""),
        alpaca_secret_key=os.getenv("ALPACA_SECRET_KEY", ""),
        paper=os.getenv("ALPACA_PAPER", "true").lower() == "true",
    )

    running = True
    def _shutdown(sig, frame):
        nonlocal running
        logger.warning("Shutdown signal -- finishing current cycle.")
        running = False

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    while running:
        try:
            run_research_cycle(analyst, store, scanner)
        except Exception as e:
            logger.exception("Unhandled error in research cycle: %s", e)

        if running:
            logger.info("Sleeping %ds until next cycle...", RESEARCH_INTERVAL)
            time.sleep(RESEARCH_INTERVAL)

    store.close()
    logger.info("Research agent shut down cleanly.")


if __name__ == "__main__":
    main()
