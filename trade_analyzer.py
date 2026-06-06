"""
Trade History Analyzer — local desktop app.

Run with:
    streamlit run trade_analyzer.py --server.port 8502

Reads trade executions from the same DB as the live agent (PostgreSQL via
DATABASE_URL, or SQLite at logs/trades.db). Fetches OHLCV history from
Alpaca and overlays every BUY and SELL on an interactive candlestick chart.
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from plotly.subplots import make_subplots

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Trade Analyzer",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
body, .stApp { background:#0d1117; color:#f9fafb; }
[data-testid="stSidebar"] { background:#111827; }
.stDataFrame { font-size:12px; }
.metric-box {
    background:#1f2937; border-radius:8px; padding:12px 16px;
    text-align:center; margin:4px;
}
.metric-box .label { font-size:11px; color:#6b7280; margin-bottom:4px; }
.metric-box .value { font-size:20px; font-weight:700; color:#f9fafb; }
.metric-box .sub   { font-size:11px; color:#9ca3af; margin-top:2px; }
</style>
""", unsafe_allow_html=True)

# ── Credentials ───────────────────────────────────────────────────────────────
DATABASE_URL   = os.getenv("DATABASE_URL", "")
ALPACA_KEY     = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET  = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_DATA    = "https://data.alpaca.markets"
SQLITE_PATH    = Path(__file__).parent / "logs" / "trades.db"

_ALPACA_HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
}

# ── DB helpers ────────────────────────────────────────────────────────────────

def _is_postgres() -> bool:
    return DATABASE_URL.startswith("postgresql") or DATABASE_URL.startswith("postgres")


@st.cache_resource
def _pg_conn():
    import psycopg2
    from urllib.parse import urlparse, unquote
    url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    p = urlparse(url)
    return psycopg2.connect(
        host=p.hostname, port=p.port or 5432,
        dbname=p.path.lstrip("/"), user=p.username,
        password=unquote(p.password or ""), sslmode="require",
        connect_timeout=10,
    )


def _query(sql: str, params: tuple = ()) -> pd.DataFrame:
    """Run a SELECT and return a DataFrame. Works with both backends."""
    try:
        if _is_postgres():
            import psycopg2.extras
            conn = _pg_conn()
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
            return pd.DataFrame([dict(r) for r in rows])
        else:
            conn = sqlite3.connect(str(SQLITE_PATH))
            sql_lite = sql.replace("%s", "?")
            df = pd.read_sql_query(sql_lite, conn, params=params)
            conn.close()
            return df
    except Exception as e:
        st.warning(f"DB query failed: {e}")
        return pd.DataFrame()


# ── Data loaders ──────────────────────────────────────────────────────────────

@st.cache_data(ttl=30)
def load_traded_symbols() -> list[str]:
    df = _query("SELECT DISTINCT symbol FROM executions ORDER BY symbol")
    return df["symbol"].tolist() if not df.empty else []


