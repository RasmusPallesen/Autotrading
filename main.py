"""
Main agent loop.
Ties together data fetching, signal computation, AI decisions, risk checks, and execution.
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

# ── Logging setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, config.agent.log_level, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/agent.log"),
    ],
)
logger = logging.getLogger("main")


def validate_config():
    """Fail fast if critical credentials are missing."""
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


def run_loop(
    data_fetcher: AlpacaDataFetcher,
    signal_engine,
    ai_engine: AIDecisionEngine,
    executor: AlpacaExecutor,
    risk: RiskManager,
    store: TradeStore,
):
    """Single iteration of the agent loop."""
    logger.info("─── Agent loop tick ───────────────────────────────────────")

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

    # 2. Fetch market data for watchlist
    all_symbols = config.watchlist.stocks
    # Note: crypto symbols differ between Alpaca and Coinbase — stocks only here
    bars = data_fetcher.get_bars(
        symbols=all_symbols,
        lookback_bars=config.agent.indicator_lookback,
        timeframe="1Min",
    )

    # 3. Compute signals
    snapshots = []
    for symbol in all_symbols:
        df = bars.get(symbol)
        snapshot = compute_signals(symbol, df)
        if snapshot:
            snapshots.append(snapshot)

    if not snapshots:
        logger.warning("No valid snapshots computed this tick.")
        return

    # 4. AI decisions
    decisions = ai_engine.decide_batch(snapshots, portfolio, positions_map)

    # 5. Risk check + execution
    for decision in decisions:
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

        if not verdict.approved or decision.action == "HOLD":
            if not verdict.approved:
                logger.info("[%s] BLOCKED — %s", decision.symbol, verdict.reason)
            continue

        # Get current price for stop/target calculation
        current_price = data_fetcher.get_latest_price(decision.symbol)
        if not current_price:
            logger.warning("[%s] Could not fetch latest price, skipping.", decision.symbol)
            continue

        stop_loss, take_profit = risk.compute_stop_and_target(current_price, decision)
        notional = verdict.adjusted_notional

        if decision.action == "BUY":
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

        elif decision.action == "SELL":
            existing = positions_map.get(decision.symbol)
            if existing:
                result = executor.sell(symbol=decision.symbol, close_all=True)
                if result:
                    sold_value = existing.get("market_value", 0)
                    risk.record_sale(sold_value)
                    store.log_execution(
                        order_id=result.get("order_id", ""),
                        symbol=decision.symbol,
                        side="SELL",
                        notional=sold_value,
                    )
            else:
                logger.info("[%s] SELL signal but no open position.", decision.symbol)

    logger.info("─── Tick complete ─────────────────────────────────────────")


def main():
    validate_config()

    logger.info("🤖 Trading Agent starting up")
    logger.info("  Paper trading: %s", config.alpaca.paper)
    logger.info("  Loop interval: %ds", config.agent.loop_interval_seconds)
    logger.info("  Watchlist: %s", config.watchlist.stocks)

    # Initialise components
    data_fetcher = AlpacaDataFetcher(config.alpaca)
    ai_engine = AIDecisionEngine(config.anthropic)
    executor = AlpacaExecutor(config.alpaca)
    risk = RiskManager(config.risk)
    store = TradeStore()

    # Reset daily risk tracker
    try:
        account = data_fetcher.get_account()
        risk.reset_daily(account["equity"])
    except Exception as e:
        logger.warning("Could not fetch initial equity for risk reset: %s", e)

    # Graceful shutdown
    running = True
    def _shutdown(sig, frame):
        nonlocal running
        logger.warning("Shutdown signal received — finishing current tick.")
        running = False

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Main loop
    while running:
        try:
            run_loop(data_fetcher, None, ai_engine, executor, risk, store)
        except Exception as e:
            logger.exception("Unhandled exception in agent loop: %s", e)

        if running:
            logger.info("Sleeping %ds until next tick...", config.agent.loop_interval_seconds)
            time.sleep(config.agent.loop_interval_seconds)

    store.close()
    logger.info("Trading agent shut down cleanly.")


if __name__ == "__main__":
    main()
