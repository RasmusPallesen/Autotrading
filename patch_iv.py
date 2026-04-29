"""
Patch script — adds IV spike monitor to research agent and dashboard.
Run from project root: python patch_iv.py
"""

# ── Patch research_agent.py ────────────────────────────────────────────────────
with open("research\\research_agent.py", "r", encoding="utf-8") as f:
    content = f.read()

# Add import
old = 'from data.insider_monitor import InsiderMonitor'
new = 'from data.insider_monitor import InsiderMonitor\nfrom data.iv_monitor import IVMonitor'
content = content.replace(old, new)

# Init in main()
old = '    insider_monitor = InsiderMonitor()'
new = '    insider_monitor = InsiderMonitor()\n    iv_monitor = IVMonitor()'
content = content.replace(old, new)

# Pass to run_research_cycle
old = '        run_research_cycle(analyst, store, scanner, earnings_cal, insider_monitor)'
new = '        run_research_cycle(analyst, store, scanner, earnings_cal, insider_monitor, iv_monitor)'
content = content.replace(old, new)

# Update signature
old = 'def run_research_cycle(analyst, store, scanner, earnings_cal=None, insider_monitor=None):'
new = 'def run_research_cycle(analyst, store, scanner, earnings_cal=None, insider_monitor=None, iv_monitor=None):'
content = content.replace(old, new)

# Add IV scan step — runs after market close only
old = '    # 2c. Insider trading monitor'
new = '''    # 2b. IV spike monitor — runs after market close for end-of-day data
    iv_items = []
    if iv_monitor and not is_market_open():
        try:
            from collector import ResearchItem
            # Get earnings symbols to distinguish explained vs unexplained IV
            earnings_soon = []
            if earnings_cal:
                try:
                    ev = earnings_cal.get_events(all_symbols)
                    earnings_soon = [s for s, e in ev.items() if e.days_until <= 7]
                except Exception:
                    pass

            iv_spikes = iv_monitor.scan(all_symbols, earnings_symbols=earnings_soon)

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
        iv_items = []
        logger.debug("IV monitor skipped -- runs after market close only")

    # 2c. Insider trading monitor'''
content = content.replace(old, new)

# Add iv_items to all_items
old = '    all_items = news_items + sec_items + reddit_items + scanner_items + insider_items'
new = '    all_items = news_items + sec_items + reddit_items + scanner_items + insider_items + iv_items'
content = content.replace(old, new)

# Update collected items log
old = '''    logger.info(
        "Collected %d items -- news=%d, SEC=%d, Reddit=%d, Scanner=%d, Insider=%d",
        len(all_items), len(news_items), len(sec_items),
        len(reddit_items), len(scanner_items), len(insider_items),
    )'''
new = '''    logger.info(
        "Collected %d items -- news=%d, SEC=%d, Reddit=%d, Scanner=%d, Insider=%d, IV=%d",
        len(all_items), len(news_items), len(sec_items),
        len(reddit_items), len(scanner_items), len(insider_items), len(iv_items),
    )'''
content = content.replace(old, new)

with open("research\\research_agent.py", "w", encoding="utf-8") as f:
    f.write(content)
print("research_agent.py patched")

# ── Patch dashboard_cloud.py ───────────────────────────────────────────────────
with open("dashboard_cloud.py", "r", encoding="utf-8") as f:
    content = f.read()

# Add IV signals loader
old = 'def load_insider_signals'
new = '''def load_iv_signals() -> pd.DataFrame:
    df = query("""
        SELECT symbol, sentiment, conviction, recommended_action, summary, ts
        FROM research_signals
        WHERE LOWER(summary) LIKE '%iv spike%'
        AND expires_at > current_timestamp
        AND id IN (SELECT MAX(id) FROM research_signals
                   WHERE expires_at > current_timestamp GROUP BY symbol)
        ORDER BY conviction DESC, ts DESC LIMIT 20
    """)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("Europe/Copenhagen")
    df["conviction_pct"] = (df["conviction"] * 100).round(0).astype(int)
    return df


def load_insider_signals'''
content = content.replace(old, new)

# Add IV section before insider section
old = '# ── Insider Trading ───────────────────────────────────────────────────────────'
new = '''# ── IV Spike Monitor ──────────────────────────────────────────────────────────
st.divider()
st.subheader("Options IV Spike Monitor")
iv_df = load_iv_signals()
if iv_df.empty:
    st.info("No unusual IV spikes detected. Monitor runs after market close.")
else:
    for _, row in iv_df.iterrows():
        is_unusual = "UNUSUAL" in str(row.get("summary", "")).upper()
        border_color = "#f59e0b" if is_unusual else "#374151"
        bg_color = "#1c1404" if is_unusual else "#111827"
        label = "UNEXPLAINED SPIKE" if is_unusual else "IV SPIKE (earnings)"
        st.markdown(f"""
        <div style="border:1px solid {border_color};border-radius:8px;
                    padding:12px 16px;margin:8px 0;background:{bg_color};">
            <div style="display:flex;justify-content:space-between;
                        align-items:center;flex-wrap:wrap;gap:8px;">
                <div>
                    <span style="font-size:16px;font-weight:700;
                                 color:#f59e0b;">&#9650; {row["symbol"]}</span>
                    <span style="color:{border_color};font-size:12px;
                                 margin-left:8px;">{label}</span>
                </div>
                <span style="background:#374151;color:white;padding:3px 8px;
                             border-radius:4px;font-size:12px;">
                    {row["conviction_pct"]}% conviction</span>
            </div>
            <p style="margin:8px 0 0;font-size:13px;color:#9ca3af;">
                {str(row["summary"])[:350]}</p>
        </div>
        """, unsafe_allow_html=True)

# ── Insider Trading ───────────────────────────────────────────────────────────'''
content = content.replace(old, new)

with open("dashboard_cloud.py", "w", encoding="utf-8") as f:
    f.write(content)
print("dashboard_cloud.py patched")
print("\nDone! Steps:")
print("1. Place iv_monitor.py in data/ folder")
print("2. Restart start_research.bat")
print("3. git add dashboard_cloud.py && git commit -m 'Add IV monitor' && git push")
