#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
export COPYFILE_DISABLE=1

APP_NAME="${APP_NAME:-grokManager}"
BUNDLE_ID="${BUNDLE_ID:-com.grokmanager.gateway}"
PACKAGE_ROOT="${PACKAGE_ROOT:-/private/tmp/${APP_NAME}-macos-package}"
ENTRYPOINT="${ROOT_DIR}/scripts/macos_launcher.py"
SPEC_DIR="${PACKAGE_ROOT}/spec"
WORK_DIR="${PACKAGE_ROOT}/work"
BUILD_DIST_DIR="${PACKAGE_ROOT}/dist"
DIST_DIR="${ROOT_DIR}/dist"
ARCH_NAME="$(uname -m)"
ZIP_PATH="${DIST_DIR}/${APP_NAME}-macos-${ARCH_NAME}.zip"

SIGN=1
ZIP=1
MAINTAINER=1

for arg in "$@"; do
  case "$arg" in
    --no-sign)
      SIGN=0
      ;;
    --no-zip)
      ZIP=0
      ;;
    --without-maintainer)
      MAINTAINER=0
      ;;
    *)
      echo "Unknown option: $arg" >&2
      exit 2
      ;;
  esac
done

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required to build the macOS package." >&2
  exit 1
fi

mkdir -p "$SPEC_DIR" "$WORK_DIR" "$BUILD_DIST_DIR" "$DIST_DIR"

SYNC_ARGS=(sync)
PYINSTALLER_ARGS=(
  --noconfirm
  --clean
  --windowed
  --name "$APP_NAME"
  --osx-bundle-identifier "$BUNDLE_ID"
  --specpath "$SPEC_DIR"
  --workpath "$WORK_DIR"
  --distpath "$BUILD_DIST_DIR"
  --paths "$ROOT_DIR"
  --add-data "${ROOT_DIR}/app/statics:app/statics"
  --add-data "${ROOT_DIR}/config.defaults.toml:."
  --add-data "${ROOT_DIR}/pyproject.toml:."
  --collect-all granian
  --collect-all curl_cffi
  --collect-all tiktoken
  --collect-data certifi
  --hidden-import app.main
  --hidden-import app.control.account.backends.local
  --hidden-import app.control.account.backends.redis
  --hidden-import app.control.account.backends.sql
  --hidden-import app.platform.config.backends.toml
  --hidden-import app.platform.config.backends.redis
  --hidden-import app.platform.config.backends.sql
)

if [[ "$MAINTAINER" -eq 1 ]]; then
  SYNC_ARGS+=(--extra maintainer)
  PYINSTALLER_ARGS+=(
    --add-data "${ROOT_DIR}/app/maintainer/turnstilePatch:app/maintainer/turnstilePatch"
    --collect-all DrissionPage
    --collect-all DataRecorder
    --collect-all DownloadKit
    --collect-all tldextract
    --hidden-import app.maintainer.runner
    --hidden-import pyvirtualdisplay
  )
fi

uv "${SYNC_ARGS[@]}"

uv run --with pyinstaller pyinstaller \
  "${PYINSTALLER_ARGS[@]}" \
  "$ENTRYPOINT"

APP_PATH="${BUILD_DIST_DIR}/${APP_NAME}.app"
STALE_APP_PATH="${DIST_DIR}/${APP_NAME}.app"

if [[ ! -d "$APP_PATH" ]]; then
  echo "Build failed: $APP_PATH was not created." >&2
  exit 1
fi

if [[ "$SIGN" -eq 1 ]]; then
  xattr -cr "$APP_PATH" 2>/dev/null || true
  xattr -d -r com.apple.FinderInfo "$APP_PATH" 2>/dev/null || true
  xattr -d -r com.apple.ResourceFork "$APP_PATH" 2>/dev/null || true
  codesign --force --deep --sign - "$APP_PATH"
  codesign --verify --deep --strict "$APP_PATH"
fi

if [[ "$ZIP" -eq 1 ]]; then
  rm -f "$ZIP_PATH"
  (
    cd "$(dirname "$APP_PATH")"
    COPYFILE_DISABLE=1 /usr/bin/zip -qry "$ZIP_PATH" "$(basename "$APP_PATH")"
  )
fi

if [[ -e "$STALE_APP_PATH" && "$STALE_APP_PATH" != "$APP_PATH" ]]; then
  rm -rf "$STALE_APP_PATH"
fi

echo "macOS app: $APP_PATH"
if [[ "$ZIP" -eq 1 ]]; then
  echo "archive: $ZIP_PATH"
fi
