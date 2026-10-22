"""Idempotent first-run setup, executed by the release step (deploy/release.sh) after migrations.

Environment:
  YALA_BOOTSTRAP_ADMIN_EMAIL / YALA_BOOTSTRAP_ADMIN_PASSWORD
      Creates the first platform administrator if none is active. The password is temporary: it must be
      changed at first sign-in. Remove the password variable after the first deploy.
  YALA_LOAD_DEMO=true
      Loads the synthetic demo portfolio (demo users share a published password — never enable this on an
      instance that holds real customer data). The demo's built-in platform admin is deactivated when a
      bootstrap admin is configured.
"""
from __future__ import annotations

import os

from sqlalchemy import select

from .core.security import check_password_policy, hash_password
from .db import system_session
from .models import Organization, User


def main() -> None:
    if os.getenv("YALA_LOAD_DEMO", "").lower() in {"1", "true", "yes"}:
        from . import seed
        seed.run(do_reset=False)
    email = (os.getenv("YALA_BOOTSTRAP_ADMIN_EMAIL") or "").strip().lower()
    password = os.getenv("YALA_BOOTSTRAP_ADMIN_PASSWORD") or ""
    with system_session() as db:
        if not email:
            if not db.scalar(select(User).where(User.role == "platform_admin", User.is_active.is_(True))):
                print("bootstrap: no platform administrator exists — set YALA_BOOTSTRAP_ADMIN_EMAIL and _PASSWORD")
            return
        existing = db.scalar(select(User).where(User.email == email))
        if existing:
            print(f"bootstrap: {email} already exists — nothing to do")
            return
        if err := check_password_policy(password):
            raise SystemExit(f"bootstrap: YALA_BOOTSTRAP_ADMIN_PASSWORD rejected: {err}")
        org = db.scalar(select(Organization).where(Organization.slug == "platform"))
        if org is None:
            org = Organization(name="YalaInsight Platform", slug="platform", plan_code="enterprise", default_currency="USD")
            db.add(org)
            db.flush()
        db.add(User(organization_id=org.id, email=email, full_name="Platform Administrator", role="platform_admin",
                    password_hash=hash_password(password), must_change_password=True))
        demo_admin = db.scalar(select(User).where(User.email == "platform@yalainsight.local"))
        if demo_admin:
            demo_admin.is_active = False
            demo_admin.token_version += 1
        db.commit()
        print(f"bootstrap: created platform administrator {email} (password change required at first sign-in)")


if __name__ == "__main__":
    main()
