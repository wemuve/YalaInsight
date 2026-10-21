"""Downloadable plant reports (PDF and CSV): performance, financial loss, maintenance, carbon.

Every report states its period, data sources, model versions, figure basis (measured / modelled /
simulated / verified) and key assumptions so that figures are traceable.
"""
from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.deps import Principal, require, require_module
from ...db import get_db
from ...models import Alert, AssetRecord, Organization, WorkOrder
from ..carbon.router import plant_carbon
from ..common import get_plant, resolve_period
from ..finance.service import AnalysisError, AnalysisRequest, analyse
from ..performance import engine as pe
from ..performance import service as perf

router = APIRouter(tags=["reports"], dependencies=[Depends(require_module("reporting"))])
ReportType = Literal["performance", "financial", "maintenance", "carbon"]


def _f(v, nd=1, pct=False) -> str:  # noqa: ANN001
    if v is None:
        return "—"
    return f"{v * 100:.{nd}f}%" if pct else f"{v:,.{nd}f}"


def build_tables(db: Session, plant, kind: str, s: datetime, e: datetime) -> tuple[list[str], list[tuple[str, list[list]]]]:  # noqa: ANN001
    """Return (notes, [(section title, rows incl. header)]) — shared by PDF and CSV renderers."""
    notes: list[str] = []
    sections: list[tuple[str, list[list]]] = []
    if kind == "performance":
        iv = perf.load_intervals(db, plant, s, e)
        sm = pe.summarize(iv, plant.dc_capacity_kwp)
        run = perf.latest_model_run(db, plant.id)
        sections.append(("Summary", [["Metric", "Value", "Basis"],
                                     ["Actual generation (kWh)", _f(sm["actual_kwh"]), "measured"],
                                     ["Expected generation (kWh)", _f(sm["expected_kwh"]), "modelled"],
                                     ["Variance", _f(sm["variance_pct"], 1) + " %" if sm["variance_pct"] is not None else "—", "modelled"],
                                     ["Performance ratio", _f(sm["performance_ratio"], 1, True), "measured/irradiance"],
                                     ["Performance index", _f(sm["performance_index"], 1, True), "modelled"],
                                     ["Availability (time-based)", _f(sm["availability"], 1, True), "measured"],
                                     ["Specific yield (kWh/kWp)", _f(sm["specific_yield_kwh_per_kwp"]), "measured"],
                                     ["Data coverage", _f(sm["data_coverage"], 1, True), "—"]]))
        rows = [["Day", "Actual kWh", "Expected kWh", "Variance %", "PR", "Availability"]]
        for r in pe.aggregate(iv, "day", plant.dc_capacity_kwp):
            rows.append([r["period"], _f(r["actual_kwh"]), _f(r["expected_kwh"]), _f(r["variance_pct"]),
                         _f(r["performance_ratio"], 1, True), _f(r["availability"], 0, True)])
        sections.append(("Daily performance", rows))
        if run:
            notes.append(f"Expected generation: model {run.model} v{run.model_version}, ±{run.uncertainty_pct}% uncertainty.")
    elif kind == "financial":
        try:
            a = analyse(db, plant, AnalysisRequest(start=s, end=e))
        except AnalysisError as exc:
            raise HTTPException(422, str(exc)) from None
        cur = a["tariff"]["currency"]
        sc = a["scenarios"]
        sections.append(("Scenario summary", [["Figure", "Low", "Base", "High", "Basis"],
            ["Shortfall (kWh)", *[_f(sc[k]["energy"]["shortfall_kwh"]["value"]) for k in ("low", "base", "high")], "modelled"],
            [f"Gross lost energy value ({cur})", *[_f(sc[k]["gross_lost_energy_value"]["value"], 2) for k in ("low", "base", "high")], "modelled"],
            [f"Lost revenue ({cur})", *[_f(sc[k]["lost_revenue"]["value"], 2) for k in ("low", "base", "high")], "modelled"],
            [f"Annual recoverable value ({cur})", *[_f(sc[k]["recovery"]["annual_recovered_value"]["value"], 2) for k in ("low", "base", "high")], "simulated"],
            [f"Net benefit over horizon ({cur})", *[_f(sc[k]["recovery"]["net_benefit"]["value"], 2) for k in ("low", "base", "high")], "simulated"],
            ["Payback (years)", *[_f(sc[k]["recovery"]["payback_years"], 2) for k in ("low", "base", "high")], "simulated"]]))
        rows = [["Loss category", "Lost kWh", f"Gross value ({cur})", f"Lost revenue ({cur})", "Addressable"]]
        for cat, v in sc["base"]["losses"].items():
            rows.append([cat, _f(v["lost_production_kwh"]["value"]), _f(v["gross_value"]["value"], 2),
                         _f(v["lost_revenue"]["value"], 2), "yes" if v["addressable_by_maintenance"] else "no"])
        sections.append(("Loss attribution (base scenario)", rows))
        notes += a["assumptions"]
        notes.append(f"Tariff: {a['tariff']['kind']} {a['tariff']['price_per_kwh']:.4f} {cur}/kWh on {a['tariff']['price_date']}. "
                     + a["tariff"]["note"])
        notes.append(f"Data coverage: {a['data_quality']['coverage']:.1%}.")
    elif kind == "maintenance":
        wos = db.scalars(select(WorkOrder).where(WorkOrder.plant_id == plant.id).order_by(WorkOrder.created_at.desc())).all()
        rows = [["Title", "Status", "Priority", "Due", "Est. cost", "Actual cost", "Est. kWh/yr", "Realised kWh/yr"]]
        for w in wos:
            rows.append([w.title[:60], w.status, w.priority, w.due_date.isoformat() if w.due_date else "—",
                         _f(float(w.estimated_cost), 0) if w.estimated_cost is not None else "—",
                         _f(float(w.actual_cost), 0) if w.actual_cost is not None else "—",
                         _f(w.est_recovery_kwh_per_year, 0), _f((w.realised or {}).get("realised_recovery_kwh_per_year"), 0)])
        sections.append(("Work orders", rows))
        alerts = db.scalars(select(Alert).where(Alert.plant_id == plant.id, Alert.window_start >= s)
                            .order_by(Alert.priority_score.desc())).all()
        sections.append(("Alerts in period", [["Alert", "Severity", "Status", "Est. kWh", "Est. impact"]] +
                         [[a.title[:70], a.severity, a.status, _f(a.est_energy_loss_kwh, 0), f"{_f(float(a.est_financial_impact), 0)} {a.currency}"]
                          for a in alerts]))
        recs = db.scalars(select(AssetRecord).where(AssetRecord.plant_id == plant.id).order_by(AssetRecord.performed_at.desc())).all()
        sections.append(("Service records", [["Date", "Type", "Title", "By"]] +
                         [[r.performed_at.isoformat(), r.kind, r.title[:60], r.contractor or r.performed_by or "—"] for r in recs]))
        notes.append("Alert status 'confirmed' requires a recorded inspection finding; other alerts are unconfirmed anomaly flags.")
    elif kind == "carbon":
        c = plant_carbon(db, plant, s, e)
        est = c["estimate"]
        if est:
            sections.append(("Estimated avoided emissions", [["Item", "Value"],
                             ["Metered generation (kWh)", _f(est["generation_kwh"])],
                             ["Emission factor (kgCO2e/kWh)", f"{est['factor']['kg_co2e_per_kwh']:.4f}"],
                             ["Factor", f"{est['factor']['name']} ({est['factor']['method']}, {est['factor']['year']})"],
                             ["Factor source", est["factor"]["source"][:90]],
                             ["Estimated avoided emissions (tCO2e)", _f(est["estimated_avoided_tco2e"], 2)]]))
            notes.append(est["methodology"])
            notes += est["warnings"]
        sections.append(("Independently verified reductions", [["Period", "tCO2e", "Standard", "Verifier", "Certificate"]] +
                         [[f"{v['period_start']} – {v['period_end']}", _f(v["tco2e"], 2), v["standard"], v["verifier"],
                           v["certificate_ref"] or "—"] for v in c["verified_reductions"]]))
        notes.append(c["note"])
    return notes, sections


