"""
Patch script — adds insider trading monitor to research agent.
Run from project root: python patch_insider.py
"""

with open("research\\research_agent.py", "r", encoding="utf-8") as f:
    content = f.read()

# Add import
old = 'from data.earnings_calendar import EarningsCalendar'
new = 'from data.earnings_calendar import EarningsCalendar\nfrom data.insider_monitor import InsiderMonitor'
content = content.replace(old, new)

# Init insider monitor in main()
old = '    earnings_cal = EarningsCalendar()'
new = '    earnings_cal = EarningsCalendar()\n    insider_monitor = InsiderMonitor()'
content = content.replace(old, new)

# Pass to run_research_cycle
old = '        run_research_cycle(analyst, store, scanner, earnings_cal)'
new = '        run_research_cycle(analyst, store, scanner, earnings_cal, insider_monitor)'
content = content.replace(old, new)

# Update signature
old = 'def run_research_cycle(analyst, store, scanner, earnings_cal=None):'
new = 'def run_research_cycle(analyst, store, scanner, earnings_cal=None, insider_monitor=None):'
content = content.replace(old, new)

# Add insider fetch step after earnings check
old = '    # 3. Collect from all sources'
new = '''    # 2c. Insider trading monitor
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

    # 3. Collect from all sources'''

content = content.replace(old, new)

# Add insider_items to all_items
old = '    all_items = news_items + sec_items + reddit_items + scanner_items'
new = '    all_items = news_items + sec_items + reddit_items + scanner_items + insider_items'
content = content.replace(old, new)

# Update log line
old = '''    logger.info(
        "Collected %d items -- news=%d, SEC=%d, Reddit=%d, Scanner=%d",
        len(all_items), len(news_items), len(sec_items), len(reddit_items), len(scanner_items),
    )'''
new = '''    logger.info(
        "Collected %d items -- news=%d, SEC=%d, Reddit=%d, Scanner=%d, Insider=%d",
        len(all_items), len(news_items), len(sec_items),
        len(reddit_items), len(scanner_items), len(insider_items),
    )'''
content = content.replace(old, new)

with open("research\\research_agent.py", "w", encoding="utf-8") as f:
    f.write(content)

print("SUCCESS: Insider monitor wired into research agent")
print("Place insider_monitor.py in the data/ folder")
print("Restart start_research.bat")
