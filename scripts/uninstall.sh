#!/usr/bin/env bash
set -euo pipefail
root=/opt/grid-assistant
if [[ ${1:-} != "--confirm" ]]; then
  echo "Refusing. Re-run with --confirm after creating an external backup." >&2
  exit 2
fi
systemctl disable --now grid-assistant.timer grid-assistant-health.timer grid-assistant-backup.timer rsshub.service hermes-gateway.service
echo "Services disabled. Data intentionally preserved at $root."
