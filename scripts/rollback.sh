#!/usr/bin/env bash
set -euo pipefail
root=/opt/grid-assistant
release=${1:-}
if [[ -z "$release" || ! -d "$release" ]]; then
  echo "usage: rollback.sh /opt/grid-assistant/releases/<release>" >&2
  exit 2
fi
resolved=$(realpath "$release")
case "$resolved" in
  "$root"/releases/*) ;;
  *) echo "release path is outside $root/releases" >&2; exit 2 ;;
esac
exec "$root/scripts/deploy.sh" "$resolved"