from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from ..models import AuditLog


def audit(db: Session, *, org_id: UUID | None, user_id: UUID | None, action: str, entity_type: str | None = None,
          entity_id: object = None, details: dict | None = None, ip: str | None = None) -> None:
    """Append an audit entry in the caller's transaction (committed together with the change)."""
    db.add(AuditLog(organization_id=org_id, user_id=user_id, action=action, entity_type=entity_type,
                    entity_id=str(entity_id) if entity_id is not None else None, details=details or {}, ip=ip))
