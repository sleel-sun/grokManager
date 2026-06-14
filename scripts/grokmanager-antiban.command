#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_PATH="${GROKMANAGER_APP_PATH:-${HERE}/grokManager.app}"

if [[ ! -d "$APP_PATH" && -d "${HERE}/../grokManager.app" ]]; then
  APP_PATH="${HERE}/../grokManager.app"
fi

DEPLOY_SCRIPT="${APP_PATH}/Contents/Resources/scripts/deploy-antiban-local.sh"

if [[ ! -x "$DEPLOY_SCRIPT" ]]; then
  echo "Cannot find anti-ban deploy script: ${DEPLOY_SCRIPT}" >&2
  echo "Put this command next to grokManager.app, or set GROKMANAGER_APP_PATH." >&2
  exit 1
fi

exec "$DEPLOY_SCRIPT" --app "$APP_PATH" --launch "$@"
