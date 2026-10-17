//! Append-only observation and promotion ledger for one rolling development-build stream.
//!
//! This is deliberately a path-free kernel. It records immutable build identities and artifact
//! provenance, deduplicates exact-key work (including work already in flight), and keeps execution
//! state independent from the operator's promotion decision. A scheduler or Glaeda transport can
//! persist and forward the envelopes; this module does not start, cancel, or mutate a build.

use std::{
    collections::{BTreeMap, BTreeSet},
    fmt,
};

use serde::{Deserialize, Serialize};

use crate::artifact::{CommitId, GitTreeId, RepositoryRef, Sha256Digest};

pub const ROLLING_TIP_LEDGER_SCHEMA_VERSION: u8 = 1;

/// A bounded repository/branch stream identity. Mutable branch names are scoped to a repository.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct TipStream {
    pub repository: RepositoryRef,
    pub branch: String,
}

impl TipStream {
    /// # Errors
    ///
    /// Returns an error when the branch is empty, too long, or contains whitespace/control data.
    pub fn new(repository: RepositoryRef, branch: &str) -> Result<Self, RollingTipLedgerError> {
        validate_token("branch", branch)?;
        Ok(Self {
            repository,
            branch: branch.to_owned(),
        })
    }
}

/// Exact inputs that make one build result reusable. No mutable alias is part of this key.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct BuildKey {
    pub stream: TipStream,
    pub commit: CommitId,
    pub tree: GitTreeId,
    pub workflow_revision: String,
    pub lockfile_digest: Option<Sha256Digest>,
    pub source_bundle_digest: Option<Sha256Digest>,
    pub toolchain: String,
    pub toolchain_image_digest: Option<Sha256Digest>,
    pub capability_digest: Option<Sha256Digest>,
    pub reset_digest: Option<Sha256Digest>,
    pub target: String,
    pub platform: String,
    pub architecture: String,
    pub configuration: String,
}

impl BuildKey {
    /// Construct an exact, content-addressable build request.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        stream: TipStream,
        commit: CommitId,
        tree: GitTreeId,
        workflow_revision: &str,
        lockfile_digest: Option<Sha256Digest>,
        source_bundle_digest: Option<Sha256Digest>,
        toolchain: &str,
        toolchain_image_digest: Option<Sha256Digest>,
        capability_digest: Option<Sha256Digest>,
        reset_digest: Option<Sha256Digest>,
        target: &str,
        platform: &str,
        architecture: &str,
        configuration: &str,
    ) -> Result<Self, RollingTipLedgerError> {
        for (field, value) in [
            ("workflow_revision", workflow_revision),
            ("toolchain", toolchain),
            ("target", target),
            ("platform", platform),
            ("architecture", architecture),
            ("configuration", configuration),
        ] {
            validate_token(field, value)?;
        }
        Ok(Self {
            stream,
            commit,
            tree,
            workflow_revision: workflow_revision.to_owned(),
            lockfile_digest,
            source_bundle_digest,
            toolchain: toolchain.to_owned(),
            toolchain_image_digest,
            capability_digest,
            reset_digest,
            target: target.to_owned(),
            platform: platform.to_owned(),
            architecture: architecture.to_owned(),
            configuration: configuration.to_owned(),
        })
    }
}

/// Immutable producer and content evidence for a published artifact.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct ArtifactProvenance {
    pub artifact_id: String,
    pub build: BuildKey,
    pub content_digest: Sha256Digest,
    pub producer: String,
    pub producer_run_id: u64,
    pub producer_attempt: u32,
    pub execution_attempt: u32,
}

