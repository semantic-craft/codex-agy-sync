#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

APP_NAME="CodexAgyHandoff"
BUNDLE_ID="dev.semanticcraft.codex-agy-handoff"
OUTPUT_DIR=""
INSTALL=0

usage() {
    cat <<'EOF'
Usage: build_menubar_app.sh [options]

Options:
  --output-dir <dir>   Target directory for CodexAgyHandoff.app (default: $REPO_ROOT/out/app)
  --bundle-id <id>     CFBundleIdentifier (default: dev.semanticcraft.codex-agy-handoff)
  --install            Output to $HOME/Applications and register with LaunchServices
  -h, --help           Show this help text
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --output-dir)
            [[ $# -ge 2 ]] || { echo "Error: --output-dir requires an argument" >&2; exit 1; }
            OUTPUT_DIR="$2"
            shift 2
            ;;
        --bundle-id)
            [[ $# -ge 2 ]] || { echo "Error: --bundle-id requires an argument" >&2; exit 1; }
            BUNDLE_ID="$2"
            shift 2
            ;;
        --install)
            INSTALL=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Error: Unknown option '$1'" >&2
            usage >&2
            exit 1
            ;;
    esac
done

if [[ -z "$OUTPUT_DIR" ]]; then
    if [[ "$INSTALL" -eq 1 ]]; then
        OUTPUT_DIR="$HOME/Applications"
    else
        OUTPUT_DIR="$REPO_ROOT/out/app"
    fi
fi

validate_output_dir() {
    local candidate="$1"
    if [[ "$candidate" == "/tmp" || "$candidate" == /tmp/* || "$candidate" == "/private/tmp" || "$candidate" == /private/tmp/* ]]; then
        echo "Error: refusing /tmp or /private/tmp output directory: $candidate" >&2
        exit 1
    fi
    local resolved
    resolved="$(python3 -c 'import os, sys; print(os.path.realpath(os.path.abspath(sys.argv[1])))' "$candidate")"
    if [[ "$resolved" == "/tmp" || "$resolved" == /tmp/* || "$resolved" == "/private/tmp" || "$resolved" == /private/tmp/* ]]; then
        echo "Error: refusing resolved /tmp or /private/tmp output directory: $resolved" >&2
        exit 1
    fi
}

validate_output_dir "$OUTPUT_DIR"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "Error: build_menubar_app.sh requires macOS (Darwin)." >&2
    exit 1
fi

command -v swift >/dev/null 2>&1 || { echo "Error: swift toolchain not found in PATH." >&2; exit 1; }

SCRATCH_PATH="$REPO_ROOT/out/swift-build"
mkdir -p "$OUTPUT_DIR" "$SCRATCH_PATH"

swift build \
    --package-path "$REPO_ROOT/macos" \
    -c release \
    --scratch-path "$SCRATCH_PATH" \
    --product "$APP_NAME"

BIN_DIR="$(swift build \
    --package-path "$REPO_ROOT/macos" \
    -c release \
    --scratch-path "$SCRATCH_PATH" \
    --show-bin-path)"
BIN_PATH="$BIN_DIR/$APP_NAME"

if [[ ! -x "$BIN_PATH" ]]; then
    echo "Error: built binary missing at $BIN_PATH" >&2
    exit 1
fi

APP_BUNDLE="$OUTPUT_DIR/$APP_NAME.app"
CONTENTS_DIR="$APP_BUNDLE/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
PLIST_PATH="$CONTENTS_DIR/Info.plist"

rm -rf "$APP_BUNDLE"
mkdir -p "$MACOS_DIR"
cp "$BIN_PATH" "$MACOS_DIR/$APP_NAME"
chmod 755 "$MACOS_DIR/$APP_NAME"

python3 - "$PLIST_PATH" "$BUNDLE_ID" "$APP_NAME" <<'PY'
import plistlib
import sys

plist_path, bundle_id, app_name = sys.argv[1:4]
payload = {
    "CFBundleDevelopmentRegion": "zh_CN",
    "CFBundleDisplayName": app_name,
    "CFBundleExecutable": app_name,
    "CFBundleIdentifier": bundle_id,
    "CFBundleInfoDictionaryVersion": "6.0",
    "CFBundleName": app_name,
    "CFBundlePackageType": "APPL",
    "CFBundleShortVersionString": "0.3.0",
    "CFBundleVersion": "1",
    "LSMinimumSystemVersion": "14.0",
    "LSUIElement": True,
    "NSHighResolutionCapable": True,
}
with open(plist_path, "wb") as handle:
    plistlib.dump(payload, handle)
PY

codesign --force --deep --sign - "$APP_BUNDLE"
codesign --verify --deep --strict "$APP_BUNDLE"

if [[ "$INSTALL" -eq 1 ]]; then
    LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
    if [[ -x "$LSREGISTER" ]]; then
        "$LSREGISTER" -f "$APP_BUNDLE" >/dev/null 2>&1 || true
    fi
fi

echo "$APP_BUNDLE"
