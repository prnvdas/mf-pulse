#!/usr/bin/env bash
# MF Pulse on a Lightsail 512MB instance (Ubuntu 24.04).
#
#   sudo bash bootstrap.sh https://github.com/<you>/mf-pulse.git
#
# Ends with nginx serving the dashboard on :80 and a FastAPI price endpoint
# on /api, plus systemd timers running the estimator.
set -euo pipefail

REPO="${1:?usage: bootstrap.sh <git-repo-url>}"
APP=/opt/mf-pulse
USER=mfpulse

echo "==> packages"
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip nginx git

echo "==> user + checkout"
id -u "$USER" &>/dev/null || useradd -r -s /usr/sbin/nologin -d "$APP" "$USER"
[ -d "$APP/.git" ] || git clone --depth 1 "$REPO" "$APP"
chown -R "$USER:$USER" "$APP"

echo "==> venv"
python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt" fastapi uvicorn

echo "==> systemd"
cp "$APP/deploy/lightsail"/*.service "$APP/deploy/lightsail"/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now mfpulse-api.service
systemctl enable --now mfpulse-estimate.timer
systemctl enable --now mfpulse-reconcile.timer

echo "==> nginx"
cp "$APP/deploy/lightsail/nginx.conf" /etc/nginx/sites-available/mfpulse
ln -sf /etc/nginx/sites-available/mfpulse /etc/nginx/sites-enabled/mfpulse
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

echo
echo "Done. Dashboard: http://$(curl -s --max-time 5 ifconfig.me || echo '<instance-ip>')/"
echo "Set Price source -> Worker -> http://<that-ip>/api"
echo
echo "Reminder: if GitHub Actions is your writer of record, disable the"
echo "reconcile timer here so the two don't fork your unit counts:"
echo "  systemctl disable --now mfpulse-reconcile.timer"
