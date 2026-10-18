//! Descriptor-bound transaction store for macOS runner diagnostic-log reclaim.
//!
//! The observer owns process facts; this module owns only the mutation boundary.  Every
//! directory used by a transaction is held by an `O_NOFOLLOW` descriptor.  Journal rows bind
//! the enrollment and root identity, so a shared state root cannot recover another runner's
//! staged file.

use std::collections::BTreeMap;
use std::ffi::CString;
use std::fs::File;
use std::io::{Read, Write};
use std::os::fd::OwnedFd;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::MetadataExt;
use std::path::{Component, Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use rustix::fs::{self as rfs, AtFlags, FileType, FlockOperation, Mode, OFlags, RenameFlags};
use serde::{Deserialize, Serialize};

use crate::runner_log_reclaim::{
    FileIdentity, ItemDecision, LogItem, Policy, ProcessEvidence, RunnerEnrollmentReceipt,
};
#[cfg(target_os = "macos")]
fn stat_nanos(value: i64) -> i64 {
    value
}
#[cfg(not(target_os = "macos"))]
fn stat_nanos(value: u64) -> i64 {
    value as i64
}
#[cfg(target_os = "macos")]
fn stat_device(value: i32) -> u64 {
    value as u32 as u64
}
#[cfg(not(target_os = "macos"))]
fn stat_device(value: u64) -> u64 {
    value
}
fn stat_links<T: Into<u64>>(value: T) -> u64 {
    value.into()
}

fn stat_matches_materialization(stat: &rfs::Stat, identity: FileIdentity) -> bool {
    stat_device(stat.st_dev) == identity.device
        && stat.st_ino == identity.inode
        && stat_links(stat.st_nlink) == identity.links
        && stat.st_size >= 0
        && stat.st_size as u64 == identity.size
        && stat.st_mtime == identity.modified_seconds
        && stat_nanos(stat.st_mtime_nsec) == identity.modified_nanos
        && stat.st_ctime == identity.changed_seconds
        && stat_nanos(stat.st_ctime_nsec) == identity.changed_nanos
}

// A successful rename updates ctime on Darwin.  The original ctime remains useful for
// materializing the source path, but it is not a stable identity for the staged name.
fn stat_matches_staged(stat: &rfs::Stat, identity: FileIdentity) -> bool {
    stat_matches_materialization(stat, identity)
}

#[allow(dead_code)]
fn identity_from_stat(stat: &rfs::Stat) -> Option<FileIdentity> {
    if stat.st_size < 0 {
        return None;
    }
    #[cfg(target_os = "macos")]
    let device = stat_device(stat.st_dev);
    #[cfg(not(target_os = "macos"))]
    let device = stat_device(stat.st_dev);
    Some(FileIdentity {
        device,
        inode: stat.st_ino,
        links: stat_links(stat.st_nlink),
        size: stat.st_size as u64,
        modified_seconds: stat.st_mtime,
        changed_seconds: stat.st_ctime,
        modified_nanos: stat_nanos(stat.st_mtime_nsec),
        changed_nanos: stat_nanos(stat.st_ctime_nsec),
    })
}

const DIR_FLAGS: OFlags = OFlags::RDONLY
    .union(OFlags::DIRECTORY)
    .union(OFlags::NOFOLLOW)
    .union(OFlags::CLOEXEC);
const FILE_FLAGS: OFlags = OFlags::RDWR.union(OFlags::NOFOLLOW).union(OFlags::CLOEXEC);
const READ_FILE_FLAGS: OFlags = OFlags::RDONLY
    .union(OFlags::NOFOLLOW)
    .union(OFlags::CLOEXEC);
const CREATE_FILE_FLAGS: OFlags = OFlags::RDWR
    .union(OFlags::CREATE)
    .union(OFlags::APPEND)
    .union(OFlags::NOFOLLOW)
    .union(OFlags::CLOEXEC);
const MIN_IDLE_SECONDS: i64 = 3_600;
const MAX_JOURNAL_BYTES: usize = 1 << 20;
const MAX_MARKER_BYTES: usize = 256;
const MAX_INSTALL_ID_BYTES: usize = 128;
const MAX_ROW_STRING_BYTES: usize = 1_024;

#[derive(Debug)]
pub enum StoreError {
    Invalid(&'static str),
    Io(&'static str),
    Busy,
    ObservationIncomplete,
}

impl std::fmt::Display for StoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Invalid(code) | Self::Io(code) => f.write_str(code),
            Self::Busy => f.write_str("apply_busy"),
            Self::ObservationIncomplete => f.write_str("observation_incomplete"),
        }
    }
}
impl std::error::Error for StoreError {}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
struct JournalRow {
    tx_id: String,
    phase: String,
    install_id: String,
    root_device: u64,
    root_inode: u64,
    root_uid: u32,
    relative_path: String,
    stage_name: String,
    identity: FileIdentity,
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct RecoveryReceipt {
    pub pending: u64,
    pub confirmed_deleted: u64,
    pub absent_after_delete_intent: u64,
    pub aborted_prepared: u64,
    pub aborted_staged: u64,
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct ApplyReceipt {
    pub reclaimed: u64,
    pub skipped: u64,
    pub receipts: Vec<String>,
}

/// Result of the explicitly synthetic fixture transaction. This is the only public mutation
/// helper exposed by the store; production callers still use the typed adapter, whose live apply
/// backend remains disabled.
#[derive(Debug, Clone, Default, Serialize)]
pub struct SyntheticFixtureApplyReceipt {
    pub recovery: RecoveryReceipt,
    pub apply: ApplyReceipt,
}

/// Require the synthetic transaction to stay inside the benchmark's private temporary fixture.
/// The install-id prefix is necessary but insufficient: callers must not be able to turn this
/// test-only mutation path into a general owned-root deletion API.
pub fn validate_synthetic_fixture_paths(root: &Path, state_root: &Path) -> Result<(), StoreError> {
    let root = std::fs::canonicalize(root).map_err(|_| StoreError::Invalid("fixture_root"))?;
    let temp = std::fs::canonicalize(std::env::temp_dir())
        .map_err(|_| StoreError::Invalid("fixture_temp_root"))?;
    let base = root
        .parent()
        .and_then(Path::parent)
        .ok_or(StoreError::Invalid("fixture_root"))?;
    let state_parent = state_root
        .parent()
        .ok_or(StoreError::Invalid("fixture_state_root"))?;
    let state_parent = std::fs::canonicalize(state_parent)
        .map_err(|_| StoreError::Invalid("fixture_state_root"))?;
    let root_parent_name = root
        .parent()
        .and_then(Path::file_name)
        .and_then(|value| value.to_str());
    let root_name = root.file_name().and_then(|value| value.to_str());
    let state_name = state_root.file_name().and_then(|value| value.to_str());
    if !base.starts_with(&temp)
        || !base
            .file_name()
            .and_then(|value| value.to_str())
            .is_some_and(|value| value.starts_with("glaeda-bench-native-"))
        || state_parent != base
        || root_parent_name != Some("home")
        || !root_name.is_some_and(|value| value.starts_with("actions-runner-"))
        || !state_name
            .is_some_and(|value| value.starts_with("actions-runner-") && value.ends_with("-state"))
    {
        return Err(StoreError::Invalid("fixture_path_scope"));
    }
    Ok(())
}

pub struct RunnerLogStore {
    root: OwnedFd,
    diag: OwnedFd,
    state: OwnedFd,
    _lock: OwnedFd,
    root_device: u64,
    root_inode: u64,
    diag_device: u64,
    diag_inode: u64,
    marker_device: u64,
    marker_inode: u64,
    marker_path: PathBuf,
    root_uid: u32,
    root_path: PathBuf,
    diag_path: PathBuf,
    install_id: String,
}

fn phase_rank(phase: &str) -> Result<u8, StoreError> {
    match phase {
        "prepared" => Ok(1),
        "staged" => Ok(2),
        "delete_intent" => Ok(3),
        "projected" | "aborted" => Ok(4),
        _ => Err(StoreError::Io("journal_phase_unknown")),
    }
}

fn read_journal(
    state: &OwnedFd,
    expected_root: (u64, u64),
    expected_uid: u32,
    expected_install: &str,
) -> Result<BTreeMap<String, JournalRow>, StoreError> {
    let journal_name = CString::new("journal.jsonl").unwrap();
    let file = match rfs::openat(state, &journal_name, FILE_FLAGS, Mode::empty()) {
        Ok(file) => file,
        Err(rustix::io::Errno::NOENT) => return Ok(BTreeMap::new()),
        Err(_) => return Err(StoreError::Io("journal_open_failed")),
    };
    let stat = rfs::fstat(&file).map_err(|_| StoreError::Io("journal_stat_failed"))?;
    let state_stat = rfs::fstat(state).map_err(|_| StoreError::Io("state_stat_failed"))?;
    if !file_is_safe(&stat, state_stat.st_uid) || stat.st_size < 0 {
        return Err(StoreError::Io("journal_unsafe"));
    }
    let file = File::from(file);
    let mut bytes = Vec::new();
    file.take((MAX_JOURNAL_BYTES + 1) as u64)
        .read_to_end(&mut bytes)
        .map_err(|_| StoreError::Io("journal_read_failed"))?;
    if bytes.len() > MAX_JOURNAL_BYTES {
        return Err(StoreError::Io("journal_too_large"));
    }
    let mut pending = BTreeMap::<String, JournalRow>::new();
    let mut seen = BTreeMap::<String, (u8, JournalRow)>::new();
    for line in bytes
        .split(|byte| *byte == b'\n')
        .filter(|line| !line.is_empty())
    {
        let row: JournalRow =
            serde_json::from_slice(line).map_err(|_| StoreError::Io("journal_corrupt"))?;
        if row.tx_id.is_empty()
            || row.tx_id.len() > MAX_ROW_STRING_BYTES
            || row.install_id.len() > MAX_INSTALL_ID_BYTES
            || row.relative_path.len() > MAX_ROW_STRING_BYTES
            || row.stage_name.len() > MAX_ROW_STRING_BYTES
        {
            return Err(StoreError::Io("journal_corrupt"));
        }
        if row.root_device != expected_root.0
            || row.root_inode != expected_root.1
            || row.root_uid != expected_uid
            || row.install_id != expected_install
        {
            continue;
        }
        let _ = relative_components(&row.relative_path)?;
        let (_, stage) = relative_components(&row.stage_name)?;
        if !stage.to_bytes().starts_with(b".glaeda-reclaiming-") {
            return Err(StoreError::Io("journal_corrupt"));
        }
        let rank = phase_rank(&row.phase)?;
        if let Some((previous, previous_row)) = seen.get(&row.tx_id)
            && (rank < *previous || (rank == *previous && previous_row != &row))
        {
            return Err(StoreError::Io("journal_phase_regression"));
        }
        seen.insert(row.tx_id.clone(), (rank, row.clone()));
        match row.phase.as_str() {
            "prepared" | "staged" | "delete_intent" => {
                pending.insert(row.tx_id.clone(), row);
            }
            "projected" | "aborted" => {
                pending.remove(&row.tx_id);
            }
            _ => unreachable!(),
        }
    }
    Ok(pending)
}

fn name(path: &Path) -> Result<CString, StoreError> {
    let value = path
        .file_name()
        .ok_or(StoreError::Invalid("name_missing"))?;
    CString::new(value.as_bytes()).map_err(|_| StoreError::Invalid("name_invalid"))
}

fn validate_clean_absolute(path: &Path, code: &'static str) -> Result<(), StoreError> {
    if !path.is_absolute() {
        return Err(StoreError::Invalid(code));
    }
    for component in path.components() {
        if !matches!(component, Component::RootDir | Component::Normal(_)) {
            return Err(StoreError::Invalid(code));
        }
    }
    Ok(())
}

fn bounded_install_id(value: &str) -> Result<(), StoreError> {
    if value.is_empty()
        || value.len() > MAX_INSTALL_ID_BYTES
        || value.as_bytes().contains(&0)
        || value.bytes().any(|byte| byte == b'\n' || byte == b'\r')
        || value.bytes().any(
            |byte| !matches!(byte, b'a'..=b'z' | b'A'..=b'Z' | b'0'..=b'9' | b'.' | b'_' | b'-'),
        )
    {
        return Err(StoreError::Invalid("install_id_invalid"));
    }
    Ok(())
}

fn directory_is_safe(stat: &rfs::Stat, owner: u32) -> bool {
    FileType::from_raw_mode(stat.st_mode).is_dir()
        && stat.st_uid == owner
        && stat.st_mode & 0o022 == 0
}

fn file_is_safe(stat: &rfs::Stat, owner: u32) -> bool {
    FileType::from_raw_mode(stat.st_mode).is_file()
        && stat.st_uid == owner
        && stat_links(stat.st_nlink) == 1
        && stat.st_mode & 0o022 == 0
}

fn relative_components(path: &str) -> Result<(Vec<CString>, CString), StoreError> {
    if path.len() > MAX_ROW_STRING_BYTES {
        return Err(StoreError::Invalid("candidate_path_too_large"));
    }
    let mut parts = Path::new(path).components();
    if parts.next().and_then(|part| part.as_os_str().to_str()) != Some("_diag") {
        return Err(StoreError::Invalid("candidate_scope"));
    }
    let mut leaf = None;
    for component in parts {
        let Component::Normal(value) = component else {
            return Err(StoreError::Invalid("candidate_path"));
        };
        if leaf.is_some() {
            return Err(StoreError::Invalid("candidate_nested"));
        }
        let item =
            CString::new(value.as_bytes()).map_err(|_| StoreError::Invalid("candidate_name"))?;
        leaf = Some(item.clone());
    }
    let leaf = leaf.ok_or(StoreError::Invalid("candidate_name_missing"))?;
    Ok((Vec::new(), leaf))
}

fn open_child(parent: &OwnedFd, child: &CString, flags: OFlags) -> Result<OwnedFd, StoreError> {
    rfs::openat(parent, child, flags, Mode::empty()).map_err(|_| StoreError::Io("openat_failed"))
}

fn open_relative_dir(root: &OwnedFd, dirs: &[CString]) -> Result<OwnedFd, StoreError> {
    let mut current = rustix::io::dup(root).map_err(|_| StoreError::Io("root_dup_failed"))?;
    for child in dirs {
        current = open_child(&current, child, DIR_FLAGS)?;
    }
    Ok(current)
}

fn open_dir_chain(path: &Path) -> Result<OwnedFd, StoreError> {
    validate_clean_absolute(path, "directory_path")?;
    let mut current = rfs::open("/", DIR_FLAGS, Mode::empty())
        .map_err(|_| StoreError::Io("root_directory_open_failed"))?;
    for component in path.components() {
        let Component::Normal(value) = component else {
            continue;
        };
        let child =
            CString::new(value.as_bytes()).map_err(|_| StoreError::Invalid("directory_name"))?;
        current = open_child(&current, &child, DIR_FLAGS)?;
    }
    Ok(current)
}

fn append_row(state: &OwnedFd, row: &JournalRow) -> Result<(), StoreError> {
    if row.tx_id.len() > MAX_ROW_STRING_BYTES
        || row.install_id.len() > MAX_INSTALL_ID_BYTES
        || row.relative_path.len() > MAX_ROW_STRING_BYTES
        || row.stage_name.len() > MAX_ROW_STRING_BYTES
    {
        return Err(StoreError::Io("journal_row_too_large"));
    }
    let mut bytes = serde_json::to_vec(row).map_err(|_| StoreError::Io("journal_encode_failed"))?;
    bytes.push(b'\n');
    if bytes.len() > MAX_JOURNAL_BYTES {
        return Err(StoreError::Io("journal_row_too_large"));
    }
    let name = CString::new("journal.jsonl").unwrap();
    let file = rfs::openat(state, &name, CREATE_FILE_FLAGS, Mode::from_raw_mode(0o600))
        .map_err(|_| StoreError::Io("journal_open_failed"))?;
    let file_stat = rfs::fstat(&file).map_err(|_| StoreError::Io("journal_stat_failed"))?;
    let state_stat = rfs::fstat(state).map_err(|_| StoreError::Io("state_stat_failed"))?;
    if !file_is_safe(&file_stat, state_stat.st_uid)
        || file_stat.st_size < 0
        || file_stat.st_size as usize > MAX_JOURNAL_BYTES.saturating_sub(bytes.len())
    {
        return Err(StoreError::Io("journal_unsafe"));
    }
    let mut file = File::from(file);
    file.write_all(&bytes)
        .map_err(|_| StoreError::Io("journal_write_failed"))?;
    file.sync_data()
        .map_err(|_| StoreError::Io("journal_sync_failed"))?;
    rfs::fsync(state).map_err(|_| StoreError::Io("journal_parent_sync_failed"))
}

#[allow(dead_code)]
fn transaction_id(install_id: &str, identity: FileIdentity) -> Result<String, StoreError> {
    let mut nonce = [0_u8; 16];
    let mut random =
        File::open("/dev/urandom").map_err(|_| StoreError::Io("transaction_id_failed"))?;
    random
        .read_exact(&mut nonce)
        .map_err(|_| StoreError::Io("transaction_id_failed"))?;
    Ok(format!(
        "{install_id}-{:016x}-{:016x}-{}",
        identity.device,
        identity.inode,
        nonce
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>()
    ))
}

fn now_seconds() -> Result<i64, StoreError> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|value| value.as_secs() as i64)
        .map_err(|_| StoreError::Io("clock_before_epoch"))
}

