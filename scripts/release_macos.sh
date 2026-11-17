#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

OUT_DIR="${OUT_DIR:-$REPO_ROOT/out/release}"
ARCHIVE_ZIP=""
BUNDLE_ID=""
SIGN_IDENTITY=""
NOTARY_PROFILE_ARG=""
INSTALL=0

usage() {
    cat <<'EOF'
Usage: release_macos.sh [options]

Build, Developer ID sign (hardened runtime + timestamp), notarize, staple,
Gatekeeper-verify, and archive CodexAgySync.app with SHA256SUMS.txt.

Options:
  --out-dir <dir>           Output directory for release artifacts (default: $OUT_DIR or $REPO_ROOT/out/release)
  --output-dir <dir>        Alias for --out-dir
  --archive-zip <path>      Target zip path (default: <out-dir>/CodexAgySync-macos.zip)
  --bundle-id <id>          Override CFBundleIdentifier (default: dev.semanticcraft.codex-agy-sync)
  --sign-identity <id>      Explicit Developer ID Application identity (or CODESIGN_IDENTITY env)
  --notary-profile <name>   Notary keychain profile (or NOTARY_PROFILE / macos-notary-profile)
  --install                 Atomically install notarized bundle into $HOME/Applications/CodexAgySync.app
  -h, --help                Show this help text
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --out-dir|--output-dir)
            [[ $# -ge 2 ]] || { echo "Error: $1 requires an argument" >&2; exit 1; }
            OUT_DIR="$2"
            shift 2
            ;;
        --archive-zip)
            [[ $# -ge 2 ]] || { echo "Error: --archive-zip requires an argument" >&2; exit 1; }
            ARCHIVE_ZIP="$2"
            shift 2
            ;;
        --bundle-id)
            [[ $# -ge 2 ]] || { echo "Error: --bundle-id requires an argument" >&2; exit 1; }
            BUNDLE_ID="$2"
            shift 2
            ;;
        --sign-identity)
            [[ $# -ge 2 ]] || { echo "Error: --sign-identity requires an argument" >&2; exit 1; }
            SIGN_IDENTITY="$2"
            shift 2
            ;;
        --notary-profile)
            [[ $# -ge 2 ]] || { echo "Error: --notary-profile requires an argument" >&2; exit 1; }
            NOTARY_PROFILE_ARG="$2"
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

if [[ -z "$ARCHIVE_ZIP" ]]; then
    ARCHIVE_ZIP="$OUT_DIR/CodexAgySync-macos.zip"
fi

ARGS=(--release --archive-zip "$ARCHIVE_ZIP")
if [[ "$INSTALL" -eq 1 ]]; then
    ARGS+=(--install)
else
    ARGS+=(--output-dir "$OUT_DIR")
fi
if [[ -n "$BUNDLE_ID" ]]; then
    ARGS+=(--bundle-id "$BUNDLE_ID")
fi
if [[ -n "$SIGN_IDENTITY" ]]; then
    ARGS+=(--sign-identity "$SIGN_IDENTITY")
fi
if [[ -n "$NOTARY_PROFILE_ARG" ]]; then
    ARGS+=(--notary-profile "$NOTARY_PROFILE_ARG")
fi

exec "$SCRIPT_DIR/build_menubar_app.sh" "${ARGS[@]}"
