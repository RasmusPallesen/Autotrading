"""
Research agent main loop.
Collects data, scans market, analyses, writes signals to DB, sends email alerts.
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
from data.earnings_calendar import EarningsCalendar
from data.insider_monitor import InsiderMonitor
from data.iv_monitor import IVMonitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            os.path.join(os.getcwd(), "logs", "research.log"),
            encoding="utf-8"
        ),
    ],
)
logger = logging.getLogger("research_agent")

RESEARCH_INTERVAL   = int(os.getenv("RESEARCH_INTERVAL_SECONDS", "900"))
CONVICTION_THRESHOLD = float(os.getenv("CONVICTION_THRESHOLD", "0.70"))
SCANNER_MIN_SCORE   = float(os.getenv("SCANNER_MIN_SCORE", "0.50"))
SCANNER_MAX_HITS    = int(os.getenv("SCANNER_MAX_HITS", "10"))


def is_market_open() -> bool:
    """Returns True if NYSE is currently open (Mon-Fri 13:30-20:00 UTC)."""
    from datetime import time as _dtime
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    return _dtime(13, 30) <= now.time() <= _dtime(20, 0)


def run_research_cycle(analyst, store, scanner, earnings_cal=None, insider_monitor=None, iv_monitor=None):
    logger.info("=== Research cycle starting ===")

    # 1. Base watchlist
    base_symbols = config.watchlist.all_symbols
    logger.info("Base watchlist: %d symbols", len(base_symbols))

    # 2. Market scanner — discover new opportunities (market hours only)
    scanner_hits = []
    binary_catalyst_symbols = []
    discovered_symbols = []

    if is_market_open():
        try:
            scanner_hits = scanner.scan(max_results=SCANNER_MAX_HITS)
        except Exception as e:
            logger.warning("Market scanner error: %s", e)
    else:
        logger.info("Scanner skipped -- market closed")

    # 2b. Earnings calendar check
    if earnings_cal:
        try:
            earnings_events = earnings_cal.get_events(base_symbols)
            pre = [s for s, ev in earnings_events.items() if ev.is_pre_earnings_window]
            upcoming = [f"{s} ({ev.days_until}d)" for s, ev in earnings_events.items() if ev.days_until <= 7]
            if pre:
                logger.warning("PRE-EARNINGS this week: %s", pre)
            if upcoming:
                logger.info("Upcoming earnings (<=7 days): %s", upcoming)
            post = [s for s, ev in earnings_events.items() if ev.is_post_earnings]
            if post:
                logger.info("POST-EARNINGS reaction symbols: %s", post)
                for sym in post:
                    if sym not in discovered_symbols:
                        discovered_symbols.append(sym)
        except Exception as e:
            logger.warning("Earnings calendar error: %s", e)

    # Process scanner hits
    from collector import ResearchItem
    scanner_items = []

    for hit in scanner_hits:
        if hit.score >= SCANNER_MIN_SCORE:
            is_binary = abs(hit.change_pct) >= 20
            if is_binary:
                binary_catalyst_symbols.append(hit.symbol)
                catalyst_note = (
                    f"BINARY CATALYST ALERT: {hit.change_pct:+.1f}% single-day move. "
                    "This magnitude strongly suggests a fundamental catalyst event. "
                    "Technical indicators are unreliable today. "
                )
            else:
                catalyst_note = ""

            if hit.symbol not in base_symbols:
                discovered_symbols.append(hit.symbol)

            detail = scanner.get_symbol_detail(hit.symbol) or {}
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
        logger.info("BINARY CATALYST STOCKS detected (>20%% move): %s", binary_catalyst_symbols)

    # Combined symbol list
    all_symbols = list(dict.fromkeys(base_symbols + discovered_symbols))
    logger.info("Total symbols to research this cycle: %d", len(all_symbols))

    # 2b. IV spike monitor — runs after market close for end-of-day data
    iv_items = []
    if iv_monitor and not is_market_open():
        try:
            from collector import ResearchItem
            # Get earnings symbols to distinguish explained vs unexplained IV
            earnings_soon = []
            if earnings_cal:
                try:
                    ev = earnings_cal.get_events(all_symbols)
                    earnings_soon = [s for s, e in ev.items() if e.days_until <= 7]
                except Exception:
                    pass

            iv_spikes = iv_monitor.scan(all_symbols, earnings_symbols=earnings_soon)

            for snap in iv_spikes:
                has_earnings = snap.symbol in earnings_soon
                summary = snap.to_research_summary(has_earnings)
                iv_items.append(ResearchItem(
                    source="iv_spike",
                    symbol=snap.symbol,
                    title=(
                        f"[IV SPIKE{'|EARNINGS' if has_earnings else '|UNUSUAL'}] "
                        f"{snap.symbol}: IV rank {snap.iv_rank*100:.0f}% "
                        f"({snap.signal_strength})"
                    ),
                    summary=summary,
                    url=f"https://finance.yahoo.com/quote/{snap.symbol}/options",
                    published_at=datetime.now(timezone.utc),
                    raw={
                        "iv_rank": snap.iv_rank,
                        "current_iv": snap.current_iv,
                        "put_call_ratio": snap.put_call_ratio,
                        "signal_type": snap.signal_type,
                        "has_earnings": has_earnings,
                    },
                ))
                if not has_earnings:
                    logger.warning(
                        "UNEXPLAINED IV SPIKE [%s]: rank=%.0f%% type=%s -- possible catalyst ahead",
                        snap.symbol, snap.iv_rank * 100, snap.signal_type,
                    )
        except Exception as e:
            logger.warning("IV monitor error: %s", e)
    elif is_market_open():
        iv_items = []
        logger.debug("IV monitor skipped -- runs after market close only")

    # 2c. Insider trading monitor
    insider_items = []
    if insider_monitor:
        try:
            from collector import ResearchItem
            significant_buys = insider_monitor.get_significant_buys(
                all_symbols, days_back=14
            )
            if significant_buys:
                logger.info(
                    "Insider Monitor: %d significant buys found",
                    len(significant_buys),
                )
            for txn in significant_buys:
                logger.info(
                    "INSIDER BUY: %s -- %s (%s) bought $%,.0f worth",
                    txn.symbol, txn.insider_name,
                    txn.insider_title, txn.total_value,
                )
                insider_items.append(ResearchItem(
                    source="insider",
                    symbol=txn.symbol,
                    title=(
                        f"[INSIDER BUY] {txn.insider_name} ({txn.insider_title}) "
                        f"bought ${txn.total_value:,.0f} of {txn.symbol}"
                    ),
                    summary=txn.to_research_summary(),
                    url=txn.form_url,
                    published_at=datetime.combine(
                        txn.filing_date,
                        datetime.min.time(),
                        tzinfo=timezone.utc,
                    ),
                    raw={
                        "shares": txn.shares,
                        "price": txn.price_per_share,
                        "value": txn.total_value,
                        "signal_strength": txn.signal_strength,
                    },
                ))
        except Exception as e:
            logger.warning("Insider monitor error: %s", e)

    # 3. Collect from all sources
    news_items = fetch_news(
        all_symbols,
        api_key=os.getenv("MASSIVE_API_KEY", os.getenv("BENZINGA_API_KEY", "")),
    )

    symbols_with_news = {item.symbol for item in news_items}
    sec_priority = [
        s for s in all_symbols
        if s not in symbols_with_news or s in discovered_symbols
    ]
    sec_items = fetch_sec_filings(sec_priority) if sec_priority else []

    reddit_items = fetch_reddit(
        all_symbols,
        client_id=os.getenv("REDDIT_CLIENT_ID", ""),
        client_secret=os.getenv("REDDIT_CLIENT_SECRET", ""),
    )

    all_items = news_items + sec_items + reddit_items + scanner_items + insider_items + iv_items
    logger.info(
        "Collected %d items -- news=%d, SEC=%d, Reddit=%d, Scanner=%d, Insider=%d, IV=%d",
        len(all_items), len(news_items), len(sec_items),
        len(reddit_items), len(scanner_items), len(insider_items), len(iv_items),
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

    logger.info("=== Research cycle complete ===")


def main():
    os.makedirs(os.path.join(os.getcwd(), "logs"), exist_ok=True)

    logger.info("Research Agent starting up")
    logger.info("  Interval: %ds (%d min)", RESEARCH_INTERVAL, RESEARCH_INTERVAL // 60)
    logger.info("  Conviction threshold: %.0f%%", CONVICTION_THRESHOLD * 100)
    logger.info("  Watchlist: %d symbols", len(config.watchlist.all_symbols))

    analyst      = ResearchAnalyst(config.anthropic)
    store        = ResearchStore()
    earnings_cal = EarningsCalendar()
    insider_monitor = InsiderMonitor()
    iv_monitor = IVMonitor()
    scanner      = MarketScanner(
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
            run_research_cycle(analyst, store, scanner, earnings_cal, insider_monitor, iv_monitor)
        except Exception as e:
            logger.exception("Unhandled error in research cycle: %s", e)

        if running:
            if is_market_open():
                interval = RESEARCH_INTERVAL
                logger.info("Market open -- next cycle in %ds", interval)
            else:
                interval = 7200
                logger.info("Market closed -- next cycle in 2h")
            time.sleep(interval)

    store.close()
    logger.info("Research agent shut down cleanly.")


if __name__ == "__main__":
    main()
