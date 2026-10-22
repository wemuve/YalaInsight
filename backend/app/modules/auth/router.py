from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ...config import settings
from ...core import totp
from ...core.audit import audit
from ...core.crypto import decrypt, encrypt
from ...core.deps import Principal, client_ip, get_principal
from ...core.ratelimit import limiter
from ...core.rbac import PERMISSIONS
from ...core.security import check_password_policy, create_access_token, hash_password, verify_password
from ...db import system_session
from ...licensing import enabled_modules
from ...models import Organization, SubscriptionPlan, User

router = APIRouter(prefix="/auth", tags=["auth"])

# Pre-computed hash so unknown emails take as long as wrong passwords (no user enumeration by timing).
_DUMMY_HASH = hash_password("timing-equaliser-not-a-password-1")


class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(min_length=1, max_length=200)
    otp: str | None = Field(None, max_length=10)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    must_change_password: bool = False


def _token(user: User) -> TokenOut:
    return TokenOut(access_token=create_access_token(user.id, user.organization_id, user.role, user.token_version),
                    expires_in=settings.jwt_ttl_minutes * 60, must_change_password=user.must_change_password)


@router.get("/config")
def public_config() -> dict:
    """Unauthenticated client configuration (no secrets)."""
    return {"show_demo_login": settings.show_demo_login, "allow_signup": settings.allow_signup, "map_tile_url": settings.map_tile_url,
            "map_tile_attribution": settings.map_tile_attribution, "environment": settings.env}


class SignupIn(BaseModel):
    organisation_name: str = Field(min_length=2, max_length=200)
    full_name: str = Field(min_length=2, max_length=200)
    email: str = Field(max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(max_length=200)
    country_code: str | None = Field(None, pattern="^[A-Z]{2}$")
    default_currency: str = Field("USD", pattern="^[A-Z]{3}$")


def _slug(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "org"
    return f"{base}-{secrets.token_hex(3)}"


@router.post("/signup", response_model=TokenOut, status_code=201)
def signup(body: SignupIn, request: Request) -> TokenOut:
    """Create a new organisation and its first administrator, then sign them in."""
    if not settings.allow_signup:
        raise HTTPException(403, "Sign-up is closed on this instance. Ask your administrator for an invitation.")
    ip = client_ip(request)
    ok, retry = limiter.allow(f"signup:{ip}", settings.signups_per_hour_per_ip, window_s=3600)
    if not ok:
        raise HTTPException(429, "Too many sign-ups from this network. Try again later.", headers={"Retry-After": str(retry)})
    if err := check_password_policy(body.password):
        raise HTTPException(422, err)
    email = body.email.strip().lower()
    with system_session() as db:
        if db.scalar(select(User).where(func.lower(User.email) == email)):
            raise HTTPException(409, "An account with this email already exists. Sign in instead.")
        if not db.get(SubscriptionPlan, settings.signup_plan):
            raise HTTPException(500, "Sign-up plan is misconfigured")
        org = Organization(name=body.organisation_name.strip(), slug=_slug(body.organisation_name), plan_code=settings.signup_plan,
                           default_currency=body.default_currency, country_code=body.country_code)
        db.add(org)
        db.flush()
        user = User(organization_id=org.id, email=email, full_name=body.full_name.strip(), role="client_admin",
                    password_hash=hash_password(body.password), last_login_at=datetime.now(UTC))
        db.add(user)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "An account with this email already exists. Sign in instead.") from None
        audit(db, org_id=org.id, user_id=user.id, action="auth.signup", entity_type="organization", entity_id=org.id,
              details={"plan": org.plan_code}, ip=ip)
        db.commit()
        return _token(user)


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, request: Request) -> TokenOut:
    ip = client_ip(request)
    ok, retry = limiter.allow(f"login:{ip}", settings.login_rate_limit_per_minute)
    if not ok:
        raise HTTPException(429, "Too many login attempts", headers={"Retry-After": str(retry)})
    now = datetime.now(UTC)
    with system_session() as db:
        user = db.scalar(select(User).where(func.lower(User.email) == body.email.strip().lower()))
        if user and user.locked_until and user.locked_until > now:
            audit(db, org_id=user.organization_id, user_id=user.id, action="auth.login_locked", ip=ip)
            db.commit()
            raise HTTPException(423, f"Account temporarily locked after repeated failures. Try again after "
                                     f"{user.locked_until:%H:%M} UTC or ask an administrator to reset it.")
        valid = verify_password(body.password, user.password_hash if user else _DUMMY_HASH)
        if not user or not user.is_active or not valid:
            if user:
                user.failed_logins += 1
                if user.failed_logins >= settings.login_max_failures:
                    user.locked_until = now + timedelta(minutes=settings.lockout_minutes)
                    user.failed_logins = 0
            audit(db, org_id=user.organization_id if user else None, user_id=user.id if user else None,
                  action="auth.login_failed", details={"email": body.email[:254]}, ip=ip)
            db.commit()
            raise HTTPException(401, "Invalid email or password")
        if user.totp_enabled:
            secret = decrypt(user.totp_secret or "")
            if not body.otp:
                raise HTTPException(401, "mfa_required")
            if not secret or not totp.verify(secret, body.otp):
                user.failed_logins += 1
                audit(db, org_id=user.organization_id, user_id=user.id, action="auth.mfa_failed", ip=ip)
                db.commit()
                raise HTTPException(401, "Invalid authentication code")
        org = db.get(Organization, user.organization_id)
        if not org or not org.is_active:
            raise HTTPException(403, "Organisation is inactive")
        user.last_login_at, user.failed_logins, user.locked_until = now, 0, None
        audit(db, org_id=user.organization_id, user_id=user.id, action="auth.login", ip=ip)
        db.commit()
        return _token(user)


