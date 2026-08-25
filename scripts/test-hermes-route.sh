#!/usr/bin/env bash
set -euo pipefail

root=/opt/grid-assistant
hermes_home="$root/data/hermes"
admin_id=$(jq -r 'keys[0]' "$hermes_home/platforms/pairing/weixin-approved.json")
test -n "$admin_id"

cd "$hermes_home"
export HERMES_HOME="$hermes_home"
export HERMES_SESSION_USER_ID="$admin_id"
export HERMES_SESSION_PLATFORM=weixin

exec "$root/bin/hermes" -z "查看我的关注"
