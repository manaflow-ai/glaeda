//! Safe, bounded macOS observation and reclaim of expired Actions runner diagnostics.
//!
//! The implementation deliberately uses fixed Apple tools (`ps` and `lsof`) through argv-only
//! subprocesses.  They are the stable, supported macOS process/file-descriptor interfaces and
//! keep this crate free of project-local FFI.  Process evidence is scoped to the enrolled runner
//! account; an unreadable in-scope enumeration is a veto, never evidence of absence.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File};
use std::io;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};

#[cfg(target_os = "macos")]
#[repr(C)]
#[derive(Default)]
struct VNodeFDInfo {
    pfi: libproc::libproc::net_info::ProcFileInfo,
    pvi: libproc::libproc::net_info::VInfoStat,
    vi_type: i32,
    vi_pad: i32,
    vi_fsid: [i32; 2],
}

#[cfg(target_os = "macos")]
impl libproc::libproc::file_info::PIDFDInfo for VNodeFDInfo {
    fn flavor() -> libproc::libproc::file_info::PIDFDInfoFlavor {
        libproc::libproc::file_info::PIDFDInfoFlavor::VNodeInfo
    }
}

pub const RUNNER_LOG_RECLAIM_SCHEMA_VERSION: u8 = 1;
pub const MAX_RUNNER_LOG_RECLAIM_BATCH: u32 = 64;
pub const DEFAULT_RUNNER_LOG_RECLAIM_BATCH: u32 = 16;
pub const MIN_RUNNER_LOG_RECLAIM_IDLE_SECONDS: i64 = 3_600;
const MAX_DIAG_ENTRIES: usize = 2_048;
const MAX_REPORT_ITEMS: usize = 128;
const MAX_JOURNAL_BYTES: u64 = 1 << 20;
const MARKER: &str = ".glaeda-cmux-runner";

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RunnerEnrollmentReceipt {
    #[serde(rename = "runnerDirOwned")]
    pub runner_dir_owned: bool,
    #[serde(rename = "runnerDir")]
    pub runner_dir: String,
    #[serde(rename = "installId")]
    pub install_id: String,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, PartialOrd, Ord)]
pub struct FileIdentity {
    pub device: u64,
    pub inode: u64,
    pub links: u64,
    pub size: u64,
    pub modified_seconds: i64,
    pub changed_seconds: i64,
    pub modified_nanos: i64,
    pub changed_nanos: i64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
struct OpenIdentity {
    device: u64,
    inode: u64,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
pub enum ItemDecision {
    #[serde(rename = "expired_runner_diagnostic_log")]
    ExpiredRunnerDiagnosticLog,
    #[serde(rename = "recent")]
    Recent,
    #[serde(rename = "open_or_busy")]
    OpenOrBusy,
    #[serde(rename = "preserved_scope")]
    PreservedScope,
    #[serde(rename = "unknown")]
    Unknown,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct LogItem {
    pub relative_path: String,
    pub identity: FileIdentity,
    pub bytes: u64,
    pub modified_seconds: i64,
    pub decision: ItemDecision,
    pub reason: &'static str,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct ProcessEvidence {
    pub uid: u32,
    pub in_scope_pids_seen: u64,
    pub in_scope_pids_complete: bool,
    pub unreadable_in_scope_pids: u64,
    pub busy_processes_observed: u64,
    pub open_identity_count: u64,
    #[serde(skip_serializing)]
    pub(crate) open_identities: BTreeSet<OpenIdentity>,
}

impl ProcessEvidence {
    pub(crate) fn is_open(&self, identity: FileIdentity) -> bool {
        self.open_identities.contains(&OpenIdentity {
            device: identity.device,
            inode: identity.inode,
        })
    }

    #[cfg(test)]
    pub(crate) fn test_complete() -> Self {
        Self {
            uid: 501,
            in_scope_pids_seen: 1,
            in_scope_pids_complete: true,
            unreadable_in_scope_pids: 0,
            busy_processes_observed: 0,
            open_identity_count: 0,
            open_identities: BTreeSet::new(),
        }
    }

