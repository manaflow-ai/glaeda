#!/usr/bin/env python3
"""Bounded contract tests for glaeda-linux-vm-base with a fake limactl; no VM, no network."""
from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import io
import json
import pathlib
import sys
import tarfile
import tempfile
import unittest
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOADER = importlib.machinery.SourceFileLoader("glaeda_linux_vm_base", str(ROOT / "scripts" / "glaeda-linux-vm-base"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
assert SPEC
base = importlib.util.module_from_spec(SPEC)
sys.modules[LOADER.name] = base
LOADER.exec_module(base)

HOST = {"system": "Darwin", "machine": "arm64", "cpus": 14, "memory_gib": 48, "free_gib": 115}
IDENTITY = json.loads((ROOT / "examples/lima/glaeda-prepared-template.json").read_text())
PROV = IDENTITY["provisioning"]
MARKER = json.dumps({
    "schema_version": 2,
    "actions_runner_version": IDENTITY["actions_runner"]["version"],
    "actions_runner_digest": IDENTITY["actions_runner"]["digest"],
    "workload_user": PROV["workload_user"],
    "runner_listener_protection": "setgid_nondumpable",
    "runner_install_directory": PROV["runner_install_directory"],
    "runner_work_directory": PROV["runner_work_directory"],
    "jit_launcher_path": PROV["jit_launcher_path"],
    "jit_launcher_digest": PROV["jit_launcher_digest"],
}, indent=2)


def ok(stdout="", code=0, stderr=""):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


class FakeLima:
    """A stateful limactl: instances live in LIMA_HOME directories like the real one."""

    def __init__(self, root: pathlib.Path, *, version="2.2.0", guest_arch="aarch64", mounts="", groups=None):
        self.root = root
        self.version = version
        self.guest_arch = guest_arch
        self.mounts = mounts
        self.groups = groups or PROV["workload_user"]
        self.status: dict[str, str] = {}
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str]] = []

    def home(self, env) -> pathlib.Path:
        return pathlib.Path(env["LIMA_HOME"])

    def __call__(self, argv, *, env, timeout):
        self.calls.append(list(argv))
        self.envs.append(dict(env))
        assert argv[0].endswith("/bin/limactl"), argv
        if argv[1:] == ["--version"]:
            return ok(f"limactl version {self.version}\n")
        assert argv[1] == "--tty=false", argv
        cmd, rest = argv[2], argv[3:]
        home = self.home(env)
        if cmd == "list":
            lines = [json.dumps({"name": n, "status": s, "vmType": "vz", "arch": "aarch64", "cpus": 4 if "smoke" in n else 2,
                                 "memory": 8 << 30 if "smoke" in n else 2 << 30,
                                 "config": {"plain": True, "mounts": [], "portForwards": [], "networks": []}})
                     for n, s in sorted(self.status.items())]
            return ok("\n".join(lines) + ("\n" if lines else ""))
        if cmd == "start" and rest[0] == "--name":
            name, template = rest[1], pathlib.Path(rest[-1])
            (home / name).mkdir(parents=True)
            (home / name / "lima.yaml").write_bytes(template.read_bytes())
            self.status[name] = "Running"
            return ok()
        if cmd == "start":
            self.status[rest[-1]] = "Running"
            return ok()
        if cmd == "stop":
            self.status[rest[0]] = "Stopped"
            return ok()
        if cmd == "clone":
            src, dst = rest[0], rest[1]
            (home / dst).mkdir()
            (home / dst / "lima.yaml").write_bytes((home / src / "lima.yaml").read_bytes())
            self.status[dst] = "Running"
            return ok()
        if cmd == "delete":
            self.status.pop(rest[-1])
            return ok()
        if cmd == "shell":
            assert rest[:2] == ["--workdir", "/"] and rest[3] == "--", rest
            return self.guest(rest[4:])
        raise AssertionError(argv)

    def guest(self, argv):
        if argv == ["/usr/bin/uname", "-m"]:
            return ok(self.guest_arch + "\n")
        if argv[0] == "/usr/bin/findmnt":
            return ok(self.mounts, 0 if self.mounts else 1)
        if argv[0] == "/bin/cat":
            return ok(MARKER + "\n")
        if argv[0] == "/usr/bin/stat":
            return ok("root:root:444\n")
        if argv[0] == "/usr/bin/test":
            return ok()
        if argv[:2] == ["/usr/bin/sudo", "--non-interactive"] and argv[2].endswith("integrity"):
            return ok("smolrunner-runner-integrity-ok\n")
        if argv[:2] == ["/usr/bin/sudo", "--non-interactive"] and argv[2] == "--user":
            return ok("", 1, "a password is required")
        if argv[0] == "/usr/bin/id":
            return ok(self.groups + "\n")
        if argv[0] == "/usr/bin/systemctl":
            return ok("masked\n", 1)
        raise AssertionError(argv)


