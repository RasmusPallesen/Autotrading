"""
Patch script — adds insider trading section to dashboard_cloud.py
Run from project root: python patch_dashboard_insider.py
"""

with open("dashboard_cloud.py", "r", encoding="utf-8") as f:
    content = f.read()

# Add insider signals loader
old = 'def badge(text, css_class):'
new = '''def load_insider_signals() -> pd.DataFrame:
    """Load recent insider buy signals from research_signals table."""
    df = query("""
        SELECT symbol, sentiment, conviction, recommended_action, summary, ts
        FROM research_signals
        WHERE LOWER(summary) LIKE '%insider%'
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


def badge(text, css_class):'''
content = content.replace(old, new)

# Add insider section before research signals section
old = '# ── Research signals ─────────────────────────────────────────────────────────'
new = '''# ── Insider Trading ───────────────────────────────────────────────────────────
st.divider()
st.subheader("Insider Trading Signals")
insider_df = load_insider_signals()
if insider_df.empty:
    st.info("No insider trading signals detected in the last 14 days.")
else:
    for _, row in insider_df.iterrows():
        sc = sentiment_color(row["sentiment"])
        st.markdown(f"""
        <div style="border:1px solid #6d28d9;border-radius:8px;padding:12px 16px;
                    margin:8px 0;background:#1e1b4b;">
            <div style="display:flex;justify-content:space-between;
                        align-items:center;flex-wrap:wrap;gap:8px;">
                <div>
                    <span style="font-size:16px;font-weight:700;
                                 color:#a78bfa;">&#128274; {row["symbol"]}</span>
                    <span style="color:#6d28d9;font-size:12px;
                                 margin-left:8px;">INSIDER FILING</span>
                </div>
                <div>
                    <span style="background:{sc};color:white;padding:3px 8px;
                                 border-radius:4px;font-size:12px;margin-right:6px;">
                        {row["sentiment"]}</span>
                    <span style="background:#374151;color:white;padding:3px 8px;
                                 border-radius:4px;font-size:12px;">
                        {row["conviction_pct"]}% conviction</span>
                </div>
            </div>
            <p style="margin:8px 0 0;font-size:13px;color:#c4b5fd;">
                {row["summary"][:300]}</p>
        </div>
        """, unsafe_allow_html=True)

# ── Research signals ─────────────────────────────────────────────────────────'''
content = content.replace(old, new)

with open("dashboard_cloud.py", "w", encoding="utf-8") as f:
    f.write(content)

print("SUCCESS: Insider trading section added to dashboard")
