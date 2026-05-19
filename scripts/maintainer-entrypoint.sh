#!/usr/bin/env sh
set -eu

/app/scripts/init_storage.sh

CONFIG_PATH="${GROK_MAINTAINER_CONFIG:-/tmp/maintainer.config.json}"
MAINTAINER_ENABLED="${MAINTAINER_ENABLED:-true}"
MAINTAINER_COUNT="${MAINTAINER_COUNT:-1}"
MAINTAINER_INTERVAL_SEC="${MAINTAINER_INTERVAL_SEC:-1800}"
MAINTAINER_RETRY_SEC="${MAINTAINER_RETRY_SEC:-60}"
MAINTAINER_API_HEALTH_URL="${MAINTAINER_API_HEALTH_URL:-http://grokmanager:8000/health}"

mkdir -p "$(dirname "$CONFIG_PATH")"

write_config_from_env() {
  GENERATED_PATH="$CONFIG_PATH" python3 - <<'PY'
import json
import os
from pathlib import Path


def as_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


domains = [
    item.strip()
    for item in os.getenv("MAINTAINER_EMAIL_DOMAINS", "").split(",")
    if item.strip()
]
if not domains:
    single = os.getenv("MAINTAINER_EMAIL_DOMAIN", "").strip()
    if single:
        domains = [single]

payload = {
    "run": {
        "count": int(os.getenv("MAINTAINER_COUNT", "1") or "1"),
    },
    "email": {
        "worker_domain": os.getenv("MAINTAINER_EMAIL_WORKER_DOMAIN", "").strip(),
        "email_domains": domains,
        "admin_password": os.getenv("MAINTAINER_EMAIL_ADMIN_PASSWORD", "").strip(),
        "verify_ssl": as_bool("MAINTAINER_EMAIL_VERIFY_SSL", True),
    },
    "api": {
        "endpoint": os.getenv(
            "MAINTAINER_API_ENDPOINT",
            "http://grokmanager:8000/v1/admin/tokens",
        ).strip(),
        "token": (
            os.getenv("MAINTAINER_API_TOKEN", "").strip()
            or os.getenv("GROK_APP_APP_KEY", "").strip()
            or "grok2api"
        ),
        "append": as_bool("MAINTAINER_API_APPEND", True),
        "pool": os.getenv("MAINTAINER_API_POOL", "basic").strip() or "basic",
        "verify_ssl": as_bool("MAINTAINER_API_VERIFY_SSL", True),
    },
}

path = Path(os.environ["GENERATED_PATH"])
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(payload, ensure_ascii=False, indent=4), encoding="utf-8")
print(path)
PY
}

config_ready() {
  [ -n "${MAINTAINER_EMAIL_WORKER_DOMAIN:-}" ] || return 1
  [ -n "${MAINTAINER_EMAIL_ADMIN_PASSWORD:-}" ] || return 1
  [ -n "${MAINTAINER_EMAIL_DOMAINS:-${MAINTAINER_EMAIL_DOMAIN:-}}" ] || return 1
  return 0
}

wait_for_api() {
  until wget -qO /dev/null "$MAINTAINER_API_HEALTH_URL"; do
    echo "[maintainer] waiting for API health endpoint: $MAINTAINER_API_HEALTH_URL"
    sleep "$MAINTAINER_RETRY_SEC"
  done
}

run_once() {
  EXTRA_ARGS=""
  if [ "${MAINTAINER_EXTRACT_NUMBERS:-false}" = "true" ]; then
    EXTRA_ARGS="--extract-numbers"
  fi
  python3 -m app.maintainer --config "$CONFIG_PATH" --count "$MAINTAINER_COUNT" $EXTRA_ARGS
}

while true; do
  if [ "$MAINTAINER_ENABLED" != "true" ]; then
    echo "[maintainer] disabled via MAINTAINER_ENABLED=$MAINTAINER_ENABLED, sleeping"
    sleep "$MAINTAINER_INTERVAL_SEC"
    continue
  fi

  if [ ! -f "$CONFIG_PATH" ]; then
    if config_ready; then
      echo "[maintainer] generating runtime config: $CONFIG_PATH"
      write_config_from_env >/dev/null
    else
      echo "[maintainer] config missing and env incomplete, sleeping"
      sleep "$MAINTAINER_RETRY_SEC"
      continue
    fi
  fi

  wait_for_api

  if run_once; then
    echo "[maintainer] cycle completed successfully"
    sleep "$MAINTAINER_INTERVAL_SEC"
  else
    echo "[maintainer] cycle failed, retrying after ${MAINTAINER_RETRY_SEC}s"
    sleep "$MAINTAINER_RETRY_SEC"
  fi
done
