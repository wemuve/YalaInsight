"""Request dependencies: authentication, tenant binding, permissions, module licensing, rate limits."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db, set_tenant, system_session
from ..licensing import enabled_modules
from ..models import ApiKey, Organization, SubscriptionPlan, User
from .ratelimit import limiter
from .rbac import has_permission
from .security import api_key_matches, decode_access_token


@dataclass
class Principal:
    org_id: UUID
    role: str
    user_id: UUID | None = None
    api_key_id: UUID | None = None
    email: str | None = None
    plant_scope: list[UUID] | None = None  # None = every plant in the organisation

    @property
    def actor(self) -> str:
        return self.email or f"api_key:{self.api_key_id}"


PASSWORD_CHANGE_ALLOWED = {"/api/v1/auth/me", "/api/v1/auth/change-password", "/api/v1/auth/logout-all"}


def _unauthorized(detail: str = "Not authenticated") -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"})


def _resolve_api_key(raw: str) -> tuple[ApiKey, int]:
    parts = raw.split("_")
    if len(parts) < 3 or parts[0] != "yala":
        raise _unauthorized("Invalid API key")
    with system_session() as sdb:
        key = sdb.scalar(select(ApiKey).where(ApiKey.prefix == parts[1], ApiKey.revoked_at.is_(None)))
        if not key or not api_key_matches(raw, key.key_hash):
            raise _unauthorized("Invalid API key")
        org = sdb.get(Organization, key.organization_id)
        plan = sdb.get(SubscriptionPlan, org.plan_code) if org else None
        if not org or not org.is_active:
            raise _unauthorized("Organisation inactive")
        if "api" not in enabled_modules(sdb, org):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "API access is not included in this organisation's plan")
        key.last_used_at = datetime.now(UTC)
        sdb.commit()
        sdb.expunge(key)
        return key, (plan.api_requests_per_minute if plan else 60)


def get_principal(request: Request, db: Session = Depends(get_db), authorization: str | None = Header(None),
                  x_api_key: str | None = Header(None)) -> Principal:
    from ..config import settings

    if x_api_key:
        key, limit = _resolve_api_key(x_api_key)
        ok, retry = limiter.allow(f"key:{key.id}", limit)
        if not ok:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "API rate limit exceeded", headers={"Retry-After": str(retry)})
        principal = Principal(org_id=key.organization_id, role=key.role, api_key_id=key.id)
    else:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise _unauthorized()
        try:
            claims = decode_access_token(authorization.split(" ", 1)[1])
        except jwt.PyJWTError:
            raise _unauthorized("Invalid or expired token") from None
        principal = Principal(org_id=UUID(claims["org"]), role=claims["role"], user_id=UUID(claims["sub"]))
        token_version = claims.get("ver", 0)
        ok, retry = limiter.allow(f"user:{principal.user_id}", settings.rate_limit_per_minute)
        if not ok:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Rate limit exceeded", headers={"Retry-After": str(retry)})

    set_tenant(db, principal.org_id)
    if principal.user_id:
        user = db.get(User, principal.user_id)  # RLS-scoped lookup also proves the token's org matches
        if not user or not user.is_active or user.organization_id != principal.org_id:
            raise _unauthorized("User inactive")
        if user.token_version != token_version:
            raise _unauthorized("Session has been revoked — please sign in again")
        principal.role = user.role  # role changes take effect immediately
        principal.email = user.email
        principal.plant_scope = [UUID(str(x)) for x in user.plant_scope] if user.plant_scope is not None else None
        if user.must_change_password and request.url.path not in PASSWORD_CHANGE_ALLOWED:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Password change required before continuing")
    request.state.principal = principal
    return principal


def require(permission: str):
    def dep(principal: Principal = Depends(get_principal)) -> Principal:
        if not has_permission(principal.role, permission):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Missing permission: {permission}")
        return principal

    return dep


def require_module(module: str):
    def dep(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> None:
        org = db.get(Organization, principal.org_id)
        if module not in enabled_modules(db, org):
            raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED,
                                f"Module '{module}' is not enabled for this organisation's subscription")

    return dep


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None
