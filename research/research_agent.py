"""
Research agent main loop.
Collects data, scans market, analyses, writes signals to DB, sends email alerts.

Caching added for all external data sources:
- Earnings calendar:    6h TTL in-memory  (dates change weekly, not per-cycle)
- Insider monitor:      4h TTL in-memory  (SEC Form 4s filed within 2 business days)
- IV monitor:           daily TTL         (markets closed overnight, data static)
- Scanner symbol detail: session cache    (sector/description never changes)
- Motley Fool URLs:     24h TTL on disk   (avoids re-fetching same articles)
- Claude analysis:      4h TTL on disk    (handled in analyst.py)
"""

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone, timedelta

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
from data.motley_fool_fetcher import fetch_motley_fool
from data.breakout_screener import BreakoutScreener
from data.institutional_monitor import InstitutionalMonitor, get_ticker_cik_map
from data.clinical_catalyst_calendar import ClinicalCatalystCalendar

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

RESEARCH_INTERVAL    = int(os.getenv("RESEARCH_INTERVAL_SECONDS", "900"))
CONVICTION_THRESHOLD = float(os.getenv("CONVICTION_THRESHOLD", "0.70"))
SCANNER_MIN_SCORE    = float(os.getenv("SCANNER_MIN_SCORE", "0.50"))
SCANNER_MAX_HITS     = int(os.getenv("SCANNER_MAX_HITS", "10"))

EARNINGS_CACHE_TTL_HOURS = int(os.getenv("EARNINGS_CACHE_TTL_HOURS", "6"))
INSIDER_CACHE_TTL_HOURS  = int(os.getenv("INSIDER_CACHE_TTL_HOURS", "4"))


def is_market_open() -> bool:
    """Returns True if NYSE is currently open (Mon-Fri 13:30-20:00 UTC)."""
    from datetime import time as _dtime
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    return _dtime(13, 30) <= now.time() <= _dtime(20, 0)


# ── Earnings calendar cache ────────────────────────────────────────────────────

_earnings_cache: dict = {}
_earnings_cache_filled_at: datetime | None = None


def _get_earnings_events(earnings_cal, symbols: list) -> dict:
    """Return earnings events, refreshing at most once every 6 hours."""
    global _earnings_cache, _earnings_cache_filled_at

    now = datetime.now(timezone.utc)
    cache_age_h = (
        (now - _earnings_cache_filled_at).total_seconds() / 3600
        if _earnings_cache_filled_at else float("inf")
    )

    if _earnings_cache and cache_age_h < EARNINGS_CACHE_TTL_HOURS:
        logger.debug("Earnings cache HIT (age=%.1fh)", cache_age_h)
        return {s: _earnings_cache[s] for s in symbols if s in _earnings_cache}

    logger.info("Earnings cache MISS (age=%.1fh) -- fetching", cache_age_h)
    try:
        events = earnings_cal.get_events(symbols)
        _earnings_cache = events
        _earnings_cache_filled_at = now
        logger.info("Earnings cache refreshed: %d events", len(events))
        return events
    except Exception as e:
        logger.warning("Earnings calendar fetch error: %s", e)
        if _earnings_cache:
            logger.info("Returning stale earnings cache after fetch error")
            return {s: _earnings_cache[s] for s in symbols if s in _earnings_cache}
        return {}


# ── Insider monitor cache ──────────────────────────────────────────────────────

_insider_cache: list = []
_insider_cache_filled_at: datetime | None = None
_insider_cache_symbols: list = []


