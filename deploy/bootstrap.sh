#!/usr/bin/env bash
# =============================================================================
# One-time bootstrap for a fresh Ubuntu 24.04 ARM instance (t4g.small).
#
#   sudo bash bootstrap.sh <public-ip-or-domain> <tfnsw-api-key>
#
# Installs PostgreSQL + PostGIS, Python, Caddy, the application, and three
# systemd units:
#   gtfsobs-web        Gunicorn behind Caddy
#   gtfsobs-harvester  the 60-second poller, restarted automatically
#   gtfsobs-analyse    timer, re-aggregates and prunes every 6 hours
#
# The harvester runs under systemd rather than cron because a missed poll cannot
# be backfilled: the realtime feed keeps no history. systemd restarts within
# seconds of a crash and survives reboots.
# =============================================================================
set -euo pipefail

HOST="${1:?usage: bootstrap.sh <public-ip-or-domain> <api-key>}"
API_KEY="${2:?usage: bootstrap.sh <public-ip-or-domain> <api-key>}"

APP_USER=gtfsobs
APP_DIR=/opt/gtfsobs
REPO=https://github.com/DeuGeo369/GTFS-Observations.git
DB_PASS=$(openssl rand -hex 24)
SECRET_KEY=$(openssl rand -hex 32)

# A bare IP cannot get a certificate, so Caddy serves plain HTTP in that case.
if [[ "$HOST" =~ ^[0-9.]+$ ]]; then
    CADDY_SITE=":80"
    echo ">>> no domain given - serving HTTP on port 80"
else
    CADDY_SITE="$HOST"
    echo ">>> domain given - Caddy will obtain a certificate automatically"
fi

# --------------------------------------------------------------------- swap
# 2 GB of RAM is not enough headroom for Postgres plus a 3.6M row bulk load.
if [ ! -f /swapfile ]; then
    echo ">>> adding 2GB swap"
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo ">>> packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    postgresql postgresql-contrib postgis postgresql-16-postgis-3 \
    python3-venv python3-dev build-essential \
    gdal-bin libgdal-dev binutils libproj-dev \
    libpq-dev git curl debian-keyring debian-archive-keyring apt-transport-https

echo ">>> caddy"
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | tee /etc/apt/sources.list.d/caddy-stable.list > /dev/null
apt-get update -qq && apt-get install -y -qq caddy

echo ">>> database"
# Created and owned by the application user from the start. Doing this as
# postgres and granting privileges later leaves the app unable to VACUUM or
# ALTER its own tables, which matters on an update-heavy table.
sudo -u postgres psql -qc "CREATE USER ${APP_USER} WITH PASSWORD '${DB_PASS}';" || true
sudo -u postgres psql -qc "CREATE DATABASE gtfsobs OWNER ${APP_USER};" || true
sudo -u postgres psql -qd gtfsobs -c "CREATE EXTENSION IF NOT EXISTS postgis;"
sudo -u postgres psql -qd gtfsobs -c "GRANT ALL ON SCHEMA public TO ${APP_USER};"
sudo -u postgres psql -qd gtfsobs -c "ALTER SCHEMA public OWNER TO ${APP_USER};"

# Defaults assume far more memory than this instance has.
sudo -u postgres psql -qc "ALTER SYSTEM SET shared_buffers = '384MB';"
sudo -u postgres psql -qc "ALTER SYSTEM SET work_mem = '16MB';"
sudo -u postgres psql -qc "ALTER SYSTEM SET maintenance_work_mem = '192MB';"
sudo -u postgres psql -qc "ALTER SYSTEM SET effective_cache_size = '1GB';"
sudo -u postgres psql -qc "ALTER SYSTEM SET random_page_cost = 1.1;"
sudo -u postgres psql -qc "ALTER SYSTEM SET max_connections = 40;"
sudo -u postgres psql -qc "ALTER DATABASE gtfsobs SET timezone TO 'UTC';"
systemctl restart postgresql

echo ">>> application"
id -u "$APP_USER" &>/dev/null || useradd -r -m -d "$APP_DIR" -s /bin/bash "$APP_USER"
if [ ! -d "$APP_DIR/.git" ]; then
    sudo -u "$APP_USER" git clone -q "$REPO" "$APP_DIR"
