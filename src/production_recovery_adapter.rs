//! Report-only production recovery composition with a synthetic apply boundary.
//!
//! This adapter consumes the public runner observer report/action types and adds a
//! durable lifecycle projection for acknowledgement, cancellation, and restart. Its
//! apply effect is deliberately limited to an in-memory synthetic projection: there
//! is no live fleet, filesystem, or process deletion backend in this milestone.

use std::collections::BTreeSet;
use std::path::Path;

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::runner_log_reclaim::{
    FileIdentity, ItemDecision, PlannedAction, Policy as ReclaimPolicy, ProcessEvidence,
    RUNNER_LOG_RECLAIM_SCHEMA_VERSION, Report, RunnerEnrollmentReceipt,
};
use crate::runner_log_reclaim_store::{RunnerLogStore, StoreError};

const MAX_ACTIONS: usize = 128;
const MAX_EVENTS: usize = 1_024;
const MAX_RECEIPT_STRING_BYTES: usize = 1_024;
const SYNTHETIC_TOKEN_DOMAIN: u64 = 0x5349_4e54_4845_5449;

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, PartialOrd, Ord)]
pub struct ObserverIdentity(u64);

impl ObserverIdentity {
    pub fn new(generation: u64) -> Result<Self, AdapterError> {
        if generation == 0 {
            return Err(AdapterError::InvalidIdentity("observer_generation_zero"));
        }
        Ok(Self(generation))
    }

    pub fn generation(self) -> u64 {
        self.0
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, PartialOrd, Ord)]
pub struct StoreIdentity(u64);

impl StoreIdentity {
    pub fn new(generation: u64) -> Result<Self, AdapterError> {
        if generation == 0 {
            return Err(AdapterError::InvalidIdentity("store_generation_zero"));
        }
        Ok(Self(generation))
    }

    pub fn generation(self) -> u64 {
        self.0
    }
}

/// Exact observer/store identity captured by one fresh report and its projection store.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
pub struct FreshIdentity {
    pub observer: ObserverIdentity,
    pub store: StoreIdentity,
    pub revision: u64,
}

impl FreshIdentity {
    pub fn new(
        observer: ObserverIdentity,
        store: StoreIdentity,
        revision: u64,
    ) -> Result<Self, AdapterError> {
        if revision == 0 {
            return Err(AdapterError::InvalidIdentity("revision_zero"));
        }
        Ok(Self {
            observer,
            store,
            revision,
        })
    }
}

/// Typed apply admission. The only constructor is explicitly synthetic and benchmark-only.
/// A production caller cannot turn a CLI boolean into this capability, and this adapter has
/// no live deletion backend to invoke.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SyntheticApplyAdmission {
    identity: FreshIdentity,
    token: u64,
}

impl SyntheticApplyAdmission {
    pub fn for_benchmark(identity: FreshIdentity) -> Self {
        let token = SYNTHETIC_TOKEN_DOMAIN
            ^ identity.observer.generation()
            ^ identity.store.generation().rotate_left(17)
            ^ identity.revision.rotate_left(31);
        Self {
            identity,
            token: token | 1,
        }
    }

    pub fn identity(self) -> FreshIdentity {
        self.identity
    }

