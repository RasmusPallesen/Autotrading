"""
Trading Agent Dashboard — Streamlit Community Cloud compatible.
Reads from PostgreSQL using Streamlit secrets.
Mobile-first redesign: single-column layout, card-based UI,
touch-friendly tap targets, no horizontal scroll.
"""

import os
import time
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from risk.settlement_tracker import settlement_date

st.set_page_config(
    page_title="Trading Agent",
    page_icon="📈",
    layout="centered",           # ← was "wide" — centered works on all screen sizes
    initial_sidebar_state="collapsed",
)

if "confirm_sell" not in st.session_state:
    st.session_state.confirm_sell = None
if "sell_result" not in st.session_state:
    st.session_state.sell_result = None

st.markdown("""
<style>
    /* ── Reset & base ─────────────────────────────────────────────── */
    @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Syne:wght@400;600;700;800&display=swap');

    html, body, [class*="css"] {
        font-family: 'Syne', sans-serif;
    }

    /* ── Black canvas ─────────────────────────────────────────────── */
    .stApp, .stApp > div, section[data-testid="stSidebar"],
    [data-testid="stAppViewContainer"], [data-testid="stHeader"] {
        background: #000000 !important;
    }
    .stTabs [data-baseweb="tab-list"] {
        background: #000000 !important;
    }

    .block-container {
        padding: 1rem 1rem 2rem 1rem !important;
        max-width: 100% !important;
    }

    /* ── Mobile metric grid ───────────────────────────────────────── */
    .metric-grid {
        display: grid;
        grid-template-columns: repeat(2, 1fr);
        gap: 8px;
        margin-bottom: 16px;
    }
    .metric-grid.three { grid-template-columns: repeat(3, 1fr); }
    .metric-grid.four  { grid-template-columns: repeat(4, 1fr); }

    .metric-card {
        background: #0d1117;
        border: 1px solid #1f2937;
        border-radius: 10px;
        padding: 10px 12px;
    }
    .metric-label {
        font-size: 10px;
        font-weight: 600;
        color: #9ca3af;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        margin-bottom: 4px;
    }
    .metric-value {
        font-family: 'JetBrains Mono', monospace;
        font-size: 18px;
        font-weight: 700;
        color: #f9fafb;
        line-height: 1.1;
    }
    .metric-delta {
        font-family: 'JetBrains Mono', monospace;
        font-size: 11px;
        margin-top: 2px;
    }
    .delta-pos { color: #22c55e; }
    .delta-neg { color: #ef4444; }

    /* ── Cards ────────────────────────────────────────────────────── */
    .card {
        background: #0d1117;
        border: 1px solid #1f2937;
        border-radius: 12px;
        padding: 14px 16px;
        margin-bottom: 10px;
    }
    .card-header {
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        flex-wrap: wrap;
        gap: 6px;
        margin-bottom: 8px;
    }
    .card-symbol {
        font-family: 'JetBrains Mono', monospace;
        font-size: 18px;
        font-weight: 700;
        color: #f9fafb;
    }
    .card-badges {
        display: flex;
        flex-wrap: wrap;
        gap: 4px;
    }
    .badge {
        font-size: 10px;
        font-weight: 700;
        padding: 3px 7px;
        border-radius: 4px;
        white-space: nowrap;
    }
    .badge-buy    { background: #052e16; color: #22c55e; border: 1px solid #166534; }
    .badge-sell   { background: #450a0a; color: #ef4444; border: 1px solid #991b1b; }
    .badge-hold   { background: #1f2937; color: #d1d5db; border: 1px solid #374151; }
    .badge-yes    { background: #052e16; color: #22c55e; }
    .badge-no     { background: #450a0a; color: #ef4444; }
    .badge-high   { background: #450a0a; color: #ef4444; }
    .badge-medium { background: #451a03; color: #f59e0b; }
    .badge-low    { background: #1f2937; color: #d1d5db; }
    .badge-bull   { background: #052e16; color: #22c55e; }
    .badge-bear   { background: #450a0a; color: #ef4444; }
    .badge-neutral{ background: #1f2937; color: #d1d5db; }
    .badge-watch  { background: #451a03; color: #f59e0b; }
    .badge-pct    { background: #1f2937; color: #e5e7eb; }
    .badge-purple { background: #2e1065; color: #a78bfa; border: 1px solid #4c1d95; }

    .card-meta {
        font-family: 'JetBrains Mono', monospace;
        font-size: 11px;
        color: #9ca3af;
        margin-bottom: 6px;
    }
    .card-text {
        font-size: 12px;
        color: #d1d5db;
        line-height: 1.5;
    }
    .card-note {
        font-size: 11px;
        color: #9ca3af;
        margin-top: 6px;
        padding-top: 6px;
        border-top: 1px solid #1f2937;
    }

    /* ── Position row ─────────────────────────────────────────────── */
    .pos-row {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 10px 0;
        border-bottom: 1px solid #1f2937;
    }
    .pos-row:last-child { border-bottom: none; }
    .pos-symbol { font-family: 'JetBrains Mono', monospace; font-size: 15px; font-weight: 700; color: #f9fafb; }
    .pos-detail { font-size: 11px; color: #9ca3af; margin-top: 2px; }
    .pos-pnl    { font-family: 'JetBrains Mono', monospace; font-size: 14px; font-weight: 700; text-align: right; }

    /* ── Scanner banner ───────────────────────────────────────────── */
    .scanner-banner {
        background: linear-gradient(135deg, #1a1a2e, #16213e);
        border: 1px solid #f59e0b;
        border-radius: 12px;
        padding: 14px 16px;
        margin-bottom: 16px;
    }
    .scanner-title {
        color: #f59e0b;
        font-size: 14px;
        font-weight: 800;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        margin-bottom: 12px;
        display: flex;
        align-items: center;
        gap: 8px;
    }
    .scanner-live {
        background: #f59e0b;
        color: #000;
        font-size: 9px;
        font-weight: 800;
        padding: 2px 6px;
        border-radius: 10px;
        letter-spacing: 0.05em;
    }
    .scanner-grid {
        display: grid;
        grid-template-columns: repeat(2, 1fr);
        gap: 8px;
    }
    .scanner-card {
        background: #0d1117;
        border: 1px solid #f59e0b33;
        border-radius: 8px;
        padding: 10px;
    }
    .scanner-sym { font-family: 'JetBrains Mono', monospace; font-size: 16px; font-weight: 800; color: #f59e0b; }
    .scanner-text { font-size: 10px; color: #9ca3af; margin-top: 6px; line-height: 1.4; }

    /* ── Section headers ──────────────────────────────────────────── */
    .section-header {
        font-size: 13px;
        font-weight: 800;
        color: #9ca3af;
        text-transform: uppercase;
        letter-spacing: 0.1em;
        margin: 20px 0 10px 0;
        display: flex;
        align-items: center;
        gap: 8px;
    }
    .section-header::after {
        content: '';
        flex: 1;
        height: 1px;
        background: #1f2937;
    }

    /* ── Stat row (decision KPIs) ─────────────────────────────────── */
    .stat-row {
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 6px;
        margin-bottom: 12px;
    }
    .stat-box {
        background: #0d1117;
        border: 1px solid #1f2937;
        border-radius: 8px;
        padding: 8px 10px;
        text-align: center;
    }
    .stat-val { font-family: 'JetBrains Mono', monospace; font-size: 20px; font-weight: 700; color: #f9fafb; }
    .stat-lbl { font-size: 9px; color: #9ca3af; text-transform: uppercase; letter-spacing: 0.05em; }

    /* ── Streamlit overrides ──────────────────────────────────────── */
    div[data-testid="metric-container"] { display: none; }
    div[data-testid="stExpander"] > div { padding: 0; }
    .stSelectbox > div, .stMultiSelect > div { font-size: 13px; }

    /* Scrollable table wrapper */
    .table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; border-radius: 8px; }
    .table-wrap table { width: 100%; border-collapse: collapse; font-size: 12px; }
    .table-wrap th { background: #111827; color: #9ca3af; font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em; padding: 8px 10px; text-align: left; font-weight: 600; border-bottom: 1px solid #1f2937; white-space: nowrap; }
    .table-wrap td { padding: 8px 10px; border-bottom: 1px solid #0d1117; color: #e5e7eb; vertical-align: top; }
    .table-wrap tr:last-child td { border-bottom: none; }
    .table-wrap tr:hover td { background: #111827; }

    /* Hide default streamlit elements */
    #MainMenu { visibility: hidden; }
    footer    { visibility: hidden; }
    header    { visibility: hidden; }

    /* ── Detail panel ─────────────────────────────────────────────── */
    .detail-panel {
        background: #111827;
        border: 1px solid #374151;
        border-radius: 12px;
        padding: 16px;
        margin: -4px 0 10px 0;
        animation: slideDown 0.15s ease;
    }
    @keyframes slideDown {
        from { opacity: 0; transform: translateY(-6px); }
        to   { opacity: 1; transform: translateY(0); }
    }
    .detail-title {
        font-size: 11px;
        font-weight: 700;
        color: #9ca3af;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        margin: 12px 0 6px 0;
    }
    .detail-title:first-child { margin-top: 0; }
    .detail-body { font-size: 13px; color: #d1d5db; line-height: 1.6; }
    .detail-point {
        display: flex;
        gap: 8px;
        padding: 5px 0;
        border-bottom: 1px solid #1f2937;
        font-size: 12px;
        color: #d1d5db;
        line-height: 1.4;
    }
    .detail-point:last-child { border-bottom: none; }
    .detail-point-icon { flex-shrink: 0; color: #9ca3af; }
    .detail-risk {
        display: flex;
        gap: 8px;
        padding: 5px 0;
        border-bottom: 1px solid #1f2937;
        font-size: 12px;
        color: #fca5a5;
        line-height: 1.4;
    }
    .detail-risk:last-child { border-bottom: none; }
    .detail-link {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        font-size: 12px;
        color: #60a5fa;
        text-decoration: none;
        margin-top: 10px;
        padding: 6px 0;
    }

    /* Tap-to-expand button style */
    /* ── Expand/collapse card buttons ─────────────────────────────── */
    div[data-testid="stButton"] > button {
        background: #111827 !important;
        border: 1px solid #1f2937 !important;
        border-top: none !important;
        border-radius: 0 0 10px 10px !important;
        color: #4b5563 !important;
        font-size: 11px !important;
        font-weight: 600 !important;
        letter-spacing: 0.05em !important;
        padding: 6px 12px !important;
        margin-top: -2px !important;
        margin-bottom: 8px !important;
        width: 100% !important;
        text-align: center !important;
        min-height: 36px !important;
        cursor: pointer !important;
        transition: background 0.1s ease !important;
    }
    div[data-testid="stButton"] > button:hover,
    div[data-testid="stButton"] > button:focus {
        background: #1f2937 !important;
        color: #9ca3af !important;
        border-color: #374151 !important;
    }

    /* ── Tab strip ────────────────────────────────────────────────── */
    .stTabs [data-baseweb="tab-list"] {
        gap: 0;
        background: #000000;
        border-bottom: 1px solid #1f2937;
        margin-bottom: 12px;
    }
    .stTabs [data-baseweb="tab"] {
        font-family: 'Syne', sans-serif;
        font-size: 13px;
        font-weight: 700;
        color: #6b7280;
        background: transparent;
        border-radius: 0;
        padding: 12px 20px;
        min-height: 44px;
        border-bottom: 2px solid transparent;
    }
    .stTabs [aria-selected="true"] {
        color: #f9fafb !important;
        background: transparent !important;
        border-bottom: 2px solid #3b82f6 !important;
    }
    .stTabs [data-baseweb="tab-panel"] { padding: 0; }

    /* ── Plotly charts ────────────────────────────────────────────── */
    .js-plotly-plot .modebar { display: none !important; }
    .stPlotlyChart { border-radius: 8px; overflow: hidden; margin-bottom: 8px; }

    /* ── Sell / confirm / cancel buttons — muted, not alarming ──────── */
    /* Close / Confirm / Cancel — small, muted, right-aligned column */
    div[data-testid="stButton"][data-key*="sell_"] > button {
        background: transparent !important;
        border: 1px solid #374151 !important;
        border-top: 1px solid #374151 !important;
        border-radius: 6px !important;
        color: #9ca3af !important;
        font-size: 11px !important;
        font-weight: 500 !important;
        padding: 3px 8px !important;
        min-height: 28px !important;
        margin-top: 4px !important;
    }
    div[data-testid="stButton"][data-key*="sell_"] > button:hover {
        border-color: #6b7280 !important;
        color: #d1d5db !important;
        background: #111827 !important;
    }
    div[data-testid="stButton"][data-key*="confirm_"] > button {
        background: transparent !important;
        border: 1px solid #374151 !important;
        border-top: 1px solid #374151 !important;
        border-radius: 6px !important;
        color: #d1d5db !important;
        font-size: 11px !important;
        font-weight: 500 !important;
        padding: 3px 8px !important;
        min-height: 28px !important;
        margin-top: 4px !important;
    }
    div[data-testid="stButton"][data-key*="confirm_"] > button:hover {
        border-color: #ef4444 !important;
        color: #ef4444 !important;
    }
    div[data-testid="stButton"][data-key*="cancel_"] > button {
        background: transparent !important;
        border: 1px solid #1f2937 !important;
        border-top: 1px solid #1f2937 !important;
        border-radius: 6px !important;
        color: #6b7280 !important;
        font-size: 11px !important;
        font-weight: 500 !important;
        padding: 3px 8px !important;
        min-height: 28px !important;
        margin-top: 4px !important;
    }

    /* ── Inline refresh row ───────────────────────────────────────── */
    .stSlider > div { padding-top: 0 !important; }
</style>
""", unsafe_allow_html=True)