def _get_insider_buys(insider_monitor, symbols: list) -> list:
    """Return significant insider buys, refreshing at most once every 4 hours."""
    global _insider_cache, _insider_cache_filled_at, _insider_cache_symbols

    now = datetime.now(timezone.utc)
    cache_age_h = (
        (now - _insider_cache_filled_at).total_seconds() / 3600
        if _insider_cache_filled_at else float("inf")
    )
    symbols_unchanged = set(symbols) == set(_insider_cache_symbols)

    if _insider_cache_filled_at and cache_age_h < INSIDER_CACHE_TTL_HOURS and symbols_unchanged:
        logger.debug("Insider cache HIT (age=%.1fh, %d txns)", cache_age_h, len(_insider_cache))
        return _insider_cache

    logger.info("Insider cache MISS (age=%.1fh) -- fetching from SEC EDGAR", cache_age_h)
    try:
        buys = insider_monitor.get_significant_buys(symbols, days_back=14)
        _insider_cache = buys
        _insider_cache_filled_at = now
        _insider_cache_symbols = list(symbols)
        logger.info("Insider cache refreshed: %d significant buys", len(buys))
        return buys
    except Exception as e:
        logger.warning("Insider monitor fetch error: %s", e)
        if _insider_cache:
            logger.info("Returning stale insider cache after fetch error")
        return _insider_cache


# ── IV monitor cache ───────────────────────────────────────────────────────────

_iv_cache: list = []
_iv_cache_date: str | None = None


def _get_iv_spikes(iv_monitor, symbols: list, earnings_soon: list) -> list:
    """
    Return IV spikes, cached for the full calendar day.
    Overnight re-runs reuse the same data since markets are closed.
    """
    global _iv_cache, _iv_cache_date

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if _iv_cache and _iv_cache_date == today:
        logger.debug("IV cache HIT (date=%s, %d spikes)", today, len(_iv_cache))
        return _iv_cache

    logger.info("IV cache MISS (date=%s) -- scanning options chains", today)
    try:
        spikes = iv_monitor.scan(symbols, earnings_symbols=earnings_soon)
        _iv_cache = spikes
        _iv_cache_date = today
        logger.info("IV cache refreshed: %d spikes found", len(spikes))
        return spikes
    except Exception as e:
        logger.warning("IV monitor scan error: %s", e)
        return _iv_cache


# ── Scanner symbol detail cache ────────────────────────────────────────────────

_symbol_detail_cache: dict = {}


def _get_symbol_detail(scanner, symbol: str) -> dict:
    """Return scanner symbol detail from session-scoped in-memory cache."""
    if symbol not in _symbol_detail_cache:
        _symbol_detail_cache[symbol] = scanner.get_symbol_detail(symbol) or {}
        logger.debug("Symbol detail cached for %s", symbol)
    return _symbol_detail_cache[symbol]


# ── Motley Fool URL disk cache ─────────────────────────────────────────────────

def _fool_url_cache_path() -> str:
    logs_dir = os.path.join(os.getcwd(), "logs")
    os.makedirs(logs_dir, exist_ok=True)
    return os.path.join(logs_dir, "motley_fool_url_cache.json")


def _load_fool_url_cache() -> dict:
    try:
        p = _fool_url_cache_path()
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning("Could not load Motley Fool URL cache: %s", e)
    return {}


