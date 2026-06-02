"""
Main agent loop.
Ties together data fetching, signal computation, AI decisions, risk checks, and execution.
Dynamically expands watchlist with high-conviction scanner discoveries each tick.
Run with: python main.py
"""

import logging
import signal
import sys
import time
from datetime import datetime, timezone

import config
from agent.decision_engine import AIDecisionEngine
from data.alpaca_fetcher import AlpacaDataFetcher
from execution.alpaca_executor import AlpacaExecutor
from risk.risk_manager import RiskManager
from signals.technical import compute_signals
from storage.trade_store import TradeStore
from storage.research_store import ResearchStore
from data.massive_indicators import MassiveIndicatorFetcher
from data.earnings_calendar import EarningsCalendar
from data.alpaca_stream import AlpacaBarStream
try:
    from notifier import notify_startup, notify_shutdown
    _NOTIFY = True
except ImportError as _e:
    logger.warning("notifier.py not found -- push notifications disabled: %s", _e)
    _NOTIFY = False
from data.clinical_catalyst_calendar import ClinicalCatalystCalendar

# Logging setup
# Create logs directory if it doesn't exist (needed for local runs)
import os as _os
_os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    level=getattr(logging, config.agent.log_level, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    handlers=[
        logging.StreamHandler(open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)),
        logging.FileHandler("logs/agent.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")

# Minimum conviction for a scanner discovery to be traded
SCANNER_TRADE_THRESHOLD = float(config.agent.min_confidence)

# ── Dynamic stream subscriptions ─────────────────────────────────────────────
# Symbols discovered by scanners that are not in the core watchlist.
# Subscribed to the WebSocket stream within 60 seconds of discovery so the
# hot path watches them immediately — long before the next full sweep.
_dynamic_stream_symbols: set = set()

# ── Dedup: last Claude evaluation timestamp per symbol ───────────────────────
# Updated by both the hot path and the mini loop after every AI decide() call.
# The mini loop checks this before evaluating — if the symbol was already
# evaluated within MINI_INTERVAL seconds (by the hot path or a prior mini tick),
# it skips the call to avoid paying for the same decision twice.
_last_evaluated: dict = {}  # symbol → float (time.time())


def _sync_dynamic_stream(bar_stream, research_store, core_symbols: set) -> None:
    """
    Fast-track scanner discoveries to the stream within 60s of being found.

    Any symbol in the research store with conviction >= 0.45 that isn't already
    in the core watchlist gets subscribed immediately, without waiting for the
    next full sweep (which runs every 15 minutes).
    """
    global _dynamic_stream_symbols
    try:
        active = research_store.get_all_active()
    except Exception:
        return
    new_symbols = {
        s["symbol"] for s in active
        if s["symbol"] not in core_symbols
        and float(s.get("conviction", 0)) >= 0.45
    }
    to_add = new_symbols - _dynamic_stream_symbols
    if to_add:
        all_current = core_symbols | _dynamic_stream_symbols | to_add
        bar_stream.update_symbols(list(all_current))
        _dynamic_stream_symbols.update(to_add)
        logger.info(
            "[STREAM] Fast-tracked %d discovered symbol(s) to stream: %s",
            len(to_add), sorted(to_add)[:5],
        )


from datetime import time as _dtime


# ── Market regime filter ──────────────────────────────────────────────────────
# Cached result: (regime_ok, reason, cached_at_unix)
_regime_cache: tuple = (True, "not yet computed", 0.0)
_REGIME_CACHE_TTL = 1800  # seconds — re-check every 30 minutes


def check_market_regime(alpaca_config) -> tuple:
    """
    Returns (regime_ok, reason_str).
    When False, the main loop skips new BUYs but still evaluates SELLs.

    Checks:
      1. SPY is above its 200-day SMA — the most battle-tested macro filter.
         Historical impact: max drawdown 83% → 29% (1929–2019 study).
      2. VIX rank (percentile of past 252 days) is below 70.
         Historical impact: max drawdown −32% in 2024-2025 momentum backtests.

    Both checks fail gracefully — a data error returns (True, reason) so
    trading is never blocked by a missing API response.
    """
    import requests as _req
    import pandas as _pd

    global _regime_cache
    ok, reason, cached_at = _regime_cache
    if time.time() - cached_at < _REGIME_CACHE_TTL:
        return ok, reason

    headers = {
        "APCA-API-KEY-ID": alpaca_config.api_key,
        "APCA-API-SECRET-KEY": alpaca_config.secret_key,
    }

    # ── 1. SPY 200-day SMA ────────────────────────────────────────────────────
    spy_ok = True
    spy_detail = "SPY check unavailable"
    try:
        resp = _req.get(
            "https://data.alpaca.markets/v2/stocks/SPY/bars",
            headers=headers,
            params={"timeframe": "1Day", "limit": 220, "adjustment": "raw", "feed": "iex"},
            timeout=8,
        )
        resp.raise_for_status()
        bars = resp.json().get("bars", [])
        if len(bars) >= 200:
            closes = _pd.Series([b["c"] for b in bars])
            sma200 = closes.rolling(200).mean().iloc[-1]
            spy_close = closes.iloc[-1]
            spy_ok = spy_close > sma200
            spy_detail = (
                f"SPY {spy_close:.2f} {'>' if spy_ok else '<'} 200d SMA {sma200:.2f}"
            )
        else:
            spy_detail = f"SPY bars insufficient ({len(bars)} bars)"
    except Exception as e:
        spy_detail = f"SPY check error: {e}"
        logger.debug("Regime SPY check failed: %s", e)

    # ── 2. VIX rank (CBOE public CSV) ─────────────────────────────────────────
    vix_ok = True
    vix_detail = "VIX check unavailable"
    try:
        resp = _req.get(
            "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
            timeout=8,
        )
        resp.raise_for_status()
        from io import StringIO
        df_vix = _pd.read_csv(StringIO(resp.text))
        df_vix.columns = [c.strip().upper() for c in df_vix.columns]
        close_col = next((c for c in df_vix.columns if "CLOSE" in c), None)
        if close_col:
            closes = _pd.to_numeric(df_vix[close_col], errors="coerce").dropna()
            if len(closes) >= 252:
                vix_now = closes.iloc[-1]
                window = closes.iloc[-252:]
                vix_rank = (vix_now - window.min()) / (window.max() - window.min()) * 100
                vix_ok = vix_rank <= 70
                vix_detail = f"VIX {vix_now:.1f} rank {vix_rank:.0f}/100 ({'OK' if vix_ok else 'HIGH'})"
    except Exception as e:
        vix_detail = f"VIX check error: {e}"
        logger.debug("Regime VIX check failed: %s", e)

    regime_ok = spy_ok and vix_ok
    reason = f"{spy_detail} | {vix_detail}"
    if not regime_ok:
        logger.warning("[REGIME] Unfavorable — BUYs paused. %s", reason)
    else:
        logger.info("[REGIME] OK. %s", reason)

    _regime_cache = (regime_ok, reason, time.time())
    return regime_ok, reason


def is_market_open() -> bool:
    """
    Returns True if NYSE is currently open.
    NYSE hours: Mon-Fri 09:30-16:00 ET = 13:30-20:00 UTC = 15:30-22:00 Copenhagen.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    return _dtime(13, 30) <= now.time() <= _dtime(20, 0)


def find_weakest_position(positions: list, positions_map: dict,
                           research_signals: dict) -> dict | None:
    """
    Find the weakest current position to potentially sell.
    Scores each position by combining:
    - Current P&L % (negative = weaker)
    - Research signal conviction (low conviction = weaker)
    - Days held (longer with poor performance = weaker)
    Returns the weakest position dict or None.
    """
    if not positions:
        return None

    scored = []
    for pos in positions:
        symbol = pos.get("symbol", "")
        pnl_pct = float(pos.get("unrealized_plpc", 0)) * 100

        # Research conviction for this position (lower = weaker hold)
        research = research_signals.get(symbol, {})
        research_conviction = float(research.get("conviction", 0.5))
        research_action = research.get("recommended_action", "HOLD")

        # Score: lower is weaker (more sellable)
        # Negative P&L hurts score, low research conviction hurts score
        # If research says SELL that really hurts score
        action_penalty = -0.3 if research_action == "SELL" else 0
        score = (pnl_pct / 100) + (research_conviction - 0.5) + action_penalty

        scored.append((score, pos))
        logger.debug(
            "Position score [%s]: pnl=%.1f%% research=%.0f%% action=%s -> score=%.3f",
            symbol, pnl_pct, research_conviction * 100,
            research_action, score,
        )

    # Return the position with the lowest score (weakest)
    scored.sort(key=lambda x: x[0])
    return scored[0][1] if scored else None


def should_opportunity_sell(
    new_signal_confidence: float,
    weakest_position: dict,
    research_signals: dict,
    min_confidence_gap: float = 0.10,  # CHANGED: 10% gap instead of 15% for faster rotation
    earnings_events: dict = None,
) -> tuple:
    """
    Decide whether to sell the weakest position to fund a new opportunity.
    Returns (should_sell: bool, reason: str)

    Rules (adjusted for short-term cycling):
    - New signal must be at least 10% more confident than weakest position conviction
    - Only protect positions >20% profitable (short-term cycling wants to rotate capital)
    - Earnings beat protection REMOVED for short-term cycling
    - Never sell if weakest position has exceptional research BULLISH signal (≥0.80)
    """
    symbol = weakest_position.get("symbol", "")
    pnl_pct = float(weakest_position.get("unrealized_plpc", 0)) * 100

    # CHANGED: Only protect big winners (>20%), allow rotation of smaller winners
    if pnl_pct > 20.0:
        return False, f"{symbol} is up {pnl_pct:.1f}% -- protecting large winner"

    # REMOVED: Earnings beat protection (counter to short-term cycling)
    # Short-term trading wants to participate in volatility, not avoid it

    # Get current research conviction for weakest position
    research = research_signals.get(symbol, {})
    pos_conviction = float(research.get("conviction", 0.5))
    pos_sentiment = research.get("sentiment", "NEUTRAL")

    # CHANGED: Only protect exceptional BULLISH signals (≥0.80), not just ≥0.70
    if pos_sentiment == "BULLISH" and pos_conviction >= 0.80:
        return False, f"{symbol} has exceptional BULLISH research signal ({pos_conviction:.0%})"

    # Check confidence gap
    confidence_gap = new_signal_confidence - pos_conviction
    if confidence_gap < min_confidence_gap:
        return False, (
            f"Confidence gap too small: new={new_signal_confidence:.0%} "
            f"vs {symbol}={pos_conviction:.0%} (gap={confidence_gap:.0%} < {min_confidence_gap:.0%})"
        )

    return True, (
        f"Opportunity sell: {symbol} (pnl={pnl_pct:.1f}%, conviction={pos_conviction:.0%}) "
        f"replaced by higher-conviction opportunity (gap={confidence_gap:.0%})"
    )


def execute_opportunity_sell(
    decision,
    positions: list,
    positions_map: dict,
    research_signals: dict,
    executor: AlpacaExecutor,
    risk: RiskManager,
    store: TradeStore,
    data_fetcher: AlpacaDataFetcher,
    earnings_events: dict = None,
) -> tuple:
    """
    If at max positions and a new BUY signal arrives for an unheld symbol,
    evaluate and potentially sell the weakest position to make room.

    Must be called BEFORE risk.check() so that freed cash is visible
    to the risk manager when it evaluates the incoming BUY.

    Returns (positions, positions_map) — refreshed from Alpaca if a sell
    executed, unchanged otherwise.
    """
    current_count = len({p["symbol"] for p in positions})
    symbol_held = decision.symbol in positions_map

    if current_count < config.risk.max_open_positions or symbol_held:
        return positions, positions_map

    weakest = find_weakest_position(positions, positions_map, research_signals)
    if not weakest:
        return positions, positions_map

    should_sell, sell_reason = should_opportunity_sell(
        decision.confidence, weakest, research_signals,
        earnings_events=earnings_events,
    )

    if not should_sell:
        logger.info("Opportunity sell declined: %s", sell_reason)
        return positions, positions_map

    # PDT guard: don't sell a position opened today — that's a day trade
    if store and store.bought_today(weakest["symbol"]):
        logger.info(
            "[%s] Opportunity sell skipped — position opened today (PDT protection)",
            weakest["symbol"],
        )
        return positions, positions_map

    logger.info("OPPORTUNITY SELL triggered: %s", sell_reason)
    sell_result = executor.sell(symbol=weakest["symbol"], close_all=True)

    if not sell_result:
        logger.warning("Opportunity sell order failed for %s", weakest["symbol"])
        return positions, positions_map

    sold_val = float(weakest.get("market_value") or 0)
    risk.record_sale(sold_val)
    store.log_execution(
        order_id=sell_result.get("order_id", ""),
        symbol=weakest["symbol"],
        side="SELL",
        notional=sold_val,
    )
    store.log_decision(
        symbol=weakest["symbol"],
        action="SELL",
        confidence=0.70,
        rationale=f"Opportunity sell: {sell_reason}",
        urgency="MEDIUM",
        approved=True,
        approval_reason="Sold to fund higher-conviction opportunity",
        notional=sold_val,
    )

    # Optimistically remove the sold position from local state immediately.
    # This guarantees risk.check() sees the correct count even if Alpaca's
    # API hasn't reflected the sell yet (race condition — both events at 17:01).
    sold_symbol = weakest["symbol"]
    positions = [p for p in positions if p["symbol"] != sold_symbol]
    positions_map = {p["symbol"]: p for p in positions}
    logger.debug(
        "Optimistic position removal: %s removed locally, count now %d",
        sold_symbol, len(positions),
    )

    # Attempt a live refresh from Alpaca to get accurate cash/buying_power.
    # Retry once after a short delay to give Alpaca time to process the order.
    # Only accept the refresh if Alpaca has actually dropped the position —
    # if the order is still pending, keep our optimistic local state instead.
    import time as _time
    for attempt in range(2):
        try:
            refreshed = data_fetcher.get_positions()
            if not any(p["symbol"] == sold_symbol for p in refreshed):
                positions = refreshed
                positions_map = {p["symbol"]: p for p in positions}
                logger.debug(
                    "Alpaca refresh confirmed sell: %d positions remaining",
                    len(positions),
                )
                break
            else:
                if attempt == 0:
                    logger.debug(
                        "Alpaca still shows %s -- retrying in 1s", sold_symbol
                    )
                    _time.sleep(1)
                else:
                    logger.info(
                        "Alpaca still shows %s after retry -- "
                        "using optimistic local state (%d positions)",
                        sold_symbol, len(positions),
                    )
        except Exception as e:
            logger.warning("Portfolio refresh after opportunity sell failed: %s", e)
            break

    return positions, positions_map


def validate_config():
    missing = []
    if not config.alpaca.api_key:
        missing.append("ALPACA_API_KEY")
    if not config.alpaca.secret_key:
        missing.append("ALPACA_SECRET_KEY")
    if not config.anthropic.api_key:
        missing.append("ANTHROPIC_API_KEY")
    if missing:
        logger.critical("Missing environment variables: %s", ", ".join(missing))
        sys.exit(1)


# Minimum research conviction to include a symbol in the trading cycle
RESEARCH_GATE_THRESHOLD = 0.45  # CHANGED: was 0.55; lowered to match agent.min_confidence

def get_dynamic_symbols(
    research_store: ResearchStore,
    full_universe: list,
    positions_map: dict = None,
    min_conviction: float = RESEARCH_GATE_THRESHOLD,
    extended_universe: set = None,
) -> tuple:
    """
    Build the active trading symbol list from research signals.

    Logic:
    - Held positions are ALWAYS included (can't miss a sell signal)
    - Symbols with adequate research conviction (>= min_conviction) are included
    - Symbols with active but LOW conviction signals are excluded (research flagged them)
    - Symbols with NO signal are skipped in the full sweep — the hot path (bar stream)
      handles discovery for those symbols based on real-time price/volume activity
    - extended_universe: dynamically-subscribed symbols (scanner discoveries fast-tracked
      to the stream); evaluated here when they have a conviction signal
    - Returns (active_symbols, discovered_symbols, research_signals_map)
    """
    research_signals = {}
    active_symbols = []
    discovered_symbols = []
    skipped = []
    unsignaled = []

    try:
        active_signals = research_store.get_all_active()
        research_signals = {s["symbol"]: s for s in active_signals}
    except Exception as e:
        logger.warning("Could not load research signals: %s", e)
        # Fall back to full universe if DB unavailable
        return full_universe, [], {}

    # Held positions are always evaluated regardless of signal strength
    held_symbols = set(positions_map.keys()) if positions_map else set()

    for symbol in full_universe:
        signal = research_signals.get(symbol)
        if symbol in held_symbols:
            # Always evaluate held positions — never gate out a potential sell
            active_symbols.append(symbol)
            logger.debug("[%s] Included (held position)", symbol)
        elif signal:
            conviction = float(signal.get("conviction", 0))
            if conviction >= min_conviction:
                active_symbols.append(symbol)
            else:
                skipped.append(f"{symbol}({conviction:.0%})")
        else:
            # No research signal yet = neutral, not bad. Collect for rotating batch.
            unsignaled.append(symbol)

    # Add dynamically-subscribed symbols that now have a signal
    if extended_universe:
        for symbol in extended_universe:
            if symbol in active_symbols:
                continue
            signal = research_signals.get(symbol)
            if signal:
                conviction = float(signal.get("conviction", 0))
                if conviction >= min_conviction:
                    active_symbols.append(symbol)
                    if symbol not in full_universe:
                        discovered_symbols.append(symbol)
                    logger.debug("[%s] Extended universe symbol admitted (conviction=%.0f%%)", symbol, conviction * 100)

    # Add scanner discoveries not already in universe
    for symbol, signal in research_signals.items():
        if symbol not in full_universe:
            conviction = float(signal.get("conviction", 0))
            summary = signal.get("summary", "").lower()
            is_scanner_hit = (
                any(kw in summary for kw in [
                    "gainer", "volume", "scanner", "active", "explosive",
                    "surge", "spike", "rally", "turnaround", "upgrade",
                    "breakout", "momentum", "jump", "soar", "beat",
                    "record", "growth", "demand",
                    # Intraday monitor keywords
                    "intraday", "dip", "wave", "vwap", "capitulation",
                    "universe discovery", "sector sympathy",
                ])
                or conviction >= 0.70
            )
            if conviction >= SCANNER_TRADE_THRESHOLD and is_scanner_hit:
                active_symbols.append(symbol)
                discovered_symbols.append(symbol)
                logger.info(
                    "Scanner discovery added: %s (conviction=%.0f%%)",
                    symbol, conviction * 100,
                )

    # Deduplicate while preserving order
    active_symbols = list(dict.fromkeys(active_symbols))

    if skipped:
        logger.info("Skipped (low conviction): %s", ", ".join(skipped[:10]))
    if unsignaled:
        logger.debug(
            "Unsignaled pool: %d symbols — covered by hot path (bar stream)",
            len(unsignaled),
        )

    return active_symbols, discovered_symbols, research_signals


def run_loop(
    data_fetcher: AlpacaDataFetcher,
    signal_engine,
    ai_engine: AIDecisionEngine,
    executor: AlpacaExecutor,
    risk: RiskManager,
    store: TradeStore,
    research_store: ResearchStore = None,
    massive_fetcher=None,
    earnings_cal=None,
    clinical_cal=None,
    fast_retry_symbols: list = None,
    bar_stream: "AlpacaBarStream | None" = None,
    regime_ok: bool = True,
):
    logger.info("--- Agent loop tick ---")

    # 1. Portfolio state
    try:
        portfolio = data_fetcher.get_account()
        positions = data_fetcher.get_positions()
    except Exception as e:
        logger.error("Failed to fetch portfolio state: %s", e)
        return

    logger.info(
        "Portfolio: equity=$%.2f | cash=$%.2f | positions=%d",
        portfolio["equity"], portfolio["cash"], len(positions),
    )

    positions_map = {p["symbol"]: p for p in positions}

    # 2. Build dynamic symbol list from full research universe
    # In fast retry mode, only evaluate the blocked HIGH urgency symbols
    # to minimise API cost and latency on the retry tick.
    full_universe = config.watchlist.all_symbols
    if fast_retry_symbols:
        logger.info("Fast retry mode: evaluating only %s", fast_retry_symbols)
        all_symbols = fast_retry_symbols
        discovered_symbols = []
        try:
            active_signals = research_store.get_all_active() if research_store else []
            research_signals = {s["symbol"]: s for s in active_signals}
        except Exception:
            research_signals = {}
    else:
        all_symbols, discovered_symbols, research_signals = get_dynamic_symbols(
            research_store,
            full_universe,
            positions_map=positions_map,
            min_conviction=RESEARCH_GATE_THRESHOLD,
            extended_universe=_dynamic_stream_symbols,
        )

    logger.info(
        "Active this tick: %d/%d symbols (research-gated) + %d scanner discoveries",
        len(all_symbols) - len(discovered_symbols),
        len(full_universe),
        len(discovered_symbols),
    )

    # 3. Sync stream subscriptions with active symbol list
    if bar_stream:
        held_set = set(positions_map.keys())
        bar_stream.update_symbols(all_symbols, held_symbols=held_set)

    # 3b. Fetch market data — prefer live stream buffer, fall back to REST
    bars: dict = {}
    rest_needed: list = []
    if bar_stream:
        for sym in all_symbols:
            df = bar_stream.get_bars_df(sym)
            if df is not None and len(df) >= 10:
                bars[sym] = df
                logger.debug("[BARS] Stream buffer for %s (%d bars)", sym, len(df))
            else:
                rest_needed.append(sym)
    else:
        rest_needed = list(all_symbols)

    if rest_needed:
        rest_bars = data_fetcher.get_bars(
            symbols=rest_needed,
            lookback_bars=config.agent.indicator_lookback,
            timeframe="1Min",
        )
        bars.update(rest_bars)
        if bar_stream and rest_needed:
            logger.debug("[BARS] REST fallback for %d symbols (buffers not yet full)", len(rest_needed))

    # 4. Compute technical signals
    snapshots = []
    for symbol in all_symbols:
        df = bars.get(symbol)
        snapshot = compute_signals(symbol, df)
        if snapshot:
            snapshots.append(snapshot)
        elif symbol in discovered_symbols:
            logger.warning(
                "Scanner discovery %s has no market data — skipping this tick",
                symbol,
            )

    # Fast lookup: symbol → snapshot (for ATR access during execution)
    snapshots_map = {s.symbol: s for s in snapshots}

    if not snapshots:
        logger.warning("No valid snapshots computed this tick.")
        return

    # 5. Fetch Massive end-of-day indicators for cross-validation
    massive_indicators = {}
    if massive_fetcher and massive_fetcher.api_key:
        for symbol in all_symbols[:2]:  # 2 symbols x 2 calls x 1.5s = ~6s per tick
            ind = massive_fetcher.fetch_all(symbol)
            if any(v is not None for v in [ind.rsi_14, ind.ema_9, ind.macd_value]):
                massive_indicators[symbol] = ind
                logger.debug("Massive indicators fetched for %s", symbol)

    # 6. Earnings calendar check
    earnings_events = {}
    pre_earnings_symbols = []
    post_earnings_symbols = []
    if earnings_cal:
        try:
            earnings_events = earnings_cal.get_events(all_symbols)
            pre_earnings_symbols = earnings_cal.get_pre_earnings_symbols(all_symbols)
            post_earnings_symbols = earnings_cal.get_post_earnings_symbols(all_symbols)
            if pre_earnings_symbols:
                logger.warning(
                    "PRE-EARNINGS CAUTION: %s reporting soon -- agent will be conservative",
                    pre_earnings_symbols,
                )
            if post_earnings_symbols:
                logger.info(
                    "POST-EARNINGS REACTION: %s reported recently -- checking for beat/miss",
                    post_earnings_symbols,
                )
        except Exception as e:
            logger.warning("Earnings calendar error: %s", e)

    # 7. Pre-flight check — skip symbols where notional < $10 regardless of cash.
    # Avoids wasting Claude API calls on positions the risk manager will block anyway.
    min_notional = 10.0
    tradeable_snapshots = []
    skipped_low_equity = []
    for snap in snapshots:
        max_notional = portfolio["equity"] * config.risk.max_position_pct
        if max_notional < min_notional:
            skipped_low_equity.append(snap.symbol)
        else:
            tradeable_snapshots.append(snap)

    if skipped_low_equity:
        logger.warning(
            "Skipping %d symbols — equity too low for minimum trade "
            "(equity=$%.2f x %.0f%% = $%.2f < $%.0f): %s",
            len(skipped_low_equity),
            portfolio["equity"],
            config.risk.max_position_pct * 100,
            portfolio["equity"] * config.risk.max_position_pct,
            min_notional,
            skipped_low_equity[:5],
        )
    snapshots = tradeable_snapshots

    if not snapshots:
        logger.warning(
            "No tradeable symbols this tick — account equity $%.2f too low "
            "to meet $%.0f minimum trade at %.0f%% position sizing. "
            "Add funds or increase max_position_pct.",
            portfolio["equity"], min_notional,
            config.risk.max_position_pct * 100,
        )
        return

    # 7. AI decisions — pass research signals, Massive indicators, and earnings context
    decisions = ai_engine.decide_batch(
        snapshots,
        portfolio,
        positions_map,
        sector_bias_boost=config.agent.sector_bias_boost,
        research_signals=research_signals,
        massive_indicators=massive_indicators,
        earnings_events=earnings_events,
    )

    # 8. Rank all decisions by conviction before acting
    # SELLs always go first (free up cash), then BUYs ranked by conviction
    sells = sorted(
        [d for d in decisions if d.action == "SELL"],
        key=lambda d: d.confidence, reverse=True,
    )

    # Urgency-weighted buy ranking.
    # Pure confidence ranking caused the PODD RSI-10.5 HIGH urgency signal on 05/04
    # to lose its queue position to MEDIUM urgency buys that consumed settled cash first.
    # Scoring: urgency tier (HIGH=3, MEDIUM=2, LOW=1) × 0.20 + confidence.
    # This means a HIGH urgency signal at 77% confidence scores 3×0.20 + 0.77 = 1.37,
    # beating a MEDIUM urgency signal at 90% confidence (2×0.20 + 0.90 = 1.30).
    # Effectively: HIGH urgency adds ~13% to the ranking score vs MEDIUM.
    _URGENCY_WEIGHT = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}

    def _buy_score(d) -> float:
        urgency_tier = _URGENCY_WEIGHT.get(getattr(d, "urgency", "MEDIUM"), 2)
        return urgency_tier * 0.20 + d.confidence

    buys = sorted(
        [d for d in decisions if d.action == "BUY"],
        key=_buy_score, reverse=True,
    )
    holds = [d for d in decisions if d.action == "HOLD"]

    logger.info(
        "Decision summary: %d SELLs | %d BUYs (urgency-weighted ranking) | %d HOLDs",
        len(sells), len(buys), len(holds),
    )
    if buys:
        logger.info(
            "BUY ranking: %s",
            " > ".join(
                f"{d.symbol}({d.confidence:.0%},{getattr(d,'urgency','?')},"
                f"score={_buy_score(d):.2f})"
                for d in buys
            ),
        )

    # Log HOLDs first (no action needed)
    for decision in holds:
        store.log_decision(
            symbol=decision.symbol,
            action=decision.action,
            confidence=decision.confidence,
            rationale=decision.rationale,
            urgency=decision.urgency,
            approved=True,
            approval_reason="HOLD -- no trade to validate.",
            notional=None,
        )

    # 9. Execute SELLs first to free up cash, then BUYs ranked by conviction
    for decision in sells + buys:
        is_discovery = decision.symbol in discovered_symbols

        if is_discovery:
            logger.info(
                "*** SCANNER DISCOVERY [%s] %s conf=%.2f -- %s",
                decision.symbol, decision.action,
                decision.confidence, decision.rationale,
            )

        # Re-fetch portfolio state after each Claude-initiated SELL so
        # subsequent BUYs see the freed cash correctly
        if decision.action == "BUY" and sells:
            try:
                portfolio = data_fetcher.get_account()
                positions = data_fetcher.get_positions()
                positions_map = {p["symbol"]: p for p in positions}
            except Exception as e:
                logger.warning("Could not refresh portfolio state: %s", e)

        # Suppress new BUYs near clinical catalyst events (Phase 3 / PDUFA)
        # Wider window than earnings — 7 days by default via PRE_CATALYST_DAYS
        pre_catalyst_symbols = []
        if clinical_cal:
            try:
                pre_catalyst_symbols = clinical_cal.get_pre_catalyst_symbols(all_symbols)
                if pre_catalyst_symbols:
                    logger.warning(
                        "Clinical catalyst symbols in caution window: %s",
                        pre_catalyst_symbols,
                    )
            except Exception as _e:
                logger.debug("Clinical catalyst check error: %s", _e)

        if (decision.action == "BUY"
                and decision.symbol in pre_catalyst_symbols
                and decision.symbol not in positions_map):
            logger.info(
                "[%s] BUY suppressed -- clinical catalyst within caution window "
                "(Phase3/PDUFA binary risk)",
                decision.symbol,
            )
            store.log_decision(
                symbol=decision.symbol,
                action="HOLD",
                confidence=decision.confidence,
                rationale=f"Clinical catalyst caution: {decision.rationale}",
                urgency="LOW",
                approved=False,
                approval_reason="Clinical catalyst within caution window -- no new positions",
                notional=None,
            )
            continue

        # REMOVED: Pre-earnings trading pause (counter to short-term cycling)
        # Short-term strategy wants to trade volatility around catalysts, not avoid it
        # Keep clinical catalyst caution above (FDA events are truly binary), but allow earnings trading

        # ── Regime gate — block new BUYs in unfavorable macro conditions ────
        if decision.action == "BUY" and not regime_ok:
            logger.info(
                "[%s] BUY blocked — unfavorable market regime (SPY/VIX filter)",
                decision.symbol,
            )
            store.log_decision(
                symbol=decision.symbol,
                action="HOLD",
                confidence=decision.confidence,
                rationale=decision.rationale,
                urgency="LOW",
                approved=False,
                approval_reason="Regime filter: market conditions unfavorable for new longs",
                notional=None,
            )
            continue

        # ── Opportunity-cost sell ────────────────────────────────────────────
        # Runs BEFORE risk.check() so any freed cash is visible to the risk
        # manager. If a sell executes, positions/positions_map are refreshed
        # from Alpaca before we proceed. This is the single canonical location
        # for opportunity-sell logic — there is no second block below.
        if decision.action == "BUY":
            positions, positions_map = execute_opportunity_sell(
                decision=decision,
                positions=positions,
                positions_map=positions_map,
                research_signals=research_signals,
                executor=executor,
                risk=risk,
                store=store,
                data_fetcher=data_fetcher,
                earnings_events=earnings_events,
            )

        # ── Risk verdict (always sees post-sell portfolio state) ─────────────
        # Guard: skip SELL decisions for symbols not in the portfolio.
        # Claude evaluates all watchlist symbols — SELL on an unheld stock is noise,
        # not a trade signal. Drop it here before risk-check and logging so it
        # never appears in the dashboard as an "approved" SELL.
        if decision.action == "SELL" and decision.symbol not in positions_map:
            logger.debug("[%s] SELL signal skipped — symbol not held", decision.symbol)
            continue

        _snap = snapshots_map.get(decision.symbol)
        _atr = _snap.atr_14 if _snap else None
        _snap_price = _snap.current_price if _snap else None
        verdict = risk.check(
            decision=decision,
            portfolio=portfolio,
            positions=positions,
            min_confidence=config.agent.min_confidence,
            atr=_atr,
            current_price=_snap_price,
        )

        store.log_decision(
            symbol=decision.symbol,
            action=decision.action,
            confidence=decision.confidence,
            rationale=decision.rationale,
            urgency=decision.urgency,
            approved=verdict.approved,
            approval_reason=verdict.reason,
            notional=verdict.adjusted_notional,
        )

        if not verdict.approved:
            logger.info("[%s] BLOCKED -- %s", decision.symbol, verdict.reason)
            continue

        current_price = data_fetcher.get_latest_price(decision.symbol)
        if not current_price:
            logger.warning("[%s] Could not fetch latest price, skipping.", decision.symbol)
            continue

        stop_loss, take_profit = risk.compute_stop_and_target(current_price, decision, atr=_atr)
        notional = verdict.adjusted_notional

        # ── Execute BUY ──────────────────────────────────────────────────────
        if decision.action == "BUY":
            # Skip buy if PDT block is already active this session
            if executor.is_pdt_blocked:
                logger.warning(
                    "[%s] BUY skipped -- PDT block active this session",
                    decision.symbol,
                )
                continue

            result = executor.buy(
                symbol=decision.symbol,
                notional=notional,
                stop_loss_price=stop_loss,
                take_profit_price=take_profit,
            )
            if result and not hasattr(result, 'is_pdt'):
                store.log_execution(
                    order_id=result["order_id"],
                    symbol=decision.symbol,
                    side="BUY",
                    notional=notional,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                )
                if is_discovery:
                    logger.info(
                        "*** SCANNER DISCOVERY TRADED: %s BUY $%.2f",
                        decision.symbol, notional,
                    )
                # Deduct from local portfolio cash estimate so subsequent
                # buys in this tick see updated available funds
                portfolio["cash"] = max(0, float(portfolio.get("cash", 0)) - notional)
                portfolio["buying_power"] = max(0, float(portfolio.get("buying_power", 0)) - notional)

        # ── Execute Claude-initiated SELL ────────────────────────────────────
        elif decision.action == "SELL":
            existing = positions_map.get(decision.symbol)
            if existing:
                # PDT guard: don't sell a position opened today
                if store and store.bought_today(decision.symbol):
                    logger.info(
                        "[%s] SELL skipped — position opened today (PDT protection)",
                        decision.symbol,
                    )
                    continue
                result = executor.sell(symbol=decision.symbol, close_all=True)
                if result and not hasattr(result, 'is_pdt'):
                    market_value = existing.get("market_value")
                    if not market_value:
                        qty = float(existing.get("qty", 0))
                        price = float(existing.get("current_price") or existing.get("avg_entry_price", 0))
                        market_value = qty * price
                    sold_value = float(market_value)
                    risk.record_sale(sold_value)
                    store.log_execution(
                        order_id=result.get("order_id", ""),
                        symbol=decision.symbol,
                        side="SELL",
                        notional=sold_value,
                    )
                elif hasattr(result, 'is_pdt') and result.is_pdt:
                    logger.warning(
                        "[%s] SELL blocked by PDT -- position retained, "
                        "stop-loss bracket at Alpaca remains active",
                        decision.symbol,
                    )
            else:
                logger.info("[%s] SELL signal but no open position.", decision.symbol)

    # ── Collect blocked HIGH urgency BUYs for fast retry ──────────────────────
    # These are returned to main() so it can re-run the loop for just these
    # symbols after a 60-second wait, rather than waiting the full interval.
    high_urgency_blocked = []
    for decision in buys:
        if (decision.urgency == "HIGH"
                and decision.symbol not in [
                    d.symbol for d in decisions
                    if d.action == "BUY" and d.confidence >= config.agent.min_confidence
                    and d.symbol in (positions_map or {})
                ]):
            # Check if it was actually executed this tick
            was_executed = decision.symbol in {
                p["symbol"] for p in (positions or [])
                if p not in (positions_map or {})
            }
            if not was_executed and decision.action == "BUY":
                high_urgency_blocked.append(decision.symbol)

    if high_urgency_blocked:
        logger.info(
            "HIGH urgency BUYs not executed this tick: %s -- queued for fast retry",
            high_urgency_blocked,
        )

    logger.info("--- Tick complete ---")
    return high_urgency_blocked


def run_mini_loop(
    held_symbols: list,
    bar_stream: "AlpacaBarStream",
    data_fetcher: AlpacaDataFetcher,
    ai_engine: AIDecisionEngine,
    executor: AlpacaExecutor,
    risk: RiskManager,
    store: TradeStore,
    research_store,
    earnings_cal,
) -> None:
    """
    Lightweight 1-minute evaluation of held positions only.
    Uses stream buffer bars — no REST calls unless the buffer is empty.
    Ensures held positions are re-assessed every bar close, not just every 5 minutes.
    """
    if not held_symbols:
        return

    logger.info("[MINI] Evaluating %d held position(s): %s", len(held_symbols), held_symbols)

    try:
        portfolio = data_fetcher.get_account()
        positions = data_fetcher.get_positions()
    except Exception as e:
        logger.warning("[MINI] Portfolio fetch failed: %s", e)
        return

    positions_map = {p["symbol"]: p for p in positions}

    for symbol in held_symbols:
        # Skip if the hot path already evaluated this symbol within the last
        # MINI_INTERVAL seconds — avoids paying for the same decision twice.
        last_eval = _last_evaluated.get(symbol, 0)
        if time.time() - last_eval < MINI_INTERVAL:
            logger.debug(
                "[MINI] %s skipped — hot path evaluated %.0fs ago",
                symbol, time.time() - last_eval,
            )
            continue

        df = bar_stream.get_bars_df(symbol)
        if df is None or len(df) < 10:
            # Buffer not warm yet — fall back to REST so newly-bought positions
            # are monitored immediately rather than left unprotected for ~200 minutes.
            try:
                rest_bars = data_fetcher.get_bars(
                    [symbol],
                    lookback_bars=config.agent.indicator_lookback,
                    timeframe="1Min",
                )
                df = rest_bars.get(symbol)
                if df is not None and len(df) >= 10:
                    logger.debug("[MINI] %s REST fallback (%d bars)", symbol, len(df))
                else:
                    logger.debug("[MINI] %s buffer + REST both unavailable — skipping", symbol)
                    continue
            except Exception as e:
                logger.debug("[MINI] %s REST fallback failed: %s — skipping", symbol, e)
                continue

        snapshot = compute_signals(symbol, df)
        if not snapshot:
            continue

        research_signal = None
        try:
            if research_store:
                research_signal = research_store.get_signal(symbol)
        except Exception:
            pass

        earnings_event = None
        try:
            if earnings_cal:
                earnings_event = earnings_cal.get_events([symbol]).get(symbol)
        except Exception:
            pass

        decision = ai_engine.decide(
            snapshot, portfolio, positions_map.get(symbol),
            research_signal=research_signal,
            earnings_event=earnings_event,
        )
        _last_evaluated[symbol] = time.time()
        if not decision or decision.action == "HOLD":
            logger.debug(
                "[MINI] %s → HOLD (conf=%.0f%%)",
                symbol, (decision.confidence if decision else 0) * 100,
            )
            continue

        logger.info(
            "[MINI] %s → %s (conf=%.0f%%, urgency=%s)",
            symbol, decision.action, decision.confidence * 100,
            getattr(decision, "urgency", "?"),
        )

        _atr = snapshot.atr_14 if snapshot else None
        verdict = risk.check(
            decision, portfolio, positions_map,
            min_confidence=config.agent.min_confidence,
            atr=_atr,
            current_price=snapshot.current_price if snapshot else None,
        )
        if not verdict.approved:
            logger.info("[MINI] %s %s blocked: %s", symbol, decision.action, verdict.reason)
            continue

        price = data_fetcher.get_latest_price(symbol)
        if decision.action == "SELL" and symbol in positions_map:
            result = executor.sell(symbol, close_all=True)
            store.log_execution(symbol, "SELL", float(positions_map[symbol]["market_value"]), price, None, None)
            logger.info("[MINI] SELL %s executed: %s", symbol, result)
        elif decision.action == "BUY" and symbol not in positions_map:
            stop, target = risk.compute_stop_and_target(price, decision, atr=_atr)
            result = executor.buy(symbol, verdict.adjusted_notional, stop, target)
            store.log_execution(symbol, "BUY", verdict.adjusted_notional, price, stop, target)
            logger.info("[MINI] BUY %s executed: %s", symbol, result)


def _drain_hot_queue(
    bar_stream: "AlpacaBarStream",
    data_fetcher: AlpacaDataFetcher,
    ai_engine: AIDecisionEngine,
    executor: AlpacaExecutor,
    risk: RiskManager,
    store: TradeStore,
    research_store,
    earnings_cal,
) -> None:
    """
    Process any hot-symbol events that arrived since the last tick.
    Each hot event triggers a single-symbol decide → risk → execute pass
    without waiting for the next 5-minute tick.

    We cap at 10 hot events per drain call to handle busier markets now that
    the hot criteria cover more setups. Events older than 90 seconds are
    discarded — the bar has aged out and the signal is stale.
    """
    processed = 0
    while processed < 10:
        try:
            symbol, bar_dict = bar_stream.hot_queue.get_nowait()
        except Exception:
            break

        # Discard stale events — bar closed more than 90s ago
        bar_ts = bar_dict.get("timestamp")
        if bar_ts is not None:
            try:
                ts_dt = bar_ts.to_pydatetime() if hasattr(bar_ts, "to_pydatetime") else bar_ts
                if isinstance(ts_dt, datetime):
                    if ts_dt.tzinfo is None:
                        ts_dt = ts_dt.replace(tzinfo=timezone.utc)
                    age = (datetime.now(timezone.utc) - ts_dt).total_seconds()
                    if age > 90:
                        logger.debug("[HOT PATH] Stale event for %s (age=%.0fs) — skipped", symbol, age)
                        continue
            except Exception:
                pass

        logger.info("[HOT PATH] %s triggered fast eval (close=%.2f)", symbol, bar_dict["close"])

        try:
            df = bar_stream.get_bars_df(symbol)
            if df is None or len(df) < 10:
                logger.debug("[HOT PATH] %s buffer too small, skipping", symbol)
                continue

            snapshot = compute_signals(symbol, df)
            if not snapshot:
                continue

            # Fetch portfolio state (lightweight — cached by SDK internally)
            try:
                portfolio  = data_fetcher.get_account()
                positions  = data_fetcher.get_positions()
            except Exception as e:
                logger.warning("[HOT PATH] Portfolio fetch failed: %s", e)
                continue

            positions_map = {p["symbol"]: p for p in positions}

            # Research signal if available
            research_signal = None
            try:
                if research_store:
                    research_signal = research_store.get_signal(symbol)
            except Exception:
                pass

            # Earnings check
            earnings_events = {}
            if earnings_cal:
                try:
                    earnings_events = earnings_cal.get_events([symbol])
                except Exception:
                    pass

            decision = ai_engine.decide(
                snapshot, portfolio, positions_map.get(symbol),
                research_signal=research_signal,
                earnings_event=earnings_events.get(symbol),
            )
            _last_evaluated[symbol] = time.time()

            if not decision or decision.action == "HOLD":
                logger.debug("[HOT PATH] %s → HOLD (%.0f%% conf)", symbol, (decision.confidence if decision else 0) * 100)
                continue

            logger.info(
                "[HOT PATH] %s → %s (conf=%.0f%%, urgency=%s)",
                symbol, decision.action, decision.confidence * 100,
                getattr(decision, "urgency", "?"),
            )

            # Risk check + execution (same path as normal loop)
            _atr = snapshot.atr_14 if snapshot else None
            verdict = risk.check(
                decision, portfolio, positions_map,
                min_confidence=config.agent.min_confidence,
                atr=_atr,
                current_price=snapshot.current_price if snapshot else None,
            )
            if verdict.approved:
                price = data_fetcher.get_latest_price(symbol)
                if decision.action == "BUY" and symbol not in positions_map:
                    stop, target = risk.compute_stop_and_target(price, decision, atr=_atr)
                    result = executor.buy(symbol, verdict.adjusted_notional, stop, target)
                    store.log_execution(symbol, "BUY", verdict.adjusted_notional, price, stop, target)
                    logger.info("[HOT PATH] BUY %s executed: %s", symbol, result)
                elif decision.action == "SELL" and symbol in positions_map:
                    result = executor.sell(symbol, close_all=True)
                    store.log_execution(symbol, "SELL", float(positions_map[symbol]["market_value"]), price, None, None)
                    logger.info("[HOT PATH] SELL %s executed: %s", symbol, result)
            else:
                logger.info("[HOT PATH] %s %s blocked: %s", symbol, decision.action, verdict.reason)

        except Exception as e:
            logger.warning("[HOT PATH] Error processing %s: %s", symbol, e)

        processed += 1


def main():
    validate_config()

    logger.info("Trading Agent starting up")
    logger.info("  Paper trading: %s", config.alpaca.paper)
    logger.info("  Loop interval: %ds", config.agent.loop_interval_seconds)
    logger.info("  Base watchlist: %s", config.watchlist.stocks)
    logger.info("  Scanner trade threshold: %.0f%%", SCANNER_TRADE_THRESHOLD * 100)

    data_fetcher    = AlpacaDataFetcher(config.alpaca)
    massive_fetcher = MassiveIndicatorFetcher()
    earnings_cal    = EarningsCalendar()
    clinical_cal    = ClinicalCatalystCalendar()
    ai_engine       = AIDecisionEngine(config.anthropic)
    executor        = AlpacaExecutor(config.alpaca)
    risk            = RiskManager(config.risk)
    store           = TradeStore()
    research_store  = ResearchStore()

    # Live bar stream (WebSocket) — pre-populates bar buffers so the trading
    # loop can read fresh data without REST calls, and fires hot-path evals.
    bar_stream: AlpacaBarStream | None = None
    try:
        bar_stream = AlpacaBarStream(
            api_key=config.alpaca.api_key,
            secret_key=config.alpaca.secret_key,
            symbols=config.watchlist.all_symbols,
        )
        # Pre-seed buffers with recent REST history so stream indicators are
        # accurate from the first tick instead of after ~200 minutes of warm-up.
        try:
            logger.info("[STREAM] Seeding buffers with REST history…")
            seed_bars = data_fetcher.get_bars(
                config.watchlist.all_symbols,
                lookback_bars=200,
                timeframe="1Min",
            )
            bar_stream.seed_buffers(seed_bars)
        except Exception as seed_err:
            logger.warning("[STREAM] Buffer seed failed (non-fatal): %s", seed_err)
        bar_stream.start()
    except Exception as e:
        logger.warning("Live bar stream unavailable — falling back to REST polling: %s", e)
        bar_stream = None

    try:
        account = data_fetcher.get_account()
        risk.reset_daily(account["equity"])
    except Exception as e:
        logger.warning("Could not fetch initial equity for risk reset: %s", e)

    if _NOTIFY:
        ok = notify_startup(
            paper=config.alpaca.paper,
            symbols=len(config.watchlist.stocks),
        )
        if ok:
            logger.info("Startup notification sent via ntfy.sh")
        else:
            logger.warning(
                "Startup notification failed -- check NTFY_TOPIC env var is set"
            )

    running = True

    def _shutdown(sig, frame):
        nonlocal running
        logger.warning("Shutdown signal received -- finishing current tick.")
        running = False

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Fast retry interval for blocked HIGH urgency signals (seconds)
    FAST_RETRY_INTERVAL = 60
    MINI_INTERVAL       = 60   # re-evaluate held positions every 1 minute
    SWEEP_INTERVAL      = 900  # full sweep is a safety net — hot path handles discovery

    last_sweep = 0.0  # force immediate sweep on first iteration
    last_mini  = 0.0

    while running:
        now = time.time()

        if is_market_open():
            # ── Full sweep (every SWEEP_INTERVAL seconds) ──────────────────────
            if now - last_sweep >= SWEEP_INTERVAL:
                try:
                    logger.info("--- [SWEEP] Full symbol sweep ---")
                    regime_ok, _ = check_market_regime(config.alpaca)
                    high_urgency_blocked = run_loop(
                        data_fetcher, None, ai_engine, executor, risk,
                        store, research_store, massive_fetcher, earnings_cal, clinical_cal,
                        bar_stream=bar_stream,
                        regime_ok=regime_ok,
                    )
                    last_sweep = time.time()
                    last_mini  = last_sweep  # sweep covers held positions too

                    if high_urgency_blocked and running and is_market_open():
                        logger.info(
                            "Fast retry in %ds for HIGH urgency blocked symbols: %s",
                            FAST_RETRY_INTERVAL, high_urgency_blocked,
                        )
                        time.sleep(FAST_RETRY_INTERVAL)
                        if running and is_market_open():
                            try:
                                logger.info("--- Fast retry tick for %s ---", high_urgency_blocked)
                                run_loop(
                                    data_fetcher, None, ai_engine, executor, risk,
                                    store, research_store, massive_fetcher,
                                    earnings_cal, clinical_cal,
                                    fast_retry_symbols=high_urgency_blocked,
                                    bar_stream=bar_stream,
                                    regime_ok=regime_ok,
                                )
                            except Exception as e:
                                logger.warning("Fast retry error: %s", e)

                except Exception as e:
                    logger.exception("Unhandled exception in agent loop: %s", e)

            # ── Mini loop (every MINI_INTERVAL seconds, between sweeps) ────────
            elif bar_stream and now - last_mini >= MINI_INTERVAL:
                try:
                    # Fast-track any newly discovered symbols to the stream
                    if research_store:
                        _sync_dynamic_stream(
                            bar_stream, research_store,
                            set(config.watchlist.all_symbols),
                        )
                    positions = data_fetcher.get_positions()
                    held = [p["symbol"] for p in positions]
                    run_mini_loop(
                        held, bar_stream, data_fetcher, ai_engine,
                        executor, risk, store, research_store, earnings_cal,
                    )
                    last_mini = time.time()
                except Exception as e:
                    logger.warning("Mini loop error: %s", e)

            # ── Hot queue drain (every 5 seconds) ──────────────────────────────
            if bar_stream:
                _drain_hot_queue(
                    bar_stream, data_fetcher, ai_engine, executor, risk,
                    store, research_store, earnings_cal,
                )

        else:
            logger.info("Market closed -- agent paused (NYSE open 15:30-22:00 Copenhagen time weekdays)")

        if running:
            time.sleep(5)

    if bar_stream:
        bar_stream.stop()
    store.close()
    research_store.close()
    logger.info("Trading agent shut down cleanly.")
    if _NOTIFY:
        ok = notify_shutdown(paper=config.alpaca.paper)
        if ok:
            logger.info("Shutdown notification sent via ntfy.sh")
        else:
            logger.warning("Shutdown notification failed")


if __name__ == "__main__":
    main()
