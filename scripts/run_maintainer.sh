#!/usr/bin/env sh
set -eu

/app/scripts/init_storage.sh

API_BASE="${MAINTAINER_API_BASE_URL:-http://grokmanager:8000}"
API_ENDPOINT="${MAINTAINER_API_ENDPOINT:-${API_BASE%/}/admin/api/tokens/add}"
CONFIG_PATH="${GROK_MAINTAINER_CONFIG:-/app/data/maintainer/compose/maintainer.config.json}"
SAVED_WEB_CONFIG="${MAINTAINER_WEB_CONFIG:-/app/data/maintainer/web/maintainer.config.json}"
ENV_FALLBACK_PATH="${CONFIG_PATH}.env"
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

bootstrap_env_from_saved_config() {
  if has_required_config || [ ! -f "$SAVED_WEB_CONFIG" ]; then
    return 0
  fi

  mkdir -p "$(dirname "$ENV_FALLBACK_PATH")"
  if python - "$SAVED_WEB_CONFIG" "$ENV_FALLBACK_PATH" <<'PY'
import json
import os
import shlex
import sys

source_path = sys.argv[1]
env_path = sys.argv[2]

try:
    with open(source_path, "r", encoding="utf-8") as handle:
        saved = json.load(handle)
except Exception as exc:
    print(f"[maintainer] failed to read saved WebUI config: {exc}", file=sys.stderr)
    sys.exit(1)

email = saved.get("email") if isinstance(saved.get("email"), dict) else {}
api = saved.get("api") if isinstance(saved.get("api"), dict) else {}
web = saved.get("web") if isinstance(saved.get("web"), dict) else {}

exports: dict[str, str] = {}

def export_if_empty(key: str, value: object) -> None:
    if os.getenv(key, "").strip():
        return
    if value is None:
        return
    if isinstance(value, bool):
        value = "true" if value else "false"
    elif isinstance(value, list):
        value = ",".join(str(item).strip() for item in value if str(item).strip())
    else:
        value = str(value)
    if value:
        exports[key] = value

export_if_empty("MAINTAINER_EMAIL_WORKER_DOMAIN", email.get("worker_domain"))
export_if_empty("MAINTAINER_EMAIL_DOMAINS", email.get("email_domains"))
export_if_empty("MAINTAINER_EMAIL_ADMIN_PASSWORD", email.get("admin_password"))
export_if_empty("MAINTAINER_API_TOKEN", api.get("token"))
export_if_empty("MAINTAINER_POOL", api.get("pool"))
export_if_empty("MAINTAINER_VERIFY_SSL", email.get("verify_ssl"))
export_if_empty("MAINTAINER_TURNSTILE_SOLVER_PROVIDER", web.get("turnstile_solver_provider"))
export_if_empty("MAINTAINER_TURNSTILE_SOLVER_API_KEY", web.get("turnstile_solver_api_key"))
export_if_empty("MAINTAINER_TURNSTILE_SOLVER_TIMEOUT_SEC", web.get("turnstile_solver_timeout_sec"))
export_if_empty("MAINTAINER_TURNSTILE_SOLVER_POLL_SEC", web.get("turnstile_solver_poll_sec"))
export_if_empty("MAINTAINER_TURNSTILE_MANUAL_WAIT_SEC", web.get("turnstile_manual_wait_sec"))

worker_domain = os.getenv("MAINTAINER_EMAIL_WORKER_DOMAIN", "").strip() or exports.get("MAINTAINER_EMAIL_WORKER_DOMAIN", "")
email_domains = os.getenv("MAINTAINER_EMAIL_DOMAINS", "").strip() or exports.get("MAINTAINER_EMAIL_DOMAINS", "")
admin_password = os.getenv("MAINTAINER_EMAIL_ADMIN_PASSWORD", "") or exports.get("MAINTAINER_EMAIL_ADMIN_PASSWORD", "")
if not (worker_domain and email_domains and admin_password):
    print("[maintainer] saved WebUI config is missing required email fields", file=sys.stderr)
    sys.exit(1)

with open(env_path, "w", encoding="utf-8") as handle:
    for key in sorted(exports):
        handle.write(f"export {key}={shlex.quote(exports[key])}\n")
os.chmod(env_path, 0o600)
PY
  then
    . "$ENV_FALLBACK_PATH"
    echo "[maintainer] loaded saved WebUI config from $SAVED_WEB_CONFIG"
  fi
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
manual_wait_raw = os.getenv("MAINTAINER_TURNSTILE_MANUAL_WAIT_SEC", "0").strip() or "0"
try:
    manual_wait_value = int(manual_wait_raw)
except ValueError:
    manual_wait_value = manual_wait_raw
solver_provider = os.getenv("MAINTAINER_TURNSTILE_SOLVER_PROVIDER", "").strip().lower()
solver_api_key = (
    os.getenv("MAINTAINER_TURNSTILE_SOLVER_API_KEY", "").strip()
    or os.getenv("CAPSOLVER_API_KEY", "").strip()
    or os.getenv("TWOCAPTCHA_API_KEY", "").strip()
    or os.getenv("TWO_CAPTCHA_API_KEY", "").strip()
    or os.getenv("2CAPTCHA_API_KEY", "").strip()
)
solver_timeout = int(os.getenv("MAINTAINER_TURNSTILE_SOLVER_TIMEOUT_SEC", "150") or "150")
solver_poll = int(os.getenv("MAINTAINER_TURNSTILE_SOLVER_POLL_SEC", "5") or "5")
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
    "web": {
        "turnstile_manual_wait_sec": manual_wait_value,
        "turnstile_solver_provider": solver_provider,
        "turnstile_solver_api_key": solver_api_key,
        "turnstile_solver_timeout_sec": solver_timeout,
        "turnstile_solver_poll_sec": solver_poll,
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
export MAINTAINER_TURNSTILE_MANUAL_WAIT_SEC="${MAINTAINER_TURNSTILE_MANUAL_WAIT_SEC:-0}"
export MAINTAINER_TURNSTILE_SOLVER_PROVIDER="${MAINTAINER_TURNSTILE_SOLVER_PROVIDER:-}"
export MAINTAINER_TURNSTILE_SOLVER_API_KEY="${MAINTAINER_TURNSTILE_SOLVER_API_KEY:-}"
export MAINTAINER_TURNSTILE_SOLVER_TIMEOUT_SEC="${MAINTAINER_TURNSTILE_SOLVER_TIMEOUT_SEC:-150}"
export MAINTAINER_TURNSTILE_SOLVER_POLL_SEC="${MAINTAINER_TURNSTILE_SOLVER_POLL_SEC:-5}"

bootstrap_env_from_saved_config

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
