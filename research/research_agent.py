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
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
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
from data.breakout_screener import BreakoutScreener
from data.institutional_monitor import InstitutionalMonitor, get_ticker_cik_map
from data.universe_scanner import UniverseScanner
from data.intraday_monitor import IntradayMonitor, CORE_INTRADAY
from data.momentum_monitor import MomentumMonitor
from data.ipo_monitor import IpoMonitor

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

RESEARCH_INTERVAL      = int(os.getenv("RESEARCH_INTERVAL_SECONDS", "1800"))  # Deep research: 30 min
INTRADAY_INTERVAL      = int(os.getenv("INTRADAY_INTERVAL_SECONDS", "120"))  # Fast momentum: 2 min (short-term cycling)
CONVICTION_THRESHOLD   = float(os.getenv("CONVICTION_THRESHOLD", "0.70"))
SCANNER_MIN_SCORE      = float(os.getenv("SCANNER_MIN_SCORE", "0.50"))
SCANNER_MAX_HITS       = int(os.getenv("SCANNER_MAX_HITS", "10"))

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


# ── Main research cycle ────────────────────────────────────────────────────────

def run_research_cycle(analyst, store, scanner, earnings_cal=None, insider_monitor=None, iv_monitor=None, breakout_screener=None, alpaca_config=None, institutional_monitor=None, universe_scanner=None, intraday_monitor=None, momentum_monitor=None, ipo_monitor=None, universe_candidates=None, research_signals_map=None):
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

    # ── Intraday monitor — 1-minute momentum/dip signals ────────────────────
    # Watches CORE_INTRADAY symbols + universe discoveries every research cycle.
    # Writes high-TTL signals directly — bypasses Claude for speed.
    intraday_items = []
    if intraday_monitor and is_market_open():
        try:
            # Combine core intraday watchlist with universe discoveries
            intraday_symbols = list(dict.fromkeys(
                CORE_INTRADAY + [c.symbol for c in (universe_candidates or [])]
            ))

            # Fetch fresh 1-min bars
            from data.alpaca_fetcher import AlpacaDataFetcher
            _fetcher = AlpacaDataFetcher(alpaca_config) if alpaca_config else None
            bars_1min = {}
            if _fetcher:
                bars_1min = _fetcher.get_bars(
                    symbols=intraday_symbols,
                    lookback_bars=60,
                    timeframe="1Min",
                )

            # Get current research signals for fundamental gate
            current_signals = research_signals_map or {}

            intraday_signals = intraday_monitor.scan(
                symbols=intraday_symbols,
                bars_1min=bars_1min,
                research_signals=current_signals,
            )

            for sig in intraday_signals:
                logger.info(
                    "[INTRADAY] %s %s conviction=%.0f%% fundamental=%s",
                    sig.symbol, sig.signal_type,
                    sig.conviction * 100, sig.has_fundamental,
                )
                # Write directly to store — bypasses Claude analysis
                store.write_signal(
                    symbol=sig.symbol,
                    sentiment=sig.sentiment,
                    conviction=sig.conviction,
                    recommended_action=sig.recommended_action,
                    summary=sig.to_summary(),
                    key_points=sig.to_key_points(),
                    risk_factors=sig.to_risk_factors(),
                    sources_used=1,
                    ttl_hours=sig.ttl_hours,
                    signal_type="TECHNICAL",
                )
                # Also add as ResearchItem for Claude context in next cycle
                intraday_items.append(ResearchItem(
                    source="intraday_monitor",
                    symbol=sig.symbol,
                    title=f"[INTRADAY|{sig.signal_type}] {sig.symbol}: {sig.rationale[:80]}",
                    summary=sig.to_summary(),
                    url=f"https://finance.yahoo.com/quote/{sig.symbol}",
                    published_at=datetime.now(timezone.utc),
                    raw={
                        "signal_type": sig.signal_type,
                        "conviction": sig.conviction,
                        "rsi": sig.rsi,
                        "volume_ratio": sig.volume_ratio,
                        "has_fundamental": sig.has_fundamental,
                    },
                ))
        except Exception as e:
            logger.warning("Intraday monitor error: %s", e)
    elif not is_market_open():
        logger.debug("Intraday monitor skipped -- market closed")

    # News, SEC, Reddit — no caching here (freshness matters)
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

    fool_items = []  # Removed: HTML scraping too fragile for production use

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

    # Multi-timeframe momentum monitor — 15-min + 1-hr bar signals (no Claude, fast)
    momentum_items = []
    if momentum_monitor and is_market_open():
        try:
            momentum_signals = momentum_monitor.scan(all_symbols, alpaca_config)
            for sig in momentum_signals:
                logger.info(
                    "[MOMENTUM] %s %s conviction=%.0f%%",
                    sig.symbol, sig.signal_type, sig.conviction * 100,
                )
                store.write_signal(
                    symbol=sig.symbol,
                    sentiment=sig.sentiment,
                    conviction=sig.conviction,
                    recommended_action=sig.recommended_action,
                    summary=sig.summary,
                    key_points=sig.key_points,
                    risk_factors=sig.risk_factors,
                    sources_used=1,
                    ttl_hours=sig.ttl_hours,
                    signal_type="MOMENTUM",
                )
                momentum_items.append(ResearchItem(
                    source="momentum_monitor",
                    symbol=sig.symbol,
                    title=f"[MOMENTUM|{sig.signal_type}] {sig.symbol}: {sig.summary[:80]}",
                    summary=sig.summary,
                    url=f"https://finance.yahoo.com/quote/{sig.symbol}",
                    published_at=datetime.now(timezone.utc),
                    raw={"signal_type": sig.signal_type, "conviction": sig.conviction},
                ))
        except Exception as e:
            logger.warning("Clinical catalyst monitor error: %s", e)

    # Upcoming IPOs — write CATALYST signals directly, cached 24h (no Claude call)
    ipo_count = 0
    if ipo_monitor:
        try:
            upcoming_ipos = ipo_monitor.fetch_upcoming()
            for ipo in upcoming_ipos:
                sym = ipo.get("symbol") or ipo.get("ticker", "")
                if not sym:
                    continue
                ipo_date   = ipo.get("date") or ipo.get("ipo_date", "unknown date")
                name       = ipo.get("name") or ipo.get("company_name", sym)
                price_low  = ipo.get("price_low") or ipo.get("low_price", "?")
                price_high = ipo.get("price_high") or ipo.get("high_price", "?")
                price_range = f"${price_low}–${price_high}"
                exchange   = ipo.get("exchange", "")
                store.write_signal(
                    symbol=sym,
                    sentiment="BULLISH",
                    conviction=0.68,
                    recommended_action="WATCH",
                    summary=(
                        f"Upcoming IPO: {name} ({sym}) expected {ipo_date}"
                        + (f" on {exchange}" if exchange else "")
                        + f". Price range {price_range}."
                    ),
                    key_points=[
                        f"IPO date: {ipo_date}",
                        f"Exchange: {exchange}" if exchange else "Exchange: TBD",
                        f"Expected price range: {price_range}",
                    ],
                    risk_factors=[
                        "IPO-day volatility — price discovery can swing ±20%",
                        "No post-IPO price history for technical analysis",
                    ],
                    sources_used=1,
                    ttl_hours=48,
                    signal_type="CATALYST",
                )
                logger.info("[IPO] %s (%s) — expected %s, price %s", name, sym, ipo_date, price_range)
                ipo_count += 1
        except Exception as e:
            logger.warning("IPO monitor error: %s", e)

    all_items = (
        news_items + sec_items + reddit_items +
        scanner_items + insider_items + iv_items + fool_items +
        breakout_items + institutional_items + intraday_items + momentum_items
    )
    logger.info(
        "Collected %d items -- news=%d, SEC=%d, Reddit=%d, "
        "Scanner=%d, Insider=%d, IV=%d, MotleyFool=%d, "
        "Breakout=%d, Institutional=%d, Intraday=%d, Momentum=%d, IPO=%d",
        len(all_items), len(news_items), len(sec_items),
        len(reddit_items), len(scanner_items), len(insider_items),
        len(iv_items), len(fool_items),
        len(breakout_items), len(institutional_items), len(intraday_items), len(momentum_items),
        ipo_count,
    )

    # Hot-symbol prioritization: force fresh Claude analysis for symbols
    # that have an active technical/momentum signal this cycle.
    hot_symbols = set(discovered_symbols)
    for sym, sig in (research_signals_map or {}).items():
        if (
            sig.get("signal_type") in ("TECHNICAL", "MOMENTUM")
            and float(sig.get("conviction", 0)) >= 0.60
        ):
            hot_symbols.add(sym)
    if hot_symbols:
        force_invalidate_symbols |= hot_symbols
        logger.info(
            "Hot symbols (forcing fresh analysis): %s",
            sorted(hot_symbols),
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
            signal_type="FUNDAMENTAL",
        )

    if high_conviction:
        send_alert(high_conviction)
        logger.info("Alert sent for: %s", [r.symbol for r in high_conviction])

    logger.info("=== Research cycle complete ===")


