#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

APP_NAME="CodexAgySync"
BUNDLE_ID="dev.semanticcraft.codex-agy-sync"
DISPLAY_NAME=""
OUTPUT_DIR=""
ARCHIVE_ZIP=""
INSTALL=0
ADHOC=0
NOTARIZE=0
RELEASE=0
SKIP_LSREGISTER="${CODEX_AGY_SKIP_LSREGISTER:-0}"
SIGN_IDENTITY="${CODESIGN_IDENTITY:-}"
SIGN_IDENTITY_CLI_SET=0
NOTARY_PROFILE="${NOTARY_PROFILE:-}"

usage() {
    cat <<'EOF'
Usage: build_menubar_app.sh [options]

Options:
  --output-dir <dir>        Target directory for CodexAgySync.app (default: $REPO_ROOT/out/app)
  --bundle-id <id>          CFBundleIdentifier (default: dev.semanticcraft.codex-agy-sync)
  --display-name <name>     CFBundleDisplayName / CFBundleName (default: CodexAgySync)
  --archive-zip <path>      Package signed .app into <path> via ditto and write <path>.sha256
  --install                 Output to $HOME/Applications and register with LaunchServices
  --skip-lsregister         Skip LaunchServices registration (for isolated build/package tests)
  --adhoc                   Force ad-hoc signing (--sign -) for local dev/QA builds
  --sign-identity <id>      Explicit Developer ID Application signing identity (or CODESIGN_IDENTITY env)
  --notarize                Sign with Developer ID + hardened runtime + timestamp and notarize
  --release                 Implies --notarize and writes zipped release artifact + SHA256SUMS.txt
  --notary-profile <name>   Notary keychain profile name (or NOTARY_PROFILE / macos-notary-profile)
  -h, --help                Show this help text
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
        --display-name)
            [[ $# -ge 2 ]] || { echo "Error: --display-name requires an argument" >&2; exit 1; }
            DISPLAY_NAME="$2"
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
        --skip-lsregister)
            SKIP_LSREGISTER=1
            shift
            ;;
        --adhoc)
            ADHOC=1
            shift
            ;;
        --sign-identity)
            [[ $# -ge 2 ]] || { echo "Error: --sign-identity requires an argument" >&2; exit 1; }
            SIGN_IDENTITY="$2"
            SIGN_IDENTITY_CLI_SET=1
            shift 2
            ;;
        --notarize)
            NOTARIZE=1
            shift
            ;;
        --release)
            RELEASE=1
            NOTARIZE=1
            shift
            ;;
        --notary-profile)
            [[ $# -ge 2 ]] || { echo "Error: --notary-profile requires an argument" >&2; exit 1; }
            NOTARY_PROFILE="$2"
            shift 2
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

if [[ "$ADHOC" -eq 1 && "$NOTARIZE" -eq 1 ]]; then
    echo "Error: --adhoc cannot be combined with --notarize or --release." >&2
    exit 1
fi

if [[ "$ADHOC" -eq 1 && "$SIGN_IDENTITY_CLI_SET" -eq 1 && "$SIGN_IDENTITY" != "-" ]]; then
    echo "Error: --adhoc cannot be combined with --sign-identity '$SIGN_IDENTITY'." >&2
    exit 1
fi

USER_SPECIFIED_OUTPUT_DIR=0
if [[ -n "$OUTPUT_DIR" ]]; then
    USER_SPECIFIED_OUTPUT_DIR=1
else
    if [[ "$INSTALL" -eq 1 ]]; then
        OUTPUT_DIR="$HOME/Applications"
    elif [[ "$RELEASE" -eq 1 ]]; then
        OUTPUT_DIR="${OUT_DIR:-$REPO_ROOT/out/release}"
    else
        OUTPUT_DIR="$REPO_ROOT/out/app"
    fi
fi

if [[ "$RELEASE" -eq 1 && -z "$ARCHIVE_ZIP" ]]; then
    if [[ "$INSTALL" -eq 0 && "$USER_SPECIFIED_OUTPUT_DIR" -eq 1 ]]; then
        ARCHIVE_ZIP="$OUTPUT_DIR/${APP_NAME}-macos.zip"
    else
        ARCHIVE_ZIP="${OUT_DIR:-$REPO_ROOT/out/release}/${APP_NAME}-macos.zip"
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

resolve_cargo_bin() {
    if command -v cargo >/dev/null 2>&1; then
        command -v cargo
        return 0
    fi
    local cand
    for cand in "$HOME/.cargo/bin/cargo" "/opt/homebrew/bin/cargo" "/usr/local/bin/cargo"; do
        if [[ -x "$cand" ]]; then
            printf '%s\n' "$cand"
            return 0
        fi
    done
    return 1
}

CARGO_BIN="$(resolve_cargo_bin || true)"
if [[ -z "$CARGO_BIN" ]]; then
    echo "Error: cargo toolchain not found in PATH or standard locations (\$HOME/.cargo/bin, /opt/homebrew/bin, /usr/local/bin)." >&2
    exit 1
fi
CARGO_BIN_DIR="$(dirname "$CARGO_BIN")"
case ":$PATH:" in
    *":$CARGO_BIN_DIR:"*) ;;
    *) export PATH="$CARGO_BIN_DIR:$PATH" ;;
