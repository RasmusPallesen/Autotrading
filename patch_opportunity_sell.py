"""
Patch script — adds opportunity-cost selling logic to main.py
When max positions reached and a high-conviction BUY appears,
the agent evaluates selling the weakest position to fund it.
Run from project root: python patch_opportunity_sell.py
"""

with open("main.py", "r", encoding="utf-8") as f:
    content = f.read()

# Add opportunity cost evaluation function after imports
old = 'def validate_config():'
new = '''def find_weakest_position(positions: list, positions_map: dict,
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


def validate_config():'''

content = content.replace(old, new)

# Add opportunity-cost logic in the execution loop
# Find where BUY decisions are processed when positions are full
old = '''        if decision.action == "BUY":
            result = executor.buy(
                symbol=decision.symbol,
                notional=notional,
                stop_loss_price=stop_loss,
                take_profit_price=take_profit,
            )'''

new = '''        if decision.action == "BUY":
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
            )'''

content = content.replace(old, new)

with open("main.py", "w", encoding="utf-8") as f:
    f.write(content)

print("SUCCESS: Opportunity-cost selling added to main.py")
print("Restart start_agent.bat")
