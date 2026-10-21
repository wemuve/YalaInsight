"""Organisation, users, API keys, module licensing, audit log and platform administration."""
from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...core.audit import audit
from ...core.deps import Principal, client_ip, require
from ...core.security import hash_password, new_api_key
from ...db import get_db, system_session
from ...licensing import ALL_MODULES, enabled_modules
from ...models import ROLES, ApiKey, AuditLog, Organization, OrgModuleOverride, Plant, SubscriptionPlan, User

router = APIRouter(tags=["organisation"])


def _check_password(pw: str) -> None:
    if len(pw) < 10 or not re.search(r"[A-Za-z]", pw) or not re.search(r"\d", pw):
        raise HTTPException(422, "Password must be at least 10 characters and contain letters and digits")


@router.get("/org")
def get_org(p: Principal = Depends(require("org:read")), db: Session = Depends(get_db)) -> dict:
    org = db.get(Organization, p.org_id)
    plan = db.get(SubscriptionPlan, org.plan_code)
    mods = enabled_modules(db, org)
    return {"id": str(org.id), "name": org.name, "slug": org.slug, "country_code": org.country_code,
            "default_currency": org.default_currency,
            "plan": {"code": plan.code, "name": plan.name, "max_plants": plan.max_plants,
                     "api_requests_per_minute": plan.api_requests_per_minute},
            "modules": [{"key": k, "name": v, "enabled": k in mods} for k, v in ALL_MODULES.items()],
            "plant_count": db.scalar(select(func.count(Plant.id)))}


class OrgPatch(BaseModel):
    name: str | None = Field(None, max_length=200)
    default_currency: str | None = Field(None, pattern="^[A-Z]{3}$")
    country_code: str | None = Field(None, pattern="^[A-Z]{2}$")


@router.patch("/org")
def patch_org(body: OrgPatch, request: Request, p: Principal = Depends(require("org:manage")), db: Session = Depends(get_db)) -> dict:
    org = db.get(Organization, p.org_id)
    changes = body.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(org, k, v)
    audit(db, org_id=p.org_id, user_id=p.user_id, action="org.update", entity_type="organization", entity_id=org.id,
          details=changes, ip=client_ip(request))
    db.commit()
    return get_org(p, db)


# --------------------------------------------------------------------------- users


