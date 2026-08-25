#!/usr/bin/env bash
set -euo pipefail
exec runuser -u gridassistant -- /opt/grid-assistant/bin/grid-assistant migrate
