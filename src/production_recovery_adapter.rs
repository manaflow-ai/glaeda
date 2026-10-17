//! Report-only production recovery composition with a synthetic apply boundary.
//!
//! This adapter consumes the public runner observer report/action types and adds a
//! durable lifecycle projection for acknowledgement, cancellation, and restart. Its
//! apply effect is deliberately limited to an in-memory synthetic projection: there
//! is no live fleet, filesystem, or process deletion backend in this milestone.

use std::collections::BTreeSet;

use serde::{Deserialize, Serialize};

use crate::runner_log_reclaim::{ItemDecision, PlannedAction, Report};

const MAX_ACTIONS: usize = 128;
const MAX_EVENTS: usize = 1_024;
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
        Self { identity, token }
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
    pub receipt_id: String,
}

impl From<&PlannedAction> for ActionSnapshot {
    fn from(action: &PlannedAction) -> Self {
        Self {
            relative_path: action.relative_path.clone(),
            receipt_id: action.receipt_id.clone(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct InspectionReceipt {
    pub identity: FreshIdentity,
    pub actions: Vec<ActionSnapshot>,
    pub complete: bool,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
pub struct ApplyReceipt {
    pub effect: &'static str,
    pub projected: u64,
    pub already_projected: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
enum Event {
    Inspected {
        identity: FreshIdentity,
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
    JournalTooLarge,
    JournalCorrupt,
}

impl std::fmt::Display for AdapterError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::InvalidIdentity(code)
            | Self::InvalidReport(code)
            | Self::InvalidTransition(code) => f.write_str(code),
            Self::AdmissionRequired => f.write_str("apply_admission_required"),
            Self::AdmissionIdentityMismatch => f.write_str("fresh_identity_mismatch"),
            Self::JournalTooLarge => f.write_str("journal_too_large"),
            Self::JournalCorrupt => f.write_str("journal_corrupt"),
        }
    }
}
impl std::error::Error for AdapterError {}

impl Default for ProductionRecoveryAdapter {
    fn default() -> Self {
        Self {
            phase: Phase::Empty,
            identity: None,
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
        if report.operation != "plan" || !report.errors.is_empty() {
            return Err(AdapterError::InvalidReport("report_not_plan_only"));
        }
        if !report.observation.complete {
            return Err(AdapterError::InvalidReport("observation_incomplete"));
        }
        self.inspect_actions(identity, &report.actions, true)
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
        self.append(Event::Inspected {
            identity,
            actions: actions.clone(),
            complete,
        })?;
        Ok(InspectionReceipt {
            identity,
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
                continue;
            }
            self.append(Event::Projected {
                identity,
                receipt_id: action.receipt_id,
            })?;
            receipt.projected += 1;
        }
        Ok(receipt)
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
                actions,
                complete,
            } => {
                self.phase = Phase::Inspected;
                self.identity = Some(*identity);
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
    use crate::runner_log_reclaim::{FileIdentity, ItemDecision};

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
}
