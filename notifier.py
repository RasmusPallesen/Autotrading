"""
Push notification layer via ntfy.sh.
Sends instant phone notifications on approved trade executions.

Setup (one-time, ~2 minutes):
1. Install the ntfy app on your iPhone (free, App Store)
2. Choose a unique topic name — e.g. "rasmus-trading-8472" (keep this private)
3. In the ntfy app: tap + → enter your topic name → Subscribe
4. Set env var: NTFY_TOPIC=rasmus-trading-8472
5. Done — notifications will appear instantly on your phone

No account, no sign-up, no API key needed.
The topic name IS your password — keep it unguessable (add random digits).

ntfy.sh is free and open source. Messages are ephemeral — not stored long-term.
"""

import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)

NTFY_BASE_URL = os.getenv("NTFY_BASE_URL", "https://ntfy.sh")
NTFY_TOPIC    = os.getenv("NTFY_TOPIC", "")

# Emoji and priority mapping
_ACTION_EMOJI = {
    "BUY":  "📈",
    "SELL": "📉",
}

_URGENCY_PRIORITY = {
    "HIGH":   "high",     # ntfy priority: bypasses DND on iOS
    "MEDIUM": "default",
    "LOW":    "low",
}

# ntfy priorities map to iOS notification importance
# urgent = critical alert (bypasses silent mode) — reserved for kill switch
# high   = prominent notification
# default = normal
# low    = quiet


def _send(
    title: str,
    message: str,
    priority: str = "default",
    tags: list = None,
    topic: str = None,
) -> bool:
    """
    Send a notification via ntfy.sh.
    Returns True on success, False on failure (never raises).
    """
    target_topic = topic or NTFY_TOPIC
    if not target_topic:
        logger.debug("NTFY_TOPIC not set — skipping notification")
        return False

    url = f"{NTFY_BASE_URL}/{target_topic}"
    # HTTP headers only support latin-1 encoding — strip non-latin chars from
    # the Title header and encode it safely. Emojis go in the message body instead.
    import unicodedata
    safe_title = "".join(
        c for c in title
        if unicodedata.category(c) not in ("So", "Sm") and ord(c) < 256
    ).strip() or title.encode("ascii", "ignore").decode("ascii")

    headers = {
        "Title":    safe_title,
        "Priority": priority,
        "Tags":     ",".join(tags or []),
        "Content-Type": "text/plain; charset=utf-8",
    }

    try:
        resp = requests.post(url, data=message.encode("utf-8"),
                             headers=headers, timeout=5)
        if resp.status_code == 200:
            logger.debug("Notification sent: %s", title)
            return True
        else:
            logger.warning("ntfy.sh returned %d: %s", resp.status_code, resp.text[:200])
            return False
    except requests.RequestException as e:
        logger.warning("Notification failed (ntfy.sh unreachable): %s", e)
        return False


def notify_buy(
    symbol: str,
    notional: float,
    confidence: float,
    urgency: str = "MEDIUM",
    rationale: str = "",
    stop_loss: float = None,
    take_profit: float = None,
    paper: bool = True,
) -> bool:
    """Send notification for a confirmed BUY execution."""
    mode    = "PAPER" if paper else "LIVE 💰"
    emoji   = _ACTION_EMOJI["BUY"]
    priority = _URGENCY_PRIORITY.get(urgency.upper(), "default")

    title = f"{emoji} BUY {symbol} — ${notional:.2f} [{mode}]"

    lines = [
        f"Confidence: {confidence*100:.0f}% | Urgency: {urgency}",
    ]
    if stop_loss:
        lines.append(f"Stop loss: ${stop_loss:.2f}")
    if take_profit:
        lines.append(f"Take profit: ${take_profit:.2f}")
    if rationale:
        lines.append(f"Reason: {rationale[:180]}")

    tags = ["chart_increasing", "white_check_mark"]
    if urgency.upper() == "HIGH":
        tags.append("rotating_light")

    return _send(title, "\n".join(lines), priority=priority, tags=tags)


