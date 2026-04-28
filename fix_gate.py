"""
Patch script — fixes research gate to always include core watchlist.
Run from project root: python fix_gate.py
"""

with open("main.py", "r", encoding="utf-8") as f:
    content = f.read()

old = '''# Gate universe symbols through research signals
    for symbol in full_universe:
        signal = research_signals.get(symbol)
        if signal:
            conviction = float(signal.get("conviction", 0))
            sentiment = signal.get("sentiment", "NEUTRAL")
            if conviction >= min_conviction:
                active_symbols.append(symbol)
            else:
                skipped.append(f"{symbol}({conviction:.0%})")
        else:
            # No research signal yet — include anyway so agent always
            # evaluates the core watchlist even before research runs
            if symbol in config.watchlist.stocks:
                active_symbols.append(symbol)
            else:
                skipped.append(f"{symbol}(no signal)")'''

new = '''# Gate universe symbols through research signals
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
            skipped.append(f"{symbol}(no signal)")'''

if old in content:
    content = content.replace(old, new)
    with open("main.py", "w", encoding="utf-8") as f:
        f.write(content)
    print("SUCCESS: Research gate fixed — core watchlist always active")
else:
    print("ERROR: Could not find gate logic in main.py")
    print("Make sure you are running this from the project root folder")
