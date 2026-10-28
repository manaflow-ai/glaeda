//! Descriptor-bound publication audit for immutable Git object-pool candidates.
//!
//! This is the intentionally O(N) companion to the hot #585 ownership observer. It runs at
//! generation publication, never on each task admission. The positive receipt can be minted only
//! from the exact retained #585 source observation plus a sealed retained staging-candidate
//! descriptor. Raw paths remain lower-level test/reference locators and carry no publication
//! authority.
//!
//! The audit proves bounded safe entry types, single-link candidate regular files,
//! source/candidate object-inode disjointness, logical byte counts, and absence of nested Git
//! alternates. It deliberately does not sum `st_blocks` or claim unique physical allocation on
//! reflink-capable filesystems.

use std::collections::BTreeSet;
use std::fmt;
use std::os::fd::{AsFd as _, BorrowedFd, OwnedFd};

use rustix::fs::{self as rustix_fs, AtFlags, Dir, FileType, Mode, OFlags};
use rustix::io::Errno;
use serde::Serialize;
use sha2::{Digest as _, Sha256};

use crate::artifact::Sha256Digest;
use crate::immutable_git_object_pool::GitObjectPoolBinding;
use crate::immutable_git_object_pool_marker::git_object_pool_binding_digest;
use crate::immutable_git_object_pool_observation::ImmutableGitObjectPoolObservation;

pub const IMMUTABLE_GIT_OBJECT_POOL_GENERATION_AUDIT_SCHEMA_VERSION: u8 = 1;
pub const MAX_IMMUTABLE_GIT_OBJECT_POOL_AUDIT_ENTRIES: u64 = 2_000_000;
pub const MAX_IMMUTABLE_GIT_OBJECT_POOL_AUDIT_DEPTH: u16 = 64;

const DIRECTORY_FLAGS: OFlags = OFlags::RDONLY
    .union(OFlags::DIRECTORY)
    .union(OFlags::NOFOLLOW)
    .union(OFlags::CLOEXEC);
const FILE_FLAGS: OFlags = OFlags::RDONLY
    .union(OFlags::NOFOLLOW)
    .union(OFlags::CLOEXEC)
    .union(OFlags::NONBLOCK);
const PHYSICAL_IDENTITY_DOMAIN: &[u8] = b"smolrunner-immutable-git-pool-audit-physical-v1\0";
const REDACTED_CANDIDATE: &str = "<private-retained-git-pool-candidate>";
const REDACTED_TRANSACTION: &str = "<opaque-git-pool-staging-transaction>";

#[derive(Clone, PartialEq, Eq, Hash, Serialize)]
#[serde(transparent)]
pub struct ImmutableGitObjectPoolStagingTransactionIdentity(Sha256Digest);

impl ImmutableGitObjectPoolStagingTransactionIdentity {
    #[allow(dead_code)] // Constructed by the next #592 staging transaction.
    pub(crate) const fn from_digest(digest: Sha256Digest) -> Self {
        Self(digest)
    }

    #[must_use]
    pub const fn digest(&self) -> &Sha256Digest {
        &self.0
    }
}

impl fmt::Debug for ImmutableGitObjectPoolStagingTransactionIdentity {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(REDACTED_TRANSACTION)
    }
}

/// Sealed retained descriptor authority for one staged candidate generation.
///
/// There is no public constructor. The #592 publication transaction will construct this from the
/// exact candidate root descriptor it already owns after the admin producer has exited. Tests use
/// the same descriptor constructor from inside the crate.
pub struct ImmutableGitObjectPoolCandidateAuditLease {
    binding: GitObjectPoolBinding,
    staging_transaction: ImmutableGitObjectPoolStagingTransactionIdentity,
    root: BoundDirectory,
    objects: BoundDirectory,
    binding_digest: Sha256Digest,
    physical_identity: Sha256Digest,
}

impl ImmutableGitObjectPoolCandidateAuditLease {
    #[allow(dead_code)] // Constructed by the next #592 staging transaction.
    pub(crate) fn from_retained_root(
        root: BorrowedFd<'_>,
        binding: GitObjectPoolBinding,
        staging_transaction: ImmutableGitObjectPoolStagingTransactionIdentity,
    ) -> Result<Self, ImmutableGitObjectPoolGenerationAuditError> {
        let root = duplicate_bound_directory(root)?;
        let objects = BoundDirectory::open_child(&root.fd, "objects")?;
        if root.snapshot.device != objects.snapshot.device {
            return Err(unsafe_entry());
        }
        let binding_digest =
            git_object_pool_binding_digest(&binding).map_err(|_| identity_error())?;
        let physical_identity = physical_identity_digest(
            b"candidate",
            &binding_digest,
            &[root.snapshot, objects.snapshot],
        )?;
        let candidate = Self {
            binding,
            staging_transaction,
            root,
            objects,
            binding_digest,
            physical_identity,
        };
        candidate.confirm()?;
        Ok(candidate)
    }

    #[must_use]
    pub const fn binding(&self) -> &GitObjectPoolBinding {
        &self.binding
    }

    #[must_use]
    pub const fn staging_transaction(&self) -> &ImmutableGitObjectPoolStagingTransactionIdentity {
        &self.staging_transaction
    }

