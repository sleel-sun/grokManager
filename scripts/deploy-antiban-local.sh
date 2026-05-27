#!/usr/bin/env bash
set -euo pipefail

APP_NAME="${APP_NAME:-grokManager}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

APP_PATH="${GROKMANAGER_APP_PATH:-}"
PREFIX="${ANTI_BAN_PREFIX:-}"
PROXY_URL="${ANTI_BAN_PROXY_URL:-http://127.0.0.1:${ANTI_BAN_WARP_PROXY_PORT:-40000}}"
FLARESOLVERR_URL="${ANTI_BAN_FLARESOLVERR_URL:-http://127.0.0.1:${ANTI_BAN_FLARESOLVERR_PORT:-8191}}"
SERVER_HOST="${SERVER_HOST:-127.0.0.1}"
SERVER_PORT="${SERVER_PORT:-8000}"
SERVER_WORKERS="${SERVER_WORKERS:-1}"
LAUNCH=1

usage() {
  cat <<'EOF'
Usage: deploy-antiban-local.sh [options]

Non-Docker anti-ban bootstrap for grokManager.

Options:
  --prefix DIR              Deployment directory for .env, data, logs and runner.
  --app PATH                Path to grokManager.app. When set, the script launches the app.
  --proxy-url URL           Local WARP/proxy URL. Default: http://127.0.0.1:40000
  --flaresolverr-url URL    Local FlareSolverr URL. Default: http://127.0.0.1:8191
  --server-port PORT        grokManager listen port. Default: 8000
  --configure-only          Write local anti-ban config and runner without launching.
  --launch                  Write config and launch. This is the default.
  -h, --help                Show this help.

Useful environment variables:
  FLARESOLVERR_BIN          Optional FlareSolverr executable to start automatically.
  ANTI_BAN_SKIP_WARP_CONFIG Set to 1 to skip warp-cli configuration.
  ANTI_BAN_SKIP_PORT_CHECK  Set to 1 to skip local port readiness checks.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix)
      PREFIX="$2"
      shift 2
      ;;
    --app)
      APP_PATH="$2"
      shift 2
      ;;
    --proxy-url)
      PROXY_URL="$2"
      shift 2
      ;;
    --flaresolverr-url)
      FLARESOLVERR_URL="$2"
      shift 2
      ;;
    --server-port)
      SERVER_PORT="$2"
      shift 2
      ;;
    --configure-only)
      LAUNCH=0
      shift
      ;;
    --launch)
      LAUNCH=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$PREFIX" ]]; then
  if [[ -n "$APP_PATH" && "$(uname -s)" == "Darwin" ]]; then
    PREFIX="${HOME}/Library/Application Support/${APP_NAME}"
  else
    PREFIX="${ROOT_DIR}/.antiban"
  fi
fi

DATA_DIR="${ANTI_BAN_DATA_DIR:-${PREFIX}/data}"
LOG_DIR="${ANTI_BAN_LOG_DIR:-${PREFIX}/logs}"
ENV_FILE="${PREFIX}/.env"
RUNNER="${PREFIX}/run-grokmanager-antiban.sh"

env_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

write_env_line() {
  local key="$1"
  local value="$2"
  if [[ "$value" =~ [[:space:]] ]]; then
    printf '%s="%s"\n' "$key" "$(env_escape "$value")"
  else
    printf '%s=%s\n' "$key" "$value"
  fi
}

extract_host() {
  local value="${1#*://}"
  value="${value##*@}"
  value="${value%%/*}"
  value="${value%%:*}"
  printf '%s' "${value:-127.0.0.1}"
}

extract_port() {
  local value="${1#*://}"
  value="${value##*@}"
  value="${value%%/*}"
  local port="${value##*:}"
  if [[ "$port" =~ ^[0-9]+$ ]]; then
    printf '%s' "$port"
  fi
}

port_open() {
  local host="$1"
  local port="$2"
  [[ -n "$host" && -n "$port" ]] || return 1
  command -v nc >/dev/null 2>&1 || return 1
  nc -z "$host" "$port" >/dev/null 2>&1
}

maybe_configure_warp_proxy() {
  [[ "${ANTI_BAN_SKIP_WARP_CONFIG:-0}" == "1" ]] && return 0

  local port
  port="$(extract_port "$PROXY_URL")"
  [[ -n "$port" ]] || return 0

  if ! command -v warp-cli >/dev/null 2>&1; then
    echo "warp-cli not found; install Cloudflare WARP and enable local proxy on ${port}."
    return 0
  fi

  warp-cli set-mode proxy >/dev/null 2>&1 || warp-cli mode proxy >/dev/null 2>&1 || true
  warp-cli set-proxy-port "$port" >/dev/null 2>&1 || warp-cli proxy port "$port" >/dev/null 2>&1 || true
  warp-cli connect >/dev/null 2>&1 || true
  echo "Cloudflare WARP local proxy requested on port ${port}."
}