impl RunnerLogStore {
    /// Opaque generation derived from the retained descriptor identities.  It is used only to
    /// bind a fresh admission to the exact store opened under its lock.
    pub(crate) fn store_generation(&self) -> u64 {
        self.root_device
            .wrapping_mul(0x9e37_79b9_7f4a_7c15)
            .wrapping_add(self.root_inode.rotate_left(13))
            .wrapping_add(self.diag_device.rotate_left(29))
            .wrapping_add(self.diag_inode.rotate_left(41))
            .wrapping_add(self.marker_device.rotate_left(7))
            .wrapping_add(self.marker_inode.rotate_left(17))
    }

    #[allow(dead_code)]
    fn journal_row(
        &self,
        tx_id: &str,
        phase: &str,
        relative_path: &str,
        stage_name: &str,
        identity: FileIdentity,
    ) -> JournalRow {
        JournalRow {
            tx_id: tx_id.into(),
            phase: phase.into(),
            install_id: self.install_id.clone(),
            root_device: self.root_device,
            root_inode: self.root_inode,
            root_uid: self.root_uid,
            relative_path: relative_path.into(),
            stage_name: stage_name.into(),
            identity,
        }
    }

    fn process_is_safe(&self, process: &ProcessEvidence) -> bool {
        process.uid == self.root_uid
            && process.in_scope_pids_complete
            && process.busy_processes_observed == 0
    }

