use std::fs::File;
use std::io::Read;
use std::path::PathBuf;
use std::process::ExitCode;

use clap::Parser;
use glaeda::runner_log_reclaim::{
    DEFAULT_RUNNER_LOG_RECLAIM_BATCH, MIN_RUNNER_LOG_RECLAIM_IDLE_SECONDS, Policy,
    RunnerEnrollmentReceipt, reclaim,
};

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
    #[arg(long, default_value = "json", value_parser = ["json", "human"])]
    output: String,
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    let mut bytes = Vec::new();
    let receipt = match File::open(&cli.enrollment_receipt)
        .ok()
        .and_then(|mut file| {
            file.take(65_537)
                .read_to_end(&mut bytes)
                .ok()
                .map(|_| bytes)
        })
        .filter(|bytes| bytes.len() <= 65_536)
        .and_then(|bytes| serde_json::from_slice::<RunnerEnrollmentReceipt>(&bytes).ok())
    {
        Some(receipt) => receipt,
        None => {
            eprintln!("glaeda-runner-log-reclaim: enrollment_receipt_invalid");
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
    let report = match reclaim(
        &cli.runner_root,
        &receipt,
        policy,
        cli.state_root.as_deref(),
        cli.apply,
    ) {
        Ok(report) => report,
        Err(error) => {
            eprintln!("glaeda-runner-log-reclaim: {error}");
            return ExitCode::from(1);
        }
    };
    if cli.output == "human" {
        println!(
            "{} operation={} candidates={} reclaimed={} complete={} errors={}",
            report.document_type,
            report.operation,
            report.actions.len(),
            report.reclaimed_count,
            report.observation.complete,
            report.errors.len()
        );
    } else if let Err(_) = serde_json::to_writer(std::io::stdout(), &report) {
        return ExitCode::from(1);
    } else {
        println!();
    }
    if report.errors.is_empty() {
        ExitCode::SUCCESS
    } else {
        ExitCode::from(1)
    }
}
