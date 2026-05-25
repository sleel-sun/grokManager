#!/usr/bin/env sh
set -eu

/app/scripts/init_storage.sh

API_BASE="${MAINTAINER_API_BASE_URL:-http://grokmanager:8000}"
API_ENDPOINT="${MAINTAINER_API_ENDPOINT:-${API_BASE%/}/admin/api/tokens/add}"
CONFIG_PATH="${GROK_MAINTAINER_CONFIG:-/app/data/maintainer/compose/maintainer.config.json}"
COUNT="${MAINTAINER_COUNT:-1}"
WORKERS="${MAINTAINER_WORKERS:-1}"
INTERVAL_SEC="${MAINTAINER_INTERVAL_SEC:-3600}"

wait_for_api() {
  health_url="${API_BASE%/}/health"
  while ! wget -qO /dev/null "$health_url"; do
    echo "[maintainer] waiting for grokmanager at $health_url"
    sleep 5
  done
}

has_required_config() {
  test -n "${MAINTAINER_EMAIL_WORKER_DOMAIN:-}" \
    && test -n "${MAINTAINER_EMAIL_DOMAINS:-}" \
    && test -n "${MAINTAINER_EMAIL_ADMIN_PASSWORD:-}"
}

write_config() {
  mkdir -p "$(dirname "$CONFIG_PATH")"
  python - "$CONFIG_PATH" <<'PY'
import json
import os
import sys

path = sys.argv[1]
api_token = (
    os.getenv("MAINTAINER_API_TOKEN", "").strip()
    or os.getenv("GROK_APP_APP_KEY", "").strip()
    or "grok2api"
)
domains = [
    part.strip()
    for part in os.getenv("MAINTAINER_EMAIL_DOMAINS", "").split(",")
    if part.strip()
]
payload = {
    "run": {
        "count": int(os.getenv("MAINTAINER_COUNT", "1") or "1"),
        "workers": int(os.getenv("MAINTAINER_WORKERS", "1") or "1"),
    },
    "email": {
        "worker_domain": os.getenv("MAINTAINER_EMAIL_WORKER_DOMAIN", "").strip(),
        "email_domains": domains,
        "admin_password": os.getenv("MAINTAINER_EMAIL_ADMIN_PASSWORD", ""),
        "verify_ssl": os.getenv("MAINTAINER_VERIFY_SSL", "true").lower() in {"1", "true", "yes", "on"},
    },
    "api": {
        "endpoint": os.getenv("MAINTAINER_API_ENDPOINT", "").strip(),
        "token": api_token,
        "append": True,
        "pool": os.getenv("MAINTAINER_POOL", "basic").strip().lower() or "basic",
        "verify_ssl": os.getenv("MAINTAINER_VERIFY_SSL", "true").lower() in {"1", "true", "yes", "on"},
    },
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=False, indent=2)
os.chmod(path, 0o600)
PY
}

export MAINTAINER_API_ENDPOINT="$API_ENDPOINT"
export GROK_MAINTAINER_CONFIG="$CONFIG_PATH"
export MAINTAINER_HEADLESS="${MAINTAINER_HEADLESS:-false}"
export MAINTAINER_USE_XVFB="${MAINTAINER_USE_XVFB:-true}"
export MAINTAINER_NO_SANDBOX="${MAINTAINER_NO_SANDBOX:-true}"
export MAINTAINER_DISABLE_DEV_SHM="${MAINTAINER_DISABLE_DEV_SHM:-true}"
export MAINTAINER_BROWSER_PATH="${MAINTAINER_BROWSER_PATH:-/usr/bin/chromium-browser}"
export MAINTAINER_WINDOW_SIZE="${MAINTAINER_WINDOW_SIZE:-1440,900}"

while ! has_required_config; do
  echo "[maintainer] missing MAINTAINER_EMAIL_WORKER_DOMAIN, MAINTAINER_EMAIL_DOMAINS, or MAINTAINER_EMAIL_ADMIN_PASSWORD; retrying"
  sleep 30
done

wait_for_api

while :; do
  write_config
  echo "[maintainer] starting registration batch count=$COUNT workers=$WORKERS endpoint=$API_ENDPOINT"
  python -m app.maintainer --config "$CONFIG_PATH" --count "$COUNT" --workers "$WORKERS" || true

  if [ "$INTERVAL_SEC" = "0" ]; then
    break
  fi
  echo "[maintainer] batch finished; sleeping ${INTERVAL_SEC}s"
  sleep "$INTERVAL_SEC"
done