@st.cache_data(ttl=30)
def load_executions(symbol: str) -> pd.DataFrame:
    df = _query(
        "SELECT ts, side, notional, qty, stop_loss, take_profit "
        "FROM executions WHERE symbol = %s ORDER BY ts",
        (symbol,),
    )
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    for col in ("notional", "qty", "stop_loss", "take_profit"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


@st.cache_data(ttl=30)
def load_decisions(symbol: str) -> pd.DataFrame:
    df = _query(
        "SELECT ts, action, confidence, rationale, urgency, approved "
        "FROM decisions WHERE symbol = %s ORDER BY ts",
        (symbol,),
    )
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df


@st.cache_data(ttl=120)
def fetch_bars(symbol: str, start: str, end: str, timeframe: str) -> pd.DataFrame:
    """Fetch OHLCV bars from Alpaca REST."""
    if not ALPACA_KEY:
        st.warning("ALPACA_API_KEY not set — cannot fetch price history.")
        return pd.DataFrame()
    try:
        params = {
            "timeframe": timeframe,
            "start": start,
            "end": end,
            "limit": 10000,
            "feed": "iex",
            "adjustment": "raw",
        }
        bars = []
        url = f"{ALPACA_DATA}/v2/stocks/{symbol}/bars"
        while url:
            r = requests.get(url, headers=_ALPACA_HEADERS, params=params, timeout=10)
            r.raise_for_status()
            data = r.json()
            bars.extend(data.get("bars") or [])
            next_token = data.get("next_page_token")
            if next_token:
                params = {"page_token": next_token}
            else:
                url = None
        if not bars:
            return pd.DataFrame()
        df = pd.DataFrame(bars)
        df["t"] = pd.to_datetime(df["t"], utc=True)
        df = df.rename(columns={"t": "ts", "o": "open", "h": "high",
                                  "l": "low", "c": "close", "v": "volume"})
        df = df.set_index("ts").sort_index()
        return df[["open", "high", "low", "close", "volume"]]
    except Exception as e:
        st.warning(f"Alpaca bar fetch failed: {e}")
        return pd.DataFrame()


# ── P&L pairing (FIFO) ───────────────────────────────────────────────────────

def pair_trades(executions: pd.DataFrame) -> pd.DataFrame:
    """
    Match BUY→SELL pairs by FIFO. Returns a DataFrame with one row per closed trade.
    Open (unmatched BUY) positions are included with sell_ts=NaT.
    """
    buys  = executions[executions["side"] == "BUY"].copy().reset_index(drop=True)
    sells = executions[executions["side"] == "SELL"].copy().reset_index(drop=True)

    pairs = []
    sell_queue = list(sells.itertuples())

    for buy in buys.itertuples():
        buy_price = (buy.notional / buy.qty) if (buy.qty and buy.qty > 0) else None
        if sell_queue:
            sell = sell_queue.pop(0)
            sell_price = (sell.notional / sell.qty) if (sell.qty and sell.qty > 0) else None
            pnl_usd = (sell.notional or 0) - (buy.notional or 0)
            pnl_pct = (pnl_usd / buy.notional * 100) if buy.notional else None
            hold_days = (sell.ts - buy.ts).total_seconds() / 86400
            pairs.append({
                "buy_ts":    buy.ts,
                "sell_ts":   sell.ts,
                "buy_price": buy_price,
                "sell_price": sell_price,
                "buy_notional":  buy.notional,
                "sell_notional": sell.notional,
                "qty":       buy.qty,
                "pnl_usd":  pnl_usd,
                "pnl_pct":  pnl_pct,
                "hold_days": round(hold_days, 1),
                "open":      False,
            })
        else:
            pairs.append({
                "buy_ts":    buy.ts,
                "sell_ts":   pd.NaT,
                "buy_price": buy_price,
                "sell_price": None,
                "buy_notional":  buy.notional,
                "sell_notional": None,
                "qty":       buy.qty,
                "pnl_usd":  None,
                "pnl_pct":  None,
                "hold_days": None,
                "open":      True,
            })

    return pd.DataFrame(pairs) if pairs else pd.DataFrame()


# ── Chart builder ─────────────────────────────────────────────────────────────

def build_chart(
    bars: pd.DataFrame,
    executions: pd.DataFrame,
    decisions: pd.DataFrame,
    symbol: str,
) -> go.Figure:
    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.04,
        row_heights=[0.78, 0.22],
    )

    # ── Candlestick ─────────────────────────────────────────────────────────
    fig.add_trace(go.Candlestick(
        x=bars.index,
        open=bars["open"], high=bars["high"],
        low=bars["low"],   close=bars["close"],
        name="Price",
        increasing_line_color="#22c55e",
        decreasing_line_color="#ef4444",
        increasing_fillcolor="#166534",
        decreasing_fillcolor="#7f1d1d",
    ), row=1, col=1)

    # ── Volume bars ──────────────────────────────────────────────────────────
    colors = [
        "#166534" if c >= o else "#7f1d1d"
        for o, c in zip(bars["open"], bars["close"])
    ]
    fig.add_trace(go.Bar(
        x=bars.index, y=bars["volume"],
        name="Volume", marker_color=colors,
        opacity=0.7,
    ), row=2, col=1)

    # ── BUY markers ──────────────────────────────────────────────────────────
    buys = executions[executions["side"] == "BUY"].copy()
    if not buys.empty and not bars.empty:
        # Snap buy timestamp to nearest bar for y-position
        buy_y = []
        for ts in buys["ts"]:
            idx = bars.index.searchsorted(ts, side="left")
            idx = min(idx, len(bars) - 1)
            buy_y.append(bars.iloc[idx]["low"] * 0.992)

        # Build hover text with decision rationale
        hover = []
        for _, row in buys.iterrows():
            closest_dec = None
            if not decisions.empty:
                mask = (decisions["action"] == "BUY") & \
                       (abs(decisions["ts"] - row["ts"]) < pd.Timedelta("5min"))
                matching = decisions[mask]
                if not matching.empty:
                    closest_dec = matching.iloc[0]
            price_str = f"${row.notional / row.qty:.2f}" if row.qty else "—"
            rationale = (closest_dec["rationale"][:120] + "…") if closest_dec is not None and pd.notna(closest_dec["rationale"]) else ""
            conf = f"{closest_dec['confidence']:.0%}" if closest_dec is not None else ""
            hover.append(
                f"<b>BUY {symbol}</b><br>"
                f"Time: {row.ts.strftime('%Y-%m-%d %H:%M')}<br>"
                f"Price: {price_str}<br>"
                f"Notional: ${row.notional:,.2f}<br>"
                f"Confidence: {conf}<br>"
                f"{rationale}"
            )

        fig.add_trace(go.Scatter(
            x=buys["ts"], y=buy_y,
            mode="markers",
            marker=dict(symbol="triangle-up", size=14, color="#22c55e",
                        line=dict(color="#f9fafb", width=1)),
            name="BUY",
            text=hover,
            hoverinfo="text",
        ), row=1, col=1)

    # ── SELL markers ─────────────────────────────────────────────────────────
    sells = executions[executions["side"] == "SELL"].copy()
    if not sells.empty and not bars.empty:
        sell_y = []
        for ts in sells["ts"]:
            idx = bars.index.searchsorted(ts, side="left")
            idx = min(idx, len(bars) - 1)
            sell_y.append(bars.iloc[idx]["high"] * 1.008)

        hover = []
        for _, row in sells.iterrows():
            closest_dec = None
            if not decisions.empty:
                mask = (decisions["action"] == "SELL") & \
                       (abs(decisions["ts"] - row["ts"]) < pd.Timedelta("5min"))
                matching = decisions[mask]
                if not matching.empty:
                    closest_dec = matching.iloc[0]
            price_str = f"${row.notional / row.qty:.2f}" if row.qty else "—"
            rationale = (closest_dec["rationale"][:120] + "…") if closest_dec is not None and pd.notna(closest_dec["rationale"]) else ""
            hover.append(
                f"<b>SELL {symbol}</b><br>"
                f"Time: {row.ts.strftime('%Y-%m-%d %H:%M')}<br>"
                f"Price: {price_str}<br>"
                f"Notional: ${row.notional:,.2f}<br>"
                f"{rationale}"
            )

        fig.add_trace(go.Scatter(
            x=sells["ts"], y=sell_y,
            mode="markers",
            marker=dict(symbol="triangle-down", size=14, color="#ef4444",
                        line=dict(color="#f9fafb", width=1)),
            name="SELL",
            text=hover,
            hoverinfo="text",
        ), row=1, col=1)

    # ── Layout ───────────────────────────────────────────────────────────────
    fig.update_layout(
        paper_bgcolor="#0d1117",
        plot_bgcolor="#111827",
        font=dict(color="#9ca3af", size=11),
        margin=dict(l=10, r=10, t=30, b=10),
        height=560,
        legend=dict(orientation="h", yanchor="bottom", y=1.02,
                    bgcolor="rgba(0,0,0,0)"),
        xaxis_rangeslider_visible=False,
        hovermode="x unified",
        xaxis2=dict(showgrid=True, gridcolor="#1f2937"),
        yaxis=dict(showgrid=True, gridcolor="#1f2937", side="right"),
        yaxis2=dict(showgrid=False, side="right"),
    )
    fig.update_xaxes(showspikes=True, spikecolor="#6b7280", spikethickness=1)
    return fig


