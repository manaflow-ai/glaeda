use std::fs::{self, OpenOptions};

use glaeda::{
    artifact::{CommitId, GitTreeId, RepositoryRef, Sha256Digest},
    rolling_tip_ledger::{
        ArtifactProvenance, BuildAdmission, BuildExecutionState, BuildKey, PromotionDecision,
        TipStream,
    },
    rolling_tip_ledger_store::{RollingTipLedgerStore, RollingTipLedgerStoreError},
};

fn stream() -> TipStream {
    TipStream::new(
        RepositoryRef::parse("manaflow-ai/cmux").expect("repo"),
        "feat-cmux-next",
    )
    .expect("stream")
}

fn digest(byte: &str) -> Sha256Digest {
    Sha256Digest::parse(&format!("sha256:{}", byte.repeat(64))).expect("digest")
}

fn build(commit_byte: &str) -> BuildKey {
    BuildKey::new(
        stream(),
        CommitId::parse(&commit_byte.repeat(40)).expect("commit"),
        GitTreeId::parse(&"b".repeat(40)).expect("tree"),
        "workflow-v1",
        Some(digest("f")),
        Some(digest("e")),
        "swift-6",
        Some(digest("d")),
        Some(digest("c")),
        Some(digest("b")),
        "cmux-next",
        "macos",
        "arm64",
        "debug",
    )
    .expect("build")
}

fn artifact(build: BuildKey) -> ArtifactProvenance {
    ArtifactProvenance::new(
        "dev-build-1",
        build,
        digest("a"),
        "github-actions",
        42,
        1,
        1,
    )
    .expect("artifact")
}

#[test]
fn durable_store_replays_artifact_reuse_and_promotion() {
    let root = tempfile_path();
    let first = build("a");
    let mut store = RollingTipLedgerStore::open(&root, stream()).expect("open");
    assert_eq!(
        store.admit(first.clone(), 1).expect("admit"),
        BuildAdmission::Queued
    );
    store
        .record_state(first.clone(), BuildExecutionState::Succeeded, 1, 2)
        .expect("success");
    store
        .publish_artifact(artifact(first.clone()), 3)
        .expect("publish");
    store
        .request_promotion(first.clone(), 0, 4)
        .expect("request");
    assert_eq!(
        store
            .decide_promotion(first.clone(), PromotionDecision::Accepted, 0, 5)
            .expect("accept"),
        1
    );
    let before = store.receipt();
    store.close().expect("close");

    let mut reopened = RollingTipLedgerStore::open(&root, stream()).expect("reopen");
    assert_eq!(reopened.receipt(), before);
    assert_eq!(reopened.snapshot().events(), store_snapshot_events(&root));
    assert_eq!(
        reopened.admit(first, 6).expect("reuse"),
        BuildAdmission::Reused(artifact(build("a")))
    );
    reopened.close().expect("close reopened");
}

