//! Safe, bounded macOS observation and reclaim of expired Actions runner diagnostics.
//!
//! Safe libproc bindings inspect the enrolled UID and vnode file descriptors without
//! project-local FFI or subprocess probes. Budgets are cooperative deadlines between
//! kernel calls; incomplete evidence never proves absence. This delivery observes only:
//! apply explicitly refuses until the descriptor-bound store is independently accepted.

use std::collections::BTreeSet;
use std::fs::{self, File};
use std::io::{self, Read};
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

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
    pub errors: Vec<&'static str>,
    #[serde(skip_serializing)]
    open_identities: BTreeSet<OpenIdentity>,
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
            uid: rustix::process::geteuid().as_raw(),
            in_scope_pids_seen: 1,
            in_scope_pids_complete: true,
            unreadable_in_scope_pids: 0,
            busy_processes_observed: 0,
            open_identity_count: 0,
            errors: Vec::new(),
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
    // Darwin's vnode API exposes dev_t as unsigned 32 bits; std may sign-extend it.
    #[cfg(target_os = "macos")]
    let device = metadata.dev() as u32 as u64;
    #[cfg(not(target_os = "macos"))]
    let device = metadata.dev();
    FileIdentity {
        device,
        inode: metadata.ino(),
        links: metadata.nlink(),
        size: metadata.len(),
        modified_seconds: metadata.mtime(),
        changed_seconds: metadata.ctime(),
        modified_nanos: metadata.mtime_nsec(),
        changed_nanos: metadata.ctime_nsec(),
    }
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

fn read_bounded(path: &Path, limit: usize) -> io::Result<Vec<u8>> {
    let fd = rustix::fs::open(
        path,
        rustix::fs::OFlags::RDONLY
            | rustix::fs::OFlags::NOFOLLOW
            | rustix::fs::OFlags::NONBLOCK
            | rustix::fs::OFlags::CLOEXEC,
        rustix::fs::Mode::empty(),
    )
    .map_err(|_| io::Error::new(io::ErrorKind::NotFound, "bounded_open"))?;
    let stat = rustix::fs::fstat(&fd)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "bounded_stat"))?;
    if !rustix::fs::FileType::from_raw_mode(stat.st_mode).is_file()
        || stat.st_nlink != 1
        || stat.st_mode & 0o022 != 0
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "unsafe_bounded_read",
        ));
    }
    let mut bytes = Vec::new();
    File::from(fd)
        .take((limit as u64).saturating_add(1))
        .read_to_end(&mut bytes)?;
    if bytes.len() > limit {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "bounded_read"));
    }
    Ok(bytes)
}

/// Read an explicit enrollment receipt from a bounded, single-link regular file.
pub fn read_enrollment_receipt(path: &Path) -> Result<RunnerEnrollmentReceipt, ReclaimError> {
    let bytes = read_bounded(path, 65_536)
        .map_err(|_| ReclaimError::InvalidAuthority("enrollment_receipt_invalid"))?;
    serde_json::from_slice(&bytes)
        .map_err(|_| ReclaimError::InvalidAuthority("enrollment_receipt_invalid"))
}

fn preserved_path(path: &Path) -> bool {
    path.components().any(|component| {
        let name = component.as_os_str().to_string_lossy().to_ascii_lowercase();
        ["warm", "shared", "source", "history"]
            .iter()
            .any(|keep| name == *keep)
    })
}

