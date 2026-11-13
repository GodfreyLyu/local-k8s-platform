#!/usr/bin/env bash
# Purpose: Initialize fresh PostgreSQL data once and verify mounted credentials on every restart.
set +x
set -Eeuo pipefail
umask 077
export PGDATA=/var/lib/postgresql/data/pgdata
readonly marker="$PGDATA/.local-k8s-platform-initialized"
readonly ready_file=/var/run/postgresql/platform-startup-validated
temporary_server=false

# Inputs: Temporary-server flag and runtime files. Outputs: Stopped bootstrap server and removed pgpass files.
# Purpose: Remove startup-only sensitive material. Dependencies: pg_ctl and rm.
cleanup() {
  if [[ "$temporary_server" == true ]]; then
    pg_ctl -D "$PGDATA" -m fast -w -t 30 stop >/dev/null 2>&1 || true
  fi
  rm -f /var/run/postgresql/admin.pgpass /var/run/postgresql/app.pgpass /var/run/postgresql/metrics.pgpass >/dev/null 2>&1 || true
}
# Inputs: Startup failure. Outputs: Exit status 1 and a fixed diagnostic.
# Purpose: Stop safely without resetting data. Dependencies: cleanup and printf.
failed() {
  cleanup
  printf '%s\n' 'PostgreSQL startup refused: verify data ownership, initialization and credential prerequisites; no data was reset.' >&2
  exit 1
}
trap failed ERR
trap 'cleanup; exit 143' TERM INT

[[ "$(id -u)" == 999 ]] || failed
rm -f "$ready_file" || failed
[[ "$(cat /secrets/app/username 2>/dev/null)" == "$APP_USERNAME" ]] || failed
[[ "$(cat /secrets/metrics/username 2>/dev/null)" == postgres_exporter ]] || failed
for password in /secrets/admin/password /secrets/app/password /secrets/metrics/password; do
  [[ -s "$password" && -r "$password" ]] || failed
done
for name in tls.crt tls.key ca.crt; do
  [[ -s "/tls-source/$name" ]] || failed
  cp "/tls-source/$name" "/run/tls/$name" || failed
  chmod 0600 "/run/tls/$name" || failed
done

fresh=false
if [[ -f "$PGDATA/PG_VERSION" ]]; then
  [[ "$(cat "$PGDATA/PG_VERSION")" == 17 ]] || failed
  [[ -f "$marker" && "$(cat "$marker")" == local-k8s-platform-v1 ]] || failed
else
  if [[ -d "$PGDATA" && -n "$(find "$PGDATA" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    failed
  fi
  mkdir -p "$PGDATA" || failed
  chmod 0700 "$PGDATA" || failed
  # Any interrupted init leaves a nonempty directory or an unsealed PG_VERSION;
  # later starts refuse it rather than deleting files or silently initializing again.
  initdb -D "$PGDATA" --username=postgres --pwfile=/secrets/admin/password --auth-local=peer --auth-host=scram-sha-256 --encoding=UTF8 --data-checksums >/dev/null 2>&1 || failed
  fresh=true
fi

# The temporary server listens only inside this Pod. Initialization uses Unix
# peer auth, then file credentials are actually checked over verified TLS.
# No Service/Pod-IP listener or readiness marker exists until checks pass.
temporary_server=true
pg_ctl -D "$PGDATA" -w -t 60 -l /var/run/postgresql/bootstrap-server.log -o "-c config_file=/config/postgresql.conf -c hba_file=/config/pg_hba.conf -c listen_addresses=localhost" start >/dev/null 2>&1 || failed
if [[ "$fresh" == true ]]; then
  psql --no-psqlrc --no-password --quiet --host=/var/run/postgresql --username=postgres --dbname=postgres --set=ON_ERROR_STOP=1 --set=app_user="$APP_USERNAME" --set=app_database="$APP_DATABASE" --file=/config/bootstrap.sql >/dev/null 2>&1 || failed
fi

# Inputs: Mounted password path and private pgpass destination. Outputs: A mode-0600 pgpass file.
# Purpose: Transfer prepared hexadecimal credentials without CLI exposure. Dependencies: cat, printf, chmod.
password_file() {
  local password
  password="$(cat "$1" 2>/dev/null)" || failed
  # Prepared credentials use random hexadecimal passwords; fail closed on a
  # malformed/multiline file rather than emitting an invalid pgpass record.
  [[ "$password" =~ ^[0-9a-f]{64}$ ]] || failed
  printf '*:*:*:*:%s\n' "$password" >"$2" || failed
  chmod 0600 "$2" || failed
  unset password
}
# Inputs: Identity directory, SQL user, and database. Outputs: Success or failed startup.
# Purpose: Check current database privileges using verified TLS. Dependencies: password_file, psql, SQL assets.
readonly_check() {
  local identity="$1" user="$2" database="$3" pass_file result
  pass_file="/var/run/postgresql/$identity.pgpass"
  password_file "/secrets/$identity/password" "$pass_file"
  result="$(PGPASSFILE="$pass_file" PGCONNECT_TIMEOUT=5 PGOPTIONS='-c default_transaction_read_only=on' psql --no-psqlrc --no-password --quiet --tuples-only --no-align --dbname="host=localhost port=5432 dbname=$database user=$user sslmode=verify-full sslrootcert=/run/tls/ca.crt" --set=ON_ERROR_STOP=1 --set=app_user="$APP_USERNAME" --set=app_database="$APP_DATABASE" --file="/config/check-$identity.sql" 2>/dev/null)" || failed
  rm -f "$pass_file" || failed
  [[ "$result" == t ]] || failed
}
# Existing initialized data is never reset or given new role passwords on restart.
# Credential rotation needs an explicit, reviewed database operation beforehand.
readonly_check admin postgres postgres
readonly_check app "$APP_USERNAME" "$APP_DATABASE"
readonly_check metrics postgres_exporter "$APP_DATABASE"
pg_ctl -D "$PGDATA" -m fast -w -t 30 stop >/dev/null 2>&1 || failed
temporary_server=false
if [[ "$fresh" == true ]]; then
  printf '%s\n' local-k8s-platform-v1 >"$marker" || failed
fi
touch "$ready_file" || failed
trap - ERR TERM INT
exec postgres -D "$PGDATA" -c config_file=/config/postgresql.conf -c hba_file=/config/pg_hba.conf