    fn staged_stat_is_safe(
        &self,
        stat: &rfs::Stat,
        identity: FileIdentity,
        policy: Policy,
    ) -> Result<(), StoreError> {
        if !FileType::from_raw_mode(stat.st_mode).is_file()
            || stat_links(stat.st_nlink) != 1
            || stat.st_uid != self.root_uid
            || stat.st_mode & 0o022 != 0
            || !stat_matches_staged(stat, identity)
            || now_seconds()?.saturating_sub(stat.st_mtime) < policy.minimum_idle_seconds
        {
            return Err(StoreError::Invalid("staged_identity_changed"));
        }
        Ok(())
    }

    pub(crate) fn open(
        root: &Path,
        receipt: &RunnerEnrollmentReceipt,
        state_root: &Path,
    ) -> Result<Self, StoreError> {
        validate_clean_absolute(root, "root_path")?;
        validate_clean_absolute(state_root, "state_path")?;
        bounded_install_id(&receipt.install_id)?;
        if !receipt.runner_dir_owned || receipt.runner_dir != root.to_string_lossy() {
            return Err(StoreError::Invalid("enrollment_mismatch"));
        }
        let root_path = root.to_path_buf();
        let root_name = name(root)?;
        let root_parent = root
            .parent()
            .ok_or(StoreError::Invalid("root_parent_missing"))?;
        let root_parent = open_dir_chain(root_parent)?;
        let root = open_child(&root_parent, &root_name, DIR_FLAGS)?;
        let root_stat = rfs::fstat(&root).map_err(|_| StoreError::Io("root_stat_failed"))?;
        let owner = rustix::process::geteuid().as_raw();
        if !directory_is_safe(&root_stat, owner) {
            return Err(StoreError::Invalid("root_unsafe"));
        }
        let marker = CString::new(".glaeda-cmux-runner").unwrap();
        let marker_file = rfs::openat(&root, &marker, READ_FILE_FLAGS, Mode::empty())
            .map_err(|_| StoreError::Io("marker_open_failed"))?;
        let marker_stat =
            rfs::fstat(&marker_file).map_err(|_| StoreError::Io("marker_stat_failed"))?;
        if !file_is_safe(&marker_stat, root_stat.st_uid) {
            return Err(StoreError::Invalid("marker_unsafe"));
        }
        let marker_file = File::from(marker_file);
        let mut marker_value = String::new();
        marker_file
            .take((MAX_MARKER_BYTES + 1) as u64)
            .read_to_string(&mut marker_value)
            .map_err(|_| StoreError::Io("marker_read_failed"))?;
        if marker_value.len() > MAX_MARKER_BYTES {
            return Err(StoreError::Invalid("marker_too_large"));
        }
        if marker_value.trim() != receipt.install_id {
            return Err(StoreError::Invalid("marker_mismatch"));
        }
        let diag_name = CString::new("_diag").unwrap();
        let diag = open_child(&root, &diag_name, DIR_FLAGS)?;
        let diag_stat = rfs::fstat(&diag).map_err(|_| StoreError::Io("diag_stat_failed"))?;
        if !directory_is_safe(&diag_stat, root_stat.st_uid) {
            return Err(StoreError::Invalid("diag_unsafe"));
        }
        let state_name = name(state_root)?;
        let state_parent_path = state_root
            .parent()
            .ok_or(StoreError::Invalid("state_parent_missing"))?;
        let state_parent = open_dir_chain(state_parent_path)?;
        let state = match open_child(&state_parent, &state_name, DIR_FLAGS) {
            Ok(state) => state,
            Err(StoreError::Io("openat_failed")) => {
                rfs::mkdirat(&state_parent, &state_name, Mode::from_raw_mode(0o700))
                    .map_err(|_| StoreError::Io("state_create_failed"))?;
                rfs::fsync(&state_parent)
                    .map_err(|_| StoreError::Io("state_parent_sync_failed"))?;
                open_child(&state_parent, &state_name, DIR_FLAGS)?
            }
            Err(error) => return Err(error),
        };
        let state_stat = rfs::fstat(&state).map_err(|_| StoreError::Io("state_stat_failed"))?;
        if !directory_is_safe(&state_stat, root_stat.st_uid) {
            return Err(StoreError::Invalid("state_unsafe"));
        }
        let lock_name = CString::new(".lock").unwrap();
        let lock = rfs::openat(
            &state,
            &lock_name,
            CREATE_FILE_FLAGS,
            Mode::from_raw_mode(0o600),
        )
        .map_err(|_| StoreError::Io("lock_open_failed"))?;
        let lock_stat = rfs::fstat(&lock).map_err(|_| StoreError::Io("lock_stat_failed"))?;
        if !file_is_safe(&lock_stat, state_stat.st_uid) {
            return Err(StoreError::Invalid("lock_unsafe"));
        }
        rfs::flock(&lock, FlockOperation::NonBlockingLockExclusive)
            .map_err(|_| StoreError::Busy)?;
        Ok(Self {
            root,
            diag,
            state,
            _lock: lock,
            root_device: stat_device(root_stat.st_dev),
            root_inode: root_stat.st_ino,
            marker_device: stat_device(marker_stat.st_dev),
            marker_inode: marker_stat.st_ino,
            marker_path: root_path.join(".glaeda-cmux-runner"),
            root_uid: root_stat.st_uid,
            diag_device: stat_device(diag_stat.st_dev),
            diag_inode: diag_stat.st_ino,
            root_path: root_path.clone(),
            diag_path: root_path.join("_diag"),
            install_id: receipt.install_id.clone(),
        })
    }