#[cfg(target_os = "macos")]
fn observe_processes(root: &Path, uid: u32) -> Result<ProcessEvidence, ReclaimError> {
    use libproc::libproc::bsd_info::BSDInfo;
    use libproc::libproc::file_info::{ListFDs, ProcFDType, pidfdinfo};
    use libproc::libproc::proc_pid::{listpidinfo, pidinfo};

    const MAX_PIDS: usize = 4_096;
    const MAX_FDS_PER_PID: usize = 4_096;
    const MAX_FDS: usize = 1_000_000;
    const MAX_TIME: Duration = Duration::from_secs(5);
    let started = Instant::now();
    let pids = libproc::processes::pids_by_type(libproc::processes::ProcFilter::ByUID { uid })
        .map_err(|_| ReclaimError::Observation("process_table_unavailable"))?;
    let worker_path = root.join("bin/Runner.Worker");
    let mut failed = BTreeSet::new();
    let mut errors = BTreeSet::new();
    let mut busy = BTreeSet::new();
    let mut opened = BTreeSet::new();
    let mut scanned_fds = 0_usize;
    if started.elapsed() >= MAX_TIME {
        errors.insert("process_scan_budget_exhausted");
    }
    if pids.len() > MAX_PIDS {
        failed.extend(pids.iter().skip(MAX_PIDS).copied());
        errors.insert("pid_budget_exhausted");
    }
    for (index, pid) in pids.iter().take(MAX_PIDS).enumerate() {
        if started.elapsed() >= MAX_TIME || scanned_fds >= MAX_FDS {
            failed.extend(pids.iter().skip(index).copied());
            errors.insert("process_scan_budget_exhausted");
            break;
        }
        let before = match pidinfo::<BSDInfo>(*pid as i32, 0) {
            Ok(info) if info.pbi_uid == uid => (info.pbi_start_tvsec, info.pbi_start_tvusec),
            Ok(_) => {
                failed.insert(*pid);
                errors.insert("pid_uid_changed");
                continue;
            }
            Err(_) => {
                failed.insert(*pid);
                errors.insert("pid_unreadable_or_exited");
                continue;
            }
        };
        match libproc::libproc::proc_pid::pidpath(*pid as i32) {
            Ok(path) if !path.contains('\0') => {
                // A known busy executable vetoes reclaim even if its FD list cannot be read.
                if Path::new(&path) == worker_path {
                    busy.insert(*pid);
                }
            }
            _ => {
                failed.insert(*pid);
                errors.insert("pid_path_unreadable");
            }
        }
        match listpidinfo::<ListFDs>(*pid as i32, MAX_FDS_PER_PID) {
            Ok(fds) => {
                if fds.len() >= MAX_FDS_PER_PID {
                    failed.insert(*pid);
                    errors.insert("pid_fd_budget_exhausted");
                }
                for fd in fds {
                    if started.elapsed() >= MAX_TIME || scanned_fds >= MAX_FDS {
                        failed.insert(*pid);
                        errors.insert("process_scan_budget_exhausted");
                        break;
                    }
                    scanned_fds += 1;
                    if !matches!(ProcFDType::from(fd.proc_fdtype), ProcFDType::VNode) {
                        continue;
                    }
                    match pidfdinfo::<VNodeFDInfo>(*pid as i32, fd.proc_fd) {
                        Ok(vnode) => {
                            opened.insert(OpenIdentity {
                                device: vnode.pvi.vst_dev as u64,
                                inode: vnode.pvi.vst_ino,
                            });
                        }
                        Err(_) => {
                            failed.insert(*pid);
                            errors.insert("pid_fd_unreadable");
                        }
                    }
                }
            }
            Err(_) => {
                failed.insert(*pid);
                errors.insert("pid_fd_list_unreadable");
            }
        }
        match pidinfo::<BSDInfo>(*pid as i32, 0) {
            Ok(info)
                if info.pbi_uid == uid
                    && (info.pbi_start_tvsec, info.pbi_start_tvusec) == before => {}
            Ok(_) => {
                failed.insert(*pid);
                errors.insert("pid_identity_changed");
            }
            Err(_) => {
                failed.insert(*pid);
                errors.insert("pid_unreadable_or_exited");
            }
        }
        // Bind the busy decision to the same PID birth identity as the final
        // process check. A same-birth exec can otherwise change its executable
        // after the first path read and evade the worker veto.
        match libproc::libproc::proc_pid::pidpath(*pid as i32) {
            Ok(path) if worker_path.as_path() == Path::new(&path) => {
                busy.insert(*pid);
            }
            Ok(_) => {
                if busy.remove(pid) {
                    errors.insert("pid_path_changed");
                }
            }
            Err(_) => {
                busy.remove(pid);
                failed.insert(*pid);
                errors.insert("pid_path_unreadable");
            }
        }
    }
    Ok(ProcessEvidence {
        uid,
        in_scope_pids_seen: pids.len() as u64,
        in_scope_pids_complete: failed.is_empty() && errors.is_empty(),
        unreadable_in_scope_pids: failed.len() as u64,
        busy_processes_observed: busy.len() as u64,
        open_identity_count: opened.len() as u64,
        errors: errors.into_iter().collect(),
        open_identities: opened,
    })
}

#[cfg(not(target_os = "macos"))]
fn observe_processes(_root: &Path, _uid: u32) -> Result<ProcessEvidence, ReclaimError> {
    Err(ReclaimError::Observation("macos_only"))
}

