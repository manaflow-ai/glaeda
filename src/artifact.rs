use std::fmt;

use serde::{Deserialize, Serialize};

const SHA1_HEX_LEN: usize = 40;
const SHA256_HEX_LEN: usize = 64;
const SHA256_PREFIX: &str = "sha256:";

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct RepositoryRef(String);

impl RepositoryRef {
    /// Validate one GitHub-style `owner/name` repository reference.
    ///
    /// # Errors
    ///
    /// Returns an error unless the value contains exactly two bounded ASCII components separated by
    /// one slash.
    pub fn parse(value: &str) -> Result<Self, ArtifactIdentityError> {
        let mut components = value.split('/');
        let owner = components.next().unwrap_or_default();
        let name = components.next().unwrap_or_default();
        if components.next().is_some()
            || !valid_repository_component(owner)
            || !valid_repository_component(name)
        {
            return Err(ArtifactIdentityError::new(
                "repository",
                "must be an owner/name pair using bounded ASCII letters, digits, '.', '_', or '-'",
            ));
        }
        Ok(Self(value.to_owned()))
    }

    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct CommitId(String);

impl CommitId {
    /// Validate an immutable Git object identifier.
    ///
    /// Both 40-character SHA-1 and 64-character SHA-256 object identifiers are accepted so the
    /// contract survives Git's hash transition.
    ///
    /// # Errors
    ///
    /// Returns an error for abbreviated, uppercase, or non-hexadecimal values.
    pub fn parse(value: &str) -> Result<Self, ArtifactIdentityError> {
        if !matches!(value.len(), SHA1_HEX_LEN | SHA256_HEX_LEN) || !value.bytes().all(is_lower_hex)
        {
            return Err(ArtifactIdentityError::new(
                "commit",
                "must be a complete 40- or 64-character lowercase hexadecimal Git object ID",
            ));
        }
        Ok(Self(value.to_owned()))
    }

    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct GitTreeId(String);

impl GitTreeId {
    /// Validate an immutable Git tree object identifier.
    ///
    /// Both 40-character SHA-1 and 64-character SHA-256 object identifiers are accepted so the
    /// contract survives Git's hash transition.
    ///
    /// # Errors
    ///
    /// Returns an error for abbreviated, uppercase, or non-hexadecimal values.
    pub fn parse(value: &str) -> Result<Self, ArtifactIdentityError> {
        if !matches!(value.len(), SHA1_HEX_LEN | SHA256_HEX_LEN) || !value.bytes().all(is_lower_hex)
        {
            return Err(ArtifactIdentityError::new(
                "tree",
                "must be a complete 40- or 64-character lowercase hexadecimal Git object ID",
            ));
        }
        Ok(Self(value.to_owned()))
    }

    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct Sha256Digest(String);

impl Sha256Digest {
    /// Validate a content digest in canonical `sha256:<hex>` form.
    ///
    /// # Errors
    ///
    /// Returns an error for another algorithm, uppercase hexadecimal, or an incorrect length.
    pub fn parse(value: &str) -> Result<Self, ArtifactIdentityError> {
        let Some(hex) = value.strip_prefix(SHA256_PREFIX) else {
            return Err(ArtifactIdentityError::new(
                "digest",
                "must use canonical sha256:<64 lowercase hexadecimal characters> form",
            ));
        };
        if hex.len() != SHA256_HEX_LEN || !hex.bytes().all(is_lower_hex) {
            return Err(ArtifactIdentityError::new(
                "digest",
                "must use canonical sha256:<64 lowercase hexadecimal characters> form",
            ));
        }
        Ok(Self(value.to_owned()))
    }

    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ArtifactKind {
    OciImage,
    StaticArchive,
    SourceArchive,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct ArtifactIdentity {
    pub repository: RepositoryRef,
    pub commit: CommitId,
    pub kind: ArtifactKind,
    pub digest: Sha256Digest,
}

impl ArtifactIdentity {
    #[must_use]
    pub const fn new(
        repository: RepositoryRef,
        commit: CommitId,
        kind: ArtifactKind,
        digest: Sha256Digest,
    ) -> Self {
        Self {
            repository,
            commit,
            kind,
            digest,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ArtifactIdentityError {
    pub field: String,
    pub problem: String,
}

impl ArtifactIdentityError {
    fn new(field: impl Into<String>, problem: impl Into<String>) -> Self {
        Self {
            field: field.into(),
            problem: problem.into(),
        }
    }
}

impl fmt::Display for ArtifactIdentityError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{} {}", self.field, self.problem)
    }
}

impl std::error::Error for ArtifactIdentityError {}

fn valid_repository_component(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 100
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
}

const fn is_lower_hex(byte: u8) -> bool {
    byte.is_ascii_digit() || matches!(byte, b'a'..=b'f')
}

#[cfg(test)]
mod tests {
    use super::{ArtifactIdentity, ArtifactKind, CommitId, GitTreeId, RepositoryRef, Sha256Digest};

    fn digest() -> String {
        format!("sha256:{}", "ab".repeat(32))
    }

    #[test]
    fn immutable_identity_accepts_complete_source_and_content_evidence() {
        let identity = ArtifactIdentity::new(
            RepositoryRef::parse("example/project").expect("repository"),
            CommitId::parse(&"1a".repeat(20)).expect("commit"),
            ArtifactKind::OciImage,
            Sha256Digest::parse(&digest()).expect("digest"),
        );

        assert_eq!(identity.repository.as_str(), "example/project");
        assert_eq!(identity.commit.as_str().len(), 40);
        assert_eq!(identity.digest.as_str(), digest());
    }

    #[test]
    fn commit_ids_reject_abbreviations_and_uppercase_hex() {
        assert!(CommitId::parse("abc123").is_err());
        assert!(CommitId::parse(&"AB".repeat(20)).is_err());
        assert!(CommitId::parse(&"ab".repeat(32)).is_ok());
    }

    #[test]
    fn git_tree_ids_accept_complete_hashes_and_reject_invalid_forms() {
        assert!(GitTreeId::parse(&"ab".repeat(20)).is_ok());
        assert!(GitTreeId::parse(&"ab".repeat(32)).is_ok());
        assert!(GitTreeId::parse("abc123").is_err());
        assert!(GitTreeId::parse(&"AB".repeat(20)).is_err());
        assert!(GitTreeId::parse(&format!("{}g", "ab".repeat(19))).is_err());
    }

    #[test]
    fn digests_require_canonical_sha256_form() {
        assert!(Sha256Digest::parse(&digest()).is_ok());
        assert!(Sha256Digest::parse(&"ab".repeat(32)).is_err());
        assert!(Sha256Digest::parse(&format!("sha512:{}", "ab".repeat(32))).is_err());
        assert!(Sha256Digest::parse(&format!("sha256:{}", "AB".repeat(32))).is_err());
    }

    #[test]
    fn repositories_require_exactly_one_owner_and_name() {
        assert!(RepositoryRef::parse("example/project").is_ok());
        assert!(RepositoryRef::parse("project").is_err());
        assert!(RepositoryRef::parse("example/project/extra").is_err());
        assert!(RepositoryRef::parse("example/project name").is_err());
    }
}
