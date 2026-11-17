//! Unified `codex-agy-sync` Rust CLI binary with Wave-1 parity across `menubar-feed`, `pair`, `status`, `inspect`, and `sync`.

use std::env;
use std::ffi::OsString;
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode};

use cas_core::{
    PACKAGE_VERSION, PairingPolicy, SyncDirection, TransportMode, normalize_workspace_spec,
    resolve_strict_false,
};
use cas_host::{
    HostEnvironmentPaths, HostError, PresenceWalGuard, ProjectRegistry, build_menubar_feed,
    diagnose_workspace_binding, execute_handoff, inspect_session_ledger, pair_workspace_with_mode,
    reconcile_workspace_handoffs, resolve_handoff_request, summarize_workspace_handoffs,
    validate_handoff_selector_flags,
};
use clap::{Args, Parser, Subcommand, ValueEnum};
use serde_json::json;

#[derive(Debug, Parser)]
#[command(
    name = "codex-agy-sync",
    version = PACKAGE_VERSION,
    about = "Antigravity <-> Codex session parity bridge"
)]
struct Cli {
    #[arg(long, global = true, value_name = "PATH")]
    agy_data_dir: Option<PathBuf>,

    #[arg(long, global = true, value_name = "PATH")]
    agy_cli_data_dir: Option<PathBuf>,

    #[arg(long, global = true, value_name = "PATH")]
    gemini_config_dir: Option<PathBuf>,

    #[arg(long, global = true, value_name = "PATH")]
    codex_home_dir: Option<PathBuf>,

    #[arg(long, global = true, value_name = "PATH")]
    sync_state_dir: Option<PathBuf>,

    #[command(subcommand)]
    command: Commands,
}

#[derive(Debug, Subcommand)]
enum Commands {
    /// Output a bounded JSON snapshot of recent Antigravity and Codex sessions for the menubar app
    #[command(name = "menubar-feed")]
    MenubarFeed(MenubarFeedArgs),

    /// Register and verify bidirectional workspace identity across Antigravity and Codex
    Pair(PairArgs),

    /// Inspect workspace pairing, sessions, handoff chains, and pending receipts
    Status(StatusArgs),

    /// Inspect one Antigravity cascade or Codex thread read-only
    Inspect(InspectArgs),

    /// One-shot session handoff or pending receipt reconciliation
    Sync(SyncArgs),

    /// Start the Stage-1 read-only Local Workbench server (delegates to Python CLI)
    #[command(name = "serve", disable_help_flag = true)]
    Serve(ServeArgs),
}

#[derive(Debug, Args)]
struct ServeArgs {
    #[arg(trailing_var_arg = true, allow_hyphen_values = true)]
    args: Vec<OsString>,
}

#[derive(Debug, Args)]
struct MenubarFeedArgs {
    #[arg(short = 'w', long = "workspace", value_name = "PATH_OR_URI")]
    workspace: Vec<String>,

    #[arg(long, default_value_t = 6)]
    limit: i64,
}

#[derive(Debug, Args)]
struct PairArgs {
    #[arg(short = 'w', long = "workspace", value_name = "PATH_OR_URI")]
    workspace: Option<String>,
}

#[derive(Debug, Args)]
struct StatusArgs {
    #[arg(short = 'w', long = "workspace", value_name = "PATH_OR_URI")]
    workspace: Option<String>,

    #[arg(long)]
    include_subagents: bool,
}

#[derive(Debug, Clone, Copy, ValueEnum)]
enum CliTransportMode {
    #[value(name = "auto")]
    Auto,
    #[value(name = "online_rpc")]
    OnlineRpc,
    #[value(name = "offline_storage")]
    OfflineStorage,
}

impl From<CliTransportMode> for TransportMode {
    fn from(value: CliTransportMode) -> Self {
        match value {
            CliTransportMode::Auto => Self::Auto,
            CliTransportMode::OnlineRpc => Self::OnlineRpc,
            CliTransportMode::OfflineStorage => Self::OfflineStorage,
        }
    }
}

#[derive(Debug, Args)]
struct InspectArgs {
    #[arg(short = 'w', long = "workspace", value_name = "PATH_OR_URI")]
    workspace: Option<String>,

    #[arg(long, value_name = "UUID")]
    cascade_id: Option<String>,

