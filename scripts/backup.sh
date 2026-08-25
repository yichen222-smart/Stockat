#!/usr/bin/env bash
set -euo pipefail
root=/opt/grid-assistant
stamp=$(date +%Y%m%d-%H%M%S)
target="$root/backups/$stamp"
install -d -m 0700 -o gridassistant -g gridassistant "$target"
sqlite3 "$root/data/app.db" ".backup '$target/app.db'"
tar --create --gzip --file "$target/config.tar.gz" \
  --directory "$root" config skills AGENTS.md
sha256sum "$target/app.db" "$target/config.tar.gz" > "$target/SHA256SUMS"
chown -R gridassistant:gridassistant "$target"
chmod -R go-rwx "$target"
find "$root/backups" -mindepth 1 -maxdepth 1 -type d -mtime +14 -print0 |
  xargs -0 --no-run-if-empty rm -rf --
echo "backup=$target"