    pub fn is_synthetic(self) -> bool {
        self.token != 0
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
pub enum Phase {
    Empty,
    Inspected,
    Acknowledged,
    Cancelled,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ActionSnapshot {
    pub relative_path: String,
    pub identity: FileIdentity,
    pub decision: ItemDecision,
    pub receipt_id: String,
}

impl From<&PlannedAction> for ActionSnapshot {
    fn from(action: &PlannedAction) -> Self {
        Self {
            relative_path: action.relative_path.clone(),
            identity: action.identity,
            decision: action.decision,
            receipt_id: action.receipt_id.clone(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct InspectionReceipt {
    pub identity: FreshIdentity,
    pub plan_digest: String,
    pub actions: Vec<ActionSnapshot>,
    pub complete: bool,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
pub struct ApplyReceipt {
    pub effect: &'static str,
    pub projected: u64,
    pub already_projected: u64,
    pub recovery: RecoveryReceipt,
    pub items: Vec<ItemReceipt>,
    pub post_action: Option<PostActionReceipt>,
}

/// Bounded evidence that the store recovery pass ran before this admission.
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
pub struct RecoveryReceipt {
    pub pending_before: u64,
    pub repaired: u64,
    pub pending_after: u64,
    pub disposition: String,
}

impl RecoveryReceipt {
    fn from_store(receipt: crate::runner_log_reclaim_store::RecoveryReceipt) -> Self {
        let repaired = receipt
            .confirmed_deleted
            .saturating_add(receipt.absent_after_delete_intent)
            .saturating_add(receipt.aborted_prepared)
            .saturating_add(receipt.aborted_staged);
        Self {
            pending_before: receipt.pending + repaired,
            repaired,
            pending_after: receipt.pending,
            disposition: if receipt.pending == 0 {
                "clean_after_recovery".into()
            } else {
                "pending_recovery".into()
            },
        }
    }
}

/// One bounded outcome for one exact planned action.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ItemReceipt {
    pub admission_id: String,
    pub receipt_id: String,
    pub relative_path: String,
    pub identity: FileIdentity,
    pub outcome: &'static str,
}

/// Fresh synthetic post-action evidence.  No private path or raw process evidence is exposed.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PostActionReceipt {
    pub identity: FreshIdentity,
    pub pending_recovery: u64,
    pub projected: u64,
    pub already_projected: u64,
    pub complete: bool,
}

/// The production adapter's typed admission token.  Fields are private so callers cannot mint
/// an apply capability from a boolean, arbitrary candidate list, or stale report.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StoreAdmission {
    receipt: AdmissionReceipt,
    token: SyntheticApplyAdmission,
    backend: AdmissionBackend,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum AdmissionBackend {
    Synthetic,
    DescriptorBoundStore,
}

impl StoreAdmission {
    pub fn receipt(&self) -> &AdmissionReceipt {
        &self.receipt
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct AdmissionReceipt {
    pub admission_id: String,
    pub identity: FreshIdentity,
    pub plan_digest: String,
    pub recovery: RecoveryReceipt,
    pub actions: Vec<ActionSnapshot>,
}

/// Explicitly synthetic recovery backend used by tests and benchmark callers.  A production
/// live filesystem backend is intentionally absent until its independent acceptance gate lands.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SyntheticStoreBackend {
    pending_recovery: u64,
}

/// Typed fresh observer used by the descriptor-bound store admission path.  The adapter never
/// accepts an untyped observation closure from a production caller.
pub trait FreshProcessObserver {
    fn observe(&mut self) -> Result<ProcessEvidence, StoreError>;
}

fn plan_digest(
    identity: FreshIdentity,
    actions: &[ActionSnapshot],
) -> Result<String, AdapterError> {
    let bytes = serde_json::to_vec(&(identity, actions))
        .map_err(|_| AdapterError::InvalidReport("plan_digest_failed"))?;
    let digest = Sha256::digest(bytes);
    Ok(format!(
        "sha256:{}",
        digest
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>()
    ))
}

impl SyntheticStoreBackend {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn with_pending_recovery(pending: u64) -> Self {
        Self {
            pending_recovery: pending,
        }
    }

    pub fn pending_recovery(&self) -> u64 {
        self.pending_recovery
    }

    fn recover(&mut self) -> RecoveryReceipt {
        let pending = self.pending_recovery;
        RecoveryReceipt {
            pending_before: pending,
            repaired: 0,
            pending_after: pending,
            disposition: if pending == 0 {
                "clean_after_recovery".into()
            } else {
                "pending_recovery".into()
            },
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
enum Event {
    Inspected {
        identity: FreshIdentity,
        plan_digest: String,
        actions: Vec<ActionSnapshot>,
        complete: bool,
    },
    Acknowledged {
        identity: FreshIdentity,
    },
    Cancelled {
        identity: FreshIdentity,
    },
    Projected {
        identity: FreshIdentity,
        receipt_id: String,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
struct Journal {
    schema_version: u8,
    events: Vec<Event>,
}

/// Report-only by default. Applying only records synthetic projections after typed admission.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ProductionRecoveryAdapter {
    phase: Phase,
    identity: Option<FreshIdentity>,
    plan_digest: String,
    actions: Vec<ActionSnapshot>,
    complete: bool,
    projected: BTreeSet<String>,
    events: Vec<Event>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AdapterError {
    InvalidIdentity(&'static str),
    InvalidReport(&'static str),
    InvalidTransition(&'static str),
    AdmissionRequired,
    AdmissionIdentityMismatch,
    RecoveryPending(RecoveryReceipt),
    LiveApplyDisabled,
    JournalTooLarge,
    JournalCorrupt,
    StoreRefused(&'static str),
}

impl std::fmt::Display for AdapterError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::InvalidIdentity(code)
            | Self::InvalidReport(code)
            | Self::InvalidTransition(code) => f.write_str(code),
            Self::AdmissionRequired => f.write_str("apply_admission_required"),
            Self::AdmissionIdentityMismatch => f.write_str("fresh_identity_mismatch"),
            Self::RecoveryPending(_) => f.write_str("recovery_pending"),
            Self::LiveApplyDisabled => f.write_str("live_apply_disabled"),
            Self::JournalTooLarge => f.write_str("journal_too_large"),
            Self::JournalCorrupt => f.write_str("journal_corrupt"),
            Self::StoreRefused(code) => f.write_str(code),
        }
    }
}
impl std::error::Error for AdapterError {}

impl AdapterError {
    fn store(error: StoreError) -> Self {
        Self::StoreRefused(match error {
            StoreError::Invalid(code) | StoreError::Io(code) => code,
            StoreError::Busy => "store_busy",
            StoreError::ObservationIncomplete => "observation_incomplete",
        })
    }
}

impl Default for ProductionRecoveryAdapter {
    fn default() -> Self {
        Self {
            phase: Phase::Empty,
            identity: None,
            plan_digest: String::new(),
            actions: Vec::new(),
            complete: false,
            projected: BTreeSet::new(),
            events: Vec::new(),
        }
    }
}

impl ProductionRecoveryAdapter {
    pub fn phase(&self) -> Phase {
        self.phase
    }

    pub fn inspection(&self) -> Option<InspectionReceipt> {
        self.identity.map(|identity| InspectionReceipt {
            identity,
            plan_digest: self.plan_digest.clone(),
            actions: self.actions.clone(),
            complete: self.complete,
        })
    }

    /// Consume a report produced by the existing observer. A report is always inspected
    /// without invoking its native reclaim/apply path.
    pub fn inspect_report(
        &mut self,
        identity: FreshIdentity,
        report: &Report,
    ) -> Result<InspectionReceipt, AdapterError> {
        if report.document_type != "glaeda-runner-log-reclaim"
            || report.schema_version != RUNNER_LOG_RECLAIM_SCHEMA_VERSION
            || report.operation != "plan"
            || !report.errors.is_empty()
        {
            return Err(AdapterError::InvalidReport("report_not_plan_only"));
        }
        if !report.observation.complete {
            return Err(AdapterError::InvalidReport("observation_incomplete"));
        }
        if report.actions.len() > MAX_ACTIONS
            || report.actions.len() > report.batch_size as usize
            || report.batch_size == 0
        {
            return Err(AdapterError::InvalidReport("action_limit"));
        }
        let mut seen = BTreeSet::new();
        for action in &report.actions {
            if action.decision != ItemDecision::ExpiredRunnerDiagnosticLog
                || action.relative_path.len() > MAX_RECEIPT_STRING_BYTES
                || action.receipt_id.len() > MAX_RECEIPT_STRING_BYTES
                || !seen.insert(action.receipt_id.clone())
                || !report.observation.catalog.iter().any(|item| {
                    item.relative_path == action.relative_path
                        && item.identity == action.identity
                        && item.decision == action.decision
                })
            {
                return Err(AdapterError::InvalidReport(
                    "action_not_bound_to_observation",
                ));
            }
        }
        self.inspect_actions(identity, &report.actions, true)
    }

    /// Recover the synthetic store before creating a typed admission. Any unresolved recovery
    /// debt is a hard veto and is returned with its bounded receipt.
    pub fn admit_report(
        &mut self,
        identity: FreshIdentity,
        report: &Report,
        backend: &mut SyntheticStoreBackend,
    ) -> Result<StoreAdmission, AdapterError> {
        let recovery = backend.recover();
        if recovery.pending_after != 0 {
            return Err(AdapterError::RecoveryPending(recovery));
        }
        let inspection = self.inspect_report(identity, report)?;
        Ok(StoreAdmission {
            receipt: AdmissionReceipt {
                admission_id: inspection.plan_digest.clone(),
                identity,
                plan_digest: inspection.plan_digest,
                recovery,
                actions: inspection.actions,
            },
            token: SyntheticApplyAdmission::for_benchmark(identity),
            backend: AdmissionBackend::Synthetic,
        })
    }

    /// Admit one exact report against the descriptor-bound store. Recovery is performed while
    /// the store's non-blocking lock is held, before the report can become new work. This method
    /// never invokes `apply_batch`; live deletion remains explicitly disabled at apply time.
    #[allow(clippy::too_many_arguments)]
    pub fn admit_descriptor_store<O: FreshProcessObserver>(
        &mut self,
        identity: FreshIdentity,
        report: &Report,
        root: &Path,
        enrollment: &RunnerEnrollmentReceipt,
        state_root: &Path,
        policy: ReclaimPolicy,
        observer: &mut O,
    ) -> Result<StoreAdmission, AdapterError> {
        let mut store =
            RunnerLogStore::open(root, enrollment, state_root).map_err(AdapterError::store)?;
        if identity.store.generation() != store.store_generation() {
            return Err(AdapterError::AdmissionIdentityMismatch);
        }
        let recovery = store
            .recover(policy, || observer.observe())
            .map_err(AdapterError::store)?;
        let recovery = RecoveryReceipt::from_store(recovery);
        if recovery.pending_after != 0 {
            return Err(AdapterError::RecoveryPending(recovery));
        }
        let inspection = self.inspect_report(identity, report)?;
        Ok(StoreAdmission {
            receipt: AdmissionReceipt {
                admission_id: inspection.plan_digest.clone(),
                identity,
                plan_digest: inspection.plan_digest,
                recovery,
                actions: inspection.actions,
            },
            token: SyntheticApplyAdmission::for_benchmark(identity),
            backend: AdmissionBackend::DescriptorBoundStore,
        })
    }

    /// Synthetic/test admission path for benchmark fixtures. This only records an
    /// in-memory projection and never calls the native descriptor-bound store.
    pub fn inspect_actions(
        &mut self,
        identity: FreshIdentity,
        actions: &[PlannedAction],
        complete: bool,
    ) -> Result<InspectionReceipt, AdapterError> {
        if actions.len() > MAX_ACTIONS {
            return Err(AdapterError::InvalidReport("action_limit"));
        }
        if actions
            .iter()
            .any(|action| action.decision != ItemDecision::ExpiredRunnerDiagnosticLog)
        {
            return Err(AdapterError::InvalidReport("action_not_expired"));
        }
        if self.phase != Phase::Empty {
            return Err(AdapterError::InvalidTransition("already_inspected"));
        }
        let actions = actions.iter().map(ActionSnapshot::from).collect::<Vec<_>>();
        let plan_digest = plan_digest(identity, &actions)?;
        self.append(Event::Inspected {
            identity,
            plan_digest: plan_digest.clone(),
            actions: actions.clone(),
            complete,
        })?;
        Ok(InspectionReceipt {
            identity,
            plan_digest,
            actions,
            complete,
        })
    }

    pub fn acknowledge(&mut self) -> Result<(), AdapterError> {
        let identity = self
            .identity
            .ok_or(AdapterError::InvalidTransition("nothing_to_acknowledge"))?;
        if self.phase != Phase::Inspected {
            return Err(AdapterError::InvalidTransition(
                "acknowledgement_not_allowed",
            ));
        }
        self.append(Event::Acknowledged { identity })
    }

    pub fn cancel(&mut self) -> Result<(), AdapterError> {
        let identity = self
            .identity
            .ok_or(AdapterError::InvalidTransition("nothing_to_cancel"))?;
        if matches!(self.phase, Phase::Cancelled) {
            return Ok(());
        }
        if matches!(self.phase, Phase::Empty) {
            return Err(AdapterError::InvalidTransition("nothing_to_cancel"));
        }
        self.append(Event::Cancelled { identity })
    }

    pub fn apply(
        &mut self,
        admission: Option<&SyntheticApplyAdmission>,
    ) -> Result<ApplyReceipt, AdapterError> {
        let admission = admission.ok_or(AdapterError::AdmissionRequired)?;
        let identity = self
            .identity
            .ok_or(AdapterError::InvalidTransition("nothing_to_apply"))?;
        if admission.identity != identity || !admission.is_synthetic() {
            return Err(AdapterError::AdmissionIdentityMismatch);
        }
        if self.phase != Phase::Acknowledged {
            return Err(AdapterError::InvalidTransition("acknowledgement_required"));
        }
        let mut receipt = ApplyReceipt {
            effect: "synthetic_projection_only",
            ..ApplyReceipt::default()
        };
        for action in self.actions.clone() {
            if self.projected.contains(&action.receipt_id) {
                receipt.already_projected += 1;
                receipt.items.push(ItemReceipt {
                    admission_id: self.plan_digest.clone(),
                    receipt_id: action.receipt_id,
                    relative_path: action.relative_path,
                    identity: action.identity,
                    outcome: "already_projected",
                });
                continue;
            }
            let receipt_id = action.receipt_id.clone();
            self.append(Event::Projected {
                identity,
                receipt_id,
            })?;
            receipt.projected += 1;
            receipt.items.push(ItemReceipt {
                admission_id: self.plan_digest.clone(),
                receipt_id: action.receipt_id,
                relative_path: action.relative_path,
                identity: action.identity,
                outcome: "reclaimed",
            });
        }
        receipt.post_action = Some(PostActionReceipt {
            identity,
            pending_recovery: 0,
            projected: receipt.projected,
            already_projected: receipt.already_projected,
            complete: true,
        });
        Ok(receipt)
    }

    /// Consume an adapter-owned typed admission. This is the only public apply path for the
    /// production-visible adapter; its effect remains an in-memory synthetic projection.
    pub fn apply_admitted(
        &mut self,
        admission: &StoreAdmission,
    ) -> Result<ApplyReceipt, AdapterError> {
        let identity = self
            .identity
            .ok_or(AdapterError::InvalidTransition("nothing_to_apply"))?;
        if admission.receipt.identity != identity
            || admission.receipt.plan_digest != self.plan_digest
            || admission.token.identity() != identity
        {
            return Err(AdapterError::AdmissionIdentityMismatch);
        }
        if admission.backend == AdmissionBackend::DescriptorBoundStore {
            return Err(AdapterError::LiveApplyDisabled);
        }
        if self.phase == Phase::Inspected {
            self.acknowledge()?;
        }
        let mut receipt = self.apply(Some(&admission.token))?;
        receipt.recovery = admission.receipt.recovery.clone();
        for item in &mut receipt.items {
            item.admission_id = admission.receipt.admission_id.clone();
        }
        if let Some(post_action) = &mut receipt.post_action {
            post_action.pending_recovery = admission.receipt.recovery.pending_after;
        }
        Ok(receipt)
    }

    /// Live filesystem deletion is intentionally disabled until the independent store acceptance
    /// gate lands. Keeping this refusal explicit prevents a caller from treating the synthetic
    /// backend as a production mutation capability.
    pub fn apply_live(
        &mut self,
        _admission: &StoreAdmission,
    ) -> Result<ApplyReceipt, AdapterError> {
        Err(AdapterError::LiveApplyDisabled)
    }

    /// Serialize the durable lifecycle journal for a crash/restart boundary.
    pub fn checkpoint(&self) -> Result<Vec<u8>, AdapterError> {
        serde_json::to_vec(&Journal {
            schema_version: 1,
            events: self.events.clone(),
        })
        .map_err(|_| AdapterError::JournalCorrupt)
    }

    /// Rebuild state from the durable journal and replay projections idempotently.
    pub fn restart(bytes: &[u8]) -> Result<Self, AdapterError> {
        if bytes.len() > 1 << 20 {
            return Err(AdapterError::JournalTooLarge);
        }
        let journal: Journal =
            serde_json::from_slice(bytes).map_err(|_| AdapterError::JournalCorrupt)?;
        if journal.schema_version != 1 || journal.events.len() > MAX_EVENTS {
            return Err(AdapterError::JournalCorrupt);
        }
        let mut adapter = Self::default();
        for event in journal.events {
            adapter.append(event)?;
        }
        Ok(adapter)
    }

    fn append(&mut self, event: Event) -> Result<(), AdapterError> {
        if self.events.len() >= MAX_EVENTS {
            return Err(AdapterError::JournalTooLarge);
        }
        match &event {
            Event::Inspected {
                identity,
                plan_digest,
                actions,
                complete,
            } => {
                self.phase = Phase::Inspected;
                self.identity = Some(*identity);
                self.plan_digest = plan_digest.clone();
                self.actions = actions.clone();
                self.complete = *complete;
            }
            Event::Acknowledged { identity } => {
                if self.identity != Some(*identity) || self.phase != Phase::Inspected {
                    return Err(AdapterError::InvalidTransition(
                        "acknowledgement_not_allowed",
                    ));
                }
                self.phase = Phase::Acknowledged;
            }
            Event::Cancelled { identity } => {
                if self.identity != Some(*identity) || matches!(self.phase, Phase::Empty) {
                    return Err(AdapterError::InvalidTransition("cancellation_not_allowed"));
                }
                self.phase = Phase::Cancelled;
            }
            Event::Projected {
                identity,
                receipt_id,
            } => {
                if self.identity != Some(*identity) || self.phase != Phase::Acknowledged {
                    return Err(AdapterError::InvalidTransition("projection_not_allowed"));
                }
                self.projected.insert(receipt_id.clone());
            }
        }
        self.events.push(event);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::runner_log_reclaim::{
        AuthorityEvidence, FileIdentity, ItemDecision, Observation, ProcessEvidence,
        RecoveryEvidence,
    };
    use std::fs;
    use std::os::unix::fs::{MetadataExt, PermissionsExt};
    use std::path::PathBuf;

    fn identity() -> FreshIdentity {
        FreshIdentity::new(
            ObserverIdentity::new(11).unwrap(),
            StoreIdentity::new(22).unwrap(),
            33,
        )
        .unwrap()
    }

    fn action(id: &str) -> PlannedAction {
        PlannedAction {
            relative_path: format!("_diag/{id}.log"),
            identity: FileIdentity {
                device: 1,
                inode: id.as_bytes()[0] as u64,
                links: 1,
                size: 1,
                modified_seconds: 1,
                changed_seconds: 1,
                modified_nanos: 0,
                changed_nanos: 0,
            },
            decision: ItemDecision::ExpiredRunnerDiagnosticLog,
            receipt_id: id.into(),
        }
    }

    fn empty_report() -> Report {
        Report {
            document_type: "glaeda-runner-log-reclaim",
            schema_version: RUNNER_LOG_RECLAIM_SCHEMA_VERSION,
            operation: "plan",
            runner_root: "<enrolled-runner>".into(),
            minimum_idle_seconds: 3_600,
            batch_size: 4,
            observation: Observation {
                runner_root: "<enrolled-runner>".into(),
                authority: AuthorityEvidence {
                    kind: "enrolled_runner_uid",
                    uid: rustix::process::geteuid().as_raw(),
                    scope_complete: true,
                },
                process: ProcessEvidence::test_complete(),
                items: Vec::new(),
                catalog: Vec::new(),
                item_count: 0,
                eligible_count: 0,
                enumeration_truncated: false,
                items_truncated: false,
                complete: true,
            },
            actions: Vec::new(),
            remaining_count: 0,
            reclaimed_count: 0,
            recovery: RecoveryEvidence {
                state_root: None,
                pending_count: 0,
            },
            errors: Vec::new(),
        }
    }

    struct StubObserver(ProcessEvidence);

    impl FreshProcessObserver for StubObserver {
        fn observe(&mut self) -> Result<ProcessEvidence, StoreError> {
            Ok(self.0.clone())
        }
    }

    fn real_fixture() -> (PathBuf, RunnerEnrollmentReceipt, PathBuf, FreshIdentity) {
        let root = std::env::temp_dir().join(format!(
            "glaeda-admission-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let root = fs::canonicalize(&root).unwrap();
        let runner = root.join("runner");
        let state = root.join("state");
        fs::create_dir_all(runner.join("_diag")).unwrap();
        fs::create_dir_all(&state).unwrap();
        fs::set_permissions(&runner, fs::Permissions::from_mode(0o700)).unwrap();
        fs::set_permissions(&state, fs::Permissions::from_mode(0o700)).unwrap();
        fs::write(runner.join(".glaeda-cmux-runner"), "install\n").unwrap();
        let enrollment = RunnerEnrollmentReceipt {
            runner_dir_owned: true,
            runner_dir: runner.to_string_lossy().into_owned(),
            install_id: "install".into(),
        };
        let store = RunnerLogStore::open(&runner, &enrollment, &state).unwrap();
        let store_generation = store.store_generation();
        drop(store);
        let identity = FreshIdentity::new(
            ObserverIdentity::new(1).unwrap(),
            StoreIdentity::new(store_generation).unwrap(),
            1,
        )
        .unwrap();
        (root, enrollment, state, identity)
    }

    #[test]
    fn default_is_report_only_and_requires_typed_admission() {
        let mut adapter = ProductionRecoveryAdapter::default();
        adapter
            .inspect_actions(identity(), &[action("a")], true)
            .unwrap();
        assert_eq!(adapter.apply(None), Err(AdapterError::AdmissionRequired));
    }

    #[test]
    fn acknowledgement_cancellation_and_restart_recover_durable_projection() {
        let mut adapter = ProductionRecoveryAdapter::default();
        adapter
            .inspect_actions(identity(), &[action("a"), action("b")], true)
            .unwrap();
        adapter.acknowledge().unwrap();
        let admission = SyntheticApplyAdmission::for_benchmark(identity());
        let first = adapter.apply(Some(&admission)).unwrap();
        assert_eq!(first.projected, 2);
        assert_eq!(
            adapter.apply(Some(&admission)).unwrap().already_projected,
            2
        );

        let bytes = adapter.checkpoint().unwrap();
        let restarted = ProductionRecoveryAdapter::restart(&bytes).unwrap();
        assert_eq!(restarted.phase(), Phase::Acknowledged);
        assert_eq!(restarted.inspection().unwrap().actions.len(), 2);

        let mut cancelled = ProductionRecoveryAdapter::default();
        cancelled
            .inspect_actions(identity(), &[action("a")], true)
            .unwrap();
        cancelled.cancel().unwrap();
        assert_eq!(
            cancelled.apply(Some(&admission)),
            Err(AdapterError::InvalidTransition("acknowledgement_required"))
        );
    }

    #[test]
    fn stale_admission_identity_is_refused() {
        let mut adapter = ProductionRecoveryAdapter::default();
        adapter
            .inspect_actions(identity(), &[action("a")], true)
            .unwrap();
        adapter.acknowledge().unwrap();
        let other = FreshIdentity::new(
            ObserverIdentity::new(12).unwrap(),
            StoreIdentity::new(22).unwrap(),
            33,
        )
        .unwrap();
        let admission = SyntheticApplyAdmission::for_benchmark(other);
        assert_eq!(
            adapter.apply(Some(&admission)),
            Err(AdapterError::AdmissionIdentityMismatch)
        );
    }

    #[test]
    fn report_binding_rejects_actions_not_present_in_the_same_observation() {
        let mut report = empty_report();
        report.actions = vec![action("missing")];
        let mut adapter = ProductionRecoveryAdapter::default();
        assert_eq!(
            adapter.inspect_report(identity(), &report),
            Err(AdapterError::InvalidReport(
                "action_not_bound_to_observation"
            ))
        );
    }

    #[test]
    fn descriptor_admission_recovers_clean_store_and_keeps_live_apply_disabled() {
        let (root, enrollment, state, identity) = real_fixture();
        let runner = PathBuf::from(&enrollment.runner_dir);
        let mut adapter = ProductionRecoveryAdapter::default();
        let mut observer = StubObserver(ProcessEvidence::test_complete());
        let admission = adapter
            .admit_descriptor_store(
                identity,
                &empty_report(),
                &runner,
                &enrollment,
                &state,
                ReclaimPolicy::new(3_600, 4).unwrap(),
                &mut observer,
            )
            .unwrap();
        assert_eq!(admission.receipt().recovery.pending_after, 0);
        assert_eq!(
            adapter.apply_admitted(&admission),
            Err(AdapterError::LiveApplyDisabled)
        );
        assert_eq!(
            adapter.apply_live(&admission),
            Err(AdapterError::LiveApplyDisabled)
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn descriptor_admission_vetoes_pending_recovery_before_new_work() {
        let (root, enrollment, state, identity) = real_fixture();
        let runner = PathBuf::from(&enrollment.runner_dir);
        let staged = runner.join("_diag/.glaeda-reclaiming-pending");
        fs::write(&staged, b"pending").unwrap();
        let old = std::time::UNIX_EPOCH + std::time::Duration::from_secs(1_600_000_000);
        fs::File::options()
            .write(true)
            .open(&staged)
            .unwrap()
            .set_times(fs::FileTimes::new().set_modified(old))
            .unwrap();
        let metadata = fs::metadata(&staged).unwrap();
        let root_metadata = fs::metadata(&runner).unwrap();
        let uid = root_metadata.uid();
        let row = serde_json::json!({
            "tx_id": "pending",
            "phase": "staged",
            "install_id": "install",
            "root_device": root_metadata.dev(),
            "root_inode": root_metadata.ino(),
            "root_uid": uid,
            "relative_path": "_diag/pending.log",
            "stage_name": "_diag/.glaeda-reclaiming-pending",
            "identity": {
                "device": metadata.dev(), "inode": metadata.ino(), "links": metadata.nlink(),
                "size": metadata.len(), "modified_seconds": metadata.mtime(),
                "changed_seconds": metadata.ctime(), "modified_nanos": metadata.mtime_nsec(),
                "changed_nanos": metadata.ctime_nsec()
            }
        });
        fs::write(state.join("journal.jsonl"), format!("{}\n", row)).unwrap();
        let mut adapter = ProductionRecoveryAdapter::default();
        let mut observer = StubObserver(ProcessEvidence::test_open(FileIdentity {
            device: metadata.dev(),
            inode: metadata.ino(),
            links: metadata.nlink(),
            size: metadata.len(),
            modified_seconds: metadata.mtime(),
            changed_seconds: metadata.ctime(),
            modified_nanos: metadata.mtime_nsec(),
            changed_nanos: metadata.ctime_nsec(),
        }));
        let result = adapter.admit_descriptor_store(
            identity,
            &empty_report(),
            &runner,
            &enrollment,
            &state,
            ReclaimPolicy::new(3_600, 4).unwrap(),
            &mut observer,
        );
        assert!(
            matches!(result, Err(AdapterError::RecoveryPending(receipt)) if receipt.pending_after == 1)
        );
        assert!(
            staged.exists(),
            "pending recovery must preserve the staged file"
        );
        fs::remove_dir_all(root).unwrap();
    }
}