    #[arg(long, value_name = "UUID")]
    codex_thread_id: Option<String>,

    #[arg(long, value_enum, default_value_t = CliTransportMode::Auto)]
    mode: CliTransportMode,
}

#[derive(Debug, Clone, Copy, ValueEnum)]
enum CliSyncDirection {
    #[value(name = "codex_to_agy")]
    CodexToAgy,
    #[value(name = "agy_to_codex")]
    AgyToCodex,
}

impl From<CliSyncDirection> for SyncDirection {
    fn from(value: CliSyncDirection) -> Self {
        match value {
            CliSyncDirection::CodexToAgy => Self::CodexToAgy,
            CliSyncDirection::AgyToCodex => Self::AgyToCodex,
        }
    }
}

#[derive(Debug, Args)]
struct SyncArgs {
    #[arg(short = 'w', long = "workspace", value_name = "PATH_OR_URI")]
    workspace: Option<String>,

    #[arg(short = 'd', long = "direction", value_enum)]
    direction: Option<CliSyncDirection>,

    #[arg(long, value_name = "UUID")]
    source_id: Option<String>,

    #[arg(long)]
    title: Option<String>,

    #[arg(long)]
    latest: bool,

    #[arg(long)]
    chain: bool,

    #[arg(long)]
    pick: bool,

    #[arg(long)]
    pair_if_offline: bool,

    #[arg(long)]
    auto_pair: bool,

    #[arg(long)]
    include_interrupted: bool,

    #[arg(long)]
    reconcile: bool,

    #[arg(long)]
    dry_run: bool,

    #[arg(long)]
    json: bool,
}

fn subcommand_name(cmd: &Commands) -> &'static str {
    match cmd {
        Commands::MenubarFeed(_) => "menubar-feed",
        Commands::Pair(_) => "pair",
        Commands::Status(_) => "status",
        Commands::Inspect(_) => "inspect",
        Commands::Sync(_) => "sync",
        Commands::Serve(_) => "serve",
    }
}

enum PythonServeTarget {
    CliScript(PathBuf),
    PythonScript { python: PathBuf, script: PathBuf },
    PythonModule(PathBuf),
}

fn read_shebang_line(path: &Path) -> Option<String> {
    let file = fs::File::open(path).ok()?;
    let mut reader = std::io::BufReader::new(file);
    let mut line = String::new();
    use std::io::BufRead;
    if reader.read_line(&mut line).ok()? == 0 {
        return None;
    }
    if line.starts_with("#!") {
        Some(line)
    } else {
        None
    }
}

fn is_executable_script(path: &Path) -> bool {
    if !path.is_file() {
        return false;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let Ok(meta) = fs::metadata(path) else {
            return false;
        };
        if (meta.permissions().mode() & 0o111) == 0 {
            return false;
        }
    }
    let Ok(mut file) = fs::File::open(path) else {
        return false;
    };
    let mut magic = [0_u8; 2];
    matches!(file.read_exact(&mut magic), Ok(()) if &magic == b"#!")
}

fn is_executable_file(path: &Path) -> bool {
    if !path.is_file() {
        return false;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::metadata(path)
            .map(|m| (m.permissions().mode() & 0o111) != 0)
            .unwrap_or(false)
    }
    #[cfg(not(unix))]
    {
        true
    }
}

fn is_same_executable(candidate: &Path, self_exe: Option<&Path>) -> bool {
    let Some(self_path) = self_exe else {
        return false;
    };
    let Ok(cand_canon) = fs::canonicalize(candidate) else {
        return false;
    };
    let Ok(self_canon) = fs::canonicalize(self_path) else {
        return false;
    };
    cand_canon == self_canon
}

fn parse_python_version_dir_key(path: &Path) -> (u32, u32, String) {
    let name = path
        .file_name()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_default();
    let mut parts = name.split('.');
    let major = parts
        .next()
        .and_then(|s| s.parse::<u32>().ok())
        .unwrap_or(0);
    let minor = parts
        .next()
        .and_then(|s| s.parse::<u32>().ok())
        .unwrap_or(0);
    (major, minor, name)
}

