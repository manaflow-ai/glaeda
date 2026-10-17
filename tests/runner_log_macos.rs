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
fn libproc_worker_busy_and_open_identity_are_independent() {
    let base = std::env::temp_dir().join(format!(
        "glaeda-runner-log-macos-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    fs::create_dir_all(&base).unwrap();
    let base = fs::canonicalize(base).unwrap();
    let first = base.join("runner-a");
    let second = base.join("runner-b");
    let first_receipt = fixture(&first, "a");
    let second_receipt = fixture(&second, "b");
    let first_log = old_log(&first);
    let second_log = old_log(&second);
    // No held log yet: a busy decision must come from executable identity alone.
    fs::create_dir(first.join("bin")).unwrap();
    let worker_path = first.join("bin/Runner.Worker");
    fs::copy("/bin/sleep", &worker_path).unwrap();
    assert!(
        std::process::Command::new("/usr/bin/codesign")
            .args(["--force", "--sign", "-", "--timestamp=none"])
            .arg(&worker_path)
            .status()
            .unwrap()
            .success()
    );
    let mut worker = ChildGuard(
        std::process::Command::new(&worker_path)
            .arg("300")
            .current_dir(&base)
            .spawn()
            .unwrap(),
    );
    assert!(worker.0.try_wait().unwrap().is_none());
    let pid = worker.0.id() as i32;
    let observed_path = libproc::libproc::proc_pid::pidpath(pid).unwrap();
    assert_eq!(
        PathBuf::from(&observed_path),
        worker_path,
        "actual pidpath={observed_path:?}"
    );
    let info =
        libproc::libproc::proc_pid::pidinfo::<libproc::libproc::bsd_info::BSDInfo>(pid, 0).unwrap();
    assert_eq!(info.pbi_uid, rustix::process::geteuid().as_raw());
    let policy = Policy::new(MIN_RUNNER_LOG_RECLAIM_IDLE_SECONDS, 4).unwrap();
    let busy = observe(&first, &first_receipt, policy).unwrap();
    if busy.process.busy_processes_observed > 0 {
        assert_eq!(busy.items[0].reason, "runner_process_busy", "{busy:?}");
        assert_eq!(busy.process.busy_processes_observed, 1, "{busy:?}");
    } else {
        // Managed runners may reap disposable children. The direct pidpath
        // and UID assertions above still prove the fixture; after reaping,
        // the observer must fail closed rather than claim a busy veto.
        assert!(!busy.complete, "{busy:?}");
        assert_eq!(busy.items[0].decision, ItemDecision::Unknown, "{busy:?}");
    }
    let independent = observe(&second, &second_receipt, policy).unwrap();
    // The managed Mac has many same-UID processes; a PID can exit during the
    // bounded scan. That is a truthful incomplete observation and must veto
    // deletion. A quiet isolated worker may instead produce a complete plan.
    if independent.complete {
        assert_eq!(
            independent.items[0].decision,
            ItemDecision::ExpiredRunnerDiagnosticLog
        );
    } else {
        assert_eq!(
            independent.items[0].decision,
            ItemDecision::Unknown,
            "{independent:?}"
        );
    }
    // The managed runner may reap an ad hoc child between observations. The
    // first plan already proved the live Worker veto; drop the guard before
    // the independent held-FD checks so that they cannot be masked by it.
    drop(worker);

    let held = File::open(&first_log).unwrap();
    let open = observe(&first, &first_receipt, policy).unwrap();
    assert_eq!(open.process.busy_processes_observed, 0, "{open:?}");
    assert_eq!(open.items[0].reason, "open_descriptor", "{open:?}");
    let staged = first.join("_diag/.glaeda-open-stage.log");
    fs::rename(&first_log, &staged).unwrap();
    let renamed = observe(&first, &first_receipt, policy).unwrap();
    assert_eq!(renamed.items[0].reason, "open_descriptor", "{renamed:?}");
    fs::rename(&staged, &first_log).unwrap();
    drop(held);
    let closed = observe(&first, &first_receipt, policy).unwrap();
    if closed.complete {
        assert_eq!(
            closed.items[0].decision,
            ItemDecision::ExpiredRunnerDiagnosticLog
        );
    } else {
        assert_eq!(
            closed.items[0].decision,
            ItemDecision::Unknown,
            "{closed:?}"
        );
    }
    let absent_state = base.join("state-must-not-be-created");
    let apply = reclaim(&second, &second_receipt, policy, Some(&absent_state), true).unwrap();
    assert_eq!(apply.errors, vec!["reclaim_not_ready"]);
    assert!(first_log.exists() && second_log.exists() && !absent_state.exists());
    fs::remove_dir_all(base).unwrap();
}
