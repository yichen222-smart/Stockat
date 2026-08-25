#!/usr/bin/env bash
set -euo pipefail
root=/opt/grid-assistant
backup=${1:-}
if [[ -z "$backup" || ! -d "$backup" ]]; then
  echo "usage: restore.sh /opt/grid-assistant/backups/YYYYMMDD-HHMMSS" >&2
  exit 2
fi
resolved=$(realpath "$backup")
case "$resolved" in
  "$root"/backups/*) ;;
  *) echo "backup path is outside $root/backups" >&2; exit 2 ;;
esac
(cd "$resolved" && sha256sum --check SHA256SUMS)
sqlite3 "$resolved/app.db" "PRAGMA integrity_check;" | grep -qx ok
systemctl stop grid-assistant.service
install -m 0600 -o gridassistant -g gridassistant "$resolved/app.db" "$root/data/app.db.restore"
mv "$root/data/app.db.restore" "$root/data/app.db"
tar --extract --gzip --file "$resolved/config.tar.gz" --directory "$root"
systemctl start hermes-gateway.service rsshub.service
"$root/scripts/healthcheck.sh"
echo "restore=ok"
