"""Runtime configuration, read from environment variables.

Secrets (JWT signing key, database password, third-party API keys) must be
supplied through the environment or a secret manager — never committed.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in {"1", "true", "yes", "on"}


def _db_url(url: str) -> str:
    """Accept provider-style URLs (postgres://, postgresql://) and select the psycopg 3 driver."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


@dataclass(frozen=True)
class Settings:
    env: str = field(default_factory=lambda: os.getenv("YALA_ENV", "development"))
    database_url: str = field(default_factory=lambda: _db_url(
        os.getenv("YALA_DATABASE_URL", "postgresql+psycopg://yala:yala@localhost:5432/yalainsight")))
    jwt_secret: str = field(default_factory=lambda: os.getenv("YALA_JWT_SECRET", "dev-insecure-secret-change-me-0123456789"))
    jwt_ttl_minutes: int = field(default_factory=lambda: int(os.getenv("YALA_JWT_TTL_MINUTES", "480")))
    rate_limit_per_minute: int = field(default_factory=lambda: int(os.getenv("YALA_RATE_LIMIT_PER_MINUTE", "600")))
    login_rate_limit_per_minute: int = field(default_factory=lambda: int(os.getenv("YALA_LOGIN_RATE_LIMIT", "10")))
    cors_origins: str = field(default_factory=lambda: os.getenv("YALA_CORS_ORIGINS", ""))
    serve_frontend: bool = field(default_factory=lambda: _bool("YALA_SERVE_FRONTEND", True))
    pvwatts_api_key: str | None = field(default_factory=lambda: os.getenv("NREL_API_KEY"))
    external_http_timeout: float = field(default_factory=lambda: float(os.getenv("YALA_HTTP_TIMEOUT", "20")))
    max_upload_mb: int = field(default_factory=lambda: int(os.getenv("YALA_MAX_UPLOAD_MB", "25")))
    # Key for encrypting secrets stored in the database (2FA seeds). Required in production.
    data_key: str = field(default_factory=lambda: os.getenv("YALA_DATA_KEY", ""))
    redis_url: str | None = field(default_factory=lambda: os.getenv("YALA_REDIS_URL") or None)
    login_max_failures: int = field(default_factory=lambda: int(os.getenv("YALA_LOGIN_MAX_FAILURES", "5")))
    lockout_minutes: int = field(default_factory=lambda: int(os.getenv("YALA_LOCKOUT_MINUTES", "15")))
    scheduler_enabled: bool = field(default_factory=lambda: _bool("YALA_SCHEDULER_ENABLED", False))
    detection_interval_minutes: int = field(default_factory=lambda: int(os.getenv("YALA_DETECTION_INTERVAL_MINUTES", "60")))
    show_demo_login: bool = field(default_factory=lambda: _bool("YALA_SHOW_DEMO_LOGIN", False))
    map_tile_url: str = field(default_factory=lambda: os.getenv(
        "YALA_MAP_TILE_URL", "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"))
    map_tile_attribution: str = field(default_factory=lambda: os.getenv(
        "YALA_MAP_TILE_ATTRIBUTION", "© OpenStreetMap contributors"))

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    def validate(self) -> None:
        if self.is_production:
            if self.jwt_secret.startswith("dev-") or len(self.jwt_secret) < 32:
                raise RuntimeError("YALA_JWT_SECRET must be set to a strong secret (>=32 chars) in production")
            if len(self.data_key) < 32:
                raise RuntimeError("YALA_DATA_KEY must be set (>=32 chars) in production")


settings = Settings()