    fn confirm(&self) -> Result<(), ImmutableGitObjectPoolGenerationAuditError> {
        self.root.revalidate()?;
        self.objects.revalidate()?;
        let rebound =
            BoundDirectory::open_child(&self.root.fd, "objects").map_err(|_| changed())?;
        if rebound.snapshot != self.objects.snapshot
            || self.root.snapshot.device != self.objects.snapshot.device
        {
            return Err(changed());
        }
        Ok(())
    }
}

impl fmt::Debug for ImmutableGitObjectPoolCandidateAuditLease {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("ImmutableGitObjectPoolCandidateAuditLease")
            .field("binding", &self.binding)
            .field("staging_transaction", &self.staging_transaction)
            .field("descriptors", &REDACTED_CANDIDATE)
            .finish()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ImmutableGitObjectPoolGenerationAuditDisposition {
    InodeIndependentCandidate,
}

/// Bounded publication evidence for one exact retained source generation and one exact staged
/// candidate transaction.
///
/// Counts are logical inventory observations only. Physical input identities are opaque
/// domain-separated digests; raw device/inode/owner/timestamp values are never exposed.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ImmutableGitObjectPoolGenerationAuditReceipt {
    schema_version: u8,
    disposition: ImmutableGitObjectPoolGenerationAuditDisposition,
    source_binding_digest: Sha256Digest,
    candidate_binding_digest: Sha256Digest,
    staging_transaction_identity: Sha256Digest,
    source_physical_identity: Sha256Digest,
    candidate_physical_identity: Sha256Digest,
    source_object_regular_files: u64,
    candidate_directories: u64,
    candidate_regular_files: u64,
    candidate_object_regular_files: u64,
    candidate_logical_bytes: u64,
    inode_independent: bool,
    candidate_single_link_regular_files: bool,
    safe_entry_types: bool,
    nested_alternates_absent: bool,
}

impl ImmutableGitObjectPoolGenerationAuditReceipt {
    #[must_use]
    pub const fn schema_version(&self) -> u8 {
        self.schema_version
    }

    #[must_use]
    pub const fn disposition(&self) -> ImmutableGitObjectPoolGenerationAuditDisposition {
        self.disposition
    }

    #[must_use]
    pub const fn source_binding_digest(&self) -> &Sha256Digest {
        &self.source_binding_digest
    }

    #[must_use]
    pub const fn candidate_binding_digest(&self) -> &Sha256Digest {
        &self.candidate_binding_digest
    }

    #[must_use]
    pub const fn staging_transaction_identity(&self) -> &Sha256Digest {
        &self.staging_transaction_identity
    }

    #[must_use]
    pub const fn source_physical_identity(&self) -> &Sha256Digest {
        &self.source_physical_identity
    }

    #[must_use]
    pub const fn candidate_physical_identity(&self) -> &Sha256Digest {
        &self.candidate_physical_identity
    }

    #[must_use]
    pub const fn source_object_regular_files(&self) -> u64 {
        self.source_object_regular_files
    }

    #[must_use]
    pub const fn candidate_directories(&self) -> u64 {
        self.candidate_directories
    }

    #[must_use]
    pub const fn candidate_regular_files(&self) -> u64 {
        self.candidate_regular_files
    }

    #[must_use]
    pub const fn candidate_object_regular_files(&self) -> u64 {
        self.candidate_object_regular_files
    }

    #[must_use]
    pub const fn candidate_logical_bytes(&self) -> u64 {
        self.candidate_logical_bytes
    }

    #[must_use]
    pub const fn inode_independent(&self) -> bool {
        self.inode_independent
    }

    #[must_use]
    pub const fn candidate_single_link_regular_files(&self) -> bool {
        self.candidate_single_link_regular_files
    }

    #[must_use]
    pub const fn safe_entry_types(&self) -> bool {
        self.safe_entry_types
    }

    #[must_use]
    pub const fn nested_alternates_absent(&self) -> bool {
        self.nested_alternates_absent
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ImmutableGitObjectPoolGenerationAuditErrorKind {
    Missing,
    UnsafeEntry,
    TooLarge,
    SharedObjectInode,
    CandidateHardlink,
    NestedAlternates,
    SourceChanged,
    CandidateChanged,
    IdentityFailure,
    Io,
}

#[derive(Clone, PartialEq, Eq, Serialize)]
pub struct ImmutableGitObjectPoolGenerationAuditError {
    kind: ImmutableGitObjectPoolGenerationAuditErrorKind,
    code: &'static str,
    message: &'static str,
}

impl ImmutableGitObjectPoolGenerationAuditError {
    #[must_use]
    pub const fn kind(&self) -> ImmutableGitObjectPoolGenerationAuditErrorKind {
        self.kind
    }

    #[must_use]
    pub const fn code(&self) -> &'static str {
        self.code
    }
}

impl fmt::Debug for ImmutableGitObjectPoolGenerationAuditError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("ImmutableGitObjectPoolGenerationAuditError")
            .field("kind", &self.kind)
            .field("code", &self.code)
            .field("message", &self.message)
            .finish()
    }
}

impl fmt::Display for ImmutableGitObjectPoolGenerationAuditError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.message)
    }
}

impl std::error::Error for ImmutableGitObjectPoolGenerationAuditError {}

