"""
Trading Agent Dashboard — Streamlit Community Cloud compatible.
Reads from PostgreSQL (Railway) using Streamlit secrets.
"""

import os
import time
from datetime import datetime

import pandas as pd
import requests
import streamlit as st

st.set_page_config(
    page_title="Trading Agent",
    page_icon="chart_with_upwards_trend",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
    .block-container { padding-top: 1.5rem; }
    .action-buy   { color: #22c55e; font-weight: 700; }
    .action-sell  { color: #ef4444; font-weight: 700; }
    .action-hold  { color: #6b7280; font-weight: 700; }
    .approved-yes { color: #22c55e; }
    .approved-no  { color: #ef4444; }
    .urgency-high   { color: #ef4444; font-weight: 600; }
    .urgency-medium { color: #f59e0b; font-weight: 600; }
    .urgency-low    { color: #6b7280; }
    div[data-testid="metric-container"] {
        background: #111827;
        border: 1px solid #1f2937;
        border-radius: 8px;
        padding: 0.75rem 1rem;
    }
</style>
""", unsafe_allow_html=True)


# ── Secrets — works on Streamlit Cloud and locally ────────────────────────────
def _secret(key: str, default: str = "") -> str:
    try:
        return st.secrets[key]
    except Exception:
        return os.getenv(key, default)


DATABASE_URL    = _secret("DATABASE_URL")
ALPACA_API_KEY  = _secret("ALPACA_API_KEY")
ALPACA_SECRET   = _secret("ALPACA_SECRET_KEY")
ALPACA_PAPER    = _secret("ALPACA_PAPER", "true").lower() == "true"
ALPACA_BASE     = "https://paper-api.alpaca.markets" if ALPACA_PAPER else "https://api.alpaca.markets"


# ── DB connection ──────────────────────────────────────────────────────────────
@st.cache_resource
@st.cache_resource
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
        st.error(f"DB connection failed: {e}")
        return None, None


def query(sql: str, params=()) -> pd.DataFrame:
    backend, conn = get_conn()
    if conn is None:
        return pd.DataFrame()
    try:
        with conn.cursor() as cur:
            cur.execute(sql.replace("?", "%s"), params or None)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            return pd.DataFrame(rows, columns=cols)
    except Exception as e:
        st.error(f"Query error: {e}")
        return pd.DataFrame()


# ── Alpaca live data ───────────────────────────────────────────────────────────
@st.cache_data(ttl=30)
def fetch_account() -> dict:
    if not ALPACA_API_KEY:
        return {}
    try:
        r = requests.get(
            f"{ALPACA_BASE}/v2/account",
            headers={"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET},
            timeout=5,
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


@st.cache_data(ttl=30)
def fetch_positions() -> list:
    if not ALPACA_API_KEY:
        return []
    try:
        r = requests.get(
            f"{ALPACA_BASE}/v2/positions",
            headers={"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET},
            timeout=5,
        )
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
    df = query("SELECT * FROM executions ORDER BY id DESC LIMIT 100")
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
        ORDER BY conviction DESC LIMIT 12
    """)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("Europe/Copenhagen")
    df["conviction_pct"] = (df["conviction"] * 100).round(0).astype(int)
    return df


def badge(text, css_class):
    return f'<span class="{css_class}">{text}</span>'


def action_badge(a):
    cls = {"BUY": "action-buy", "SELL": "action-sell", "HOLD": "action-hold"}.get(str(a), "")
    return badge(a, cls)


def urgency_badge(u):
    cls = {"HIGH": "urgency-high", "MEDIUM": "urgency-medium", "LOW": "urgency-low"}.get(str(u).upper(), "")
    return badge(u, cls)


def approved_badge(v):
    return '<span class="approved-yes">YES</span>' if int(v) == 1 else '<span class="approved-no">NO</span>'


def sentiment_color(s):
    return {"BULLISH": "#22c55e", "BEARISH": "#ef4444", "NEUTRAL": "#6b7280"}.get(str(s), "#6b7280")


def action_color(a):
    return {"BUY": "#22c55e", "SELL": "#ef4444", "HOLD": "#6b7280", "WATCH": "#f59e0b"}.get(str(a), "#6b7280")


# ── Layout ─────────────────────────────────────────────────────────────────────
backend, conn = get_conn()
refresh = st.sidebar.selectbox("Auto-refresh", [10, 30, 60, 120], index=1)
st.sidebar.caption(f"DB: {'connected' if conn else 'disconnected'}")
st.sidebar.caption(f"Paper: {ALPACA_PAPER}")

st.title("Trading Agent")

# ── Scanner discoveries ────────────────────────────────────────────────────────
scanner = load_scanner()
if not scanner.empty:
    st.markdown("""
    <div style="background:linear-gradient(135deg,#1a1a2e,#16213e);border:1px solid #f59e0b;
                border-radius:12px;padding:16px 20px;margin-bottom:20px;">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px;">
            <h2 style="margin:0;color:#f59e0b;font-size:18px;font-weight:700;">
                Market Scanner Discoveries
            </h2>
            <span style="background:#f59e0b;color:#000;padding:2px 8px;border-radius:12px;
                         font-size:11px;font-weight:700;">LIVE</span>
        </div>
    """, unsafe_allow_html=True)

    cols = st.columns(min(len(scanner), 4))
    for i, (_, row) in enumerate(scanner.head(4).iterrows()):
        sc = sentiment_color(row["sentiment"])
        ac = action_color(row["recommended_action"])
        with cols[i % 4]:
            st.markdown(f"""
            <div style="background:#0d1117;border:1px solid #f59e0b;border-radius:8px;padding:12px;">
                <div style="font-size:20px;font-weight:800;color:#f59e0b;">{row["symbol"]}</div>
                <div style="margin:6px 0;display:flex;gap:6px;flex-wrap:wrap;">
                    <span style="background:{sc};color:white;padding:2px 7px;border-radius:4px;font-size:11px;">{row["sentiment"]}</span>
                    <span style="background:{ac};color:white;padding:2px 7px;border-radius:4px;font-size:11px;">{row["recommended_action"]}</span>
                    <span style="background:#1f2937;color:#9ca3af;padding:2px 7px;border-radius:4px;font-size:11px;">{row["conviction_pct"]}%</span>
                </div>
                <div style="font-size:11px;color:#6b7280;line-height:1.4;">{str(row["summary"])[:150]}</div>
            </div>
            """, unsafe_allow_html=True)

    if len(scanner) > 4:
        with st.expander(f"Show all {len(scanner)} discoveries"):
            for _, row in scanner.iloc[4:].iterrows():
                sc = sentiment_color(row["sentiment"])
                st.markdown(f"""
                <div style="border:1px solid #374151;border-radius:6px;padding:10px;margin:4px 0;background:#0d1117;">
                    <strong style="color:#f59e0b;">{row["symbol"]}</strong>
                    <span style="background:{sc};color:white;padding:2px 6px;border-radius:4px;font-size:11px;margin-left:8px;">{row["sentiment"]}</span>
                    <span style="color:#6b7280;font-size:11px;margin-left:8px;">{row["conviction_pct"]}% conviction</span>
                    <div style="font-size:11px;color:#6b7280;margin-top:4px;">{str(row["summary"])[:200]}</div>
                </div>
                """, unsafe_allow_html=True)

    st.markdown("</div>", unsafe_allow_html=True)
    st.divider()

# ── Portfolio ──────────────────────────────────────────────────────────────────
st.subheader("Portfolio")
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

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Portfolio",     f"${port_val:,.2f}")
    c2.metric("Cash",          f"${cash:,.2f}")
    c3.metric("Buying Power",  f"${buying_power:,.2f}")
    c4.metric("Open Exposure", f"${open_exp:,.2f}")
    c5.metric("Unrealised P&L", f"${open_pl:,.2f}", delta=f"{open_pl_pct:.2f}%")
    c6.metric("Day P&L",        f"${day_pnl:,.2f}", delta=f"{day_pnl_pct:.2f}%")

if positions:
    pos_df = pd.DataFrame([{
        "Symbol":      p.get("symbol"),
        "Qty":         float(p.get("qty", 0)),
        "Entry":       f"${float(p.get('avg_entry_price', 0)):,.2f}",
        "Current":     f"${float(p.get('current_price', 0)):,.2f}",
        "Value":       f"${float(p.get('market_value', 0)):,.2f}",
        "P&L":         f"${float(p.get('unrealized_pl', 0)):,.2f}",
        "P&L %":       f"{float(p.get('unrealized_plpc', 0))*100:.2f}%",
    } for p in positions])
    st.dataframe(pos_df, use_container_width=True, hide_index=True)

st.divider()

# ── Decision KPIs ──────────────────────────────────────────────────────────────
decisions   = load_decisions()
executions  = load_executions()

st.subheader("Agent Decisions (last 200)")
if not decisions.empty:
    acted   = decisions[decisions["action"].isin(["BUY","SELL"]) & (decisions["approved"] == 1)]
    blocked = decisions[decisions["action"].isin(["BUY","SELL"]) & (decisions["approved"] == 0)]
    avg_conf = acted["confidence"].mean() * 100 if not acted.empty else 0

    d1, d2, d3, d4, d5, d6 = st.columns(6)
    d1.metric("BUY signals",     int((decisions["action"]=="BUY").sum()))
    d2.metric("SELL signals",    int((decisions["action"]=="SELL").sum()))
    d3.metric("HOLD signals",    int((decisions["action"]=="HOLD").sum()))
    d4.metric("Trades executed", len(acted))
    d5.metric("Trades blocked",  len(blocked))
    d6.metric("Avg confidence",  f"{avg_conf:.1f}%")

st.divider()

# ── Decisions + Executions ─────────────────────────────────────────────────────
col_left, col_right = st.columns([3, 2])

with col_left:
    st.subheader("Recent Decisions")
    if decisions.empty:
        st.info("No decisions yet.")
    else:
        fc1, fc2, fc3 = st.columns(3)
        action_f   = fc1.multiselect("Action",   ["BUY","SELL","HOLD"], default=["BUY","SELL","HOLD"])
        approved_f = fc2.multiselect("Approved", ["Yes","No"],          default=["Yes","No"])
        symbol_f   = fc3.multiselect("Symbol",   sorted(decisions["symbol"].unique()))

        f = decisions.copy()
        if action_f:
            f = f[f["action"].isin(action_f)]
        if approved_f:
            vals = ([1] if "Yes" in approved_f else []) + ([0] if "No" in approved_f else [])
            f = f[f["approved"].isin(vals)]
        if symbol_f:
            f = f[f["symbol"].isin(symbol_f)]

        disp = f[["ts","symbol","action","confidence_pct","urgency","approved","rationale","approval_reason"]].copy()
        disp.columns = ["Time","Symbol","Action","Conf%","Urgency","Approved","Rationale","Risk Note"]
        disp["Time"]     = disp["Time"].dt.strftime("%d/%m %H:%M")
        disp["Action"]   = disp["Action"].apply(action_badge)
        disp["Urgency"]  = disp["Urgency"].apply(urgency_badge)
        disp["Approved"] = disp["Approved"].apply(approved_badge)
        st.write(disp.to_html(escape=False, index=False), unsafe_allow_html=True)

with col_right:
    st.subheader("Executions")
    if executions.empty:
        st.info("No executions yet.")
    else:
        e = executions[["ts","symbol","side","notional","stop_loss","take_profit"]].copy()
        e.columns = ["Time","Symbol","Side","Notional","Stop Loss","Take Profit"]
        e["Time"]        = e["Time"].dt.strftime("%d/%m %H:%M")
        e["Side"]        = e["Side"].apply(action_badge)
        e["Notional"]    = e["Notional"].apply(lambda x: f"${x:,.2f}" if pd.notna(x) else "-")
        e["Stop Loss"]   = e["Stop Loss"].apply(lambda x: f"${x:.2f}" if pd.notna(x) else "-")
        e["Take Profit"] = e["Take Profit"].apply(lambda x: f"${x:.2f}" if pd.notna(x) else "-")
        st.write(e.to_html(escape=False, index=False), unsafe_allow_html=True)

    st.divider()
    st.subheader("Signal breakdown")
    if not decisions.empty:
        st.bar_chart(decisions["action"].value_counts())
        conf = decisions[decisions["action"].isin(["BUY","SELL"])][["ts","confidence"]].copy()
        if not conf.empty:
            st.subheader("Confidence over time")
            conf = conf.sort_values("ts").set_index("ts")
            conf.index = conf.index.strftime("%d/%m %H:%M")
            st.line_chart(conf)

# ── Research signals ───────────────────────────────────────────────────────────
st.divider()
st.subheader("Active Research Signals")
research = load_research()
if research.empty:
    st.info("No active research signals.")
else:
    for _, row in research.iterrows():
        sc = sentiment_color(row["sentiment"])
        ac = action_color(row["recommended_action"])
        st.markdown(f"""
        <div style="border:1px solid #1f2937;border-radius:8px;padding:12px 16px;margin:8px 0;background:#111827;">
            <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
                <strong style="font-size:16px;">{row["symbol"]}</strong>
                <div>
                    <span style="background:{sc};color:white;padding:3px 8px;border-radius:4px;font-size:12px;margin-right:6px;">{row["sentiment"]}</span>
                    <span style="background:{ac};color:white;padding:3px 8px;border-radius:4px;font-size:12px;margin-right:6px;">{row["recommended_action"]}</span>
                    <span style="background:#374151;color:white;padding:3px 8px;border-radius:4px;font-size:12px;">{row["conviction_pct"]}%</span>
                </div>
            </div>
            <p style="margin:8px 0 0;font-size:13px;color:#9ca3af;">{row["summary"]}</p>
        </div>
        """, unsafe_allow_html=True)

st.caption(f"Last updated: {datetime.now().strftime('%H:%M:%S')} -- refreshing in {refresh}s")
time.sleep(refresh)
st.rerun()