def run_fast_intraday_cycle(intraday_monitor, store, universe_candidates, research_signals_map):
    """
    Fast 3-minute cycle for high-velocity momentum detection.
    Only runs intraday monitor (no SEC filings, no insider trades, no Claude analysis).
    Writes directly to research_signals for immediate consumption by trading agent.
    """
    if not is_market_open():
        return

    logger.info("=== FAST INTRADAY CYCLE ===")

    # Combine core volatile stocks with universe discoveries
    intraday_symbols = list(dict.fromkeys(
        CORE_INTRADAY +
        [c.symbol for c in (universe_candidates or [])[:20]]
    ))

    logger.info(
        "Intraday scan: %d symbols (%d core + %d discovered)",
        len(intraday_symbols),
        len([s for s in intraday_symbols if s in CORE_INTRADAY]),
        len([s for s in intraday_symbols if s not in CORE_INTRADAY]),
    )

    try:
        signals = intraday_monitor.scan(
            symbols=intraday_symbols,
            existing_research=research_signals_map or {},
        )

        if signals:
            logger.info("Intraday signals: %d detected", len(signals))
            for sig in signals:
                # Write directly to research_signals with short TTL
                store.write_signal(
                    symbol=sig.symbol,
                    sentiment=sig.sentiment,
                    conviction=sig.conviction,
                    recommended_action=sig.recommended_action,
                    summary=sig.to_summary(),
                    key_points=[sig.rationale],
                    risk_factors=[
                        "Intraday signal — expires fast. Use tight stops.",
                        "Mean reversion or momentum continuation — watch for reversal.",
                    ],
                    sources_used=1,
                    ttl_hours=sig.ttl_hours,
                    signal_type="TECHNICAL",
                )
                logger.info(
                    "[%s] %s | conviction=%.0f%% | %s | price=$%.2f | chg_1h=%+.1f%%",
                    sig.symbol, sig.signal_type, sig.conviction * 100,
                    sig.sentiment, sig.price, sig.price_change_1h,
                )
        else:
            logger.info("No intraday signals detected this cycle")

    except Exception as e:
        logger.warning("Fast intraday cycle error: %s", e)

    logger.info("=== Fast intraday cycle complete ===")


