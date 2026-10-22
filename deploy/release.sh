#!/bin/sh
# Release step: run once per deploy, before new instances take traffic (Render preDeployCommand).
set -eu
cd "$(dirname "$0")/../backend"
alembic upgrade head
python -m app.bootstrap
