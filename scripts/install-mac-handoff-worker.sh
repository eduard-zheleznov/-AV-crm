#!/bin/bash
set -euo pipefail

usage() {
  echo "Использование: bash scripts/install-mac-handoff-worker.sh --settings /путь/handoff.env"
}

settings_path=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --settings)
      [[ $# -ge 2 ]] || { usage >&2; exit 2; }
      settings_path="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Неизвестный аргумент: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

source_dir="$(cd "$(dirname "$0")/.." && pwd -P)"
app_dir="${HOME}/Library/Application Support/AvitoCRM-Handoff"
venv_dir="${app_dir}/venv"
env_path="${app_dir}/.env"
logs_dir="${app_dir}/logs"
data_dir="${app_dir}/data"
output_dir="${app_dir}/output"
agent_dir="${HOME}/Library/LaunchAgents"
agent_path="${agent_dir}/com.eduard.avito-crm-handoff.plist"
agent_label="com.eduard.avito-crm-handoff"

if [[ -n "$settings_path" ]]; then
  settings_path="$(cd "$(dirname "$settings_path")" && pwd -P)/$(basename "$settings_path")"
  [[ -f "$settings_path" ]] || { echo "Файл настроек не найден: $settings_path" >&2; exit 2; }
elif [[ ! -f "$env_path" ]]; then
  usage >&2
  exit 2
fi

mkdir -p "$app_dir" "$logs_dir" "$data_dir" "$output_dir" "$agent_dir"
chmod 700 "$app_dir" "$logs_dir" "$data_dir" "$output_dir"

python3 -c 'import sys; raise SystemExit("Нужен Python 3.11 или новее") if sys.version_info < (3, 11) else None'
python3 -m venv "$venv_dir"
"${venv_dir}/bin/python" -m pip install --disable-pip-version-check --quiet \
  'httpx>=0.27,<1' 'python-dotenv>=1.0,<2' 'tzdata>=2024.1'
"${venv_dir}/bin/python" -m pip install --disable-pip-version-check --quiet \
  --force-reinstall --no-deps "$source_dir"

if [[ -n "$settings_path" ]]; then
  SETTINGS_SOURCE="$settings_path" ENV_DESTINATION="$env_path" \
  APP_DIR="$app_dir" "${venv_dir}/bin/python" - <<'PY'
import os
from pathlib import Path

from dotenv import dotenv_values

source = Path(os.environ["SETTINGS_SOURCE"])
destination = Path(os.environ["ENV_DESTINATION"])
app_dir = Path(os.environ["APP_DIR"])
allowed = {
    "LPTRACKER_BASE_URL", "LPTRACKER_LOGIN", "LPTRACKER_PASSWORD",
    "LPTRACKER_PROJECT_ID", "LPTRACKER_PROJECT_NAME", "LPTRACKER_FIELD_NAME",
    "LPTRACKER_FIELD_VALUE", "LPTRACKER_SERVICE_NAME", "LPTRACKER_TIMEZONE",
    "ROBOT_HANDOFF_SOURCE_FUNNEL_NAME", "ROBOT_HANDOFF_SOURCE_FIELD_VALUES",
    "ROBOT_HANDOFF_TARGET_FUNNEL_NAME", "ROBOT_HANDOFF_FIELD_NAME",
    "ROBOT_HANDOFF_FIELD_VALUE", "ROBOT_HANDOFF_STAGE_DATE_FIELD_NAME",
    "ROBOT_HANDOFF_STAGE_DELAY_DAYS", "ROBOT_HANDOFF_POLL_SECONDS",
    "ROBOT_HANDOFF_LOOKBACK_HOURS", "ROBOT_HANDOFF_BATCH_SIZE",
    "ROBOT_HANDOFF_MIN_CONFIDENCE", "GEMINI_API_KEY", "GEMINI_MODEL",
    "GEMINI_API_BASE_URL", "GEMINI_MAX_AUDIO_BYTES", "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_PRIMARY_CHAT_IDS", "TELEGRAM_BACKUP_CHAT_IDS",
    "TELEGRAM_REMINDER_MINUTES", "TELEGRAM_REQUEST_TIMEOUT_SECONDS",
    "TELEGRAM_SEND_ATTEMPTS", "MAX_API_BASE_URL", "MAX_BOT_TOKEN",
    "MAX_PRIMARY_RECIPIENTS", "MAX_BACKUP_RECIPIENTS",
    "MAX_REQUEST_TIMEOUT_SECONDS", "MAX_SEND_ATTEMPTS", "SMTP_HOST",
    "SMTP_PORT", "SMTP_SECURITY", "SMTP_USERNAME", "SMTP_PASSWORD",
    "SMTP_FROM_ADDRESS", "EMAIL_PRIMARY_RECIPIENTS", "EMAIL_BACKUP_RECIPIENTS",
    "EMAIL_REQUEST_TIMEOUT_SECONDS", "EMAIL_SEND_ATTEMPTS",
}
values = dotenv_values(source, interpolate=False, encoding="utf-8-sig")
required = {"LPTRACKER_LOGIN", "LPTRACKER_PASSWORD", "GEMINI_API_KEY"}
missing = sorted(key for key in required if not str(values.get(key) or "").strip())
if not (str(values.get("LPTRACKER_PROJECT_ID") or "").strip() or str(values.get("LPTRACKER_PROJECT_NAME") or "").strip()):
    missing.append("LPTRACKER_PROJECT_ID или LPTRACKER_PROJECT_NAME")
if missing:
    raise SystemExit("Не заполнены обязательные настройки: " + ", ".join(missing))

def quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'

runtime = {
    "APP_DATA_DIR": str(app_dir / "data"),
    "APP_OUTPUT_DIR": str(app_dir / "output"),
    "APP_LOGS_DIR": str(app_dir / "logs"),
    "ROBOT_HANDOFF_ENABLED": "true",
    "NOTIFICATION_COMPUTER_NAME": "Mac",
}
lines = ["# Локальные настройки Mac-worker. Не отправлять в чат или Git."]
for key, value in runtime.items():
    lines.append(f"{key}={quote(value)}")
for key in sorted(allowed):
    value = values.get(key)
    if value is not None:
        lines.append(f"{key}={quote(str(value))}")
destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
fi
chmod 600 "$env_path"

"${venv_dir}/bin/python" -m avito_crm.mac_handoff_worker \
  --root "$app_dir" --check-config

PLIST_PATH="$agent_path" VENV_DIR="$venv_dir" APP_DIR="$app_dir" \
LOGS_DIR="$logs_dir" AGENT_LABEL="$agent_label" python3 - <<'PY'
import os
import plistlib
from pathlib import Path

venv_dir = Path(os.environ["VENV_DIR"])
app_dir = Path(os.environ["APP_DIR"])
logs_dir = Path(os.environ["LOGS_DIR"])
payload = {
    "Label": os.environ["AGENT_LABEL"],
    "ProgramArguments": [
        "/usr/bin/caffeinate",
        "-i",
        str(venv_dir / "bin" / "python"),
        "-m",
        "avito_crm.mac_handoff_worker",
        "--root",
        str(app_dir),
        "--apply",
    ],
    "RunAtLoad": True,
    "KeepAlive": True,
    "ProcessType": "Background",
    "Nice": 10,
    "LowPriorityIO": True,
    "ThrottleInterval": 30,
    "StandardOutPath": str(logs_dir / "launch-agent.out.log"),
    "StandardErrorPath": str(logs_dir / "launch-agent.err.log"),
}
with Path(os.environ["PLIST_PATH"]).open("wb") as handle:
    plistlib.dump(payload, handle, sort_keys=False)
PY
chmod 600 "$agent_path"
plutil -lint "$agent_path" >/dev/null

domain="gui/$(id -u)"
launchctl bootout "${domain}/${agent_label}" >/dev/null 2>&1 || true
launchctl bootstrap "$domain" "$agent_path"
launchctl kickstart -k "${domain}/${agent_label}"

sleep 2
if launchctl print "${domain}/${agent_label}" >/dev/null 2>&1; then
  echo "ГОТОВО: обработка «Лид с робота» постоянно работает на Mac."
  echo "Состояние: bash '${source_dir}/scripts/mac-handoff-status.sh'"
else
  echo "Установка завершена, но LaunchAgent не запустился. Проверьте: ${logs_dir}" >&2
  exit 1
fi
