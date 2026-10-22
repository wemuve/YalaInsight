"""Database engine, sessions and tenant context.

Tenant isolation is enforced twice:
1. Application layer — every tenant-scoped query filters on organization_id.
2. Database layer — PostgreSQL row-level security (RLS) policies, created in
   the initial migration, restrict rows to ``current_setting('app.org_id')``.

The tenant context is stored on ``Session.info`` and re-applied with
``set_config(..., is_local => true)`` at the start of every transaction, so it
cannot leak between pooled connections.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.database_url, pool_pre_ping=True, pool_size=10, max_overflow=20)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@event.listens_for(Session, "after_begin")
def _apply_tenant_context(session: Session, transaction, connection) -> None:  # noqa: ANN001
    org_id = session.info.get("org_id")
    bypass = session.info.get("bypass_rls", False)
    connection.execute(
        text("SELECT set_config('app.org_id', :o, true), set_config('app.bypass_rls', :b, true)"),
        {"o": str(org_id) if org_id else "", "b": "on" if bypass else "off"},
    )


def set_tenant(session: Session, org_id: UUID | None, bypass: bool = False) -> None:
    """Bind the session to a tenant. Takes effect immediately and for later transactions."""
    session.info["org_id"] = org_id
    session.info["bypass_rls"] = bypass
    if session.in_transaction():
        session.execute(
            text("SELECT set_config('app.org_id', :o, true), set_config('app.bypass_rls', :b, true)"),
            {"o": str(org_id) if org_id else "", "b": "on" if bypass else "off"},
        )


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def system_session() -> Iterator[Session]:
    """Session that bypasses RLS — for migrations, seeding, login lookups and background jobs only."""
    db = SessionLocal()
    set_tenant(db, None, bypass=True)
    try:
        yield db
    finally:
        db.close()
