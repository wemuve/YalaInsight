"""Release step: bring the database schema to the latest version and run the first-run bootstrap.

Runs at every container start (deploy/start.sh) and is safe to run concurrently: a PostgreSQL
advisory lock serialises instances, migrations are idempotent, and the bootstrap only creates what
is missing. This does not depend on the host offering a separate pre-deploy hook.
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from .db import engine

LOCK_KEY = 7301_2026
log = logging.getLogger("yala.release")


def _wait_for_database(timeout_s: int = 90) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            with engine.connect() as c:
                c.execute(text("SELECT 1"))
            return
        except OperationalError as exc:
            if time.monotonic() > deadline:
                raise SystemExit(f"release: database not reachable after {timeout_s}s: {exc}") from None
            print("release: waiting for database…", flush=True)
            time.sleep(3)


def main() -> None:
    _wait_for_database()
    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    with engine.connect() as lock:
        lock.execute(text("SELECT pg_advisory_lock(:k)"), {"k": LOCK_KEY})
        try:
            command.upgrade(cfg, "head")
            from . import bootstrap
            bootstrap.main()
        finally:
            lock.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
            lock.commit()
    print("release: database is up to date", flush=True)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — print a clear reason in the host's deploy log
        print(f"release: FAILED — {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise
