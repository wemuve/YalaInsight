"""Module licensing: which product modules an organisation may use.

Effective modules = plan modules ± per-organisation overrides. No payment
processing is implemented; plans are assigned by a platform administrator.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Organization, OrgModuleOverride, SubscriptionPlan

ALL_MODULES = {
    "performance": "Plant Performance Intelligence",
    "finance": "Financial Loss & Recovery Simulator",
    "anomaly": "Anomaly Detection & Diagnostics",
    "maintenance": "Maintenance & Asset Lifecycle",
    "carbon": "Carbon & Sustainability Intelligence",
    "forecasting": "Renewable Energy Forecasting",
    "gis": "Multi-Plant GIS Intelligence",
    "reporting": "Reporting & Exports",
    "integrations": "Connectors & Webhooks",
    "api": "Third-party API access (API keys)",
}


def enabled_modules(db: Session, org: Organization | None) -> set[str]:
    if org is None:
        return set()
    plan = db.get(SubscriptionPlan, org.plan_code)
    mods = set(plan.modules if plan else [])
    for o in db.scalars(select(OrgModuleOverride).where(OrgModuleOverride.organization_id == org.id)):
        (mods.add if o.enabled else mods.discard)(o.module)
    return mods