def notify_sell(
    symbol: str,
    notional: float,
    confidence: float,
    urgency: str = "MEDIUM",
    rationale: str = "",
    pnl: float = None,
    pnl_pct: float = None,
    paper: bool = True,
    is_opportunity_sell: bool = False,
) -> bool:
    """Send notification for a confirmed SELL execution."""
    mode  = "PAPER" if paper else "LIVE 💰"
    emoji = _ACTION_EMOJI["SELL"]
    priority = _URGENCY_PRIORITY.get(urgency.upper(), "default")

    sell_type = "OPP. SELL" if is_opportunity_sell else "SELL"
    title = f"{emoji} {sell_type} {symbol} — ${notional:.2f} [{mode}]"

    lines = [f"Confidence: {confidence*100:.0f}% | Urgency: {urgency}"]
    if pnl is not None and pnl_pct is not None:
        pnl_str = f"+${pnl:.2f} (+{pnl_pct:.1f}%)" if pnl >= 0 else f"-${abs(pnl):.2f} ({pnl_pct:.1f}%)"
        lines.append(f"P&L: {pnl_str}")
    if rationale:
        lines.append(f"Reason: {rationale[:180]}")

    tags = ["chart_decreasing", "white_check_mark"]
    if is_opportunity_sell:
        tags.append("arrows_counterclockwise")

    return _send(title, "\n".join(lines), priority=priority, tags=tags)


def notify_kill_switch(reason: str, equity: float, drawdown_pct: float) -> bool:
    """Send urgent notification when daily drawdown kill switch fires."""
    title = "🚨 KILL SWITCH ACTIVATED"
    message = (
        f"Agent halted — daily drawdown limit hit.\n"
        f"Drawdown: {drawdown_pct:.2f}%\n"
        f"Current equity: ${equity:,.2f}\n"
        f"Reason: {reason}"
    )
    return _send(title, message, priority="urgent",
                 tags=["rotating_light", "skull", "no_entry"])


def notify_pdt_block(symbol: str, paper: bool = True) -> bool:
    """Send notification when a trade is blocked by PDT protection."""
    mode  = "PAPER" if paper else "LIVE"
    title = f"⛔ PDT BLOCK — {symbol} [{mode}]"
    message = (
        "Pattern Day Trader protection triggered.\n"
        "Position cannot be closed today.\n"
        "Stop-loss bracket at Alpaca remains active."
    )
    return _send(title, message, priority="high", tags=["no_entry", "rotating_light"])


def notify_startup(paper: bool = True, symbols: int = 0) -> bool:
    """Send notification when agent starts up."""
    mode  = "📄 Paper" if paper else "💰 LIVE"
    title = f"🤖 Agent Started — {mode}"
    message = f"Trading agent is online.\nWatching {symbols} symbols."
    return _send(title, message, priority="low", tags=["robot", "green_circle"])


def notify_shutdown(paper: bool = True) -> bool:
    """Send notification when agent shuts down."""
    mode  = "Paper" if paper else "LIVE"
    title = f"🔴 Agent Stopped — {mode}"
    message = "Trading agent has shut down cleanly."
    return _send(title, message, priority="low", tags=["red_circle"])


# ── Self-test ──────────────────────────────────────────────────────────────────
# Run directly to verify your setup:
#   python notifier.py
# You should see a test notification appear on your phone within seconds.

if __name__ == "__main__":
    import sys
    topic = os.getenv("NTFY_TOPIC", "")
    if not topic:
        print("ERROR: NTFY_TOPIC environment variable is not set.")
        print("Set it in your terminal:")
        print("  Windows:  set NTFY_TOPIC=your-topic-name")
        print("  Mac/Linux: export NTFY_TOPIC=your-topic-name")
        sys.exit(1)

    print(f"Sending test notification to topic: {topic}")
    ok = _send(
        title="🤖 Trading Agent — Test",
        message="Notifications are working correctly.\nYou will receive alerts for trades, kill switch, and startup/shutdown.",
        priority="default",
        tags=["white_check_mark", "robot"],
    )
    if ok:
        print("✅ Notification sent successfully — check your phone.")
    else:
        print("❌ Notification failed. Check:")
        print("  1. Is NTFY_TOPIC set correctly?")
        print("  2. Are you subscribed to the topic in the ntfy app?")
        print("  3. Does your machine have internet access?")
        sys.exit(1)
