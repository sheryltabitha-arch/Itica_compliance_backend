"""
app/services/sanctions.py
Stub sanctions screening — replace URL with real OFAC/UN API when ready.
"""
from __future__ import annotations
import logging
import os
import requests

logger = logging.getLogger(__name__)

SANCTIONS_API_URL = os.environ.get("SANCTIONS_API_URL", "")
SANCTIONS_API_KEY = os.environ.get("SANCTIONS_API_KEY", "")


def screen_entity(name: str, dob: str = "", nationality: str = "") -> dict:
    """
    Screen an extracted entity against sanctions lists.
    Returns match result with risk level.
    """
    if not SANCTIONS_API_URL:
        # Stub — log and return clear until real API is wired
        logger.warning("SANCTIONS_API_URL not set — sanctions screening skipped")
        return {"screened": False, "match": False, "risk": "unknown", "detail": "Screening not configured"}

    try:
        resp = requests.post(
            SANCTIONS_API_URL,
            headers={"Authorization": f"Bearer {SANCTIONS_API_KEY}"},
            json={"name": name, "dob": dob, "nationality": nationality},
            timeout=10,
        )
        resp.raise_for_status()
        result = resp.json()
        return {
            "screened": True,
            "match": result.get("match", False),
            "risk": "high" if result.get("match") else "clear",
            "detail": result.get("detail", ""),
            "lists_checked": result.get("lists_checked", []),
        }
    except Exception as e:
        logger.error(f"Sanctions screening failed: {e}")
        return {"screened": False, "match": False, "risk": "unknown", "detail": str(e)}
