#!/bin/sh
# Creates the application role. It must NOT be a superuser: superusers bypass row-level security.
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE ROLE yala_app LOGIN PASSWORD '${YALA_APP_PASSWORD}' NOSUPERUSER NOBYPASSRLS NOCREATEROLE;
GRANT ALL ON DATABASE ${POSTGRES_DB} TO yala_app;
GRANT ALL ON SCHEMA public TO yala_app;
ALTER DATABASE ${POSTGRES_DB} OWNER TO yala_app;
SQL