def render_pdf(title: str, org: str, plant, s: datetime, e: datetime, notes: list[str], sections) -> bytes:  # noqa: ANN001
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=15 * mm,
                            title=title, author="YalaInsight")
    st = getSampleStyleSheet()
    small = st["BodyText"].clone("small", fontSize=8, leading=10)
    story = [Paragraph(f"YalaInsight — {title}", st["Title"]),
             Paragraph(f"{org} · {plant.name} ({plant.code}) · {plant.dc_capacity_kwp:,.0f} kWp", st["Normal"]),
             Paragraph(f"Period: {s:%Y-%m-%d %H:%M} – {e:%Y-%m-%d %H:%M} UTC · generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC", small),
             Spacer(1, 6 * mm)]
    for heading, rows in sections:
        story.append(Paragraph(heading, st["Heading2"]))
        if len(rows) <= 1:
            story.append(Paragraph("No records.", small))
            continue
        t = Table([[Paragraph(str(c), small) for c in r] for r in rows[:400]], repeatRows=1)
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f3d3e")),
                               ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                               ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#c8d0d0")),
                               ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f6f6")])]))
        story += [t, Spacer(1, 4 * mm)]
    if notes:
        story.append(Paragraph("Methodology, assumptions and limitations", st["Heading2"]))
        story += [Paragraph("• " + n, small) for n in notes]
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph("Figure basis: measured = metered data; modelled = derived from the expected-generation model; "
                           "simulated = depends on scenario assumptions; verified = confirmed by inspection or third party. "
                           "Anomaly flags are not confirmed faults.", small))
    doc.build(story)
    return buf.getvalue()


@router.get("/plants/{plant_id}/reports/{kind}.{fmt}")
def report(plant_id: UUID, kind: ReportType, fmt: Literal["pdf", "csv"], start: datetime | None = None,
           end: datetime | None = None, p: Principal = Depends(require("reports:read")), db: Session = Depends(get_db)) -> Response:
    plant = get_plant(db, plant_id, p)
    s, e = resolve_period(db, [plant.id], start, end, default_days=30)
    notes, sections = build_tables(db, plant, kind, s, e)
    fname = f"{plant.code}_{kind}_{s:%Y%m%d}-{e:%Y%m%d}.{fmt}"
    if fmt == "pdf":
        org = db.get(Organization, p.org_id)
        body = render_pdf(f"{kind.capitalize()} report", org.name, plant, s, e, notes, sections)
        return Response(body, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{fname}"'})
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"YalaInsight {kind} report", plant.name, plant.code, s.isoformat(), e.isoformat()])
    for heading, rows in sections:
        w.writerow([])
        w.writerow([f"## {heading}"])
        w.writerows(rows)
    w.writerow([])
    for n in notes:
        w.writerow(["# " + n])
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{fname}"'})
