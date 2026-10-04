"""
app/services/email_service.py

Transactional email via Resend's HTTPS API.

Why Resend and not SMTP: Render's free web services block outbound SMTP
ports, so smtplib silently hangs there. An HTTPS call always works.

Env vars:
  RESEND_API_KEY      required to actually send (without it, send_email()
                      returns (False, "email_not_configured") and nothing breaks)
  INVITE_FROM_EMAIL   e.g. "Itica <invites@iticacompliance.com>" — the domain
                      must be verified in Resend. Falls back to Resend's shared
                      test sender, which can only deliver to your own address.
"""
from __future__ import annotations

import logging
import os
from typing import Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"
_FALLBACK_FROM = "Itica <onboarding@resend.dev>"


async def send_email(
    to: str,
    subject: str,
    html: str,
    text: str,
    reply_to: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """Returns (sent, error_code). Never raises."""
    key = os.environ.get("RESEND_API_KEY", "").strip()
    if not key:
        return False, "email_not_configured"

    body = {
        "from": os.environ.get("INVITE_FROM_EMAIL", "").strip() or _FALLBACK_FROM,
        "to": [to],
        "subject": subject,
        "html": html,
        "text": text,
    }
    if reply_to:
        body["reply_to"] = reply_to

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                RESEND_URL,
                json=body,
                headers={"Authorization": f"Bearer {key}"},
            )
    except Exception as e:  # network error, timeout, DNS, ...
        logger.warning(f"Resend request failed: {e!r}")
        return False, "email_send_failed"

    if resp.status_code in (200, 201, 202):
        return True, None

    logger.warning(f"Resend rejected email to {to}: HTTP {resp.status_code} {resp.text[:300]}")
    return False, f"email_rejected_{resp.status_code}"
