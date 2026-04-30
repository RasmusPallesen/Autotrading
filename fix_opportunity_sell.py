"""
Fixes opportunity-cost selling — moves the check BEFORE the risk verdict
so it fires when max positions is reached.
Run from project root: python fix_opportunity_sell.py
"""

with open("main.py", "r", encoding="utf-8") as f:
    content = f.read()

# Find the execution loop and add opportunity check before risk.check()
old = '''        verdict = risk.check(
            decision=decision,
            portfolio=portfolio,
            positions=positions,
            min_confidence=config.agent.min_confidence,
        )

        store.log_decision('''

new = '''        # Opportunity-cost check: before risk verdict, evaluate selling
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

        store.log_decision('''

if old in content:
    content = content.replace(old, new)
    print("SUCCESS: Opportunity sell moved before risk check")
else:
    print("ERROR: Could not find insertion point")
    print("Make sure you have run patch_opportunity_sell.py first")

with open("main.py", "w", encoding="utf-8") as f:
    f.write(content)