maybe_start_flaresolverr() {
  local host port bin
  host="$(extract_host "$FLARESOLVERR_URL")"
  port="$(extract_port "$FLARESOLVERR_URL")"
  [[ -n "$port" ]] || return 0

  if port_open "$host" "$port"; then
    echo "FlareSolverr already listening at ${FLARESOLVERR_URL}."
    return 0
  fi

  bin="${FLARESOLVERR_BIN:-}"
  if [[ -z "$bin" ]]; then
    bin="$(command -v flaresolverr 2>/dev/null || true)"
  fi
  if [[ -z "$bin" || ! -x "$bin" ]]; then
    echo "FlareSolverr is not running; set FLARESOLVERR_BIN or start it at ${FLARESOLVERR_URL}."
    return 0
  fi

  mkdir -p "$LOG_DIR"
  HOST="$host" PORT="$port" LOG_LEVEL="${FLARESOLVERR_LOG_LEVEL:-info}" PROXY_URL="$PROXY_URL" \
    nohup "$bin" >"${LOG_DIR}/flaresolverr.log" 2>&1 &
  echo "Started FlareSolverr with pid $!."
}

check_local_ports() {
  [[ "${ANTI_BAN_SKIP_PORT_CHECK:-0}" == "1" ]] && return 0

  local proxy_host proxy_port fs_host fs_port
  proxy_host="$(extract_host "$PROXY_URL")"
  proxy_port="$(extract_port "$PROXY_URL")"
  fs_host="$(extract_host "$FLARESOLVERR_URL")"
  fs_port="$(extract_port "$FLARESOLVERR_URL")"

  if [[ -n "$proxy_port" ]] && ! port_open "$proxy_host" "$proxy_port"; then
    echo "Warning: proxy ${PROXY_URL} is not reachable yet."
  fi
  if [[ -n "$fs_port" ]] && ! port_open "$fs_host" "$fs_port"; then
    echo "Warning: FlareSolverr ${FLARESOLVERR_URL} is not reachable yet."
  fi
}

write_environment() {
  mkdir -p "$PREFIX" "$DATA_DIR" "$LOG_DIR"
  {
    write_env_line DATA_DIR "$DATA_DIR"
    write_env_line LOG_DIR "$LOG_DIR"
    write_env_line SERVER_HOST "$SERVER_HOST"
    write_env_line SERVER_PORT "$SERVER_PORT"
    write_env_line SERVER_WORKERS "$SERVER_WORKERS"
    write_env_line GROK_PROXY_EGRESS_MODE "single_proxy"
    write_env_line GROK_PROXY_EGRESS_PROXY_URL "$PROXY_URL"
    write_env_line GROK_PROXY_EGRESS_RESOURCE_PROXY_URL "$PROXY_URL"
    write_env_line GROK_PROXY_CLEARANCE_MODE "flaresolverr"
    write_env_line GROK_PROXY_CLEARANCE_FLARESOLVERR_URL "$FLARESOLVERR_URL"
    write_env_line GROK_PROXY_CLEARANCE_REFRESH_INTERVAL "600"
    write_env_line GROK_PROXY_CLEARANCE_TIMEOUT_SEC "60"
    write_env_line MAINTAINER_PROXY "$PROXY_URL"
  } >"$ENV_FILE"
  chmod 600 "$ENV_FILE" || true
}

write_runner() {
  local env_q root_q app_q
  printf -v env_q '%q' "$ENV_FILE"
  printf -v root_q '%q' "$ROOT_DIR"
  printf -v app_q '%q' "$APP_PATH"

  cat >"$RUNNER" <<EOF
#!/usr/bin/env bash
set -euo pipefail

ENV_FILE=${env_q}
ROOT_DIR=${root_q}
APP_PATH=${app_q}

set -a
. "\$ENV_FILE"
set +a

if [[ -n "\$APP_PATH" && -d "\$APP_PATH" ]]; then
  exec open "\$APP_PATH"
fi

cd "\$ROOT_DIR"
exec uv run granian --interface asgi --host "\${SERVER_HOST:-127.0.0.1}" --port "\${SERVER_PORT:-8000}" --workers "\${SERVER_WORKERS:-1}" app.main:app
EOF
  chmod +x "$RUNNER"
}

write_environment
write_runner

maybe_configure_warp_proxy
maybe_start_flaresolverr
check_local_ports

echo "Anti-ban local environment: ${ENV_FILE}"
echo "Reusable launcher: ${RUNNER}"
echo "Proxy URL: ${PROXY_URL}"
echo "FlareSolverr URL: ${FLARESOLVERR_URL}"

if [[ "$LAUNCH" -eq 1 ]]; then
  exec "$RUNNER"
fi
