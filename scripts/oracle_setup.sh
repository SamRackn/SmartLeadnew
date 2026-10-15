#!/usr/bin/env bash
# One-shot setup for an Ubuntu 24.04 VM (Oracle Always-Free AMD micro, 1 GB).
# Run ON THE VM as the default user, after copying the app tarball over:
#
#   scp -i ~/.ssh/oracle_mazz mazz.tar.gz scripts/oracle_setup.sh ubuntu@<ip>:~
#   ssh -i ~/.ssh/oracle_mazz ubuntu@<ip>
#   bash oracle_setup.sh
#
# Safe to re-run: secrets are generated once and never rotated (rotating
# UNSUBSCRIBE_SECRET kills every unsubscribe link already in someone's inbox;
# losing OAUTH_TOKEN_KEY forces every mailbox to reconnect). Sending stays OFF.
set -euo pipefail

TARBALL="${TARBALL:-$HOME/mazz.tar.gz}"
APP_DIR=/opt/mazz
ENV_FILE=/etc/mazz/env
APP_HOST="${APP_HOST:-app.getsearchsitemarketing.com}"
UNSUB_HOST="${UNSUB_HOST:-outreach.getsearchsitemarketing.com}"

[ -f "$TARBALL" ] || { echo "error: $TARBALL not found (see header)" >&2; exit 1; }
[ "$(id -u)" -ne 0 ] || { echo "error: run as the normal user, not root" >&2; exit 1; }

echo "== packages"
# Caddy is not in Ubuntu 22.04's repos; add the official one.
sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  postgresql curl caddy iptables-persistent unattended-upgrades
echo 'APT::Periodic::Update-Package-Lists "1"; APT::Periodic::Unattended-Upgrade "1";' |
  sudo tee /etc/apt/apt.conf.d/20auto-upgrades >/dev/null

echo "== swap (1 GB RAM is tight)"
if ! swapon --show | grep -q /swapfile; then
  sudo fallocate -l 2G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

echo "== host firewall: Oracle images block 80/443 in iptables as well as in the VCN"
for p in 80 443; do
  sudo iptables -C INPUT -p tcp --dport "$p" -j ACCEPT 2>/dev/null ||
    sudo iptables -I INPUT -p tcp --dport "$p" -j ACCEPT
done
sudo netfilter-persistent save

echo "== postgres (small footprint)"
PGCONF=$(ls -d /etc/postgresql/*/main)/conf.d
sudo tee "$PGCONF/mazz.conf" >/dev/null <<'EOF'
shared_buffers = 64MB
work_mem = 4MB
maintenance_work_mem = 32MB
max_connections = 30
effective_cache_size = 256MB
EOF
sudo systemctl restart postgresql

echo "== app user and code"
id mazz >/dev/null 2>&1 || sudo useradd --system --create-home --home-dir /home/mazz --shell /usr/sbin/nologin mazz
sudo mkdir -p "$APP_DIR" /etc/mazz
sudo tar -xzf "$TARBALL" -C "$APP_DIR"
sudo chown -R mazz:mazz "$APP_DIR"
# Ubuntu 22.04 ships Python 3.10; the app needs 3.12. uv fetches 3.12 itself.
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh
fi
# Run from the app dir: uv reads config from the cwd, and mazz cannot read ~ubuntu.
cd "$APP_DIR"
sudo -u mazz uv venv --allow-existing --python 3.12 "$APP_DIR/.venv"
sudo -u mazz uv pip install --python "$APP_DIR/.venv/bin/python" -e "$APP_DIR[google,excel]"

echo "== env file (created once, never overwritten)"
if [ ! -f "$ENV_FILE" ]; then
  gen() { sudo -u mazz "$APP_DIR/.venv/bin/python" -c "import secrets;print(secrets.token_urlsafe(48))"; }
  FERNET=$(sudo -u mazz "$APP_DIR/.venv/bin/python" -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
  # Peer auth over the local socket: OS user mazz is DB role mazz, no password to keep.
  sudo -u postgres psql -c "CREATE ROLE mazz LOGIN" 2>/dev/null || true
  sudo -u postgres psql -lqt | cut -d'|' -f1 | grep -qw mazz || sudo -u postgres createdb -O mazz mazz
  sudo tee "$ENV_FILE" >/dev/null <<EOF
DATABASE_URL=postgresql+psycopg://mazz@/mazz?host=/var/run/postgresql
SESSION_SECRET=$(gen)
UNSUBSCRIBE_SECRET=$(gen)
OAUTH_TOKEN_KEY=${FERNET}
APP_BASE_URL=https://${APP_HOST}
UNSUBSCRIBE_BASE_URL=https://${UNSUB_HOST}
UNSUBSCRIBE_HOST=${UNSUB_HOST}
SEND_ENABLED=false
EMAIL_SENDER=stub
DRAFT_COMPOSER=template
GOOGLE_OAUTH_CLIENT_ID=
GOOGLE_OAUTH_CLIENT_SECRET=
EOF
  sudo chown root:mazz "$ENV_FILE"
  sudo chmod 640 "$ENV_FILE"
fi

echo "== migrations"
sudo -u mazz bash -c "set -a; . $ENV_FILE; set +a; cd $APP_DIR && .venv/bin/alembic upgrade head"

echo "== services"
sudo tee /etc/systemd/system/mazz-web.service >/dev/null <<EOF
[Unit]
Description=mazz web
After=network.target postgresql.service
[Service]
User=mazz
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/uvicorn mazz.web.app:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips 127.0.0.1
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
sudo tee /etc/systemd/system/mazz-worker.service >/dev/null <<EOF
[Unit]
Description=mazz worker (one only: rotation and caps are counted, not locked)
After=network.target postgresql.service
[Service]
User=mazz
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/python -m mazz.ops worker
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF

echo "== caddy (automatic https once DNS points here)"
sudo tee /etc/caddy/Caddyfile >/dev/null <<EOF
${APP_HOST}, ${UNSUB_HOST} {
	reverse_proxy 127.0.0.1:8000
}
EOF

echo "== nightly backup (local; copy off the box too)"
sudo install -d -m 700 -o postgres -g postgres /var/backups/mazz
sudo tee /etc/cron.daily/mazz-backup >/dev/null <<'EOF'
#!/bin/sh
f=/var/backups/mazz/mazz-$(date +%F).sql.gz
su postgres -c "pg_dump mazz" | gzip > "$f" && chmod 600 "$f"
find /var/backups/mazz -name 'mazz-*.sql.gz' -mtime +14 -delete
EOF
sudo chmod +x /etc/cron.daily/mazz-backup

sudo systemctl daemon-reload
sudo systemctl enable --now mazz-web mazz-worker
sudo systemctl enable caddy
sudo systemctl restart caddy

sleep 3
echo "== check"
curl -fsS http://127.0.0.1:8000/health && echo " <- web healthy"
systemctl is-active mazz-web mazz-worker caddy
echo "Done. Sending is OFF. Next: DNS A records -> this VM's public IP; then fill in the"
echo "Google client id/secret with:  sudoedit $ENV_FILE  &&  sudo systemctl restart mazz-web mazz-worker"
