"""
app/services/sanctions.py
Sanctions/PEP screening. Primary provider: dilisense (per-call pricing,
UN/OFAC/EU + African Development Bank Group coverage). Falls back to a
generic SANCTIONS_API_URL if dilisense is not configured, and to the
unconfigured stub if neither is set.
"""
from __future__ import annotations
import logging
import os
import requests

logger = logging.getLogger(__name__)

DILISENSE_API_URL = os.environ.get("DILISENSE_API_URL", "https://api.dilisense.com/v1/screening")
DILISENSE_API_KEY = os.environ.get("DILISENSE_API_KEY", "")

SANCTIONS_API_URL = os.environ.get("SANCTIONS_API_URL", "")
SANCTIONS_API_KEY = os.environ.get("SANCTIONS_API_KEY", "")


def _screen_via_dilisense(name: str, dob: str, nationality: str) -> dict:
    resp = requests.post(
        DILISENSE_API_URL,
        headers={"Authorization": f"Bearer {DILISENSE_API_KEY}"},
        json={"name": name, "dob": dob, "country": nationality},
        timeout=10,
    )
    resp.raise_for_status()
    result = resp.json()
    hits = result.get("hits", [])
    return {
        "screened": True,
        "match": len(hits) > 0,
        "risk": "high" if hits else "clear",
        "detail": result.get("detail", ""),
        "lists_checked": result.get("lists_checked", []),
        "hits": hits,
        "provider": "dilisense",
    }


def _screen_via_generic(name: str, dob: str, nationality: str) -> dict:
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
        "provider": "generic",
    }


def screen_entity(name: str, dob: str = "", nationality: str = "") -> dict:
    """
    Screen an extracted entity against sanctions/PEP lists.
    Tries dilisense first, then a generic configured provider, then
    falls back to the unconfigured stub. Never silently marks an
    entity as screened/clear when no real provider ran.
    """
    if DILISENSE_API_KEY:
        try:
            return _screen_via_dilisense(name, dob, nationality)
        except Exception as e:
            logger.error(f"dilisense screening failed: {e}")
            return {"screened": False, "match": False, "risk": "unknown", "detail": str(e), "provider": "dilisense"}

    if SANCTIONS_API_URL:
        try:
            return _screen_via_generic(name, dob, nationality)
        except Exception as e:
            logger.error(f"Sanctions screening failed: {e}")
            return {"screened": False, "match": False, "risk": "unknown", "detail": str(e), "provider": "generic"}

    # Stub — log and return unscreened until a real provider is wired.
    # extraction.py already forces requires_review when screened is False.
    logger.warning("No sanctions provider configured (DILISENSE_API_KEY / SANCTIONS_API_URL) — screening skipped")
    return {"screened": False, "match": False, "risk": "unknown", "detail": "Screening not configured", "provider": None}
