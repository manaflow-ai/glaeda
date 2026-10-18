//! Crash-safe JSONL persistence for the rolling-tip ledger.
//!
//! The store serializes a cloned ledger operation, syncs its newly-created
//! envelopes, and only then swaps the clone into memory. A restart therefore
//! either sees the prior complete prefix or the newly synced events; a killed
//! writer's unterminated tail is discarded before replay.

use std::{
    fs::{File, OpenOptions},
    io::{self, BufRead, BufReader, Seek, SeekFrom, Write},
    os::unix::fs::OpenOptionsExt,
    path::{Path, PathBuf},
};

use serde::{Deserialize, Serialize};

use crate::rolling_tip_ledger::{
    ActiveLease, ArtifactProvenance, BuildAdmission, BuildExecutionState, BuildKey, EventEnvelope,
    PromotionDecision, PromotionPointer, ReceiptOutcome, RollingTipLedger, RollingTipLedgerError,
    TipStream,
};

const MAX_EVENT_BYTES: usize = 2 << 20;

#[derive(Debug)]
pub enum RollingTipLedgerStoreError {
    Io(io::Error),
    Json(serde_json::Error),
    Ledger(RollingTipLedgerError),
    Closed,
    EventTooLarge,
    EmptyEvent,
}

impl std::fmt::Display for RollingTipLedgerStoreError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Io(error) => write!(formatter, "ledger store I/O: {error}"),
            Self::Json(error) => write!(formatter, "ledger event JSON: {error}"),
            Self::Ledger(error) => write!(formatter, "ledger replay: {error}"),
            Self::Closed => formatter.write_str("ledger store is closed"),
            Self::EventTooLarge => formatter.write_str("ledger event exceeds size limit"),
            Self::EmptyEvent => formatter.write_str("ledger journal contains an empty line"),
        }
    }
}

impl std::error::Error for RollingTipLedgerStoreError {}

impl From<io::Error> for RollingTipLedgerStoreError {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}

impl From<serde_json::Error> for RollingTipLedgerStoreError {
    fn from(error: serde_json::Error) -> Self {
        Self::Json(error)
    }
}

impl From<RollingTipLedgerError> for RollingTipLedgerStoreError {
    fn from(error: RollingTipLedgerError) -> Self {
        Self::Ledger(error)
    }
}

/// Replay-derived query for fleet intelligence and operator receipts.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RollingTipLedgerReceipt {
    pub stream: TipStream,
    pub event_count: u64,
    pub latest_sequence: u64,
    pub latest_tip: Option<BuildKey>,
    pub latest_artifact: Option<ArtifactProvenance>,
    pub promotion: Option<PromotionPointer>,
    #[serde(default)]
    pub active_leases: Vec<ActiveLease>,
}

/// One durable event stream for one rolling-tip branch.
#[derive(Debug)]
pub struct RollingTipLedgerStore {
    path: PathBuf,
    file: Option<File>,
    ledger: RollingTipLedger,
}

impl RollingTipLedgerStore {
    /// Open an existing journal and replay its complete event prefix, or create
    /// a new journal for `stream`. A final partial JSONL record is discarded.
    pub fn open(
        path: impl AsRef<Path>,
        stream: TipStream,
    ) -> Result<Self, RollingTipLedgerStoreError> {
        let path = path.as_ref().to_path_buf();
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let (events, complete_bytes, partial) = read_events(&path)?;
        let ledger = RollingTipLedger::replay(stream, events)?;
        let file = OpenOptions::new()
            .create(true)
            .append(true)
            .read(true)
            .mode(0o600)
            .open(&path)?;
        if partial {
            file.set_len(complete_bytes)?;
            file.sync_data()?;
        }
        Ok(Self {
            path,
            file: Some(file),
            ledger,
        })
    }

    #[must_use]
    pub fn path(&self) -> &Path {
        &self.path
    }

    #[must_use]
    pub fn snapshot(&self) -> RollingTipLedger {
        self.ledger.clone()
    }

