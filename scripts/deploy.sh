#!/usr/bin/env bash
set -euo pipefail
root=/opt/grid-assistant
release=${1:-}
if [[ -z "$release" || ! -f "$release/project.tar.gz" ]]; then
  echo "usage: deploy.sh /opt/grid-assistant/releases/<release>" >&2
  exit 2
fi
resolved=$(realpath "$release")
case "$resolved" in
  "$root"/releases/*) ;;
  *) echo "release path is outside $root/releases" >&2; exit 2 ;;
esac
if tar -tzf "$resolved/project.tar.gz" |
  awk '/^\// || /(^|\/)\.\.(\/|$)/ { bad=1 } END { exit bad }'
then
  :
else
  echo "unsafe paths in release archive" >&2
  exit 2
fi
"$root/scripts/backup.sh"
tar -xzf "$resolved/project.tar.gz" -C "$root"
chown -R root:gridassistant \
  "$root/app" "$root/config" "$root/scripts" "$root/skills" "$root/systemd" "$root/tests"
find "$root/scripts" -type f -name '*.sh' -exec chmod 0750 {} +
install -m 0750 -o root -g gridassistant "$root/scripts/grid-assistant" "$root/bin/grid-assistant"
install -m 0750 -o root -g gridassistant "$root/scripts/add-weixin-user" "$root/bin/add-weixin-user"
install -m 0644 "$root"/systemd/*.service "$root"/systemd/*.timer /etc/systemd/system/
install -m 0644 "$root/config/grid-assistant.logrotate" /etc/logrotate.d/grid-assistant
"$root/scripts/migrate.sh"
systemctl daemon-reload
systemctl enable --now hermes-gateway.service rsshub.service
systemctl enable --now grid-assistant.timer grid-assistant-health.timer grid-assistant-backup.timer
"$root/scripts/healthcheck.sh"
echo "deploy=ok release=$resolved"