# ── Helpers ────────────────────────────────────────────────────────────────────
def _secret(key: str, default: str = "") -> str:
    try:
        return st.secrets[key]
    except Exception:
        return os.getenv(key, default)


DATABASE_URL   = _secret("DATABASE_URL")
ALPACA_API_KEY = _secret("ALPACA_API_KEY")
ALPACA_SECRET  = _secret("ALPACA_SECRET_KEY")
ALPACA_PAPER   = _secret("ALPACA_PAPER", "true").lower() == "true"
ALPACA_BASE    = "https://paper-api.alpaca.markets" if ALPACA_PAPER else "https://api.alpaca.markets"


# ── DB connection ──────────────────────────────────────────────────────────────
@st.cache_resource(ttl=60)
def get_conn():
    if not DATABASE_URL:
        return None, None
    try:
        import psycopg2
        from urllib.parse import urlparse, unquote
        url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        parsed = urlparse(url)
        conn = psycopg2.connect(
            host=parsed.hostname,
            port=parsed.port or 5432,
            dbname=parsed.path.lstrip("/"),
            user=parsed.username,
            password=unquote(parsed.password or ""),
            sslmode="require",
            connect_timeout=10,
        )
        return "postgres", conn
    except Exception as e:
        st.error(f"DB: {e}")
        return None, None


def _alpaca_close_position(symbol: str) -> dict:
    """Close a position via Alpaca REST API (no alpaca-py dependency)."""
    import requests as _requests
    headers = {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }
    # Check for open orders first
    r = _requests.get(
        f"{ALPACA_BASE}/v2/orders",
        params={"status": "open", "symbols": symbol},
        headers=headers,
        timeout=8,
    )
    if r.ok and r.json():
        return {"skipped": "already_pending", "orders": r.json()}

    # Close the position (DELETE /v2/positions/{symbol})
    r = _requests.delete(
        f"{ALPACA_BASE}/v2/positions/{symbol}",
        headers=headers,
        timeout=8,
    )
    if r.ok:
        return {"placed": True, "data": r.json()}
    # Surface Alpaca error codes
    try:
        err = r.json()
    except Exception:
        err = {"message": r.text}
    code = err.get("code", r.status_code)
    msg  = err.get("message", "unknown error")
    if code == 40310100:
        return {"pdt_blocked": True, "message": msg}
    raise RuntimeError(f"Alpaca {r.status_code}: {msg}")


def query(sql: str, params=(), silent: bool = False) -> pd.DataFrame:
    backend, conn = get_conn()
    if conn is None:
        return pd.DataFrame()
    try:
        with conn.cursor() as cur:
            cur.execute(sql.replace("?", "%s"), params or None)
            cols = [d[0] for d in cur.description]
            return pd.DataFrame(cur.fetchall(), columns=cols)
    except Exception as e:
        try:
            conn.rollback()  # clear aborted-transaction state so subsequent queries work
        except Exception:
            pass
        if not silent:
            st.error(f"Query: {e}")
        return pd.DataFrame()