/// Observe the synthetic benchmark fixture with a bounded fixture-local process contract while
/// keeping unrelated host-process inspection failures out of the authority result. This path is
/// restricted to the explicit synthetic enrollment namespace and is never used by production
/// reclaim. A fixture containing the runner worker executable is treated as busy and therefore
/// vetoed; production uses the full libproc descriptor observer above.
pub fn synthetic_fixture_process_evidence(
    root: &Path,
    receipt: &RunnerEnrollmentReceipt,
) -> Result<ProcessEvidence, ReclaimError> {
    if !receipt.install_id.starts_with("synthetic-") {
        return Err(ReclaimError::InvalidAuthority("synthetic_fixture_required"));
    }
    let (_, uid) = validate_authority(root, receipt)?;
    let busy = root.join("bin/Runner.Worker").is_file();
    let mut open_identities = BTreeSet::new();
    if let Ok(metadata) = fs::symlink_metadata(root.join("_diag/open.log"))
        && metadata.is_file()
        && !metadata.file_type().is_symlink()
    {
        let identity = identity(&metadata);
        open_identities.insert(OpenIdentity {
            device: identity.device,
            inode: identity.inode,
        });
    }
    Ok(ProcessEvidence {
        uid,
        in_scope_pids_seen: u64::from(busy),
        in_scope_pids_complete: !busy,
        unreadable_in_scope_pids: 0,
        busy_processes_observed: u64::from(busy),
        open_identity_count: open_identities.len() as u64,
        errors: Vec::new(),
        open_identities,
    })
}

