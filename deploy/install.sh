#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/opt/bell
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR=/var/backups/bell-system

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run this installer as root: sudo bash deploy/install.sh" >&2
  exit 1
fi
if ! command -v python3 >/dev/null || ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
  echo "python3, ffmpeg, and ffprobe must be installed before deployment." >&2
  exit 1
fi

id bell >/dev/null 2>&1 || useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin bell
install -d -m 0750 -o bell -g bell "$APP_DIR" "$APP_DIR/state" "$APP_DIR/logs"
install -d -m 0700 -o root -g root "$BACKUP_DIR"

PYTHONPATH="$SOURCE_DIR" python3 -m bell.backup --app-dir "$APP_DIR" --backup-dir "$BACKUP_DIR"

if [[ "$SOURCE_DIR" != "$APP_DIR" ]]; then
  cp -a "$SOURCE_DIR/bell" "$SOURCE_DIR/deploy" "$SOURCE_DIR/docs" "$SOURCE_DIR/scripts" "$APP_DIR/"
  install -m 0644 "$SOURCE_DIR/pyproject.toml" "$SOURCE_DIR/README.md" "$APP_DIR/"
  if [[ ! -f "$APP_DIR/config/settings.yaml" ]]; then
    install -d -m 0750 -o bell -g bell "$APP_DIR/config"
    cp -a "$SOURCE_DIR/config/." "$APP_DIR/config/"
  fi
  if [[ ! -d "$APP_DIR/sounds" ]]; then
    cp -a "$SOURCE_DIR/sounds" "$APP_DIR/sounds"
  fi
fi

install -d -m 0750 -o bell -g bell "$APP_DIR/config" "$APP_DIR/sounds" "$APP_DIR/state" "$APP_DIR/logs"
chown -R bell:bell "$APP_DIR/config" "$APP_DIR/sounds" "$APP_DIR/state" "$APP_DIR/logs"
cd "$APP_DIR"
if [[ ! -x .venv/bin/python ]]; then
  runuser -u bell -- python3 -m venv .venv
fi
runuser -u bell -- .venv/bin/python -m pip install --upgrade pip setuptools
runuser -u bell -- .venv/bin/python -m pip install .

generated_ui_password=""
if [[ ! -f config/bell.env ]]; then
  generated_ui_password="$(runuser -u bell -- .venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(18))')"
  session_secret="$(runuser -u bell -- .venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"
  install -m 0600 -o bell -g bell /dev/null config/bell.env
  printf 'BELL_UI_PASSWORD=%s\nBELL_UI_SESSION_SECRET=%s\n' "$generated_ui_password" "$session_secret" > config/bell.env
fi
chmod 0600 config/bell.env
chown bell:bell config/bell.env

# Validate the exact installed environment before changing the running unit. The Python loader treats
# this as data and never shell-evaluates values from the service-owned file.
runuser -u bell -- .venv/bin/python -m bell.service --check-only --config-dir config --env-file config/bell.env
install -m 0644 deploy/bell-system.service /etc/systemd/system/bell-system.service
systemctl daemon-reload
systemctl enable bell-system.service
systemctl restart bell-system.service
systemctl --quiet is-active bell-system.service

echo "Bell service installed. Health: http://127.0.0.1:8000/health"
echo "Metrics: http://127.0.0.1:8000/metrics"
echo "Front-office UI: http://$(hostname -I | awk '{print $1}'):8080"
if [[ -n "$generated_ui_password" ]]; then
  echo "Generated front-office password (store it securely): $generated_ui_password"
fi
