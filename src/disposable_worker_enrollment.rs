//! Canonical, secret-free operator enrollment for one disposable Scale Set worker.
//!
//! The document names the Mac-local control-plane inputs, but never contains the GitHub App
//! private key, an installation token, or a runner JIT value. Schema 2 names one credential source:
//! the Keychain item holding the App key (`keychain`), or the path of an installation token file the
//! key's home host pushes (`installation_token_file`). Schema 1 documents still decode, as the
//! Keychain kind, under their own canonical encoding. Construction delegates policy validation to the existing
//! bridge, prepared-template, resource, Lima, and consumer boundaries instead of duplicating
//! those semantics here.

// The validated parts become live when the following slice wires `worker serve`; keeping the
// decoder independently testable prevents that process entry point from becoming the policy
// parser. Remove this allowance when the service facade consumes `into_parts`.
#![allow(dead_code)]

use std::fmt;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::artifact::Sha256Digest;
use crate::disposable_clone_runtime::DisposableCloneRuntime;
use crate::disposable_host_storage::DisposableHostStorage;
use crate::disposable_lima_worker::{
    validate_disposable_lima_home_path_budget, validate_disposable_lima_resources,
};
use crate::disposable_prepared_template::current_disposable_prepared_template;
use crate::disposable_runner_runtime::DisposableRunnerRuntime;
use crate::disposable_worker_reconciler::DisposableWorkerResources;
use crate::github_scale_set_bridge::{
    GitHubAppKeychainConfig, InstallationTokenFileConfig, ScaleSetBridgeConfig,
    ScaleSetBridgeCredential, ScaleSetBridgeTarget,
};
use crate::github_scale_set_delivery_consumer::ScaleSetDeliveryConsumerPolicy;
use crate::lima_observation::LimaInstanceName;

pub const DISPOSABLE_WORKER_ENROLLMENT_SCHEMA_VERSION: u8 = 2;
/// The Keychain-only schema; still decoded so existing enrollments keep working unchanged.
pub const DISPOSABLE_WORKER_ENROLLMENT_SCHEMA_VERSION_V1: u8 = 1;
pub const MAX_DISPOSABLE_WORKER_ENROLLMENT_BYTES: usize = 16 * 1024;
const BRIDGE_PROGRAM: &str = "/opt/smolrunner/bin/scaleset-bridge";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum DisposableWorkerEnrollmentErrorKind {
    InvalidDocument,
    VersionIncompatible,
    NonCanonical,
    InvalidConfiguration,
}

#[derive(Clone, Copy, PartialEq, Eq, Serialize)]
pub struct DisposableWorkerEnrollmentError {
    kind: DisposableWorkerEnrollmentErrorKind,
    code: &'static str,
    message: &'static str,
}

impl DisposableWorkerEnrollmentError {
    #[must_use]
    pub const fn kind(self) -> DisposableWorkerEnrollmentErrorKind {
        self.kind
    }

    #[must_use]
    pub const fn code(self) -> &'static str {
        self.code
    }
}

impl fmt::Debug for DisposableWorkerEnrollmentError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("DisposableWorkerEnrollmentError")
            .field("kind", &self.kind)
            .field("code", &self.code)
            .finish()
    }
}

impl fmt::Display for DisposableWorkerEnrollmentError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.message)
    }
}

impl std::error::Error for DisposableWorkerEnrollmentError {}

/// Validated construction inputs for one local, single-concurrency Scale Set service.
///
/// The type deliberately has no serialization, path accessor, `Clone`, or derived `Debug`.
pub struct DisposableWorkerEnrollment {
    state_root: PathBuf,
    bridge_config: ScaleSetBridgeConfig,
    consumer_policy: ScaleSetDeliveryConsumerPolicy,
    host_storage: DisposableHostStorage,
    clone_runtime: DisposableCloneRuntime,
    runner_runtime: DisposableRunnerRuntime,
}

impl fmt::Debug for DisposableWorkerEnrollment {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("DisposableWorkerEnrollment")
            .field("state_root", &"<private-state-root>")
            .field("bridge", &"<enrolled-scale-set>")
            .field("policy", &self.consumer_policy)
            .finish()
    }
}

