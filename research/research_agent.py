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
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data.earnings_calendar import EarningsCalendar
from analyst import ResearchAnalyst
from emailer import send_alert
from storage.research_store import ResearchStore
from data.market_scanner import MarketScanner

# Create logs directory if it doesn't exist
_log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
os.makedirs(_log_dir, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            os.path.join(_log_dir, "research.log"),
            encoding="utf-8"
        ),
    ],
)
logger = logging.getLogger("research_agent")

RESEARCH_INTERVAL  = int(os.getenv("RESEARCH_INTERVAL_SECONDS", "900"))
CONVICTION_THRESHOLD = float(os.getenv("CONVICTION_THRESHOLD", "0.70"))
SCANNER_MIN_SCORE  = float(os.getenv("SCANNER_MIN_SCORE", "0.50"))
SCANNER_MAX_HITS   = int(os.getenv("SCANNER_MAX_HITS", "10"))



def is_market_open() -> bool:
    """Returns True if NYSE is currently open (Mon-Fri 13:30-20:00 UTC)."""
    from datetime import datetime, timezone, time as _dtime
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    return _dtime(13, 30) <= now.time() <= _dtime(20, 0)


def run_research_cycle(analyst: ResearchAnalyst, store: ResearchStore, scanner: MarketScanner, earnings_cal=None):
    logger.info("=== Research cycle starting ===")

    # 1. Base watchlist
    base_symbols = config.watchlist.all_symbols
    logger.info("Base watchlist: %d symbols", len(base_symbols))

    # 2. Market scanner — discover new opportunities
    discovered_symbols = []
    scanner_hits = []
    binary_catalyst_symbols = []
    if is_market_open():
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

    # 2b. Check earnings calendar — log upcoming events
    if earnings_cal:
        try:
            earnings_events = earnings_cal.get_events(all_symbols)
            pre = [s for s, ev in earnings_events.items() if ev.is_pre_earnings_window]
            upcoming = [f"{s} ({ev.days_until}d)" for s, ev in earnings_events.items() if ev.days_until <= 7]
            if pre:
                logger.warning("PRE-EARNINGS this week: %s", pre)
            if upcoming:
                logger.info("Upcoming earnings (<=7 days): %s", upcoming)
            # Add post-earnings symbols to SEC fetch priority
            post = [s for s, ev in earnings_events.items() if ev.is_post_earnings]
            if post:
                logger.info("POST-EARNINGS reaction symbols: %s", post)
                for sym in post:
                    if sym not in discovered_symbols:
                        discovered_symbols.append(sym)
        except Exception as e:
            logger.warning("Earnings calendar error in research: %s", e)

    # 3. Collect from all sources
    # News first — Benzinga via Massive covers large/mid caps well
    news_items = fetch_news(
        all_symbols,
        api_key=os.getenv("MASSIVE_API_KEY", os.getenv("BENZINGA_API_KEY", "")),
    )

    # SEC filings — only for symbols with no Benzinga coverage
    # Always include scanner discoveries regardless of news coverage
    symbols_with_news = {item.symbol for item in news_items}
    sec_priority = [
        s for s in all_symbols
        if s not in symbols_with_news or s in discovered_symbols
    ]
    if sec_priority:
        logger.info(
            "SEC filing fetch for %d symbols (no Benzinga coverage or scanner discovery): %s",
            len(sec_priority), sec_priority[:10],
        )
    sec_items = fetch_sec_filings(sec_priority) if sec_priority else []

    reddit_items  = fetch_reddit(
        all_symbols,
        client_id=os.getenv("REDDIT_CLIENT_ID", ""),
        client_secret=os.getenv("REDDIT_CLIENT_SECRET", ""),
    )

    # Add scanner hits as synthetic research items
    from collector import ResearchItem
    scanner_items = []
    binary_catalyst_symbols = []  # Track stocks with >20% moves

    for hit in scanner_hits:
        if hit.score >= SCANNER_MIN_SCORE:
            detail = scanner.get_symbol_detail(hit.symbol) or {}

            # Detect binary catalyst moves (>20% single day)
            is_binary = abs(hit.change_pct) >= 20
            if is_binary:
                binary_catalyst_symbols.append(hit.symbol)
                catalyst_note = (
                    f"BINARY CATALYST ALERT: {hit.change_pct:+.1f}% single-day move. "
                    "This magnitude strongly suggests a fundamental catalyst event "
                    "(clinical trial data, FDA decision, earnings surprise, M&A, "
                    "major contract). Technical indicators are unreliable today. "
                    "Prioritise fundamental research to confirm catalyst. "
                )
            else:
                catalyst_note = ""

            summary = (
                f"{detail.get('company_name', hit.symbol)} "
                f"({detail.get('sector','?')} / {detail.get('industry','?')}): "
                f"{hit.reason}. Price ${hit.price:.2f}, change {hit.change_pct:+.1f}% today. "
                f"{catalyst_note}"
                f"{detail.get('description', '')}"
            )
            scanner_items.append(ResearchItem(
                source="scanner",
                symbol=hit.symbol,
                title=f"[SCANNER{'|CATALYST' if is_binary else ''}] {hit.symbol}: {hit.reason}",
                summary=summary[:800],
                url=f"https://finance.yahoo.com/quote/{hit.symbol}",
                published_at=datetime.now(timezone.utc),
                raw={"score": hit.score, "change_pct": hit.change_pct, "binary": is_binary},
            ))

    if binary_catalyst_symbols:
        logger.info(
            "BINARY CATALYST STOCKS detected (>20%% move): %s -- "
            "technical indicators unreliable, prioritising fundamental research",
            binary_catalyst_symbols,
        )

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
        # Binary catalyst stocks get 6h TTL (longer — catalyst effect persists)
        # Other scanner discoveries get 2h TTL
        # Regular watchlist gets 4h TTL
        if r.symbol in binary_catalyst_symbols:
            ttl = 6
        elif r.symbol in discovered_symbols:
            ttl = 2
        else:
            ttl = 4
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
    earnings_cal = EarningsCalendar()
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
            run_research_cycle(analyst, store, scanner, earnings_cal)
        except Exception as e:
            logger.exception("Unhandled error in research cycle: %s", e)

        if running:
            if is_market_open():
                interval = RESEARCH_INTERVAL
                logger.info("Market open -- next research cycle in %ds", interval)
            else:
                interval = 7200  # 2 hours outside market hours
                logger.info("Market closed -- next research cycle in 2h")
            time.sleep(interval)

    store.close()
    logger.info("Research agent shut down cleanly.")


if __name__ == "__main__":
    main()
