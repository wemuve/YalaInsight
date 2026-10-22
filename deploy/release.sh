#!/bin/sh
# Release step on its own (optional — deploy/start.sh already runs it at every start).
set -eu
cd "$(dirname "$0")/../backend"
python -m app.release
