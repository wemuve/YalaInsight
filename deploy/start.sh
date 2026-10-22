#!/bin/sh
# Container entrypoint: migrate + bootstrap (serialised across instances), then serve.
set -eu
cd "$(dirname "$0")/../backend"
python -m app.release
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers "${WEB_CONCURRENCY:-2}" \
  --proxy-headers --forwarded-allow-ips '*'