esac

discover_developer_id_identity() {
    local line
    line="$(security find-identity -v -p codesigning 2>/dev/null | grep 'Developer ID Application:' | head -n 1 || true)"
    if [[ -n "$line" ]]; then
        if [[ "$line" =~ \"([^\"]*Developer\ ID\ Application:[^\"]*)\" ]]; then
            printf '%s\n' "${BASH_REMATCH[1]}"
            return 0
        fi
        local hash
        hash="$(printf '%s\n' "$line" | awk '{print $2}')"
        if [[ -n "$hash" ]]; then
            printf '%s\n' "$hash"
            return 0
        fi
    fi
    return 1
}

resolve_notary_profile() {
    if [[ -n "$NOTARY_PROFILE" ]]; then
        printf '%s\n' "$NOTARY_PROFILE"
        return 0
    fi
    local profile_cmd=""
    if command -v macos-notary-profile >/dev/null 2>&1; then
        profile_cmd="$(command -v macos-notary-profile)"
    elif [[ -x "$HOME/.local/bin/macos-notary-profile" ]]; then
        profile_cmd="$HOME/.local/bin/macos-notary-profile"
    else
        echo "Error: NOTARY_PROFILE is not set and macos-notary-profile was not found in PATH or \$HOME/.local/bin." >&2
        return 1
    fi
    local resolved
    resolved="$("$profile_cmd")" || {
        echo "Error: Failed to resolve notary profile via $profile_cmd." >&2
        return 1
    }
    resolved="$(printf '%s' "$resolved" | tr -d '\r' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    if [[ -z "$resolved" ]]; then
        echo "Error: $profile_cmd returned an empty notary profile." >&2
        return 1
    fi
    printf '%s\n' "$resolved"
}

notarize_and_verify_stage_app() {
    local app_path="$1"
    local notary_bin=""
    if command -v macos-notary >/dev/null 2>&1; then
        notary_bin="$(command -v macos-notary)"
    elif [[ -x "$HOME/.local/bin/macos-notary" ]]; then
        notary_bin="$HOME/.local/bin/macos-notary"
    fi

    if [[ -n "$notary_bin" ]]; then
        "$notary_bin" submit "$app_path" --profile "$NOTARY_PROFILE"
    else
        local submit_zip="$STAGE_DIR/${APP_NAME}-notarize.zip"
        rm -f "$submit_zip"
        /usr/bin/ditto -c -k --sequesterRsrc --keepParent "$app_path" "$submit_zip"
        local submit_json
        submit_json="$(xcrun notarytool submit "$submit_zip" --keychain-profile "$NOTARY_PROFILE" --wait --output-format json)" || {
            echo "Error: xcrun notarytool submit failed for $app_path." >&2
            return 1
        }
        python3 - "$submit_json" <<'PY'
import json, sys
payload = json.loads(sys.argv[1])
if not isinstance(payload, dict) or payload.get("status") != "Accepted":
    raise SystemExit(f"Error: notarization was not accepted: {payload}")
PY
        xcrun stapler staple "$app_path"
        xcrun stapler validate "$app_path"
    fi

    local spctl_out=""
    spctl_out="$(spctl --assess --type execute --verbose=4 "$app_path" 2>&1)" || {
        echo "Error: spctl Gatekeeper assessment failed for $app_path:" >&2
        echo "$spctl_out" >&2
        return 1
    }
    if [[ "$spctl_out" != *"Notarized Developer ID"* ]]; then
        echo "Error: spctl did not report 'Notarized Developer ID' for $app_path:" >&2
        echo "$spctl_out" >&2
        return 1
    fi
}

SIGN_MODE=""
if [[ "$ADHOC" -eq 1 ]]; then
    SIGN_MODE="adhoc"
    SIGN_IDENTITY="-"
elif [[ "$SIGN_IDENTITY" == "-" ]]; then
    if [[ "$NOTARIZE" -eq 1 ]]; then
        echo "Error: ad-hoc signing ('-') cannot be used with --notarize or --release." >&2
        exit 1
    fi
    SIGN_MODE="adhoc"
elif [[ -n "$SIGN_IDENTITY" ]]; then
    SIGN_MODE="developer_id"
else
    SIGN_IDENTITY="$(discover_developer_id_identity || true)"
    if [[ -n "$SIGN_IDENTITY" ]]; then
        SIGN_MODE="developer_id"
    elif [[ "$NOTARIZE" -eq 1 || "$INSTALL" -eq 1 ]]; then
        echo "Error: No 'Developer ID Application:' codesigning identity found (set CODESIGN_IDENTITY, install a Developer ID certificate, or pass --adhoc explicitly for local dev)." >&2
        exit 1
    else
        SIGN_MODE="adhoc"
        SIGN_IDENTITY="-"
        echo "Note: No Developer ID Application identity found; using ad-hoc dev signing (--sign -)." >&2
    fi
fi

if [[ "$NOTARIZE" -eq 1 ]]; then
    NOTARY_PROFILE="$(resolve_notary_profile)"
fi

APP_VERSION="$(python3 -c '
import pathlib, re, sys
py_text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
py_match = re.search(r"^version\s*=\s*\"([^\"]+)\"", py_text, re.M)
if not py_match:
    raise SystemExit("Error: missing version in pyproject.toml")
py_ver = py_match.group(1)
cargo_text = pathlib.Path(sys.argv[2]).read_text(encoding="utf-8")
cargo_match = re.search(r"^version\s*=\s*\"([^\"]+)\"", cargo_text, re.M)
if not cargo_match:
    raise SystemExit("Error: missing version in rust/Cargo.toml")
cargo_ver = cargo_match.group(1)
if cargo_ver != py_ver:
    raise SystemExit(f"Error: version mismatch between pyproject.toml ({py_ver}) and rust/Cargo.toml ({cargo_ver})")
print(py_ver)
' "$REPO_ROOT/pyproject.toml" "$REPO_ROOT/rust/Cargo.toml")"

SCRATCH_PATH="$REPO_ROOT/out/swift-build"
RUST_TARGET_DIR="${CARGO_TARGET_DIR:-$REPO_ROOT/out/rust-target}"
ICON_SCRATCH_PATH="$REPO_ROOT/out/app-icon"
mkdir -p "$OUTPUT_DIR" "$SCRATCH_PATH" "$RUST_TARGET_DIR" "$ICON_SCRATCH_PATH"

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

CARGO_TARGET_DIR="$RUST_TARGET_DIR" "$CARGO_BIN" build \
    --manifest-path "$REPO_ROOT/rust/Cargo.toml" \
    --release \
    -p cas-cli

RUST_CLI_PATH="$RUST_TARGET_DIR/release/codex-agy-sync"
if [[ ! -x "$RUST_CLI_PATH" ]]; then
    echo "Error: built Rust CLI binary missing at $RUST_CLI_PATH" >&2
    exit 1
fi

LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
APP_BUNDLE="$OUTPUT_DIR/$APP_NAME.app"
STAGE_DIR="$(mktemp -d "$SCRATCH_PATH/bundle-stage.XXXXXX")"
DEST_STAGE="$OUTPUT_DIR/.${APP_NAME}.app.stage.$$"
STAGE_APP="$STAGE_DIR/$APP_NAME.app"
cleanup_stage() {
    if [[ "$SKIP_LSREGISTER" != "1" && -x "$LSREGISTER" && -d "$STAGE_APP" ]]; then
        "$LSREGISTER" -u "$STAGE_APP" >/dev/null 2>&1 || true
    fi
    rm -rf "$STAGE_DIR" "$DEST_STAGE"
}
trap cleanup_stage EXIT

EFFECTIVE_DISPLAY_NAME="${DISPLAY_NAME:-$APP_NAME}"
CONTENTS_DIR="$STAGE_APP/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
HELPERS_DIR="$CONTENTS_DIR/Helpers"
RESOURCES_DIR="$CONTENTS_DIR/Resources"
PLIST_PATH="$CONTENTS_DIR/Info.plist"
ICNS_PATH="$RESOURCES_DIR/AppIcon.icns"

mkdir -p "$MACOS_DIR" "$HELPERS_DIR" "$RESOURCES_DIR"
cp "$BIN_PATH" "$MACOS_DIR/$APP_NAME"
chmod 755 "$MACOS_DIR/$APP_NAME"
cp "$RUST_CLI_PATH" "$HELPERS_DIR/codex-agy-sync"
chmod 755 "$HELPERS_DIR/codex-agy-sync"

swift "$REPO_ROOT/scripts/generate_app_icon.swift" "$ICNS_PATH" "$ICON_SCRATCH_PATH"

python3 - "$PLIST_PATH" "$BUNDLE_ID" "$APP_NAME" "$EFFECTIVE_DISPLAY_NAME" "$APP_VERSION" <<'PY'
import plistlib
import sys

plist_path, bundle_id, app_name, display_name, app_version = sys.argv[1:6]
payload = {
    "CFBundleDevelopmentRegion": "zh_CN",
    "CFBundleDisplayName": display_name,
    "CFBundleExecutable": app_name,
    "CFBundleIconFile": "AppIcon",
    "CFBundleIconName": "AppIcon",
    "CFBundleIdentifier": bundle_id,
    "CFBundleInfoDictionaryVersion": "6.0",
    "CFBundleLocalizations": ["zh-Hans", "en"],
    "CFBundleName": display_name,
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

if [[ "$SIGN_MODE" == "developer_id" ]]; then
    codesign --force --options runtime --timestamp --sign "$SIGN_IDENTITY" "$HELPERS_DIR/codex-agy-sync"
    codesign --force --options runtime --timestamp --sign "$SIGN_IDENTITY" "$STAGE_APP"
else
    codesign --force --sign - "$HELPERS_DIR/codex-agy-sync"
    codesign --force --sign - "$STAGE_APP"
fi
codesign --verify --deep --strict "$STAGE_APP"

if [[ "$NOTARIZE" -eq 1 ]]; then
    notarize_and_verify_stage_app "$STAGE_APP"
fi

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
    if [[ "$NOTARIZE" -eq 1 ]]; then
        cp -f "$ARCHIVE_ZIP_ABS.sha256" "$ARCHIVE_DIR/SHA256SUMS.txt"
    fi
fi

if [[ "$INSTALL" -eq 1 ]]; then
    LEGACY_NAME="CodexAgy""Handoff"
    LEGACY_BUNDLE_ID="dev.semanticcraft.codex-agy-""handoff"
    LEGACY_BUNDLE="$OUTPUT_DIR/$LEGACY_NAME.app"
    if [[ -d "$LEGACY_BUNDLE" ]]; then
        pkill -f "$LEGACY_BUNDLE/Contents/MacOS/$LEGACY_NAME" >/dev/null 2>&1 || true
        if [[ "$SKIP_LSREGISTER" != "1" ]]; then
            tccutil reset All "$LEGACY_BUNDLE_ID" >/dev/null 2>&1 || true
            if [[ -x "$LSREGISTER" ]]; then
                "$LSREGISTER" -u "$LEGACY_BUNDLE" >/dev/null 2>&1 || true
            fi
        fi
        rm -rf "$LEGACY_BUNDLE"
    fi
    if [[ -n "${HOME:-}" && -n "${LEGACY_NAME:-}" && -n "${LEGACY_BUNDLE_ID:-}" ]]; then
        rm -rf "$HOME/Library/Application Support/$LEGACY_NAME"
        rm -rf "$HOME/Library/Caches/$LEGACY_BUNDLE_ID"
        rm -rf "$HOME/Library/Caches/$LEGACY_NAME"
    fi
    if [[ "$SKIP_LSREGISTER" != "1" && -x "$LSREGISTER" ]]; then
        "$LSREGISTER" -f "$APP_BUNDLE" >/dev/null 2>&1 || true
    fi
fi

echo "$APP_BUNDLE"
