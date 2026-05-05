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
        color: #6b7280;
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
    .detail-point-icon { flex-shrink: 0; color: #4b5563; }
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
    /* Hide the checkbox widget — card ▲▼ icon is the visual affordance.
       The checkbox still works for click/tap detection via on_change callback. */
    div[data-testid="stCheckbox"] {
        height: 0;
        overflow: hidden;
        margin: -6px 0 2px 0;
        opacity: 0;
        pointer-events: none;
        position: relative;
        z-index: 10;
    }
    /* Re-enable pointer events on the actual input for tap detection */
    div[data-testid="stCheckbox"] input {
        pointer-events: all;
        opacity: 0;
        width: 100%;
        height: 44px;  /* iOS minimum tap target */
        position: absolute;
        top: -40px;
        left: 0;
        cursor: pointer;
    }
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


# ── Session state for tappable cards ─────────────────────────────────────────
import json as _json

if "selected" not in st.session_state:
    st.session_state.selected = None   # Format: "section:symbol_or_idx"

def _toggle(key: str):
    """Toggle detail panel — used as on_change callback for checkboxes."""
    # Checkbox value is read from session_state directly via the widget key
    cb_key = f"cb_{key}"
    if st.session_state.get(cb_key, False):
        st.session_state.selected = key
    else:
        st.session_state.selected = None

def _card_button(key: str, symbol: str):
    """
    Render an invisible checkbox that acts as the tap target.
    Using checkbox instead of button avoids Streamlit's button-bleeding bug
    where clicks on any card trigger the first card's state.
    The checkbox is hidden via CSS and the ▲▼ icon in the card is the visual cue.
    """
    cb_key = f"cb_{key}"
    # Sync checkbox value with selected state
    current_val = st.session_state.selected == key
    st.checkbox(
        label=f"Details for {symbol}",
        value=current_val,
        key=cb_key,
        on_change=_toggle,
        args=(key,),
        label_visibility="collapsed",
    )

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

    kp_html = "".join(
        f'<div class="detail-point"><span class="detail-point-icon">›</span>{p}</div>'
        for p in key_points
    ) if key_points else ""

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


# ── Sidebar ────────────────────────────────────────────────────────────────────
backend, conn = get_conn()
refresh = st.sidebar.selectbox("Auto-refresh", [10, 30, 60, 120], index=1)
mode_label = "📄 PAPER" if ALPACA_PAPER else "💰 LIVE"
st.sidebar.caption(f"{mode_label} | DB: {'✅' if conn else '❌'}")
if st.sidebar.button("Reconnect DB"):
    st.cache_resource.clear()
    st.rerun()


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
        <div style="font-size:10px;color:#4b5563;margin-top:1px;">auto-refresh {refresh}s</div>
    </div>
</div>
""", unsafe_allow_html=True)


# ── Scanner discoveries ────────────────────────────────────────────────────────
scanner = load_scanner()
if not scanner.empty:
    items_html = ""
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

    for idx, row in scan_rows.iterrows():
        sc_cls = {"BULLISH": "badge-bull", "BEARISH": "badge-bear"}.get(row["sentiment"], "badge-neutral")
        ac_cls = {"BUY": "badge-buy", "SELL": "badge-sell"}.get(row["recommended_action"], "badge-hold")
        key    = f"scanner:{row['symbol']}"
        is_open = st.session_state.selected == key
        expand_icon = "▲" if is_open else "▼"

        st.markdown(f"""
        <div class="card" style="margin-bottom:2px;">
            <div class="card-header">
                <span class="card-symbol" style="color:#f59e0b;">{row["symbol"]}</span>
                <div class="card-badges">
                    <span class="badge {sc_cls}">{row["sentiment"]}</span>
                    <span class="badge {ac_cls}">{row["recommended_action"]}</span>
                    <span class="badge badge-pct">{row["conviction_pct"]}%</span>
                    <span style="font-size:10px;color:#4b5563;padding-left:4px;">{expand_icon}</span>
                </div>
            </div>
            <div class="scanner-text">{str(row["summary"])[:100]}</div>
        </div>""", unsafe_allow_html=True)

        _card_button(key, row['symbol'])
        if is_open:
            _detail_panel(row.to_dict(), "scanner")


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

        for idx, row in enumerate(f.head(30).itertuples()):
            ab  = action_badge(row.action)
            ub  = urgency_badge(getattr(row, "urgency", "LOW"))
            apb = approved_badge(row.approved)
            ts  = row.ts.strftime("%d/%m %H:%M")
            key = f"decision:{row.symbol}:{idx}"
            is_open = st.session_state.selected == key
            expand_icon = "▲" if is_open else "▼"

            st.markdown(f"""
            <div class="card" style="margin-bottom:2px;">
                <div class="card-header">
                    <span class="card-symbol">{row.symbol}</span>
                    <div class="card-badges">{ab} {ub} {apb}
                        <span style="font-size:10px;color:#4b5563;padding-left:4px;">{expand_icon}</span>
                    </div>
                </div>
                <div class="card-meta">{ts} · {row.confidence_pct:.0f}% confidence</div>
                <div class="card-text">{str(getattr(row,"rationale",""))[:160]}</div>
            </div>""", unsafe_allow_html=True)

            _card_button(key, row.symbol)
            if is_open:
                row_dict = {c: getattr(row, c, None) for c in f.columns}
                _detail_panel(row_dict, "decision")

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
    for idx, row in research.iterrows():
        sc_cls = {"BULLISH": "badge-bull", "BEARISH": "badge-bear", "NEUTRAL": "badge-neutral"}.get(row["sentiment"], "badge-neutral")
        ac_cls = {"BUY": "badge-buy", "SELL": "badge-sell", "HOLD": "badge-hold", "WATCH": "badge-watch"}.get(row["recommended_action"], "badge-hold")
        key    = f"research:{row['symbol']}"
        is_open = st.session_state.selected == key
        expand_icon = "▲" if is_open else "▼"

        st.markdown(f"""
        <div class="card" style="margin-bottom:2px;">
            <div class="card-header">
                <span class="card-symbol">{row["symbol"]}</span>
                <div class="card-badges">
                    <span class="badge {sc_cls}">{row["sentiment"]}</span>
                    <span class="badge {ac_cls}">{row["recommended_action"]}</span>
                    <span class="badge badge-pct">{row["conviction_pct"]}%</span>
                    <span style="font-size:10px;color:#4b5563;padding-left:4px;">{expand_icon}</span>
                </div>
            </div>
            <div class="card-text">{str(row["summary"])[:160]}</div>
        </div>""", unsafe_allow_html=True)

        _card_button(key, row['symbol'])
        if is_open:
            _detail_panel(row.to_dict(), "research")


# ── Footer ─────────────────────────────────────────────────────────────────────
st.markdown(f"""
<div style="text-align:center;font-family:'JetBrains Mono',monospace;font-size:10px;
            color:#374151;padding:20px 0 8px 0;">
    Updated {datetime.now().strftime('%H:%M:%S')} · refreshing in {refresh}s
</div>""", unsafe_allow_html=True)

time.sleep(refresh)
st.rerun()