    fn aliases_unchanged(&self) -> bool {
        let Ok(root) = std::fs::symlink_metadata(&self.root_path) else {
            return false;
        };
        let Ok(diag) = std::fs::symlink_metadata(&self.diag_path) else {
            return false;
        };
        let Ok(marker) = std::fs::symlink_metadata(&self.marker_path) else {
            return false;
        };
        let Ok(held_root) = rfs::fstat(&self.root) else {
            return false;
        };
        let Ok(held_diag) = rfs::fstat(&self.diag) else {
            return false;
        };
        let Ok(held_marker) = rfs::statat(
            &self.root,
            CString::new(".glaeda-cmux-runner").unwrap(),
            AtFlags::SYMLINK_NOFOLLOW,
        ) else {
            return false;
        };
        let marker_file = rfs::openat(
            &self.root,
            CString::new(".glaeda-cmux-runner").unwrap(),
            READ_FILE_FLAGS,
            Mode::empty(),
        );
        let Ok(marker_file) = marker_file else {
            return false;
        };
        let Ok(marker_stat) = rfs::fstat(&marker_file) else {
            return false;
        };
        let mut marker_value = String::new();
        if File::from(marker_file)
            .take((MAX_MARKER_BYTES + 1) as u64)
            .read_to_string(&mut marker_value)
            .is_err()
            || marker_value.len() > MAX_MARKER_BYTES
        {
            return false;
        }
        root.dev() == self.root_device
            && root.ino() == self.root_inode
            && diag.dev() == self.diag_device
            && diag.ino() == self.diag_inode
            && marker.dev() == self.marker_device
            && marker.ino() == self.marker_inode
            && directory_is_safe(&held_root, self.root_uid)
            && directory_is_safe(&held_diag, self.root_uid)
            && file_is_safe(&held_marker, self.root_uid)
            && file_is_safe(&marker_stat, self.root_uid)
            && marker_value.trim() == self.install_id
    }

