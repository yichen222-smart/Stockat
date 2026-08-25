#!/usr/bin/env bash
set -euo pipefail
root=/opt/grid-assistant
id gridassistant >/dev/null
install -d -m 0750 -o gridassistant -g gridassistant \
  "$root/data" "$root/data/market_cache" "$root/data/raw_news" \
  "$root/data/report_materials" "$root/logs" "$root/backups" "$root/releases"
"$root/scripts/migrate.sh"
echo "bootstrap=ok"
