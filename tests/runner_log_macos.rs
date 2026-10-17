#![cfg(target_os = "macos")]

use std::fs::{self, File};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use glaeda::runner_log_reclaim::{
    ItemDecision, MIN_RUNNER_LOG_RECLAIM_IDLE_SECONDS, Policy, RunnerEnrollmentReceipt, observe,
    reclaim,
};

struct ChildGuard(std::process::Child);

impl Drop for ChildGuard {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn fixture(root: &Path, id: &str) -> RunnerEnrollmentReceipt {
    fs::create_dir_all(root.join("_diag")).expect("diag");
    fs::write(root.join(".glaeda-cmux-runner"), format!("{id}\n")).expect("marker");
    fs::set_permissions(root, fs::Permissions::from_mode(0o700)).expect("mode");
    RunnerEnrollmentReceipt {
        runner_dir_owned: true,
        runner_dir: root.to_string_lossy().into_owned(),
        install_id: id.into(),
    }
}

fn old_log(root: &Path) -> PathBuf {
    let path = root.join("_diag/old.log");
    fs::write(&path, b"expired").expect("log");
    let status = std::process::Command::new("/usr/bin/touch")
        .args(["-t", "202001010000", path.to_str().expect("utf8")])
        .status()
        .expect("touch");
    assert!(status.success());
    path
}

#[test]
fn libproc_open_identity_survives_same_volume_stage_and_second_runner_is_independent() {
    let base = std::env::temp_dir().join(format!(
        "glaeda-runner-log-macos-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("clock")
            .as_nanos()
    ));
    fs::create_dir_all(&base).expect("temp root");
    let first = base.join("runner-a");
    let second = base.join("runner-b");
    let first_receipt = fixture(&first, "a");
    let second_receipt = fixture(&second, "b");
    let first_log = old_log(&first);
    let second_log = old_log(&second);
    let held = File::open(&first_log).expect("open first log");
    fs::create_dir_all(first.join("bin")).expect("bin");
    let worker_path = first.join("bin/Runner.Worker");
    fs::copy("/bin/sleep", &worker_path).expect("worker binary");
    let signed = std::process::Command::new("/usr/bin/codesign")
        .args(["--force", "--sign", "-", "--timestamp=none"])
        .arg(&worker_path)
        .status()
        .expect("codesign worker");
    assert!(signed.success());
    let worker = std::process::Command::new(&worker_path)
        .arg("300")
        .current_dir(&base)
        .spawn()
        .expect("runner worker");
    let mut worker = ChildGuard(worker);
    assert!(worker.0.try_wait().expect("worker status").is_none());
    let policy = Policy::new(MIN_RUNNER_LOG_RECLAIM_IDLE_SECONDS, 4).expect("policy");
    let first_plan = reclaim(&first, &first_receipt, policy, None, false).expect("first plan");
    assert_eq!(
        first_plan.observation.items[0].decision,
        ItemDecision::OpenOrBusy
    );
    assert!(first_plan.observation.process.busy_processes_observed > 0);
    let second_plan = reclaim(&second, &second_receipt, policy, None, false).expect("second plan");
    assert_eq!(
        second_plan.observation.items[0].decision,
        ItemDecision::ExpiredRunnerDiagnosticLog
    );
    let staged = first.join("_diag/.glaeda-open-stage.log");
    fs::rename(&first_log, &staged).expect("stage");
    let staged_plan = observe(&first, &first_receipt, policy).expect("staged observation");
    assert_eq!(staged_plan.items[0].decision, ItemDecision::OpenOrBusy);
    fs::rename(&staged, &first_log).expect("restore");
    assert!(worker.0.try_wait().expect("worker status").is_none());
    drop(held);
    let second_apply = reclaim(&second, &second_receipt, policy, None, true).expect("apply report");
    assert!(
        second_apply
            .errors
            .iter()
            .any(|error| *error == "reclaim_not_ready")
    );
    assert!(second_log.exists() && first_log.exists());
    let _ = fs::remove_dir_all(base);
}

#[test]
#[ignore = "enabled when descriptor-bound store lands"]
fn apply_is_bounded_and_reports_remainder() {
    let base = std::env::temp_dir().join(format!(
        "glaeda-runner-log-batch-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("clock")
            .as_nanos()
    ));
    let root = base.join("runner");
    let receipt = fixture(&root, "batch");
    for index in 0..70 {
        let path = root.join(format!("_diag/{index:03}.log"));
        fs::write(&path, b"expired").expect("log");
        let status = std::process::Command::new("/usr/bin/touch")
            .args(["-t", "202001010000", path.to_str().expect("utf8")])
            .status()
            .expect("touch");
        assert!(status.success());
    }
    let policy = Policy::new(MIN_RUNNER_LOG_RECLAIM_IDLE_SECONDS, 64).expect("policy");
    let first = reclaim(&root, &receipt, policy, None, true).expect("first apply");
    assert_eq!(first.reclaimed_count, 64);
    assert_eq!(first.remaining_count, 6);
    let second = reclaim(&root, &receipt, policy, None, true).expect("second apply");
    assert_eq!(second.reclaimed_count, 6);
    assert_eq!(second.remaining_count, 0);
    let _ = fs::remove_dir_all(base);
}
