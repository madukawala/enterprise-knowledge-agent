#!/bin/sh
# Runs once, the first time the Postgres volume is created.
# Keycloak gets its own database and user, so it cannot read or change the
# application's tables (and the application cannot read Keycloak's).
#
# To run it again after changing it: make down-volumes && make up
set -eu

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" \
     --dbname "$POSTGRES_DB" \
     -v kc_password="$KEYCLOAK_DB_PASSWORD" <<'SQL'
CREATE ROLE keycloak LOGIN PASSWORD :'kc_password';
CREATE DATABASE keycloak OWNER keycloak;
REVOKE ALL ON DATABASE keycloak FROM PUBLIC;
SQL
