"""
app/services/integrations/crypto.py

Encrypts vendor credentials before they're stored in
integration_connections.credentials. Without this, a future client's real
Fireblocks/Sardine API keys would sit in Supabase as plain jsonb — readable
by anyone with DB access, visible in any accidental query log, and a
liability the moment you onboard a real client.

Uses Fernet (symmetric, AES128-CBC + HMAC) — appropriate here because the
backend itself needs to read these credentials back to make API calls;
this is encryption-at-rest against DB-level exposure, not end-to-end
encryption the backend itself can't decrypt.

Requires: pip install cryptography  (add to requirements.txt)

Setup (one-time):
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
Set the output as INTEGRATION_ENCRYPTION_KEY in Render's environment
variables. Treat it like any other production secret — losing it makes
every stored credential permanently undecryptable; rotating it requires
re-encrypting every existing row.
"""
from __future__ import annotations

import json
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = os.environ.get("INTEGRATION_ENCRYPTION_KEY")
        if not key:
            raise RuntimeError(
                "INTEGRATION_ENCRYPTION_KEY is not set — cannot encrypt/decrypt "
                "vendor credentials. Generate one with: "
                "python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\" "
                "and set it in your environment before connecting any integration."
            )
        _fernet = Fernet(key.encode())
    return _fernet


def encrypt_credentials(credentials: dict) -> str:
    """Returns an opaque encrypted string, safe to store in a jsonb/text column."""
    raw = json.dumps(credentials).encode()
    return _get_fernet().encrypt(raw).decode()


def decrypt_credentials(encrypted: str) -> dict:
    """Inverse of encrypt_credentials. Raises if the key is wrong or data is corrupted."""
    try:
        raw = _get_fernet().decrypt(encrypted.encode())
        return json.loads(raw)
    except InvalidToken:
        logger.error("Failed to decrypt stored credentials — wrong key or corrupted data")
        raise
