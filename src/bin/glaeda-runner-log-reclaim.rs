use std::path::PathBuf;
use std::process::ExitCode;

use clap::Parser;
use glaeda::runner_log_reclaim::{
    DEFAULT_RUNNER_LOG_RECLAIM_BATCH, ItemDecision, LogItem, MIN_RUNNER_LOG_RECLAIM_IDLE_SECONDS,
    Policy, observe_synthetic_fixture, read_enrollment_receipt, reclaim, reclaim_synthetic_fixture,
    synthetic_fixture_process_evidence,
};
use glaeda::runner_log_reclaim_store::{
    StoreError, apply_synthetic_fixture_batch, validate_synthetic_fixture_paths,
};
use serde_json::json;

#[derive(Debug, Parser)]
#[command(
    name = "glaeda-runner-log-reclaim",
    about = "Observe or reclaim expired macOS Actions runner diagnostics"
)]
struct Cli {
    #[arg(long)]
    runner_root: PathBuf,
    #[arg(long)]
    enrollment_receipt: PathBuf,
    #[arg(long, default_value_t = MIN_RUNNER_LOG_RECLAIM_IDLE_SECONDS)]
    minimum_idle_seconds: i64,
    #[arg(long, default_value_t = DEFAULT_RUNNER_LOG_RECLAIM_BATCH)]
    batch_size: u32,
    #[arg(long)]
    state_root: Option<PathBuf>,
    #[arg(long)]
    apply: bool,
    /// Enable only the isolated synthetic-fixture transaction path. This never admits a live
    /// runner root; the enrollment install id must begin with `synthetic-`.
    #[arg(long)]
    synthetic_fixture: bool,
    #[arg(long, default_value = "json", value_parser = ["json", "human"])]
    output: String,
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    let receipt = match read_enrollment_receipt(&cli.enrollment_receipt) {
        Ok(receipt) => receipt,
        Err(error) => {
            eprintln!("glaeda-runner-log-reclaim: {error}");
            return ExitCode::from(2);
        }
    };
    let policy = match Policy::new(cli.minimum_idle_seconds, cli.batch_size) {
        Ok(policy) => policy,
        Err(error) => {
            eprintln!("glaeda-runner-log-reclaim: {error}");
            return ExitCode::from(2);
        }
    };
    if cli.synthetic_fixture && !cli.apply {
        eprintln!("glaeda-runner-log-reclaim: --synthetic-fixture requires --apply");
        return ExitCode::from(2);
    }
    if cli.synthetic_fixture && !receipt.install_id.starts_with("synthetic-") {
        eprintln!("glaeda-runner-log-reclaim: synthetic_fixture_required");
        return ExitCode::from(2);
    }
    if cli.synthetic_fixture {
        let Some(state_root) = cli.state_root.as_deref() else {
            eprintln!("glaeda-runner-log-reclaim: synthetic fixture requires --state-root");
            return ExitCode::from(2);
        };
        if let Err(error) = validate_synthetic_fixture_paths(&cli.runner_root, state_root) {
            eprintln!("glaeda-runner-log-reclaim: {error}");
            return ExitCode::from(2);
        }
    }
    let report = match if cli.synthetic_fixture {
        reclaim_synthetic_fixture(&cli.runner_root, &receipt, policy)
    } else {
        reclaim(
            &cli.runner_root,
            &receipt,
            policy,
            cli.state_root.as_deref(),
            cli.apply,
        )
    } {
        Ok(report) => report,
        Err(error) => {
            eprintln!("glaeda-runner-log-reclaim: {error}");
            return ExitCode::from(1);
        }
    };
    let mut output = match serde_json::to_value(&report) {
        Ok(value) => value,
        Err(_) => return ExitCode::from(1),
    };
    if cli.synthetic_fixture && report.observation.complete {
        let candidates = report
            .actions
            .iter()
            .map(|action| LogItem {
                relative_path: action.relative_path.clone(),
                identity: action.identity,
                bytes: action.identity.size,
                modified_seconds: action.identity.modified_seconds,
                decision: ItemDecision::ExpiredRunnerDiagnosticLog,
                reason: "expired_retention",
            })
            .collect::<Vec<_>>();
        let state_root = cli.state_root.as_deref().expect("validated above");
        let transaction = apply_synthetic_fixture_batch(
            &cli.runner_root,
            &receipt,
            state_root,
            &candidates,
            policy,
            || {
                synthetic_fixture_process_evidence(&cli.runner_root, &receipt)
                    .map_err(|_| StoreError::ObservationIncomplete)
            },
        );
        match transaction {
            Ok(transaction) => {
                let post = observe_synthetic_fixture(&cli.runner_root, &receipt, policy)
                    .map(|value| value.complete && value.process.busy_processes_observed == 0)
                    .unwrap_or(false);
                let items = report
                    .actions
                    .iter()
                    .zip(transaction.apply.receipts.iter())
                    .map(|(action, receipt_id)| {
                        json!({
                            "receipt_id": receipt_id,
                            "relative_path": action.relative_path,
                            "identity": action.identity,
                            "outcome": "reclaimed",
                        })
                    })
                    .collect::<Vec<_>>();
                output["reclaimed_count"] = json!(transaction.apply.reclaimed);
                output["operation"] = json!("apply");
                output["recovery_receipt"] = json!({
                    "pending_at_start": 0,
                    "pending_after": transaction.recovery.pending,
                    "outcome": if transaction.recovery.pending == 0 { "clean" } else { "pending" },
                });
                output["item_receipts"] = json!(items);
                output["post_action_receipt"] = json!({
                    "pending_count": transaction.recovery.pending,
                    "observed": post,
                });
                output["errors"] = json!([]);
            }
            Err(error) => {
                output["errors"] = json!(["reclaim_not_ready", error.to_string()]);
            }
        }
    } else if cli.synthetic_fixture {
        output["errors"] = json!(["reclaim_not_ready"]);
    }
    if cli.output == "human" {
        println!(
            "{} operation={} candidates={} reclaimed={} complete={} errors={}",
            report.document_type,
            report.operation,
            report.actions.len(),
            output["reclaimed_count"]
                .as_u64()
                .unwrap_or(report.reclaimed_count),
            report.observation.complete,
            output["errors"]
                .as_array()
                .map(Vec::len)
                .unwrap_or(report.errors.len())
        );
    } else if serde_json::to_writer(std::io::stdout(), &output).is_err() {
        return ExitCode::from(1);
    } else {
        println!();
    }
    if output["errors"].as_array().is_none_or(Vec::is_empty) {
        ExitCode::SUCCESS
    } else {
        ExitCode::from(1)
    }
}