# ── Stats helpers ─────────────────────────────────────────────────────────────

def _stat_card(label: str, value: str, sub: str = "", colour: str = "#f9fafb") -> str:
    return (
        f'<div class="metric-box">'
        f'<div class="label">{label}</div>'
        f'<div class="value" style="color:{colour};">{value}</div>'
        f'<div class="sub">{sub}</div>'
        f'</div>'
    )


# ── Sidebar ───────────────────────────────────────────────────────────────────

st.sidebar.markdown("## 📊 Trade Analyzer")

symbols = load_traded_symbols()
if not symbols:
    st.sidebar.error("No traded symbols found in DB.")
    st.stop()

symbol = st.sidebar.selectbox("Symbol", symbols)

today      = datetime.now(timezone.utc).date()
default_start = today - timedelta(days=180)

col_s, col_e = st.sidebar.columns(2)
start_date = col_s.date_input("From", value=default_start, max_value=today)
end_date   = col_e.date_input("To",   value=today, max_value=today)

timeframe = st.sidebar.radio(
    "Bar interval",
    options=["1Day", "1Hour", "15Min"],
    index=0,
    horizontal=True,
)

st.sidebar.markdown("---")
st.sidebar.caption(
    f"DB: {'PostgreSQL' if _is_postgres() else 'SQLite'}\n\n"
    f"Alpaca: {'✅ connected' if ALPACA_KEY else '❌ no key'}"
)

# ── Main ──────────────────────────────────────────────────────────────────────

st.markdown(f"### {symbol} — Trade History")

executions = load_executions(symbol)
decisions  = load_decisions(symbol)

