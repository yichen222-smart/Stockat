#!/usr/bin/env bash
set -euo pipefail

root=/opt/grid-assistant

systemd-analyze verify \
  /etc/systemd/system/grid-assistant.service \
  /etc/systemd/system/grid-assistant.timer \
  /etc/systemd/system/grid-assistant-health.service \
  /etc/systemd/system/grid-assistant-health.timer \
  /etc/systemd/system/grid-assistant-backup.service \
  /etc/systemd/system/grid-assistant-backup.timer \
  /etc/systemd/system/hermes-gateway-member2.service \
  /etc/systemd/system/rsshub.service

"$root/scripts/healthcheck.sh"
"$root/scripts/backup.sh"
logrotate -d /etc/logrotate.d/grid-assistant >/dev/null

stat -c %a "$root/data/hermes/.env" | grep -qx 600
stat -c %a "$root/data/hermes-member2/.env" | grep -qx 600
stat -c %U "$root/data/app.db" | grep -qx gridassistant

systemctl is-enabled \
  hermes-gateway.service \
  hermes-gateway-member2.service \
  rsshub.service \
  grid-assistant.timer \
  grid-assistant-health.timer \
  grid-assistant-backup.timer
systemctl is-active \
  hermes-gateway.service \
  hermes-gateway-member2.service \
  rsshub.service \
  grid-assistant.timer \
  grid-assistant-health.timer \
  grid-assistant-backup.timer

sqlite3 "$root/data/app.db" \
  'select role || ":" || status || ":" || count(*) from users group by role,status;'
"$root/runtime/app-venv/bin/python" "$root/scripts/test-dual-user.py"

latest=$(
  find "$root/backups" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' |
    sort -nr |
    head -n 1 |
    cut -d' ' -f2-
)
(
  cd "$latest"
  sha256sum -c SHA256SUMS
)
sqlite3 "$latest/app.db" 'pragma integrity_check;' | grep -qx ok

scan_dir=$(mktemp -d /tmp/grid-release-scan.XXXXXX)
tar -xzf "$root/releases/20260730-final/project.tar.gz" -C "$scan_dir"
if grep -RIlE 'sk-[A-Za-z0-9]{12,}|Ubuntu[0-9]{4,}' "$scan_dir" >/dev/null; then
  echo "secret_scan=failed"
  exit 1
fi

echo "secret_scan=clean"
echo "system_acceptance=ok"