    /// Flush and close the journal. Future writes fail closed.
    pub fn close(&mut self) -> Result<(), RollingTipLedgerStoreError> {
        let Some(file) = self.file.take() else {
            return Err(RollingTipLedgerStoreError::Closed);
        };
        file.sync_all()?;
        drop(file);
        Ok(())
    }

    pub fn admit(
        &mut self,
        build: BuildKey,
        occurred_at_millis: u64,
    ) -> Result<BuildAdmission, RollingTipLedgerStoreError> {
        self.transact(|ledger| ledger.admit(build, occurred_at_millis))
    }

    pub fn record_state(
        &mut self,
        build: BuildKey,
        state: BuildExecutionState,
        attempt: u32,
        occurred_at_millis: u64,
    ) -> Result<(), RollingTipLedgerStoreError> {
        self.transact(|ledger| ledger.record_state(build, state, attempt, occurred_at_millis))
    }

    pub fn record_lease(
        &mut self,
        build: BuildKey,
        lease_id: &str,
        host_id: &str,
        acquired: bool,
        occurred_at_millis: u64,
    ) -> Result<(), RollingTipLedgerStoreError> {
        self.transact(|ledger| {
            ledger.record_lease(build, lease_id, host_id, acquired, occurred_at_millis)
        })
    }

    #[allow(clippy::too_many_arguments)]
    pub fn acquire_lease(
        &mut self,
        build: BuildKey,
        attempt: u32,
        lease_id: &str,
        host_id: &str,
        fencing_token: u64,
        expires_at_millis: u64,
        occurred_at_millis: u64,
    ) -> Result<(), RollingTipLedgerStoreError> {
        self.transact(|ledger| {
            ledger.acquire_lease(
                build,
                attempt,
                lease_id,
                host_id,
                fencing_token,
                expires_at_millis,
                occurred_at_millis,
            )
        })
    }

    #[allow(clippy::too_many_arguments)]
    pub fn release_lease(
        &mut self,
        build: BuildKey,
        attempt: u32,
        lease_id: &str,
        host_id: &str,
        fencing_token: u64,
        expires_at_millis: u64,
        occurred_at_millis: u64,
    ) -> Result<(), RollingTipLedgerStoreError> {
        self.transact(|ledger| {
            ledger.release_lease(
                build,
                attempt,
                lease_id,
                host_id,
                fencing_token,
                expires_at_millis,
                occurred_at_millis,
            )
        })
    }

    #[allow(clippy::too_many_arguments)]
    pub fn reclaim_expired_lease(
        &mut self,
        build: BuildKey,
        attempt: u32,
        lease_id: &str,
        host_id: &str,
        fencing_token: u64,
        expires_at_millis: u64,
        reclaimed_by: &str,
        occurred_at_millis: u64,
    ) -> Result<(), RollingTipLedgerStoreError> {
        self.transact(|ledger| {
            ledger.reclaim_expired_lease(
                build,
                attempt,
                lease_id,
                host_id,
                fencing_token,
                expires_at_millis,
                reclaimed_by,
                occurred_at_millis,
            )
        })
    }

    pub fn publish_artifact(
        &mut self,
        artifact: ArtifactProvenance,
        occurred_at_millis: u64,
    ) -> Result<(), RollingTipLedgerStoreError> {
        self.transact(|ledger| ledger.publish_artifact(artifact, occurred_at_millis))
    }

    pub fn request_promotion(
        &mut self,
        build: BuildKey,
        expected_revision: u64,
        occurred_at_millis: u64,
    ) -> Result<(), RollingTipLedgerStoreError> {
        self.transact(|ledger| {
            ledger.request_promotion(build, expected_revision, occurred_at_millis)
        })
    }

    pub fn decide_promotion(
        &mut self,
        build: BuildKey,
        decision: PromotionDecision,
        expected_revision: u64,
        occurred_at_millis: u64,
    ) -> Result<u64, RollingTipLedgerStoreError> {
        self.transact(|ledger| {
            ledger.decide_promotion(build, decision, expected_revision, occurred_at_millis)
        })
    }

    pub fn record_backpressure(
        &mut self,
        build: BuildKey,
        reason: &str,
        occurred_at_millis: u64,
    ) -> Result<(), RollingTipLedgerStoreError> {
        self.transact(|ledger| ledger.record_backpressure(build, reason, occurred_at_millis))
    }