/// Audit one staged bare Git generation against the exact retained #585 source generation.
///
/// The source comes from the already accepted `ImmutableGitObjectPoolObservation`; the candidate
/// comes from the sealed publication-transaction descriptor lease. No caller-selected path can mint
/// this positive receipt. The source and candidate are confirmed before the walk and both are
/// confirmed again at the final boundary after all O(N) inspection.
///
/// # Errors
///
/// Fails closed on source/candidate drift, unsafe entries, hardlinks, shared object inodes, nested
/// alternates, excessive inventory/depth, identity encoding failure, or I/O failure.
pub fn audit_immutable_git_object_pool_generation_candidate(
    source: &mut ImmutableGitObjectPoolObservation,
    candidate: &ImmutableGitObjectPoolCandidateAuditLease,
) -> Result<ImmutableGitObjectPoolGenerationAuditReceipt, ImmutableGitObjectPoolGenerationAuditError>
{
    audit_with_hook(source, candidate, || {})
}

fn audit_with_hook<F>(
    source: &mut ImmutableGitObjectPoolObservation,
    candidate: &ImmutableGitObjectPoolCandidateAuditLease,
    before_final_revalidation: F,
) -> Result<ImmutableGitObjectPoolGenerationAuditReceipt, ImmutableGitObjectPoolGenerationAuditError>
where
    F: FnOnce(),
{
    let source_binding = source.binding().clone();
    source
        .confirm(&source_binding)
        .map_err(|_| source_changed())?;
    candidate.confirm().map_err(|_| candidate_changed())?;

    let source_objects = duplicate_bound_directory(source.retained_objects_descriptor())
        .map_err(map_source_walk_error)?;
    let source_binding_digest =
        git_object_pool_binding_digest(&source_binding).map_err(|_| identity_error())?;
    let source_physical_identity = physical_identity_digest(
        b"source",
        &source_binding_digest,
        &[source_objects.snapshot],
    )?;

    if source_objects.snapshot.device == candidate.objects.snapshot.device
        && source_objects.snapshot.inode == candidate.objects.snapshot.inode
    {
        return Err(shared_inode());
    }

    let source_audit =
        audit_tree(&source_objects.fd, false, true).map_err(map_source_walk_error)?;
    source_objects.revalidate().map_err(|_| source_changed())?;

    let candidate_audit = audit_tree(&candidate.root.fd, true, false)?;
    candidate.root.revalidate()?;

    let candidate_objects_audit = audit_tree(&candidate.objects.fd, true, true)?;
    require_nested_alternates_absent(&candidate.objects)?;
    candidate.objects.revalidate()?;
    candidate.confirm().map_err(|_| candidate_changed())?;

    if source_audit.regular_file_inodes.iter().any(|identity| {
        candidate_objects_audit
            .regular_file_inodes
            .contains(identity)
    }) {
        return Err(shared_inode());
    }
    if candidate_audit.has_multiple_links || candidate_objects_audit.has_multiple_links {
        return Err(candidate_hardlink());
    }

    before_final_revalidation();
    candidate.confirm().map_err(|_| candidate_changed())?;
    source
        .confirm(&source_binding)
        .map_err(|_| source_changed())?;

    Ok(ImmutableGitObjectPoolGenerationAuditReceipt {
        schema_version: IMMUTABLE_GIT_OBJECT_POOL_GENERATION_AUDIT_SCHEMA_VERSION,
        disposition: ImmutableGitObjectPoolGenerationAuditDisposition::InodeIndependentCandidate,
        source_binding_digest,
        candidate_binding_digest: candidate.binding_digest.clone(),
        staging_transaction_identity: candidate.staging_transaction.0.clone(),
        source_physical_identity,
        candidate_physical_identity: candidate.physical_identity.clone(),
        source_object_regular_files: source_audit.regular_files,
        candidate_directories: candidate_audit.directories,
        candidate_regular_files: candidate_audit.regular_files,
        candidate_object_regular_files: candidate_objects_audit.regular_files,
        candidate_logical_bytes: candidate_audit.logical_bytes,
        inode_independent: true,
        candidate_single_link_regular_files: true,
        safe_entry_types: true,
        nested_alternates_absent: true,
    })
}

fn duplicate_bound_directory(
    descriptor: BorrowedFd<'_>,
) -> Result<BoundDirectory, ImmutableGitObjectPoolGenerationAuditError> {
    let held = rustix_fs::fstat(descriptor).map_err(|_| io_error())?;
    let held = snapshot_directory_stat(&held)?;
    // #609 deliberately retains O_PATH authority. Reopen `.` through that exact descriptor to
    // obtain a readable directory handle, then require exact identity before enumeration.
    let fd =
        rustix_fs::openat(descriptor, ".", DIRECTORY_FLAGS, Mode::empty()).map_err(map_open)?;
    let snapshot = snapshot_directory(&fd)?;
    if snapshot != held {
        return Err(changed());
    }
    Ok(BoundDirectory { fd, snapshot })
}

fn map_source_walk_error(
    error: ImmutableGitObjectPoolGenerationAuditError,
) -> ImmutableGitObjectPoolGenerationAuditError {
    if error.kind == ImmutableGitObjectPoolGenerationAuditErrorKind::CandidateChanged {
        source_changed()
    } else {
        error
    }
}

