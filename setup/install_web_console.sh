#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/root/.openclaw/workspace}"
WEB_DIR="$PROJECT_ROOT/web-console"
SERVICE_SRC="$PROJECT_ROOT/systemd/moss-web-console.service"
SERVICE_DST="/etc/systemd/system/moss-web-console.service"

if [[ ! -d "$WEB_DIR" ]]; then
  echo "ERROR: web-console not found at $WEB_DIR"
  echo "Set PROJECT_ROOT to the repository root if you installed elsewhere."
  exit 1
fi

python3 -m venv "$WEB_DIR/.venv"
"$WEB_DIR/.venv/bin/pip" install --upgrade pip
"$WEB_DIR/.venv/bin/pip" install -r "$WEB_DIR/requirements.txt"

if [[ ! -f "$SERVICE_SRC" ]]; then
  echo "ERROR: missing $SERVICE_SRC"
  exit 1
fi

cp "$SERVICE_SRC" "$SERVICE_DST"
systemctl daemon-reload
systemctl enable moss-web-console.service
systemctl restart moss-web-console.service

sleep 1
systemctl --no-pager --full status moss-web-console.service || true

echo
echo "MOSS Web Console installed."
echo "Open: http://<RDK-X5-IP>:5500"
echo "Logs: journalctl -u moss-web-console.service -f"