# ── Alpaca ─────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=30)
def fetch_account() -> dict:
    if not ALPACA_API_KEY:
        return {}
    try:
        r = requests.get(f"{ALPACA_BASE}/v2/account",
            headers={"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET},
            timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


@st.cache_data(ttl=30)
def fetch_positions() -> list:
    if not ALPACA_API_KEY:
        return []
    try:
        r = requests.get(f"{ALPACA_BASE}/v2/positions",
            headers={"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET},
            timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception:
        return []


_ALPACA_DATA = "https://data.alpaca.markets"
_BENCHMARKS  = {
    "SPY": "S&P 500",
    "QQQ": "NASDAQ",
    "DIA": "Dow Jones",
}

@st.cache_data(ttl=60)
def fetch_benchmark_data() -> dict:
    """
    Returns {symbol: {name, price, change_pct}} for SPY, QQQ, DIA.
    Uses Alpaca snapshot endpoint — dailyBar vs prevDailyBar gives today's % move.
    """
    if not ALPACA_API_KEY:
        return {}
    try:
        r = requests.get(
            f"{_ALPACA_DATA}/v2/stocks/snapshots",
            headers={"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET},
            params={"symbols": ",".join(_BENCHMARKS), "feed": "iex"},
            timeout=6,
        )
        r.raise_for_status()
        data = r.json()
        result = {}
        for sym, label in _BENCHMARKS.items():
            snap = data.get(sym, {})
            daily     = snap.get("dailyBar") or {}
            prev      = snap.get("prevDailyBar") or {}
            price     = float(daily.get("c") or daily.get("o") or 0)
            prev_close = float(prev.get("c", 0))
            change_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0.0
            if price > 0:
                result[sym] = {"name": label, "price": price, "change_pct": change_pct}
        return result
    except Exception:
        return {}



@st.cache_data(ttl=60)
def calc_unsettled_proceeds() -> float:
    """Sum SELL notionals from the last 5 days whose T+2 settlement date is still in the future."""
    today = datetime.now(timezone.utc).date()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    try:
        df = query(
            "SELECT ts, notional FROM executions "
            "WHERE side = 'SELL' AND ts > ? AND notional IS NOT NULL",
            params=(cutoff,),
        )
        if df.empty:
            return 0.0
        total = 0.0
        for _, row in df.iterrows():
            trade_date = pd.to_datetime(row["ts"], utc=True).date()
            if settlement_date(trade_date) > today:
                total += float(row["notional"])
        return total
    except Exception:
        return 0.0


@st.cache_data(ttl=60)
def get_settlement_breakdown() -> list:
    """
    Returns list of {date, date_str, amount} for each future settlement date,
    sorted ascending. Shows the user exactly when each batch of T+2 funds clears.
    """
    from collections import defaultdict
    today = datetime.now(timezone.utc).date()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    try:
        df = query(
            "SELECT ts, notional FROM executions "
            "WHERE side = 'SELL' AND ts > ? AND notional IS NOT NULL",
            params=(cutoff,),
        )
        if df.empty:
            return []
        buckets = defaultdict(float)
        for _, row in df.iterrows():
            trade_date = pd.to_datetime(row["ts"], utc=True).date()
            settle = settlement_date(trade_date)
            if settle > today:
                buckets[settle] += float(row["notional"])
        return [
            {"date": d, "date_str": d.strftime("%a %-d %b"), "amount": v}
            for d, v in sorted(buckets.items())
        ]
    except Exception:
        return []


# ── API cost loaders ───────────────────────────────────────────────────────────
# All cost queries use silent=True so a missing api_usage table (before the first
# agent restart) returns an empty DataFrame without polluting the dashboard with errors.
def load_api_costs_daily() -> pd.DataFrame:
    df = query("""
        SELECT
            DATE(ts AT TIME ZONE 'Europe/Copenhagen') AS day,
            agent,
            SUM(cost_usd) AS cost,
            SUM(input_tokens) AS inp,
            SUM(output_tokens) AS out,
            SUM(cache_creation_tokens) AS cw,
            SUM(cache_read_tokens) AS cr,
            COUNT(*) AS calls
        FROM api_usage
        GROUP BY day, agent
        ORDER BY day DESC
        LIMIT 60
    """, silent=True)
    if not df.empty:
        df["day"] = pd.to_datetime(df["day"])
    return df


def load_api_costs_totals() -> pd.DataFrame:
    return query("""
        SELECT
            agent,
            SUM(cost_usd)                                              AS total_cost,
            SUM(input_tokens + output_tokens
                + cache_creation_tokens + cache_read_tokens)          AS total_tokens,
            COUNT(*)                                                   AS total_calls,
            SUM(input_tokens)                                          AS total_inp,
            SUM(output_tokens)                                         AS total_out,
            SUM(cache_creation_tokens)                                 AS total_cw,
            SUM(cache_read_tokens)                                     AS total_cr
        FROM api_usage
        GROUP BY agent
    """, silent=True)


def load_api_costs_recent(days: int = 7) -> dict:
    """Returns summary dict for the last N days (scalar values for KPIs)."""
    df = query("""
        SELECT
            SUM(cost_usd)              AS cost,
            SUM(input_tokens)          AS inp,
            SUM(cache_creation_tokens) AS cw,
            SUM(cache_read_tokens)     AS cr,
            COUNT(*)                   AS calls
        FROM api_usage
        WHERE ts >= NOW() - INTERVAL '7 days'
    """, silent=True)
    if df.empty:
        return {"cost": 0.0, "inp": 0, "cw": 0, "cr": 0, "calls": 0}
    row = df.iloc[0]
    return {k: (row[k] or 0) for k in ("cost", "inp", "cw", "cr", "calls")}


def load_api_cost_today() -> float:
    df = query("""
        SELECT SUM(cost_usd) AS cost
        FROM api_usage
        WHERE DATE(ts AT TIME ZONE 'Europe/Copenhagen') = CURRENT_DATE
    """, silent=True)
    if df.empty or df.iloc[0]["cost"] is None:
        return 0.0
    return float(df.iloc[0]["cost"])


def load_api_cost_month() -> float:
    df = query("""
        SELECT SUM(cost_usd) AS cost
        FROM api_usage
        WHERE DATE_TRUNC('month', ts) = DATE_TRUNC('month', NOW())
    """, silent=True)
    if df.empty or df.iloc[0]["cost"] is None:
        return 0.0
    return float(df.iloc[0]["cost"])


# ── Position locks ─────────────────────────────────────────────────────────────
@st.cache_data(ttl=5)
def load_locked_symbols() -> set:
    df = query("SELECT symbol FROM symbol_locks", silent=True)
    return set(df["symbol"].tolist()) if not df.empty else set()


@st.cache_resource
def get_lock_store():
    from storage.trade_store import TradeStore
    return TradeStore()


def _toggle_lock(symbol: str, lock: bool) -> None:
    s = get_lock_store()
    if lock:
        s.lock_symbol(symbol)
    else:
        s.unlock_symbol(symbol)
    load_locked_symbols.clear()


# ── Data loaders ───────────────────────────────────────────────────────────────
def load_decisions() -> pd.DataFrame:
    df = query("SELECT * FROM decisions ORDER BY id DESC LIMIT 200")
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_localize(None).dt.tz_localize("Europe/Copenhagen", ambiguous="infer", nonexistent="shift_forward")
    df["confidence_pct"] = (df["confidence"] * 100).round(1)
    return df


def load_executions() -> pd.DataFrame:
    df = query("SELECT * FROM executions ORDER BY id DESC LIMIT 50")
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_localize(None).dt.tz_localize("Europe/Copenhagen", ambiguous="infer", nonexistent="shift_forward")
    return df


def load_research() -> pd.DataFrame:
    df = query("""
        SELECT symbol, sentiment, conviction, recommended_action, summary, key_points, risk_factors, sources_used, ts
        FROM research_signals
        WHERE expires_at > current_timestamp
        AND id IN (SELECT MAX(id) FROM research_signals
                   WHERE expires_at > current_timestamp GROUP BY symbol)
        ORDER BY conviction DESC
    """)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_localize(None).dt.tz_localize("Europe/Copenhagen", ambiguous="infer", nonexistent="shift_forward")
    df["conviction_pct"] = (df["conviction"] * 100).round(0).astype(int)
    return df


def load_scanner() -> pd.DataFrame:
    df = query("""
        SELECT symbol, sentiment, conviction, recommended_action, summary, key_points, risk_factors, sources_used, ts
        FROM research_signals
        WHERE expires_at > current_timestamp
        AND (LOWER(summary) LIKE '%gainer%' OR LOWER(summary) LIKE '%surge%'
             OR LOWER(summary) LIKE '%scanner%' OR LOWER(summary) LIKE '%explosive%'
             OR LOWER(summary) LIKE '%volume%')
        AND id IN (SELECT MAX(id) FROM research_signals
                   WHERE expires_at > current_timestamp GROUP BY symbol)
        ORDER BY conviction DESC LIMIT 8
    """)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_localize(None).dt.tz_localize("Europe/Copenhagen", ambiguous="infer", nonexistent="shift_forward")
    df["conviction_pct"] = (df["conviction"] * 100).round(0).astype(int)
    return df


def load_iv_signals() -> pd.DataFrame:
    df = query("""
        SELECT symbol, sentiment, conviction, recommended_action, summary, key_points, risk_factors, sources_used, ts
        FROM research_signals
        WHERE LOWER(summary) LIKE '%iv spike%'
        AND expires_at > current_timestamp
        AND id IN (SELECT MAX(id) FROM research_signals
                   WHERE expires_at > current_timestamp GROUP BY symbol)
        ORDER BY conviction DESC, ts DESC LIMIT 20
    """)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_localize(None).dt.tz_localize("Europe/Copenhagen", ambiguous="infer", nonexistent="shift_forward")
    df["conviction_pct"] = (df["conviction"] * 100).round(0).astype(int)
    return df


def load_news_signals() -> pd.DataFrame:
    df = query("""
        SELECT symbol, sentiment, conviction, recommended_action, summary, key_points, risk_factors, sources_used, ts
        FROM research_signals
        WHERE signal_type IN ('NEWS', 'BREAKING_NEWS', 'NEWS_SENTIMENT')
        AND expires_at > current_timestamp
        AND id IN (SELECT MAX(id) FROM research_signals
                   WHERE signal_type IN ('NEWS', 'BREAKING_NEWS', 'NEWS_SENTIMENT')
                   AND expires_at > current_timestamp GROUP BY symbol)
        ORDER BY ts DESC LIMIT 10
    """)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_localize(None).dt.tz_localize("Europe/Copenhagen", ambiguous="infer", nonexistent="shift_forward")
    df["conviction_pct"] = (df["conviction"] * 100).round(0).astype(int)
    return df


def load_insider_signals() -> pd.DataFrame:
    df = query("""
        SELECT symbol, sentiment, conviction, recommended_action, summary, key_points, risk_factors, sources_used, ts
        FROM research_signals
        WHERE LOWER(summary) LIKE '%insider%'
        AND expires_at > current_timestamp
        AND id IN (SELECT MAX(id) FROM research_signals
                   WHERE expires_at > current_timestamp GROUP BY symbol)
        ORDER BY conviction DESC, ts DESC LIMIT 20
    """)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_localize(None).dt.tz_localize("Europe/Copenhagen", ambiguous="infer", nonexistent="shift_forward")
    df["conviction_pct"] = (df["conviction"] * 100).round(0).astype(int)
    return df


# ── Badge helpers ──────────────────────────────────────────────────────────────
def action_badge(a):
    cls = {"BUY": "badge-buy", "SELL": "badge-sell", "HOLD": "badge-hold"}.get(str(a), "badge-hold")
    return f'<span class="badge {cls}">{a}</span>'

def urgency_badge(u):
    cls = {"HIGH": "badge-high", "MEDIUM": "badge-medium", "LOW": "badge-low"}.get(str(u).upper(), "badge-low")
    return f'<span class="badge {cls}">{u}</span>'

def approved_badge(v):
    return '<span class="badge badge-yes">YES</span>' if int(v) == 1 else '<span class="badge badge-no">NO</span>'

def sentiment_badge(s):
    cls = {"BULLISH": "badge-bull", "BEARISH": "badge-bear", "NEUTRAL": "badge-neutral"}.get(str(s), "badge-neutral")
    return f'<span class="badge {cls}">{s}</span>'

def action_rec_badge(a):
    cls = {"BUY": "badge-buy", "SELL": "badge-sell", "HOLD": "badge-hold", "WATCH": "badge-watch"}.get(str(a), "badge-hold")
    return f'<span class="badge {cls}">{a}</span>'

def pnl_color(val: float) -> str:
    return "#22c55e" if val >= 0 else "#ef4444"


# ── Plotly chart helpers ───────────────────────────────────────────────────────
_PLOTLY_CFG = {"displayModeBar": False, "staticPlot": False}
_DARK = "#0d1117"
_GRID = "#1f2937"
_FONT = dict(color="#d1d5db", family="Syne")
_MONO = "JetBrains Mono"


def _base_layout(**kwargs) -> dict:
    return dict(
        paper_bgcolor=_DARK, plot_bgcolor=_DARK, font=_FONT,
        showlegend=False, **kwargs
    )


def render_chart(fig, height: int = None):
    """Render a Plotly figure with mobile-safe config. No-op if fig is None."""
    if fig is None:
        return
    if height:
        fig.update_layout(height=height)
    st.plotly_chart(fig, use_container_width=True, config=_PLOTLY_CFG)


def chart_positions_pnl(positions: list):
    """Horizontal grouped bar chart: total unrealised P&L + today's change."""
    if not positions:
        return None
    pairs = sorted(
        [
            (
                float(p.get("unrealized_plpc", 0)) * 100,
                float(p.get("change_today", 0)) * 100,
                p.get("symbol", ""),
            )
            for p in positions
        ]
    )
    total_pcts  = [r[0] for r in pairs]
    daily_pcts  = [r[1] for r in pairs]
    syms        = [r[2] for r in pairs]
    total_colors = ["#22c55e" if v >= 0 else "#ef4444" for v in total_pcts]
    daily_colors = ["#16a34a" if v >= 0 else "#b91c1c" for v in daily_pcts]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        name="Total P&L",
        x=total_pcts, y=syms, orientation="h",
        marker_color=total_colors,
        text=[f"{v:+.2f}%" for v in total_pcts],
        textposition="outside",
        cliponaxis=False,
        textfont=dict(family=_MONO, size=11, color="#f9fafb"),
    ))
    fig.add_trace(go.Bar(
        name="Today",
        x=daily_pcts, y=syms, orientation="h",
        marker_color=daily_colors,
        opacity=0.75,
        text=[f"{v:+.2f}%" for v in daily_pcts],
        textposition="outside",
        cliponaxis=False,
        textfont=dict(family=_MONO, size=11, color="#f9fafb"),
    ))
    fig.update_layout(**_base_layout(
        barmode="group",
        margin=dict(l=4, r=80, t=6, b=6),
        height=max(180, len(positions) * 52),
        xaxis=dict(gridcolor=_GRID, zerolinecolor="#374151",
                   tickfont=dict(family=_MONO, size=10)),
        yaxis=dict(tickfont=dict(family=_MONO, size=12, color="#f9fafb")),
        legend=dict(orientation="h", yanchor="bottom", y=1.01,
                    font=dict(family=_MONO, size=10, color="#9ca3af")),
    ))
    return fig


def chart_decisions_donut(decisions: pd.DataFrame):
    """Donut chart of BUY / SELL / HOLD distribution."""
    if decisions.empty:
        return None
    counts = decisions["action"].value_counts()
    labels = counts.index.tolist()
    colors = [{"BUY": "#22c55e", "SELL": "#ef4444", "HOLD": "#6b7280"}.get(l, "#374151")
              for l in labels]
    fig = go.Figure(go.Pie(
        labels=labels, values=counts.values.tolist(), hole=0.6,
        marker=dict(colors=colors, line=dict(color=_DARK, width=2)),
        textfont=dict(family=_MONO, size=11),
        textinfo="label+percent",
    ))
    fig.update_layout(**_base_layout(margin=dict(l=4, r=4, t=6, b=6), height=220))
    return fig


def chart_research_conviction(research: pd.DataFrame):
    """Vertical bar chart of research conviction scores, colored by sentiment."""
    if research.empty:
        return None
    df = research.sort_values("conviction", ascending=False).head(12)
    colors = [{"BULLISH": "#22c55e", "BEARISH": "#ef4444", "NEUTRAL": "#6b7280"}
              .get(s, "#6b7280") for s in df["sentiment"]]
    fig = go.Figure(go.Bar(
        x=df["symbol"].tolist(),
        y=(df["conviction"] * 100).tolist(),
        marker_color=colors,
        text=[f"{v:.0f}%" for v in (df["conviction"] * 100)],
        textposition="outside",
        textfont=dict(family=_MONO, size=10, color="#9ca3af"),
    ))
    fig.update_layout(**_base_layout(
        margin=dict(l=4, r=4, t=20, b=4),
        height=220,
        xaxis=dict(tickfont=dict(family=_MONO, size=10, color="#f9fafb"), gridcolor=_GRID),
        yaxis=dict(range=[0, 115], tickfont=dict(family=_MONO, size=10), gridcolor=_GRID),
    ))
    return fig


def chart_cumulative_pnl(executions: pd.DataFrame):
    """Cumulative signed notional over time (SELL positive, BUY negative)."""
    if executions.empty:
        return None
    df = executions.sort_values("ts").copy()
    df["signed"] = df.apply(
        lambda r: float(r.get("notional", 0) or 0) * (1 if r["side"] == "SELL" else -1),
        axis=1,
    )
    df["cumulative"] = df["signed"].cumsum()
    last = df["cumulative"].iloc[-1]
    color = "#22c55e" if last >= 0 else "#ef4444"
    fill  = "rgba(34,197,94,0.08)" if last >= 0 else "rgba(239,68,68,0.08)"
    fig = go.Figure(go.Scatter(
        x=df["ts"].tolist(), y=df["cumulative"].tolist(),
        mode="lines", line=dict(color=color, width=2),
        fill="tozeroy", fillcolor=fill,
    ))
    fig.update_layout(**_base_layout(
        margin=dict(l=4, r=4, t=6, b=4),
        height=180,
        xaxis=dict(gridcolor=_GRID, tickfont=dict(family=_MONO, size=9)),
        yaxis=dict(gridcolor=_GRID, tickprefix="$", tickfont=dict(family=_MONO, size=10)),
    ))
    return fig


def chart_confidence_histogram(decisions: pd.DataFrame):
    """Histogram of decision confidence scores (0–100%)."""
    if decisions.empty:
        return None
    fig = go.Figure(go.Histogram(
        x=(decisions["confidence"] * 100).tolist(),
        nbinsx=20,
        marker_color="#3b82f6",
        marker_line=dict(color=_DARK, width=1),
    ))
    fig.update_layout(**_base_layout(
        margin=dict(l=4, r=4, t=6, b=4),
        height=220,
        bargap=0.05,
        xaxis=dict(title=dict(text="Confidence %", font=dict(size=10)), gridcolor=_GRID,
                   tickfont=dict(family=_MONO, size=10)),
        yaxis=dict(gridcolor=_GRID, tickfont=dict(family=_MONO, size=10)),
    ))
    return fig


@st.cache_data(ttl=120)
def fetch_position_sparkline(symbol: str) -> list:
    """Fetch last 35 min of 1-min closes from Alpaca data API."""
    if not ALPACA_API_KEY:
        return []
    try:
        end   = datetime.now(timezone.utc)
        start = end - timedelta(minutes=40)
        r = requests.get(
            f"https://data.alpaca.markets/v2/stocks/{symbol}/bars",
            headers={"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET},
            params={"timeframe": "1Min", "start": start.isoformat(),
                    "end": end.isoformat(), "limit": 35, "feed": "iex"},
            timeout=5,
        )
        r.raise_for_status()
        return [float(b["c"]) for b in r.json().get("bars", [])]
    except Exception:
        return []


def chart_sparkline(prices: list, entry: float):
    """50px transparent sparkline with dotted entry line."""
    if not prices:
        return None
    color = "#22c55e" if prices[-1] >= entry else "#ef4444"
    fig = go.Figure(go.Scatter(
        y=prices, mode="lines", line=dict(color=color, width=1.5),
    ))
    fig.add_hline(y=entry, line_dash="dot", line_color="#6b7280", line_width=1)
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=0, r=0, t=0, b=0), height=50,
        xaxis=dict(visible=False), yaxis=dict(visible=False),
        showlegend=False,
    )
    return fig


# ── Session state for tappable cards ─────────────────────────────────────────
import json as _json

if "selected" not in st.session_state:
    st.session_state.selected = None   # Format: "section:symbol_or_idx"

def _toggle(key: str):
    """Toggle detail panel open/closed."""
    if st.session_state.selected == key:
        st.session_state.selected = None
    else:
        st.session_state.selected = key

def _card_button(key: str, symbol: str, is_open: bool):
    """
    Render a visible expand/collapse button below each card.
    Key must be globally unique (includes section + symbol + row index)
    so Streamlit never confuses two buttons.
    """
    label = f"▲ Collapse {symbol}" if is_open else f"▼ Details"
    if st.button(label, key=f"btn_{key}", use_container_width=True):
        _toggle(key)

def _parse_kp(raw) -> list:
    """Parse key_points/risk_factors JSON string from DB."""
    if not raw or str(raw) in ("None", "nan", ""):
        return []
    try:
        return _json.loads(str(raw))
    except Exception:
        return []

def _detail_panel(row: dict, section: str):
    """Render the expandable detail panel for a research/scanner/decision card."""
    summary     = str(row.get("summary", ""))
    key_points  = _parse_kp(row.get("key_points"))
    risk_factors= _parse_kp(row.get("risk_factors"))
    sources     = row.get("sources_used", "")
    symbol      = str(row.get("symbol", ""))
    rationale   = str(row.get("rationale", ""))
    approval    = str(row.get("approval_reason", ""))
    ts          = row.get("ts", "")
    ts_str      = ts.strftime("%d %b %Y %H:%M") if hasattr(ts, "strftime") else str(ts)

    def _kp_item(p):
        if str(p).startswith("Article: "):
            href = str(p)[len("Article: "):]
            return f'<div class="detail-point"><span class="detail-point-icon">›</span><a href="{href}" target="_blank" rel="noopener" style="color:#f59e0b;">Read article →</a></div>'
        return f'<div class="detail-point"><span class="detail-point-icon">›</span>{p}</div>'

    kp_html = "".join(_kp_item(p) for p in key_points) if key_points else ""

    rf_html = "".join(
        f'<div class="detail-risk"><span class="detail-point-icon">⚠</span>{r}</div>'
        for r in risk_factors
    ) if risk_factors else ""

    # Use rationale for decisions (no summary), summary for research
    body_text = rationale if rationale and rationale not in ("None","nan","") else summary

    html = f'''<div class="detail-panel">'''

    if body_text and body_text not in ("None","nan",""):
        html += f'''
        <div class="detail-title">Analysis</div>
        <div class="detail-body">{body_text}</div>'''

    if kp_html:
        html += f'''
        <div class="detail-title">Key Findings</div>
        {kp_html}'''

    if rf_html:
        html += f'''
        <div class="detail-title">Risk Factors</div>
        {rf_html}'''

    if approval and approval not in ("None","nan",""):
        html += f'''
        <div class="detail-title">Risk Verdict</div>
        <div class="detail-body">{approval}</div>'''

    meta_parts = []
    if ts_str:
        meta_parts.append(ts_str)
    if sources:
        meta_parts.append(f"{sources} sources")
    if meta_parts:
        html += f'''
        <div class="detail-title">Meta</div>
        <div class="detail-body" style="color:#6b7280;">{" · ".join(str(m) for m in meta_parts)}</div>'''

    if symbol:
        html += f'''
        <a class="detail-link" href="https://finance.yahoo.com/quote/{symbol}" target="_blank">
            View {symbol} on Yahoo Finance →
        </a>'''

    html += "</div>"
    st.markdown(html, unsafe_allow_html=True)


# ── Log helpers ───────────────────────────────────────────────────────────────
# Absolute project root — works regardless of the dashboard's working directory
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

_LOG_FILE_MAP = {
    "trading-live":     os.path.join(_PROJECT_ROOT, "logs", "agent.log"),
    "trading-paper":    os.path.join(_PROJECT_ROOT, "logs", "agent.log"),
    "trading-research": os.path.join(_PROJECT_ROOT, "logs", "research.log"),
}


def fetch_logs(service: str, n_lines: int) -> list:
    import subprocess

    # Strategy 1: read log file directly (fastest, no subprocess needed)
    log_path = _LOG_FILE_MAP.get(service)
    if log_path and os.path.exists(log_path):
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            return [l.rstrip() for l in lines[-n_lines:]]
        except Exception:
            pass  # fall through to journalctl

    # Strategy 2: journalctl with full path (PATH may be restricted in dashboard process)
    for jctl in ("/usr/bin/journalctl", "/bin/journalctl"):
        if not os.path.exists(jctl):
            continue
        try:
            result = subprocess.run(
                [jctl, "-u", service, f"-n{n_lines}", "--no-pager", "--output=short-iso"],
                capture_output=True, text=True, timeout=5,
            )
            lines = result.stdout.strip().splitlines()
            return lines if lines else ["(no log output)"]
        except Exception as e:
            return [f"(journalctl error: {e})"]

    return [f"(log unavailable: {log_path!r} not found and journalctl not installed)"]


def _colorize_log_line(line: str) -> str:
    l = line.lower()
    if "[hot path]" in l:
        color, bg = "#fed7aa", "#431407"
    elif "[mini]" in l:
        color, bg = "#bfdbfe", "#1e3a5f"
    elif "[sweep]" in l:
        color, bg = "#bbf7d0", "#14532d"
    elif "buy executed" in l or "] buy " in l:
        color, bg = "#86efac", "#052e16"
    elif "sell executed" in l or "] sell " in l:
        color, bg = "#fdba74", "#431407"
    elif "error" in l or "critical" in l or "exception" in l:
        color, bg = "#fca5a5", "#450a0a"
    elif "warning" in l or "warn" in l:
        color, bg = "#fde68a", "#422006"
    else:
        color, bg = "#9ca3af", "transparent"
    safe = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        f'<div style="font-family:monospace;font-size:11px;color:{color};background:{bg};'
        f'padding:1px 4px;white-space:pre-wrap;word-break:break-all;">{safe}</div>'
    )


# ── Title ──────────────────────────────────────────────────────────────────────
from datetime import timezone, timedelta
import pytz

# Copenhagen time
cph_tz  = pytz.timezone("Europe/Copenhagen")
nyse_tz = pytz.timezone("America/New_York")
now_utc = datetime.now(timezone.utc)
now_cph  = now_utc.astimezone(cph_tz)
now_nyse = now_utc.astimezone(nyse_tz)

cph_str  = now_cph.strftime("%H:%M")
nyse_str = now_nyse.strftime("%H:%M ET")

# NYSE market hours: Mon-Fri 09:30-16:00 ET
def _market_status(dt_nyse) -> tuple:
    """Returns (status_label, color, detail)"""
    wd = dt_nyse.weekday()
    if wd >= 5:
        return "CLOSED", "#ef4444", "Weekend"
    t = dt_nyse.time()
    from datetime import time as _t
    if _t(9, 30) <= t < _t(16, 0):
        # Minutes until close
        close = dt_nyse.replace(hour=16, minute=0, second=0, microsecond=0)
        mins  = int((close - dt_nyse).total_seconds() / 60)
        return "OPEN", "#22c55e", f"Closes in {mins}m"
    elif _t(4, 0) <= t < _t(9, 30):
        open_ = dt_nyse.replace(hour=9, minute=30, second=0, microsecond=0)
        mins  = int((open_ - dt_nyse).total_seconds() / 60)
        return "PRE-MKT", "#f59e0b", f"Opens in {mins}m"
    elif _t(16, 0) <= t < _t(20, 0):
        return "AFTER-HRS", "#f59e0b", "16:00-20:00 ET"
    else:
        return "CLOSED", "#ef4444", "Opens 09:30 ET"

mkt_status, mkt_color, mkt_detail = _market_status(now_nyse)

st.markdown(f"""
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
    <div>
        <div style="font-family:'JetBrains Mono',monospace;font-size:11px;color:#6b7280;
                    text-transform:uppercase;letter-spacing:0.1em;">
            {'📄 Paper' if ALPACA_PAPER else '💰 Live'} Trading
        </div>
        <div style="font-size:22px;font-weight:800;color:#f9fafb;line-height:1.1;">
            Agent Dashboard
        </div>
        <div style="display:flex;align-items:center;gap:8px;margin-top:6px;">
            <span style="background:{mkt_color}22;color:{mkt_color};border:1px solid {mkt_color}44;
                         font-family:'JetBrains Mono',monospace;font-size:10px;font-weight:700;
                         padding:2px 8px;border-radius:4px;">{mkt_status}</span>
            <span style="font-size:11px;color:#6b7280;">{mkt_detail}</span>
        </div>
    </div>
    <div style="text-align:right;">
        <div style="font-family:'JetBrains Mono',monospace;font-size:18px;
                    font-weight:700;color:#f9fafb;">{cph_str}</div>
        <div style="font-family:'JetBrains Mono',monospace;font-size:11px;
                    color:#6b7280;margin-top:2px;">{nyse_str}</div>
        <div style="font-size:10px;color:#4b5563;margin-top:1px;">auto-refreshing</div>
    </div>
</div>
""", unsafe_allow_html=True)


# ── Pre-load all data ─────────────────────────────────────────────────────────
account    = fetch_account()
positions  = fetch_positions()
benchmarks = fetch_benchmark_data()
decisions  = load_decisions()
executions = load_executions()
research   = load_research()
scanner    = load_scanner()
iv_df      = load_iv_signals()
insider_df = load_insider_signals()
news_df    = load_news_signals()

# Pre-compute account values
if account:
    equity       = float(account.get("equity", 0))
    cash         = float(account.get("cash", 0))
    port_val     = float(account.get("portfolio_value", 0))
    last_equity  = float(account.get("last_equity", equity))
    day_pnl      = equity - last_equity
    day_pnl_pct  = (day_pnl / last_equity * 100) if last_equity else 0
    open_exp     = sum(float(p.get("market_value", 0)) for p in positions)
    open_pl      = sum(float(p.get("unrealized_pl", 0)) for p in positions)
    open_pl_pct  = (open_pl / (open_exp - open_pl) * 100) if (open_exp - open_pl) > 0 else 0
    dpnl_cls     = "delta-pos" if day_pnl >= 0 else "delta-neg"
    opnl_cls     = "delta-pos" if open_pl >= 0 else "delta-neg"
    unsettled    = calc_unsettled_proceeds()
    settled      = max(cash - unsettled, 0.0)
    breakdown    = get_settlement_breakdown()

# ── Tabs ──────────────────────────────────────────────────────────────────────
tab_portfolio, tab_signals, tab_costs = st.tabs(["Portfolio", "Signals", "Costs"])


# ════════════════════════════════════════════════════════════════════
# TAB 1 — PORTFOLIO
# ════════════════════════════════════════════════════════════════════
with tab_portfolio:

    # ── Portfolio KPIs ─────────────────────────────────────────────
    st.markdown('<div class="section-header">Portfolio</div>', unsafe_allow_html=True)
    if account:
        st.markdown(f"""
        <div class="metric-grid">
            <div class="metric-card">
                <div class="metric-label">Portfolio</div>
                <div class="metric-value">${port_val:,.2f}</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Day P&L</div>
                <div class="metric-value">${day_pnl:+,.2f}</div>
                <div class="metric-delta {dpnl_cls}">{day_pnl_pct:+.2f}%</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Cash</div>
                <div class="metric-value">${cash:,.2f}</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Settled Cash</div>
                <div class="metric-value">${settled:,.2f}</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Open Exposure</div>
                <div class="metric-value">${open_exp:,.2f}</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Unrealised P&L</div>
                <div class="metric-value">${open_pl:+,.2f}</div>
                <div class="metric-delta {opnl_cls}">{open_pl_pct:+.2f}%</div>
            </div>
        </div>
        """, unsafe_allow_html=True)

    if breakdown:
        lines = " &nbsp;|&nbsp; ".join(
            f"Settles {b['date_str']}: <strong>${b['amount']:,.2f}</strong>"
            for b in breakdown
        )
        st.markdown(
            f'<div style="color:#f59e0b;font-size:0.82rem;margin-top:4px;">'
            f'⏳ T+2 pending <strong>${unsettled:,.2f}</strong> &nbsp;—&nbsp; {lines}'
            f'</div>',
            unsafe_allow_html=True,
        )

    # ── Benchmark comparison ───────────────────────────────────────
    if benchmarks:
        bench_cards = ""
        for sym, b in benchmarks.items():
            chg = b["change_pct"]
            colour = "#4ade80" if chg >= 0 else "#f87171"
            arrow = "▲" if chg >= 0 else "▼"
            bench_cards += f"""
            <div class="metric-card">
                <div class="metric-label">{b['name']}</div>
                <div class="metric-value" style="font-size:22px;color:{colour};">{arrow} {chg:+.2f}%</div>
                <div style="font-size:10px;color:#6b7280;">{sym} ${b['price']:,.2f}</div>
            </div>"""
        # Compare portfolio day % vs benchmarks
        if account:
            port_vs = ""
            for sym, b in benchmarks.items():
                diff = day_pnl_pct - b["change_pct"]
                sign = "+" if diff >= 0 else ""
                port_vs += f"vs {b['name']}: <b style='color:{'#4ade80' if diff>=0 else '#f87171'}'>{sign}{diff:.2f}%</b> &nbsp;"
            st.markdown(
                f'<div class="metric-grid three">{bench_cards}</div>'
                f'<div style="font-size:11px;color:#6b7280;padding:4px 2px;">'
                f'Portfolio day {day_pnl_pct:+.2f}% — {port_vs}</div>',
                unsafe_allow_html=True
            )
        else:
            st.markdown(f'<div class="metric-grid three">{bench_cards}</div>', unsafe_allow_html=True)

    # ── Positions P&L chart ────────────────────────────────────────
    if positions:
        render_chart(chart_positions_pnl(positions))

    # ── Open positions ─────────────────────────────────────────────
    if positions:
        positions_sorted = sorted(positions,
            key=lambda p: float(p.get("unrealized_plpc", 0)), reverse=True)
        locked_symbols = load_locked_symbols()

        # Sell result banner
        if st.session_state.sell_result:
            sym_done, msg = st.session_state.sell_result
            bg = "#052e16" if "placed" in msg.lower() else "#450a0a"
            st.markdown(
                f'<div style="background:{bg};border-radius:6px;padding:8px 12px;'
                f'margin-bottom:8px;font-size:13px;">{msg}</div>',
                unsafe_allow_html=True,
            )
            st.session_state.sell_result = None

        st.markdown('<div class="section-header">Positions</div>', unsafe_allow_html=True)
        for p in positions_sorted:
            sym    = p.get("symbol", "")
            qty    = float(p.get("qty", 0))
            entry  = float(p.get("avg_entry_price", 0))
            curr   = float(p.get("current_price", 0))
            val    = float(p.get("market_value", 0))
            pl     = float(p.get("unrealized_pl", 0))
            pl_pct = float(p.get("unrealized_plpc", 0)) * 100
            col    = pnl_color(pl)
            pl_bar_w = min(abs(pl_pct) * 3, 100)  # visual bar width capped at 100%
            pl_bar_col = "#22c55e22" if pl >= 0 else "#ef444422"

            st.markdown(f"""
            <div style="background:#0d1117;border:1px solid #1f2937;border-radius:12px;
                        padding:14px 16px;margin-bottom:8px;position:relative;overflow:hidden;">
                <!-- P&L color bar along the bottom edge -->
                <div style="position:absolute;bottom:0;left:0;height:2px;
                            width:{pl_bar_w}%;background:{col};opacity:0.6;
                            border-radius:0 0 0 12px;"></div>
                <!-- Top row: symbol + P&L -->
                <div style="display:flex;justify-content:space-between;align-items:flex-start;
                            margin-bottom:6px;">
                    <a href="https://finance.yahoo.com/quote/{sym}" target="_blank"
                       rel="noopener" style="text-decoration:none;">
                        <span style="font-family:'JetBrains Mono',monospace;font-size:17px;
                                     font-weight:700;color:#f9fafb;letter-spacing:0.02em;">{sym}</span>
                    </a>
                    <span style="font-family:'JetBrains Mono',monospace;font-size:17px;
                                 font-weight:700;color:{col};">{pl_pct:+.2f}%</span>
                </div>
                <!-- Bottom row: position detail + dollar P&L + value -->
                <div style="display:flex;justify-content:space-between;align-items:center;">
                    <span style="font-size:11px;color:#9ca3af;">
                        {qty:.4f} sh · ${entry:.2f} → ${curr:.2f}
                    </span>
                    <div style="text-align:right;">
                        <div style="font-family:'JetBrains Mono',monospace;font-size:11px;
                                    color:{col};">${pl:+,.2f}</div>
                        <div style="font-size:10px;color:#6b7280;">${val:,.2f}</div>
                    </div>
                </div>
            </div>""", unsafe_allow_html=True)

            # Sparkline — compact, sits flush below the card
            spark = fetch_position_sparkline(sym)
            if spark:
                render_chart(chart_sparkline(spark, entry))

            # Lock toggle + sell controls
            is_locked = sym in locked_symbols
            _lock_col, _sell_col = st.columns([1, 3])

            lock_label = "🔒" if is_locked else "🔓"
            lock_help = "Locked — agent cannot sell. Tap to unlock." if is_locked else "Tap to lock (prevents agent sells)"
            if _lock_col.button(lock_label, key=f"lock_{sym}", use_container_width=True, help=lock_help):
                _toggle_lock(sym, not is_locked)
                st.rerun()

            if is_locked:
                _sell_col.markdown(
                    '<div style="font-size:11px;color:#6b7280;padding:8px 0;text-align:center;">'
                    'Agent sells blocked</div>',
                    unsafe_allow_html=True,
                )
            elif st.session_state.confirm_sell == sym:
                c1, c2 = _sell_col.columns(2)
                if c1.button("✓ Confirm", key=f"confirm_{sym}", use_container_width=True):
                    try:
                        result = _alpaca_close_position(sym)
                        if result.get("skipped") == "already_pending":
                            st.session_state.sell_result = (sym, f"⚠️ {sym} — order already pending")
                        elif result.get("pdt_blocked"):
                            st.session_state.sell_result = (sym, f"⚠️ {sym} — PDT protection (bought today)")
                        else:
                            st.session_state.sell_result = (sym, f"✓ {sym} close order placed · ${val:,.2f}")
                    except Exception as e:
                        st.session_state.sell_result = (sym, f"Error: {e}")
                    st.session_state.confirm_sell = None
                    st.rerun()
                if c2.button("✕", key=f"cancel_{sym}", use_container_width=True):
                    st.session_state.confirm_sell = None
                    st.rerun()
            else:
                if _sell_col.button("Close", key=f"sell_{sym}", use_container_width=True):
                    st.session_state.confirm_sell = sym
                    st.rerun()

    # ── Cumulative P&L + executions ────────────────────────────────
    st.markdown('<div class="section-header">Trade History</div>', unsafe_allow_html=True)
    if not executions.empty:
        render_chart(chart_cumulative_pnl(executions))
        with st.expander("Execution log"):
            for _, row in executions.head(20).iterrows():
                side_cls = "badge-buy" if row["side"] == "BUY" else "badge-sell"
                ts = row["ts"].strftime("%d/%m %H:%M")
                sl = f'SL ${float(row["stop_loss"]):.2f}' if pd.notna(row.get("stop_loss")) else ""
                tp = f'TP ${float(row["take_profit"]):.2f}' if pd.notna(row.get("take_profit")) else ""
                st.markdown(f"""
                <div class="card">
                    <div class="card-header">
                        <a href="https://finance.yahoo.com/quote/{row["symbol"]}" target="_blank"
                           rel="noopener" style="text-decoration:none;color:inherit;">
                            <span class="card-symbol">{row["symbol"]}</span>
                        </a>
                        <div class="card-badges">
                            <span class="badge {side_cls}">{row["side"]}</span>
                            <span class="badge badge-pct">${float(row.get("notional",0)):,.2f}</span>
                        </div>
                    </div>
                    <div class="card-meta">{ts}{f" · {sl}" if sl else ""}{f" · {tp}" if tp else ""}</div>
                </div>""", unsafe_allow_html=True)
    else:
        st.info("No executions yet.")


# ════════════════════════════════════════════════════════════════════
# TAB 2 — SIGNALS
# ════════════════════════════════════════════════════════════════════
with tab_signals:

    # ── Breaking News Movers ────────────────────────────────────────
    if not news_df.empty:
        st.markdown("""
        <div class="scanner-banner">
            <div class="scanner-title">
                📰 Breaking News Movers
                <span class="scanner-live">LIVE</span>
            </div>
        </div>
        """, unsafe_allow_html=True)

        show_all_news = st.toggle("Show all news hits", value=False, key="news_all")
        news_rows = news_df if show_all_news else news_df.head(5)

        for idx in range(len(news_rows)):
            row = news_rows.iloc[idx]
            sc_cls = {"BULLISH": "badge-bull", "BEARISH": "badge-bear"}.get(str(row["sentiment"]), "badge-neutral")
            border_color = "#22c55e" if row["sentiment"] == "BULLISH" else "#ef4444" if row["sentiment"] == "BEARISH" else "#6b7280"
            is_breaking = "BREAKING" in str(row.get("summary", ""))
            badge_label = "BREAKING" if is_breaking else "NEWS"
            key = f"news:{row['symbol']}:{idx}"
            is_open = st.session_state.selected == key

            # Extract article URL from key_points if present
            kp_list = _parse_kp(row.get("key_points"))
            article_url = next((p[len("Article: "):] for p in kp_list if str(p).startswith("Article: ")), "")
            read_link = f'<a href="{article_url}" target="_blank" rel="noopener" style="color:#f59e0b;font-size:0.75rem;white-space:nowrap;">Read →</a>' if article_url else ""

            # Format timestamp
            try:
                import dateutil.parser as _dp
                _ts = _dp.parse(str(row["ts"])).strftime("%H:%M") if row.get("ts") else ""
            except Exception:
                _ts = ""
            ts_badge = f'<span style="color:#9ca3af;font-size:0.7rem;margin-left:6px;">{_ts}</span>' if _ts else ""

            st.markdown(f"""
            <div class="card" style="margin-bottom:2px;border-left:3px solid {border_color};">
                <div class="card-header">
                    <a href="https://finance.yahoo.com/quote/{row['symbol']}" target="_blank"
                       rel="noopener" style="text-decoration:none;color:inherit;">
                        <span class="card-symbol" style="color:#f59e0b;">{row['symbol']}</span>
                    </a>
                    <div class="card-badges">
                        <span class="badge {sc_cls}">{row['sentiment']}</span>
                        <span class="badge badge-neutral">{badge_label}</span>
                        <span class="badge badge-pct">{row['conviction_pct']}%</span>
                        {ts_badge}
                        {read_link}
                    </div>
                </div>
                <div class="card-text">{str(row['summary'])[:180]}</div>
            </div>""", unsafe_allow_html=True)

            _card_button(key, row['symbol'], is_open)
            if is_open:
                _detail_panel(row.to_dict(), "news")

    # ── Scanner discoveries ─────────────────────────────────────────
    if not scanner.empty:
        st.markdown("""
        <div class="scanner-banner">
            <div class="scanner-title">
                ⚡ Scanner Discoveries
                <span class="scanner-live">LIVE</span>
            </div>
        </div>
        """, unsafe_allow_html=True)

        show_all_scanner = st.toggle("Show all scanner hits", value=False, key="scanner_all")
        scan_rows = scanner if show_all_scanner else scanner.head(4)

        for idx in range(len(scan_rows)):
            row    = scan_rows.iloc[idx]
            sc_cls = {"BULLISH": "badge-bull", "BEARISH": "badge-bear"}.get(row["sentiment"], "badge-neutral")
            ac_cls = {"BUY": "badge-buy", "SELL": "badge-sell"}.get(row["recommended_action"], "badge-hold")
            key     = f"scanner:{row['symbol']}:{idx}"
            is_open = st.session_state.selected == key

            st.markdown(f"""
            <div class="card" style="margin-bottom:2px;">
                <div class="card-header">
                    <a href="https://finance.yahoo.com/quote/{row["symbol"]}" target="_blank"
                       rel="noopener" style="text-decoration:none;color:inherit;">
                        <span class="card-symbol" style="color:#f59e0b;">{row["symbol"]}</span>
                    </a>
                    <div class="card-badges">
                        <span class="badge {sc_cls}">{row["sentiment"]}</span>
                        <span class="badge {ac_cls}">{row["recommended_action"]}</span>
                        <span class="badge badge-pct">{row["conviction_pct"]}%</span>
                    </div>
                </div>
                <div class="scanner-text">{str(row["summary"])[:100]}</div>
            </div>""", unsafe_allow_html=True)

            _card_button(key, row['symbol'], is_open)
            if is_open:
                _detail_panel(row.to_dict(), "scanner")


    # ── Research conviction chart ───────────────────────────────────
    if not research.empty:
        st.markdown('<div class="section-header">Research Conviction</div>', unsafe_allow_html=True)
        render_chart(chart_research_conviction(research))

    # ── Agent Decisions KPIs ────────────────────────────────────────
    st.markdown('<div class="section-header">Agent Decisions</div>', unsafe_allow_html=True)
    if not decisions.empty:
        acted    = decisions[decisions["action"].isin(["BUY","SELL"]) & (decisions["approved"] == 1)]
        blocked  = decisions[decisions["action"].isin(["BUY","SELL"]) & (decisions["approved"] == 0)]
        avg_conf = acted["confidence"].mean() * 100 if not acted.empty else 0
        n_buy    = int((decisions["action"] == "BUY").sum())
        n_sell   = int((decisions["action"] == "SELL").sum())
        n_hold   = int((decisions["action"] == "HOLD").sum())

        st.markdown(f"""
        <div class="stat-row">
            <div class="stat-box">
                <div class="stat-val" style="color:#22c55e;">{n_buy}</div>
                <div class="stat-lbl">BUY signals</div>
            </div>
            <div class="stat-box">
                <div class="stat-val" style="color:#ef4444;">{n_sell}</div>
                <div class="stat-lbl">SELL signals</div>
            </div>
            <div class="stat-box">
                <div class="stat-val" style="color:#9ca3af;">{n_hold}</div>
                <div class="stat-lbl">HOLD signals</div>
            </div>
            <div class="stat-box">
                <div class="stat-val" style="color:#22c55e;">{len(acted)}</div>
                <div class="stat-lbl">Executed</div>
            </div>
            <div class="stat-box">
                <div class="stat-val" style="color:#ef4444;">{len(blocked)}</div>
                <div class="stat-lbl">Blocked</div>
            </div>
            <div class="stat-box">
                <div class="stat-val">{avg_conf:.0f}%</div>
                <div class="stat-lbl">Avg conf.</div>
            </div>
        </div>
        """, unsafe_allow_html=True)

        # Donut + histogram side by side
        _dc1, _dc2 = st.columns(2)
        with _dc1:
            render_chart(chart_decisions_donut(decisions), height=220)
        with _dc2:
            render_chart(chart_confidence_histogram(decisions), height=220)

        with st.expander("Recent Decisions", expanded=False):
            fc1, fc2 = st.columns(2)
            action_f   = fc1.multiselect("Action", ["BUY","SELL","HOLD"],
                                         default=["BUY","SELL"], key="af")
            approved_f = fc2.multiselect("Approved", ["Yes","No"],
                                         default=["Yes","No"], key="apf")
            f = decisions.copy()
            if action_f:
                f = f[f["action"].isin(action_f)]
            if approved_f:
                vals = ([1] if "Yes" in approved_f else []) + ([0] if "No" in approved_f else [])
                f = f[f["approved"].isin(vals)]

            for idx, row in enumerate(f.head(30).itertuples()):
                ab  = action_badge(row.action)
                ub  = urgency_badge(getattr(row, "urgency", "LOW"))
                apb = approved_badge(row.approved)
                ts  = row.ts.strftime("%d/%m %H:%M")
                key = f"decision:{row.symbol}:{idx}"
                is_open = st.session_state.selected == key
                st.markdown(f"""
                <div class="card" style="margin-bottom:2px;">
                    <div class="card-header">
                        <a href="https://finance.yahoo.com/quote/{row.symbol}" target="_blank"
                           rel="noopener" style="text-decoration:none;color:inherit;">
                            <span class="card-symbol">{row.symbol}</span>
                        </a>
                        <div class="card-badges">{ab} {ub} {apb}</div>
                    </div>
                    <div class="card-meta">{ts} · {row.confidence_pct:.0f}% confidence</div>
                    <div class="card-text">{str(getattr(row,"rationale",""))[:160]}</div>
                </div>""", unsafe_allow_html=True)
                _card_button(key, row.symbol, is_open)
                if is_open:
                    row_dict = {c: getattr(row, c, None) for c in f.columns}
                    _detail_panel(row_dict, "decision")

    # ── IV Spike Monitor ────────────────────────────────────────────
    st.markdown('<div class="section-header">IV Spike Monitor</div>', unsafe_allow_html=True)
    if iv_df.empty:
        st.info("No unusual IV spikes.")
    else:
        for _, row in iv_df.iterrows():
            is_unusual   = "UNUSUAL" in str(row.get("summary", "")).upper()
            border_color = "#f59e0b" if is_unusual else "#374151"
            bg_color     = "#1c1404" if is_unusual else "#0d1117"
            label        = "⚠️ UNEXPLAINED" if is_unusual else "📊 EARNINGS IV"
            st.markdown(f"""
            <div class="card" style="border-color:{border_color};background:{bg_color};">
                <div class="card-header">
                    <a href="https://finance.yahoo.com/quote/{row["symbol"]}" target="_blank"
                       rel="noopener" style="text-decoration:none;color:inherit;">
                        <span class="card-symbol" style="color:#f59e0b;">{row["symbol"]}</span>
                    </a>
                    <div class="card-badges">
                        <span class="badge badge-watch">{label}</span>
                        <span class="badge badge-pct">{row["conviction_pct"]}%</span>
                    </div>
                </div>
                <div class="card-text">{str(row["summary"])[:300]}</div>
            </div>""", unsafe_allow_html=True)

    # ── Insider Signals ─────────────────────────────────────────────
    st.markdown('<div class="section-header">Insider Signals</div>', unsafe_allow_html=True)
    if insider_df.empty:
        st.info("No insider signals in the last 14 days.")
    else:
        for _, row in insider_df.iterrows():
            st.markdown(f"""
            <div class="card" style="border-color:#4c1d95;background:#12082e;">
                <div class="card-header">
                    <a href="https://finance.yahoo.com/quote/{row["symbol"]}" target="_blank"
                       rel="noopener" style="text-decoration:none;color:inherit;">
                        <span class="card-symbol" style="color:#a78bfa;">🔒 {row["symbol"]}</span>
                    </a>
                    <div class="card-badges">
                        <span class="badge badge-purple">INSIDER</span>
                        <span class="badge badge-pct">{row["conviction_pct"]}%</span>
                    </div>
                </div>
                <div class="card-text" style="color:#c4b5fd;">{row["summary"][:280]}</div>
            </div>""", unsafe_allow_html=True)

    # ── Research Signals ────────────────────────────────────────────
    st.markdown('<div class="section-header">Research Signals</div>', unsafe_allow_html=True)
    if research.empty:
        st.info("No active research signals.")
    else:
        for idx, row in enumerate(research.itertuples()):
            row = research.iloc[idx]
            sc_cls = {"BULLISH": "badge-bull", "BEARISH": "badge-bear",
                      "NEUTRAL": "badge-neutral"}.get(row["sentiment"], "badge-neutral")
            ac_cls = {"BUY": "badge-buy", "SELL": "badge-sell", "HOLD": "badge-hold",
                      "WATCH": "badge-watch"}.get(row["recommended_action"], "badge-hold")
            key     = f"research:{row['symbol']}:{idx}"
            is_open = st.session_state.selected == key
            try:
                _ts = row["ts"].strftime("%H:%M") if row.get("ts") is not None and pd.notna(row["ts"]) else ""
            except Exception:
                _ts = ""
            ts_badge = f'<span style="color:#9ca3af;font-size:0.7rem;margin-left:6px;">{_ts}</span>' if _ts else ""
            st.markdown(f"""
            <div class="card" style="margin-bottom:2px;">
                <div class="card-header">
                    <a href="https://finance.yahoo.com/quote/{row["symbol"]}" target="_blank"
                       rel="noopener" style="text-decoration:none;color:inherit;">
                        <span class="card-symbol">{row["symbol"]}</span>
                    </a>
                    <div class="card-badges">
                        <span class="badge {sc_cls}">{row["sentiment"]}</span>
                        <span class="badge {ac_cls}">{row["recommended_action"]}</span>
                        <span class="badge badge-pct">{row["conviction_pct"]}%</span>
                        {ts_badge}
                    </div>
                </div>
                <div class="card-text">{str(row["summary"])[:160]}</div>
            </div>""", unsafe_allow_html=True)
            _card_button(key, row['symbol'], is_open)
            if is_open:
                _detail_panel(row.to_dict(), "research")


# ── Refresh control ────────────────────────────────────────────────────────────
_, conn = get_conn()
_rc1, _rc2, _rc3 = st.columns([3, 1, 1])
refresh = _rc1.select_slider(
    "refresh",
    options=[10, 30, 60, 120],
    value=30,
    format_func=lambda x: f"↻ {x}s",
    label_visibility="collapsed",
)
_rc2.markdown(
    f'<div style="font-size:11px;color:#9ca3af;padding-top:10px;text-align:center;">'
    f'{"📄" if ALPACA_PAPER else "💰"} DB {"✅" if conn else "❌"}</div>',
    unsafe_allow_html=True,
)
if _rc3.button("Reconnect", use_container_width=True):
    st.cache_resource.clear()
    st.rerun()


# ── Costs tab ─────────────────────────────────────────────────────────────────
with tab_costs:
    daily_df   = load_api_costs_daily()
    totals_df  = load_api_costs_totals()
    recent     = load_api_costs_recent(7)
    cost_today = load_api_cost_today()
    cost_month = load_api_cost_month()

    total_all = float(totals_df["total_cost"].sum()) if not totals_df.empty else 0.0
    cache_hit_rate = 0.0
    if recent["inp"] + recent["cw"] + recent["cr"] > 0:
        cache_hit_rate = recent["cr"] / (recent["inp"] + recent["cw"] + recent["cr"]) * 100

    st.markdown('<div class="section-header">Claude API Costs</div>', unsafe_allow_html=True)

    # KPI row
    st.markdown(f"""
    <div class="metric-grid four">
        <div class="metric-card">
            <div class="metric-label">Total Spend</div>
            <div class="metric-value">${total_all:.4f}</div>
        </div>
        <div class="metric-card">
            <div class="metric-label">Today</div>
            <div class="metric-value">${cost_today:.4f}</div>
        </div>
        <div class="metric-card">
            <div class="metric-label">This Month</div>
            <div class="metric-value">${cost_month:.4f}</div>
        </div>
        <div class="metric-card">
            <div class="metric-label">Cache Hit Rate (7d)</div>
            <div class="metric-value">{cache_hit_rate:.1f}%</div>
        </div>
    </div>""", unsafe_allow_html=True)

    if daily_df.empty:
        st.info("No API usage data yet — data appears here after the first trading cycle.")
    else:
        # Daily stacked bar chart
        fig_daily = go.Figure()
        colours = {"trading": "#3b82f6", "research": "#22c55e"}
        for agent_name, colour in colours.items():
            sub = daily_df[daily_df["agent"] == agent_name].sort_values("day")
            if not sub.empty:
                fig_daily.add_trace(go.Bar(
                    x=sub["day"], y=sub["cost"],
                    name=agent_name.title(),
                    marker_color=colour,
                    hovertemplate="<b>%{x}</b><br>$%{y:.5f}<extra>" + agent_name.title() + "</extra>",
                ))
        fig_daily.update_layout(
            barmode="stack",
            title="Daily Claude API Spend",
            yaxis=dict(tickprefix="$", tickformat=".5f", title="Cost (USD)"),
            xaxis=dict(title=""),
            height=280,
            template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            margin=dict(l=0, r=0, t=32, b=0),
            legend=dict(orientation="h", yanchor="bottom", y=1.02),
        )
        st.plotly_chart(fig_daily, use_container_width=True, config={"displayModeBar": False})

        # Cache effectiveness
        savings_usd = recent["cr"] * (0.80 - 0.08) / 1_000_000
        daily_rate = cost_today
        monthly_proj = daily_rate * 22  # ~22 trading days
        st.markdown(f"""
        <div class="metric-grid three">
            <div class="metric-card">
                <div class="metric-label">Cache Reads (7d)</div>
                <div class="metric-value">{recent['cr']:,} tok</div>
                <div class="metric-delta" style="color:#6b7280;">10× cheaper than input</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Savings from Cache (7d)</div>
                <div class="metric-value">${savings_usd:.4f}</div>
                <div class="metric-delta" style="color:#6b7280;">vs. full input price</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Projected Monthly</div>
                <div class="metric-value">${monthly_proj:.2f}</div>
                <div class="metric-delta" style="color:#6b7280;">today × 22 trading days</div>
            </div>
        </div>""", unsafe_allow_html=True)

        # Per-agent breakdown table
        if not totals_df.empty:
            st.markdown("**7-day breakdown by agent**")
            week_df = query("""
                SELECT agent,
                    COUNT(*)                   AS calls,
                    SUM(input_tokens)          AS inp_tok,
                    SUM(output_tokens)         AS out_tok,
                    SUM(cache_creation_tokens) AS cache_write_tok,
                    SUM(cache_read_tokens)     AS cache_read_tok,
                    SUM(cost_usd)              AS cost
                FROM api_usage
                WHERE ts >= NOW() - INTERVAL '7 days'
                GROUP BY agent
                ORDER BY cost DESC
            """, silent=True)
            if not week_df.empty:
                week_df["cost"] = week_df["cost"].apply(lambda x: f"${x:.4f}")
                week_df.columns = ["Agent", "Calls", "Input tok", "Output tok", "Cache write tok", "Cache read tok", "Cost"]
                st.dataframe(week_df, use_container_width=True, hide_index=True)

# ── Footer ─────────────────────────────────────────────────────────────────────
st.markdown(f"""
<div style="text-align:center;font-family:'JetBrains Mono',monospace;font-size:10px;
            color:#374151;padding:20px 0 8px 0;">
    Updated {datetime.now().strftime('%H:%M:%S')} · refreshing in {refresh}s
</div>""", unsafe_allow_html=True)

time.sleep(refresh)
st.rerun()