class UserIn(BaseModel):
    email: str = Field(max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    full_name: str = Field(max_length=200)
    role: str
    password: str = Field(max_length=200)


class UserPatch(BaseModel):
    full_name: str | None = None
    role: str | None = None
    is_active: bool | None = None
    password: str | None = None


def _user_out(u: User) -> dict:
    return {"id": str(u.id), "email": u.email, "full_name": u.full_name, "role": u.role, "is_active": u.is_active,
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None}


def _check_role(role: str, actor: Principal) -> None:
    if role not in ROLES:
        raise HTTPException(422, f"role must be one of {ROLES}")
    if role == "platform_admin" and actor.role != "platform_admin":
        raise HTTPException(403, "Only platform administrators can grant platform_admin")


@router.get("/users")
def list_users(p: Principal = Depends(require("users:manage")), db: Session = Depends(get_db)) -> list[dict]:
    return [_user_out(u) for u in db.scalars(select(User).order_by(User.email))]


@router.get("/users/directory")
def user_directory(p: Principal = Depends(require("workorders:read")), db: Session = Depends(get_db)) -> list[dict]:
    """Minimal list for assignee pickers."""
    return [{"id": str(u.id), "full_name": u.full_name, "role": u.role}
            for u in db.scalars(select(User).where(User.is_active.is_(True)).order_by(User.full_name))]


@router.post("/users", status_code=201)
def create_user(body: UserIn, request: Request, p: Principal = Depends(require("users:manage")), db: Session = Depends(get_db)) -> dict:
    _check_role(body.role, p)
    _check_password(body.password)
    u = User(organization_id=p.org_id, email=body.email.strip().lower(), full_name=body.full_name, role=body.role,
             password_hash=hash_password(body.password))
    db.add(u)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A user with this email already exists") from None
    audit(db, org_id=p.org_id, user_id=p.user_id, action="user.create", entity_type="user", entity_id=u.id,
          details={"email": u.email, "role": u.role}, ip=client_ip(request))
    db.commit()
    return _user_out(u)


@router.patch("/users/{user_id}")
def patch_user(user_id: UUID, body: UserPatch, request: Request, p: Principal = Depends(require("users:manage")),
               db: Session = Depends(get_db)) -> dict:
    u = db.get(User, user_id)
    if not u or u.organization_id != p.org_id:
        raise HTTPException(404, "User not found")
    changes = body.model_dump(exclude_none=True)
    if "role" in changes:
        _check_role(changes["role"], p)
    if "password" in changes:
        _check_password(changes["password"])
        u.password_hash = hash_password(changes.pop("password"))
        changes["password"] = "changed"
    for k in ("full_name", "role", "is_active"):
        if k in changes:
            setattr(u, k, changes[k])
    audit(db, org_id=p.org_id, user_id=p.user_id, action="user.update", entity_type="user", entity_id=u.id,
          details=changes, ip=client_ip(request))
    db.commit()
    return _user_out(u)


# --------------------------------------------------------------------------- API keys


class ApiKeyIn(BaseModel):
    name: str = Field(max_length=120)
    role: str = "read_only"


@router.get("/api-keys")
def list_keys(p: Principal = Depends(require("apikeys:manage")), db: Session = Depends(get_db)) -> list[dict]:
    return [{"id": str(k.id), "name": k.name, "prefix": k.prefix, "role": k.role, "created_at": k.created_at.isoformat(),
             "revoked_at": k.revoked_at.isoformat() if k.revoked_at else None,
             "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None}
            for k in db.scalars(select(ApiKey).order_by(ApiKey.created_at.desc()))]


@router.post("/api-keys", status_code=201)
def create_key(body: ApiKeyIn, request: Request, p: Principal = Depends(require("apikeys:manage")), db: Session = Depends(get_db)) -> dict:
    if body.role not in ROLES or body.role in ("platform_admin", "client_admin"):
        raise HTTPException(422, "API keys may use roles: plant_owner, analyst, technician, read_only")
    full, prefix, h = new_api_key()
    k = ApiKey(organization_id=p.org_id, name=body.name, prefix=prefix, key_hash=h, role=body.role, created_by=p.user_id)
    db.add(k)
    db.flush()
    audit(db, org_id=p.org_id, user_id=p.user_id, action="apikey.create", entity_type="api_key", entity_id=k.id,
          details={"name": body.name, "role": body.role}, ip=client_ip(request))
    db.commit()
    return {"id": str(k.id), "key": full, "prefix": prefix, "role": k.role,
            "note": "Store this key securely; it is shown only once. Send it as the X-API-Key header."}


@router.delete("/api-keys/{key_id}", status_code=204)
def revoke_key(key_id: UUID, request: Request, p: Principal = Depends(require("apikeys:manage")), db: Session = Depends(get_db)) -> None:
    k = db.get(ApiKey, key_id)
    if not k or k.organization_id != p.org_id:
        raise HTTPException(404, "API key not found")
    k.revoked_at = datetime.now(UTC)
    audit(db, org_id=p.org_id, user_id=p.user_id, action="apikey.revoke", entity_type="api_key", entity_id=k.id, ip=client_ip(request))
    db.commit()


# --------------------------------------------------------------------------- audit


@router.get("/audit-logs")
def audit_logs(limit: int = 100, p: Principal = Depends(require("audit:read")), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(min(limit, 1000)))
    return [{"id": r.id, "action": r.action, "user_id": str(r.user_id) if r.user_id else None, "entity_type": r.entity_type,
             "entity_id": r.entity_id, "details": r.details, "ip": r.ip, "created_at": r.created_at.isoformat()} for r in rows]


# --------------------------------------------------------------------------- platform administration


class OrgCreate(BaseModel):
    name: str = Field(max_length=200)
    slug: str = Field(pattern="^[a-z0-9-]{3,80}$")
    plan_code: str = "starter"
    default_currency: str = Field("USD", pattern="^[A-Z]{3}$")
    country_code: str | None = Field(None, pattern="^[A-Z]{2}$")
    admin_email: str
    admin_name: str
    admin_password: str


class LicensePatch(BaseModel):
    plan_code: str | None = None
    module_overrides: dict[str, bool | None] | None = None  # None removes an override
    is_active: bool | None = None


@router.get("/platform/plans", tags=["platform"])
def plans(p: Principal = Depends(require("org:read")), db: Session = Depends(get_db)) -> list[dict]:
    return [{"code": x.code, "name": x.name, "description": x.description, "modules": x.modules, "max_plants": x.max_plants,
             "api_requests_per_minute": x.api_requests_per_minute} for x in db.scalars(select(SubscriptionPlan))]


@router.get("/platform/organizations", tags=["platform"])
def list_orgs(p: Principal = Depends(require("platform:admin"))) -> list[dict]:
    with system_session() as db:
        return [{"id": str(o.id), "name": o.name, "slug": o.slug, "plan_code": o.plan_code, "is_active": o.is_active,
                 "modules": sorted(enabled_modules(db, o))} for o in db.scalars(select(Organization).order_by(Organization.name))]


@router.post("/platform/organizations", status_code=201, tags=["platform"])
def create_org(body: OrgCreate, request: Request, p: Principal = Depends(require("platform:admin"))) -> dict:
    _check_password(body.admin_password)
    with system_session() as db:
        if not db.get(SubscriptionPlan, body.plan_code):
            raise HTTPException(422, "unknown plan")
        org = Organization(name=body.name, slug=body.slug, plan_code=body.plan_code, default_currency=body.default_currency,
                           country_code=body.country_code)
        db.add(org)
        try:
            db.flush()
            admin = User(organization_id=org.id, email=body.admin_email.lower(), full_name=body.admin_name, role="client_admin",
                         password_hash=hash_password(body.admin_password))
            db.add(admin)
            db.flush()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "Organisation slug or admin email already exists") from None
        audit(db, org_id=org.id, user_id=p.user_id, action="platform.org_create", entity_type="organization", entity_id=org.id,
              details={"plan": body.plan_code}, ip=client_ip(request))
        db.commit()
        return {"id": str(org.id), "admin_user_id": str(admin.id)}


@router.patch("/platform/organizations/{org_id}/license", tags=["platform"])
def patch_license(org_id: UUID, body: LicensePatch, request: Request, p: Principal = Depends(require("platform:admin"))) -> dict:
    with system_session() as db:
        org = db.get(Organization, org_id)
        if not org:
            raise HTTPException(404, "Organisation not found")
        if body.plan_code:
            if not db.get(SubscriptionPlan, body.plan_code):
                raise HTTPException(422, "unknown plan")
            org.plan_code = body.plan_code
        if body.is_active is not None:
            org.is_active = body.is_active
        for mod, enabled in (body.module_overrides or {}).items():
            if mod not in ALL_MODULES:
                raise HTTPException(422, f"unknown module {mod}")
            ov = db.scalar(select(OrgModuleOverride).where(OrgModuleOverride.organization_id == org.id, OrgModuleOverride.module == mod))
            if enabled is None:
                if ov:
                    db.delete(ov)
            elif ov:
                ov.enabled = enabled
            else:
                db.add(OrgModuleOverride(organization_id=org.id, module=mod, enabled=enabled))
        audit(db, org_id=org.id, user_id=p.user_id, action="platform.license_update", entity_type="organization",
              entity_id=org.id, details=body.model_dump(exclude_none=True), ip=client_ip(request))
        db.commit()
        return {"id": str(org.id), "plan_code": org.plan_code, "modules": sorted(enabled_modules(db, org))}