fn python_meets_min_version(python: &Path) -> bool {
    Command::new(python)
        .args([
            "-c",
            "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)",
        ])
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

fn python_can_import_package(python: &Path) -> bool {
    Command::new(python)
        .args([
            "-c",
            "import sys, codex_agy_sync; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)",
        ])
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

fn collect_python3_candidates() -> Vec<PathBuf> {
    let mut python_candidates: Vec<PathBuf> = Vec::new();
    if let Some(path_var) = env::var_os("PATH") {
        for dir in env::split_paths(&path_var) {
            python_candidates.push(dir.join("python3"));
        }
    }
    python_candidates.push(PathBuf::from("/opt/homebrew/bin/python3"));
    python_candidates.push(PathBuf::from("/usr/local/bin/python3"));
    python_candidates.push(PathBuf::from("/usr/bin/python3"));
    python_candidates
}

fn wrap_cli_script_target(script: PathBuf) -> Option<PythonServeTarget> {
    if let Some(shebang) = read_shebang_line(&script) {
        let lower = shebang.to_ascii_lowercase();
        if lower.contains("/env ") && lower.contains("python") {
            for py in collect_python3_candidates() {
                if is_executable_file(&py) && python_meets_min_version(&py) {
                    return Some(PythonServeTarget::PythonScript { python: py, script });
                }
            }
            return None;
        }
    }
    Some(PythonServeTarget::CliScript(script))
}

fn resolve_python_serve_target() -> Option<PythonServeTarget> {
    let self_exe = env::current_exe().ok();
    if let Some(explicit) = env::var_os("CODEX_AGY_SYNC_PYTHON_BIN").filter(|v| !v.is_empty()) {
        let p = PathBuf::from(explicit);
        if is_executable_file(&p) && !is_same_executable(&p, self_exe.as_deref()) {
            let is_python_interp = !is_executable_script(&p)
                && p.file_name()
                    .map(|n| {
                        n.to_string_lossy()
                            .to_ascii_lowercase()
                            .starts_with("python")
                    })
                    .unwrap_or(false);
            if is_python_interp {
                if python_can_import_package(&p) {
                    return Some(PythonServeTarget::PythonModule(p));
                }
                return None;
            }
            return wrap_cli_script_target(p);
        }
        return None;
    }

    let mut script_candidates: Vec<PathBuf> = Vec::new();
    if let Some(ref exe) = self_exe {
        for ancestor in exe.ancestors().skip(1).take(8) {
            script_candidates.push(ancestor.join("bin").join("codex-agy-sync"));
        }
    }
    if let Some(path_var) = env::var_os("PATH") {
        for dir in env::split_paths(&path_var) {
            script_candidates.push(dir.join("codex-agy-sync"));
        }
    }
    if let Some(home) = env::var_os("HOME")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
    {
        script_candidates.push(home.join(".local").join("bin").join("codex-agy-sync"));
        let py_lib = home.join("Library").join("Python");
        if let Ok(entries) = fs::read_dir(py_lib) {
            let mut dirs: Vec<PathBuf> = entries
                .filter_map(Result::ok)
                .map(|e| e.path())
                .filter(|p| p.is_dir())
                .collect();
            dirs.sort_by_key(|p| std::cmp::Reverse(parse_python_version_dir_key(p)));
            for dir in dirs {
                script_candidates.push(dir.join("bin").join("codex-agy-sync"));
            }
        }
    }
    script_candidates.push(PathBuf::from("/opt/homebrew/bin/codex-agy-sync"));
    script_candidates.push(PathBuf::from("/usr/local/bin/codex-agy-sync"));

    for cand in script_candidates {
        if is_executable_script(&cand)
            && !is_same_executable(&cand, self_exe.as_deref())
            && let Some(target) = wrap_cli_script_target(cand)
        {
            return Some(target);
        }
    }

    for py in collect_python3_candidates() {
        if !is_executable_file(&py) {
            continue;
        }
        if python_can_import_package(&py) {
            return Some(PythonServeTarget::PythonModule(py));
        }
    }
    None
}

fn build_serve_forwarded_args(cli: &Cli, serve_args: &ServeArgs) -> Vec<OsString> {
    let mut out: Vec<OsString> = Vec::new();
    if let Some(ref p) = cli.agy_data_dir {
        out.push(OsString::from("--agy-data-dir"));
        out.push(p.as_os_str().to_os_string());
    }
    if let Some(ref p) = cli.agy_cli_data_dir {
        out.push(OsString::from("--agy-cli-data-dir"));
        out.push(p.as_os_str().to_os_string());
    }
    if let Some(ref p) = cli.gemini_config_dir {
        out.push(OsString::from("--gemini-config-dir"));
        out.push(p.as_os_str().to_os_string());
    }
    if let Some(ref p) = cli.codex_home_dir {
        out.push(OsString::from("--codex-home-dir"));
        out.push(p.as_os_str().to_os_string());
    }
    if let Some(ref p) = cli.sync_state_dir {
        out.push(OsString::from("--sync-state-dir"));
        out.push(p.as_os_str().to_os_string());
    }
    out.push(OsString::from("serve"));
    out.extend(serve_args.args.iter().cloned());
    out
}

fn delegate_serve_to_python(cli: &Cli, serve_args: &ServeArgs) -> ExitCode {
    if env::var_os("CODEX_AGY_SYNC_SERVE_DELEGATED")
        .filter(|v| !v.is_empty())
        .is_some()
    {
        let payload = json!({
            "error": "serve_python_unavailable",
            "message": "Recursive serve delegation detected; install Python codex-agy-sync CLI for `serve`."
        });
        let rendered =
            serde_json::to_string_pretty(&payload).unwrap_or_else(|_| payload.to_string());
        eprintln!("{rendered}");
        return ExitCode::from(2);
    }

    let Some(target) = resolve_python_serve_target() else {
        let payload = json!({
            "error": "serve_python_unavailable",
            "message": "The `serve` workbench requires the Python `codex-agy-sync` CLI, which was not found in PATH or standard Python locations."
        });
        let rendered =
            serde_json::to_string_pretty(&payload).unwrap_or_else(|_| payload.to_string());
        eprintln!("{rendered}");
        return ExitCode::from(2);
    };

    let forwarded = build_serve_forwarded_args(cli, serve_args);
    let mut cmd = match target {
        PythonServeTarget::CliScript(script) => {
            let mut c = Command::new(script);
            c.args(&forwarded);
            c
        }
        PythonServeTarget::PythonScript { python, script } => {
            let mut c = Command::new(python);
            c.arg(script);
            c.args(&forwarded);
            c
        }
        PythonServeTarget::PythonModule(py) => {
            let mut c = Command::new(py);
            c.args(["-m", "codex_agy_sync"]);
            c.args(&forwarded);
            c
        }
    };
    cmd.env("CODEX_AGY_SYNC_SERVE_DELEGATED", "1");

    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        let err = cmd.exec();
        let payload = json!({
            "error": "serve_launch_failed",
            "message": format!("Failed to delegate `serve` to Python CLI: {err}")
        });
        let rendered =
            serde_json::to_string_pretty(&payload).unwrap_or_else(|_| payload.to_string());
        eprintln!("{rendered}");
        ExitCode::from(2)
    }
    #[cfg(not(unix))]
    {
        match cmd.status() {
            Ok(status) => {
                let code = u8::try_from(status.code().unwrap_or(1)).unwrap_or(1);
                ExitCode::from(code)
            }
            Err(err) => {
                let payload = json!({
                    "error": "serve_launch_failed",
                    "message": format!("Failed to delegate `serve` to Python CLI: {err}")
                });
                let rendered =
                    serde_json::to_string_pretty(&payload).unwrap_or_else(|_| payload.to_string());
                eprintln!("{rendered}");
                ExitCode::from(2)
            }
        }
    }
}

fn build_paths(cli: &Cli) -> HostEnvironmentPaths {
    let discovered = HostEnvironmentPaths::discover(None);
    let agy_data_dir = cli
        .agy_data_dir
        .as_ref()
        .map_or(discovered.agy_data_dir.clone(), |p| resolve_strict_false(p));

    let agy_cli_data_dir = if let Some(ref explicit_cli) = cli.agy_cli_data_dir {
        Some(resolve_strict_false(explicit_cli))
    } else if let Some(ref explicit_agy) = cli.agy_data_dir
        && env::var_os("ANTIGRAVITY_CLI_DATA_DIR")
            .filter(|v| !v.is_empty())
            .is_none()
    {
        let sibling = resolve_strict_false(explicit_agy)
            .parent()
            .map(|p| p.join("antigravity-cli"));
        sibling.filter(|p| p.is_dir())
    } else {
        discovered.agy_cli_data_dir
    };

    HostEnvironmentPaths {
        gemini_config_dir: cli
            .gemini_config_dir
            .as_ref()
            .map_or(discovered.gemini_config_dir, |p| resolve_strict_false(p)),
        agy_data_dir,
        codex_home_dir: cli
            .codex_home_dir
            .as_ref()
            .map_or(discovered.codex_home_dir, |p| resolve_strict_false(p)),
        sync_state_dir: cli
            .sync_state_dir
            .as_ref()
            .map_or(discovered.sync_state_dir, |p| resolve_strict_false(p)),
        agy_cli_data_dir,
        codex_app_server_socket: discovered.codex_app_server_socket,
    }
}

fn resolve_workspace_arg(raw: Option<&str>) -> String {
    match raw {
        Some(spec) => normalize_workspace_spec(spec),
        None => env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("."))
            .display()
            .to_string(),
    }
}

