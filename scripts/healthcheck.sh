#!/usr/bin/env bash
set -euo pipefail
root=/opt/grid-assistant
"$root/bin/grid-assistant" health
systemctl is-active --quiet hermes-gateway.service
systemctl is-active --quiet hermes-gateway-member2.service
systemctl is-active --quiet rsshub.service
curl --fail --silent --show-error --max-time 15 http://127.0.0.1:1200/healthz >/dev/null
test "$(stat -c %a "$root/data/hermes/.env")" = "600"
test "$(stat -c %a "$root/data/hermes-member2/.env")" = "600"
test "$(stat -c %U "$root/data/app.db")" = "gridassistant"
shopt -s nullglob
delivery_failures=("$root"/data/hermes*/weixin/delivery-health.json)
if [[ ${#delivery_failures[@]} -gt 0 ]]; then
  for state_file in "${delivery_failures[@]}"; do
    route=$(basename "$(dirname "$(dirname "$state_file")")")
    echo "weixin_delivery=unhealthy route=$route reason=user_interaction_required" >&2
  done
  exit 1
fi
route_count=$(sqlite3 "$root/data/app.db" 'select count(distinct user_id) from user_delivery_routes;')
test "$route_count" -ge 2
available_kb=$(df --output=avail "$root" | tail -n 1)
test "$available_kb" -gt 1048576
echo "health=ok"