start_str = start_date.isoformat() + "T00:00:00Z"
end_str   = end_date.isoformat()   + "T23:59:59Z"

bars = fetch_bars(symbol, start_str, end_str, timeframe)

# Filter executions to selected date range
if not executions.empty:
    mask = (executions["ts"].dt.date >= start_date) & \
           (executions["ts"].dt.date <= end_date)
    exec_range = executions[mask]
else:
    exec_range = executions

# ── Chart ─────────────────────────────────────────────────────────────────────
if bars.empty:
    st.info("No price data available for the selected range. Check Alpaca credentials and date range.")
else:
    fig = build_chart(bars, exec_range, decisions, symbol)
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

# ── Stats row ─────────────────────────────────────────────────────────────────
pairs = pair_trades(executions)  # use ALL executions for lifetime stats

if not pairs.empty:
    closed = pairs[~pairs["open"]]
    total_trades = len(closed)
    wins         = (closed["pnl_usd"] > 0).sum() if total_trades else 0
    win_rate     = wins / total_trades * 100 if total_trades else 0
    total_pnl    = closed["pnl_usd"].sum() if total_trades else 0
    avg_hold     = closed["hold_days"].mean() if total_trades else 0
    best         = closed["pnl_pct"].max() if total_trades else 0
    worst        = closed["pnl_pct"].min() if total_trades else 0
    open_count   = pairs["open"].sum()

    pnl_col   = "#22c55e" if total_pnl >= 0 else "#ef4444"
    wr_col    = "#22c55e" if win_rate >= 50 else "#ef4444"
    best_col  = "#22c55e" if best >= 0 else "#ef4444"
    worst_col = "#22c55e" if worst >= 0 else "#ef4444"

    cards = "".join([
        _stat_card("Total trades",  str(total_trades)),
        _stat_card("Win rate",      f"{win_rate:.0f}%",   colour=wr_col),
        _stat_card("Total P&L",     f"${total_pnl:+,.2f}", colour=pnl_col),
        _stat_card("Avg hold",      f"{avg_hold:.1f}d"),
        _stat_card("Best trade",    f"{best:+.1f}%",  colour=best_col),
        _stat_card("Worst trade",   f"{worst:+.1f}%", colour=worst_col),
        _stat_card("Open positions", str(int(open_count))),
    ])
    st.markdown(
        f'<div style="display:flex;flex-wrap:wrap;gap:6px;margin:12px 0;">{cards}</div>',
        unsafe_allow_html=True,
    )
else:
    st.info(f"No trades found for {symbol}.")

# ── Trade log table ───────────────────────────────────────────────────────────
if not pairs.empty:
    st.markdown("#### Trade Log")

    display = pairs.copy()
    display["Entry date"]  = display["buy_ts"].dt.strftime("%Y-%m-%d %H:%M")
    display["Exit date"]   = display["sell_ts"].apply(
        lambda x: x.strftime("%Y-%m-%d %H:%M") if pd.notna(x) else "OPEN"
    )
    display["Entry price"] = display["buy_price"].apply(
        lambda x: f"${x:.2f}" if pd.notna(x) else "—"
    )
    display["Exit price"]  = display["sell_price"].apply(
        lambda x: f"${x:.2f}" if pd.notna(x) else "—"
    )
    display["Notional"]    = display["buy_notional"].apply(
        lambda x: f"${x:,.2f}" if pd.notna(x) else "—"
    )
    display["Hold (days)"] = display["hold_days"].apply(
        lambda x: f"{x:.1f}" if pd.notna(x) else "—"
    )
    display["P&L $"]  = display["pnl_usd"].apply(
        lambda x: f"${x:+,.2f}" if pd.notna(x) else "—"
    )
    display["P&L %"]  = display["pnl_pct"].apply(
        lambda x: f"{x:+.2f}%" if pd.notna(x) else "—"
    )

    show_cols = ["Entry date", "Exit date", "Entry price", "Exit price",
                 "Notional", "Hold (days)", "P&L $", "P&L %"]
    st.dataframe(
        display[show_cols],
        use_container_width=True,
        hide_index=True,
    )

# ── Raw decisions expander ────────────────────────────────────────────────────
if not decisions.empty:
    with st.expander(f"All agent decisions for {symbol} ({len(decisions)} total)"):
        show = decisions[["ts", "action", "confidence", "urgency",
                          "approved", "approval_reason", "rationale"]].copy()
        show["ts"] = show["ts"].dt.strftime("%Y-%m-%d %H:%M")
        show["confidence"] = show["confidence"].apply(
            lambda x: f"{x:.0%}" if pd.notna(x) else "—"
        )
        st.dataframe(show, use_container_width=True, hide_index=True)