fn validate_cli_args(cmd: &Commands) -> Result<(), HostError> {
    match cmd {
        Commands::MenubarFeed(args) => {
            if !(1..=50).contains(&args.limit) {
                return Err(HostError::InvalidArgument(
                    "--limit must be between 1 and 50.".to_owned(),
                ));
            }
            Ok(())
        }
        Commands::Sync(args) => {
            if args.dry_run && !args.reconcile {
                return Err(HostError::InvalidArgument(
                    "--dry-run requires --reconcile.".to_owned(),
                ));
            }
            if args.reconcile {
                if args.latest || args.chain || args.pick || args.pair_if_offline || args.auto_pair
                {
                    return Err(HostError::InvalidArgument(
                        "--reconcile cannot be combined with --latest, --chain, --pick, --pair-if-offline, or --auto-pair."
                            .to_owned(),
                    ));
                }
                return Ok(());
            }
            if args.auto_pair && args.pair_if_offline {
                return Err(HostError::InvalidArgument(
                    "--auto-pair cannot be combined with --pair-if-offline.".to_owned(),
                ));
            }
            validate_handoff_selector_flags(
                args.source_id.as_deref(),
                args.latest,
                args.chain,
                args.pick,
                false,
            )
        }
        Commands::Pair(_) | Commands::Status(_) | Commands::Inspect(_) | Commands::Serve(_) => {
            Ok(())
        }
    }
}

