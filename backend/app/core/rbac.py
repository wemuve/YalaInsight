"""Role-based permissions.

Roles are hierarchical in practice but permissions are listed explicitly so the
matrix is auditable (see docs/SECURITY.md).
"""
from __future__ import annotations

PERMISSIONS: dict[str, set[str]] = {
    "platform_admin": {"*"},
    "client_admin": {
        "org:read", "org:manage", "users:manage", "apikeys:manage", "webhooks:manage",
        "plants:read", "plants:write", "data:import", "data:export", "finance:read", "finance:simulate",
        "alerts:read", "alerts:review", "alerts:run", "workorders:read", "workorders:write", "workorders:execute",
        "carbon:read", "carbon:write", "reports:read", "audit:read",
    },
    "plant_owner": {
        "org:read", "plants:read", "plants:write", "data:import", "data:export", "finance:read", "finance:simulate",
        "alerts:read", "alerts:review", "alerts:run", "workorders:read", "workorders:write", "carbon:read",
        "carbon:write", "reports:read",
    },
    "analyst": {
        "org:read", "plants:read", "data:import", "data:export", "finance:read", "finance:simulate", "alerts:read",
        "alerts:review", "alerts:run", "workorders:read", "workorders:write", "carbon:read", "reports:read",
    },
    "technician": {
        "org:read", "plants:read", "alerts:read", "alerts:review", "workorders:read", "workorders:execute", "reports:read",
    },
    "read_only": {"org:read", "plants:read", "finance:read", "alerts:read", "workorders:read", "carbon:read", "reports:read"},
}


def has_permission(role: str, permission: str) -> bool:
    perms = PERMISSIONS.get(role, set())
    return "*" in perms or permission in perms
