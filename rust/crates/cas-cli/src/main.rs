//! Unified `codex-agy-sync` Rust CLI binary with Wave-1 parity across `menubar-feed`, `pair`, `status`, `inspect`, and `sync`.

use std::env;
use std::path::PathBuf;
use std::process::ExitCode;

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
        Commands::Pair(_) | Commands::Status(_) | Commands::Inspect(_) => Ok(()),
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