    fn fresh<F>(&self, observe: &mut F) -> Result<ProcessEvidence, StoreError>
    where
        F: FnMut() -> Result<ProcessEvidence, StoreError>,
    {
        if !self.aliases_unchanged() {
            return Err(StoreError::Invalid("root_alias_changed"));
        }
        let process = observe()?;
        if process.uid != self.root_uid {
            return Err(StoreError::Invalid("process_uid_changed"));
        }
        if !self.aliases_unchanged() {
            return Err(StoreError::Invalid("root_alias_changed"));
        }
        Ok(process)
    }

    pub(crate) fn recover<F>(
        &mut self,
        policy: Policy,
        mut observe: F,
    ) -> Result<RecoveryReceipt, StoreError>
    where
        F: FnMut() -> Result<ProcessEvidence, StoreError>,
    {
        if policy.minimum_idle_seconds < MIN_IDLE_SECONDS {
            return Err(StoreError::Invalid("idle_window_too_small"));
        }
        let process = self.fresh(&mut observe)?;
        if !self.process_is_safe(&process) {
            return Err(StoreError::ObservationIncomplete);
        }
        let pending = read_journal(
            &self.state,
            (self.root_device, self.root_inode),
            self.root_uid,
            &self.install_id,
        )?;
        let mut receipt = RecoveryReceipt {
            pending: pending.len() as u64,
            ..RecoveryReceipt::default()
        };
        for row in pending.values() {
            let process = self.fresh(&mut observe)?;
            if !self.process_is_safe(&process) {
                return Err(StoreError::ObservationIncomplete);
            }
            let (dirs, stage_name) = relative_components(&row.stage_name)?;
            let directory = open_relative_dir(&self.diag, &dirs)?;
            let current = match rfs::statat(&directory, &stage_name, AtFlags::SYMLINK_NOFOLLOW) {
                Ok(stat) => stat,
                Err(rustix::io::Errno::NOENT) => {
                    if row.phase == "delete_intent" {
                        receipt.absent_after_delete_intent += 1;
                        append_row(
                            &self.state,
                            &JournalRow {
                                phase: "projected".into(),
                                ..row.clone()
                            },
                        )?;
                    } else if row.phase == "prepared" || row.phase == "staged" {
                        if row.phase == "prepared" {
                            receipt.aborted_prepared += 1;
                        } else {
                            receipt.aborted_staged += 1;
                        }
                        append_row(
                            &self.state,
                            &JournalRow {
                                phase: "aborted".into(),
                                ..row.clone()
                            },
                        )?;
                    }
                    continue;
                }
                Err(_) => return Err(StoreError::Io("recovery_observe_failed")),
            };
            if row.phase == "prepared" {
                return Err(StoreError::ObservationIncomplete);
            }
            self.staged_stat_is_safe(&current, row.identity, policy)?;
            if process.is_open(row.identity) {
                continue;
            }
            append_row(
                &self.state,
                &JournalRow {
                    phase: "delete_intent".into(),
                    ..row.clone()
                },
            )?;
            rfs::unlinkat(&directory, &stage_name, AtFlags::empty())
                .map_err(|_| StoreError::Io("recovery_delete_failed"))?;
            rfs::fsync(&directory).map_err(|_| StoreError::Io("recovery_delete_sync_failed"))?;
            receipt.confirmed_deleted += 1;
            append_row(
                &self.state,
                &JournalRow {
                    phase: "projected".into(),
                    ..row.clone()
                },
            )?;
        }
        receipt.pending = receipt.pending.saturating_sub(
            receipt.confirmed_deleted
                + receipt.absent_after_delete_intent
                + receipt.aborted_prepared
                + receipt.aborted_staged,
        );
        Ok(receipt)
    }