#[test]
fn incomplete_tail_is_truncated_before_replay() {
    let root = tempfile_path();
    let mut store = RollingTipLedgerStore::open(&root, stream()).expect("open");
    store.admit(build("a"), 1).expect("admit");
    let count = store.receipt().event_count;
    store.close().expect("close");
    let mut file = OpenOptions::new().append(true).open(&root).expect("append");
    std::io::Write::write_all(&mut file, br#"{"schema_version":1"#).expect("partial");
    drop(file);
    let mut reopened = RollingTipLedgerStore::open(&root, stream()).expect("reopen");
    assert_eq!(reopened.receipt().event_count, count);
    assert!(fs::read(&root).expect("journal").last() == Some(&b'\n'));
    reopened.close().expect("close reopened");
}

#[test]
fn failed_operation_is_not_committed() {
    let root = tempfile_path();
    let first = build("a");
    let mut store = RollingTipLedgerStore::open(&root, stream()).expect("open");
    store.admit(first.clone(), 1).expect("admit");
    assert!(
        store
            .record_state(first, BuildExecutionState::Succeeded, 2, 2)
            .is_err()
    );
    assert_eq!(store.receipt().event_count, 3);
    store.close().expect("close");
    let mut reopened = RollingTipLedgerStore::open(&root, stream()).expect("reopen");
    assert_eq!(reopened.receipt().event_count, 3);
    reopened.close().expect("close reopened");
}

#[test]
fn reordered_event_is_rejected_on_open() {
    let root = tempfile_path();
    let mut store = RollingTipLedgerStore::open(&root, stream()).expect("open");
    store.admit(build("a"), 1).expect("admit");
    store.close().expect("close");
    let bytes = fs::read(&root).expect("journal");
    let mut lines: Vec<serde_json::Value> = bytes
        .split(|byte| *byte == b'\n')
        .filter(|line| !line.is_empty())
        .map(|line| serde_json::from_slice(line).expect("event JSON"))
        .collect();
    lines[1]["sequence"] = serde_json::json!(1);
    let encoded = lines
        .into_iter()
        .map(|line| serde_json::to_vec(&line).expect("encode"))
        .collect::<Vec<_>>()
        .join(&b'\n');
    fs::write(&root, [encoded, vec![b'\n']].concat()).expect("rewrite");
    assert!(matches!(
        RollingTipLedgerStore::open(&root, stream()),
        Err(RollingTipLedgerStoreError::Ledger(_))
    ));
}

fn tempfile_path() -> std::path::PathBuf {
    let root = std::env::temp_dir().join(format!(
        "glaeda-tip-ledger-{}-{}.jsonl",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("clock")
            .as_nanos()
    ));
    let _ = fs::remove_file(&root);
    root
}

fn store_snapshot_events(path: &std::path::Path) -> Vec<glaeda::rolling_tip_ledger::EventEnvelope> {
    let bytes = fs::read(path).expect("journal");
    bytes
        .split(|byte| *byte == b'\n')
        .filter(|line| !line.is_empty())
        .map(|line| serde_json::from_slice(line).expect("event"))
        .collect()
}

mod lease_fencing_tests {
    use glaeda::{
        artifact::{CommitId, GitTreeId, RepositoryRef, Sha256Digest},
        rolling_tip_ledger::{
            BuildExecutionState, BuildKey, EventEnvelope, RollingTipEvent, RollingTipLedger,
            RollingTipLedgerError, TipStream,
        },
        rolling_tip_ledger_store::RollingTipLedgerStore,
    };

    fn stream() -> TipStream {
        TipStream::new(
            RepositoryRef::parse("manaflow-ai/cmux").expect("repo"),
            "feat-cmux-next",
        )
        .expect("stream")
    }

    fn build(commit_byte: &str) -> BuildKey {
        let digest = |byte: &str| {
            Sha256Digest::parse(&format!("sha256:{}", byte.repeat(64))).expect("digest")
        };
        BuildKey::new(
            stream(),
            CommitId::parse(&commit_byte.repeat(40)).expect("commit"),
            GitTreeId::parse(&"b".repeat(40)).expect("tree"),
            "workflow-v1",
            Some(digest("f")),
            Some(digest("e")),
            "swift-6",
            Some(digest("d")),
            Some(digest("c")),
            Some(digest("b")),
            "cmux-next",
            "macos",
            "arm64",
            "debug",
        )
        .expect("build")
    }

    fn leased_ledger() -> (RollingTipLedger, BuildKey) {
        let first = build("a");
        let mut ledger = RollingTipLedger::new(stream());
        ledger.admit(first.clone(), 1).expect("admit");
        ledger
            .record_state(first.clone(), BuildExecutionState::Leased, 1, 2)
            .expect("leased state");
        (ledger, first)
    }

    #[test]
    fn active_lease_requires_exact_fence_and_owner_evidence() {
        let (mut ledger, first) = leased_ledger();
        ledger
            .acquire_lease(first.clone(), 1, "lease-1", "cmux16", 7, 10, 3)
            .expect("acquire");
        assert_eq!(
            ledger.active_lease(&first).expect("active").fencing_token,
            7
        );
        assert_eq!(
            ledger.acquire_lease(first.clone(), 1, "lease-2", "cmux17", 8, 11, 4),
            Err(RollingTipLedgerError::LeaseAlreadyActive)
        );
        assert_eq!(
            ledger.release_lease(first.clone(), 1, "lease-1", "cmux17", 7, 10, 5),
            Err(RollingTipLedgerError::LeaseHostMismatch)
        );
        assert_eq!(
            ledger.release_lease(first.clone(), 1, "lease-1", "cmux16", 8, 10, 5),
            Err(RollingTipLedgerError::LeaseFencingMismatch)
        );
        assert_eq!(
            ledger.release_lease(first.clone(), 2, "lease-1", "cmux16", 7, 10, 5),
            Err(RollingTipLedgerError::LeaseAttemptMismatch {
                expected: 1,
                actual: 2,
            })
        );
        assert_eq!(
            ledger.release_lease(first.clone(), 1, "lease-1", "cmux16", 7, 10, 11),
            Err(RollingTipLedgerError::LeaseExpired)
        );
        assert_eq!(
            ledger.reclaim_expired_lease(
                first.clone(),
                1,
                "lease-1",
                "cmux16",
                7,
                10,
                "glaeda-controller",
                10,
            ),
            Err(RollingTipLedgerError::LeaseNotExpired)
        );
        ledger
            .reclaim_expired_lease(
                first.clone(),
                1,
                "lease-1",
                "cmux16",
                7,
                10,
                "glaeda-controller",
                11,
            )
            .expect("reclaim expired lease");
        assert!(ledger.active_lease(&first).is_none());
        ledger
            .acquire_lease(first.clone(), 1, "lease-2", "cmux17", 8, 20, 12)
            .expect("new fence after release");
    }

    #[test]
    fn acquire_rejects_unknown_attempt_and_wrong_state() {
        let first = build("a");
        let mut ledger = RollingTipLedger::new(stream());
        ledger.admit(first.clone(), 1).expect("admit");
        assert_eq!(
            ledger.acquire_lease(first.clone(), 1, "lease-1", "cmux16", 1, 10, 2),
            Err(RollingTipLedgerError::InvalidLeaseState)
        );
        assert_eq!(
            ledger.acquire_lease(first, 2, "lease-1", "cmux16", 1, 10, 2),
            Err(RollingTipLedgerError::LeaseAttemptMismatch {
                expected: 1,
                actual: 2,
            })
        );
    }

    #[test]
    fn stale_release_cannot_clear_a_new_attempt() {
        let (mut ledger, first) = leased_ledger();
        ledger
            .acquire_lease(first.clone(), 1, "lease-1", "cmux16", 7, 20, 3)
            .expect("acquire attempt one");
        ledger
            .record_state(first.clone(), BuildExecutionState::Failed, 1, 4)
            .expect("fail attempt one");
        ledger
            .release_lease(first.clone(), 1, "lease-1", "cmux16", 7, 20, 5)
            .expect("release attempt one");
        ledger
            .record_state(first.clone(), BuildExecutionState::Queued, 2, 6)
            .expect("queue attempt two");
        ledger
            .record_state(first.clone(), BuildExecutionState::Leased, 2, 7)
            .expect("lease state attempt two");
        ledger
            .acquire_lease(first.clone(), 2, "lease-2", "cmux17", 1, 30, 8)
            .expect("acquire attempt two");
        assert_eq!(
            ledger.release_lease(first.clone(), 1, "lease-1", "cmux16", 7, 20, 9),
            Err(RollingTipLedgerError::LeaseAttemptMismatch {
                expected: 2,
                actual: 1,
            })
        );
        assert_eq!(
            ledger
                .active_lease(&first)
                .expect("attempt two active")
                .attempt,
            2
        );
    }

    #[test]
    fn replay_rejects_duplicate_acquire_and_release_before_acquire() {
        let (mut ledger, first) = leased_ledger();
        ledger
            .acquire_lease(first.clone(), 1, "lease-1", "cmux16", 7, 20, 3)
            .expect("acquire");
        let mut duplicate = ledger.events().to_vec();
        let mut event = duplicate.last().expect("acquire event").clone();
        event.sequence += 1;
        event.event_id = format!("feat-cmux-next-{:016x}", event.sequence);
        duplicate.push(event);
        assert!(matches!(
            RollingTipLedger::replay(stream(), duplicate),
            Err(RollingTipLedgerError::LeaseAlreadyActive)
        ));

        let mut release_first = ledger.events().to_vec();
        let release = EventEnvelope {
            schema_version: glaeda::rolling_tip_ledger::ROLLING_TIP_LEDGER_SCHEMA_VERSION,
            sequence: release_first.last().expect("event").sequence + 1,
            event_id: "feat-cmux-next-0000000000000006".to_owned(),
            stream: stream(),
            occurred_at_millis: 4,
            event: RollingTipEvent::LeaseReleased {
                build: first,
                attempt: 1,
                lease_id: "lease-unknown".to_owned(),
                host_id: "cmux16".to_owned(),
                fencing_token: 7,
                expires_at_millis: 20,
            },
        };
        release_first.push(release);
        assert!(matches!(
            RollingTipLedger::replay(stream(), release_first),
            Err(RollingTipLedgerError::LeaseIdMismatch)
        ));
    }

    #[test]
    fn replay_migrates_v1_non_lease_events_after_schema_bump() {
        let mut ledger = RollingTipLedger::new(stream());
        ledger.admit(build("a"), 1).expect("admit");
        let mut legacy = ledger.events().to_vec();
        for event in &mut legacy {
            event.schema_version = 1;
        }
        let replayed = RollingTipLedger::replay(stream(), legacy).expect("v1 replay");
        assert_eq!(replayed.events().len(), ledger.events().len());
    }

    #[test]
    fn durable_replay_retains_active_lease_identity_and_expiry() {
        let path = std::env::temp_dir().join(format!(
            "glaeda-lease-replay-{}-{}.jsonl",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .expect("clock")
                .as_nanos()
        ));
        let first = build("a");
        let mut store = RollingTipLedgerStore::open(&path, stream()).expect("open");
        store.admit(first.clone(), 1).expect("admit");
        store
            .record_state(first.clone(), BuildExecutionState::Leased, 1, 2)
            .expect("leased state");
        store
            .acquire_lease(first.clone(), 1, "lease-1", "cmux16", 7, 20, 3)
            .expect("acquire");
        let expected = store.snapshot().active_lease(&first).cloned();
        store.close().expect("close");

        let mut reopened = RollingTipLedgerStore::open(&path, stream()).expect("reopen");
        assert_eq!(reopened.snapshot().active_lease(&first).cloned(), expected);
        reopened
            .release_lease(first, 1, "lease-1", "cmux16", 7, 20, 4)
            .expect("release after replay");
        reopened.close().expect("close reopened");
        let _ = std::fs::remove_file(path);
    }
}