    #[cfg(test)]
    pub(crate) fn test_busy() -> Self {
        let mut value = Self::test_complete();
        value.busy_processes_observed = 1;
        value
    }

    #[cfg(test)]
    pub(crate) fn test_incomplete() -> Self {
        let mut value = Self::test_complete();
        value.in_scope_pids_complete = false;
        value.unreadable_in_scope_pids = 1;
        value
    }

    #[cfg(test)]
    pub(crate) fn test_open(identity: FileIdentity) -> Self {
        let mut value = Self::test_complete();
        value.open_identities.insert(OpenIdentity {
            device: identity.device,
            inode: identity.inode,
        });
        value.open_identity_count = 1;
        value
    }
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct AuthorityEvidence {
    pub kind: &'static str,
    pub uid: u32,
    pub scope_complete: bool,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct Observation {
    pub runner_root: String,
    pub authority: AuthorityEvidence,
    pub process: ProcessEvidence,
    pub items: Vec<LogItem>,
    #[serde(skip_serializing)]
    pub(crate) catalog: Vec<LogItem>,
    pub item_count: u64,
    pub eligible_count: u64,
    pub enumeration_truncated: bool,
    pub items_truncated: bool,
    pub complete: bool,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct PlannedAction {
    pub relative_path: String,
    pub identity: FileIdentity,
    pub decision: ItemDecision,
    pub receipt_id: String,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct RecoveryEvidence {
    pub state_root: Option<String>,
    pub pending_count: u64,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct Report {
    pub document_type: &'static str,
    pub schema_version: u8,
    pub operation: &'static str,
    pub runner_root: String,
    pub minimum_idle_seconds: i64,
    pub batch_size: u32,
    pub observation: Observation,
    pub actions: Vec<PlannedAction>,
    pub remaining_count: u64,
    pub reclaimed_count: u64,
    pub recovery: RecoveryEvidence,
    pub errors: Vec<&'static str>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Policy {
    pub minimum_idle_seconds: i64,
    pub batch_size: u32,
}

impl Policy {
    pub fn new(minimum_idle_seconds: i64, batch_size: u32) -> Result<Self, ReclaimError> {
        if minimum_idle_seconds < MIN_RUNNER_LOG_RECLAIM_IDLE_SECONDS {
            return Err(ReclaimError::InvalidPolicy("idle_window_too_small"));
        }
        if batch_size == 0 || batch_size > MAX_RUNNER_LOG_RECLAIM_BATCH {
            return Err(ReclaimError::InvalidPolicy("batch_size_out_of_range"));
        }
        Ok(Self {
            minimum_idle_seconds,
            batch_size,
        })
    }
}

#[derive(Debug)]
pub enum ReclaimError {
    InvalidPolicy(&'static str),
    InvalidAuthority(&'static str),
    InvalidPath(&'static str),
    Observation(&'static str),
    Io(&'static str),
}

impl std::fmt::Display for ReclaimError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::InvalidPolicy(code)
            | Self::InvalidAuthority(code)
            | Self::InvalidPath(code)
            | Self::Observation(code)
            | Self::Io(code) => f.write_str(code),
        }
    }
}

impl std::error::Error for ReclaimError {}

fn identity(metadata: &fs::Metadata) -> FileIdentity {
    FileIdentity {
        device: metadata.dev(),
        inode: metadata.ino(),
        links: metadata.nlink(),
        size: metadata.len(),
        modified_seconds: metadata.mtime(),
        changed_seconds: metadata.ctime(),
        modified_nanos: metadata.mtime_nsec(),
        changed_nanos: metadata.ctime_nsec(),
    }
}

#[cfg(target_os = "macos")]
pub(crate) fn stat_matches_materialization(
    stat: &rustix::fs::Stat,
    expected: FileIdentity,
) -> bool {
    stat.st_dev == expected.device
        && stat.st_ino == expected.inode
        && stat.st_nlink == expected.links
        && stat.st_size.max(0) as u64 == expected.size
        && stat.st_mtime == expected.modified_seconds
        && stat.st_mtime_nsec == expected.modified_nanos
}

fn now_seconds() -> Result<i64, ReclaimError> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .map_err(|_| ReclaimError::Observation("clock_before_epoch"))
}

fn relative_path(root: &Path, path: &Path) -> String {
    path.strip_prefix(root)
        .unwrap_or(path)
        .to_string_lossy()
        .into_owned()
}

fn preserved_path(path: &Path) -> bool {
    path.components().any(|component| {
        let name = component.as_os_str().to_string_lossy().to_ascii_lowercase();
        ["warm", "shared", "source", "history"]
            .iter()
            .any(|keep| name == *keep)
    })
}

fn reject_symlink_ancestors(path: &Path) -> Result<(), ReclaimError> {
    let mut current = PathBuf::new();
    for component in path.components() {
        current.push(component.as_os_str());
        if let Ok(metadata) = fs::symlink_metadata(&current) {
            if metadata.file_type().is_symlink() {
                return Err(ReclaimError::InvalidPath("symlink_ancestor"));
            }
        }
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn observe_processes(root: &Path, uid: u32) -> Result<ProcessEvidence, ReclaimError> {
    use libproc::libproc::bsd_info::BSDInfo;
    use libproc::libproc::file_info::{ListFDs, ProcFDType, pidfdinfo};
    use libproc::libproc::proc_pid::{listpidinfo, pidinfo};

    let pids = libproc::processes::pids_by_type(libproc::processes::ProcFilter::ByUID { uid })
        .map_err(|_| ReclaimError::Observation("process_table_unavailable"))?;
    const MAX_SCANNED_PIDS: usize = 512;
    const MAX_FDS_PER_PID: usize = 4_096;
    let mut busy = 0_u64;
    let mut unreadable = 0_u64;
    let root_stat = fs::symlink_metadata(root)
        .map_err(|_| ReclaimError::Observation("runner_root_unreadable"))?;
    let mut open_identities = BTreeSet::new();
    if pids.len() > MAX_SCANNED_PIDS {
        unreadable = unreadable.saturating_add(1);
    }
    for pid in pids.iter().take(MAX_SCANNED_PIDS) {
        let before = match pidinfo::<BSDInfo>(*pid as i32, 0) {
            Ok(info) if info.pbi_uid == uid => (info.pbi_start_tvsec, info.pbi_start_tvusec),
            Ok(_) | Err(_) => {
                unreadable = unreadable.saturating_add(1);
                continue;
            }
        };
        let worker_pid = match libproc::libproc::proc_pid::pidpath(*pid as i32) {
            Ok(path) if Path::new(&path) == root.join("bin/Runner.Worker") => true,
            Ok(_) => false,
            Err(_) => {
                unreadable = unreadable.saturating_add(1);
                false
            }
        };
        let fds = match listpidinfo::<ListFDs>(*pid as i32, MAX_FDS_PER_PID) {
            Ok(fds) => fds,
            Err(_) => {
                unreadable = unreadable.saturating_add(1);
                continue;
            }
        };
        if fds.len() == MAX_FDS_PER_PID {
            unreadable = unreadable.saturating_add(1);
        }
        for fd in fds {
            if !matches!(ProcFDType::from(fd.proc_fdtype), ProcFDType::VNode) {
                continue;
            }
            let vnode = match pidfdinfo::<VNodeFDInfo>(*pid as i32, fd.proc_fd) {
                Ok(vnode) => vnode,
                Err(_) => {
                    unreadable = unreadable.saturating_add(1);
                    continue;
                }
            };
            let identity = OpenIdentity {
                device: vnode.pvi.vst_dev as u64,
                inode: vnode.pvi.vst_ino,
            };
            open_identities.insert(identity);
            if worker_pid && identity.device == root_stat.dev() && identity.inode == root_stat.ino()
            {
                busy = busy.saturating_add(1);
            }
        }
        match pidinfo::<BSDInfo>(*pid as i32, 0) {
            Ok(info)
                if info.pbi_uid == uid
                    && (info.pbi_start_tvsec, info.pbi_start_tvusec) == before => {}
            Ok(_) | Err(_) => unreadable = unreadable.saturating_add(1),
        }
    }
    Ok(ProcessEvidence {
        uid,
        in_scope_pids_seen: pids.len() as u64,
        in_scope_pids_complete: unreadable == 0,
        unreadable_in_scope_pids: unreadable,
        busy_processes_observed: busy,
        open_identity_count: open_identities.len() as u64,
        open_identities,
    })
}

#[cfg(not(target_os = "macos"))]
fn observe_processes(_root: &Path, _uid: u32) -> Result<ProcessEvidence, ReclaimError> {
    Err(ReclaimError::Observation("macos_only"))
}

fn validate_authority(
    root: &Path,
    receipt: &RunnerEnrollmentReceipt,
) -> Result<(PathBuf, u32), ReclaimError> {
    if !receipt.runner_dir_owned
        || receipt.runner_dir != root.to_string_lossy()
        || receipt.install_id.is_empty()
    {
        return Err(ReclaimError::InvalidAuthority("runner_enrollment_mismatch"));
    }
    let metadata = fs::symlink_metadata(root)
        .map_err(|_| ReclaimError::InvalidPath("runner_root_unreadable"))?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return Err(ReclaimError::InvalidPath("runner_root_not_directory"));
    }
    let marker = root.join(MARKER);
    let marker_metadata = fs::symlink_metadata(&marker)
        .map_err(|_| ReclaimError::InvalidAuthority("runner_marker_missing"))?;
    if !marker_metadata.is_file() || marker_metadata.file_type().is_symlink() {
        return Err(ReclaimError::InvalidAuthority("runner_marker_invalid"));
    }
    let marker_value = fs::read_to_string(&marker)
        .map_err(|_| ReclaimError::InvalidAuthority("runner_marker_unreadable"))?;
    if marker_value.trim() != receipt.install_id {
        return Err(ReclaimError::InvalidAuthority("runner_marker_mismatch"));
    }
    Ok((root.to_path_buf(), metadata.uid()))
}

fn enumerate_diag(
    root: &Path,
    process: &ProcessEvidence,
    policy: Policy,
    now: i64,
) -> Result<Vec<LogItem>, ReclaimError> {
    let diag = root.join("_diag");
    let metadata = match fs::symlink_metadata(&diag) {
        Ok(value) => value,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(_) => return Err(ReclaimError::Observation("diagnostics_unreadable")),
    };
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return Err(ReclaimError::Observation("diagnostics_unsafe_shape"));
    }
    let mut stack = vec![diag];
    let mut items = Vec::new();
    while let Some(dir) = stack.pop() {
        let entries =
            fs::read_dir(&dir).map_err(|_| ReclaimError::Observation("diagnostics_unreadable"))?;
        for entry in entries.take(MAX_DIAG_ENTRIES.saturating_sub(items.len())) {
            let entry = entry.map_err(|_| ReclaimError::Observation("diagnostics_unreadable"))?;
            let path = entry.path();
            let metadata = fs::symlink_metadata(&path)
                .map_err(|_| ReclaimError::Observation("diagnostics_unreadable"))?;
            if metadata.file_type().is_symlink() {
                continue;
            }
            if metadata.is_dir() {
                stack.push(path);
                continue;
            }
            if !metadata.is_file() || path.extension().and_then(|v| v.to_str()) != Some("log") {
                continue;
            }
            let modified = metadata
                .modified()
                .ok()
                .and_then(|v| v.duration_since(UNIX_EPOCH).ok())
                .map(|v| v.as_secs() as i64)
                .ok_or(ReclaimError::Observation(
                    "diagnostics_timestamp_unavailable",
                ))?;
            let age = now
                .checked_sub(modified)
                .ok_or(ReclaimError::Observation("diagnostics_timestamp_overflow"))?;
            let id = identity(&metadata);
            let (decision, reason) = if preserved_path(path.strip_prefix(root).unwrap_or(&path)) {
                (ItemDecision::PreservedScope, "protected_scope")
            } else if process.open_identities.contains(&OpenIdentity {
                device: id.device,
                inode: id.inode,
            }) {
                (ItemDecision::OpenOrBusy, "open_descriptor")
            } else if age < 0 || age < policy.minimum_idle_seconds {
                (ItemDecision::Recent, "below_idle_window")
            } else if metadata.uid() != process.uid {
                (ItemDecision::Unknown, "foreign_owner")
            } else if !process.in_scope_pids_complete {
                (
                    ItemDecision::Unknown,
                    "in_scope_process_observation_incomplete",
                )
            } else {
                (
                    ItemDecision::ExpiredRunnerDiagnosticLog,
                    "expired_retention",
                )
            };
            items.push(LogItem {
                relative_path: relative_path(root, &path),
                identity: id,
                bytes: metadata.len(),
                modified_seconds: modified,
                decision,
                reason,
            });
        }
        if items.len() >= MAX_DIAG_ENTRIES {
            break;
        }
    }
    Ok(items)
}

pub fn observe(
    root: &Path,
    receipt: &RunnerEnrollmentReceipt,
    policy: Policy,
) -> Result<Observation, ReclaimError> {
    if !root.is_absolute() {
        return Err(ReclaimError::InvalidPath("runner_root_must_be_absolute"));
    }
    let (root, uid) = validate_authority(root, receipt)?;
    let mut process = observe_processes(&root, uid)?;
    let mut all_items = enumerate_diag(&root, &process, policy, now_seconds()?)?;
    let complete = process.in_scope_pids_complete;
    let item_count = all_items.len() as u64;
    let enumeration_truncated = all_items.len() >= MAX_DIAG_ENTRIES;
    let eligible_count = all_items
        .iter()
        .filter(|item| item.decision == ItemDecision::ExpiredRunnerDiagnosticLog)
        .count() as u64;
    let items_truncated = all_items.len() > MAX_REPORT_ITEMS;
    let items = all_items.iter().take(MAX_REPORT_ITEMS).cloned().collect();
    Ok(Observation {
        runner_root: root.to_string_lossy().into_owned(),
        authority: AuthorityEvidence {
            kind: "enrolled_runner_uid",
            uid,
            scope_complete: complete,
        },
        process,
        items,
        catalog: all_items,
        item_count,
        eligible_count,
        enumeration_truncated,
        items_truncated,
        complete,
    })
}

fn receipt_id(item: &LogItem) -> String {
    format!("{:016x}-{:016x}", item.identity.device, item.identity.inode)
}

pub fn reclaim(
    root: &Path,
    receipt: &RunnerEnrollmentReceipt,
    policy: Policy,
    _requested_state_root: Option<&Path>,
    apply: bool,
) -> Result<Report, ReclaimError> {
    let observation = observe(root, receipt, policy)?;
    let actions = observation
        .catalog
        .iter()
        .filter(|item| item.decision == ItemDecision::ExpiredRunnerDiagnosticLog)
        .take(policy.batch_size as usize)
        .map(|item| PlannedAction {
            relative_path: item.relative_path.clone(),
            identity: item.identity,
            decision: item.decision,
            receipt_id: receipt_id(item),
        })
        .collect::<Vec<_>>();
    let remaining_count = observation
        .eligible_count
        .saturating_sub(actions.len() as u64);
    Ok(Report {
        document_type: "glaeda-runner-log-reclaim",
        schema_version: RUNNER_LOG_RECLAIM_SCHEMA_VERSION,
        operation: if apply { "apply" } else { "plan" },
        runner_root: observation.runner_root.clone(),
        minimum_idle_seconds: policy.minimum_idle_seconds,
        batch_size: policy.batch_size,
        observation,
        actions,
        remaining_count,
        reclaimed_count: 0,
        recovery: RecoveryEvidence {
            state_root: None,
            pending_count: 0,
        },
        errors: if apply {
            vec!["reclaim_not_ready"]
        } else {
            Vec::new()
        },
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    fn fixture() -> (PathBuf, RunnerEnrollmentReceipt) {
        let temp = std::env::temp_dir().join(format!(
            "glaeda-runner-log-reclaim-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .expect("clock")
                .as_nanos()
        ));
        fs::create_dir_all(&temp).expect("temp root");
        let root = temp.join("runner");
        fs::create_dir(&root).expect("runner");
        fs::create_dir(root.join("_diag")).expect("diag");
        fs::write(root.join(MARKER), "install-1\n").expect("marker");
        fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).expect("mode");
        (
            temp,
            RunnerEnrollmentReceipt {
                runner_dir_owned: true,
                runner_dir: root.to_string_lossy().into_owned(),
                install_id: "install-1".into(),
            },
        )
    }

    #[test]
    fn policy_caps_batch() {
        assert!(
            Policy::new(
                MIN_RUNNER_LOG_RECLAIM_IDLE_SECONDS,
                MAX_RUNNER_LOG_RECLAIM_BATCH + 1
            )
            .is_err()
        );
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn vnode_fd_info_layout_matches_public_libproc_components() {
        use libproc::libproc::net_info::{ProcFileInfo, VInfoStat};
        assert_eq!(std::mem::offset_of!(VNodeFDInfo, pfi), 0);
        assert_eq!(
            std::mem::offset_of!(VNodeFDInfo, pvi),
            std::mem::size_of::<ProcFileInfo>()
        );
        assert_eq!(
            std::mem::offset_of!(VNodeFDInfo, vi_type),
            std::mem::size_of::<ProcFileInfo>() + std::mem::size_of::<VInfoStat>()
        );
        assert_eq!(
            std::mem::size_of::<VNodeFDInfo>(),
            std::mem::size_of::<ProcFileInfo>() + std::mem::size_of::<VInfoStat>() + 16
        );
    }

    #[test]
    fn v1_fixture_matches_serde_wire_shape() {
        let fixture: serde_json::Value = serde_json::from_slice(include_bytes!(
            "../tests/fixtures/runner-log-reclaim/v1-plan.json"
        ))
        .expect("fixture JSON");
        let identity = FileIdentity {
            device: 1,
            inode: 2,
            links: 1,
            size: 10,
            modified_seconds: 1_700_000_000,
            changed_seconds: 1_700_000_000,
            modified_nanos: 0,
            changed_nanos: 0,
        };
        let item = LogItem {
            relative_path: "_diag/old.log".into(),
            identity,
            bytes: 10,
            modified_seconds: 1_700_000_000,
            decision: ItemDecision::ExpiredRunnerDiagnosticLog,
            reason: "expired_retention",
        };
        let report = Report {
            document_type: "glaeda-runner-log-reclaim",
            schema_version: 1,
            operation: "plan",
            runner_root: "/fixture/runner".into(),
            minimum_idle_seconds: 3_600,
            batch_size: 4,
            observation: Observation {
                runner_root: "/fixture/runner".into(),
                authority: AuthorityEvidence {
                    kind: "enrolled_runner_uid",
                    uid: 501,
                    scope_complete: true,
                },
                process: ProcessEvidence {
                    uid: 501,
                    in_scope_pids_seen: 1,
                    in_scope_pids_complete: true,
                    unreadable_in_scope_pids: 0,
                    busy_processes_observed: 0,
                    open_identity_count: 0,
                    open_identities: BTreeSet::new(),
                },
                items: vec![item.clone()],
                catalog: vec![item.clone()],
                item_count: 1,
                eligible_count: 1,
                enumeration_truncated: false,
                items_truncated: false,
                complete: true,
            },
            actions: vec![PlannedAction {
                relative_path: item.relative_path,
                identity,
                decision: item.decision,
                receipt_id: "0000000000000001-0000000000000002".into(),
            }],
            remaining_count: 0,
            reclaimed_count: 0,
            recovery: RecoveryEvidence {
                state_root: None,
                pending_count: 0,
            },
            errors: Vec::new(),
        };
        assert_eq!(fixture, serde_json::to_value(report).expect("wire report"));
    }

    #[test]
    fn authority_requires_marker() {
        let (temp, mut receipt) = fixture();
        receipt.install_id = "wrong".into();
        let error = observe(
            &temp.join("runner"),
            &receipt,
            Policy::new(MIN_RUNNER_LOG_RECLAIM_IDLE_SECONDS, 1).unwrap(),
        )
        .unwrap_err();
        assert!(matches!(
            error,
            ReclaimError::InvalidAuthority("runner_marker_mismatch")
        ));
        let _ = fs::remove_dir_all(temp);
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn preserves_non_diag_and_protected_names() {
        let (temp, receipt) = fixture();
        fs::create_dir(temp.join("runner/_diag/warm")).expect("warm");
        fs::write(temp.join("runner/_diag/warm/old.log"), b"warm").expect("warm log");
        fs::write(temp.join("runner/_diag/old.log"), b"old").expect("old log");
        let report = reclaim(
            &temp.join("runner"),
            &receipt,
            Policy::new(MIN_RUNNER_LOG_RECLAIM_IDLE_SECONDS, 4).unwrap(),
            None,
            false,
        )
        .unwrap();
        assert!(
            report
                .observation
                .items
                .iter()
                .any(|item| item.relative_path == "_diag/old.log")
        );
        assert!(
            report
                .observation
                .items
                .iter()
                .any(|item| item.decision == ItemDecision::PreservedScope)
        );
        let _ = fs::remove_dir_all(temp);
    }

    #[cfg(target_os = "macos")]
    #[test]
    #[ignore = "enabled when descriptor-bound store lands"]
    fn native_open_descriptor_vetoes_then_apply_reclaims_after_close() {
        let (temp, receipt) = fixture();
        let root = temp.join("runner");
        let old = root.join("_diag/old.log");
        fs::write(&old, b"expired").expect("old log");
        let touch = std::process::Command::new("/usr/bin/touch")
            .args(["-t", "202001010000", old.to_str().expect("utf8")])
            .status()
            .expect("touch");
        assert!(touch.success());
        let held = File::open(&old).expect("open descriptor");
        let plan = reclaim(
            &root,
            &receipt,
            Policy::new(MIN_RUNNER_LOG_RECLAIM_IDLE_SECONDS, 4).expect("policy"),
            None,
            false,
        )
        .expect("plan");
        assert!(plan.observation.process.open_identity_count > 0);
        assert_eq!(plan.observation.items[0].decision, ItemDecision::OpenOrBusy);
        let staged = root.join("_diag/.glaeda-test-stage.log");
        fs::rename(&old, &staged).expect("stage while held");
        let staged_plan = observe(&root, &receipt, policy).expect("staged observation");
        assert_eq!(staged_plan.items[0].decision, ItemDecision::OpenOrBusy);
        fs::rename(&staged, &old).expect("restore staged path");
        let refused = reclaim(
            &root,
            &receipt,
            Policy::new(MIN_RUNNER_LOG_RECLAIM_IDLE_SECONDS, 4).expect("policy"),
            None,
            true,
        )
        .expect("busy apply report");
        assert_eq!(refused.reclaimed_count, 0);
        assert!(
            refused
                .errors
                .iter()
                .any(|error| *error == "runner_process_busy")
        );
        drop(held);
        let applied = reclaim(
            &root,
            &receipt,
            Policy::new(MIN_RUNNER_LOG_RECLAIM_IDLE_SECONDS, 4).expect("policy"),
            None,
            true,
        )
        .expect("apply");
        assert_eq!(applied.reclaimed_count, 1);
        assert!(!old.exists());
        let _ = fs::remove_dir_all(temp);
    }
}
