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
