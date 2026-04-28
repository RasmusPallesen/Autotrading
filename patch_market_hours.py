"""
Patch script — adds market hours check to trading and research agents.
Run from project root: python patch_market_hours.py
"""
import re

# ── Patch main.py ──────────────────────────────────────────────────────────────
with open("main.py", "r", encoding="utf-8") as f:
    main = f.read()

market_hours_fn = '''
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


'''

# Insert after imports, before validate_config
if "def is_market_open" not in main:
    main = main.replace("def validate_config():", market_hours_fn + "def validate_config():")
    print("Added is_market_open() to main.py")

# Replace the main while loop to check market hours
old_loop = '''    while running:
        try:
            run_loop(data_fetcher, None, ai_engine, executor, risk, store, research_store, massive_fetcher, earnings_cal)
        except Exception as e:
            logger.exception("Unhandled exception in agent loop: %s", e)

        if running:
            logger.info("Sleeping %ds until next tick...", config.agent.loop_interval_seconds)
            time.sleep(config.agent.loop_interval_seconds)'''

new_loop = '''    while running:
        if is_market_open():
            try:
                run_loop(data_fetcher, None, ai_engine, executor, risk, store, research_store, massive_fetcher, earnings_cal)
            except Exception as e:
                logger.exception("Unhandled exception in agent loop: %s", e)
        else:
            logger.info("Market closed -- agent paused (NYSE open 15:30-22:00 Copenhagen time weekdays)")

        if running:
            time.sleep(config.agent.loop_interval_seconds)'''

if old_loop in main:
    main = main.replace(old_loop, new_loop)
    print("Patched main.py while loop with market hours check")
else:
    print("WARNING: Could not find exact loop in main.py -- trying fallback")
    # Fallback: find the while loop more loosely
    main = re.sub(
        r'(    while running:\n        try:\n            run_loop\([^)]+\))',
        '    while running:\n        if is_market_open():\n            try:\n                run_loop(' +
        'data_fetcher, None, ai_engine, executor, risk, store, research_store, massive_fetcher, earnings_cal)',
        main
    )

with open("main.py", "w", encoding="utf-8") as f:
    f.write(main)
print("main.py patched")

# ── Patch research/research_agent.py ──────────────────────────────────────────
with open("research/research_agent.py", "r", encoding="utf-8") as f:
    research = f.read()

research_hours_fn = '''
def is_market_open() -> bool:
    """Returns True if NYSE is currently open (Mon-Fri 13:30-20:00 UTC)."""
    from datetime import datetime, timezone, time as _dtime
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    return _dtime(13, 30) <= now.time() <= _dtime(20, 0)


'''

if "def is_market_open" not in research:
    research = research.replace("def run_research_cycle(", research_hours_fn + "def run_research_cycle(")
    print("Added is_market_open() to research_agent.py")

# Replace the research while loop with market-aware sleep
old_research_loop = '''        if running:
            logger.info("Sleeping %ds until next cycle...", RESEARCH_INTERVAL)
            time.sleep(RESEARCH_INTERVAL)'''

new_research_loop = '''        if running:
            if is_market_open():
                interval = RESEARCH_INTERVAL
                logger.info("Market open -- next research cycle in %ds", interval)
            else:
                interval = 7200  # 2 hours outside market hours
                logger.info("Market closed -- next research cycle in 2h")
            time.sleep(interval)'''

if old_research_loop in research:
    research = research.replace(old_research_loop, new_research_loop)
    print("Patched research_agent.py with market-aware sleep interval")
else:
    print("WARNING: Could not find exact sleep line in research_agent.py")

# Also skip scanner outside market hours (no gainers/losers data)
old_scanner = '''    scanner_hits = []\n    binary_catalyst_symbols = []\n    try:\n        scanner_hits = scanner.scan(max_results=SCANNER_MAX_HITS)'''
new_scanner = '''    scanner_hits = []\n    binary_catalyst_symbols = []\n    if is_market_open():\n        try:\n            scanner_hits = scanner.scan(max_results=SCANNER_MAX_HITS)'''

if old_scanner in research:
    research = research.replace(old_scanner, new_scanner)
    # Close the if block properly
    research = research.replace(
        '    except Exception as e:\n        logger.warning("Market scanner error: %s", e)\n\n    # 3.',
        '        except Exception as e:\n            logger.warning("Market scanner error: %s", e)\n    else:\n        logger.info("Scanner skipped -- market closed")\n\n    # 3.'
    )
    print("Scanner now skips outside market hours")

with open("research/research_agent.py", "w", encoding="utf-8") as f:
    f.write(research)
print("research_agent.py patched")

print("\nDone! Restart both start_agent.bat and start_research.bat")
print("Trading agent will now only evaluate during 15:30-22:00 Copenhagen time (weekdays)")
print("Research agent will run every 15min during hours, every 2h outside hours")
