"""Password hashing, JWT issuance and API-key hashing."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

import bcrypt
import jwt

from ..config import settings

ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        return False


def create_access_token(user_id: UUID, org_id: UUID, role: str) -> str:
    now = datetime.now(UTC)
    payload = {"sub": str(user_id), "org": str(org_id), "role": role, "iat": now,
               "exp": now + timedelta(minutes=settings.jwt_ttl_minutes), "typ": "access"}
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM], options={"require": ["exp", "sub", "org"]})


def new_api_key() -> tuple[str, str, str]:
    """Return (full_key, prefix, sha256_hash). Only the hash is stored."""
    prefix = secrets.token_hex(4)
    full = f"yala_{prefix}_{secrets.token_urlsafe(32)}"
    return full, prefix, hash_api_key(full)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def api_key_matches(key: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_api_key(key), stored_hash)