impl ArtifactProvenance {
    /// # Errors
    ///
    /// Returns an error when the artifact/producer labels are unbounded or a provider/execution
    /// identifier is zero. Content and build identity remain caller-supplied immutable evidence.
    pub fn new(
        artifact_id: &str,
        build: BuildKey,
        content_digest: Sha256Digest,
        producer: &str,
        producer_run_id: u64,
        producer_attempt: u32,
        execution_attempt: u32,
    ) -> Result<Self, RollingTipLedgerError> {
        validate_token("artifact_id", artifact_id)?;
        validate_token("producer", producer)?;
        if producer_run_id == 0 || producer_attempt == 0 || execution_attempt == 0 {
            return Err(RollingTipLedgerError::InvalidIdentifier("producer run"));
        }
        Ok(Self {
            artifact_id: artifact_id.to_owned(),
            build,
            content_digest,
            producer: producer.to_owned(),
            producer_run_id,
            producer_attempt,
            execution_attempt,
        })
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BuildExecutionState {
    Observed,
    Queued,
    Leased,
    Running,
    Succeeded,
    Failed,
    Cancelled,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReuseDecision {
    Hit,
    Miss,
    InFlight,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PromotionDecision {
    Accepted,
    Delayed,
    Rejected,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReceiptOutcome {
    Succeeded,
    Failed,
    Superseded,
}

/// Typed, append-only facts consumed by Glaeda or a fleet coordinator.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum RollingTipEvent {
    TipObserved {
        build: BuildKey,
    },
    BuildStateChanged {
        build: BuildKey,
        state: BuildExecutionState,
        attempt: u32,
    },
    LeaseAcquired {
        build: BuildKey,
        lease_id: String,
        host_id: String,
    },
    LeaseReleased {
        build: BuildKey,
        lease_id: String,
        host_id: String,
    },
    ArtifactPublished {
        artifact: ArtifactProvenance,
    },
    ArtifactReuse {
        build: BuildKey,
        decision: ReuseDecision,
        artifact_digest: Option<Sha256Digest>,
    },
    PromotionRequested {
        build: BuildKey,
        expected_revision: u64,
    },
    PromotionDecided {
        build: BuildKey,
        decision: PromotionDecision,
        expected_revision: u64,
        pointer_revision: u64,
    },
    Backpressure {
        build: BuildKey,
        reason: String,
    },
    ReceiptClosed {
        build: BuildKey,
        outcome: ReceiptOutcome,
    },
}

/// Envelope identity and ordering are assigned by [`RollingTipLedger::append`].
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EventEnvelope {
    pub schema_version: u8,
    pub sequence: u64,
    pub event_id: String,
    pub stream: TipStream,
    pub occurred_at_millis: u64,
    pub event: RollingTipEvent,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PromotionPointer {
    pub stream: TipStream,
    pub revision: u64,
    pub build: BuildKey,
    pub decision: PromotionDecision,
}

// The artifact is deliberately returned inline so exact-key reuse carries its
// immutable provenance without a second lookup. Keep the public result shape;
// callers already treat this as a short-lived admission decision.
#[allow(clippy::large_enum_variant)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BuildAdmission {
    Queued,
    Reused(ArtifactProvenance),
    InFlight,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RollingTipLedgerError {
    InvalidIdentifier(&'static str),
    InvalidToken(&'static str),
    InvalidTimestamp,
    InvalidAttempt,
    InvalidAttemptTransition { expected: u32, actual: u32 },
    InvalidBuildState,
    StreamMismatch,
    UnknownBuild,
    ConflictingArtifact,
    StaleBuildAttempt { expected: u32, actual: u32 },
    StaleBuildState,
    PromotionNotRequested,
    PromotionArtifactUnavailable,
    ArtifactExecutionMismatch,
    StalePromotionRevision { expected: u64, actual: u64 },
}

impl fmt::Display for RollingTipLedgerError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidIdentifier(field) => write!(formatter, "invalid {field} identifier"),
            Self::InvalidToken(field) => write!(formatter, "invalid {field} token"),
            Self::InvalidTimestamp => formatter.write_str("timestamp must be non-zero"),
            Self::InvalidAttempt => formatter.write_str("build attempt must be non-zero"),
            Self::InvalidAttemptTransition { expected, actual } => {
                write!(
                    formatter,
                    "invalid build attempt transition: expected {expected}, actual {actual}"
                )
            }
            Self::InvalidBuildState => formatter.write_str("invalid build execution state"),
            Self::StreamMismatch => formatter.write_str("build belongs to another stream"),
            Self::UnknownBuild => formatter.write_str("build has not been admitted"),
            Self::ConflictingArtifact => {
                formatter.write_str("conflicting artifact for exact build key")
            }
            Self::StaleBuildAttempt { expected, actual } => {
                write!(
                    formatter,
                    "stale build attempt: expected at least {expected}, actual {actual}"
                )
            }
            Self::StaleBuildState => formatter.write_str("stale build state transition"),
            Self::PromotionNotRequested => formatter.write_str("promotion was not requested"),
            Self::PromotionArtifactUnavailable => {
                formatter.write_str("promotion requires a published successful artifact")
            }
            Self::ArtifactExecutionMismatch => {
                formatter.write_str("artifact is not bound to the current successful execution")
            }
            Self::StalePromotionRevision { expected, actual } => {
                write!(
                    formatter,
                    "stale promotion revision: expected {expected}, actual {actual}"
                )
            }
        }
    }
}

impl std::error::Error for RollingTipLedgerError {}

/// In-memory MVP ledger. Persistence and transport are intentionally outside this kernel.
#[derive(Debug, Clone)]
pub struct RollingTipLedger {
    stream: TipStream,
    next_sequence: u64,
    events: Vec<EventEnvelope>,
    artifacts: BTreeMap<BuildKey, ArtifactProvenance>,
    in_flight: BTreeSet<BuildKey>,
    states: BTreeMap<BuildKey, ExecutionRecord>,
    promotion_requests: BTreeSet<BuildKey>,
    promotion: Option<PromotionPointer>,
}

#[derive(Debug, Clone, Copy)]
struct ExecutionRecord {
    attempt: u32,
    state: BuildExecutionState,
}

impl RollingTipLedger {
    #[must_use]
    pub fn new(stream: TipStream) -> Self {
        Self {
            stream,
            next_sequence: 1,
            events: Vec::new(),
            artifacts: BTreeMap::new(),
            in_flight: BTreeSet::new(),
            states: BTreeMap::new(),
            promotion_requests: BTreeSet::new(),
            promotion: None,
        }
    }

    #[must_use]
    pub fn stream(&self) -> &TipStream {
        &self.stream
    }

    #[must_use]
    pub fn events(&self) -> &[EventEnvelope] {
        &self.events
    }

    #[must_use]
    pub fn promotion(&self) -> Option<&PromotionPointer> {
        self.promotion.as_ref()
    }

    /// Rebuild a ledger from its durable event envelopes.
    ///
    /// Replay accepts only the exact schema, stream, sequence, and event-id
    /// contract emitted by [`RollingTipLedger::append`]. Every transition is
    /// revalidated before the envelope is added to the derived indexes, so a
    /// corrupt or reordered journal cannot manufacture a successful artifact
    /// or promotion pointer.
    pub fn replay(
        stream: TipStream,
        events: impl IntoIterator<Item = EventEnvelope>,
    ) -> Result<Self, RollingTipLedgerError> {
        let mut ledger = Self::new(stream);
        for envelope in events {
            if envelope.schema_version != ROLLING_TIP_LEDGER_SCHEMA_VERSION
                || envelope.stream != ledger.stream
                || envelope.sequence != ledger.next_sequence
                || envelope.occurred_at_millis == 0
            {
                return Err(RollingTipLedgerError::InvalidIdentifier("event envelope"));
            }
            let expected_id = format!("{}-{:016x}", ledger.stream.branch, envelope.sequence);
            if envelope.event_id != expected_id {
                return Err(RollingTipLedgerError::InvalidIdentifier("event id"));
            }
            ledger.apply_replayed_event(&envelope)?;
            ledger.events.push(envelope);
            ledger.next_sequence = ledger.next_sequence.saturating_add(1);
        }
        Ok(ledger)
    }

    fn apply_replayed_event(
        &mut self,
        envelope: &EventEnvelope,
    ) -> Result<(), RollingTipLedgerError> {
        let timestamp = envelope.occurred_at_millis;
        match &envelope.event {
            RollingTipEvent::TipObserved { build } => {
                self.require_stream(build)?;
                self.require_timestamp(timestamp)
            }
            RollingTipEvent::BuildStateChanged {
                build,
                state,
                attempt,
            } => self.replay_state(build, *state, *attempt, timestamp),
            RollingTipEvent::LeaseAcquired {
                build,
                lease_id,
                host_id,
            }
            | RollingTipEvent::LeaseReleased {
                build,
                lease_id,
                host_id,
            } => {
                self.require_stream(build)?;
                self.require_timestamp(timestamp)?;
                validate_token("lease_id", lease_id)?;
                validate_token("host_id", host_id)
            }
            RollingTipEvent::ArtifactPublished { artifact } => {
                self.require_stream(&artifact.build)?;
                self.require_timestamp(timestamp)?;
                let Some(execution) = self.states.get(&artifact.build) else {
                    return Err(RollingTipLedgerError::UnknownBuild);
                };
                if execution.state != BuildExecutionState::Succeeded
                    || execution.attempt != artifact.execution_attempt
                {
                    return Err(RollingTipLedgerError::ArtifactExecutionMismatch);
                }
                if self.artifacts.contains_key(&artifact.build) {
                    return Err(RollingTipLedgerError::ConflictingArtifact);
                }
                self.artifacts
                    .insert(artifact.build.clone(), artifact.clone());
                self.in_flight.remove(&artifact.build);
                Ok(())
            }
            RollingTipEvent::ArtifactReuse {
                build,
                decision,
                artifact_digest,
            } => {
                self.require_stream(build)?;
                self.require_timestamp(timestamp)?;
                match decision {
                    ReuseDecision::Hit => {
                        let Some(artifact) = self.artifacts.get(build) else {
                            return Err(RollingTipLedgerError::ArtifactExecutionMismatch);
                        };
                        let Some(execution) = self.states.get(build) else {
                            return Err(RollingTipLedgerError::ArtifactExecutionMismatch);
                        };
                        if artifact_digest.as_ref() != Some(&artifact.content_digest)
                            || execution.state != BuildExecutionState::Succeeded
                            || artifact.execution_attempt != execution.attempt
                        {
                            return Err(RollingTipLedgerError::ArtifactExecutionMismatch);
                        }
                    }
                    ReuseDecision::InFlight => {
                        if artifact_digest.is_some() || !self.in_flight.contains(build) {
                            return Err(RollingTipLedgerError::StaleBuildState);
                        }
                    }
                    ReuseDecision::Miss => {
                        if artifact_digest.is_some()
                            || self.artifacts.contains_key(build)
                            || self.in_flight.contains(build)
                        {
                            return Err(RollingTipLedgerError::StaleBuildState);
                        }
                    }
                }
                Ok(())
            }
            RollingTipEvent::PromotionRequested {
                build,
                expected_revision: _,
            } => {
                self.require_stream(build)?;
                self.require_timestamp(timestamp)?;
                if !self.is_known(build) {
                    return Err(RollingTipLedgerError::UnknownBuild);
                }
                self.promotion_requests.insert(build.clone());
                Ok(())
            }
            RollingTipEvent::PromotionDecided {
                build,
                decision,
                expected_revision,
                pointer_revision,
            } => {
                self.require_stream(build)?;
                self.require_timestamp(timestamp)?;
                if !self.promotion_requests.contains(build) {
                    return Err(RollingTipLedgerError::PromotionNotRequested);
                }
                let actual = self
                    .promotion
                    .as_ref()
                    .map_or(0, |pointer| pointer.revision);
                if *expected_revision != actual || *pointer_revision != actual.saturating_add(1) {
                    return Err(RollingTipLedgerError::StalePromotionRevision {
                        expected: *expected_revision,
                        actual,
                    });
                }
                if *decision == PromotionDecision::Accepted {
                    let Some(artifact) = self.artifacts.get(build) else {
                        return Err(RollingTipLedgerError::PromotionArtifactUnavailable);
                    };
                    let Some(execution) = self.states.get(build) else {
                        return Err(RollingTipLedgerError::PromotionArtifactUnavailable);
                    };
                    if execution.state != BuildExecutionState::Succeeded
                        || artifact.execution_attempt != execution.attempt
                    {
                        return Err(RollingTipLedgerError::PromotionArtifactUnavailable);
                    }
                }
                self.promotion = Some(PromotionPointer {
                    stream: self.stream.clone(),
                    revision: *pointer_revision,
                    build: build.clone(),
                    decision: *decision,
                });
                Ok(())
            }
            RollingTipEvent::Backpressure { build, reason } => {
                self.require_stream(build)?;
                self.require_timestamp(timestamp)?;
                validate_token("backpressure reason", reason)
            }
            RollingTipEvent::ReceiptClosed { build, outcome: _ } => {
                self.require_stream(build)?;
                self.require_timestamp(timestamp)
            }
        }
    }

    fn replay_state(
        &mut self,
        build: &BuildKey,
        state: BuildExecutionState,
        attempt: u32,
        timestamp: u64,
    ) -> Result<(), RollingTipLedgerError> {
        self.require_stream(build)?;
        self.require_timestamp(timestamp)?;
        if attempt == 0 {
            return Err(RollingTipLedgerError::InvalidAttempt);
        }
        if state == BuildExecutionState::Observed {
            return Err(RollingTipLedgerError::InvalidBuildState);
        }
        let Some(current) = self.states.get(build).copied() else {
            if state != BuildExecutionState::Queued || attempt != 1 {
                return Err(RollingTipLedgerError::UnknownBuild);
            }
            self.states
                .insert(build.clone(), ExecutionRecord { attempt, state });
            self.in_flight.insert(build.clone());
            return Ok(());
        };
        if attempt < current.attempt {
            return Err(RollingTipLedgerError::StaleBuildAttempt {
                expected: current.attempt,
                actual: attempt,
            });
        }
        if attempt > current.attempt {
            let expected = current.attempt.saturating_add(1);
            if attempt != expected
                || !is_terminal(current.state)
                || state != BuildExecutionState::Queued
            {
                return Err(RollingTipLedgerError::InvalidAttemptTransition {
                    expected,
                    actual: attempt,
                });
            }
        } else if state_rank(state) < state_rank(current.state)
            || (is_terminal(state) && is_terminal(current.state) && state != current.state)
        {
            return Err(RollingTipLedgerError::StaleBuildState);
        }
        self.states
            .insert(build.clone(), ExecutionRecord { attempt, state });
        if attempt > current.attempt && state == BuildExecutionState::Queued {
            self.in_flight.insert(build.clone());
        }
        if is_terminal(state) {
            self.in_flight.remove(build);
        }
        Ok(())
    }

    /// Observe a tip and admit it once. Existing artifacts and in-flight work are never rebuilt.
    pub fn admit(
        &mut self,
        build: BuildKey,
        occurred_at_millis: u64,
    ) -> Result<BuildAdmission, RollingTipLedgerError> {
        self.require_stream(&build)?;
        self.require_timestamp(occurred_at_millis)?;
        let event_count = if self.artifacts.contains_key(&build) || self.in_flight.contains(&build)
        {
            2
        } else {
            3
        };
        self.ensure_capacity(event_count)?;
        self.append(
            occurred_at_millis,
            RollingTipEvent::TipObserved {
                build: build.clone(),
            },
        )?;
        if let Some(artifact) = self.artifacts.get(&build).cloned() {
            if self.states.get(&build).is_none_or(|execution| {
                execution.state != BuildExecutionState::Succeeded
                    || execution.attempt != artifact.execution_attempt
            }) {
                return Err(RollingTipLedgerError::ArtifactExecutionMismatch);
            }
            self.append(
                occurred_at_millis,
                RollingTipEvent::ArtifactReuse {
                    build,
                    decision: ReuseDecision::Hit,
                    artifact_digest: Some(artifact.content_digest.clone()),
                },
            )?;
            return Ok(BuildAdmission::Reused(artifact));
        }
        if self.in_flight.contains(&build) {
            self.append(
                occurred_at_millis,
                RollingTipEvent::ArtifactReuse {
                    build,
                    decision: ReuseDecision::InFlight,
                    artifact_digest: None,
                },
            )?;
            return Ok(BuildAdmission::InFlight);
        }
        self.append(
            occurred_at_millis,
            RollingTipEvent::ArtifactReuse {
                build: build.clone(),
                decision: ReuseDecision::Miss,
                artifact_digest: None,
            },
        )?;
        let attempt = self
            .states
            .get(&build)
            .map_or(1, |record| record.attempt.saturating_add(1));
        if attempt == 0 {
            return Err(RollingTipLedgerError::InvalidAttempt);
        }
        self.append(
            occurred_at_millis,
            RollingTipEvent::BuildStateChanged {
                build: build.clone(),
                state: BuildExecutionState::Queued,
                attempt,
            },
        )?;
        self.in_flight.insert(build.clone());
        self.states.insert(
            build,
            ExecutionRecord {
                attempt,
                state: BuildExecutionState::Queued,
            },
        );
        Ok(BuildAdmission::Queued)
    }

    /// Record execution state. Terminal state removes the dedupe lease; it never deletes artifacts.
    pub fn record_state(
        &mut self,
        build: BuildKey,
        state: BuildExecutionState,
        attempt: u32,
        occurred_at_millis: u64,
    ) -> Result<(), RollingTipLedgerError> {
        self.require_stream(&build)?;
        self.require_timestamp(occurred_at_millis)?;
        if attempt == 0 {
            return Err(RollingTipLedgerError::InvalidAttempt);
        }
        if state == BuildExecutionState::Observed {
            return Err(RollingTipLedgerError::InvalidBuildState);
        }
        let Some(current) = self.states.get(&build).copied() else {
            return Err(RollingTipLedgerError::UnknownBuild);
        };
        if attempt < current.attempt {
            return Err(RollingTipLedgerError::StaleBuildAttempt {
                expected: current.attempt,
                actual: attempt,
            });
        }
        if attempt > current.attempt {
            let expected = current.attempt.saturating_add(1);
            if attempt != expected
                || !is_terminal(current.state)
                || state != BuildExecutionState::Queued
            {
                return Err(RollingTipLedgerError::InvalidAttemptTransition {
                    expected,
                    actual: attempt,
                });
            }
        } else if state_rank(state) < state_rank(current.state)
            || (is_terminal(state) && is_terminal(current.state) && state != current.state)
        {
            return Err(RollingTipLedgerError::StaleBuildState);
        }
        self.append(
            occurred_at_millis,
            RollingTipEvent::BuildStateChanged {
                build: build.clone(),
                state,
                attempt,
            },
        )?;
        self.states
            .insert(build.clone(), ExecutionRecord { attempt, state });
        if attempt > current.attempt && state == BuildExecutionState::Queued {
            self.in_flight.insert(build.clone());
        }
        if matches!(
            state,
            BuildExecutionState::Succeeded
                | BuildExecutionState::Failed
                | BuildExecutionState::Cancelled
        ) {
            self.in_flight.remove(&build);
        }
        Ok(())
    }

    pub fn record_lease(
        &mut self,
        build: BuildKey,
        lease_id: &str,
        host_id: &str,
        acquired: bool,
        occurred_at_millis: u64,
    ) -> Result<(), RollingTipLedgerError> {
        self.require_stream(&build)?;
        self.require_timestamp(occurred_at_millis)?;
        validate_token("lease_id", lease_id)?;
        validate_token("host_id", host_id)?;
        let event = if acquired {
            RollingTipEvent::LeaseAcquired {
                build,
                lease_id: lease_id.to_owned(),
                host_id: host_id.to_owned(),
            }
        } else {
            RollingTipEvent::LeaseReleased {
                build,
                lease_id: lease_id.to_owned(),
                host_id: host_id.to_owned(),
            }
        };
        self.append(occurred_at_millis, event)
    }

    pub fn publish_artifact(
        &mut self,
        artifact: ArtifactProvenance,
        occurred_at_millis: u64,
    ) -> Result<(), RollingTipLedgerError> {
        self.require_stream(&artifact.build)?;
        self.require_timestamp(occurred_at_millis)?;
        let Some(execution) = self.states.get(&artifact.build) else {
            return Err(RollingTipLedgerError::UnknownBuild);
        };
        if execution.state != BuildExecutionState::Succeeded
            || execution.attempt != artifact.execution_attempt
        {
            return Err(RollingTipLedgerError::ArtifactExecutionMismatch);
        }
        if let Some(existing) = self.artifacts.get(&artifact.build) {
            if existing == &artifact {
                return Ok(());
            }
            return Err(RollingTipLedgerError::ConflictingArtifact);
        }
        self.ensure_capacity(1)?;
        self.append(
            occurred_at_millis,
            RollingTipEvent::ArtifactPublished {
                artifact: artifact.clone(),
            },
        )?;
        self.artifacts
            .insert(artifact.build.clone(), artifact.clone());
        self.in_flight.remove(&artifact.build);
        Ok(())
    }

    #[must_use]
    pub fn artifact_for(&self, build: &BuildKey) -> Option<&ArtifactProvenance> {
        self.artifacts.get(build)
    }

    pub fn request_promotion(
        &mut self,
        build: BuildKey,
        expected_revision: u64,
        occurred_at_millis: u64,
    ) -> Result<(), RollingTipLedgerError> {
        self.require_stream(&build)?;
        self.require_timestamp(occurred_at_millis)?;
        if !self.is_known(&build) {
            return Err(RollingTipLedgerError::UnknownBuild);
        }
        self.ensure_capacity(1)?;
        self.append(
            occurred_at_millis,
            RollingTipEvent::PromotionRequested {
                build: build.clone(),
                expected_revision,
            },
        )?;
        self.promotion_requests.insert(build);
        Ok(())
    }

    /// Compare-and-swap the candidate pointer. Execution state is untouched; accepted artifacts
    /// remain immutable and newer tips do not cancel work that is already running.
    pub fn decide_promotion(
        &mut self,
        build: BuildKey,
        decision: PromotionDecision,
        expected_revision: u64,
        occurred_at_millis: u64,
    ) -> Result<u64, RollingTipLedgerError> {
        self.require_stream(&build)?;
        self.require_timestamp(occurred_at_millis)?;
        if !self.promotion_requests.contains(&build) {
            return Err(RollingTipLedgerError::PromotionNotRequested);
        }
        let actual = self
            .promotion
            .as_ref()
            .map_or(0, |pointer| pointer.revision);
        if expected_revision != actual {
            return Err(RollingTipLedgerError::StalePromotionRevision {
                expected: expected_revision,
                actual,
            });
        }
        if decision == PromotionDecision::Accepted {
            let Some(artifact) = self.artifacts.get(&build) else {
                return Err(RollingTipLedgerError::PromotionArtifactUnavailable);
            };
            let Some(execution) = self.states.get(&build) else {
                return Err(RollingTipLedgerError::PromotionArtifactUnavailable);
            };
            if execution.state != BuildExecutionState::Succeeded
                || artifact.execution_attempt != execution.attempt
            {
                return Err(RollingTipLedgerError::PromotionArtifactUnavailable);
            }
        }
        let revision = actual.saturating_add(1);
        self.ensure_capacity(1)?;
        self.append(
            occurred_at_millis,
            RollingTipEvent::PromotionDecided {
                build: build.clone(),
                decision,
                expected_revision,
                pointer_revision: revision,
            },
        )?;
        self.promotion = Some(PromotionPointer {
            stream: self.stream.clone(),
            revision,
            build: build.clone(),
            decision,
        });
        Ok(revision)
    }

    pub fn record_backpressure(
        &mut self,
        build: BuildKey,
        reason: &str,
        occurred_at_millis: u64,
    ) -> Result<(), RollingTipLedgerError> {
        self.require_stream(&build)?;
        self.require_timestamp(occurred_at_millis)?;
        validate_token("backpressure reason", reason)?;
        self.append(
            occurred_at_millis,
            RollingTipEvent::Backpressure {
                build,
                reason: reason.to_owned(),
            },
        )
    }

    pub fn close_receipt(
        &mut self,
        build: BuildKey,
        outcome: ReceiptOutcome,
        occurred_at_millis: u64,
    ) -> Result<(), RollingTipLedgerError> {
        self.require_stream(&build)?;
        self.require_timestamp(occurred_at_millis)?;
        self.append(
            occurred_at_millis,
            RollingTipEvent::ReceiptClosed { build, outcome },
        )
    }

    fn require_stream(&self, build: &BuildKey) -> Result<(), RollingTipLedgerError> {
        if build.stream == self.stream {
            Ok(())
        } else {
            Err(RollingTipLedgerError::StreamMismatch)
        }
    }

    fn require_timestamp(&self, timestamp: u64) -> Result<(), RollingTipLedgerError> {
        if timestamp == 0 {
            Err(RollingTipLedgerError::InvalidTimestamp)
        } else {
            Ok(())
        }
    }

    fn is_known(&self, build: &BuildKey) -> bool {
        self.states.contains_key(build)
            || self.in_flight.contains(build)
            || self.artifacts.contains_key(build)
    }

    fn ensure_capacity(&self, count: u64) -> Result<(), RollingTipLedgerError> {
        self.next_sequence
            .checked_add(count)
            .ok_or(RollingTipLedgerError::InvalidIdentifier("event sequence"))
            .map(|_| ())
    }

    fn append(
        &mut self,
        occurred_at_millis: u64,
        event: RollingTipEvent,
    ) -> Result<(), RollingTipLedgerError> {
        self.ensure_capacity(1)?;
        let event_id = format!("{}-{:016x}", self.stream.branch, self.next_sequence);
        self.events.push(EventEnvelope {
            schema_version: ROLLING_TIP_LEDGER_SCHEMA_VERSION,
            sequence: self.next_sequence,
            event_id,
            stream: self.stream.clone(),
            occurred_at_millis,
            event,
        });
        self.next_sequence += 1;
        Ok(())
    }
}

const fn state_rank(state: BuildExecutionState) -> u8 {
    match state {
        BuildExecutionState::Observed => 0,
        BuildExecutionState::Queued => 1,
        BuildExecutionState::Leased => 2,
        BuildExecutionState::Running => 3,
        BuildExecutionState::Succeeded
        | BuildExecutionState::Failed
        | BuildExecutionState::Cancelled => 4,
    }
}

const fn is_terminal(state: BuildExecutionState) -> bool {
    matches!(
        state,
        BuildExecutionState::Succeeded
            | BuildExecutionState::Failed
            | BuildExecutionState::Cancelled
    )
}

fn validate_token(field: &'static str, value: &str) -> Result<(), RollingTipLedgerError> {
    if value.is_empty()
        || value.len() > 200
        || value
            .bytes()
            .any(|byte| byte.is_ascii_control() || byte.is_ascii_whitespace())
    {
        return Err(RollingTipLedgerError::InvalidToken(field));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn digest(byte: &str) -> Sha256Digest {
        Sha256Digest::parse(&format!("sha256:{}", byte.repeat(64))).expect("digest")
    }

    fn build(commit_byte: &str) -> BuildKey {
        let stream = TipStream::new(
            RepositoryRef::parse("manaflow-ai/cmux").expect("repo"),
            "feat-cmux-next",
        )
        .expect("stream");
        BuildKey::new(
            stream,
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
            digest("c"),
            "github-actions",
            42,
            1,
            1,
        )
        .expect("artifact")
    }

    #[test]
    fn exact_key_reuses_artifact_and_deduplicates_in_flight_work() {
        let stream = build("a").stream.clone();
        let mut ledger = RollingTipLedger::new(stream);
        let first = build("a");
        assert_eq!(
            ledger.admit(first.clone(), 1).expect("admit"),
            BuildAdmission::Queued
        );
        assert_eq!(
            ledger.admit(first.clone(), 2).expect("dedupe"),
            BuildAdmission::InFlight
        );
        ledger
            .record_state(first.clone(), BuildExecutionState::Succeeded, 1, 3)
            .expect("success");
        ledger
            .publish_artifact(artifact(first.clone()), 4)
            .expect("publish");
        assert!(matches!(
            ledger.admit(first, 5).expect("reuse"),
            BuildAdmission::Reused(_)
        ));
        assert!(ledger.events().iter().any(|event| matches!(
            event.event,
            RollingTipEvent::ArtifactReuse {
                decision: ReuseDecision::InFlight,
                ..
            }
        )));
        assert!(ledger.events().iter().any(|event| matches!(
            event.event,
            RollingTipEvent::ArtifactReuse {
                decision: ReuseDecision::Hit,
                ..
            }
        )));
    }

    #[test]
    fn promotion_pointer_is_compare_and_swap_and_does_not_cancel_execution() {
        let first = build("a");
        let stream = first.stream.clone();
        let mut ledger = RollingTipLedger::new(stream);
        ledger.admit(first.clone(), 1).expect("admit");
        ledger
            .record_state(first.clone(), BuildExecutionState::Succeeded, 1, 2)
            .expect("success");
        ledger
            .publish_artifact(artifact(first.clone()), 3)
            .expect("publish");
        ledger
            .request_promotion(first.clone(), 0, 2)
            .expect("request");
        assert_eq!(
            ledger
                .decide_promotion(first.clone(), PromotionDecision::Accepted, 0, 4)
                .expect("accept"),
            1
        );
        let newer = build("b");
        ledger.admit(newer.clone(), 5).expect("new tip");
        ledger
            .request_promotion(newer.clone(), 1, 6)
            .expect("new request");
        assert!(matches!(
            ledger.decide_promotion(newer, PromotionDecision::Delayed, 0, 7),
            Err(RollingTipLedgerError::StalePromotionRevision { .. })
        ));
        assert!(ledger.in_flight.contains(&build("b")));
        assert_eq!(
            ledger.promotion().expect("pointer").decision,
            PromotionDecision::Accepted
        );
    }

    #[test]
    fn event_envelope_is_typed_and_serializable() {
        let build = build("a");
        let mut ledger = RollingTipLedger::new(build.stream.clone());
        ledger
            .record_backpressure(build, "capacity", 1)
            .expect("event");
        let json = serde_json::to_string(&ledger.events()[0]).expect("json");
        assert!(json.contains("backpressure"));
        assert!(json.contains("feat-cmux-next"));
    }

    #[test]
    fn conflicting_artifact_preserves_first_provenance() {
        let first = build("a");
        let mut ledger = RollingTipLedger::new(first.stream.clone());
        ledger.admit(first.clone(), 1).expect("admit");
        ledger
            .record_state(first.clone(), BuildExecutionState::Succeeded, 1, 2)
            .expect("success");
        let original = artifact(first.clone());
        ledger
            .publish_artifact(original.clone(), 3)
            .expect("publish");
        let conflicting = ArtifactProvenance::new(
            "dev-build-2",
            first.clone(),
            digest("d"),
            "github-actions",
            43,
            1,
            1,
        )
        .expect("artifact");
        assert_eq!(
            ledger.publish_artifact(conflicting, 4),
            Err(RollingTipLedgerError::ConflictingArtifact)
        );
        assert_eq!(ledger.artifact_for(&first), Some(&original));
    }

    #[test]
    fn stale_attempt_and_state_cannot_clear_newer_in_flight_work() {
        let first = build("a");
        let mut ledger = RollingTipLedger::new(first.stream.clone());
        ledger.admit(first.clone(), 1).expect("attempt one");
        ledger
            .record_state(first.clone(), BuildExecutionState::Failed, 1, 2)
            .expect("failed attempt one");
        ledger.admit(first.clone(), 3).expect("attempt two");
        assert_eq!(
            ledger.record_state(first.clone(), BuildExecutionState::Failed, 1, 4),
            Err(RollingTipLedgerError::StaleBuildAttempt {
                expected: 2,
                actual: 1,
            })
        );
        assert_eq!(
            ledger.record_state(first.clone(), BuildExecutionState::Queued, 1, 5),
            Err(RollingTipLedgerError::StaleBuildAttempt {
                expected: 2,
                actual: 1,
            })
        );
        assert!(ledger.in_flight.contains(&first));
    }

    #[test]
    fn promotion_requires_request_and_successful_published_artifact() {
        let first = build("a");
        let mut ledger = RollingTipLedger::new(first.stream.clone());
        assert_eq!(
            ledger.decide_promotion(first.clone(), PromotionDecision::Accepted, 0, 1),
            Err(RollingTipLedgerError::PromotionNotRequested)
        );
        ledger.admit(first.clone(), 2).expect("admit");
        ledger
            .request_promotion(first.clone(), 0, 3)
            .expect("request");
        assert_eq!(
            ledger.decide_promotion(first.clone(), PromotionDecision::Accepted, 0, 4),
            Err(RollingTipLedgerError::PromotionArtifactUnavailable)
        );
        ledger
            .record_state(first.clone(), BuildExecutionState::Succeeded, 1, 5)
            .expect("success");
        ledger
            .publish_artifact(artifact(first.clone()), 6)
            .expect("publish");
        assert_eq!(
            ledger
                .decide_promotion(first, PromotionDecision::Accepted, 0, 7)
                .expect("promotion"),
            1
        );
    }

    #[test]
    fn delayed_and_rejected_decisions_can_precede_artifact_success() {
        let first = build("a");
        let mut ledger = RollingTipLedger::new(first.stream.clone());
        ledger.admit(first.clone(), 1).expect("admit");
        ledger
            .request_promotion(first.clone(), 0, 2)
            .expect("request");
        assert_eq!(
            ledger
                .decide_promotion(first.clone(), PromotionDecision::Delayed, 0, 3)
                .expect("delay"),
            1
        );
        ledger
            .request_promotion(first.clone(), 1, 4)
            .expect("request");
        assert_eq!(
            ledger
                .decide_promotion(first, PromotionDecision::Rejected, 1, 5)
                .expect("reject"),
            2
        );
    }

    #[test]
    fn attempt_leaps_and_invented_states_are_rejected() {
        let first = build("a");
        let mut ledger = RollingTipLedger::new(first.stream.clone());
        ledger.admit(first.clone(), 1).expect("admit");
        assert_eq!(
            ledger.record_state(first.clone(), BuildExecutionState::Running, 3, 2),
            Err(RollingTipLedgerError::InvalidAttemptTransition {
                expected: 2,
                actual: 3,
            })
        );
        assert_eq!(
            ledger.record_state(first.clone(), BuildExecutionState::Running, 2, 3),
            Err(RollingTipLedgerError::InvalidAttemptTransition {
                expected: 2,
                actual: 2,
            })
        );
        assert_eq!(
            ledger.record_state(first.clone(), BuildExecutionState::Observed, 1, 4),
            Err(RollingTipLedgerError::InvalidBuildState)
        );
        assert!(ledger.in_flight.contains(&first));
    }

    #[test]
    fn artifact_requires_current_successful_execution_attempt() {
        let first = build("a");
        let mut ledger = RollingTipLedger::new(first.stream.clone());
        let unknown = artifact(first.clone());
        assert_eq!(
            ledger.publish_artifact(unknown, 1),
            Err(RollingTipLedgerError::UnknownBuild)
        );
        ledger.admit(first.clone(), 2).expect("admit");
        assert_eq!(
            ledger.publish_artifact(artifact(first.clone()), 3),
            Err(RollingTipLedgerError::ArtifactExecutionMismatch)
        );
        ledger
            .record_state(first.clone(), BuildExecutionState::Failed, 1, 4)
            .expect("failed");
        ledger.admit(first.clone(), 5).expect("retry");
        ledger
            .record_state(first.clone(), BuildExecutionState::Succeeded, 2, 6)
            .expect("success");
        assert_eq!(
            ledger.publish_artifact(artifact(first.clone()), 7),
            Err(RollingTipLedgerError::ArtifactExecutionMismatch)
        );
        let attempt_two = ArtifactProvenance::new(
            "dev-build-attempt-two",
            first.clone(),
            digest("e"),
            "github-actions",
            44,
            1,
            2,
        )
        .expect("artifact");
        ledger
            .publish_artifact(attempt_two, 8)
            .expect("publish matching artifact");
        assert!(ledger.artifact_for(&first).is_some());
    }

    #[test]
    fn terminal_to_next_attempt_queued_restores_in_flight_deduplication() {
        let first = build("a");
        let mut ledger = RollingTipLedger::new(first.stream.clone());
        ledger.admit(first.clone(), 1).expect("attempt one");
        ledger
            .record_state(first.clone(), BuildExecutionState::Failed, 1, 2)
            .expect("failed");
        ledger
            .record_state(first.clone(), BuildExecutionState::Queued, 2, 3)
            .expect("retry queued");
        assert_eq!(
            ledger.admit(first, 4).expect("dedupe retry"),
            BuildAdmission::InFlight
        );
    }

    #[test]
    fn accepted_promotion_rejects_stale_artifact_attempt() {
        let first = build("a");
        let mut ledger = RollingTipLedger::new(first.stream.clone());
        ledger.admit(first.clone(), 1).expect("admit");
        ledger
            .record_state(first.clone(), BuildExecutionState::Succeeded, 1, 2)
            .expect("success");
        ledger
            .publish_artifact(artifact(first.clone()), 3)
            .expect("publish");
        // A recovered ledger can observe a newer successful execution while retaining the older
        // immutable artifact. Accepted promotion must refuse that stale provenance.
        ledger.states.insert(
            first.clone(),
            ExecutionRecord {
                attempt: 2,
                state: BuildExecutionState::Succeeded,
            },
        );
        ledger
            .request_promotion(first.clone(), 0, 4)
            .expect("request");
        assert_eq!(
            ledger.decide_promotion(first, PromotionDecision::Accepted, 0, 5),
            Err(RollingTipLedgerError::PromotionArtifactUnavailable)
        );
    }
}