fn run_command(
    paths: &HostEnvironmentPaths,
    cmd: &Commands,
) -> Result<serde_json::Value, HostError> {
    match cmd {
        Commands::MenubarFeed(args) => {
            let seeds: Vec<String> = args
                .workspace
                .iter()
                .map(|w| normalize_workspace_spec(w))
                .collect();
            let snapshot = build_menubar_feed(paths, args.limit as usize, &seeds, None)?;
            Ok(serde_json::to_value(snapshot)?)
        }
        Commands::Pair(args) => {
            let ws = resolve_workspace_arg(args.workspace.as_deref());
            let guard = PresenceWalGuard::new(paths.clone());
            let registry = ProjectRegistry::new(paths.clone());
            let (binding, _) = pair_workspace_with_mode(paths, &guard, &registry, &ws, true)?;
            Ok(serde_json::to_value(binding)?)
        }
        Commands::Status(args) => {
            let ws = resolve_workspace_arg(args.workspace.as_deref());
            let registry = ProjectRegistry::new(paths.clone());
            let binding = registry.resolve_readonly_binding(&ws)?;
            let sessions = registry.list_workspace_sessions(&binding, args.include_subagents);
            let diag = diagnose_workspace_binding(paths, &binding.workspace_path)?;
            let handoffs = summarize_workspace_handoffs(paths, &binding.workspace_path)?;
            let session_rows: Vec<serde_json::Value> = sessions
                .into_iter()
                .map(|s| {
                    let type_name = if s.agent_type_name.is_empty() {
                        None
                    } else {
                        Some(s.agent_type_name)
                    };
                    let role = if s.agent_role.is_empty() {
                        None
                    } else {
                        Some(s.agent_role)
                    };
                    json!({
                        "cascade_id": s.cascade_id,
                        "codex_thread_id": s.codex_thread_id,
                        "title": s.title,
                        "agy_step_count": s.agy_step_count,
                        "codex_turn_count": s.codex_turn_count,
                        "parent_cascade_id": s.parent_cascade_id,
                        "parent_codex_thread_id": s.parent_codex_thread_id,
                        "is_subagent": s.is_subagent,
                        "nesting_depth": s.nesting_depth,
                        "agent_type_name": type_name,
                        "agent_role": role,
                    })
                })
                .collect();
            Ok(json!({
                "workspace": binding,
                "sessions": session_rows,
                "pairing": {
                    "paired": diag.is_paired(),
                    "agy_registered": diag.agy_registered,
                    "codex_registered": diag.codex_registered,
                    "agy_pid": diag.agy_project_id,
                    "codex_pid": diag.codex_project_id,
                    "agy_project_id": diag.agy_project_id,
                    "codex_project_id": diag.codex_project_id,
                    "agy_daemon_active": diag.agy_daemon_active,
                    "codex_daemon_active": diag.codex_daemon_active,
                    "remediation_steps": diag.remediation_steps,
                },
                "handoff_chains": handoffs.handoff_chains,
                "pending_handoffs": handoffs.pending_handoffs,
            }))
        }
        Commands::Inspect(args) => {
            let ws = resolve_workspace_arg(args.workspace.as_deref());
            let report = inspect_session_ledger(
                paths,
                &ws,
                args.cascade_id.as_deref(),
                args.codex_thread_id.as_deref(),
                args.mode.into(),
            )?;
            Ok(serde_json::to_value(report)?)
        }
        Commands::Sync(args) => {
            let ws = resolve_workspace_arg(args.workspace.as_deref());
            if args.reconcile {
                let report = reconcile_workspace_handoffs(
                    paths,
                    &ws,
                    args.source_id.as_deref(),
                    args.dry_run,
                )?;
                return Ok(serde_json::to_value(report)?);
            }
            let pairing_policy = if args.auto_pair {
                PairingPolicy::AutoPair
            } else if args.pair_if_offline {
                PairingPolicy::OfflinePairOnly
            } else {
                PairingPolicy::RequirePaired
            };
            let resolved = resolve_handoff_request(
                paths,
                &ws,
                args.direction.map(Into::into),
                args.source_id.as_deref(),
                args.title.as_deref(),
                args.latest,
                args.chain,
                args.pick,
                pairing_policy,
                args.include_interrupted,
            )?;
            let receipt = execute_handoff(paths, &resolved, args.include_interrupted)?;
            Ok(serde_json::to_value(receipt)?)
        }
        Commands::Serve(_) => Err(HostError::InvalidArgument(
            "serve must be delegated prior to run_command".to_owned(),
        )),
    }
}

