"""
Email alert system using Resend API.
Sends high-conviction research reports to rasmus.pallesen@gmail.com.
"""

import json
import logging
import os
from datetime import datetime
from typing import List

import requests

from research.analyst import ResearchReport

logger = logging.getLogger(__name__)

RECIPIENT = "rasmus.pallesen@gmail.com"
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_FROM = "Trading Agent <onboarding@resend.dev>"


def send_alert(reports: List[ResearchReport]):
    """
    Send an email alert for high-conviction research reports via Resend.
    Only sends if at least one report has conviction >= 0.70.
    """
    if not RESEND_API_KEY:
        logger.warning("RESEND_API_KEY not set — skipping email")
        return

    high_conviction = [r for r in reports if r.is_high_conviction()]
    if not high_conviction:
        logger.info("No high-conviction reports — no email sent")
        return

    subject = _build_subject(high_conviction)
    html = _build_html(high_conviction)

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            data=json.dumps({
                "from": RESEND_FROM,
                "to": [RECIPIENT],
                "subject": subject,
                "html": html,
            }),
            timeout=15,
        )
        resp.raise_for_status()
        email_id = resp.json().get("id", "unknown")
        logger.info(
            "Alert email sent to %s via Resend (id=%s) — %d high-conviction reports",
            RECIPIENT, email_id, len(high_conviction),
        )

    except requests.HTTPError as e:
        logger.error("Resend API error %s: %s", resp.status_code, resp.text)
    except Exception as e:
        logger.error("Failed to send email: %s", e)


def _build_subject(reports: List[ResearchReport]) -> str:
    actions = {r.recommended_action for r in reports}
    symbols = ", ".join(r.symbol for r in reports)
    top_action = "BUY" if "BUY" in actions else "SELL" if "SELL" in actions else "WATCH"
    return f"[Trading Agent] {top_action} signal — {symbols} — {datetime.now().strftime('%d %b %H:%M')}"


def _build_html(reports: List[ResearchReport]) -> str:
    reports_html = "".join(r.to_email_html() for r in reports)
    now = datetime.now().strftime("%d %b %Y %H:%M")

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
    <body style="margin:0;padding:0;background:#f3f4f6;font-family:sans-serif;">
        <table width="100%" cellpadding="0" cellspacing="0">
            <tr><td align="center" style="padding:24px 16px;">
                <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">
                    <tr><td style="background:#111827;border-radius:8px 8px 0 0;padding:20px 24px;">
                        <h1 style="margin:0;color:white;font-size:18px;font-weight:600;">
                            Trading Agent — Research Alert
                        </h1>
                        <p style="margin:4px 0 0;color:#9ca3af;font-size:13px;">{now} Copenhagen time</p>
                    </td></tr>
                    <tr><td style="background:white;padding:20px 24px;border-radius:0 0 8px 8px;">
                        <p style="margin:0 0 16px;color:#374151;font-size:14px;">
                            The research agent has identified <strong>{len(reports)} high-conviction signal(s)</strong>
                            requiring your attention.
                        </p>
                        {reports_html}
                        <hr style="border:none;border-top:1px solid #e5e7eb;margin:20px 0;">
                        <p style="margin:0;color:#9ca3af;font-size:11px;">
                            Automated alert from your trading agent. All signals are for informational
                            purposes only. Sources: NewsAPI, SEC EDGAR, Reddit (read-only).
                        </p>
                    </td></tr>
                </table>
            </td></tr>
        </table>
    </body>
    </html>
    """
