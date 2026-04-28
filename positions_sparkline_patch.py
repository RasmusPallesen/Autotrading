"""
Patch script — replaces the open positions table with an integrated
sparkline chart version.
Run from your project root: python positions_sparkline_patch.py
"""

NEW_POSITIONS_SECTION = '''
if positions:
    st.subheader("Open Positions")

    ALPACA_DATA_BASE = "https://data.alpaca.markets"

    def fetch_bars(symbol: str) -> list:
        try:
            from datetime import datetime, timedelta, timezone
            end = datetime.now(timezone.utc)
            start = end - timedelta(hours=6)
            resp = requests.get(
                f"{ALPACA_DATA_BASE}/v2/stocks/{symbol}/bars",
                headers={
                    "APCA-API-KEY-ID": ALPACA_API_KEY,
                    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
                },
                params={
                    "timeframe": "5Min",
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "limit": 60,
                    "feed": "iex",
                },
                timeout=6,
            )
            resp.raise_for_status()
            return [b["c"] for b in resp.json().get("bars", [])]
        except Exception:
            return []

    def sparkline_svg(prices: list, entry: float, width: int = 120, height: int = 40) -> str:
        if len(prices) < 2:
            return f\'<svg width="{width}" height="{height}"><text x="4" y="20" font-size="10" fill="#888">no data</text></svg>\'
        lo, hi = min(prices + [entry]), max(prices + [entry])
        rng = hi - lo if hi != lo else 1
        def px(v): return width - int((v - lo) / rng * width)
        def py(v): return height - int((v - lo) / rng * (height - 4)) - 2
        pts = " ".join(f"{px(p)},{py(p)}" for p in prices)
        color = "#22c55e" if prices[-1] >= entry else "#ef4444"
        ey = py(entry)
        return (
            f\'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">\'
            f\'<line x1="0" y1="{ey}" x2="{width}" y2="{ey}" stroke="#6b7280" stroke-width="0.8" stroke-dasharray="3,2"/>\'
            f\'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.5" stroke-linejoin="round"/>\'
            f\'<circle cx="{px(prices[-1])}" cy="{py(prices[-1])}" r="2.5" fill="{color}"/>\'
            f\'</svg>\'
        )

    rows_html = ""
    for p in positions:
        sym    = p.get("symbol", "")
        qty    = float(p.get("qty", 0))
        entry  = float(p.get("avg_entry_price", 0))
        curr   = float(p.get("current_price", 0))
        mval   = float(p.get("market_value", 0))
        pnl    = float(p.get("unrealized_pl", 0))
        pnl_pct = float(p.get("unrealized_plpc", 0)) * 100
        pnl_color = "#22c55e" if pnl >= 0 else "#ef4444"
        arrow  = "▲" if pnl >= 0 else "▼"

        prices = fetch_bars(sym)
        spark  = sparkline_svg(prices, entry)

        rows_html += f"""
        <tr style="border-bottom:0.5px solid var(--color-border-tertiary);">
            <td style="padding:10px 8px;font-weight:500;font-size:14px;">{sym}</td>
            <td style="padding:10px 8px;font-size:13px;color:var(--color-text-secondary);">{qty:.4f}</td>
            <td style="padding:10px 8px;font-size:13px;">${entry:.2f}</td>
            <td style="padding:10px 8px;font-size:13px;">${curr:.2f}</td>
            <td style="padding:10px 8px;font-size:13px;">${mval:,.2f}</td>
            <td style="padding:10px 8px;font-size:13px;color:{pnl_color};font-weight:500;">
                {arrow} ${abs(pnl):.2f}<br>
                <span style="font-size:11px;font-weight:400;">{pnl_pct:+.2f}%</span>
            </td>
            <td style="padding:6px 8px;">{spark}</td>
        </tr>"""

    st.markdown(f"""
    <table style="width:100%;border-collapse:collapse;font-family:var(--font-sans);">
        <thead>
            <tr style="border-bottom:1px solid var(--color-border-secondary);">
                <th style="padding:8px;text-align:left;font-size:12px;color:var(--color-text-secondary);font-weight:500;">Symbol</th>
                <th style="padding:8px;text-align:left;font-size:12px;color:var(--color-text-secondary);font-weight:500;">Qty</th>
                <th style="padding:8px;text-align:left;font-size:12px;color:var(--color-text-secondary);font-weight:500;">Entry</th>
                <th style="padding:8px;text-align:left;font-size:12px;color:var(--color-text-secondary);font-weight:500;">Current</th>
                <th style="padding:8px;text-align:left;font-size:12px;color:var(--color-text-secondary);font-weight:500;">Value</th>
                <th style="padding:8px;text-align:left;font-size:12px;color:var(--color-text-secondary);font-weight:500;">P&L</th>
                <th style="padding:8px;text-align:left;font-size:12px;color:var(--color-text-secondary);font-weight:500;">6h trend</th>
            </tr>
        </thead>
        <tbody>{rows_html}</tbody>
    </table>
    """, unsafe_allow_html=True)

'''

OLD_SECTION = '''if positions:
    st.subheader("Open Positions")
    pos_df = pd.DataFrame([{
        "Symbol":       p.get("symbol"),
        "Qty":          float(p.get("qty", 0)),
        "Entry Price":  f"${float(p.get(\'avg_entry_price\', 0)):,.2f}",
        "Current":      f"${float(p.get(\'current_price\', 0)):,.2f}",
        "Market Value": f"${float(p.get(\'market_value\', 0)):,.2f}",
        "Unreal. P&L":  f"${float(p.get(\'unrealized_pl\', 0)):,.2f}",
        "P&L %":        f"{float(p.get(\'unrealized_plpc\', 0))*100:.2f}%",
    } for p in positions])
    st.dataframe(pos_df, use_container_width=True, hide_index=True)'''

with open("dashboard.py", "r", encoding="utf-8") as f:
    content = f.read()

if OLD_SECTION in content:
    content = content.replace(OLD_SECTION, NEW_POSITIONS_SECTION)
    with open("dashboard.py", "w", encoding="utf-8") as f:
        f.write(content)
    print("SUCCESS: Positions table replaced with sparkline version")
else:
    print("Could not find exact match. Writing new section to positions_insert.txt")
    with open("positions_insert.txt", "w") as f:
        f.write(NEW_POSITIONS_SECTION)
    print("Paste the contents of positions_insert.txt into dashboard.py")
    print("replacing the 'if positions:' block that shows st.dataframe")