fn main() -> ExitCode {
    let cli = match Cli::try_parse() {
        Ok(c) => c,
        Err(err) => {
            let exit_code = if matches!(
                err.kind(),
                clap::error::ErrorKind::DisplayHelp | clap::error::ErrorKind::DisplayVersion
            ) {
                let _ = err.print();
                return ExitCode::SUCCESS;
            } else {
                let _ = err.print();
                2
            };
            return ExitCode::from(exit_code);
        }
    };

    let subcmd = subcommand_name(&cli.command);
    if let Commands::Serve(ref serve_args) = cli.command {
        return delegate_serve_to_python(&cli, serve_args);
    }

    if let Err(err) = validate_cli_args(&cli.command) {
        let payload = err.to_error_payload(subcmd);
        let rendered =
            serde_json::to_string_pretty(&payload).unwrap_or_else(|_| payload.to_string());
        eprintln!("{rendered}");
        return ExitCode::from(2);
    }

    let paths = build_paths(&cli);
    match run_command(&paths, &cli.command) {
        Ok(val) => {
            let rendered = serde_json::to_string_pretty(&val).unwrap_or_else(|_| val.to_string());
            println!("{rendered}");
            ExitCode::SUCCESS
        }
        Err(err) => {
            let payload = err.to_error_payload(subcmd);
            let rendered =
                serde_json::to_string_pretty(&payload).unwrap_or_else(|_| payload.to_string());
            eprintln!("{rendered}");
            ExitCode::from(2)
        }
    }
}
