from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ...config import settings
from ...core.audit import audit
from ...core.deps import Principal, client_ip, get_principal
from ...core.ratelimit import limiter
from ...core.rbac import PERMISSIONS
from ...core.security import create_access_token, verify_password
from ...db import system_session
from ...licensing import enabled_modules
from ...models import Organization, User

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(min_length=1, max_length=200)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, request: Request) -> TokenOut:
    ok, retry = limiter.allow(f"login:{client_ip(request)}", settings.login_rate_limit_per_minute)
    if not ok:
        raise HTTPException(429, "Too many login attempts", headers={"Retry-After": str(retry)})
    with system_session() as db:
        user = db.scalar(select(User).where(func.lower(User.email) == body.email.strip().lower()))
        if not user or not user.is_active or not verify_password(body.password, user.password_hash):
            audit(db, org_id=user.organization_id if user else None, user_id=user.id if user else None,
                  action="auth.login_failed", details={"email": body.email[:254]}, ip=client_ip(request))
            db.commit()
            raise HTTPException(401, "Invalid email or password")
        org = db.get(Organization, user.organization_id)
        if not org or not org.is_active:
            raise HTTPException(403, "Organisation is inactive")
        user.last_login_at = datetime.now(UTC)
        audit(db, org_id=user.organization_id, user_id=user.id, action="auth.login", ip=client_ip(request))
        db.commit()
        return TokenOut(access_token=create_access_token(user.id, user.organization_id, user.role),
                        expires_in=settings.jwt_ttl_minutes * 60)


@router.get("/me")
def me(principal: Principal = Depends(get_principal)) -> dict:
    with system_session() as db:
        org = db.get(Organization, principal.org_id)
        user = db.get(User, principal.user_id) if principal.user_id else None
        return {
            "user": {"id": str(user.id), "email": user.email, "full_name": user.full_name, "role": user.role} if user else None,
            "api_key_id": str(principal.api_key_id) if principal.api_key_id else None,
            "organization": {"id": str(org.id), "name": org.name, "plan": org.plan_code, "currency": org.default_currency},
            "modules": sorted(enabled_modules(db, org)),
            "permissions": sorted(PERMISSIONS.get(principal.role, set())),
        }
