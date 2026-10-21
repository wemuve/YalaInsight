"""Relational schema for YalaInsight.

Every tenant-owned table carries ``organization_id`` and is protected by a
row-level-security policy (see migrations). Time-series energy values are
stored in kWh per interval; irradiance in W/m²; temperatures in °C; money as
NUMERIC with an explicit ISO-4217 currency column.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from geoalchemy2 import Geography
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

ROLES = ("platform_admin", "client_admin", "plant_owner", "analyst", "technician", "read_only")


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"), default=uuid.uuid4)


def _org_fk(nullable: bool = False) -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=nullable, index=True
    )


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# --------------------------------------------------------------------------- licensing / tenancy


class SubscriptionPlan(Base):
    __tablename__ = "subscription_plans"
    code: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    modules: Mapped[list] = mapped_column(JSONB, default=list)
    max_plants: Mapped[int | None] = mapped_column(Integer)
    api_requests_per_minute: Mapped[int] = mapped_column(Integer, default=120)


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    plan_code: Mapped[str] = mapped_column(ForeignKey("subscription_plans.code"), default="starter")
    country_code: Mapped[str | None] = mapped_column(String(2))
    default_currency: Mapped[str] = mapped_column(String(3), default="USD")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _created()


class OrgModuleOverride(Base):
    __tablename__ = "org_module_overrides"
    __table_args__ = (UniqueConstraint("organization_id", "module"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = _org_fk()
    module: Mapped[str] = mapped_column(String(50))
    enabled: Mapped[bool] = mapped_column(Boolean)


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = _org_fk()
    email: Mapped[str] = mapped_column(String(254), unique=True)
    full_name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(30))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _created()
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = _org_fk()
    name: Mapped[str] = mapped_column(String(120))
    prefix: Mapped[str] = mapped_column(String(16), unique=True)
    key_hash: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(30), default="read_only")
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    organization_id: Mapped[uuid.UUID | None] = _org_fk(nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(80))
    entity_type: Mapped[str | None] = mapped_column(String(60))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created()


# --------------------------------------------------------------------------- reference data


class EmissionFactor(Base):
    """Grid emission factor. organization_id NULL = shared reference value."""

    __tablename__ = "emission_factors"
    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID | None] = _org_fk(nullable=True)
    name: Mapped[str] = mapped_column(String(200))
    country_code: Mapped[str | None] = mapped_column(String(2))
    grid_region: Mapped[str | None] = mapped_column(String(120))
    kg_co2e_per_kwh: Mapped[Decimal] = mapped_column(Numeric(10, 6))
    method: Mapped[str] = mapped_column(String(80))  # e.g. grid average, operating margin, combined margin
    source: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    year: Mapped[int | None] = mapped_column(Integer)
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _created()


class DataSource(Base):
    __tablename__ = "data_sources"
    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = _org_fk()
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(40))  # csv_upload, pvgis, pvwatts, solargis, scada, inverter_api, synthetic, manual
    provider: Mapped[str | None] = mapped_column(String(120))
    url: Mapped[str | None] = mapped_column(Text)
    license_notes: Mapped[str | None] = mapped_column(Text)
    coverage_notes: Mapped[str | None] = mapped_column(Text)
    model_version: Mapped[str | None] = mapped_column(String(80))
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    meta: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = _created()


# --------------------------------------------------------------------------- plants & assets


class Plant(Base):
    __tablename__ = "plants"
    __table_args__ = (UniqueConstraint("organization_id", "code"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = _org_fk()
    name: Mapped[str] = mapped_column(String(200))
    code: Mapped[str] = mapped_column(String(40))
    technology: Mapped[str] = mapped_column(String(60), default="mono-Si")
    mounting: Mapped[str] = mapped_column(String(40), default="fixed")
    dc_capacity_kwp: Mapped[float] = mapped_column(Float)
    ac_capacity_kw: Mapped[float] = mapped_column(Float)
    commissioning_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(30), default="operational")
    country_code: Mapped[str | None] = mapped_column(String(2))
    region: Mapped[str | None] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(60), default="UTC")
    location = mapped_column(Geography(geometry_type="POINT", srid=4326), nullable=False)
    boundary = mapped_column(Geography(geometry_type="POLYGON", srid=4326), nullable=True)
    tilt_deg: Mapped[float] = mapped_column(Float, default=20)
    azimuth_deg: Mapped[float] = mapped_column(Float, default=0)  # PVGIS convention: 0 = equator-facing
    temp_coeff_pct_per_c: Mapped[float] = mapped_column(Float, default=-0.37)
    noct_c: Mapped[float] = mapped_column(Float, default=45)
    system_loss_pct: Mapped[float] = mapped_column(Float, default=14)
    annual_degradation_pct: Mapped[float] = mapped_column(Float, default=0.5)
    expected_pr: Mapped[float] = mapped_column(Float, default=0.80)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    emission_factor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("emission_factors.id", ondelete="SET NULL"))
    meta: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = _created()

    equipment: Mapped[list[Equipment]] = relationship(back_populates="plant", cascade="all, delete-orphan")


class Equipment(Base):
    __tablename__ = "equipment"
    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = _org_fk()
    plant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plants.id", ondelete="CASCADE"), index=True)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("equipment.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(40))  # inverter, string, combiner, meter, weather_station, transformer, tracker, sensor
    name: Mapped[str] = mapped_column(String(200))
    manufacturer: Mapped[str | None] = mapped_column(String(120))
    model: Mapped[str | None] = mapped_column(String(120))
    serial_number: Mapped[str | None] = mapped_column(String(120))
    rated_power_kw: Mapped[float | None] = mapped_column(Float)
    dc_capacity_kwp: Mapped[float | None] = mapped_column(Float)
    commissioning_date: Mapped[date | None] = mapped_column(Date)
    warranty_end: Mapped[date | None] = mapped_column(Date)
    warranty_terms: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="in_service")
    location = mapped_column(Geography(geometry_type="POINT", srid=4326), nullable=True)
    meta: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = _created()

    plant: Mapped[Plant] = relationship(back_populates="equipment")


class EnergyContract(Base):
    """How generated energy is valued: PPA, feed-in tariff, avoided retail cost or merchant price."""

    __tablename__ = "energy_contracts"
    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = _org_fk()
    plant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plants.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(30))  # ppa, feed_in_tariff, self_consumption, merchant
    counterparty: Mapped[str | None] = mapped_column(String(200))
    price_per_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 6))
    currency: Mapped[str] = mapped_column(String(3))
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    escalation_pct_per_year: Mapped[Decimal] = mapped_column(Numeric(6, 3), default=0)
    curtailment_compensated: Mapped[bool] = mapped_column(Boolean, default=False)
    export_cap_kw: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


class ExchangeRate(Base):
    __tablename__ = "exchange_rates"
    __table_args__ = (UniqueConstraint("organization_id", "base", "quote", "rate_date"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = _org_fk()
    base: Mapped[str] = mapped_column(String(3))
    quote: Mapped[str] = mapped_column(String(3))
    rate: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    rate_date: Mapped[date] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(200))


# --------------------------------------------------------------------------- time series & ingestion


class ImportBatch(Base):
    __tablename__ = "import_batches"
    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = _org_fk()
    plant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plants.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("data_sources.id", ondelete="SET NULL"))
    filename: Mapped[str | None] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(20))  # energy, weather, events
    status: Mapped[str] = mapped_column(String(20), default="completed")
    sha256: Mapped[str | None] = mapped_column(String(64))
    rows_total: Mapped[int] = mapped_column(Integer, default=0)
    rows_imported: Mapped[int] = mapped_column(Integer, default=0)
    rows_duplicate: Mapped[int] = mapped_column(Integer, default=0)
    rows_rejected: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list] = mapped_column(JSONB, default=list)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()


class EnergyReading(Base):
    """Metered AC energy per interval. equipment_id NULL = plant revenue/export meter."""

    __tablename__ = "energy_readings"
    __table_args__ = (
        UniqueConstraint("plant_id", "equipment_id", "ts", name="uq_energy_plant_equipment_ts", postgresql_nulls_not_distinct=True),
        Index("ix_energy_plant_ts", "plant_id", "ts"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    organization_id: Mapped[uuid.UUID] = _org_fk()
    plant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plants.id", ondelete="CASCADE"))
    equipment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("equipment.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # interval start, UTC
    interval_minutes: Mapped[int] = mapped_column(Integer, default=60)
    energy_kwh: Mapped[float] = mapped_column(Float)
    power_kw: Mapped[float | None] = mapped_column(Float)
    quality: Mapped[str] = mapped_column(String(20), default="ok")
    source_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("data_sources.id", ondelete="SET NULL"))
    batch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("import_batches.id", ondelete="SET NULL"))


class WeatherReading(Base):
    __tablename__ = "weather_readings"
    __table_args__ = (UniqueConstraint("plant_id", "ts", name="uq_weather_plant_ts"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    organization_id: Mapped[uuid.UUID] = _org_fk()
    plant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plants.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    poa_wm2: Mapped[float | None] = mapped_column(Float)
    ghi_wm2: Mapped[float | None] = mapped_column(Float)
    ambient_temp_c: Mapped[float | None] = mapped_column(Float)
    module_temp_c: Mapped[float | None] = mapped_column(Float)
    wind_ms: Mapped[float | None] = mapped_column(Float)
    source_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("data_sources.id", ondelete="SET NULL"))
    batch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("import_batches.id", ondelete="SET NULL"))


class OperationalEvent(Base):
    """Known outages, curtailment and maintenance windows — used to attribute and exclude losses."""

    __tablename__ = "operational_events"
    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = _org_fk()
    plant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plants.id", ondelete="CASCADE"), index=True)
    equipment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("equipment.id", ondelete="SET NULL"))
    category: Mapped[str] = mapped_column(String(40))  # outage, grid_outage, curtailment, planned_maintenance, inverter_fault, soiling, other
    start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(30), default="manual")
    created_at: Mapped[datetime] = _created()


class ModelRun(Base):
    __tablename__ = "model_runs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = _org_fk()
    plant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plants.id", ondelete="CASCADE"), index=True)
    model: Mapped[str] = mapped_column(String(60))
    model_version: Mapped[str] = mapped_column(String(30))
    parameters: Mapped[dict] = mapped_column(JSONB, default=dict)
    uncertainty_pct: Mapped[float] = mapped_column(Float, default=5.0)
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hours_modelled: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _created()


class ExpectedGeneration(Base):
    __tablename__ = "expected_generation"
    __table_args__ = (UniqueConstraint("plant_id", "ts", name="uq_expected_plant_ts"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    organization_id: Mapped[uuid.UUID] = _org_fk()
    plant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plants.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expected_kwh: Mapped[float] = mapped_column(Float)
    reference_yield_kwh: Mapped[float] = mapped_column(Float)  # kWp * POA kWh/m2 — denominator of PR
    cell_temp_c: Mapped[float | None] = mapped_column(Float)
    model_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("model_runs.id", ondelete="CASCADE"))


# --------------------------------------------------------------------------- anomalies & maintenance


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (UniqueConstraint("organization_id", "dedup_key"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = _org_fk()
    plant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plants.id", ondelete="CASCADE"), index=True)
    equipment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("equipment.id", ondelete="SET NULL"))
    rule_id: Mapped[str] = mapped_column(String(60))
    rule_version: Mapped[str] = mapped_column(String(20))
    category: Mapped[str] = mapped_column(String(40))
    severity: Mapped[str] = mapped_column(String(20))
    confidence: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="open")
    title: Mapped[str] = mapped_column(String(300))
    explanation: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    recommended_actions: Mapped[list] = mapped_column(JSONB, default=list)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    est_energy_loss_kwh: Mapped[float] = mapped_column(Float, default=0)
    est_financial_impact: Mapped[Decimal] = mapped_column(Numeric(16, 2), default=0)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    urgency_score: Mapped[float] = mapped_column(Float, default=0)
    priority_score: Mapped[float] = mapped_column(Float, default=0)
    dedup_key: Mapped[str] = mapped_column(String(200))
    finding_notes: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class WorkOrder(Base):
    __tablename__ = "work_orders"
    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = _org_fk()
    plant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plants.id", ondelete="CASCADE"), index=True)
    equipment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("equipment.id", ondelete="SET NULL"))
    alert_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("alerts.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(30), default="corrective")  # corrective, preventive, inspection, commissioning
    priority: Mapped[str] = mapped_column(String(20), default="medium")
    status: Mapped[str] = mapped_column(String(20), default="open")
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    contractor: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    due_date: Mapped[date | None] = mapped_column(Date)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    actual_cost: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    est_recovery_kwh_per_year: Mapped[float | None] = mapped_column(Float)
    est_recovery_value_per_year: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    realised: Mapped[dict | None] = mapped_column(JSONB)  # post-repair evaluation result
    created_at: Mapped[datetime] = _created()


class WorkOrderEvent(Base):
    __tablename__ = "work_order_events"
    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = _org_fk()
    work_order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("work_orders.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(30))  # created, assigned, status_change, note, inspection, diagnosis, repair, handover, verification
    message: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()


class AssetRecord(Base):
    """Service history: inspections, commissioning, contractor handovers, warranty claims, corrective actions."""

    __tablename__ = "asset_records"
    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = _org_fk()
    plant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plants.id", ondelete="CASCADE"), index=True)
    equipment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("equipment.id", ondelete="SET NULL"))
    work_order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("work_orders.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(300))
    performed_by: Mapped[str | None] = mapped_column(String(200))
    contractor: Mapped[str | None] = mapped_column(String(200))
    performed_at: Mapped[date] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _created()


# --------------------------------------------------------------------------- sustainability


class VerifiedReduction(Base):
    """Independently verified reductions / issued credits — kept separate from estimates."""

    __tablename__ = "verified_reductions"
    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = _org_fk()
    plant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plants.id", ondelete="CASCADE"), index=True)
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    tco2e: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    standard: Mapped[str] = mapped_column(String(120))  # e.g. Verra VCS, Gold Standard, I-REC (attributes, not reductions)
    verifier: Mapped[str] = mapped_column(String(200))
    certificate_ref: Mapped[str | None] = mapped_column(String(200))
    credits_issued: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


# --------------------------------------------------------------------------- integrations


class WebhookSubscription(Base):
    __tablename__ = "webhook_subscriptions"
    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = _org_fk()
    url: Mapped[str] = mapped_column(Text)
    events: Mapped[list] = mapped_column(JSONB, default=list)
    secret: Mapped[str] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _created()


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = _org_fk()
    subscription_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"))
    event: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict] = mapped_column(JSONB)
    status_code: Mapped[int | None] = mapped_column(Integer)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text)
    attempted_at: Mapped[datetime] = _created()


TENANT_TABLES = [
    "org_module_overrides", "users", "api_keys", "audit_logs", "data_sources", "plants", "equipment",
    "energy_contracts", "exchange_rates", "import_batches", "energy_readings", "weather_readings",
    "operational_events", "model_runs", "expected_generation", "alerts", "work_orders", "work_order_events",
    "asset_records", "verified_reductions", "webhook_subscriptions", "webhook_deliveries",
]
SHARED_OR_TENANT_TABLES = ["emission_factors"]  # NULL organization_id rows are readable by all tenants
