"""
Agent Controller — Linux/VM version.
Uses systemctl instead of Windows bat files.
Same ntfy command interface as the Windows version.
Replace agent_controller.py with this file on your cloud VM.
"""

import logging
import os
import subprocess
import sys
import time
import json
from datetime import datetime

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

NTFY_BASE     = os.getenv("NTFY_BASE_URL", "https://ntfy.sh")
CMD_TOPIC     = os.getenv("NTFY_CMD_TOPIC", "")
NOTIFY_TOPIC  = os.getenv("NTFY_TOPIC", "")
POLL_INTERVAL = int(os.getenv("CONTROLLER_POLL_SEC", "15"))

_live_confirm_pending_until: float = 0.0
_last_message_id: str = ""

SERVICES = {
    "paper":    "trading-paper",
    "live":     "trading-live",
    "research": "trading-research",
}


def _notify(title: str, message: str, priority: str = "default"):
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
        logger.warning("Notification error: %s", e)


def _poll_commands() -> list:
    global _last_message_id
    if not CMD_TOPIC:
        return []
    params = {"poll": "1", "since": _last_message_id if _last_message_id else "1m"}
    try:
        resp = requests.get(f"{NTFY_BASE}/{CMD_TOPIC}/json", params=params, timeout=10)
        if resp.status_code != 200:
            return []
        messages = []
        for line in resp.text.strip().splitlines():
            if not line:
                continue
            try:
                msg = json.loads(line)
                if msg.get("event") != "message":
                    continue
                _last_message_id = msg.get("id", _last_message_id)
                text = msg.get("message", "").strip()

                # Handle case where iOS Shortcuts sends JSON as the message body
                # e.g. {"topic":"rasmus-cmd-1306","message":"status"}
                if text.startswith("{"):
                    try:
                        inner = json.loads(text)
                        text = inner.get("message", text).strip()
                    except Exception:
                        pass

                if text:
                    messages.append(text)
            except Exception:
                pass
        return messages
    except Exception as e:
        logger.debug("Poll error: %s", e)
        return []


def _systemctl(action: str, service: str) -> bool:
    try:
        result = subprocess.run(
            ["sudo", "systemctl", action, service],
            capture_output=True, text=True, timeout=15,
        )
        return result.returncode == 0
    except Exception as e:
        logger.error("systemctl %s %s error: %s", action, service, e)
        return False


def _is_active(service: str) -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", service],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() == "active"
    except Exception:
        return False


def _get_status() -> str:
    lines = ["Agent Status:"]
    for label, svc in [("Paper", "trading-paper"), ("Live", "trading-live"),
                        ("Research", "trading-research"), ("Controller", "trading-controller")]:
        state = "RUNNING" if _is_active(svc) else "stopped"
        lines.append(f"  {label}: {state}")
    lines.append(f"  Time: {datetime.now().strftime('%H:%M:%S')} (UTC)")
    return "\n".join(lines)


def _handle_command(raw: str):
    global _live_confirm_pending_until
    cmd = raw.strip().lower()
    logger.info("Command: '%s'", raw)

    if cmd == "status":
        status = _get_status()
        _notify("Agent Status", status)

    elif cmd in ("start paper", "start_paper"):
        ok = _systemctl("start", "trading-paper")
        _notify("Paper Agent", "Started." if ok else "Failed to start.")

    elif cmd in ("start research", "start_research"):
        ok = _systemctl("start", "trading-research")
        _notify("Research Agent", "Started." if ok else "Failed to start.")

    elif cmd in ("start live", "start_live"):
        if time.time() < _live_confirm_pending_until:
            _live_confirm_pending_until = 0.0
            ok = _systemctl("start", "trading-live")
            _notify("LIVE Agent", "Started — real money trading active." if ok else "Failed.",
                    priority="high")
            logger.warning("LIVE agent started remotely")
        else:
            _live_confirm_pending_until = time.time() + 60
            _notify("LIVE Start — Confirm", "Send 'start live' again within 60s to confirm.",
                    priority="high")

    elif cmd in ("stop paper", "stop_paper"):
        ok = _systemctl("stop", "trading-paper")
        _notify("Paper Agent", "Stopped." if ok else "Failed to stop.")

    elif cmd in ("stop live", "stop_live"):
        ok = _systemctl("stop", "trading-live")
        _notify("Live Agent", "Stopped." if ok else "Failed to stop.", priority="high" if ok else "default")

    elif cmd in ("stop research", "stop_research"):
        ok = _systemctl("stop", "trading-research")
        _notify("Research Agent", "Stopped." if ok else "Failed to stop.")

    elif cmd in ("stop all", "stop_all"):
        results = []
        for label, svc in [("Paper", "trading-paper"), ("Live", "trading-live"),
                             ("Research", "trading-research")]:
            ok = _systemctl("stop", svc)
            results.append(f"{label}: {'stopped' if ok else 'not running'}")
        _notify("All Agents", "\n".join(results))

    elif cmd in ("restart paper", "restart_paper"):
        ok = _systemctl("restart", "trading-paper")
        _notify("Paper Agent", "Restarted." if ok else "Failed.")

    elif cmd in ("restart research", "restart_research"):
        ok = _systemctl("restart", "trading-research")
        _notify("Research Agent", "Restarted." if ok else "Failed.")

    elif cmd in ("logs paper",):
        try:
            result = subprocess.run(
                ["journalctl", "-u", "trading-paper", "-n", "20", "--no-pager"],
                capture_output=True, text=True, timeout=10,
            )
            _notify("Paper Logs (last 20)", result.stdout[-800:] or "No logs")
        except Exception as e:
            _notify("Logs Error", str(e))

    elif cmd in ("logs research",):
        try:
            result = subprocess.run(
                ["journalctl", "-u", "trading-research", "-n", "20", "--no-pager"],
                capture_output=True, text=True, timeout=10,
            )
            _notify("Research Logs (last 20)", result.stdout[-800:] or "No logs")
        except Exception as e:
            _notify("Logs Error", str(e))

    elif cmd in ("help", "?", "commands"):
        _notify("Available Commands",
            "status\n"
            "start paper / start research / start live\n"
            "stop paper / stop research / stop live / stop all\n"
            "restart paper / restart research\n"
            "logs paper / logs research\n"
            "(Live start requires second confirmation within 60s)")

    else:
        _notify("Unknown Command", f"'{raw}' not recognised. Send 'help' for commands.")


def main():
    os.makedirs(os.path.join(os.getcwd(), "logs"), exist_ok=True)

    if not CMD_TOPIC:
        logger.critical("NTFY_CMD_TOPIC not set in environment")
        sys.exit(1)

    logger.info("Controller starting (Linux/systemctl mode)")
    logger.info("  Command topic: %s/%s", NTFY_BASE, CMD_TOPIC)
    logger.info("  Poll interval: %ds", POLL_INTERVAL)

    _notify("Controller Online",
            "VM controller running. Send 'status' to check agents.", priority="low")

    while True:
        try:
            for msg in _poll_commands():
                _handle_command(msg)
        except KeyboardInterrupt:
            logger.info("Controller stopping")
            _notify("Controller Offline", "VM controller has been stopped.")
            break
        except Exception as e:
            logger.error("Controller error: %s", e)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
