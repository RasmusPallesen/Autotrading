"""
Patch script — adds position trend charts to dashboard.py
Run this from your project root:
    python positions_chart_patch.py
"""

import re

with open("dashboard.py", "r", encoding="utf-8") as f:
    content = f.read()

# The new positions chart section to insert after the open positions table
new_section = '''
# ── Position Trend Charts ──────────────────────────────────────────────────────
if positions:
    st.subheader("Position Trends (intraday)")
    st.caption("Price history fetched from Alpaca IEX feed — last 50 bars")

    ALPACA_DATA_BASE = "https://data.alpaca.markets"

    def fetch_position_bars(symbol: str, limit: int = 50) -> list:
        """Fetch recent 1-min bars for a position symbol."""
        try:
            from datetime import datetime, timedelta, timezone
            end = datetime.now(timezone.utc)
            start = end - timedelta(hours=4)
            resp = requests.get(
                f"{ALPACA_DATA_BASE}/v2/stocks/{symbol}/bars",
                headers={
                    "APCA-API-KEY-ID": ALPACA_API_KEY,
                    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
                },
                params={
                    "timeframe": "1Min",
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "limit": limit,
                    "feed": "iex",
                },
                timeout=8,
            )
            resp.raise_for_status()
            bars = resp.json().get("bars", [])
            return bars
        except Exception:
            return []

    # Show charts in a grid — 2 per row
    pos_chunks = [positions[i:i+2] for i in range(0, len(positions), 2)]
    for chunk in pos_chunks:
        cols = st.columns(len(chunk))
        for col, pos in zip(cols, chunk):
            symbol = pos.get("symbol", "")
            entry  = float(pos.get("avg_entry_price", 0))
            current = float(pos.get("current_price", 0))
            pnl_pct = float(pos.get("unrealized_plpc", 0)) * 100

            with col:
                bars = fetch_position_bars(symbol)
                pnl_color = "normal" if pnl_pct >= 0 else "inverse"
                st.metric(
                    label=f"{symbol}",
                    value=f"${current:.2f}",
                    delta=f"{pnl_pct:.2f}% (entry ${entry:.2f})",
                    delta_color=pnl_color,
                )

                if bars:
                    import pandas as pd
                    df = pd.DataFrame(bars)
                    df["t"] = pd.to_datetime(df["t"]).dt.tz_convert("Europe/Copenhagen")
                    df = df.set_index("t")[["c"]].rename(columns={"c": "Price"})

                    # Add entry price line
                    df["Entry"] = entry

                    # Colour the chart green/red based on P&L
                    st.line_chart(df, height=160)
                else:
                    st.caption("No intraday data available (market closed or outside hours)")

    st.divider()

'''

# Insert after the open positions dataframe section
# Find the marker right after the positions table
old_marker = "st.divider()\n\n# ── Decision KPIs"
new_marker = new_section + "# ── Decision KPIs"

if old_marker in content:
    content = content.replace(old_marker, new_marker)
    with open("dashboard.py", "w", encoding="utf-8") as f:
        f.write(content)
    print("SUCCESS: Position trend charts added to dashboard.py")
else:
    print("ERROR: Could not find insertion point.")
    print("Manually add the following section after your open positions table:")
    print("---")
    print(new_section)