@router.get("/me")
def me(principal: Principal = Depends(get_principal)) -> dict:
    with system_session() as db:
        org = db.get(Organization, principal.org_id)
        user = db.get(User, principal.user_id) if principal.user_id else None
        return {
            "user": {"id": str(user.id), "email": user.email, "full_name": user.full_name, "role": user.role,
                     "mfa_enabled": user.totp_enabled, "must_change_password": user.must_change_password,
                     "plant_scope": user.plant_scope} if user else None,
            "api_key_id": str(principal.api_key_id) if principal.api_key_id else None,
            "organization": {"id": str(org.id), "name": org.name, "plan": org.plan_code, "currency": org.default_currency},
            "modules": sorted(enabled_modules(db, org)),
            "permissions": sorted(PERMISSIONS.get(principal.role, set())),
        }


def _user_for(principal: Principal, db) -> User:  # noqa: ANN001
    if not principal.user_id:
        raise HTTPException(403, "Only signed-in users can manage their account")
    return db.get(User, principal.user_id)


class PasswordChangeIn(BaseModel):
    current_password: str = Field(max_length=200)
    new_password: str = Field(max_length=200)


@router.post("/change-password", response_model=TokenOut)
def change_password(body: PasswordChangeIn, request: Request, principal: Principal = Depends(get_principal)) -> TokenOut:
    with system_session() as db:
        user = _user_for(principal, db)
        if not verify_password(body.current_password, user.password_hash):
            raise HTTPException(401, "Current password is incorrect")
        if err := check_password_policy(body.new_password):
            raise HTTPException(422, err)
        if verify_password(body.new_password, user.password_hash):
            raise HTTPException(422, "New password must differ from the current one")
        user.password_hash = hash_password(body.new_password)
        user.must_change_password = False
        user.token_version += 1  # signs out every other session
        audit(db, org_id=user.organization_id, user_id=user.id, action="auth.password_changed", ip=client_ip(request))
        db.commit()
        return _token(user)


@router.post("/logout-all", status_code=204)
def logout_all(request: Request, principal: Principal = Depends(get_principal)) -> None:
    with system_session() as db:
        user = _user_for(principal, db)
        user.token_version += 1
        audit(db, org_id=user.organization_id, user_id=user.id, action="auth.logout_all", ip=client_ip(request))
        db.commit()


@router.post("/mfa/setup")
def mfa_setup(principal: Principal = Depends(get_principal)) -> dict:
    """Generate a new (not yet active) authenticator secret. Confirm it with /auth/mfa/enable."""
    with system_session() as db:
        user = _user_for(principal, db)
        if user.totp_enabled:
            raise HTTPException(409, "Two-factor authentication is already enabled")
        secret = totp.new_secret()
        user.totp_secret = encrypt(secret)
        db.commit()
        return {"secret": secret, "otpauth_uri": totp.provisioning_uri(secret, user.email)}


class OtpIn(BaseModel):
    otp: str = Field(max_length=10)
    password: str | None = Field(None, max_length=200)


@router.post("/mfa/enable")
def mfa_enable(body: OtpIn, request: Request, principal: Principal = Depends(get_principal)) -> dict:
    with system_session() as db:
        user = _user_for(principal, db)
        secret = decrypt(user.totp_secret or "")
        if not secret:
            raise HTTPException(409, "Start with /auth/mfa/setup")
        if not totp.verify(secret, body.otp):
            raise HTTPException(422, "Code does not match — check the time on your device and try again")
        user.totp_enabled = True
        audit(db, org_id=user.organization_id, user_id=user.id, action="auth.mfa_enabled", ip=client_ip(request))
        db.commit()
        return {"mfa_enabled": True}


@router.post("/mfa/disable")
def mfa_disable(body: OtpIn, request: Request, principal: Principal = Depends(get_principal)) -> dict:
    with system_session() as db:
        user = _user_for(principal, db)
        secret = decrypt(user.totp_secret or "")
        if not user.totp_enabled or not secret:
            raise HTTPException(409, "Two-factor authentication is not enabled")
        if not body.password or not verify_password(body.password, user.password_hash) or not totp.verify(secret, body.otp):
            raise HTTPException(401, "Password or authentication code is incorrect")
        user.totp_enabled, user.totp_secret = False, None
        audit(db, org_id=user.organization_id, user_id=user.id, action="auth.mfa_disabled", ip=client_ip(request))
        db.commit()
        return {"mfa_enabled": False}
