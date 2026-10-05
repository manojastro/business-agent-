#!/bin/sh
# Runs once, as the bootstrap superuser, when the Postgres volume is first created.
# Creates three least-privilege roles and two separate databases:
#   metric_app        application state (owned by APP_DB_USER)
#   metric_analytics  synthetic source data (owned by ANALYTICS_OWNER_USER)
# ANALYTICS_READER_USER is a non-owner, non-superuser, NOBYPASSRLS role that is
# granted SELECT on approved views only (grants are applied by the analytics migration).
set -eu

: "${APP_DB_USER:?}" "${APP_DB_PASSWORD:?}"
: "${ANALYTICS_OWNER_USER:?}" "${ANALYTICS_OWNER_PASSWORD:?}"
: "${ANALYTICS_READER_USER:?}" "${ANALYTICS_READER_PASSWORD:?}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  -v app_user="$APP_DB_USER" -v app_pw="$APP_DB_PASSWORD" \
  -v owner_user="$ANALYTICS_OWNER_USER" -v owner_pw="$ANALYTICS_OWNER_PASSWORD" \
  -v reader_user="$ANALYTICS_READER_USER" -v reader_pw="$ANALYTICS_READER_PASSWORD" <<'SQL'
CREATE ROLE :"app_user" LOGIN PASSWORD :'app_pw' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE :"owner_user" LOGIN PASSWORD :'owner_pw' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE :"reader_user" LOGIN PASSWORD :'reader_pw' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT;
ALTER ROLE :"reader_user" SET default_transaction_read_only = on;
ALTER ROLE :"reader_user" SET statement_timeout = '5s';

CREATE DATABASE metric_app OWNER :"app_user";
CREATE DATABASE metric_analytics OWNER :"owner_user";
REVOKE ALL ON DATABASE metric_app FROM PUBLIC;
REVOKE ALL ON DATABASE metric_analytics FROM PUBLIC;
GRANT CONNECT ON DATABASE metric_app TO :"app_user";
GRANT CONNECT ON DATABASE metric_analytics TO :"owner_user", :"reader_user";
SQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname metric_analytics <<'SQL'
REVOKE ALL ON SCHEMA public FROM PUBLIC;
SQL
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname metric_app <<'SQL'
REVOKE ALL ON SCHEMA public FROM PUBLIC;
SQL
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname metric_app -v app_user="$APP_DB_USER" <<'SQL'
ALTER SCHEMA public OWNER TO :"app_user";
SQL
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname metric_analytics -v owner_user="$ANALYTICS_OWNER_USER" <<'SQL'
ALTER SCHEMA public OWNER TO :"owner_user";
SQL
