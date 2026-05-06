"""
Agent Controller — remote start/stop via ntfy.sh.
Run this in a separate terminal window on your Windows machine.
It polls a private ntfy topic for commands and executes them.

Setup:
1. Choose a SEPARATE topic from your notification topic — e.g. "rasmus-cmd-8472"
   (keep this private — anyone who knows it can send commands)
2. Add to your bat files:
      set NTFY_TOPIC=rasmus-trading-8472        (existing — for notifications)
      set NTFY_CMD_TOPIC=rasmus-cmd-8472        (new — for commands)
3. Run: python agent_controller.py
4. From iPhone: open https://ntfy.sh/rasmus-cmd-8472 in Safari
   Tap the publish (pencil) icon and send a command

Available commands (case-insensitive):
    start paper     — starts start_agent_PAPER.bat in new window
    start live      — starts start_agent_LIVE.bat (requires YES confirmation)
    start research  — starts start_research.bat
    stop paper      — kills the paper trading agent process
    stop live       — kills the live trading agent process
    stop research   — kills the research agent process
    stop all        — kills all agent processes
    status          — replies with current agent status via ntfy notification
    restart paper   — stop + start paper agent
    restart research — stop + start research agent

Security note:
    The command topic name is your only security. Make it unguessable.
    Commands are logged with timestamp so you have an audit trail.
    LIVE start requires a second confirmation command within 60 seconds.
"""

import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            os.path.join(os.getcwd(), "logs", "controller.log"),
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("controller")

# ── Config ─────────────────────────────────────────────────────────────────────
NTFY_BASE        = os.getenv("NTFY_BASE_URL", "https://ntfy.sh")
CMD_TOPIC        = os.getenv("NTFY_CMD_TOPIC", "")      # Private command topic
NOTIFY_TOPIC     = os.getenv("NTFY_TOPIC", "")          # Existing notification topic
POLL_INTERVAL    = int(os.getenv("CONTROLLER_POLL_SEC", "15"))  # Seconds between polls

# Paths — adjust if your project is not in current working directory
PROJECT_DIR      = os.getcwd()
BAT_PAPER        = os.path.join(PROJECT_DIR, "start_agent_PAPER.bat")
BAT_LIVE         = os.path.join(PROJECT_DIR, "start_agent_LIVE.bat")
BAT_RESEARCH     = os.path.join(PROJECT_DIR, "start_research.bat")

# Live start safety — requires a second confirm command within this window
LIVE_CONFIRM_TIMEOUT = 60  # seconds


# ── State ──────────────────────────────────────────────────────────────────────
_live_confirm_pending_until: float = 0.0   # epoch time when pending expires
_last_message_id: str = ""                 # ntfy message ID cursor


# ── ntfy helpers ───────────────────────────────────────────────────────────────

def _notify(title: str, message: str, priority: str = "default"):
    """Send a notification back to the user via the notification topic."""
    if not NOTIFY_TOPIC:
        return
    try:
        import unicodedata
        safe_title = "".join(
            c for c in title
            if unicodedata.category(c) not in ("So", "Sm") and ord(c) < 256
        ).strip() or title.encode("ascii", "ignore").decode("ascii")

        requests.post(
            f"{NTFY_BASE}/{NOTIFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": safe_title,
                "Priority": priority,
                "Content-Type": "text/plain; charset=utf-8",
            },
            timeout=5,
        )
    except Exception as e:
        logger.warning("Could not send notification: %s", e)


def _poll_commands() -> list:
    """
    Poll ntfy for new messages on the command topic.
    Uses the since= parameter to only fetch messages newer than last seen.
    Returns list of message strings.
    """
    global _last_message_id

    if not CMD_TOPIC:
        return []

    params = {"poll": "1"}
    if _last_message_id:
        params["since"] = _last_message_id
    else:
        params["since"] = "1m"  # On first poll, only look back 1 minute

    try:
        resp = requests.get(
            f"{NTFY_BASE}/{CMD_TOPIC}/json",
            params=params,
            timeout=10,
        )
        if resp.status_code != 200:
            return []

        messages = []
        for line in resp.text.strip().splitlines():
            if not line:
                continue
            try:
                import json
                msg = json.loads(line)
                if msg.get("event") != "message":
                    continue
                _last_message_id = msg.get("id", _last_message_id)
                text = msg.get("message", "").strip()
                if text:
                    messages.append(text)
            except Exception:
                pass
        return messages

    except Exception as e:
        logger.debug("Poll error: %s", e)
        return []


# ── Process management ─────────────────────────────────────────────────────────

