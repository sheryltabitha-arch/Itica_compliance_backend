"""
app/services/dilisense_client.py
Real dilisense sanctions/PEP screening (GET /v1/checkIndividual, x-api-key).

Returns the same dict shape screen_entity() already uses, so extraction.py
needs no changes. Errors are RAISED (not swallowed) so screen_entity()'s
existing except-block records {"screened": False, ...} and extraction.py
forces requires_review. A failed call is never cached and never reads as clear.
"""
from __future__ import annotations
import hashlib
import logging
import os
import time
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

# Fixed on purpose: the old DILISENSE_API_URL env var pointed at a wrong path.
CHECK_INDIVIDUAL_URL = "https://api.dilisense.com/v1/checkIndividual"

# Free tier is ~100 calls/month, so repeat lookups (demo rehearsals) are cached.
# In-memory only: resets on restart/redeploy and is not shared across instances.
_CACHE: dict[str, tuple[float, dict]] = {}
_TTL_SECONDS = 24 * 60 * 60


def _to_dilisense_dob(dob: str) -> str:
    """Mindee usually returns YYYY-MM-DD; dilisense documents DD/MM/YYYY."""
    dob = (dob or "").strip()
    if len(dob) == 10 and dob[4] == "-" and dob[7] == "-":
        y, m, d = dob.split("-")
        return f"{d}/{m}/{y}"
    return dob


def check_individual(name: str, dob: str = "") -> dict:
    api_key = os.environ.get("DILISENSE_API_KEY", "")
    if not api_key:
        raise RuntimeError("DILISENSE_API_KEY not set")

    dob = _to_dilisense_dob(dob)

    # Cache key is a hash so names/DOBs are not held as plaintext keys.
    cache_key = hashlib.sha256(f"{name.strip().lower()}|{dob}".encode()).hexdigest()
    cached = _CACHE.get(cache_key)
    if cached and time.time() - cached[0] < _TTL_SECONDS:
        return cached[1]

    params = {"names": name, "fuzzy_search": 1}
    if dob:
        params["dob"] = dob

    # requests' own exception text includes the full URL (name + DOB in the
    # query string). Re-raise a sanitised error so PII never reaches logs,
    # the extractions table, or the API response.
    try:
        resp = requests.get(
            CHECK_INDIVIDUAL_URL,
            params=params,
            headers={"x-api-key": api_key},
            timeout=10,
        )
        resp.raise_for_status()  # 401/403/429/5xx -> recorded as not screened
        body = resp.json()
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "unknown"
        raise RuntimeError(f"dilisense returned HTTP {code}") from None
    except (requests.RequestException, ValueError) as e:
        raise RuntimeError(f"dilisense request failed ({type(e).__name__})") from None

    records = body.get("found_records") or []
    hits = [
        {
            "name": r.get("name"),
            "source_id": r.get("source_id"),
            "source_type": r.get("source_type"),
            "pep_type": r.get("pep_type"),
            "date_of_birth": r.get("date_of_birth"),
            "citizenship": r.get("citizenship"),
        }
        for r in records
    ]

    result = {
        "screened": True,
        "match": len(hits) > 0,
        "risk": "high" if hits else "clear",
        "detail": f"{len(hits)} potential match(es) returned by dilisense",
        "lists_checked": sorted({h["source_id"] for h in hits if h.get("source_id")}),
        "hits": hits,
        "provider": "dilisense",
        "screened_at": datetime.now(timezone.utc).isoformat(),
    }
    _CACHE[cache_key] = (time.time(), result)
    return result
