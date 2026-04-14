"""
Streamlit dashboard for monitoring the trading agent.
Run with: streamlit run dashboard.py
"""

import sqlite3
import time
from pathlib import Path

import pandas as pd
import streamlit as st

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Trading Agent",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

DB_PATH = Path(__file__).parent / "logs" / "trades.db"

# ── Styling ────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    .block-container { padding-top: 1.5rem; }
    .metric-card {
        background: #0e1117;
        border: 1px solid #1f2937;
        border-radius: 8px;
        padding: 1rem 1.25rem;
    }
    .action-buy  { color: #22c55e; font-weight: 700; }
    .action-sell { color: #ef4444; font-weight: 700; }
    .action-hold { color: #6b7280; font-weight: 700; }
    .approved-yes { color: #22c55e; }
    .approved-no  { color: #ef4444; }
    .urgency-high   { color: #ef4444; font-weight: 600; }
    .urgency-medium { color: #f59e0b; font-weight: 600; }
    .urgency-low    { color: #6b7280; }
    .stDataFrame { font-size: 0.85rem; }
    div[data-testid="metric-container"] {
        background: #111827;
        border: 1px solid #1f2937;
        border-radius: 8px;
        padding: 0.75rem 1rem;
    }
</style>
""", unsafe_allow_html=True)


# ── Data loading ───────────────────────────────────────────────────────────────
@st.cache_resource
def get_conn():
    if not DB_PATH.exists():
        return None
    return sqlite3.connect(str(DB_PATH), check_same_thread=False)


def load_decisions(conn, limit=200) -> pd.DataFrame:
    try:
        df = pd.read_sql(
            "SELECT * FROM decisions ORDER BY id DESC LIMIT ?",
            conn, params=(limit,)
        )
        df["ts"] = pd.to_datetime(df["ts"]).dt.tz_convert("Europe/Copenhagen")
        df["confidence_pct"] = (df["confidence"] * 100).round(1)
        return df
    except Exception:
        return pd.DataFrame()


def load_executions(conn, limit=100) -> pd.DataFrame:
    try:
        df = pd.read_sql(
            "SELECT * FROM executions ORDER BY id DESC LIMIT ?",
            conn, params=(limit,)
        )
        df["ts"] = pd.to_datetime(df["ts"]).dt.tz_convert("Europe/Copenhagen")
        return df
    except Exception:
        return pd.DataFrame()


def compute_stats(decisions: pd.DataFrame, executions: pd.DataFrame) -> dict:
    if decisions.empty:
        return {}

    total = len(decisions)
    buys = (decisions["action"] == "BUY").sum()
    sells = (decisions["action"] == "SELL").sum()
    holds = (decisions["action"] == "HOLD").sum()
    executed = (decisions["approved"] == 1).sum()
    blocked = (decisions["approved"] == 0).sum()
    avg_conf = decisions["confidence"].mean() * 100

    total_deployed = executions[executions["side"] == "BUY"]["notional"].sum() if not executions.empty else 0

    return {
        "total": total,
        "buys": int(buys),
        "sells": int(sells),
        "holds": int(holds),
        "executed": int(executed),
        "blocked": int(blocked),
        "avg_conf": round(avg_conf, 1),
        "total_deployed": round(total_deployed, 2),
    }


def action_badge(action: str) -> str:
    css = {"BUY": "action-buy", "SELL": "action-sell", "HOLD": "action-hold"}.get(action, "")
    return f'<span class="{css}">{action}</span>'


def urgency_badge(urgency: str) -> str:
    css = {"HIGH": "urgency-high", "MEDIUM": "urgency-medium", "LOW": "urgency-low"}.get(str(urgency).upper(), "")
    return f'<span class="{css}">{urgency}</span>'


def approved_badge(approved) -> str:
    if int(approved) == 1:
        return '<span class="approved-yes">YES</span>'
    return '<span class="approved-no">NO</span>'


# ── Main layout ────────────────────────────────────────────────────────────────
conn = get_conn()

st.title("Trading Agent — Live Dashboard")

if conn is None:
    st.warning("No database found at `logs/trades.db`. Start the agent first with `python main.py`.")
    st.stop()

# Auto-refresh
refresh_interval = st.sidebar.selectbox("Auto-refresh", [10, 30, 60, 120], index=1)
st.sidebar.caption(f"Refreshing every {refresh_interval}s")

decisions = load_decisions(conn)
executions = load_executions(conn)
stats = compute_stats(decisions, executions)

# ── KPI row ───────────────────────────────────────────────────────────────────
if stats:
    c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
    c1.metric("Total Decisions", stats["total"])
    c2.metric("BUY signals", stats["buys"])
    c3.metric("SELL signals", stats["sells"])
    c4.metric("HOLD signals", stats["holds"])
    c5.metric("Executed", stats["executed"])
    c6.metric("Avg Confidence", f"{stats['avg_conf']}%")
    c7.metric("Capital Deployed", f"${stats['total_deployed']:,.2f}")

st.divider()

# ── Recent decisions ──────────────────────────────────────────────────────────
col_left, col_right = st.columns([3, 2])

with col_left:
    st.subheader("Recent Decisions")

    # Filters
    fc1, fc2, fc3 = st.columns(3)
    action_filter = fc1.multiselect("Action", ["BUY", "SELL", "HOLD"], default=["BUY", "SELL", "HOLD"])
    approved_filter = fc2.multiselect("Approved", ["Yes", "No"], default=["Yes", "No"])
    symbol_filter = fc3.multiselect("Symbol", sorted(decisions["symbol"].unique()) if not decisions.empty else [])

    filtered = decisions.copy()
    if action_filter:
        filtered = filtered[filtered["action"].isin(action_filter)]
    if approved_filter:
        approved_vals = []
        if "Yes" in approved_filter:
            approved_vals.append(1)
        if "No" in approved_filter:
            approved_vals.append(0)
        filtered = filtered[filtered["approved"].isin(approved_vals)]
    if symbol_filter:
        filtered = filtered[filtered["symbol"].isin(symbol_filter)]

    if filtered.empty:
        st.info("No decisions match your filters.")
    else:
        display = filtered[["ts", "symbol", "action", "confidence_pct", "urgency", "approved", "rationale", "approval_reason"]].copy()
        display.columns = ["Time", "Symbol", "Action", "Conf %", "Urgency", "Approved", "Rationale", "Risk Note"]
        display["Time"] = display["Time"].dt.strftime("%H:%M:%S")
        display["Action"] = display["Action"].apply(action_badge)
        display["Urgency"] = display["Urgency"].apply(urgency_badge)
        display["Approved"] = display["Approved"].apply(approved_badge)

        st.write(
            display.to_html(escape=False, index=False),
            unsafe_allow_html=True,
        )

with col_right:
    st.subheader("Executions")

    if executions.empty:
        st.info("No executions yet.")
    else:
        exec_display = executions[["ts", "symbol", "side", "notional", "stop_loss", "take_profit"]].copy()
        exec_display.columns = ["Time", "Symbol", "Side", "Notional $", "Stop Loss", "Take Profit"]
        exec_display["Time"] = exec_display["Time"].dt.strftime("%H:%M:%S")
        exec_display["Side"] = exec_display["Side"].apply(action_badge)
        exec_display["Notional $"] = exec_display["Notional $"].apply(
            lambda x: f"${x:,.2f}" if pd.notna(x) else "-"
        )
        exec_display["Stop Loss"] = exec_display["Stop Loss"].apply(
            lambda x: f"${x:.2f}" if pd.notna(x) else "-"
        )
        exec_display["Take Profit"] = exec_display["Take Profit"].apply(
            lambda x: f"${x:.2f}" if pd.notna(x) else "-"
        )
        st.write(exec_display.to_html(escape=False, index=False), unsafe_allow_html=True)

    st.divider()
    st.subheader("Decision breakdown")

    if not decisions.empty:
        action_counts = decisions["action"].value_counts()
        st.bar_chart(action_counts)

        st.subheader("Confidence over time")
        conf_data = decisions[decisions["action"].isin(["BUY", "SELL"])][["ts", "confidence"]].copy()
        conf_data = conf_data.sort_values("ts").set_index("ts")
        conf_data.index = conf_data.index.strftime("%H:%M")
        st.line_chart(conf_data)

# ── Auto refresh ──────────────────────────────────────────────────────────────
st.caption(f"Last updated: {pd.Timestamp.now('Europe/Copenhagen').strftime('%H:%M:%S')} Copenhagen time")
time.sleep(refresh_interval)
st.rerun()