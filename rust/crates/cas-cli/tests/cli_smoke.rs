#![allow(clippy::expect_used)]

use std::fs;
use std::path::PathBuf;
use std::process::Command;
use std::sync::atomic::{AtomicUsize, Ordering};

use serde_json::Value;
use tempfile::TempDir;

static CLI_SEQ: AtomicUsize = AtomicUsize::new(1);

fn make_sandbox() -> TempDir {
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    let base = home
        .join("Projects")
        .join("_artifacts")
        .join("codex-agy-sync")
        .join("rust-cli-tests");
    fs::create_dir_all(&base).expect("mkdir artifacts");
    let seq = CLI_SEQ.fetch_add(1, Ordering::Relaxed);
    let prefix = format!("cli-test-{}-{seq}-", std::process::id());
    tempfile::Builder::new()
        .prefix(&prefix)
        .tempdir_in(&base)
        .expect("tempdir in artifacts")
}

#[test]
fn cli_version_pair_status_and_invalid_flag_exit_codes() {
    let bin = env!("CARGO_BIN_EXE_codex-agy-sync");
    let ver = Command::new(bin)
        .arg("--version")
        .output()
        .expect("run version");
    assert!(ver.status.success());
    let ver_out = String::from_utf8_lossy(&ver.stdout);
    assert!(ver_out.contains("0.4.3"), "version={ver_out}");

    let tmp = make_sandbox();
    let root = tmp.path();
    let ws = root.join("proj");
    fs::create_dir_all(&ws).expect("mkdir ws");
    let gem = root.join("gem");
    let agy = root.join("agy");
    let codex = root.join("codex");
    let state = root.join("state");
    for d in [&gem, &agy, &codex, &state] {
        fs::create_dir_all(d).expect("mkdir");
    }

    let pair = Command::new(bin)
        .args([
            "--gemini-config-dir",
            &gem.display().to_string(),
            "--agy-data-dir",
            &agy.display().to_string(),
            "--codex-home-dir",
            &codex.display().to_string(),
            "--sync-state-dir",
            &state.display().to_string(),
            "pair",
            "-w",
            &ws.display().to_string(),
        ])
        .env("HOME", root)
        .output()
        .expect("run pair");
    assert!(
        pair.status.success(),
        "stderr={}",
        String::from_utf8_lossy(&pair.stderr)
    );
    let pair_json: Value = serde_json::from_slice(&pair.stdout).expect("json pair");
    assert_eq!(pair_json["workspace_name"], "proj");

    let status = Command::new(bin)
        .args([
            "--gemini-config-dir",
            &gem.display().to_string(),
            "--agy-data-dir",
            &agy.display().to_string(),
            "--codex-home-dir",
            &codex.display().to_string(),
            "--sync-state-dir",
            &state.display().to_string(),
            "status",
            "-w",
            &ws.display().to_string(),
        ])
        .env("HOME", root)
        .output()
        .expect("run status");
    assert!(status.status.success());
    let st_json: Value = serde_json::from_slice(&status.stdout).expect("json status");
    assert_eq!(st_json["pairing"]["paired"], true);

    let feed = Command::new(bin)
        .args([
            "--gemini-config-dir",
            &gem.display().to_string(),
            "--agy-data-dir",
            &agy.display().to_string(),
            "--codex-home-dir",
            &codex.display().to_string(),
            "--sync-state-dir",
            &state.display().to_string(),
            "menubar-feed",
            "-w",
            &ws.display().to_string(),
        ])
        .env("HOME", root)
        .output()
        .expect("run feed");
    assert!(feed.status.success());

    let bad_dry = Command::new(bin)
        .args(["sync", "--dry-run", "-w", &ws.display().to_string()])
        .env("HOME", root)
        .output()
        .expect("run bad dry");
    assert_eq!(bad_dry.status.code(), Some(2));
}