    pub fn close_receipt(
        &mut self,
        build: BuildKey,
        outcome: ReceiptOutcome,
        occurred_at_millis: u64,
    ) -> Result<(), RollingTipLedgerStoreError> {
        self.transact(|ledger| ledger.close_receipt(build, outcome, occurred_at_millis))
    }

    /// Return latest observed tip, latest published artifact, and promotion.
    #[must_use]
    pub fn receipt(&self) -> RollingTipLedgerReceipt {
        let events = self.ledger.events();
        let latest_tip = events
            .iter()
            .rev()
            .find_map(|envelope| match &envelope.event {
                crate::rolling_tip_ledger::RollingTipEvent::TipObserved { build } => {
                    Some(build.clone())
                }
                _ => None,
            });
        let latest_artifact = events
            .iter()
            .rev()
            .find_map(|envelope| match &envelope.event {
                crate::rolling_tip_ledger::RollingTipEvent::ArtifactPublished { artifact } => {
                    Some(artifact.clone())
                }
                _ => None,
            });
        RollingTipLedgerReceipt {
            stream: self.ledger.stream().clone(),
            event_count: events.len() as u64,
            latest_sequence: events.last().map_or(0, |event| event.sequence),
            latest_tip,
            latest_artifact,
            promotion: self.ledger.promotion().cloned(),
            active_leases: self.ledger.active_leases().cloned().collect(),
        }
    }

    fn transact<R, F>(&mut self, operation: F) -> Result<R, RollingTipLedgerStoreError>
    where
        F: FnOnce(&mut RollingTipLedger) -> Result<R, RollingTipLedgerError>,
    {
        if self.file.is_none() {
            return Err(RollingTipLedgerStoreError::Closed);
        }
        let mut candidate = self.ledger.clone();
        let before = candidate.events().len();
        let result = operation(&mut candidate)?;
        let new_events = candidate.events()[before..].to_vec();
        if !new_events.is_empty() {
            let file = self
                .file
                .as_mut()
                .ok_or(RollingTipLedgerStoreError::Closed)?;
            let offset = file.seek(SeekFrom::End(0))?;
            let write_result = (|| {
                for event in new_events {
                    let mut encoded = serde_json::to_vec(&event)?;
                    encoded.push(b'\n');
                    if encoded.len() > MAX_EVENT_BYTES {
                        return Err(RollingTipLedgerStoreError::EventTooLarge);
                    }
                    file.write_all(&encoded)?;
                }
                file.sync_data()?;
                Ok::<(), RollingTipLedgerStoreError>(())
            })();
            if let Err(error) = write_result {
                let _ = file.set_len(offset);
                let _ = file.seek(SeekFrom::End(0));
                return Err(error);
            }
        }
        self.ledger = candidate;
        Ok(result)
    }
}

fn read_events(path: &Path) -> Result<(Vec<EventEnvelope>, u64, bool), RollingTipLedgerStoreError> {
    let file = match File::open(path) {
        Ok(file) => file,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok((Vec::new(), 0, false)),
        Err(error) => return Err(error.into()),
    };
    let mut reader = BufReader::new(file);
    let mut complete_bytes = 0u64;
    let mut events = Vec::new();
    loop {
        let mut line = Vec::new();
        let bytes = reader.read_until(b'\n', &mut line)?;
        if bytes == 0 {
            return Ok((events, complete_bytes, false));
        }
        if line.len() > MAX_EVENT_BYTES {
            return Err(RollingTipLedgerStoreError::EventTooLarge);
        }
        if line.last() != Some(&b'\n') {
            return Ok((events, complete_bytes, true));
        }
        line.pop();
        while matches!(line.last(), Some(b'\r')) {
            line.pop();
        }
        if line.iter().all(u8::is_ascii_whitespace) {
            return Err(RollingTipLedgerStoreError::EmptyEvent);
        }
        events.push(serde_json::from_slice(&line)?);
        complete_bytes = complete_bytes.saturating_add(bytes as u64);
    }
}