    #[allow(dead_code)]
    pub(crate) fn apply_batch<F>(
        &mut self,
        candidates: &[LogItem],
        policy: Policy,
        mut observe: F,
    ) -> Result<ApplyReceipt, StoreError>
    where
        F: FnMut() -> Result<ProcessEvidence, StoreError>,
    {
        if candidates.len() > policy.batch_size as usize {
            return Err(StoreError::Invalid("batch_size_out_of_range"));
        }
        self.recover(policy, &mut observe)?;
        let process = self.fresh(&mut observe)?;
        if !self.process_is_safe(&process) {
            return Err(StoreError::ObservationIncomplete);
        }
        let mut receipt = ApplyReceipt::default();
        for item in candidates {
            let process = self.fresh(&mut observe)?;
            if !self.process_is_safe(&process) {
                return Err(StoreError::ObservationIncomplete);
            }
            if item.decision != ItemDecision::ExpiredRunnerDiagnosticLog {
                receipt.skipped += 1;
                continue;
            }
            let (dirs, source_name) = relative_components(&item.relative_path)?;
            let directory = open_relative_dir(&self.diag, &dirs)?;
            let source = rfs::statat(&directory, &source_name, AtFlags::SYMLINK_NOFOLLOW)
                .map_err(|_| StoreError::Io("candidate_observe_failed"))?;
            if !dirs.is_empty()
                || !source_name.to_bytes().ends_with(b".log")
                || !FileType::from_raw_mode(source.st_mode).is_file()
                || source.st_nlink != 1
                || source.st_uid != process.uid
                || now_seconds()?.saturating_sub(item.identity.modified_seconds)
                    < policy.minimum_idle_seconds
                || !stat_matches_materialization(&source, item.identity)
            {
                receipt.skipped += 1;
                continue;
            }
            let tx_id = transaction_id(&self.install_id, item.identity)?;
            let stage_name = CString::new(format!(".glaeda-reclaiming-{tx_id}")).unwrap();
            let stage_path = format!("_diag/{}", stage_name.to_string_lossy());
            append_row(
                &self.state,
                &self.journal_row(
                    &tx_id,
                    "prepared",
                    &item.relative_path,
                    &stage_path,
                    item.identity,
                ),
            )?;
            rfs::renameat_with(
                &directory,
                &source_name,
                &directory,
                &stage_name,
                RenameFlags::NOREPLACE,
            )
            .map_err(|_| StoreError::Io("candidate_stage_failed"))?;
            rfs::fsync(&directory).map_err(|_| StoreError::Io("candidate_stage_sync_failed"))?;
            let staged = rfs::statat(&directory, &stage_name, AtFlags::SYMLINK_NOFOLLOW)
                .map_err(|_| StoreError::Io("candidate_stage_observe_failed"))?;
            let staged_identity = identity_from_stat(&staged)
                .ok_or(StoreError::Io("candidate_stage_identity_changed"))?;
            append_row(
                &self.state,
                &self.journal_row(
                    &tx_id,
                    "staged",
                    &item.relative_path,
                    &stage_path,
                    staged_identity,
                ),
            )?;
            let post = self.fresh(&mut observe)?;
            if !post.in_scope_pids_complete || post.busy_processes_observed != 0 {
                return Err(StoreError::ObservationIncomplete);
            }
            let staged = rfs::statat(&directory, &stage_name, AtFlags::SYMLINK_NOFOLLOW)
                .map_err(|_| StoreError::Io("candidate_stage_observe_failed"))?;
            self.staged_stat_is_safe(&staged, staged_identity, policy)?;
            if post.is_open(staged_identity) {
                return Err(StoreError::Io("candidate_stage_identity_changed"));
            }
            append_row(
                &self.state,
                &self.journal_row(
                    &tx_id,
                    "delete_intent",
                    &item.relative_path,
                    &stage_path,
                    staged_identity,
                ),
            )?;
            let final_process = self.fresh(&mut observe)?;
            if !self.process_is_safe(&final_process) || final_process.is_open(staged_identity) {
                return Err(StoreError::ObservationIncomplete);
            }
            let final_stage = rfs::statat(&directory, &stage_name, AtFlags::SYMLINK_NOFOLLOW)
                .map_err(|_| StoreError::Io("candidate_stage_observe_failed"))?;
            self.staged_stat_is_safe(&final_stage, staged_identity, policy)?;
            rfs::unlinkat(&directory, &stage_name, AtFlags::empty())
                .map_err(|_| StoreError::Io("candidate_delete_failed"))?;
            rfs::fsync(&directory).map_err(|_| StoreError::Io("candidate_delete_sync_failed"))?;
            append_row(
                &self.state,
                &self.journal_row(
                    &tx_id,
                    "projected",
                    &item.relative_path,
                    &stage_path,
                    staged_identity,
                ),
            )?;
            receipt.reclaimed += 1;
            receipt.receipts.push(tx_id);
        }
        Ok(receipt)
    }
}