else
    sudo -u "$APP_USER" git -C "$APP_DIR" pull -q
fi

sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q \
    -r "$APP_DIR/requirements.txt" gunicorn whitenoise

cat > "$APP_DIR/.env" <<ENV
TFNSW_API_KEY=${API_KEY}
DJANGO_SECRET_KEY=${SECRET_KEY}
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=${HOST},localhost,127.0.0.1
GTFSOBS_DB_NAME=gtfsobs
GTFSOBS_DB_USER=${APP_USER}
GTFSOBS_DB_PASSWORD=${DB_PASS}
GTFSOBS_DB_HOST=localhost
GTFSOBS_DB_PORT=5432
ENV
chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"

echo ">>> migrations"
sudo -u "$APP_USER" bash -c "cd $APP_DIR && .venv/bin/python manage.py migrate --noinput"
sudo -u "$APP_USER" bash -c "cd $APP_DIR && .venv/bin/python manage.py collectstatic --noinput -v0"

# The observation table is update-heavy by design: each poll re-reports every
# active trip. Measured at 59% dead tuples after ~1500 polls with default
# settings, so autovacuum is made far more aggressive for this table alone.
sudo -u postgres psql -qd gtfsobs -c "ALTER TABLE reliability_observation SET (autovacuum_vacuum_scale_factor = 0.02, autovacuum_vacuum_cost_delay = 0);" || true

echo ">>> systemd units"

cat > /etc/systemd/system/gtfsobs-web.service <<UNIT
[Unit]
Description=GTFS Observatory web
After=network.target postgresql.service
Requires=postgresql.service

[Service]
User=${APP_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/.venv/bin/gunicorn observatory.wsgi:application \\
    --bind 127.0.0.1:8000 --workers 2 --timeout 120 --access-logfile -
Restart=always
RestartSec=5
MemoryMax=500M

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/gtfsobs-harvester.service <<UNIT
[Unit]
Description=GTFS-Realtime harvester
After=network.target postgresql.service
Requires=postgresql.service

[Service]
User=${APP_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/.venv/bin/python manage.py harvest --interval 60
Restart=always
RestartSec=10
StartLimitIntervalSec=0
MemoryMax=600M

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/gtfsobs-analyse.service <<UNIT
[Unit]
Description=Rebuild segment performance and prune raw observations

[Service]
Type=oneshot
User=${APP_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
# headways must follow aggregate: aggregate rebuilds the segment table and
# clears the headway fields, which headways then repopulates.
ExecStart=${APP_DIR}/.venv/bin/python manage.py aggregate
ExecStart=${APP_DIR}/.venv/bin/python manage.py headways
# prune refuses to run against stale aggregation, so this ordering is a safety
# property rather than a convenience.
ExecStart=${APP_DIR}/.venv/bin/python manage.py prune --days 14
Nice=10
UNIT

cat > /etc/systemd/system/gtfsobs-analyse.timer <<UNIT
[Unit]
Description=Re-aggregate every 6 hours

[Timer]
OnBootSec=20min
OnUnitActiveSec=6h
Persistent=true

[Install]
WantedBy=timers.target
UNIT

cat > /etc/caddy/Caddyfile <<CADDY
${CADDY_SITE} {
    encode gzip
    reverse_proxy 127.0.0.1:8000
}
CADDY

systemctl daemon-reload
systemctl enable --now gtfsobs-web gtfsobs-analyse.timer
systemctl restart caddy

echo
echo "============================================================"
echo " Web service up: http://${HOST}"
echo
echo " The harvester is NOT started yet - load the timetable first:"
echo
echo "   sudo -u gtfsobs bash -c 'cd ${APP_DIR} && \\"
echo "     curl -s -o gtfs.zip -H \"Authorization: apikey ${API_KEY}\" \\"
echo "     https://api.transport.nsw.gov.au/v1/gtfs/schedule/buses && \\"
echo "     .venv/bin/python manage.py load_gtfs gtfs.zip && rm gtfs.zip'"
echo
echo " Then:  sudo systemctl enable --now gtfsobs-harvester"
echo "        journalctl -u gtfsobs-harvester -f"
echo
echo " DB password is in ${APP_DIR}/.env (mode 600)"
echo "============================================================"
