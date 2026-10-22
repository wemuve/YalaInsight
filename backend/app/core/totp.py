"""Time-based one-time passwords (RFC 6238, SHA-1, 6 digits, 30 s) — compatible with standard authenticator apps."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def code_at(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def verify(secret: str, code: str, at: float | None = None, window: int = 1) -> bool:
    """Accept the current code and ±window steps to tolerate clock drift."""
    code = (code or "").strip().replace(" ", "")
    if len(code) != 6 or not code.isdigit():
        return False
    counter = int((time.time() if at is None else at) // 30)
    return any(hmac.compare_digest(code_at(secret, counter + d), code) for d in range(-window, window + 1))


def provisioning_uri(secret: str, account: str, issuer: str = "YalaInsight") -> str:
    return (f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret}&issuer={quote(issuer)}"
            "&algorithm=SHA1&digits=6&period=30")