/// Apply an already-observed synthetic fixture batch through the descriptor-bound transaction
/// store. The enrollment namespace is deliberately reserved so this cannot be used for a live
/// runner root. The closure is still called before recovery and every candidate, preserving the
/// same fail-closed process checks as the underlying transaction path.
pub fn apply_synthetic_fixture_batch<F>(
    root: &Path,
    receipt: &RunnerEnrollmentReceipt,
    state_root: &Path,
    candidates: &[LogItem],
    policy: Policy,
    mut observe: F,
) -> Result<SyntheticFixtureApplyReceipt, StoreError>
where
    F: FnMut() -> Result<ProcessEvidence, StoreError>,
{
    if !receipt.install_id.starts_with("synthetic-") {
        return Err(StoreError::Invalid("synthetic_fixture_required"));
    }
    validate_synthetic_fixture_paths(root, state_root)?;
    let mut store = RunnerLogStore::open(root, receipt, state_root)?;
    let recovery = store.recover(policy, &mut observe)?;
    if recovery.pending != 0 {
        return Err(StoreError::ObservationIncomplete);
    }
    let apply = store.apply_batch(candidates, policy, &mut observe)?;
    Ok(SyntheticFixtureApplyReceipt { recovery, apply })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::os::unix::fs::{MetadataExt, PermissionsExt};

    fn fixture() -> (PathBuf, RunnerEnrollmentReceipt, FileIdentity) {
        let root = std::env::temp_dir().join(format!(
            "glaeda-store-{}-{}",
            std::process::id(),
            transaction_id(
                "test",
                FileIdentity {
                    device: 1,
                    inode: 1,
                    links: 1,
                    size: 0,
                    modified_seconds: 0,
                    changed_seconds: 0,
                    modified_nanos: 0,
                    changed_nanos: 0,
                }
            )
            .unwrap()
        ));
        fs::create_dir_all(root.join("runner/_diag")).unwrap();
        fs::create_dir_all(root.join("state")).unwrap();
        fs::set_permissions(root.join("runner"), fs::Permissions::from_mode(0o700)).unwrap();
        fs::set_permissions(root.join("state"), fs::Permissions::from_mode(0o700)).unwrap();
        fs::write(root.join("runner/.glaeda-cmux-runner"), "install\n").unwrap();
        let log = root.join("runner/_diag/old.log");
        fs::write(&log, b"old").unwrap();
        std::process::Command::new("/usr/bin/touch")
            .args(["-t", "202001010000", log.to_str().unwrap()])
            .status()
            .unwrap();
        let stat = fs::metadata(&log).unwrap();
        let identity = FileIdentity {
            device: stat.dev(),
            inode: stat.ino(),
            links: stat.nlink(),
            size: stat.len(),
            modified_seconds: stat.mtime(),
            changed_seconds: stat.ctime(),
            modified_nanos: stat.mtime_nsec(),
            changed_nanos: stat.ctime_nsec(),
        };
        let receipt = RunnerEnrollmentReceipt {
            runner_dir_owned: true,
            runner_dir: root.join("runner").to_string_lossy().into_owned(),
            install_id: "install".into(),
        };
        (root, receipt, identity)
    }

    #[test]
    fn marker_binding_is_required() {
        let (root, mut receipt, _) = fixture();
        receipt.install_id = "wrong".into();
        let result = RunnerLogStore::open(&root.join("runner"), &receipt, &root.join("state"));
        assert!(matches!(
            result,
            Err(StoreError::Invalid("marker_mismatch"))
        ));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn synthetic_fixture_paths_reject_non_fixture_roots() {
        let (root, _, _) = fixture();
        let runner = root.join("runner");
        let state = root.join("state");
        assert!(matches!(
            validate_synthetic_fixture_paths(&runner, &state),
            Err(StoreError::Invalid("fixture_path_scope"))
        ));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn incomplete_observation_refuses_before_mutation() {
        let (root, receipt, identity) = fixture();
        let mut store =
            RunnerLogStore::open(&root.join("runner"), &receipt, &root.join("state")).unwrap();
        let item = LogItem {
            relative_path: "_diag/old.log".into(),
            identity,
            bytes: identity.size,
            modified_seconds: identity.modified_seconds,
            decision: ItemDecision::ExpiredRunnerDiagnosticLog,
            reason: "expired_retention",
        };
        let result = store.apply_batch(
            &[item],
            Policy {
                minimum_idle_seconds: MIN_IDLE_SECONDS,
                batch_size: 1,
            },
            || Ok(ProcessEvidence::test_incomplete()),
        );
        assert!(matches!(result, Err(StoreError::ObservationIncomplete)));
        assert!(root.join("runner/_diag/old.log").exists());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn fresh_observation_is_called_before_each_candidate() {
        let (root, receipt, identity) = fixture();
        let mut store =
            RunnerLogStore::open(&root.join("runner"), &receipt, &root.join("state")).unwrap();
        let item = LogItem {
            relative_path: "_diag/old.log".into(),
            identity,
            bytes: identity.size,
            modified_seconds: identity.modified_seconds,
            decision: ItemDecision::ExpiredRunnerDiagnosticLog,
            reason: "expired_retention",
        };
        let mut observations = 0;
        let report = store
            .apply_batch(
                &[item],
                Policy {
                    minimum_idle_seconds: MIN_IDLE_SECONDS,
                    batch_size: 1,
                },
                || {
                    observations += 1;
                    Ok(ProcessEvidence::test_complete())
                },
            )
            .unwrap();
        assert_eq!(report.reclaimed, 1);
        assert!(observations >= 2);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn recovery_projects_absent_delete_intent_idempotently() {
        let (root, receipt, identity) = fixture();
        let mut store =
            RunnerLogStore::open(&root.join("runner"), &receipt, &root.join("state")).unwrap();
        let row = JournalRow {
            tx_id: "tx".into(),
            phase: "delete_intent".into(),
            install_id: "install".into(),
            root_device: store.root_device,
            root_inode: store.root_inode,
            root_uid: store.root_uid,
            relative_path: "_diag/old.log".into(),
            stage_name: "_diag/.glaeda-reclaiming-tx".into(),
            identity,
        };
        append_row(&store.state, &row).unwrap();
        let first = store
            .recover(
                Policy {
                    minimum_idle_seconds: MIN_IDLE_SECONDS,
                    batch_size: 1,
                },
                || Ok(ProcessEvidence::test_complete()),
            )
            .unwrap();
        assert_eq!(first.absent_after_delete_intent, 1);
        let second = store
            .recover(
                Policy {
                    minimum_idle_seconds: MIN_IDLE_SECONDS,
                    batch_size: 1,
                },
                || Ok(ProcessEvidence::test_complete()),
            )
            .unwrap();
        assert_eq!(second.pending, 0);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn recovery_reclaims_staged_identity_and_refuses_open_identity() {
        let (root, receipt, _identity) = fixture();
        let staged = root.join("runner/_diag/.glaeda-reclaiming-tx");
        fs::rename(root.join("runner/_diag/old.log"), &staged).unwrap();
        let staged_fd = rfs::open(&staged, READ_FILE_FLAGS, Mode::empty()).unwrap();
        let staged_identity =
            identity_from_stat(&rfs::fstat(&staged_fd).unwrap()).expect("staged identity");
        let mut store =
            RunnerLogStore::open(&root.join("runner"), &receipt, &root.join("state")).unwrap();
        let row = JournalRow {
            tx_id: "tx".into(),
            phase: "staged".into(),
            install_id: "install".into(),
            root_device: store.root_device,
            root_inode: store.root_inode,
            root_uid: store.root_uid,
            relative_path: "_diag/old.log".into(),
            stage_name: "_diag/.glaeda-reclaiming-tx".into(),
            identity: staged_identity,
        };
        append_row(&store.state, &row).unwrap();
        let open = store
            .recover(
                Policy {
                    minimum_idle_seconds: MIN_IDLE_SECONDS,
                    batch_size: 1,
                },
                || Ok(ProcessEvidence::test_open(staged_identity)),
            )
            .unwrap();
        assert_eq!(open.confirmed_deleted, 0);
        assert!(staged.exists());
        let closed = store
            .recover(
                Policy {
                    minimum_idle_seconds: MIN_IDLE_SECONDS,
                    batch_size: 1,
                },
                || Ok(ProcessEvidence::test_complete()),
            )
            .unwrap();
        assert_eq!(closed.confirmed_deleted, 1);
        assert!(!staged.exists());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn recovery_refuses_writable_staged_file() {
        let (root, receipt, _identity) = fixture();
        let staged = root.join("runner/_diag/.glaeda-reclaiming-mode");
        fs::rename(root.join("runner/_diag/old.log"), &staged).unwrap();
        fs::set_permissions(&staged, fs::Permissions::from_mode(0o664)).unwrap();
        let staged_fd = rfs::open(&staged, READ_FILE_FLAGS, Mode::empty()).unwrap();
        let staged_identity =
            identity_from_stat(&rfs::fstat(&staged_fd).unwrap()).expect("staged identity");
        let mut store =
            RunnerLogStore::open(&root.join("runner"), &receipt, &root.join("state")).unwrap();
        append_row(
            &store.state,
            &store.journal_row(
                "mode",
                "staged",
                "_diag/old.log",
                "_diag/.glaeda-reclaiming-mode",
                staged_identity,
            ),
        )
        .unwrap();
        let result = store.recover(
            Policy {
                minimum_idle_seconds: MIN_IDLE_SECONDS,
                batch_size: 1,
            },
            || Ok(ProcessEvidence::test_complete()),
        );
        assert!(matches!(
            result,
            Err(StoreError::Invalid("staged_identity_changed"))
        ));
        assert!(staged.exists());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn actual_unlink_before_projection_converges() {
        let (root, receipt, identity) = fixture();
        let staged = root.join("runner/_diag/.glaeda-reclaiming-tx");
        fs::rename(root.join("runner/_diag/old.log"), &staged).unwrap();
        fs::remove_file(&staged).unwrap();
        let mut store =
            RunnerLogStore::open(&root.join("runner"), &receipt, &root.join("state")).unwrap();
        append_row(
            &store.state,
            &JournalRow {
                tx_id: "tx".into(),
                phase: "delete_intent".into(),
                install_id: "install".into(),
                root_device: store.root_device,
                root_inode: store.root_inode,
                root_uid: store.root_uid,
                relative_path: "_diag/old.log".into(),
                stage_name: "_diag/.glaeda-reclaiming-tx".into(),
                identity,
            },
        )
        .unwrap();
        let first = store
            .recover(
                Policy {
                    minimum_idle_seconds: MIN_IDLE_SECONDS,
                    batch_size: 1,
                },
                || Ok(ProcessEvidence::test_complete()),
            )
            .unwrap();
        assert_eq!(first.absent_after_delete_intent, 1);
        let second = store
            .recover(
                Policy {
                    minimum_idle_seconds: MIN_IDLE_SECONDS,
                    batch_size: 1,
                },
                || Ok(ProcessEvidence::test_complete()),
            )
            .unwrap();
        assert_eq!(second.pending, 0);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn overlapping_apply_lock_is_refused() {
        let (root, receipt, _) = fixture();
        let _first =
            RunnerLogStore::open(&root.join("runner"), &receipt, &root.join("state")).unwrap();
        assert!(matches!(
            RunnerLogStore::open(&root.join("runner"), &receipt, &root.join("state")),
            Err(StoreError::Busy)
        ));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn replacement_after_observation_is_skipped() {
        let (root, receipt, identity) = fixture();
        let mut store =
            RunnerLogStore::open(&root.join("runner"), &receipt, &root.join("state")).unwrap();
        let item = LogItem {
            relative_path: "_diag/old.log".into(),
            identity,
            bytes: identity.size,
            modified_seconds: identity.modified_seconds,
            decision: ItemDecision::ExpiredRunnerDiagnosticLog,
            reason: "expired_retention",
        };
        let path = root.join("runner/_diag/old.log");
        let mut first = true;
        let result = store
            .apply_batch(
                &[item],
                Policy {
                    minimum_idle_seconds: MIN_IDLE_SECONDS,
                    batch_size: 1,
                },
                || {
                    if first {
                        first = false;
                        fs::write(&path, b"replacement").unwrap();
                    }
                    Ok(ProcessEvidence::test_complete())
                },
            )
            .unwrap();
        assert_eq!(result.reclaimed, 0);
        assert_eq!(fs::read(path).unwrap(), b"replacement");
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn prepared_before_rename_remains_pending_without_unlink() {
        let (root, receipt, identity) = fixture();
        let mut store =
            RunnerLogStore::open(&root.join("runner"), &receipt, &root.join("state")).unwrap();
        append_row(
            &store.state,
            &JournalRow {
                tx_id: "prepared".into(),
                phase: "prepared".into(),
                install_id: "install".into(),
                root_device: store.root_device,
                root_inode: store.root_inode,
                root_uid: store.root_uid,
                relative_path: "_diag/old.log".into(),
                stage_name: "_diag/.glaeda-reclaiming-prepared".into(),
                identity,
            },
        )
        .unwrap();
        let report = store
            .recover(
                Policy {
                    minimum_idle_seconds: MIN_IDLE_SECONDS,
                    batch_size: 1,
                },
                || Ok(ProcessEvidence::test_complete()),
            )
            .unwrap();
        assert_eq!(report.pending, 0);
        assert_eq!(report.aborted_prepared, 1);
        assert!(root.join("runner/_diag/old.log").exists());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn hardlink_candidate_is_refused() {
        let (root, receipt, identity) = fixture();
        fs::hard_link(
            root.join("runner/_diag/old.log"),
            root.join("runner/_diag/alias.log"),
        )
        .unwrap();
        let mut store =
            RunnerLogStore::open(&root.join("runner"), &receipt, &root.join("state")).unwrap();
        let item = LogItem {
            relative_path: "_diag/old.log".into(),
            identity,
            bytes: identity.size,
            modified_seconds: identity.modified_seconds,
            decision: ItemDecision::ExpiredRunnerDiagnosticLog,
            reason: "expired_retention",
        };
        let report = store
            .apply_batch(
                &[item],
                Policy {
                    minimum_idle_seconds: MIN_IDLE_SECONDS,
                    batch_size: 1,
                },
                || Ok(ProcessEvidence::test_complete()),
            )
            .unwrap();
        assert_eq!(report.reclaimed, 0);
        assert!(root.join("runner/_diag/old.log").exists());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn root_replacement_during_fresh_observation_refuses() {
        let (root, receipt, identity) = fixture();
        let runner = root.join("runner");
        let mut store = RunnerLogStore::open(&runner, &receipt, &root.join("state")).unwrap();
        let item = LogItem {
            relative_path: "_diag/old.log".into(),
            identity,
            bytes: identity.size,
            modified_seconds: identity.modified_seconds,
            decision: ItemDecision::ExpiredRunnerDiagnosticLog,
            reason: "expired_retention",
        };
        let moved = root.join("runner-moved");
        let mut first = true;
        let result = store.apply_batch(
            &[item],
            Policy {
                minimum_idle_seconds: MIN_IDLE_SECONDS,
                batch_size: 1,
            },
            || {
                if first {
                    first = false;
                    fs::rename(&runner, &moved).unwrap();
                }
                Ok(ProcessEvidence::test_complete())
            },
        );
        assert!(matches!(
            result,
            Err(StoreError::Invalid("root_alias_changed"))
        ));
        fs::rename(moved, runner).unwrap();
        fs::remove_dir_all(root).unwrap();
    }
}