fn validate_authority(
    root: &Path,
    receipt: &RunnerEnrollmentReceipt,
) -> Result<(PathBuf, u32), ReclaimError> {
    if root
        .components()
        .any(|component| component == std::path::Component::ParentDir)
    {
        return Err(ReclaimError::InvalidPath("runner_root_parent_component"));
    }
    if !receipt.runner_dir_owned
        || receipt.runner_dir != root.to_string_lossy()
        || receipt.install_id.is_empty()
    {
        return Err(ReclaimError::InvalidAuthority("runner_enrollment_mismatch"));
    }
    let canonical =
        fs::canonicalize(root).map_err(|_| ReclaimError::InvalidPath("runner_root_unreadable"))?;
    if canonical != root {
        return Err(ReclaimError::InvalidPath("runner_root_not_canonical"));
    }
    let metadata = fs::symlink_metadata(root)
        .map_err(|_| ReclaimError::InvalidPath("runner_root_unreadable"))?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() || metadata.mode() & 0o022 != 0 {
        return Err(ReclaimError::InvalidPath("runner_root_not_directory"));
    }
    let marker = root.join(MARKER);
    let marker_metadata = fs::symlink_metadata(&marker)
        .map_err(|_| ReclaimError::InvalidAuthority("runner_marker_missing"))?;
    if !marker_metadata.is_file()
        || marker_metadata.file_type().is_symlink()
        || marker_metadata.uid() != metadata.uid()
        || marker_metadata.mode() & 0o022 != 0
    {
        return Err(ReclaimError::InvalidAuthority("runner_marker_invalid"));
    }
    let marker_value = read_bounded(&marker, 4096)
        .map_err(|_| ReclaimError::InvalidAuthority("runner_marker_unreadable"))?;
    if String::from_utf8_lossy(&marker_value).trim() != receipt.install_id {
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
    if !metadata.is_dir()
        || metadata.file_type().is_symlink()
        || metadata.uid() != process.uid
        || metadata.mode() & 0o022 != 0
    {
        return Err(ReclaimError::Observation("diagnostics_unsafe_shape"));
    }
    let directory_before = identity(&metadata);
    let mut items = Vec::new();
    let entries =
        fs::read_dir(&diag).map_err(|_| ReclaimError::Observation("diagnostics_unreadable"))?;
    let mut entry_count = 0_usize;
    let started = Instant::now();
    for entry in entries {
        if started.elapsed() >= Duration::from_secs(5) {
            return Err(ReclaimError::Observation("diagnostics_deadline_exhausted"));
        }
        entry_count = entry_count.saturating_add(1);
        if entry_count > MAX_DIAG_ENTRIES {
            return Err(ReclaimError::Observation(
                "diagnostics_enumeration_truncated",
            ));
        }
        let entry = entry.map_err(|_| ReclaimError::Observation("diagnostics_unreadable"))?;
        let path = entry.path();
        let metadata = fs::symlink_metadata(&path)
            .map_err(|_| ReclaimError::Observation("diagnostics_unreadable"))?;
        if metadata.file_type().is_symlink() {
            continue;
        }
        if metadata.is_dir() {
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
        } else if process.busy_processes_observed > 0 {
            (ItemDecision::OpenOrBusy, "runner_process_busy")
        } else if metadata.nlink() != 1 {
            (ItemDecision::PreservedScope, "hard_link")
        } else if process.is_open(id) {
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
    let directory_after = fs::symlink_metadata(&diag)
        .map_err(|_| ReclaimError::Observation("diagnostics_changed"))?;
    if directory_before != identity(&directory_after) {
        return Err(ReclaimError::Observation("diagnostics_changed"));
    }
    Ok(items)
}

fn observe_with_process(
    root: &Path,
    receipt: &RunnerEnrollmentReceipt,
    policy: Policy,
    process: ProcessEvidence,
) -> Result<Observation, ReclaimError> {
    if !root.is_absolute() {
        return Err(ReclaimError::InvalidPath("runner_root_must_be_absolute"));
    }
    let (root, uid) = validate_authority(root, receipt)?;
    let root_before = identity(
        &fs::symlink_metadata(&root)
            .map_err(|_| ReclaimError::Observation("runner_root_changed"))?,
    );
    let all_items = enumerate_diag(&root, &process, policy, now_seconds()?)?;
    validate_authority(&root, receipt)?;
    let root_after = identity(
        &fs::symlink_metadata(&root)
            .map_err(|_| ReclaimError::Observation("runner_root_changed"))?,
    );
    if root_before != root_after {
        return Err(ReclaimError::Observation("runner_root_changed"));
    }
    let complete = process.in_scope_pids_complete;
    let item_count = all_items.len() as u64;
    let enumeration_truncated = false; // An incomplete catalog returns a typed error before planning.
    let eligible_count = all_items
        .iter()
        .filter(|item| item.decision == ItemDecision::ExpiredRunnerDiagnosticLog)
        .count() as u64;
    let items_truncated = all_items.len() > MAX_REPORT_ITEMS;
    let items = all_items.iter().take(MAX_REPORT_ITEMS).cloned().collect();
    Ok(Observation {
        // Public reports carry an authority class, never a private checkout
        // path. The CLI still receives the exact path through its arguments.
        runner_root: "<enrolled-runner>".into(),
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

pub fn observe(
    root: &Path,
    receipt: &RunnerEnrollmentReceipt,
    policy: Policy,
) -> Result<Observation, ReclaimError> {
    if !root.is_absolute() {
        return Err(ReclaimError::InvalidPath("runner_root_must_be_absolute"));
    }
    let (root, uid) = validate_authority(root, receipt)?;
    let root_before = identity(
        &fs::symlink_metadata(&root)
            .map_err(|_| ReclaimError::Observation("runner_root_changed"))?,
    );
    let process_root = fs::canonicalize(&root)
        .map_err(|_| ReclaimError::InvalidPath("runner_root_canonicalize_failed"))?;
    let process = observe_processes(&process_root, uid)?;
    observe_with_process(&root, receipt, policy, process).and_then(|observation| {
        let root_after = identity(
            &fs::symlink_metadata(&root)
                .map_err(|_| ReclaimError::Observation("runner_root_changed"))?,
        );
        if root_before != root_after {
            return Err(ReclaimError::Observation("runner_root_changed"));
        }
        Ok(observation)
    })
}

pub fn observe_synthetic_fixture(
    root: &Path,
    receipt: &RunnerEnrollmentReceipt,
    policy: Policy,
) -> Result<Observation, ReclaimError> {
    if !root.is_absolute() {
        return Err(ReclaimError::InvalidPath("runner_root_must_be_absolute"));
    }
    let (root, _) = validate_authority(root, receipt)?;
    let process = synthetic_fixture_process_evidence(&root, receipt)?;
    observe_with_process(&root, receipt, policy, process)
}

fn receipt_id(item: &LogItem) -> String {
    format!("{:016x}-{:016x}", item.identity.device, item.identity.inode)
}

fn report_from_observation(observation: Observation, policy: Policy, apply: bool) -> Report {
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
    Report {
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
    }
}

pub fn reclaim(
    root: &Path,
    receipt: &RunnerEnrollmentReceipt,
    policy: Policy,
    _requested_state_root: Option<&Path>,
    apply: bool,
) -> Result<Report, ReclaimError> {
    let observation = observe(root, receipt, policy)?;
    Ok(report_from_observation(observation, policy, apply))
}

pub fn reclaim_synthetic_fixture(
    root: &Path,
    receipt: &RunnerEnrollmentReceipt,
    policy: Policy,
) -> Result<Report, ReclaimError> {
    let observation = observe_synthetic_fixture(root, receipt, policy)?;
    Ok(report_from_observation(observation, policy, false))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs::File;
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
        let temp = fs::canonicalize(temp).expect("canonical fixture");
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
            runner_root: "<enrolled-runner>".into(),
            minimum_idle_seconds: 3_600,
            batch_size: 4,
            observation: Observation {
                runner_root: "<enrolled-runner>".into(),
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
                    errors: Vec::new(),
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

    #[test]
    fn catalog_is_flat_and_busy_open_unknown_gates_are_independent() {
        let (temp, _) = fixture();
        let root = temp.join("runner");
        fs::create_dir(root.join("_diag/warm")).unwrap();
        fs::write(root.join("_diag/warm/old.log"), b"warm").unwrap();
        let path = root.join("_diag/old.log");
        fs::write(&path, b"old").unwrap();
        let past = UNIX_EPOCH + Duration::from_secs(1_600_000_000);
        File::options()
            .write(true)
            .open(&path)
            .unwrap()
            .set_times(fs::FileTimes::new().set_modified(past))
            .unwrap();
        let id = identity(&fs::metadata(&path).unwrap());
        let policy = Policy::new(MIN_RUNNER_LOG_RECLAIM_IDLE_SECONDS, 4).unwrap();
        for (process, expected) in [
            (
                ProcessEvidence::test_complete(),
                ItemDecision::ExpiredRunnerDiagnosticLog,
            ),
            (ProcessEvidence::test_busy(), ItemDecision::OpenOrBusy),
            (ProcessEvidence::test_open(id), ItemDecision::OpenOrBusy),
            (ProcessEvidence::test_incomplete(), ItemDecision::Unknown),
        ] {
            let items = enumerate_diag(&root, &process, policy, 1_700_000_000).unwrap();
            assert_eq!(items.len(), 1);
            assert_eq!(items[0].relative_path, "_diag/old.log");
            assert_eq!(items[0].decision, expected);
        }
        fs::hard_link(&path, root.join("_diag/alias.log")).unwrap();
        let items = enumerate_diag(
            &root,
            &ProcessEvidence::test_complete(),
            policy,
            1_700_000_000,
        )
        .unwrap();
        assert!(
            items
                .iter()
                .all(|item| item.decision == ItemDecision::PreservedScope)
        );
        fs::remove_dir_all(temp).unwrap();
    }

    #[test]
    fn bounded_input_rejects_links_nonregular_and_oversize() {
        let (temp, receipt) = fixture();
        let path = temp.join("receipt.json");
        fs::write(&path, serde_json::to_vec(&receipt).unwrap()).unwrap();
        assert_eq!(read_enrollment_receipt(&path).unwrap(), receipt);
        std::os::unix::fs::symlink(&path, temp.join("link.json")).unwrap();
        assert!(read_enrollment_receipt(&temp.join("link.json")).is_err());
        assert!(read_enrollment_receipt(&temp.join("runner")).is_err());
        fs::hard_link(&path, temp.join("hard.json")).unwrap();
        assert!(read_enrollment_receipt(&path).is_err());
        fs::remove_file(temp.join("hard.json")).unwrap();
        fs::write(&path, vec![b' '; 65_537]).unwrap();
        assert!(read_enrollment_receipt(&path).is_err());
        let fifo = temp.join("fifo");
        assert!(
            std::process::Command::new("/usr/bin/mkfifo")
                .arg(&fifo)
                .status()
                .unwrap()
                .success()
        );
        assert!(read_enrollment_receipt(&fifo).is_err());
        fs::remove_dir_all(temp).unwrap();
    }

    #[test]
    fn authority_rejects_ancestor_alias_and_parent_components() {
        let (temp, receipt) = fixture();
        let alias = temp.join("alias");
        std::os::unix::fs::symlink(temp.join("runner"), &alias).unwrap();
        let mut alias_receipt = receipt.clone();
        alias_receipt.runner_dir = alias.to_string_lossy().into_owned();
        assert!(validate_authority(&alias, &alias_receipt).is_err());
        let parent = temp.join("runner/../runner");
        alias_receipt.runner_dir = parent.to_string_lossy().into_owned();
        assert!(validate_authority(&parent, &alias_receipt).is_err());
        fs::remove_dir_all(temp).unwrap();
    }
}
