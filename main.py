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


from datetime import time as _dtime


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
    min_confidence_gap: float = 0.15,
) -> tuple:
    """
    Decide whether to sell the weakest position to fund a new opportunity.
    Returns (should_sell: bool, reason: str)

    Rules:
    - New signal must be at least 15% more confident than weakest position conviction
    - Weakest position must not be deeply profitable (>10% gain protected)
    - Never sell if weakest position has active research BULLISH signal
    """
    symbol = weakest_position.get("symbol", "")
    pnl_pct = float(weakest_position.get("unrealized_plpc", 0)) * 100

    # Never sell a position that is up more than 10% — let winners run
    if pnl_pct > 10.0:
        return False, f"{symbol} is up {pnl_pct:.1f}% -- protecting winner"

    # Get current research conviction for weakest position
    research = research_signals.get(symbol, {})
    pos_conviction = float(research.get("conviction", 0.5))
    pos_sentiment = research.get("sentiment", "NEUTRAL")

    # Never sell if research is actively bullish on this position
    if pos_sentiment == "BULLISH" and pos_conviction >= 0.70:
        return False, f"{symbol} has active BULLISH research signal ({pos_conviction:.0%})"

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
RESEARCH_GATE_THRESHOLD = 0.55


def get_dynamic_symbols(
    research_store: ResearchStore,
    full_universe: list,
    min_conviction: float = RESEARCH_GATE_THRESHOLD,
) -> tuple:
    """
    Build the active trading symbol list from research signals.

    Logic:
    - All 42 universe symbols are eligible
    - A symbol is included if:
        a) Research agent has an active signal with conviction >= min_conviction, OR
        b) It is a scanner discovery with conviction >= SCANNER_TRADE_THRESHOLD
    - Symbols with no research signal or low conviction are skipped this tick
    - Returns (active_symbols, discovered_symbols, research_signals_map)
    """
    research_signals = {}
    active_symbols = []
    discovered_symbols = []
    skipped = []

    try:
        active_signals = research_store.get_all_active()
        research_signals = {s["symbol"]: s for s in active_signals}
    except Exception as e:
        logger.warning("Could not load research signals: %s", e)
        # Fall back to full universe if DB unavailable
        return full_universe, [], {}

    # Gate universe symbols through research signals
    core_watchlist = set(config.watchlist.stocks)
    for symbol in full_universe:
        signal = research_signals.get(symbol)
        if symbol in core_watchlist:
            # Core watchlist ALWAYS evaluates — never gated out
            active_symbols.append(symbol)
            if signal:
                conviction = float(signal.get("conviction", 0))
                if conviction < min_conviction:
                    logger.debug("[%s] Core symbol included despite low conviction %.0f%%", symbol, conviction*100)
        elif signal:
            conviction = float(signal.get("conviction", 0))
            sentiment = signal.get("sentiment", "NEUTRAL")
            if conviction >= min_conviction:
                active_symbols.append(symbol)
            else:
                skipped.append(f"{symbol}({conviction:.0%})")
        else:
            skipped.append(f"{symbol}(no signal)")

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
                    "record", "growth", "demand"
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
        logger.info("Skipped (low/no conviction): %s", ", ".join(skipped[:10]))

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
    full_universe = config.watchlist.all_symbols
    all_symbols, discovered_symbols, research_signals = get_dynamic_symbols(
        research_store,
        full_universe,
        min_conviction=RESEARCH_GATE_THRESHOLD,
    )

    logger.info(
        "Active this tick: %d/%d symbols (research-gated) + %d scanner discoveries",
        len(all_symbols) - len(discovered_symbols),
        len(full_universe),
        len(discovered_symbols),
    )

    # 3. Fetch market data for all symbols
    bars = data_fetcher.get_bars(
        symbols=all_symbols,
        lookback_bars=config.agent.indicator_lookback,
        timeframe="1Min",
    )

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

    # 6. Rank all decisions by conviction before acting
    # SELLs always go first (free up cash), then BUYs ranked by conviction
    sells = sorted(
        [d for d in decisions if d.action == "SELL"],
        key=lambda d: d.confidence, reverse=True,
    )
    buys = sorted(
        [d for d in decisions if d.action == "BUY"],
        key=lambda d: d.confidence, reverse=True,
    )
    holds = [d for d in decisions if d.action == "HOLD"]

    logger.info(
        "Decision summary: %d SELLs | %d BUYs (ranked by conviction) | %d HOLDs",
        len(sells), len(buys), len(holds),
    )
    if buys:
        logger.info(
            "BUY ranking: %s",
            " > ".join(f"{d.symbol}({d.confidence:.0%})" for d in buys),
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

    # Execute SELLs first to free up cash
    for decision in sells + buys:
        is_discovery = decision.symbol in discovered_symbols

        if is_discovery:
            logger.info(
                "*** SCANNER DISCOVERY [%s] %s conf=%.2f -- %s",
                decision.symbol, decision.action,
                decision.confidence, decision.rationale,
            )

        # Re-fetch portfolio state after each SELL so cash reflects freed funds
        if decision.action == "BUY" and sells:
            try:
                portfolio = data_fetcher.get_account()
                positions = data_fetcher.get_positions()
                positions_map = {p["symbol"]: p for p in positions}
            except Exception as e:
                logger.warning("Could not refresh portfolio state: %s", e)

        # Suppress new BUYs within 48h of earnings — binary risk
        if (decision.action == "BUY" and
                decision.symbol in pre_earnings_symbols and
                decision.symbol not in positions_map):
            logger.info(
                "[%s] BUY suppressed -- earnings in <48h (pre-earnings caution)",
                decision.symbol,
            )
            store.log_decision(
                symbol=decision.symbol,
                action="HOLD",
                confidence=decision.confidence,
                rationale=f"Pre-earnings caution: {decision.rationale}",
                urgency="LOW",
                approved=False,
                approval_reason="Earnings within 48h -- no new positions",
                notional=None,
            )
            continue

        # Opportunity-cost check: before risk verdict, evaluate selling
        # weakest position if we are at max and this is a new BUY signal
        if decision.action == "BUY":
            current_count = len({p["symbol"] for p in positions})
            symbol_held = decision.symbol in positions_map
            if current_count >= config.risk.max_open_positions and not symbol_held:
                weakest = find_weakest_position(positions, positions_map, research_signals)
                if weakest:
                    should_sell, sell_reason = should_opportunity_sell(
                        decision.confidence, weakest, research_signals,
                    )
                    if should_sell:
                        logger.info("OPPORTUNITY SELL triggered: %s", sell_reason)
                        sell_result = executor.sell(symbol=weakest["symbol"], close_all=True)
                        if sell_result:
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
                            # Refresh portfolio after sell
                            try:
                                portfolio = data_fetcher.get_account()
                                positions = data_fetcher.get_positions()
                                positions_map = {p["symbol"]: p for p in positions}
                            except Exception as e:
                                logger.warning("Portfolio refresh failed: %s", e)
                    else:
                        logger.info("Opportunity sell declined: %s", sell_reason)

        verdict = risk.check(
            decision=decision,
            portfolio=portfolio,
            positions=positions,
            min_confidence=config.agent.min_confidence,
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

        stop_loss, take_profit = risk.compute_stop_and_target(current_price, decision)
        notional = verdict.adjusted_notional

        if decision.action == "BUY":
            # Opportunity-cost check: if at max positions, evaluate selling weakest
            current_position_count = len({p["symbol"] for p in positions})
            at_max = current_position_count >= config.risk.max_open_positions
            symbol_already_held = decision.symbol in positions_map

            if at_max and not symbol_already_held:
                weakest = find_weakest_position(positions, positions_map, research_signals)
                if weakest:
                    should_sell, sell_reason = should_opportunity_sell(
                        decision.confidence, weakest, research_signals,
                    )
                    if should_sell:
                        logger.info(
                            "OPPORTUNITY SELL: %s",
                            sell_reason,
                        )
                        # Execute the sell first
                        sell_result = executor.sell(
                            symbol=weakest["symbol"], close_all=True
                        )
                        if sell_result:
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
                            # Refresh portfolio state after sell
                            try:
                                portfolio = data_fetcher.get_account()
                                positions = data_fetcher.get_positions()
                                positions_map = {p["symbol"]: p for p in positions}
                                # Re-run risk check with fresh portfolio
                                verdict = risk.check(
                                    decision=decision,
                                    portfolio=portfolio,
                                    positions=positions,
                                    min_confidence=config.agent.min_confidence,
                                )
                                if not verdict.approved:
                                    logger.warning(
                                        "[%s] Still blocked after opportunity sell: %s",
                                        decision.symbol, verdict.reason,
                                    )
                                    continue
                                notional = verdict.adjusted_notional
                            except Exception as e:
                                logger.warning("Portfolio refresh after opportunity sell failed: %s", e)
                    else:
                        logger.info("Opportunity sell declined: %s", sell_reason)

            result = executor.buy(
                symbol=decision.symbol,
                notional=notional,
                stop_loss_price=stop_loss,
                take_profit_price=take_profit,
            )
            if result:
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

        elif decision.action == "SELL":
            existing = positions_map.get(decision.symbol)
            if existing:
                result = executor.sell(symbol=decision.symbol, close_all=True)
                if result:
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
            else:
                logger.info("[%s] SELL signal but no open position.", decision.symbol)

    logger.info("--- Tick complete ---")


def main():
    validate_config()

    logger.info("Trading Agent starting up")
    logger.info("  Paper trading: %s", config.alpaca.paper)
    logger.info("  Loop interval: %ds", config.agent.loop_interval_seconds)
    logger.info("  Base watchlist: %s", config.watchlist.stocks)
    logger.info("  Scanner trade threshold: %.0f%%", SCANNER_TRADE_THRESHOLD * 100)

    data_fetcher   = AlpacaDataFetcher(config.alpaca)
    massive_fetcher = MassiveIndicatorFetcher()
    earnings_cal = EarningsCalendar()
    ai_engine      = AIDecisionEngine(config.anthropic)
    executor       = AlpacaExecutor(config.alpaca)
    risk           = RiskManager(config.risk)
    store          = TradeStore()
    research_store = ResearchStore()

    try:
        account = data_fetcher.get_account()
        risk.reset_daily(account["equity"])
    except Exception as e:
        logger.warning("Could not fetch initial equity for risk reset: %s", e)

    running = True

    def _shutdown(sig, frame):
        nonlocal running
        logger.warning("Shutdown signal received -- finishing current tick.")
        running = False

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    while running:
        if is_market_open():
            try:
                run_loop(data_fetcher, None, ai_engine, executor, risk, store, research_store, massive_fetcher, earnings_cal)
            except Exception as e:
                logger.exception("Unhandled exception in agent loop: %s", e)
        else:
            logger.info("Market closed -- agent paused (NYSE open 15:30-22:00 Copenhagen time weekdays)")

        if running:
            time.sleep(config.agent.loop_interval_seconds)

    store.close()
    research_store.close()
    logger.info("Trading agent shut down cleanly.")


if __name__ == "__main__":
    main()
