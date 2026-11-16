#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

APP_NAME="CodexAgySync"
BUNDLE_ID="dev.semanticcraft.codex-agy-sync"
OUTPUT_DIR=""
ARCHIVE_ZIP=""
INSTALL=0

usage() {
    cat <<'EOF'
Usage: build_menubar_app.sh [options]

Options:
  --output-dir <dir>    Target directory for CodexAgySync.app (default: $REPO_ROOT/out/app)
  --bundle-id <id>      CFBundleIdentifier (default: dev.semanticcraft.codex-agy-sync)
  --archive-zip <path>  Package signed .app into <path> via ditto and write <path>.sha256
  --install             Output to $HOME/Applications and register with LaunchServices
  -h, --help            Show this help text
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
        --archive-zip)
            [[ $# -ge 2 ]] || { echo "Error: --archive-zip requires an argument" >&2; exit 1; }
            ARCHIVE_ZIP="$2"
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

validate_non_tmp_path() {
    local candidate="$1"
    local label="$2"
    if [[ "$candidate" == "/tmp" || "$candidate" == /tmp/* || "$candidate" == "/private/tmp" || "$candidate" == /private/tmp/* ]]; then
        echo "Error: refusing /tmp or /private/tmp $label: $candidate" >&2
        exit 1
    fi
    local resolved
    resolved="$(python3 -c 'import os, sys; print(os.path.realpath(os.path.abspath(sys.argv[1])))' "$candidate")"
    if [[ "$resolved" == "/tmp" || "$resolved" == /tmp/* || "$resolved" == "/private/tmp" || "$resolved" == /private/tmp/* ]]; then
        echo "Error: refusing resolved /tmp or /private/tmp $label: $resolved" >&2
        exit 1
    fi
}

validate_non_tmp_path "$OUTPUT_DIR" "output directory"
if [[ -n "$ARCHIVE_ZIP" ]]; then
    validate_non_tmp_path "$ARCHIVE_ZIP" "archive zip path"
fi

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "Error: build_menubar_app.sh requires macOS (Darwin)." >&2
    exit 1
fi

command -v swift >/dev/null 2>&1 || { echo "Error: swift toolchain not found in PATH." >&2; exit 1; }

APP_VERSION="$(python3 -c '
import pathlib, re, sys
text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
match = re.search(r"^version\s*=\s*\"([^\"]+)\"", text, re.M)
if not match:
    raise SystemExit("Error: missing version in pyproject.toml")
print(match.group(1))
' "$REPO_ROOT/pyproject.toml")"

SCRATCH_PATH="$REPO_ROOT/out/swift-build"
ICON_SCRATCH_PATH="$REPO_ROOT/out/app-icon"
mkdir -p "$OUTPUT_DIR" "$SCRATCH_PATH" "$ICON_SCRATCH_PATH"

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
STAGE_DIR="$(mktemp -d "$SCRATCH_PATH/bundle-stage.XXXXXX")"
DEST_STAGE="$OUTPUT_DIR/.${APP_NAME}.app.stage.$$"
cleanup_stage() {
    rm -rf "$STAGE_DIR" "$DEST_STAGE"
}
trap cleanup_stage EXIT

STAGE_APP="$STAGE_DIR/$APP_NAME.app"
CONTENTS_DIR="$STAGE_APP/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
RESOURCES_DIR="$CONTENTS_DIR/Resources"
PLIST_PATH="$CONTENTS_DIR/Info.plist"
ICNS_PATH="$RESOURCES_DIR/AppIcon.icns"

mkdir -p "$MACOS_DIR" "$RESOURCES_DIR"
cp "$BIN_PATH" "$MACOS_DIR/$APP_NAME"
chmod 755 "$MACOS_DIR/$APP_NAME"

swift "$REPO_ROOT/scripts/generate_app_icon.swift" "$ICNS_PATH" "$ICON_SCRATCH_PATH"

python3 - "$PLIST_PATH" "$BUNDLE_ID" "$APP_NAME" "$APP_VERSION" <<'PY'
import plistlib
import sys

plist_path, bundle_id, app_name, app_version = sys.argv[1:5]
payload = {
    "CFBundleDevelopmentRegion": "zh_CN",
    "CFBundleDisplayName": app_name,
    "CFBundleExecutable": app_name,
    "CFBundleIconFile": "AppIcon",
    "CFBundleIconName": "AppIcon",
    "CFBundleIdentifier": bundle_id,
    "CFBundleInfoDictionaryVersion": "6.0",
    "CFBundleLocalizations": ["zh-Hans", "en"],
    "CFBundleName": app_name,
    "CFBundlePackageType": "APPL",
    "CFBundleShortVersionString": app_version,
    "CFBundleVersion": app_version,
    "LSMinimumSystemVersion": "14.0",
    "LSUIElement": True,
    "NSHighResolutionCapable": True,
}
with open(plist_path, "wb") as handle:
    plistlib.dump(payload, handle)
PY

codesign --force --deep --sign - "$STAGE_APP"
codesign --verify --deep --strict "$STAGE_APP"

rm -rf "$DEST_STAGE"
/usr/bin/ditto "$STAGE_APP" "$DEST_STAGE"
codesign --verify --deep --strict "$DEST_STAGE"

swift -e '
import Foundation
let fm = FileManager.default
let staged = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let target = URL(fileURLWithPath: CommandLine.arguments[2], isDirectory: true)
if fm.fileExists(atPath: target.path) {
    _ = try fm.replaceItemAt(target, withItemAt: staged)
} else {
    try fm.moveItem(at: staged, to: target)
}
' "$DEST_STAGE" "$APP_BUNDLE"

if [[ -n "$ARCHIVE_ZIP" ]]; then
    ARCHIVE_ZIP_ABS="$(python3 -c 'import os, sys; print(os.path.abspath(sys.argv[1]))' "$ARCHIVE_ZIP")"
    ARCHIVE_DIR="$(dirname "$ARCHIVE_ZIP_ABS")"
    ARCHIVE_BASE="$(basename "$ARCHIVE_ZIP_ABS")"
    ARCHIVE_TMP="$ARCHIVE_DIR/.${ARCHIVE_BASE}.tmp.$$"
    mkdir -p "$ARCHIVE_DIR"
    rm -f "$ARCHIVE_TMP"
    /usr/bin/ditto -c -k --sequesterRsrc --keepParent "$APP_BUNDLE" "$ARCHIVE_TMP"
    mv -f "$ARCHIVE_TMP" "$ARCHIVE_ZIP_ABS"
    (
        cd "$ARCHIVE_DIR"
        shasum -a 256 "$ARCHIVE_BASE"
    ) > "$ARCHIVE_ZIP_ABS.sha256"
fi

if [[ "$INSTALL" -eq 1 ]]; then
    LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
    if [[ -x "$LSREGISTER" ]]; then
        "$LSREGISTER" -f "$APP_BUNDLE" >/dev/null 2>&1 || true
    fi
fi

echo "$APP_BUNDLE"