fn physical_identity_digest(
    label: &[u8],
    binding_digest: &Sha256Digest,
    directories: &[DirectorySnapshot],
) -> Result<Sha256Digest, ImmutableGitObjectPoolGenerationAuditError> {
    let mut hasher = Sha256::new();
    hasher.update(PHYSICAL_IDENTITY_DOMAIN);
    hasher.update((label.len() as u64).to_be_bytes());
    hasher.update(label);
    hasher.update(binding_digest.as_str().as_bytes());
    for directory in directories {
        hasher.update(directory.device.to_be_bytes());
        hasher.update(directory.inode.to_be_bytes());
        hasher.update(directory.uid.to_be_bytes());
        hasher.update(directory.gid.to_be_bytes());
        hasher.update(directory.mode.to_be_bytes());
        hasher.update(directory.mtime.to_be_bytes());
        hasher.update(directory.mtime_nsec.to_be_bytes());
        hasher.update(directory.ctime.to_be_bytes());
        hasher.update(directory.ctime_nsec.to_be_bytes());
    }
    Sha256Digest::parse(&format!("sha256:{:x}", hasher.finalize())).map_err(|_| identity_error())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
struct PhysicalFileIdentity {
    device: u64,
    inode: u64,
}

#[derive(Default)]
struct TreeAudit {
    directories: u64,
    regular_files: u64,
    logical_bytes: u64,
    has_multiple_links: bool,
    regular_file_inodes: BTreeSet<PhysicalFileIdentity>,
}

fn audit_tree(
    root: &OwnedFd,
    require_single_links: bool,
    collect_inodes: bool,
) -> Result<TreeAudit, ImmutableGitObjectPoolGenerationAuditError> {
    let mut audit = TreeAudit {
        directories: 1,
        ..TreeAudit::default()
    };
    let mut entries = 0_u64;
    audit_directory(
        root,
        0,
        require_single_links,
        collect_inodes,
        &mut entries,
        &mut audit,
    )?;
    Ok(audit)
}

fn audit_directory(
    directory: &OwnedFd,
    depth: u16,
    require_single_links: bool,
    collect_inodes: bool,
    entries_seen: &mut u64,
    audit: &mut TreeAudit,
) -> Result<(), ImmutableGitObjectPoolGenerationAuditError> {
    if depth > MAX_IMMUTABLE_GIT_OBJECT_POOL_AUDIT_DEPTH {
        return Err(too_large());
    }
    let before = snapshot_directory(directory)?;
    let mut entries = Dir::read_from(directory).map_err(|_| io_error())?;
    for entry in &mut entries {
        let entry = entry.map_err(|_| io_error())?;
        let name = entry.file_name();
        let bytes = name.to_bytes();
        if bytes == b"." || bytes == b".." {
            continue;
        }
        *entries_seen = entries_seen.checked_add(1).ok_or_else(too_large)?;
        if *entries_seen > MAX_IMMUTABLE_GIT_OBJECT_POOL_AUDIT_ENTRIES {
            return Err(too_large());
        }

        let observed = rustix_fs::statat(directory.as_fd(), name, AtFlags::SYMLINK_NOFOLLOW)
            .map_err(map_stat)?;
        let file_type = FileType::from_raw_mode(observed.st_mode);
        if file_type.is_dir() {
            let child = BoundDirectory::open_child(directory, name)?;
            if !same_directory_identity(&observed, &child.snapshot) {
                return Err(changed());
            }
            audit.directories = audit.directories.checked_add(1).ok_or_else(too_large)?;
            audit_directory(
                &child.fd,
                depth.checked_add(1).ok_or_else(too_large)?,
                require_single_links,
                collect_inodes,
                entries_seen,
                audit,
            )?;
            child.revalidate()?;
            let rebound = BoundDirectory::open_child(directory, name)?;
            if rebound.snapshot != child.snapshot {
                return Err(changed());
            }
            continue;
        }
        if !file_type.is_file() {
            return Err(unsafe_entry());
        }

        let file = rustix_fs::openat(directory.as_fd(), name, FILE_FLAGS, Mode::empty())
            .map_err(map_open)?;
        let file_before = snapshot_file(&file)?;
        let path_after = rustix_fs::statat(directory.as_fd(), name, AtFlags::SYMLINK_NOFOLLOW)
            .map_err(map_stat)?;
        let path_after = snapshot_file_stat(&path_after)?;
        let file_after = snapshot_file(&file)?;
        if file_before != file_after || file_before != path_after {
            return Err(changed());
        }

        audit.regular_files = audit.regular_files.checked_add(1).ok_or_else(too_large)?;
        audit.logical_bytes = audit
            .logical_bytes
            .checked_add(file_before.size)
            .ok_or_else(too_large)?;
        if require_single_links && file_before.nlink != 1 {
            audit.has_multiple_links = true;
        }
        if collect_inodes {
            audit.regular_file_inodes.insert(PhysicalFileIdentity {
                device: file_before.device,
                inode: file_before.inode,
            });
        }
    }

    let after = snapshot_directory(directory)?;
    if before != after {
        return Err(changed());
    }
    Ok(())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct DirectorySnapshot {
    device: u64,
    inode: u64,
    uid: u32,
    gid: u32,
    mode: u32,
    mtime: i64,
    mtime_nsec: i64,
    ctime: i64,
    ctime_nsec: i64,
}

struct BoundDirectory {
    fd: OwnedFd,
    snapshot: DirectorySnapshot,
}

impl BoundDirectory {
    fn open_child(
        parent: &OwnedFd,
        name: impl rustix::path::Arg,
    ) -> Result<Self, ImmutableGitObjectPoolGenerationAuditError> {
        let fd = rustix_fs::openat(parent.as_fd(), name, DIRECTORY_FLAGS, Mode::empty())
            .map_err(map_open)?;
        let snapshot = snapshot_directory(&fd)?;
        Ok(Self { fd, snapshot })
    }

    fn revalidate(&self) -> Result<(), ImmutableGitObjectPoolGenerationAuditError> {
        if snapshot_directory(&self.fd).map_err(|_| changed())? != self.snapshot {
            return Err(changed());
        }
        Ok(())
    }
}

fn snapshot_directory(
    descriptor: &OwnedFd,
) -> Result<DirectorySnapshot, ImmutableGitObjectPoolGenerationAuditError> {
    let stat = rustix_fs::fstat(descriptor).map_err(|_| io_error())?;
    snapshot_directory_stat(&stat)
}

fn snapshot_directory_stat(
    stat: &rustix_fs::Stat,
) -> Result<DirectorySnapshot, ImmutableGitObjectPoolGenerationAuditError> {
    if !FileType::from_raw_mode(stat.st_mode).is_dir() {
        return Err(unsafe_entry());
    }
    Ok(DirectorySnapshot {
        device: stat.st_dev,
        inode: stat.st_ino,
        uid: stat.st_uid,
        gid: stat.st_gid,
        mode: stat.st_mode,
        mtime: stat.st_mtime,
        mtime_nsec: i64::try_from(stat.st_mtime_nsec).map_err(|_| unsafe_entry())?,
        ctime: stat.st_ctime,
        ctime_nsec: i64::try_from(stat.st_ctime_nsec).map_err(|_| unsafe_entry())?,
    })
}

fn same_directory_identity(stat: &rustix_fs::Stat, snapshot: &DirectorySnapshot) -> bool {
    FileType::from_raw_mode(stat.st_mode).is_dir()
        && stat.st_dev == snapshot.device
        && stat.st_ino == snapshot.inode
        && stat.st_uid == snapshot.uid
        && stat.st_gid == snapshot.gid
        && stat.st_mode == snapshot.mode
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct FileSnapshot {
    device: u64,
    inode: u64,
    nlink: u128,
    size: u64,
    mode: u32,
    uid: u32,
    gid: u32,
    mtime: i64,
    mtime_nsec: i64,
    ctime: i64,
    ctime_nsec: i64,
}

fn snapshot_file(
    descriptor: &OwnedFd,
) -> Result<FileSnapshot, ImmutableGitObjectPoolGenerationAuditError> {
    let stat = rustix_fs::fstat(descriptor).map_err(|_| io_error())?;
    snapshot_file_stat(&stat)
}

fn snapshot_file_stat(
    stat: &rustix_fs::Stat,
) -> Result<FileSnapshot, ImmutableGitObjectPoolGenerationAuditError> {
    if !FileType::from_raw_mode(stat.st_mode).is_file() {
        return Err(unsafe_entry());
    }
    Ok(FileSnapshot {
        device: stat.st_dev,
        inode: stat.st_ino,
        nlink: u128::from(stat.st_nlink),
        size: u64::try_from(stat.st_size).map_err(|_| unsafe_entry())?,
        mode: stat.st_mode,
        uid: stat.st_uid,
        gid: stat.st_gid,
        mtime: stat.st_mtime,
        mtime_nsec: i64::try_from(stat.st_mtime_nsec).map_err(|_| unsafe_entry())?,
        ctime: stat.st_ctime,
        ctime_nsec: i64::try_from(stat.st_ctime_nsec).map_err(|_| unsafe_entry())?,
    })
}

fn require_nested_alternates_absent(
    objects: &BoundDirectory,
) -> Result<(), ImmutableGitObjectPoolGenerationAuditError> {
    let before = snapshot_directory(&objects.fd)?;
    let info = match rustix_fs::openat(objects.fd.as_fd(), "info", DIRECTORY_FLAGS, Mode::empty()) {
        Ok(fd) => Some(BoundDirectory {
            snapshot: snapshot_directory(&fd)?,
            fd,
        }),
        Err(Errno::NOENT) => None,
        Err(error) => return Err(map_open(error)),
    };
    if let Some(info) = info {
        match rustix_fs::statat(info.fd.as_fd(), "alternates", AtFlags::SYMLINK_NOFOLLOW) {
            Err(Errno::NOENT) => {}
            Ok(_) => return Err(nested_alternates()),
            Err(_) => return Err(io_error()),
        }
        info.revalidate()?;
        let rebound = BoundDirectory::open_child(&objects.fd, "info")?;
        if rebound.snapshot != info.snapshot {
            return Err(changed());
        }
    } else {
        match rustix_fs::statat(objects.fd.as_fd(), "info", AtFlags::SYMLINK_NOFOLLOW) {
            Err(Errno::NOENT) => {}
            _ => return Err(changed()),
        }
    }
    if snapshot_directory(&objects.fd)? != before {
        return Err(changed());
    }
    Ok(())
}

fn map_open(error: Errno) -> ImmutableGitObjectPoolGenerationAuditError {
    match error {
        Errno::NOENT => missing(),
        Errno::LOOP | Errno::NOTDIR => unsafe_entry(),
        _ => io_error(),
    }
}

fn map_stat(error: Errno) -> ImmutableGitObjectPoolGenerationAuditError {
    match error {
        Errno::NOENT => changed(),
        Errno::LOOP | Errno::NOTDIR => unsafe_entry(),
        _ => io_error(),
    }
}

const fn error(
    kind: ImmutableGitObjectPoolGenerationAuditErrorKind,
    code: &'static str,
    message: &'static str,
) -> ImmutableGitObjectPoolGenerationAuditError {
    ImmutableGitObjectPoolGenerationAuditError {
        kind,
        code,
        message,
    }
}

const fn missing() -> ImmutableGitObjectPoolGenerationAuditError {
    error(
        ImmutableGitObjectPoolGenerationAuditErrorKind::Missing,
        "immutable_git_object_pool_audit_missing",
        "immutable Git object-pool audit input is missing",
    )
}

const fn unsafe_entry() -> ImmutableGitObjectPoolGenerationAuditError {
    error(
        ImmutableGitObjectPoolGenerationAuditErrorKind::UnsafeEntry,
        "immutable_git_object_pool_audit_unsafe_entry",
        "immutable Git object-pool audit found an unsafe entry",
    )
}

const fn too_large() -> ImmutableGitObjectPoolGenerationAuditError {
    error(
        ImmutableGitObjectPoolGenerationAuditErrorKind::TooLarge,
        "immutable_git_object_pool_audit_too_large",
        "immutable Git object-pool audit exceeded its bounded inventory",
    )
}

const fn shared_inode() -> ImmutableGitObjectPoolGenerationAuditError {
    error(
        ImmutableGitObjectPoolGenerationAuditErrorKind::SharedObjectInode,
        "immutable_git_object_pool_audit_shared_inode",
        "immutable Git object-pool candidate shares object identity with its producer",
    )
}

const fn candidate_hardlink() -> ImmutableGitObjectPoolGenerationAuditError {
    error(
        ImmutableGitObjectPoolGenerationAuditErrorKind::CandidateHardlink,
        "immutable_git_object_pool_audit_candidate_hardlink",
        "immutable Git object-pool candidate contains a multiply linked regular file",
    )
}

const fn nested_alternates() -> ImmutableGitObjectPoolGenerationAuditError {
    error(
        ImmutableGitObjectPoolGenerationAuditErrorKind::NestedAlternates,
        "immutable_git_object_pool_audit_nested_alternates",
        "immutable Git object-pool candidate contains a nested alternates entry",
    )
}

const fn source_changed() -> ImmutableGitObjectPoolGenerationAuditError {
    error(
        ImmutableGitObjectPoolGenerationAuditErrorKind::SourceChanged,
        "immutable_git_object_pool_audit_source_changed",
        "immutable Git object-pool source generation changed during audit",
    )
}

const fn candidate_changed() -> ImmutableGitObjectPoolGenerationAuditError {
    error(
        ImmutableGitObjectPoolGenerationAuditErrorKind::CandidateChanged,
        "immutable_git_object_pool_audit_candidate_changed",
        "immutable Git object-pool staged candidate changed during audit",
    )
}

const fn identity_error() -> ImmutableGitObjectPoolGenerationAuditError {
    error(
        ImmutableGitObjectPoolGenerationAuditErrorKind::IdentityFailure,
        "immutable_git_object_pool_audit_identity_failed",
        "immutable Git object-pool audit identity could not be encoded",
    )
}

const fn changed() -> ImmutableGitObjectPoolGenerationAuditError {
    candidate_changed()
}

const fn io_error() -> ImmutableGitObjectPoolGenerationAuditError {
    error(
        ImmutableGitObjectPoolGenerationAuditErrorKind::Io,
        "immutable_git_object_pool_audit_io",
        "immutable Git object-pool generation audit failed",
    )
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::os::fd::AsFd as _;
    use std::os::unix::fs::{MetadataExt as _, PermissionsExt as _, symlink};
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, Ordering};

    use rustix::fs::{self as rustix_fs, Mode};

    use super::{
        DIRECTORY_FLAGS, ImmutableGitObjectPoolCandidateAuditLease,
        ImmutableGitObjectPoolGenerationAuditDisposition,
        ImmutableGitObjectPoolGenerationAuditErrorKind,
        ImmutableGitObjectPoolStagingTransactionIdentity,
        audit_immutable_git_object_pool_generation_candidate, audit_with_hook,
    };
    use crate::artifact::Sha256Digest;
    use crate::immutable_git_object_pool::{
        GitObjectFormat, GitObjectPoolBinding, GitObjectPoolGeneration, GitObjectPoolId,
        GitObjectPoolProducerGenerationId, GitObjectPoolTrustGenerationId,
    };
    use crate::immutable_git_object_pool_marker::{
        GitObjectPoolMarkerNonce, ImmutableGitObjectPoolMarker, git_object_pool_binding_digest,
    };
    use crate::immutable_git_object_pool_observation::{
        IMMUTABLE_GIT_OBJECT_POOL_MARKER_FILE_NAME,
        observe_immutable_git_object_pool_generation_for_test,
    };
    use crate::project_catalog::ProjectIdentity;
    use crate::project_disk_lease::{ProjectDiskGeneration, ProjectDiskId};

    static NEXT_TEMP: AtomicU64 = AtomicU64::new(1);

    fn binding(generation: u64) -> GitObjectPoolBinding {
        GitObjectPoolBinding::new(
            ProjectIdentity::parse("github.com/teamleaderleo/smolrunner").unwrap(),
            GitObjectPoolId::parse("pool-a").unwrap(),
            GitObjectPoolGeneration::new(generation).unwrap(),
            ProjectDiskId::parse("disk-a").unwrap(),
            ProjectDiskGeneration::new(1).unwrap(),
            GitObjectFormat::Sha1,
            GitObjectPoolProducerGenerationId::parse("producer-a").unwrap(),
            GitObjectPoolTrustGenerationId::parse("trust-a").unwrap(),
        )
    }

    fn transaction_identity() -> ImmutableGitObjectPoolStagingTransactionIdentity {
        ImmutableGitObjectPoolStagingTransactionIdentity::from_digest(
            Sha256Digest::parse(
                "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            )
            .unwrap(),
        )
    }

    struct Fixture {
        base: PathBuf,
        source_parent: PathBuf,
        source_pool: PathBuf,
        source_objects: PathBuf,
        source_object: PathBuf,
        candidate: PathBuf,
        candidate_object: PathBuf,
        owner: (u32, u32),
        source_binding: GitObjectPoolBinding,
        candidate_binding: GitObjectPoolBinding,
    }

    impl Fixture {
        fn new() -> Self {
            let unique = NEXT_TEMP.fetch_add(1, Ordering::Relaxed);
            let base = std::env::temp_dir().join(format!(
                "smolrunner-bound-git-pool-audit-{}-{unique}",
                std::process::id()
            ));
            let source_parent = base.join("published");
            let source_pool = source_parent.join("generation");
            let source_objects = source_pool.join("objects");
            let source_info = source_objects.join("info");
            let source_object = source_objects.join("aa/source-object");
            let candidate = base.join("staging/candidate.git");
            let candidate_object = candidate.join("objects/bb/candidate-object");
            fs::create_dir_all(source_object.parent().unwrap()).unwrap();
            fs::create_dir_all(&source_info).unwrap();
            fs::create_dir_all(candidate_object.parent().unwrap()).unwrap();
            fs::create_dir_all(candidate.join("objects/info")).unwrap();
            fs::write(&source_object, b"producer object bytes").unwrap();
            fs::write(&candidate_object, b"independent candidate object bytes").unwrap();
            fs::write(candidate.join("HEAD"), b"ref: refs/heads/main\n").unwrap();

            let source_binding = binding(1);
            let candidate_binding = binding(2);
            let marker = source_pool.join(IMMUTABLE_GIT_OBJECT_POOL_MARKER_FILE_NAME);
            let marker_bytes = ImmutableGitObjectPoolMarker::new(
                &source_binding,
                GitObjectPoolMarkerNonce::new([7; 16]).unwrap(),
            )
            .unwrap()
            .encode()
            .unwrap();
            fs::write(&marker, marker_bytes).unwrap();

            let metadata = fs::metadata(&source_parent).unwrap();
            let owner = (metadata.uid(), metadata.gid());
            fs::set_permissions(&source_parent, fs::Permissions::from_mode(0o700)).unwrap();
            fs::set_permissions(&source_pool, fs::Permissions::from_mode(0o555)).unwrap();
            fs::set_permissions(&source_objects, fs::Permissions::from_mode(0o555)).unwrap();
            fs::set_permissions(&source_info, fs::Permissions::from_mode(0o555)).unwrap();
            fs::set_permissions(&marker, fs::Permissions::from_mode(0o444)).unwrap();

            Self {
                base,
                source_parent,
                source_pool,
                source_objects,
                source_object,
                candidate,
                candidate_object,
                owner,
                source_binding,
                candidate_binding,
            }
        }

        fn source_observation(
            &self,
        ) -> crate::immutable_git_object_pool_observation::ImmutableGitObjectPoolObservation
        {
            observe_immutable_git_object_pool_generation_for_test(
                &self.source_pool,
                &self.source_binding,
                self.owner,
            )
            .unwrap()
        }

        fn candidate_lease(&self) -> ImmutableGitObjectPoolCandidateAuditLease {
            let root = rustix_fs::open(&self.candidate, DIRECTORY_FLAGS, Mode::empty()).unwrap();
            ImmutableGitObjectPoolCandidateAuditLease::from_retained_root(
                root.as_fd(),
                self.candidate_binding.clone(),
                transaction_identity(),
            )
            .unwrap()
        }

        fn thaw_source(&self) {
            for path in [&self.source_pool, &self.source_objects] {
                if path.is_dir() {
                    let _ = fs::set_permissions(path, fs::Permissions::from_mode(0o755));
                }
            }
            let info = self.source_objects.join("info");
            if info.is_dir() {
                let _ = fs::set_permissions(info, fs::Permissions::from_mode(0o755));
            }
            let marker = self
                .source_pool
                .join(IMMUTABLE_GIT_OBJECT_POOL_MARKER_FILE_NAME);
            if marker.is_file() {
                let _ = fs::set_permissions(marker, fs::Permissions::from_mode(0o644));
            }
            if self.source_parent.is_dir() {
                let _ = fs::set_permissions(&self.source_parent, fs::Permissions::from_mode(0o755));
            }
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            self.thaw_source();
            let old = self.source_parent.join("generation-old");
            if old.is_dir() {
                let _ = fs::set_permissions(
                    old.join("objects/info"),
                    fs::Permissions::from_mode(0o755),
                );
                let _ = fs::set_permissions(old.join("objects"), fs::Permissions::from_mode(0o755));
                let _ = fs::set_permissions(&old, fs::Permissions::from_mode(0o755));
            }
            let _ = fs::remove_dir_all(&self.base);
        }
    }

    #[test]
    fn retained_source_and_candidate_mint_bound_receipt() {
        let fixture = Fixture::new();
        let mut source = fixture.source_observation();
        let candidate = fixture.candidate_lease();
        let receipt =
            audit_immutable_git_object_pool_generation_candidate(&mut source, &candidate).unwrap();
        assert_eq!(
            receipt.disposition(),
            ImmutableGitObjectPoolGenerationAuditDisposition::InodeIndependentCandidate
        );
        assert_eq!(
            receipt.source_binding_digest(),
            &git_object_pool_binding_digest(&fixture.source_binding).unwrap()
        );
        assert_eq!(
            receipt.candidate_binding_digest(),
            &git_object_pool_binding_digest(&fixture.candidate_binding).unwrap()
        );
        assert_eq!(
            receipt.staging_transaction_identity(),
            transaction_identity().digest()
        );
        assert_eq!(receipt.source_object_regular_files(), 1);
        assert_eq!(receipt.candidate_object_regular_files(), 1);
        assert!(receipt.inode_independent());
        assert!(receipt.candidate_single_link_regular_files());
        assert!(receipt.safe_entry_types());
        assert!(receipt.nested_alternates_absent());
        assert!(receipt.candidate_logical_bytes() > 0);
        assert_ne!(
            receipt.source_physical_identity(),
            receipt.candidate_physical_identity()
        );
    }

    #[test]
    fn retained_leases_can_be_audited_repeatedly_without_consuming_directory_state() {
        let fixture = Fixture::new();
        let mut source = fixture.source_observation();
        let candidate = fixture.candidate_lease();
        let first =
            audit_immutable_git_object_pool_generation_candidate(&mut source, &candidate).unwrap();
        let second =
            audit_immutable_git_object_pool_generation_candidate(&mut source, &candidate).unwrap();
        assert_eq!(first, second);
    }

    #[test]
    fn producer_object_hardlink_is_refused_as_shared_inode() {
        let fixture = Fixture::new();
        fs::remove_file(&fixture.candidate_object).unwrap();
        fs::hard_link(&fixture.source_object, &fixture.candidate_object).unwrap();
        let mut source = fixture.source_observation();
        let candidate = fixture.candidate_lease();
        assert_eq!(
            audit_immutable_git_object_pool_generation_candidate(&mut source, &candidate)
                .unwrap_err()
                .kind(),
            ImmutableGitObjectPoolGenerationAuditErrorKind::SharedObjectInode
        );
    }

    #[test]
    fn external_candidate_hardlink_is_refused() {
        let fixture = Fixture::new();
        let outside = fixture.base.join("outside-link");
        fs::hard_link(&fixture.candidate_object, &outside).unwrap();
        let mut source = fixture.source_observation();
        let candidate = fixture.candidate_lease();
        assert_eq!(
            audit_immutable_git_object_pool_generation_candidate(&mut source, &candidate)
                .unwrap_err()
                .kind(),
            ImmutableGitObjectPoolGenerationAuditErrorKind::CandidateHardlink
        );
    }

    #[test]
    fn symlink_entry_is_refused() {
        let fixture = Fixture::new();
        symlink("HEAD", fixture.candidate.join("alias")).unwrap();
        let mut source = fixture.source_observation();
        let candidate = fixture.candidate_lease();
        assert_eq!(
            audit_immutable_git_object_pool_generation_candidate(&mut source, &candidate)
                .unwrap_err()
                .kind(),
            ImmutableGitObjectPoolGenerationAuditErrorKind::UnsafeEntry
        );
    }

    #[test]
    fn nested_alternates_are_refused() {
        let fixture = Fixture::new();
        fs::write(
            fixture.candidate.join("objects/info/alternates"),
            b"/unexpected/objects\n",
        )
        .unwrap();
        let mut source = fixture.source_observation();
        let candidate = fixture.candidate_lease();
        assert_eq!(
            audit_immutable_git_object_pool_generation_candidate(&mut source, &candidate)
                .unwrap_err()
                .kind(),
            ImmutableGitObjectPoolGenerationAuditErrorKind::NestedAlternates
        );
    }

    #[test]
    fn source_generation_replacement_before_final_boundary_is_refused() {
        let fixture = Fixture::new();
        let mut source = fixture.source_observation();
        let candidate = fixture.candidate_lease();
        let old = fixture.source_parent.join("generation-old");
        let result = audit_with_hook(&mut source, &candidate, || {
            fs::rename(&fixture.source_pool, &old).unwrap();
        });
        assert_eq!(
            result.unwrap_err().kind(),
            ImmutableGitObjectPoolGenerationAuditErrorKind::SourceChanged
        );
    }

    #[test]
    fn candidate_drift_before_final_boundary_is_refused() {
        let fixture = Fixture::new();
        // The fixture makes the candidate and the hook writes into it within one kernel tick, and Linux stamps
        // directory times from that coarse clock: the write could leave mtime and ctime as they were. Give the
        // candidate an older mtime first, so the late write moves it as any write a tick later would.
        fs::File::open(&fixture.candidate)
            .unwrap()
            .set_times(fs::FileTimes::new().set_modified(
                std::time::SystemTime::UNIX_EPOCH + std::time::Duration::from_secs(1_000_000_000),
            ))
            .unwrap();
        let mut source = fixture.source_observation();
        let candidate = fixture.candidate_lease();
        let result = audit_with_hook(&mut source, &candidate, || {
            fs::write(fixture.candidate.join("late"), b"drift").unwrap();
        });
        assert_eq!(
            result.unwrap_err().kind(),
            ImmutableGitObjectPoolGenerationAuditErrorKind::CandidateChanged
        );
    }
}