def _find_processes(name_fragment: str) -> list:
    """Find running processes matching a name fragment using tasklist."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"WINDOWTITLE eq {name_fragment}*", "/FO", "CSV"],
            capture_output=True, text=True, timeout=5,
        )
        lines = [l for l in result.stdout.splitlines() if name_fragment.lower() in l.lower()]
        return lines
    except Exception:
        return []


def _kill_process_by_title(window_title_fragment: str) -> bool:
    """Kill a process by its window title using taskkill."""
    try:
        # taskkill by window title
        result = subprocess.run(
            ["taskkill", "/F", "/FI", f"WINDOWTITLE eq *{window_title_fragment}*"],
            capture_output=True, text=True, timeout=10,
        )
        success = "SUCCESS" in result.stdout
        if success:
            logger.info("Killed process with title fragment: %s", window_title_fragment)
        else:
            logger.warning("Could not kill '%s': %s", window_title_fragment, result.stdout.strip())
        return success
    except Exception as e:
        logger.error("Kill error: %s", e)
        return False


def _start_bat(bat_path: str, window_title: str) -> bool:
    """Launch a bat file in a new terminal window."""
    if not os.path.exists(bat_path):
        logger.error("Bat file not found: %s", bat_path)
        return False
    try:
        subprocess.Popen(
            ["cmd", "/c", "start", window_title, "cmd", "/k", bat_path],
            cwd=PROJECT_DIR,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        logger.info("Started: %s", bat_path)
        return True
    except Exception as e:
        logger.error("Could not start %s: %s", bat_path, e)
        return False


def _get_status() -> str:
    """Return a human-readable status of running agents."""
    lines = ["Agent Status:"]

    # Check for agent processes by looking for python processes with known script names
    checks = [
        ("Paper agent",    "PAPER"),
        ("Live agent",     "LIVE"),
        ("Research agent", "RESEARCH"),
    ]

    for label, title_frag in checks:
        procs = _find_processes(title_frag)
        if procs:
            lines.append(f"  {label}: RUNNING")
        else:
            lines.append(f"  {label}: stopped")

    lines.append(f"  Controller: RUNNING (polling every {POLL_INTERVAL}s)")
    lines.append(f"  Time: {datetime.now().strftime('%H:%M:%S')}")
    return "\n".join(lines)


# ── Command handlers ───────────────────────────────────────────────────────────

def _handle_command(raw: str):
    """Parse and execute a command string."""
    global _live_confirm_pending_until

    cmd = raw.strip().lower()
    logger.info("Command received: '%s'", raw)

    # ── STATUS ──────────────────────────────────────────────────────────────
    if cmd == "status":
        status = _get_status()
        _notify("Agent Status", status)
        logger.info("Status sent:\n%s", status)

    # ── START PAPER ─────────────────────────────────────────────────────────
    elif cmd in ("start paper", "start_paper"):
        ok = _start_bat(BAT_PAPER, "PAPER TRADING AGENT")
        if ok:
            _notify("Paper Agent Started", "Paper trading agent is starting up.")
            logger.info("Paper agent started")
        else:
            _notify("Start Failed", f"Could not start paper agent. Check {BAT_PAPER} exists.")

    # ── START RESEARCH ───────────────────────────────────────────────────────
    elif cmd in ("start research", "start_research"):
        ok = _start_bat(BAT_RESEARCH, "RESEARCH AGENT")
        if ok:
            _notify("Research Agent Started", "Research agent is starting up.")
            logger.info("Research agent started")
        else:
            _notify("Start Failed", f"Could not start research agent. Check {BAT_RESEARCH} exists.")

    # ── START LIVE (two-step confirmation) ──────────────────────────────────
    elif cmd in ("start live", "start_live"):
        if time.time() < _live_confirm_pending_until:
            # Second command received within timeout — execute
            _live_confirm_pending_until = 0.0
            ok = _start_bat(BAT_LIVE, "LIVE TRADING AGENT")
            if ok:
                _notify(
                    "LIVE Agent Started",
                    "LIVE trading agent is starting. Real money at risk.",
                    priority="high",
                )
                logger.warning("LIVE agent started by remote command")
            else:
                _notify("Start Failed", f"Could not start live agent. Check {BAT_LIVE} exists.")
        else:
            # First command — require confirmation
            _live_confirm_pending_until = time.time() + LIVE_CONFIRM_TIMEOUT
            _notify(
                "LIVE Start — Confirm Required",
                f"Send 'start live' again within {LIVE_CONFIRM_TIMEOUT}s to confirm. "
                f"This will start the LIVE agent with REAL MONEY.",
                priority="high",
            )
            logger.warning("Live start requested — awaiting confirmation")

    # ── STOP PAPER ──────────────────────────────────────────────────────────
    elif cmd in ("stop paper", "stop_paper"):
        ok = _kill_process_by_title("PAPER")
        msg = "Paper agent stopped." if ok else "Could not stop paper agent — may not be running."
        _notify("Paper Agent", msg)

    # ── STOP LIVE ───────────────────────────────────────────────────────────
    elif cmd in ("stop live", "stop_live"):
        ok = _kill_process_by_title("LIVE")
        msg = "Live agent stopped." if ok else "Could not stop live agent — may not be running."
        _notify("Live Agent", msg, priority="high" if ok else "default")

    # ── STOP RESEARCH ────────────────────────────────────────────────────────
    elif cmd in ("stop research", "stop_research"):
        ok = _kill_process_by_title("RESEARCH")
        msg = "Research agent stopped." if ok else "Could not stop research agent — may not be running."
        _notify("Research Agent", msg)

    # ── STOP ALL ─────────────────────────────────────────────────────────────
    elif cmd in ("stop all", "stop_all"):
        results = []
        for title, label in [("PAPER", "Paper"), ("LIVE", "Live"), ("RESEARCH", "Research")]:
            ok = _kill_process_by_title(title)
            results.append(f"{label}: {'stopped' if ok else 'not running'}")
        _notify("All Agents", "\n".join(results))
        logger.info("Stop all: %s", results)

    # ── RESTART PAPER ────────────────────────────────────────────────────────
    elif cmd in ("restart paper", "restart_paper"):
        _kill_process_by_title("PAPER")
        time.sleep(3)
        ok = _start_bat(BAT_PAPER, "PAPER TRADING AGENT")
        msg = "Paper agent restarted." if ok else "Restart failed."
        _notify("Paper Agent", msg)

    # ── RESTART RESEARCH ─────────────────────────────────────────────────────
    elif cmd in ("restart research", "restart_research"):
        _kill_process_by_title("RESEARCH")
        time.sleep(3)
        ok = _start_bat(BAT_RESEARCH, "RESEARCH AGENT")
        msg = "Research agent restarted." if ok else "Restart failed."
        _notify("Research Agent", msg)

    # ── HELP ─────────────────────────────────────────────────────────────────
    elif cmd in ("help", "?", "commands"):
        _notify(
            "Available Commands",
            "status\n"
            "start paper / start research / start live\n"
            "stop paper / stop research / stop live / stop all\n"
            "restart paper / restart research\n"
            "(Live start requires a second confirmation within 60s)"
        )

    # ── UNKNOWN ──────────────────────────────────────────────────────────────
    else:
        logger.warning("Unknown command: '%s'", raw)
        _notify(
            "Unknown Command",
            f"'{raw}' not recognised. Send 'help' for available commands.",
        )


# ── Main loop ──────────────────────────────────────────────────────────────────

def main():
    os.makedirs(os.path.join(PROJECT_DIR, "logs"), exist_ok=True)

    if not CMD_TOPIC:
        logger.critical(
            "NTFY_CMD_TOPIC environment variable not set.\n"
            "Add to your bat files: set NTFY_CMD_TOPIC=your-private-command-topic\n"
            "Then restart this controller."
        )
        sys.exit(1)

    logger.info("Agent Controller starting")
    logger.info("  Command topic : %s/%s", NTFY_BASE, CMD_TOPIC)
    logger.info("  Notify topic  : %s/%s", NTFY_BASE, NOTIFY_TOPIC or "(not set)")
    logger.info("  Poll interval : %ds", POLL_INTERVAL)
    logger.info("  Project dir   : %s", PROJECT_DIR)
    logger.info("")
    logger.info("Ready. Send commands from iPhone:")
    logger.info("  %s/%s", NTFY_BASE, CMD_TOPIC)
    logger.info("")
    logger.info("Available commands: status | start paper | start research |")
    logger.info("  start live | stop paper | stop live | stop research | stop all |")
    logger.info("  restart paper | restart research | help")

    _notify(
        "Controller Online",
        f"Agent controller is running and listening for commands.\n"
        f"Send 'status' to check agent states.",
        priority="low",
    )

    while True:
        try:
            messages = _poll_commands()
            for msg in messages:
                _handle_command(msg)
        except KeyboardInterrupt:
            logger.info("Controller shutting down.")
            _notify("Controller Offline", "Agent controller has been stopped.")
            break
        except Exception as e:
            logger.error("Controller error: %s", e)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
