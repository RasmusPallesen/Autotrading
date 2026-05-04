"""
Trading Agent Dashboard — Streamlit Community Cloud compatible.
Reads from PostgreSQL using Streamlit secrets.
Mobile-first redesign: single-column layout, card-based UI,
touch-friendly tap targets, no horizontal scroll.
"""

import os
import time
from datetime import datetime

import pandas as pd
import requests
import streamlit as st

st.set_page_config(
    page_title="Trading Agent",
    page_icon="📈",
    layout="centered",           # ← was "wide" — centered works on all screen sizes
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
    /* ── Reset & base ─────────────────────────────────────────────── */
    @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Syne:wght@400;600;700;800&display=swap');

    html, body, [class*="css"] {
        font-family: 'Syne', sans-serif;
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

    .metric-card {
        background: #0d1117;
        border: 1px solid #1f2937;
        border-radius: 10px;
        padding: 10px 12px;
    }
    .metric-label {
        font-size: 10px;
        font-weight: 600;
        color: #6b7280;
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
    .badge-hold   { background: #1f2937; color: #9ca3af; border: 1px solid #374151; }
    .badge-yes    { background: #052e16; color: #22c55e; }
    .badge-no     { background: #450a0a; color: #ef4444; }
    .badge-high   { background: #450a0a; color: #ef4444; }
    .badge-medium { background: #451a03; color: #f59e0b; }
    .badge-low    { background: #1f2937; color: #9ca3af; }
    .badge-bull   { background: #052e16; color: #22c55e; }
    .badge-bear   { background: #450a0a; color: #ef4444; }
    .badge-neutral{ background: #1f2937; color: #9ca3af; }
    .badge-watch  { background: #451a03; color: #f59e0b; }
    .badge-pct    { background: #1f2937; color: #e5e7eb; }
    .badge-purple { background: #2e1065; color: #a78bfa; border: 1px solid #4c1d95; }

    .card-meta {
        font-family: 'JetBrains Mono', monospace;
        font-size: 11px;
        color: #4b5563;
        margin-bottom: 6px;
    }
    .card-text {
        font-size: 12px;
        color: #9ca3af;
        line-height: 1.5;
    }
    .card-note {
        font-size: 11px;
        color: #6b7280;
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
    .pos-detail { font-size: 11px; color: #6b7280; margin-top: 2px; }
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
    .scanner-text { font-size: 10px; color: #6b7280; margin-top: 6px; line-height: 1.4; }

    /* ── Section headers ──────────────────────────────────────────── */
    .section-header {
        font-size: 13px;
        font-weight: 800;
        color: #6b7280;
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
    .stat-lbl { font-size: 9px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.05em; }

    /* ── Streamlit overrides ──────────────────────────────────────── */
    div[data-testid="metric-container"] { display: none; }
    div[data-testid="stExpander"] > div { padding: 0; }
    .stSelectbox > div, .stMultiSelect > div { font-size: 13px; }

    /* Scrollable table wrapper */
    .table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; border-radius: 8px; }
    .table-wrap table { width: 100%; border-collapse: collapse; font-size: 12px; }
    .table-wrap th { background: #111827; color: #6b7280; font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em; padding: 8px 10px; text-align: left; font-weight: 600; border-bottom: 1px solid #1f2937; white-space: nowrap; }
    .table-wrap td { padding: 8px 10px; border-bottom: 1px solid #0d1117; color: #e5e7eb; vertical-align: top; }
    .table-wrap tr:last-child td { border-bottom: none; }
    .table-wrap tr:hover td { background: #111827; }

    /* Hide default streamlit elements */
    #MainMenu { visibility: hidden; }
    footer    { visibility: hidden; }
    header    { visibility: hidden; }
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


def query(sql: str, params=()) -> pd.DataFrame:
    backend, conn = get_conn()
    if conn is None:
        return pd.DataFrame()
    try:
        with conn.cursor() as cur:
            cur.execute(sql.replace("?", "%s"), params or None)
            cols = [d[0] for d in cur.description]
            return pd.DataFrame(cur.fetchall(), columns=cols)
    except Exception as e:
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


# ── Data loaders ───────────────────────────────────────────────────────────────
def load_decisions() -> pd.DataFrame:
    df = query("SELECT * FROM decisions ORDER BY id DESC LIMIT 200")
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("Europe/Copenhagen")
    df["confidence_pct"] = (df["confidence"] * 100).round(1)
    return df


def load_executions() -> pd.DataFrame:
    df = query("SELECT * FROM executions ORDER BY id DESC LIMIT 50")
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("Europe/Copenhagen")
    return df


def load_research() -> pd.DataFrame:
    df = query("""
        SELECT symbol, sentiment, conviction, recommended_action, summary, ts
        FROM research_signals
        WHERE expires_at > current_timestamp
        AND id IN (SELECT MAX(id) FROM research_signals
                   WHERE expires_at > current_timestamp GROUP BY symbol)
        ORDER BY conviction DESC
    """)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("Europe/Copenhagen")
    df["conviction_pct"] = (df["conviction"] * 100).round(0).astype(int)
    return df


def load_scanner() -> pd.DataFrame:
    df = query("""
        SELECT symbol, sentiment, conviction, recommended_action, summary, ts
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
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("Europe/Copenhagen")
    df["conviction_pct"] = (df["conviction"] * 100).round(0).astype(int)
    return df


def load_iv_signals() -> pd.DataFrame:
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


def load_insider_signals() -> pd.DataFrame:
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


# ── Sidebar ────────────────────────────────────────────────────────────────────
backend, conn = get_conn()
refresh = st.sidebar.selectbox("Auto-refresh", [10, 30, 60, 120], index=1)
mode_label = "📄 PAPER" if ALPACA_PAPER else "💰 LIVE"
st.sidebar.caption(f"{mode_label} | DB: {'✅' if conn else '❌'}")
if st.sidebar.button("Reconnect DB"):
    st.cache_resource.clear()
    st.rerun()


# ── Title ──────────────────────────────────────────────────────────────────────
now_str = datetime.now().strftime("%H:%M")
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
    </div>
    <div style="text-align:right;">
        <div style="font-family:'JetBrains Mono',monospace;font-size:18px;
                    font-weight:700;color:#f9fafb;">{now_str}</div>
        <div style="font-size:10px;color:#6b7280;">auto-refresh {refresh}s</div>
    </div>
</div>
""", unsafe_allow_html=True)


# ── Scanner discoveries ────────────────────────────────────────────────────────
scanner = load_scanner()
if not scanner.empty:
    items_html = ""
    for _, row in scanner.head(4).iterrows():
        sc_cls = {"BULLISH": "badge-bull", "BEARISH": "badge-bear"}.get(row["sentiment"], "badge-neutral")
        ac_cls = {"BUY": "badge-buy", "SELL": "badge-sell"}.get(row["recommended_action"], "badge-hold")
        items_html += f"""
        <div class="scanner-card">
            <div class="scanner-sym">{row["symbol"]}</div>
            <div class="card-badges" style="margin-top:5px;">
                <span class="badge {sc_cls}">{row["sentiment"]}</span>
                <span class="badge {ac_cls}">{row["recommended_action"]}</span>
                <span class="badge badge-pct">{row["conviction_pct"]}%</span>
            </div>
            <div class="scanner-text">{str(row["summary"])[:120]}</div>
        </div>"""

    st.markdown(f"""
    <div class="scanner-banner">
        <div class="scanner-title">
            ⚡ Scanner Discoveries
            <span class="scanner-live">LIVE</span>
        </div>
        <div class="scanner-grid">{items_html}</div>
    </div>
    """, unsafe_allow_html=True)

    if len(scanner) > 4:
        with st.expander(f"Show all {len(scanner)} scanner hits"):
            for _, row in scanner.iloc[4:].iterrows():
                sc_cls = {"BULLISH": "badge-bull", "BEARISH": "badge-bear"}.get(row["sentiment"], "badge-neutral")
                st.markdown(f"""
                <div class="card" style="margin-bottom:6px;">
                    <div class="card-header">
                        <span class="card-symbol">{row["symbol"]}</span>
                        <div class="card-badges">
                            <span class="badge {sc_cls}">{row["sentiment"]}</span>
                            <span class="badge badge-pct">{row["conviction_pct"]}%</span>
                        </div>
                    </div>
                    <div class="card-text">{str(row["summary"])[:200]}</div>
                </div>""", unsafe_allow_html=True)


# ── Portfolio KPIs ─────────────────────────────────────────────────────────────
st.markdown('<div class="section-header">Portfolio</div>', unsafe_allow_html=True)
account   = fetch_account()
positions = fetch_positions()

if account:
    equity       = float(account.get("equity", 0))
    cash         = float(account.get("cash", 0))
    buying_power = float(account.get("buying_power", 0))
    port_val     = float(account.get("portfolio_value", 0))
    last_equity  = float(account.get("last_equity", equity))
    day_pnl      = equity - last_equity
    day_pnl_pct  = (day_pnl / last_equity * 100) if last_equity else 0
    open_exp     = sum(float(p.get("market_value", 0)) for p in positions)
    open_pl      = sum(float(p.get("unrealized_pl", 0)) for p in positions)
    open_pl_pct  = (open_pl / (open_exp - open_pl) * 100) if (open_exp - open_pl) > 0 else 0

    dpnl_cls = "delta-pos" if day_pnl >= 0 else "delta-neg"
    opnl_cls = "delta-pos" if open_pl >= 0 else "delta-neg"

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
            <div class="metric-label">Buying Power</div>
            <div class="metric-value">${buying_power:,.2f}</div>
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

# ── Open positions ─────────────────────────────────────────────────────────────
if positions:
    positions_sorted = sorted(positions,
        key=lambda p: float(p.get("unrealized_plpc", 0)), reverse=True)

    pos_html = ""
    for p in positions_sorted:
        sym    = p.get("symbol", "")
        qty    = float(p.get("qty", 0))
        entry  = float(p.get("avg_entry_price", 0))
        curr   = float(p.get("current_price", 0))
        val    = float(p.get("market_value", 0))
        pl     = float(p.get("unrealized_pl", 0))
        pl_pct = float(p.get("unrealized_plpc", 0)) * 100
        col    = pnl_color(pl)
        pos_html += f"""
        <div class="pos-row">
            <div>
                <div class="pos-symbol">{sym}</div>
                <div class="pos-detail">{qty:.4f} @ ${entry:.2f} → ${curr:.2f}</div>
            </div>
            <div style="text-align:right;">
                <div class="pos-pnl" style="color:{col};">{pl_pct:+.2f}%</div>
                <div style="font-size:11px;color:{col};">${pl:+.2f}</div>
                <div style="font-size:10px;color:#4b5563;">${val:,.2f}</div>
            </div>
        </div>"""

    st.markdown(f'<div class="card">{pos_html}</div>', unsafe_allow_html=True)


# ── Decision KPIs ──────────────────────────────────────────────────────────────
st.markdown('<div class="section-header">Agent Decisions</div>', unsafe_allow_html=True)
decisions  = load_decisions()
executions = load_executions()

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

# ── Recent decisions (card list, mobile friendly) ─────────────────────────────
if not decisions.empty:
    with st.expander("Recent Decisions", expanded=True):
        fc1, fc2 = st.columns(2)
        action_f = fc1.multiselect("Action", ["BUY","SELL","HOLD"],
                                   default=["BUY","SELL"], key="af")
        approved_f = fc2.multiselect("Approved", ["Yes","No"],
                                     default=["Yes","No"], key="apf")

        f = decisions.copy()
        if action_f:
            f = f[f["action"].isin(action_f)]
        if approved_f:
            vals = ([1] if "Yes" in approved_f else []) + ([0] if "No" in approved_f else [])
            f = f[f["approved"].isin(vals)]

        for _, row in f.head(30).iterrows():
            ab  = action_badge(row["action"])
            ub  = urgency_badge(row.get("urgency", "LOW"))
            apb = approved_badge(row["approved"])
            ts  = row["ts"].strftime("%d/%m %H:%M")
            st.markdown(f"""
            <div class="card">
                <div class="card-header">
                    <span class="card-symbol">{row["symbol"]}</span>
                    <div class="card-badges">{ab} {ub} {apb}</div>
                </div>
                <div class="card-meta">{ts} · {row["confidence_pct"]:.0f}% confidence</div>
                <div class="card-text">{str(row.get("rationale",""))[:200]}</div>
                {"" if not row.get("approval_reason") else f'<div class="card-note">{str(row["approval_reason"])[:150]}</div>'}
            </div>""", unsafe_allow_html=True)

# ── Executions ─────────────────────────────────────────────────────────────────
if not executions.empty:
    with st.expander("Executions"):
        for _, row in executions.head(20).iterrows():
            side_cls = "badge-buy" if row["side"] == "BUY" else "badge-sell"
            ts = row["ts"].strftime("%d/%m %H:%M")
            sl = f'SL ${float(row["stop_loss"]):.2f}' if pd.notna(row.get("stop_loss")) else ""
            tp = f'TP ${float(row["take_profit"]):.2f}' if pd.notna(row.get("take_profit")) else ""
            st.markdown(f"""
            <div class="card">
                <div class="card-header">
                    <span class="card-symbol">{row["symbol"]}</span>
                    <div class="card-badges">
                        <span class="badge {side_cls}">{row["side"]}</span>
                        <span class="badge badge-pct">${float(row.get("notional",0)):,.2f}</span>
                    </div>
                </div>
                <div class="card-meta">{ts} {f'· {sl}' if sl else ''} {f'· {tp}' if tp else ''}</div>
            </div>""", unsafe_allow_html=True)


# ── IV Spike Monitor ──────────────────────────────────────────────────────────
st.markdown('<div class="section-header">IV Spike Monitor</div>', unsafe_allow_html=True)
iv_df = load_iv_signals()
if iv_df.empty:
    st.info("No unusual IV spikes. Monitor runs after market close.")
else:
    for _, row in iv_df.iterrows():
        is_unusual   = "UNUSUAL" in str(row.get("summary", "")).upper()
        border_color = "#f59e0b" if is_unusual else "#374151"
        bg_color     = "#1c1404" if is_unusual else "#0d1117"
        label        = "⚠️ UNEXPLAINED" if is_unusual else "📊 EARNINGS IV"
        st.markdown(f"""
        <div class="card" style="border-color:{border_color};background:{bg_color};">
            <div class="card-header">
                <span class="card-symbol" style="color:#f59e0b;">{row["symbol"]}</span>
                <div class="card-badges">
                    <span class="badge badge-watch">{label}</span>
                    <span class="badge badge-pct">{row["conviction_pct"]}%</span>
                </div>
            </div>
            <div class="card-text">{str(row["summary"])[:300]}</div>
        </div>""", unsafe_allow_html=True)


# ── Insider Signals ────────────────────────────────────────────────────────────
st.markdown('<div class="section-header">Insider Signals</div>', unsafe_allow_html=True)
insider_df = load_insider_signals()
if insider_df.empty:
    st.info("No insider signals in the last 14 days.")
else:
    for _, row in insider_df.iterrows():
        st.markdown(f"""
        <div class="card" style="border-color:#4c1d95;background:#12082e;">
            <div class="card-header">
                <span class="card-symbol" style="color:#a78bfa;">🔒 {row["symbol"]}</span>
                <div class="card-badges">
                    <span class="badge badge-purple">INSIDER</span>
                    <span class="badge badge-pct">{row["conviction_pct"]}%</span>
                </div>
            </div>
            <div class="card-text" style="color:#c4b5fd;">{row["summary"][:280]}</div>
        </div>""", unsafe_allow_html=True)


# ── Research Signals ───────────────────────────────────────────────────────────
st.markdown('<div class="section-header">Research Signals</div>', unsafe_allow_html=True)
research = load_research()
if research.empty:
    st.info("No active research signals.")
else:
    for _, row in research.iterrows():
        sc_cls = {"BULLISH": "badge-bull", "BEARISH": "badge-bear", "NEUTRAL": "badge-neutral"}.get(row["sentiment"], "badge-neutral")
        ac_cls = {"BUY": "badge-buy", "SELL": "badge-sell", "HOLD": "badge-hold", "WATCH": "badge-watch"}.get(row["recommended_action"], "badge-hold")
        st.markdown(f"""
        <div class="card">
            <div class="card-header">
                <span class="card-symbol">{row["symbol"]}</span>
                <div class="card-badges">
                    <span class="badge {sc_cls}">{row["sentiment"]}</span>
                    <span class="badge {ac_cls}">{row["recommended_action"]}</span>
                    <span class="badge badge-pct">{row["conviction_pct"]}%</span>
                </div>
            </div>
            <div class="card-text">{row["summary"][:250]}</div>
        </div>""", unsafe_allow_html=True)


# ── Footer ─────────────────────────────────────────────────────────────────────
st.markdown(f"""
<div style="text-align:center;font-family:'JetBrains Mono',monospace;font-size:10px;
            color:#374151;padding:20px 0 8px 0;">
    Updated {datetime.now().strftime('%H:%M:%S')} · refreshing in {refresh}s
</div>""", unsafe_allow_html=True)

time.sleep(refresh)
st.rerun()
