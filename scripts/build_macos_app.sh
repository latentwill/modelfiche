#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${VERSION:-0.1.0}"
ARCH="$(uname -m)"
BUILD="$ROOT/dist/macos"
APP="$BUILD/Modelfiche.app"
CONTENTS="$APP/Contents"
RESOURCES="$CONTENTS/Resources"
MACOS="$CONTENTS/MacOS"
PYTHON_BASE="$($ROOT/.venv/bin/python -c 'import sys; print(sys.base_prefix)')"

rm -rf "$BUILD"
mkdir -p "$MACOS" "$RESOURCES" "$BUILD/release"

npm run build --prefix "$ROOT/apps/web"
cp -R "$PYTHON_BASE" "$RESOURCES/python"
mkdir -p "$RESOURCES/site-packages"
uv pip install --quiet --target "$RESOURCES/site-packages" "$ROOT"
cp -R "$ROOT/apps/web/dist" "$RESOURCES/web"
cp -R "$ROOT/apps/api/alembic" "$RESOURCES/alembic"

cat > "$MACOS/Modelfiche" <<'LAUNCHER'
#!/bin/bash
set -euo pipefail
CONTENTS="$(cd "$(dirname "$0")/.." && pwd)"
RESOURCES="$CONTENTS/Resources"
PYTHON="$RESOURCES/python/bin/python3"
SUPPORT="$HOME/Library/Application Support/Modelfiche"
LOGS="$HOME/Library/Logs/Modelfiche"
API_PORT="${TITLES_API_PORT:-8400}"
export PYTHONDONTWRITEBYTECODE=1
export TITLES_APP_SUPPORT="$SUPPORT"
mkdir -p "$SUPPORT/assets" "$SUPPORT/cache" "$SUPPORT/exports" "$LOGS"
export PYTHONPATH="$RESOURCES/site-packages"
export TITLES_DATABASE_URL="sqlite:///$SUPPORT/modelfiche.sqlite3"
export TITLES_ASSET_ROOT="$SUPPORT/assets"
export TITLES_CACHE_ROOT="$SUPPORT/cache"
export TITLES_ALEMBIC_SCRIPT_LOCATION="$RESOURCES/alembic"
export TITLES_EXPORT_ROOT="$SUPPORT/exports"
export TITLES_WEB_DIST="$RESOURCES/web"
export TITLES_API_PORT="$API_PORT"
export TITLES_RUN_LOCK="$SUPPORT/runtime.lock"
export TITLES_CLI_PATH="$CONTENTS/MacOS/mfiche"
export TITLES_CORS_ORIGINS="[\"http://127.0.0.1:$API_PORT\",\"http://localhost:$API_PORT\"]"

PIDS=()
cleanup() {
  for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

"$PYTHON" -m titles_api >>"$LOGS/api.log" 2>&1 & PIDS+=("$!")

for _ in $(seq 1 100); do
  if /usr/bin/curl -fsS "http://127.0.0.1:$API_PORT/api/health" >/dev/null 2>&1; then
    "$PYTHON" -m titles_worker.main >>"$LOGS/worker.log" 2>&1 & PIDS+=("$!")
    "$PYTHON" -m titles_api.wandb_ingress_main >>"$LOGS/wandb-ingress.log" 2>&1 & PIDS+=("$!")
    /usr/bin/open "http://127.0.0.1:$API_PORT/"
    break
  fi
  sleep 0.1
done

wait "${PIDS[0]}"
LAUNCHER
chmod +x "$MACOS/Modelfiche"

cat > "$MACOS/mfiche" <<'CLI'
#!/bin/bash
set -euo pipefail
CONTENTS="$(cd "$(dirname "$0")/.." && pwd)"
RESOURCES="$CONTENTS/Resources"
API_PORT="${TITLES_API_PORT:-8400}"
SUPPORT="$HOME/Library/Application Support/Modelfiche"
export PYTHONDONTWRITEBYTECODE=1
export TITLES_APP_SUPPORT="$SUPPORT"
export PYTHONPATH="$RESOURCES/site-packages"
export TITLES_DATABASE_URL="sqlite:///$SUPPORT/modelfiche.sqlite3"
export TITLES_ASSET_ROOT="$SUPPORT/assets"
export TITLES_CACHE_ROOT="$SUPPORT/cache"
export TITLES_CLI_ORIGIN="http://127.0.0.1:$API_PORT"
export TITLES_EXPORT_ROOT="$SUPPORT/exports"
exec "$RESOURCES/python/bin/python3" -m titles_cli.main "$@"
CLI
chmod +x "$MACOS/mfiche"

cat > "$CONTENTS/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Modelfiche</string>
  <key>CFBundleDisplayName</key><string>Modelfiche</string>
  <key>CFBundleIdentifier</key><string>com.latentwill.modelfiche</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleExecutable</key><string>Modelfiche</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
find "$APP" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$APP" -type f -name '*.pyc' -delete


codesign --force --deep --sign - "$APP"
/usr/bin/ditto -c -k --sequesterRsrc --keepParent "$APP" "$BUILD/release/Modelfiche-$VERSION-macos-$ARCH.zip"
mkdir -p "$BUILD/dmg"
cp -R "$APP" "$BUILD/dmg/Modelfiche.app"
ln -s /Applications "$BUILD/dmg/Applications"
hdiutil create -quiet -volname "Modelfiche $VERSION" -srcfolder "$BUILD/dmg" -ov -format UDZO "$BUILD/release/Modelfiche-$VERSION-macos-$ARCH.dmg"
(
  cd "$BUILD/release"
  shasum -a 256 ./* > "Modelfiche-$VERSION-SHA256SUMS.txt"
)
printf 'Built %s\n' "$BUILD/release"
