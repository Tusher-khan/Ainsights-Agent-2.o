"""Encryption for stored connection credentials.

Database passwords, API keys and bearer tokens are encrypted at rest with Fernet
(AES-128-CBC + HMAC). They are decrypted only inside the connector that opens the
connection, in server memory, for the duration of the call. They are never
returned by the API, never put in a prompt and never logged.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, Iterable

from config import Config
from app.utils.exceptions import AInsightError

try:  # pragma: no cover - cryptography is a listed requirement
    from cryptography.fernet import Fernet, InvalidToken

    CRYPTO_AVAILABLE = True
except Exception:  # pragma: no cover
    Fernet = None
    InvalidToken = Exception
    CRYPTO_AVAILABLE = False


class SecretDecryptError(AInsightError):
    status_code = 422

    def __init__(self, detail: str = "") -> None:
        super().__init__(
            "The saved credentials for this connection can no longer be read. "
            "Edit the connection and enter them again.",
            detail,
        )


def _fernet() -> "Fernet":
    if not CRYPTO_AVAILABLE:
        raise AInsightError(
            "Credential encryption is unavailable on this server.",
            "the 'cryptography' package is not installed",
        )
    material = Config.CONNECTOR_ENCRYPTION_KEY or Config.SECRET_KEY
    digest = hashlib.sha256(("ainsight-connector-v1|" + material).encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_json(payload: dict[str, Any]) -> str:
    return _fernet().encrypt(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")


def decrypt_json(token: str | None) -> dict[str, Any]:
    if not token:
        return {}
    try:
        return json.loads(_fernet().decrypt(token.encode("ascii")).decode("utf-8"))
    except (InvalidToken, ValueError) as exc:
        raise SecretDecryptError(type(exc).__name__) from exc


def redact(text: str, secrets: Iterable[str]) -> str:
    """Remove any secret value that leaked into an error message."""
    out = str(text)
    for value in secrets:
        value = str(value or "")
        if len(value) >= 4:
            out = out.replace(value, "***")
    return out
