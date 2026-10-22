"""YalaInsight API application."""
from __future__ import annotations

import logging
import time
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from .config import settings
from .core.logging import configure_logging, log, request_id_var
from .db import engine
from .modules.anomaly.router import router as anomaly_router
from .modules.auth.router import router as auth_router
from .modules.carbon.router import router as carbon_router
from .modules.dashboard.router import router as dashboard_router
from .modules.finance.router import router as finance_router
from .modules.forecasting.router import router as forecasting_router
from .modules.gis.router import router as gis_router
from .modules.ingestion.router import router as ingestion_router
from .modules.integrations.router import router as integrations_router
from .modules.maintenance.router import router as maintenance_router
from .modules.orgs.router import router as orgs_router
from .modules.performance.router import router as performance_router
from .modules.plants.router import router as plants_router
from .modules.reporting.router import router as reporting_router

API_VERSION = "1.1.0"
settings.validate()
configure_logging()
logger = logging.getLogger("yala.http")
metrics: Counter = Counter()

ROLE_SQL = text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")


def db_role_bypasses_rls() -> bool:
    with engine.connect() as c:
        return bool(c.execute(ROLE_SQL).scalar())


@asynccontextmanager
async def lifespan(_: FastAPI):  # noqa: ANN201
    try:
        unsafe = db_role_bypasses_rls()
    except Exception:  # noqa: BLE001 — DB not reachable yet; /ready reports it
        unsafe = None
    if unsafe:
        msg = ("The database role is a superuser or has BYPASSRLS, so row-level tenant isolation is NOT enforced. "
               "Connect with a dedicated non-superuser role (see docs/DEPLOYMENT.md).")
        if settings.is_production:
            raise RuntimeError(msg)
        logger.warning(msg)
    scheduler = None
    if settings.scheduler_enabled:
        from .jobs import Scheduler
        scheduler = Scheduler()
        scheduler.start()
    yield
    if scheduler:
        scheduler.stop()


app = FastAPI(
    lifespan=lifespan,
    title="YalaInsight API",
    version=API_VERSION,
    description="Solar plant intelligence, financial loss analytics and asset lifecycle platform. "
                "All endpoints are versioned under /api/v1. Authenticate with `Authorization: Bearer <token>` "
                "(from /api/v1/auth/login) or `X-API-Key` for machine clients.",
    openapi_url="/api/v1/openapi.json", docs_url="/api/docs", redoc_url="/api/redoc",
)

if settings.cors_origins:
    app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
                       allow_methods=["*"], allow_headers=["Authorization", "Content-Type", "X-API-Key"])


_tiles = urlsplit(settings.map_tile_url.replace("{s}", "a"))
_tile_src = f"{_tiles.scheme}://{_tiles.netloc}".replace("://a.", "://*.")
APP_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
           f"img-src 'self' data: {_tile_src}; connect-src 'self'; font-src 'self'; object-src 'none'; "
           "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
DOCS_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: https://fastapi.tiangolo.com; "
            "worker-src blob:; frame-ancestors 'none'")


@app.middleware("http")
async def request_context(request: Request, call_next):  # noqa: ANN001
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    request_id_var.set(rid)
    t0 = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("unhandled error")
        metrics["http_5xx"] += 1
        response = JSONResponse({"detail": "Internal server error", "request_id": rid}, status_code=500)
    ms = (time.perf_counter() - t0) * 1000
    metrics["http_requests"] += 1
    metrics[f"http_{response.status_code // 100}xx"] += 1
    response.headers["X-Request-ID"] = rid
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = DOCS_CSP if request.url.path.startswith(("/api/docs", "/api/redoc")) else APP_CSP
    if settings.is_production:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.url.path.startswith("/api/") and not request.url.path.startswith("/api/docs"):
        response.headers["Cache-Control"] = "no-store"
    if request.url.path not in ("/health", "/ready"):
        log(logger, logging.INFO, "request", method=request.method, path=request.url.path, status=response.status_code,
            duration_ms=round(ms, 1))
    return response


for r in (auth_router, orgs_router, plants_router, ingestion_router, performance_router, finance_router, anomaly_router,
          maintenance_router, carbon_router, forecasting_router, gis_router, reporting_router, integrations_router,
          dashboard_router):
    app.include_router(r, prefix="/api/v1")


@app.get("/health", tags=["ops"])
def health() -> dict:
    """Liveness: the process is up."""
    return {"status": "ok", "version": API_VERSION}


@app.get("/ready", tags=["ops"])
def ready() -> JSONResponse:
    """Readiness: database reachable and migrated."""
    try:
        with engine.connect() as c:
            rev = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
            postgis = c.execute(text("SELECT postgis_lib_version()")).scalar()
            unsafe = bool(c.execute(ROLE_SQL).scalar())
        from .core.ratelimit import limiter
        body = {"status": "ready", "db_revision": rev, "postgis": postgis, "tenant_isolation_enforced": not unsafe,
                "rate_limiter": limiter.backend, "scheduler": settings.scheduler_enabled}
        if getattr(limiter, "ping", None) and not limiter.ping():
            body["rate_limiter"] = "redis-unreachable"
        return JSONResponse(body)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"status": "unavailable", "error": type(exc).__name__}, status_code=503)


@app.get("/metrics", tags=["ops"], response_class=PlainTextResponse)
def prometheus_metrics() -> str:
    lines = [f"# TYPE yala_{k}_total counter\nyala_{k}_total {v}" for k, v in sorted(metrics.items())]
    pool = engine.pool
    lines.append(f"# TYPE yala_db_pool_checked_out gauge\nyala_db_pool_checked_out {pool.checkedout()}")
    return "\n".join(lines) + "\n"


FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
if settings.serve_frontend and FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(FRONTEND / "index.html")
