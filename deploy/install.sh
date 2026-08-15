#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/opt/bell
if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run this installer as root: sudo bash deploy/install.sh" >&2
  exit 1
fi

cd "$APP_DIR"
id bell >/dev/null 2>&1 || useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin bell
install -d -o bell -g bell "$APP_DIR/config" "$APP_DIR/sounds" "$APP_DIR/state" "$APP_DIR/logs"
chown -R bell:bell "$APP_DIR/config" "$APP_DIR/sounds" "$APP_DIR/state" "$APP_DIR/logs"
runuser -u bell -- python3 -m venv .venv
runuser -u bell -- .venv/bin/python -m pip install --upgrade pip setuptools
runuser -u bell -- .venv/bin/python -m pip install .

generated_ui_password=""
if [[ ! -f config/bell.env ]]; then
  generated_ui_password="$(runuser -u bell -- .venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(18))')"
  session_secret="$(runuser -u bell -- .venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"
  install -m 0600 -o bell -g bell /dev/null config/bell.env
  printf 'BELL_UI_PASSWORD=%s\nBELL_UI_SESSION_SECRET=%s\n' "$generated_ui_password" "$session_secret" > config/bell.env
fi

install -m 0644 deploy/bell-system.service /etc/systemd/system/bell-system.service
systemctl daemon-reload
systemctl enable --now bell-system.service
echo "Bell service installed. Health: http://127.0.0.1:8000/health"
echo "Front-office UI: http://$(hostname -I | awk '{print $1}'):8080"
if [[ -n "$generated_ui_password" ]]; then
  echo "Generated front-office password (store it securely): $generated_ui_password"
fi
