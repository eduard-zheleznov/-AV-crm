#!/bin/bash
set -euo pipefail

label="com.eduard.avito-crm-handoff"
domain="gui/$(id -u)"
agent_path="${HOME}/Library/LaunchAgents/${label}.plist"

launchctl bootout "${domain}/${label}" >/dev/null 2>&1 || true
rm -f "$agent_path"
echo "Mac-worker отключён. Настройки и журнал сохранены в Library/Application Support/AvitoCRM-Handoff."