def _save_fool_url_cache(cache: dict):
    try:
        with open(_fool_url_cache_path(), "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except Exception as e:
        logger.warning("Could not save Motley Fool URL cache: %s", e)


def _fetch_motley_fool_cached(symbols: list) -> list:
    """
    Fetch Motley Fool articles, skipping any URL seen in the last 24 hours.
    Persists seen URLs to disk so deduplication survives agent restarts.
    """
    url_cache = _load_fool_url_cache()
    now = datetime.now(timezone.utc)
    ttl = timedelta(hours=24)

    # Evict expired URLs
    fresh_cache = {
        url: ts for url, ts in url_cache.items()
        if now - datetime.fromisoformat(ts) < ttl
    }

    all_items = fetch_motley_fool(symbols)

    new_items = []
    for item in all_items:
        if item.url not in fresh_cache:
            new_items.append(item)
            fresh_cache[item.url] = now.isoformat()

    skipped = len(all_items) - len(new_items)
    if skipped:
        logger.debug("Motley Fool URL cache: skipped %d already-seen articles", skipped)

    _save_fool_url_cache(fresh_cache)
    logger.info(
        "Motley Fool: %d new articles (%d cached/skipped)",
        len(new_items), skipped,
    )
    return new_items


# ── Main research cycle ────────────────────────────────────────────────────────

def run_research_cycle(analyst, store, scanner, earnings_cal=None, insider_monitor=None, iv_monitor=None, clinical_cal=None, breakout_screener=None, alpaca_config=None, institutional_monitor=None):
    logger.info("=== Research cycle starting ===")

    base_symbols = config.watchlist.all_symbols
    logger.info("Base watchlist: %d symbols", len(base_symbols))

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

    # Earnings — 6h cached
    earnings_events = {}
    force_invalidate_symbols: set = set()  # Symbols needing cache bust after earnings surprise
    if earnings_cal:
        try:
            earnings_events = _get_earnings_events(earnings_cal, base_symbols)
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

            # Detect strong beats/misses — these force analysis cache invalidation
            # so stale research signals don't persist after major earnings surprises.
            # This is the fix for the LLY opportunity-sell error on 05/04 where a
            # 22% EPS beat was missed because the cache held a pre-earnings signal.
            strong_beats = earnings_cal.get_strong_beat_symbols(base_symbols) if earnings_cal else []
            strong_misses = earnings_cal.get_strong_miss_symbols(base_symbols) if earnings_cal else []
            force_invalidate_symbols = set(strong_beats + strong_misses)
            if force_invalidate_symbols:
                logger.warning(
                    "EARNINGS SURPRISE -- forcing cache invalidation for: %s "
                    "(beats=%s, misses=%s)",
                    list(force_invalidate_symbols), strong_beats, strong_misses,
                )
        except Exception as e:
            logger.warning("Earnings calendar error: %s", e)

    # Scanner hits — cached symbol detail
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

            detail = _get_symbol_detail(scanner, hit.symbol)
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

    all_symbols = list(dict.fromkeys(base_symbols + discovered_symbols))
    logger.info("Total symbols to research this cycle: %d", len(all_symbols))

    # IV spikes — daily cached (after close only)
    iv_items = []
    if iv_monitor and not is_market_open():
        try:
            earnings_soon = [
                s for s, e in earnings_events.items() if e.days_until <= 7
            ]
            iv_spikes = _get_iv_spikes(iv_monitor, all_symbols, earnings_soon)

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
        logger.debug("IV monitor skipped -- runs after market close only")

    # Insider buys — 4h cached
    insider_items = []
    if insider_monitor:
        try:
            significant_buys = _get_insider_buys(insider_monitor, all_symbols)
            if significant_buys:
                logger.info("Insider Monitor: %d significant buys found", len(significant_buys))
            for txn in significant_buys:
                logger.info(
                    "INSIDER BUY: %s -- %s (%s) bought $%,.0f worth",
                    txn.symbol, txn.insider_name, txn.insider_title, txn.total_value,
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

    # Institutional filings (13-D, 13-G, 13-F new positions)
    # Caching: seen accessions never re-fetched (disk-backed).
    # 13-F delta: only fires for positions NEW vs prior quarter.
    # Runs every cycle but does almost no work when cache is warm.
    institutional_items = []
    if institutional_monitor:
        try:
            ticker_cik = get_ticker_cik_map(all_symbols)
            inst_signals = institutional_monitor.get_signals(
                symbols=all_symbols,
                ticker_map=ticker_cik,
                days_back=90,
            )
            for sig in inst_signals:
                urgency_tag = f"|{sig.urgency}" if sig.urgency != "LOW" else ""
                institutional_items.append(ResearchItem(
                    source="institutional",
                    symbol=sig.symbol,
                    title=(
                        f"[INSTITUTIONAL|{sig.form_type}{urgency_tag}] "
                        f"{sig.filer_name} — {sig.form_type} on {sig.symbol}"
                    ),
                    summary=sig.to_research_summary(),
                    url=sig.url,
                    published_at=datetime.now(timezone.utc),
                    raw={
                        "form_type": sig.form_type,
                        "filer": sig.filer_name,
                        "filer_cik": sig.filer_cik,
                        "ownership_pct": sig.ownership_pct,
                        "shares_held": sig.shares_held,
                        "market_value": sig.market_value,
                        "is_new": sig.is_new_position,
                        "is_activist": sig.is_activist,
                        "urgency": sig.urgency,
                    },
                ))
                if sig.form_type == "13-D":
                    logger.warning(
                        "ACTIVIST 13-D: %s filed on %s -- potential catalyst",
                        sig.filer_name, sig.symbol,
                    )
        except Exception as e:
            logger.warning("Institutional monitor error: %s", e)

    # News, SEC, Reddit — no caching here (freshness matters)
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

    # Motley Fool — 24h URL disk cache
    try:
        fool_items = _fetch_motley_fool_cached(all_symbols)
    except Exception as e:
        logger.warning("Motley Fool fetcher error: %s", e)
        fool_items = []

    # Pre-breakout screener — detects accumulation BEFORE the price move.
    # Runs on 1-min bars already fetched for this cycle (no extra API cost).
    # Symbols flagged here get [PRE-BREAKOUT] ResearchItems flowing to Claude,
    # which writes them to research_signals so the trading agent is primed.
    breakout_items = []
    if breakout_screener and is_market_open():
        try:
            # Fetch fresh 1-min bars for all symbols
            from data.alpaca_fetcher import AlpacaDataFetcher
            _fetcher = AlpacaDataFetcher(alpaca_config) if alpaca_config else None
            bars_1min = {}
            if _fetcher:
                bars_1min = _fetcher.get_bars(
                    symbols=all_symbols,
                    lookback_bars=50,
                    timeframe="1Min",
                )

            breakout_signals = breakout_screener.scan(
                symbols=all_symbols,
                bars_1min=bars_1min,
                research_signals={s: research_signals.get(s, {}) for s in all_symbols},
                alpaca_config=alpaca_config,
            )

            for sig in breakout_signals:
                logger.info(
                    "[PRE-BREAKOUT] %s score=%d signals=%s",
                    sig.symbol, sig.score, sig.signals,
                )
                breakout_items.append(ResearchItem(
                    source="breakout_screener",
                    symbol=sig.symbol,
                    title=(
                        f"[PRE-BREAKOUT|score={sig.score}] {sig.symbol}: "
                        f"{' + '.join(sig.signals)}"
                    ),
                    summary=sig.to_research_summary(),
                    url=f"https://finance.yahoo.com/quote/{sig.symbol}",
                    published_at=datetime.now(timezone.utc),
                    raw={
                        "score": sig.score,
                        "signals": sig.signals,
                        "volume_ratio": sig.volume_ratio,
                        "rsi": sig.rsi,
                        "bb_width_pct": sig.bb_width_pct,
                        "near_52w_low": sig.near_52w_low,
                        "has_insider": sig.has_insider_signal,
                        "price_change_1h": sig.price_change_1h,
                    },
                ))
        except Exception as e:
            logger.warning("Breakout screener error: %s", e)
    elif not is_market_open():
        logger.debug("Breakout screener skipped -- market closed")

    # Clinical catalyst monitor
    # Clinical catalyst monitor — FDA/Phase 3/PDUFA dates for biotech symbols
    # Cached 12h inside ClinicalCatalystCalendar. Generates ResearchItems so
    # Claude treats upcoming readouts as high-risk context in its analysis.
    clinical_items = []
    clinical_catalyst_symbols = []
    if clinical_cal:
        try:
            clinical_events = clinical_cal.get_events(all_symbols)
            pre_catalyst = clinical_cal.get_pre_catalyst_symbols(all_symbols)
            high_risk = clinical_cal.get_high_risk_symbols(all_symbols)

            if pre_catalyst:
                logger.warning(
                    "CLINICAL CATALYST WARNING (within %dd): %s",
                    clinical_cal.__class__.__mro__[0].__init__.__defaults__ or [7],
                    pre_catalyst,
                )
            if high_risk:
                logger.warning(
                    "HIGH-RISK BINARY CATALYST (PDUFA/Phase3): %s -- "
                    "agent will block new positions",
                    high_risk,
                )
                clinical_catalyst_symbols.extend(high_risk)

            for sym, catalyst in clinical_events.items():
                logger.info(
                    "[%s] Clinical catalyst: %s %s in %d days (%s, %s)",
                    sym, catalyst.catalyst_type, catalyst.drug_name,
                    catalyst.days_until, catalyst.catalyst_date, catalyst.source,
                )
                clinical_items.append(ResearchItem(
                    source="clinical_catalyst",
                    symbol=sym,
                    title=(
                        f"[CLINICAL CATALYST|{catalyst.catalyst_type}] "
                        f"{sym}: {catalyst.drug_name or catalyst.catalyst_type} "
                        f"readout in {catalyst.days_until}d"
                    ),
                    summary=catalyst.to_prompt_text(),
                    url=f"https://www.biopharmcatalyst.com/company/{sym}",
                    published_at=datetime.now(timezone.utc),
                    raw={
                        "catalyst_type": catalyst.catalyst_type,
                        "catalyst_date": catalyst.catalyst_date.isoformat(),
                        "days_until": catalyst.days_until,
                        "risk_level": catalyst.risk_level,
                        "confirmed": catalyst.confirmed,
                        "drug_name": catalyst.drug_name,
                        "is_pre_catalyst": catalyst.is_pre_catalyst_window,
                        "is_high_risk": catalyst.is_high_risk,
                    },
                ))
        except Exception as e:
            logger.warning("Clinical catalyst monitor error: %s", e)

    all_items = (
        news_items + sec_items + reddit_items +
        scanner_items + insider_items + iv_items + fool_items +
        clinical_items + breakout_items + institutional_items
    )
    logger.info(
        "Collected %d items -- news=%d, SEC=%d, Reddit=%d, "
        "Scanner=%d, Insider=%d, IV=%d, MotleyFool=%d, "
        "Clinical=%d, Breakout=%d, Institutional=%d",
        len(all_items), len(news_items), len(sec_items),
        len(reddit_items), len(scanner_items), len(insider_items),
        len(iv_items), len(fool_items), len(clinical_items),
        len(breakout_items), len(institutional_items),
    )

    if not all_items:
        logger.warning("No research items collected this cycle")
        return

    # Analyse with Claude — pass force_invalidate so strong beat/miss symbols
    # bypass the analysis cache and get a fresh Claude call this cycle.
    reports = analyst.analyse_all(all_items, all_symbols, force_invalidate=force_invalidate_symbols)
    high_conviction = [r for r in reports if r.conviction >= CONVICTION_THRESHOLD]

    logger.info(
        "Analysis complete -- %d reports, %d high-conviction",
        len(reports), len(high_conviction),
    )

    for r in reports:
        if r.symbol in binary_catalyst_symbols:
            ttl = 6
        elif r.symbol in clinical_catalyst_symbols:
            ttl = 8  # Clinical catalysts get longer TTL — risk window spans days
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
    logger.info(
        "  Cache TTLs: earnings=%dh | insider=%dh | IV=daily | fool=24h | analysis=%sh",
        EARNINGS_CACHE_TTL_HOURS, INSIDER_CACHE_TTL_HOURS,
        os.getenv("ANALYSIS_CACHE_TTL_HOURS", "4"),
    )

    analyst         = ResearchAnalyst(config.anthropic)
    store           = ResearchStore()
    breakout_screener       = BreakoutScreener()
    institutional_monitor   = InstitutionalMonitor()
    earnings_cal    = EarningsCalendar()
    insider_monitor = InsiderMonitor()
    iv_monitor      = IVMonitor()
    clinical_cal    = ClinicalCatalystCalendar()
    scanner         = MarketScanner(
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
            run_research_cycle(
                analyst, store, scanner,
                earnings_cal, insider_monitor, iv_monitor,
                clinical_cal=clinical_cal,
                breakout_screener=breakout_screener,
                alpaca_config=config.alpaca,
                institutional_monitor=institutional_monitor,
            )
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

# ── Clinical catalyst integration patch ───────────────────────────────────────
# Appended to existing research_agent.py.
# Replace the run_research_cycle() call in main() with run_research_cycle_v2()
# which passes clinical_cal through, or patch run_research_cycle to accept it.
