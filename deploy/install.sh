#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR=/opt/bell
SHARED_DIR="$APP_DIR/shared"
RELEASES_DIR="$APP_DIR/releases"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR=/var/backups/bell-system
UPDATER_DIR=/var/lib/bell-updater
UPDATER_LIB=/usr/local/lib/bell-system

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run this installer as root: sudo bash deploy/install.sh" >&2
  exit 1
fi
if ! command -v python3 >/dev/null || ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
  echo "python3, ffmpeg, and ffprobe must be installed before deployment." >&2
  exit 1
fi

version="${BELL_RELEASE_VERSION:-}"
if [[ -z "$version" ]]; then
  project_version="$(cd "$SOURCE_DIR" && python3 -c 'import pathlib,tomllib; print(tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["version"])')"
  version="v${project_version}"
fi
if [[ ! "$version" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
  echo "Release version must use vMAJOR.MINOR.PATCH: $version" >&2
  exit 1
fi
project_version="${version#v}"

commit="${BELL_RELEASE_COMMIT:-}"
if [[ -z "$commit" ]] && command -v git >/dev/null && git -C "$SOURCE_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  commit="$(git -C "$SOURCE_DIR" rev-parse HEAD)"
fi
if [[ -z "$commit" ]]; then
  commit="$(sha256sum "$SOURCE_DIR/pyproject.toml" | cut -c1-40)"
fi
if [[ ! "$commit" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Release commit must be a full lowercase Git commit SHA." >&2
  exit 1
fi

release_id="${version}-${commit:0:12}"
release_dir="$RELEASES_DIR/$release_id"
if [[ -e "$release_dir" ]]; then
  release_id="${release_id}-$(date -u +%Y%m%dT%H%M%SZ)"
  release_dir="$RELEASES_DIR/$release_id"
fi
stage="$RELEASES_DIR/.staging-${release_id}-$$"
cleanup() {
  if [[ -n "${stage:-}" && -d "$stage" ]]; then
    rm -rf -- "$stage"
  fi
}
trap cleanup EXIT

id bell >/dev/null 2>&1 || useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin bell
install -d -m 0755 -o root -g root "$APP_DIR" "$SHARED_DIR" "$RELEASES_DIR"
install -d -m 0700 -o root -g root "$BACKUP_DIR" "$UPDATER_DIR"
install -d -m 0755 -o root -g root "$UPDATER_LIB"

# Preserve a consistent copy of site-owned data before changing layout or code.
PYTHONPATH="$SOURCE_DIR" python3 -m bell.backup --app-dir "$APP_DIR" --backup-dir "$BACKUP_DIR"

migrate_shared_directory() {
  local name="$1"
  local mode="$2"
  local existing="$APP_DIR/$name"
  local shared="$SHARED_DIR/$name"
  if [[ ! -d "$shared" ]]; then
    if [[ -d "$existing" && ! -L "$existing" ]]; then
      mv -- "$existing" "$shared"
    elif [[ "$name" == "config" ]]; then
      install -d -m "$mode" -o bell -g bell "$shared"
      cp -a "$SOURCE_DIR/config/." "$shared/"
    elif [[ "$name" == "sounds" ]]; then
      cp -a "$SOURCE_DIR/sounds" "$shared"
    else
      install -d -m "$mode" -o bell -g bell "$shared"
    fi
  fi
  chown -R bell:bell "$shared"
  chmod "$mode" "$shared"
  if [[ -e "$existing" && ! -L "$existing" ]]; then
    echo "Cannot create compatibility link; unexpected path exists: $existing" >&2
    exit 1
  fi
  if [[ -L "$existing" ]]; then
    rm -- "$existing"
  fi
  ln -s "shared/$name" "$existing"
}

migrate_shared_directory config 0750
migrate_shared_directory sounds 0750
migrate_shared_directory state 0750
migrate_shared_directory logs 0750
install -d -m 0750 -o bell -g bell "$APP_DIR/state/update"

generated_ui_password=""
if [[ ! -f "$APP_DIR/config/bell.env" ]]; then
  generated_ui_password="$(runuser -u bell -- python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
  session_secret="$(runuser -u bell -- python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  install -m 0600 -o bell -g bell /dev/null "$APP_DIR/config/bell.env"
  printf 'BELL_UI_PASSWORD=%s\nBELL_UI_SESSION_SECRET=%s\n' "$generated_ui_password" "$session_secret" > "$APP_DIR/config/bell.env"
fi
chmod 0600 "$APP_DIR/config/bell.env"
chown bell:bell "$APP_DIR/config/bell.env"

install -d -m 0755 -o root -g root "$stage"
cp -a "$SOURCE_DIR/bell" "$SOURCE_DIR/deploy" "$SOURCE_DIR/docs" "$SOURCE_DIR/scripts" "$stage/"
install -m 0644 "$SOURCE_DIR/pyproject.toml" "$SOURCE_DIR/README.md" "$stage/"
if [[ -d "$SOURCE_DIR/wheelhouse" ]]; then
  cp -a "$SOURCE_DIR/wheelhouse" "$stage/wheelhouse"
fi
ln -s ../../shared/config "$stage/config"
ln -s ../../shared/sounds "$stage/sounds"
ln -s ../../shared/state "$stage/state"
ln -s ../../shared/logs "$stage/logs"
install -d -m 0755 -o bell -g bell "$stage/.venv"
runuser -u bell -- python3 -m venv "$stage/.venv"
if [[ -d "$stage/wheelhouse" ]]; then
  runuser -u bell -- "$stage/.venv/bin/python" -m pip install \
    --no-index --find-links "$stage/wheelhouse" "bell-system==$project_version"
else
  runuser -u bell -- "$stage/.venv/bin/python" -m pip install --upgrade pip setuptools
  runuser -u bell -- "$stage/.venv/bin/python" -m pip install "$stage"
fi

digest="${BELL_RELEASE_DIGEST:-manual}"
if [[ "$digest" != "manual" && ! "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "Release digest is invalid." >&2
  exit 1
fi
installed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf '{"schema":1,"tag":"%s","version":"%s","commit":"%s","digest":"%s","installed_at":"%s"}\n' \
  "$version" "$project_version" "$commit" "$digest" "$installed_at" > "$stage/RELEASE.json"
chown -R root:root "$stage"
chown -R bell:bell "$stage/.venv"

# Validate the exact staged code, dependencies, configuration, interface, codecs, and sounds.
cd "$stage"
runuser -u bell -- "$stage/.venv/bin/python" -m bell.service \
  --check-only --config-dir "$APP_DIR/config" --env-file "$APP_DIR/config/bell.env"

mv -- "$stage" "$release_dir"
stage=""
install -m 0755 -o root -g root "$release_dir/deploy/ota_updater.py" "$UPDATER_LIB/ota_updater.py"
install -m 0644 "$release_dir/deploy/bell-system.service" /etc/systemd/system/bell-system.service
install -m 0644 "$release_dir/deploy/bell-update.service" /etc/systemd/system/bell-update.service
install -m 0644 "$release_dir/deploy/bell-update.path" /etc/systemd/system/bell-update.path

new_link="$APP_DIR/.current-$$"
ln -s "releases/$release_id" "$new_link"
mv -Tf -- "$new_link" "$APP_DIR/current"

systemctl daemon-reload
systemctl enable bell-system.service bell-update.path
systemctl restart bell-system.service
systemctl start bell-update.path
systemctl --quiet is-active bell-system.service
systemctl --quiet is-active bell-update.path

echo "Bell release $version ($commit) installed."
echo "Health: http://127.0.0.1:8000/health"
echo "Metrics: http://127.0.0.1:8000/metrics"
echo "Front-office UI: http://$(hostname -I | awk '{print $1}'):8080"
if [[ -n "$generated_ui_password" ]]; then
  echo "Generated front-office password (store it securely): $generated_ui_password"
fi