def lima_archive(extra_link: tuple[str, str] | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        data = b"#!/bin/sh\n"
        info = tarfile.TarInfo("./bin/limactl")
        info.size, info.mode = len(data), 0o755
        tar.addfile(info, io.BytesIO(data))
        # The real 2.2.0 archive carries this in-tree relative link.
        for name, target in [("./share/doc/lima/templates", "../../lima/templates")] + ([extra_link] if extra_link else []):
            link = tarfile.TarInfo(name)
            link.type, link.linkname = tarfile.SYMTYPE, target
            tar.addfile(link)
    return buffer.getvalue()


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name) / "linux-vm"
        self.archive = lima_archive()
        self.downloads = 0
        self.fake = FakeLima(self.root)
        self.patch_pin(len(self.archive), hashlib.sha256(self.archive).hexdigest())

    def tearDown(self):
        base.LIMA_BYTES, base.LIMA_SHA256 = self._pin
        self.tmp.cleanup()

    def patch_pin(self, size, digest):
        self._pin = (base.LIMA_BYTES, base.LIMA_SHA256)
        base.LIMA_BYTES, base.LIMA_SHA256 = size, digest

    def download(self, url, dest, timeout):
        self.downloads += 1
        self.assertEqual(url, base.LIMA_URL)
        dest.write_bytes(self.archive)

    def installer(self, **kw):
        return base.Installer(root=self.root, runner=self.fake, download=self.download, host=HOST, sleep=lambda s: None, **kw)

    def test_plan_has_no_side_effects(self):
        out = self.installer().execute(apply=False)
        self.assertEqual(out["plan"], ["install_lima", "materialize_template", "create_start", "verify", "stop"])
        self.assertEqual(out["clone_sizing"], {"cpus": 4, "memory_gib": 8, "max_clones": 4})
        self.assertFalse(self.root.exists())
        self.assertEqual(self.downloads, 0)

    def test_apply_creates_verifies_stops_then_is_idempotent(self):
        out = self.installer().execute(apply=True)
        self.assertEqual(out["disposition"], "changed")
        self.assertEqual(out["status"], "Stopped")
        self.assertEqual(out["checks"]["guest_arch"], "aarch64")
        self.assertEqual(out["checks"]["runner_integrity"], "smolrunner-runner-integrity-ok")
        create = [c for c in self.fake.calls if c[2:4] == ["start", "--name"]]
        self.assertEqual(len(create), 1)
        self.assertEqual(create[0][3:6], ["--name", base.BASE_INSTANCE, "--timeout=45m"])
        # Exact template bytes, no --set/--mount overrides on the command line.
        self.assertEqual(len(create[0]), 7)
        self.assertEqual(pathlib.Path(create[0][6]).read_bytes(), base.TEMPLATE_YAML.read_bytes())
        for env in self.fake.envs:
            self.assertEqual(set(env) - {"USER", "LOGNAME", "TMPDIR"}, {"HOME", "PATH", "LIMA_HOME", "LC_ALL"})
        receipt = json.loads((self.root / "base.json").read_text())
        self.assertEqual(receipt["state"], "ready_stopped")
        self.fake.calls.clear()
        again = self.installer().execute(apply=True)
        self.assertEqual(again["disposition"], "unchanged")
        self.assertEqual(again["plan"], [])
        self.assertEqual(self.downloads, 1)
        self.assertFalse([c for c in self.fake.calls if c[2:3] in (["start"], ["stop"], ["delete"])])

    def test_reverify_restarts_and_stops(self):
        self.installer().execute(apply=True)
        out = self.installer().execute(apply=True, reverify=True)
        self.assertEqual(out["plan"], ["start", "verify", "stop"])
        self.assertEqual(out["status"], "Stopped")

    def test_running_base_is_verified_and_stopped(self):
        self.installer().execute(apply=True)
        self.fake.status[base.BASE_INSTANCE] = "Running"
        out = self.installer().execute(apply=True)
        self.assertEqual(out["plan"], ["verify", "stop"])
        self.assertEqual(out["status"], "Stopped")

    def test_foreign_instance_is_reported_not_touched(self):
        self.installer().execute(apply=True)
        (self.root / "base.json").unlink()
        self.fake.calls.clear()
        with self.assertRaisesRegex(base.Refused, "without this installer's receipt"):
            self.installer().execute(apply=True)
        self.assertFalse([c for c in self.fake.calls if c[2:3] in (["start"], ["stop"], ["delete"])])

    def test_template_drift_is_reported_not_replaced(self):
        self.installer().execute(apply=True)
        (self.root / "lima-home" / base.BASE_INSTANCE / "lima.yaml").write_text("other\n")
        with self.assertRaisesRegex(base.Refused, "another template"):
            self.installer().execute(apply=True)
        self.assertEqual(self.fake.status[base.BASE_INSTANCE], "Stopped")

    def test_broken_instance_is_reported(self):
        self.installer().execute(apply=True)
        self.fake.status[base.BASE_INSTANCE] = "Broken"
        with self.assertRaisesRegex(base.Refused, "Broken"):
            self.installer().execute(apply=True)
        self.assertIn(base.BASE_INSTANCE, self.fake.status)

    def test_wrong_lima_version_is_refused(self):
        self.installer().execute(apply=True)
        self.fake.version = "2.3.0"
        with self.assertRaisesRegex(base.Refused, "is not Lima 2.2.0"):
            self.installer().execute(apply=True)

    def test_archive_digest_mismatch_fails_before_install(self):
        base.LIMA_SHA256 = "0" * 64
        with self.assertRaisesRegex(base.Failed, "want"):
            self.installer().execute(apply=True)
        self.assertFalse((self.root / "lima-2.2.0").exists())
        self.assertFalse([c for c in self.fake.calls if "start" in c])

    def test_archive_link_escaping_the_tree_fails_before_install(self):
        self.archive = lima_archive(("./share/x", "../../../etc"))
        base.LIMA_BYTES, base.LIMA_SHA256 = len(self.archive), hashlib.sha256(self.archive).hexdigest()
        with self.assertRaisesRegex(base.Failed, "escaping link"):
            self.installer().execute(apply=True)
        self.assertFalse((self.root / "lima-2.2.0").exists())

    def test_link_stays_inside(self):
        self.assertTrue(base.link_stays_inside("./share/doc/lima/templates", "../../lima/templates"))
        self.assertFalse(base.link_stays_inside("./share/x", "../../etc"))
        self.assertFalse(base.link_stays_inside("./a", "/etc"))

    def test_template_digest_mismatch_refuses(self):
        other = pathlib.Path(self.tmp.name) / "t.yaml"
        other.write_bytes(base.TEMPLATE_YAML.read_bytes() + b"# drift\n")
        with self.assertRaisesRegex(base.Refused, "do not match"):
            self.installer(template_yaml=other).execute(apply=False)

    def test_failed_readiness_leaves_base_unverified(self):
        self.fake.guest_arch = "x86_64"
        with self.assertRaisesRegex(base.Failed, "guest_arch"):
            self.installer().execute(apply=True)
        receipt = json.loads((self.root / "base.json").read_text())
        self.assertEqual(receipt["state"], "created")
        self.fake.guest_arch = "aarch64"
        out = self.installer().execute(apply=True)
        self.assertEqual(out["plan"], ["verify", "stop"])

    def test_host_mount_in_guest_fails_readiness(self):
        self.fake.mounts = "/mnt/host virtiofs\n"
        with self.assertRaisesRegex(base.Failed, "guest_host_mounts"):
            self.installer().execute(apply=True)

    def test_interrupted_create_without_instance_is_refused(self):
        self.root.mkdir(parents=True)
        (self.root / "base.json").write_text(json.dumps({"created_by": base.SCHEMA, "instance": base.BASE_INSTANCE,
                                                         "state": "create_started"}))
        with self.assertRaisesRegex(base.Refused, "did not finish"):
            self.installer().execute(apply=True)

    def test_clone_smoke_uses_fixed_argv_and_deletes_only_its_clone(self):
        out = self.installer().execute(apply=True, clone_smoke=True)
        smoke = out["clone_smoke"]
        self.assertEqual(smoke["argv"][2:4], ["clone", base.BASE_INSTANCE])
        self.assertEqual(smoke["argv"][5:], ["--cpus", "4", "--memory", "8", "--mount-none", "--start"])
        self.assertTrue(smoke["deleted"])
        self.assertEqual(sorted(self.fake.status), [base.BASE_INSTANCE])
        deletes = [c for c in self.fake.calls if c[2:3] == ["delete"]]
        self.assertEqual(len(deletes), 1)
        self.assertTrue(deletes[0][-1].startswith(base.SMOKE_PREFIX))

    def test_non_mac_is_refused(self):
        installer = base.Installer(root=self.root, runner=self.fake, download=self.download,
                                   host=dict(HOST, system="Linux", machine="x86_64"))
        with self.assertRaisesRegex(base.Refused, "Apple-silicon"):
            installer.execute(apply=False)


if __name__ == "__main__":
    unittest.main()