def main():
    os.makedirs(os.path.join(os.getcwd(), "logs"), exist_ok=True)

    logger.info("Research Agent starting up (TWO-SPEED MODE)")
    logger.info("  Deep research interval: %ds (%d min)", RESEARCH_INTERVAL, RESEARCH_INTERVAL // 60)
    logger.info("  Fast intraday interval: %ds (%d min)", INTRADAY_INTERVAL, INTRADAY_INTERVAL // 60)
    logger.info("  Conviction threshold: %.0f%%", CONVICTION_THRESHOLD * 100)
    logger.info("  Watchlist: %d symbols", len(config.watchlist.all_symbols))
    logger.info(
        "  Cache TTLs: earnings=%dh | insider=%dh | IV=daily | analysis=%sh",
        EARNINGS_CACHE_TTL_HOURS, INSIDER_CACHE_TTL_HOURS,
        os.getenv("ANALYSIS_CACHE_TTL_HOURS", "4"),
    )

    analyst               = ResearchAnalyst(config.anthropic)
    store                 = ResearchStore()
    breakout_screener     = BreakoutScreener()
    institutional_monitor = InstitutionalMonitor()
    universe_scanner      = UniverseScanner(
        alpaca_api_key=os.getenv("ALPACA_API_KEY", ""),
        alpaca_secret_key=os.getenv("ALPACA_SECRET_KEY", ""),
        paper=os.getenv("ALPACA_PAPER", "true").lower() == "true",
    )
    intraday_monitor      = IntradayMonitor()
    momentum_monitor      = MomentumMonitor()
    ipo_monitor           = IpoMonitor() if os.getenv("MASSIVE_API_KEY") else None
    _universe_candidates  = []
    _last_universe_scan   = None
    earnings_cal          = EarningsCalendar()
    insider_monitor       = InsiderMonitor()
    iv_monitor            = IVMonitor()
    scanner               = MarketScanner(
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

    _last_deep_research = None  # Track when last full research cycle ran
    _research_signals_map = {}

    logger.info("All monitors initialised — entering research loop")

    while running:
        now = datetime.now(timezone.utc)
        logger.debug("[LOOP] Starting loop iteration")

        # Universe scan — runs every 5 minutes during market hours
        # Discovers previously unknown momentum stocks for intraday monitoring
        if (is_market_open() and universe_scanner and (
            _last_universe_scan is None or
            (now - _last_universe_scan).total_seconds() >= 300
        )):
            try:
                logger.info("[LOOP] Running universe scan...")
                _pool = ThreadPoolExecutor(max_workers=1)
                _fut = _pool.submit(
                    universe_scanner.scan,
                    existing_watchlist=config.watchlist.all_symbols,
                )
                try:
                    _universe_candidates = _fut.result(timeout=45)
                except FuturesTimeoutError:
                    logger.warning("[LOOP] Universe scan timed out (45s) — skipping this cycle")
                    _universe_candidates = []
                finally:
                    _pool.shutdown(wait=False)  # Don't wait for stuck threads
                _last_universe_scan = now
                logger.info("[LOOP] Universe scan done: %d candidates", len(_universe_candidates))
                if _universe_candidates:
                    logger.info(
                        "Universe scan: %d new candidates -- %s",
                        len(_universe_candidates),
                        ", ".join(
                            f"{c.symbol}({c.change_pct:+.1f}%)"
                            for c in _universe_candidates[:5]
                        ),
                    )
            except Exception as e:
                logger.warning("Universe scan error: %s", e)

        # Load current research signals for intraday fundamental gate
        try:
            logger.debug("[LOOP] Loading active signals from DB")
            active_sigs = store.get_all_active()
            _research_signals_map = {s["symbol"]: s for s in active_sigs}
            logger.debug("[LOOP] Loaded %d active signals", len(_research_signals_map))
        except Exception:
            pass

        # Decide: full research cycle or fast intraday-only?
        should_run_deep = (
            _last_deep_research is None or
            (now - _last_deep_research).total_seconds() >= RESEARCH_INTERVAL
        )

        if should_run_deep:
            # SLOW CYCLE: Full research (SEC, insider, Claude analysis, everything)
            try:
                logger.info("[LOOP] Calling run_research_cycle")
                run_research_cycle(
                    analyst, store, scanner,
                    earnings_cal, insider_monitor, iv_monitor,
                    breakout_screener=breakout_screener,
                    alpaca_config=config.alpaca,
                    institutional_monitor=institutional_monitor,
                    universe_scanner=universe_scanner,
                    intraday_monitor=intraday_monitor,
                    momentum_monitor=momentum_monitor,
                    ipo_monitor=ipo_monitor,
                    universe_candidates=_universe_candidates,
                    research_signals_map=_research_signals_map,
                )
                _last_deep_research = datetime.now(timezone.utc)
            except Exception as e:
                logger.exception("Unhandled error in deep research cycle: %s", e)
        else:
            # FAST CYCLE: Intraday momentum detection only (3-min refresh)
            try:
                run_fast_intraday_cycle(
                    intraday_monitor=intraday_monitor,
                    store=store,
                    universe_candidates=_universe_candidates,
                    research_signals_map=_research_signals_map,
                )
            except Exception as e:
                logger.exception("Unhandled error in fast intraday cycle: %s", e)

        if running:
            if is_market_open():
                # Next cycle: fast intraday (every 3 min), or slow if 15 min elapsed
                interval = INTRADAY_INTERVAL
                logger.info("Market open -- next cycle in %ds", interval)
            else:
                interval = 7200
                logger.info("Market closed -- next cycle in 2h")
            time.sleep(interval)

    store.close()
    logger.info("Research agent shut down cleanly.")


if __name__ == "__main__":
    main()
