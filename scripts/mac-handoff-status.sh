#!/bin/bash
set -euo pipefail

label="com.eduard.avito-crm-handoff"
app_dir="${HOME}/Library/Application Support/AvitoCRM-Handoff"
domain="gui/$(id -u)"

if launchctl print "${domain}/${label}" >/dev/null 2>&1; then
  echo "Mac-worker: РАБОТАЕТ"
  launchctl print "${domain}/${label}" | awk '/state =|pid =|last exit code =/ { print "  " $0 }'
else
  echo "Mac-worker: НЕ ЗАПУЩЕН"
fi

log_path="${app_dir}/logs/avito-crm.log"
if [[ -f "$log_path" ]]; then
  echo "Последние события:"
  tail -n 20 "$log_path"
fi