pub(crate) struct DisposableWorkerEnrollmentParts {
    pub(crate) state_root: PathBuf,
    pub(crate) bridge_config: ScaleSetBridgeConfig,
    pub(crate) consumer_policy: ScaleSetDeliveryConsumerPolicy,
    pub(crate) host_storage: DisposableHostStorage,
    pub(crate) clone_runtime: DisposableCloneRuntime,
    pub(crate) runner_runtime: DisposableRunnerRuntime,
}

impl DisposableWorkerEnrollment {
    pub(crate) fn into_parts(self) -> DisposableWorkerEnrollmentParts {
        DisposableWorkerEnrollmentParts {
            state_root: self.state_root,
            bridge_config: self.bridge_config,
            consumer_policy: self.consumer_policy,
            host_storage: self.host_storage,
            clone_runtime: self.clone_runtime,
            runner_runtime: self.runner_runtime,
        }
    }
}

/// Strictly decode one canonical, secret-free disposable-worker enrollment document.
///
/// # Errors
///
/// Returns a bounded, path-free error for oversize, malformed, unsupported, noncanonical, or
/// policy-invalid input.
pub fn decode_disposable_worker_enrollment(
    bytes: &[u8],
) -> Result<DisposableWorkerEnrollment, DisposableWorkerEnrollmentError> {
    if bytes.len() > MAX_DISPOSABLE_WORKER_ENROLLMENT_BYTES {
        return Err(enrollment_error(
            DisposableWorkerEnrollmentErrorKind::InvalidDocument,
            "disposable_worker_enrollment_too_large",
            "the disposable-worker enrollment exceeds the reviewed byte limit",
        ));
    }
    let version: VersionWire = serde_json::from_slice(bytes).map_err(|_| invalid_document())?;
    let wire = match version.schema_version {
        DISPOSABLE_WORKER_ENROLLMENT_SCHEMA_VERSION => {
            let wire: EnrollmentWire =
                serde_json::from_slice(bytes).map_err(|_| invalid_document())?;
            require_canonical(&canonical_bytes(&wire)?, bytes)?;
            wire
        }
        DISPOSABLE_WORKER_ENROLLMENT_SCHEMA_VERSION_V1 => {
            let wire: EnrollmentWireV1 =
                serde_json::from_slice(bytes).map_err(|_| invalid_document())?;
            require_canonical(&canonical_bytes(&wire)?, bytes)?;
            wire.into_current()
        }
        _ => {
            return Err(enrollment_error(
                DisposableWorkerEnrollmentErrorKind::VersionIncompatible,
                "disposable_worker_enrollment_version_incompatible",
                "the disposable-worker enrollment schema version is unsupported",
            ));
        }
    };
    build_enrollment(wire)
}

fn require_canonical(
    canonical: &[u8],
    bytes: &[u8],
) -> Result<(), DisposableWorkerEnrollmentError> {
    if canonical != bytes {
        return Err(enrollment_error(
            DisposableWorkerEnrollmentErrorKind::NonCanonical,
            "disposable_worker_enrollment_noncanonical",
            "the disposable-worker enrollment is not canonically encoded",
        ));
    }
    Ok(())
}

fn build_enrollment(
    wire: EnrollmentWire,
) -> Result<DisposableWorkerEnrollment, DisposableWorkerEnrollmentError> {
    if wire
        .scale_set
        .labels
        .windows(2)
        .any(|pair| pair[0] >= pair[1])
    {
        return Err(invalid_configuration());
    }
    let state_root = PathBuf::from(&wire.state_root);
    let limactl_program = PathBuf::from(&wire.lima.program);
    let lima_home = PathBuf::from(&wire.lima.home);
    let source_instance =
        LimaInstanceName::parse(&wire.lima.source_instance).map_err(|_| invalid_configuration())?;
    let bridge_digest =
        Sha256Digest::parse(&wire.bridge.program_digest).map_err(|_| invalid_configuration())?;
    let credential = match &wire.github.credential {
        CredentialWire::Keychain {
            client_id,
            installation_id,
            service,
            account,
        } => ScaleSetBridgeCredential::GitHubAppKeychain(
            GitHubAppKeychainConfig::new(
                &wire.github.config_url,
                client_id,
                *installation_id,
                service,
                account,
            )
            .map_err(|_| invalid_configuration())?,
        ),
        CredentialWire::InstallationTokenFile { app_id, path } => {
            ScaleSetBridgeCredential::InstallationTokenFile(
                InstallationTokenFileConfig::new(&wire.github.config_url, *app_id, Path::new(path))
                    .map_err(|_| invalid_configuration())?,
            )
        }
    };
    let target = ScaleSetBridgeTarget::new(
        wire.scale_set.id,
        &wire.scale_set.name,
        wire.scale_set.runner_group_id,
        &wire.scale_set.labels,
        &wire.scale_set.owner,
        1,
    )
    .map_err(|_| invalid_configuration())?;
    let bridge_config = ScaleSetBridgeConfig::with_credential(
        Path::new(BRIDGE_PROGRAM),
        bridge_digest,
        credential,
        target,
    )
    .map_err(|_| invalid_configuration())?;
    let prepared = current_disposable_prepared_template().map_err(|_| invalid_configuration())?;
    let resources = DisposableWorkerResources::new(
        wire.resources.cpu_millis,
        wire.resources.memory_bytes,
        wire.resources.disk_bytes,
    )
    .map_err(|_| invalid_configuration())?;
    validate_disposable_lima_resources(resources, &prepared)
        .map_err(|_| invalid_configuration())?;
    validate_disposable_lima_home_path_budget(&lima_home).map_err(|_| invalid_configuration())?;
    let consumer_policy = ScaleSetDeliveryConsumerPolicy::new(
        wire.scale_set.id,
        &wire.scale_set.repository,
        &wire.scale_set.owner,
        &wire.scale_set.labels,
        resources,
        &prepared,
    )
    .map_err(|_| invalid_configuration())?;
    let clone_runtime = DisposableCloneRuntime::new(
        state_root.clone(),
        limactl_program.clone(),
        lima_home.clone(),
        source_instance,
    )
    .map_err(|_| invalid_configuration())?;
    let host_storage = DisposableHostStorage::new(lima_home.clone(), resources.disk_bytes())
        .map_err(|_| invalid_configuration())?;
    let runner_runtime = DisposableRunnerRuntime::new(limactl_program, lima_home)
        .map_err(|_| invalid_configuration())?;
    Ok(DisposableWorkerEnrollment {
        state_root,
        bridge_config,
        consumer_policy,
        host_storage,
        clone_runtime,
        runner_runtime,
    })
}

fn canonical_bytes(wire: &impl Serialize) -> Result<Vec<u8>, DisposableWorkerEnrollmentError> {
    let mut bytes = serde_json::to_vec_pretty(wire).map_err(|_| invalid_document())?;
    bytes.push(b'\n');
    Ok(bytes)
}

const fn invalid_document() -> DisposableWorkerEnrollmentError {
    enrollment_error(
        DisposableWorkerEnrollmentErrorKind::InvalidDocument,
        "disposable_worker_enrollment_invalid",
        "the disposable-worker enrollment document is invalid",
    )
}

const fn invalid_configuration() -> DisposableWorkerEnrollmentError {
    enrollment_error(
        DisposableWorkerEnrollmentErrorKind::InvalidConfiguration,
        "disposable_worker_enrollment_configuration_invalid",
        "the disposable-worker enrollment configuration is invalid",
    )
}

const fn enrollment_error(
    kind: DisposableWorkerEnrollmentErrorKind,
    code: &'static str,
    message: &'static str,
) -> DisposableWorkerEnrollmentError {
    DisposableWorkerEnrollmentError {
        kind,
        code,
        message,
    }
}

#[derive(Deserialize)]
struct VersionWire {
    schema_version: u8,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct EnrollmentWire {
    schema_version: u8,
    state_root: String,
    lima: LimaWire,
    bridge: BridgeWire,
    github: GitHubWire,
    scale_set: ScaleSetWire,
    resources: ResourcesWire,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct LimaWire {
    program: String,
    home: String,
    source_instance: String,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct BridgeWire {
    program_digest: String,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct GitHubWire {
    config_url: String,
    credential: CredentialWire,
}

/// One credential source. The installation token file holds a pushed token, never the App key; its
/// owner, mode, link and expiry checks run at every session start, not at decode.
#[derive(Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum CredentialWire {
    Keychain {
        client_id: String,
        installation_id: u64,
        service: String,
        account: String,
    },
    InstallationTokenFile {
        app_id: u64,
        path: String,
    },
}

/// Schema 1: the Keychain-only layout, kept byte-for-byte so its canonical check is unchanged.
#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct EnrollmentWireV1 {
    schema_version: u8,
    state_root: String,
    lima: LimaWire,
    bridge: BridgeWire,
    github: GitHubWireV1,
    scale_set: ScaleSetWire,
    resources: ResourcesWire,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct GitHubWireV1 {
    config_url: String,
    client_id: String,
    installation_id: u64,
    keychain_service: String,
    keychain_account: String,
}

impl EnrollmentWireV1 {
    fn into_current(self) -> EnrollmentWire {
        EnrollmentWire {
            schema_version: DISPOSABLE_WORKER_ENROLLMENT_SCHEMA_VERSION,
            state_root: self.state_root,
            lima: self.lima,
            bridge: self.bridge,
            github: GitHubWire {
                config_url: self.github.config_url,
                credential: CredentialWire::Keychain {
                    client_id: self.github.client_id,
                    installation_id: self.github.installation_id,
                    service: self.github.keychain_service,
                    account: self.github.keychain_account,
                },
            },
            scale_set: self.scale_set,
            resources: self.resources,
        }
    }
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct ScaleSetWire {
    id: u32,
    name: String,
    runner_group_id: u32,
    owner: String,
    repository: String,
    labels: Vec<String>,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct ResourcesWire {
    cpu_millis: u32,
    memory_bytes: u64,
    disk_bytes: u64,
}

#[cfg(test)]
mod tests {
    use super::*;

    const DIGEST: &str = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

    fn canonical_document() -> Vec<u8> {
        canonical_bytes(&EnrollmentWire {
            schema_version: DISPOSABLE_WORKER_ENROLLMENT_SCHEMA_VERSION,
            state_root: "/private/var/lib/smolrunner".to_owned(),
            lima: LimaWire {
                program: "/opt/homebrew/bin/limactl".to_owned(),
                home: "/private/var/lib/smolrunner/lima".to_owned(),
                source_instance: "smolrunner-prepared-template".to_owned(),
            },
            bridge: BridgeWire {
                program_digest: DIGEST.to_owned(),
            },
            github: GitHubWire {
                config_url: "https://github.com/acme".to_owned(),
                credential: CredentialWire::Keychain {
                    client_id: "Iv1.0123456789abcdef".to_owned(),
                    installation_id: 42,
                    service: "smolrunner.github-app".to_owned(),
                    account: "acme-ci".to_owned(),
                },
            },
            scale_set: ScaleSetWire {
                id: 17,
                name: "smolrunner-disposable".to_owned(),
                runner_group_id: 3,
                owner: "acme".to_owned(),
                repository: "widgets".to_owned(),
                labels: vec!["self-hosted".to_owned(), "smolrunner".to_owned()],
            },
            resources: ResourcesWire {
                cpu_millis: 2_000,
                memory_bytes: 2 << 30,
                disk_bytes: 20 << 30,
            },
        })
        .unwrap()
    }

    #[test]
    fn canonical_enrollment_builds_all_existing_policy_boundaries() {
        let enrollment = decode_disposable_worker_enrollment(&canonical_document()).unwrap();
        let debug = format!("{enrollment:?}");
        assert!(!debug.contains("/private"));
        assert!(!debug.contains("acme-ci"));
        assert!(!debug.contains("Iv1."));
        assert!(!debug.contains(DIGEST));

        let parts = enrollment.into_parts();
        assert_eq!(parts.state_root, Path::new("/private/var/lib/smolrunner"));
        assert_eq!(
            parts.host_storage.test_lima_home(),
            Path::new("/private/var/lib/smolrunner/lima")
        );
        assert_eq!(parts.host_storage.test_required_available_bytes(), 30 << 30);
        let _ = parts.bridge_config;
        let _ = parts.consumer_policy;
        let _ = parts.host_storage;
        let _ = parts.clone_runtime;
        let _ = parts.runner_runtime;
    }

    #[test]
    fn prior_future_unknown_and_noncanonical_documents_fail_closed() {
        let current = String::from_utf8(canonical_document()).unwrap();
        for version in [0, 3] {
            let changed = current.replacen(
                "\"schema_version\": 2",
                &format!("\"schema_version\": {version}"),
                1,
            );
            let error = decode_disposable_worker_enrollment(changed.as_bytes()).unwrap_err();
            assert_eq!(
                error.kind(),
                DisposableWorkerEnrollmentErrorKind::VersionIncompatible
            );
        }

        let unknown = current.replacen(
            "\"state_root\":",
            "\"unexpected\": true,\n  \"state_root\":",
            1,
        );
        assert_eq!(
            decode_disposable_worker_enrollment(unknown.as_bytes())
                .unwrap_err()
                .kind(),
            DisposableWorkerEnrollmentErrorKind::InvalidDocument
        );

        let compact: serde_json::Value = serde_json::from_str(&current).unwrap();
        let compact = serde_json::to_vec(&compact).unwrap();
        assert_eq!(
            decode_disposable_worker_enrollment(&compact)
                .unwrap_err()
                .kind(),
            DisposableWorkerEnrollmentErrorKind::NonCanonical
        );
    }

    #[test]
    fn invalid_paths_identity_resources_and_labels_are_bounded_and_private() {
        let current = String::from_utf8(canonical_document()).unwrap();
        let cases = [
            current.replace("/opt/homebrew/bin/limactl", "relative/limactl"),
            current.replace(
                "/private/var/lib/smolrunner/lima",
                "/private/var/lib/smolrunner/this-is-an-intentionally-too-long-lima-home",
            ),
            current.replace(DIGEST, "sha256:not-a-digest"),
            current.replace("\"cpu_millis\": 2000", "\"cpu_millis\": 1"),
            current.replace("\"cpu_millis\": 2000", "\"cpu_millis\": 2001"),
            current.replace(
                "\"memory_bytes\": 2147483648",
                "\"memory_bytes\": 2147483649",
            ),
            current.replace("\"disk_bytes\": 21474836480", "\"disk_bytes\": 21474836481"),
            current.replace(
                "\"self-hosted\",\n      \"smolrunner\"",
                "\"smolrunner\",\n      \"self-hosted\"",
            ),
            current.replace(
                "\"self-hosted\",\n      \"smolrunner\"",
                "\"smolrunner\",\n      \"smolrunner\"",
            ),
        ];
        for bytes in cases {
            let error = decode_disposable_worker_enrollment(bytes.as_bytes()).unwrap_err();
            assert_eq!(
                error.kind(),
                DisposableWorkerEnrollmentErrorKind::InvalidConfiguration
            );
            assert_eq!(
                error.code(),
                "disposable_worker_enrollment_configuration_invalid"
            );
            assert!(!format!("{error:?}").contains("private"));
        }
    }

    fn v1_document() -> String {
        String::from_utf8(canonical_document())
            .unwrap()
            .replacen("\"schema_version\": 2", "\"schema_version\": 1", 1)
            .replace(
                "    \"credential\": {\n      \"kind\": \"keychain\",\n      \"client_id\": \"Iv1.0123456789abcdef\",\n      \"installation_id\": 42,\n      \"service\": \"smolrunner.github-app\",\n      \"account\": \"acme-ci\"\n    }\n",
                "    \"client_id\": \"Iv1.0123456789abcdef\",\n    \"installation_id\": 42,\n    \"keychain_service\": \"smolrunner.github-app\",\n    \"keychain_account\": \"acme-ci\"\n",
            )
    }

    fn token_file_document() -> String {
        String::from_utf8(canonical_document()).unwrap().replace(
            "    \"credential\": {\n      \"kind\": \"keychain\",\n      \"client_id\": \"Iv1.0123456789abcdef\",\n      \"installation_id\": 42,\n      \"service\": \"smolrunner.github-app\",\n      \"account\": \"acme-ci\"\n    }\n",
            "    \"credential\": {\n      \"kind\": \"installation_token_file\",\n      \"app_id\": 5067688,\n      \"path\": \"/Users/runner/.config/glaeda/scaleset-token.json\"\n    }\n",
        )
    }

    #[test]
    fn schema_v1_keychain_document_still_decodes_as_the_keychain_kind() {
        let v1 = v1_document();
        assert!(v1.contains("\"keychain_service\""));
        let enrollment = decode_disposable_worker_enrollment(v1.as_bytes()).unwrap();
        let parts = enrollment.into_parts();
        assert!(matches!(
            parts.bridge_config.credential(),
            ScaleSetBridgeCredential::GitHubAppKeychain(_)
        ));
        // A v1 document must stay in the v1 layout; the v2 credential object is refused there.
        let mixed = String::from_utf8(canonical_document()).unwrap().replacen(
            "\"schema_version\": 2",
            "\"schema_version\": 1",
            1,
        );
        assert_eq!(
            decode_disposable_worker_enrollment(mixed.as_bytes())
                .unwrap_err()
                .kind(),
            DisposableWorkerEnrollmentErrorKind::InvalidDocument
        );
    }

    #[test]
    fn installation_token_file_kind_builds_a_token_bridge_config() {
        let document = token_file_document();
        let enrollment = decode_disposable_worker_enrollment(document.as_bytes()).unwrap();
        assert!(!format!("{enrollment:?}").contains("scaleset-token"));
        let parts = enrollment.into_parts();
        assert!(matches!(
            parts.bridge_config.credential(),
            ScaleSetBridgeCredential::InstallationTokenFile(_)
        ));
    }

    #[test]
    fn installation_token_file_kind_fails_closed() {
        let document = token_file_document();
        let invalid = [
            document.replace(
                "/Users/runner/.config/glaeda/scaleset-token.json",
                "relative.json",
            ),
            document.replace(
                "/Users/runner/.config/glaeda/scaleset-token.json",
                "/Users/runner/../token.json",
            ),
            document.replace("\"app_id\": 5067688", "\"app_id\": 0"),
        ];
        for bytes in invalid {
            assert_eq!(
                decode_disposable_worker_enrollment(bytes.as_bytes())
                    .unwrap_err()
                    .kind(),
                DisposableWorkerEnrollmentErrorKind::InvalidConfiguration
            );
        }
        let malformed = [
            // A token or key value is never accepted inline.
            document.replace(
                "      \"app_id\": 5067688,\n",
                "      \"app_id\": 5067688,\n      \"token\": \"ghs_x\",\n",
            ),
            // Keychain fields do not mix into the token kind.
            document.replace(
                "      \"app_id\": 5067688,\n",
                "      \"app_id\": 5067688,\n      \"service\": \"x\",\n",
            ),
            document.replace("\"installation_token_file\"", "\"token_env\""),
        ];
        for bytes in malformed {
            assert_eq!(
                decode_disposable_worker_enrollment(bytes.as_bytes())
                    .unwrap_err()
                    .kind(),
                DisposableWorkerEnrollmentErrorKind::InvalidDocument
            );
        }
    }

    #[test]
    fn byte_bound_precedes_json_parsing() {
        let bytes = vec![b' '; MAX_DISPOSABLE_WORKER_ENROLLMENT_BYTES + 1];
        let error = decode_disposable_worker_enrollment(&bytes).unwrap_err();
        assert_eq!(error.code(), "disposable_worker_enrollment_too_large");
    }
}
