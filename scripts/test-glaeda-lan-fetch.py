#!/usr/bin/env python3
"""Tests for scripts/glaeda-lan-fetch and glaeda-seed-serve --role product (compiled products over the LAN)."""

from __future__ import annotations

import fcntl
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVE = ROOT / "scripts/glaeda-seed-serve"


def load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, os.fspath(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


lf = load("glaeda_lan_fetch", ROOT / "scripts/glaeda-lan-fetch")
serve = load("glaeda_seed_serve_products", SERVE)

# Stands in for /usr/bin/ssh to a peer: FAKE_PEERS maps an address to that peer's product cache; the real
# serve script answers as the forced command would. FAKE_PEER_MODE_<address> overrides one peer.
FAKE_SSH = """import json, os, sys
address, request = sys.argv[-2], sys.argv[-1]
with open(os.environ["FAKE_SSH_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
peers = json.loads(os.environ["FAKE_PEERS"])
mode = os.environ.get("FAKE_PEER_MODE_" + address.replace(".", "_"), "serve")
if address not in peers or mode == "unreachable":
    sys.stderr.write(f"ssh: connect to host {address} port 22: No route to host\\n"); sys.exit(255)
if mode == "short" and request.startswith("product-v1"):
    sha = request.split()[1]
    os.write(1, f"glaeda-seed-serve 1 hit-product {sha} 1000\\n".encode() + b"x" * 10); sys.exit(0)
if mode == "stalled" and request.startswith("product-has-v1"):
    import time
    time.sleep(60)
if mode == "silent" and request.startswith("product-v1"):
    import time
    if os.environ.get("FAKE_PIDFILE"):
        open(os.environ["FAKE_PIDFILE"], "w").write(str(os.getpid()))
    time.sleep(60)
os.environ["SSH_ORIGINAL_COMMAND"] = request
states = json.loads(os.environ.get("FAKE_STATES", "{}"))
os.execv(sys.executable, [sys.executable, os.environ["FAKE_SERVE"], "--role", "product", "--products", peers[address],
                          "--run-dir", peers[address] + ".run", "--receipt", "/nonexistent",
                          "--state", states.get(address, "/nonexistent")])
"""


def put_product(root: Path, data: bytes, *, digest: str | None = None, size: int | None = None,
                schema: int = serve.PRODUCT_SCHEMA) -> tuple[str, Path]:
    """A cmux node_product_cache entry holding DATA; returns (content digest, entry)."""
    sha = digest or hashlib.sha256(data).hexdigest()
    key = hashlib.sha256(os.urandom(16)).hexdigest()
    entry = root / "objects" / key[:2] / key
    entry.mkdir(parents=True)
    (entry / serve.PRODUCT_OBJECT).write_bytes(data)
    (entry / serve.PRODUCT_METADATA).write_text(json.dumps({
        "schema_generation": schema, "format_generation": serve.PRODUCT_FORMAT, "identity": {},
        "object_digest": sha, "size": len(data) if size is None else size, "source_class": "github"}))
    return sha, entry


class ProductServeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        # realpath: the serve refuses a symlink anywhere on the path, and macOS's /var is one.
        self.products = Path(os.path.realpath(self.tmp.name)) / "node-products"
        (self.products / "objects").mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def ask(self, request: str, role: str = "product") -> tuple[int, bytes]:
        env = {**os.environ, "SSH_ORIGINAL_COMMAND": request, "SSH_CLIENT": "172.20.21.196 1 22"}
        proc = subprocess.run([sys.executable, os.fspath(SERVE), "--role", role, "--products", os.fspath(self.products),
                               "--run-dir", os.fspath(Path(self.tmp.name) / "run"), "--receipt", "/nonexistent",
                               "--state", os.fspath(Path(self.tmp.name) / "ci")], env=env, capture_output=True, timeout=60)
        return proc.returncode, proc.stdout

    def test_a_cached_product_is_found_by_content_digest_and_streamed_exactly(self):
        data = os.urandom(300000)
        sha, _ = put_product(self.products, data)
        self.assertEqual(self.ask(f"product-has-v1 {sha}"), (0, f"glaeda-seed-serve 1 has {sha} {len(data)}\n".encode()))
        code, out = self.ask(f"product-v1 {sha}")
        self.assertEqual(code, 0)
        self.assertEqual(out, f"glaeda-seed-serve 1 hit-product {sha} {len(data)}\n".encode() + data)
        self.assertEqual(self.ask("product-has-v1 " + "0" * 64), (3, b"glaeda-seed-serve 1 miss\n"))

    def test_only_what_the_cache_layout_names_is_served(self):
        data = b"x" * 1000
        cases = []
        sha, entry = put_product(self.products, data)
        (entry / serve.PRODUCT_OBJECT).unlink()
        (entry / serve.PRODUCT_OBJECT).symlink_to("/etc/hosts")  # a symlinked object
        cases.append(sha)
        sha, entry = put_product(self.products, data, size=999)  # size disagrees with the file
        cases.append(sha)
        sha, _ = put_product(self.products, data, size=serve.MAX_OBJECT_BYTES + 1)
        cases.append(sha)
        sha, _ = put_product(self.products, b"y" * 1000, schema=1)  # an older cache schema
        cases.append(sha)
        real = Path(self.tmp.name) / "elsewhere"
        sha, entry = put_product(real, b"z" * 1000)
        link = self.products / "objects" / entry.parent.name
        link.mkdir(exist_ok=True)
        (link / entry.name).symlink_to(entry)  # a symlinked entry directory
        cases.append(sha)
        for sha in cases:
            with self.subTest(sha=sha[:8]):
                self.assertEqual(self.ask(f"product-v1 {sha}")[0], 3)

    def test_the_roles_never_answer_each_others_verbs(self):
        sha, _ = put_product(self.products, b"x")
        key = "admission-derived-data-v1-macOS-ARM64-" + "a" * 32 + "-j14-" + "b" * 40
        for request in (f"seed-v1 tar {key}", f"product-v1 {sha.upper()}", f"product-v1 {sha} x",
                        f"product-v1 ../{sha[3:]}", "product-v1", f"product-v2 {sha}"):
            with self.subTest(request=request[:40]):
                code, out = self.ask(request)
                self.assertEqual(code, 2)
                self.assertTrue(out.startswith(b"glaeda-seed-serve 1 refused"))
        # The seed role refuses product verbs (and, here, everything: no trusted receipt).
        self.assertEqual(self.ask(f"product-v1 {sha}", role="seed")[0], 2)
        self.assertEqual(self.ask("ping-v1"), (0, b"glaeda-seed-serve 1 pong\n"))  # products need no receipt

    def test_product_streams_share_the_seed_caps(self):
        sha, _ = put_product(self.products, b"x" * 100)
        run = Path(self.tmp.name) / "run"
        run.mkdir()
        import fcntl
        fds = []
        for n in range(serve.QUEUE):
            fd = os.open(run / f"queue-{n}.lock", os.O_RDONLY | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fds.append(fd)
        try:
            self.assertEqual(self.ask(f"product-v1 {sha}"), (4, b"glaeda-seed-serve 1 busy\n"))
            # Lookups are admitted before the cache is scanned: a flood of them is capped too.
            self.assertEqual(self.ask(f"product-has-v1 {sha}"), (4, b"glaeda-seed-serve 1 busy\n"))
        finally:
            for fd in fds:
                os.close(fd)
        fd = os.open(run / "client-172.20.21.196.lock", os.O_RDONLY | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            self.assertEqual(self.ask(f"product-has-v1 {sha}")[0], 4)  # one request per client address
        finally:
            os.close(fd)
        self.assertEqual(self.ask(f"product-has-v1 {sha}")[0], 0)

    def test_an_inventory_poll_does_not_hold_the_same_clients_product_lane(self):
        # The broker polls every peer's inventory every INDEX_INTERVAL seconds from the same host that
        # fetches products through it. Sharing the per-client guard made a concurrent product lookup
        # answer busy, and a busy peer is indistinguishable from a miss to the fetcher, so the fetch
        # reported "no peer has it" about a peer that had it.
        sha, _ = put_product(self.products, b"x" * 100)
        run = Path(self.tmp.name) / "run"
        run.mkdir()
        import fcntl
        for holder, asked in (("client-172.20.21.196-inventory.lock", f"product-has-v1 {sha}"),
                              ("client-172.20.21.196.lock", "inventory-v1")):
            fd = os.open(run / holder, os.O_RDONLY | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                self.assertEqual(self.ask(asked)[0], 0, f"{holder} must not block {asked}")
            finally:
                os.close(fd)

    def test_hard_links_other_owners_big_metadata_and_symlinked_paths_are_refused(self):
        data = b"x" * 1000
        sha, entry = put_product(self.products, data)
        os.link(entry / serve.PRODUCT_OBJECT, Path(self.tmp.name) / "second-link")  # st_nlink == 2
        self.assertEqual(self.ask(f"product-has-v1 {sha}")[0], 3)
        (Path(self.tmp.name) / "second-link").unlink()
        self.assertEqual(self.ask(f"product-has-v1 {sha}")[0], 0)
        sha2, entry2 = put_product(self.products, b"y" * 10)
        metadata = json.loads((entry2 / serve.PRODUCT_METADATA).read_text())
        (entry2 / serve.PRODUCT_METADATA).write_text(json.dumps({**metadata, "pad": "p" * serve.MAX_METADATA_BYTES}))
        self.assertEqual(self.ask(f"product-has-v1 {sha2}")[0], 3)
        with unittest.mock.patch.object(serve.os, "getuid", return_value=os.getuid() + 1):
            self.assertIsNone(serve.open_product(self.products, sha))  # owned by someone else
        self.assertIsNotNone(serve.open_product(self.products, sha))
        # A symlink anywhere on the cache path, not only at the object.
        link = Path(os.path.realpath(self.tmp.name)) / "products-link"
        link.symlink_to(self.products)
        self.assertIsNone(serve.open_product(link, sha))
        self.assertIsNone(serve.open_product(link / "..", sha))
        objects = self.products / "objects"
        moved = Path(os.path.realpath(self.tmp.name)) / "objects-real"
        objects.rename(moved)
        objects.symlink_to(moved)
        self.assertIsNone(serve.open_product(self.products, sha))


# Stands in for /usr/bin/curl on the cache host: https://BUCKET/v1/NS/objects/FILE is FAKE_BUCKETS/BUCKET/NS/FILE.
FAKE_CURL = """import os, sys, time
args = sys.argv[1:]
out, url = args[args.index("--output") + 1], args[-1]
code_to = sys.stderr if "%{stderr}" in args[args.index("--write-out") + 1] else sys.stdout
with open(os.environ["FAKE_CURL_LOG"], "a") as log:
    log.write(url + "\\n")
bucket, rest = url.removeprefix("https://").split("/", 1)
ns, file = rest.removeprefix("v1/").split("/objects/")
source = os.path.join(os.environ["FAKE_BUCKETS"], bucket, ns, file)
if not os.path.exists(source):
    code_to.write("404"); sys.exit(22)
data = open(source, "rb").read()
with (os.fdopen(1, "wb", closefd=False) if out == "-" else open(out, "wb")) as f:
    for i in range(0, len(data), 4096):
        f.write(data[i:i + 4096]); f.flush()
        time.sleep(float(os.environ.get("FAKE_CURL_DELAY", "0")))
code_to.write("200")
"""

# Stands in for /usr/bin/ssh to the cache host: the real serve script answers as its forced command would.
FAKE_CACHE_SSH = """import json, os, sys
address, request = sys.argv[-2], sys.argv[-1]
if address != os.environ["FAKE_CACHE_HOST"]:
    sys.stderr.write("ssh: No route to host\\n"); sys.exit(255)
os.environ["SSH_ORIGINAL_COMMAND"] = request
os.environ["SSH_CLIENT"] = "172.20.21.197 1 22"
os.execv(sys.executable, [sys.executable, os.environ["FAKE_SERVE"], "--role", "cache",
                          "--cache", os.environ["FAKE_CACHE"], "--mirrors", os.environ["FAKE_MIRRORS"],
                          "--run-dir", os.environ["FAKE_CACHE"] + ".run", "--receipt", os.environ["FAKE_RECEIPT"],
                          "--curl", os.environ["FAKE_CURL"]])
"""


class LanCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(os.path.realpath(self.tmp.name))
        self.base = base
        self.buckets = base / "buckets"
        self.cache = base / "lan-cache"
        self.mirrors = base / "git"
        self.mirrors.mkdir()
        receipt = base / "receipt.json"
        receipt.write_text(json.dumps({"member": {"trustedRef": "refs/heads/main"}}))
        conf = base / "lan-cache-conf"
        conf.mkdir()
        (conf / "id_ed25519").write_text("k")
        (conf / "known_hosts").write_text("x")
        (conf / "config.json").write_text(json.dumps({"schema": 1, "source": "lan", "host": "cmux15", "user": "cmux",
                                                      "addresses": ["172.20.21.9", "172.20.21.202"]}))
        self.conf = conf / "config.json"
        for name, text in (("fake-ssh", FAKE_CACHE_SSH), ("fake-curl", FAKE_CURL)):
            (base / name).write_text(f"#!{sys.executable}\n" + text)
            (base / name).chmod(0o755)
        self.saved = (lf.SSH, dict(os.environ))
        lf.SSH = os.fspath(base / "fake-ssh")
        self.curl_log = base / "curl.log"
        os.environ.update({"FAKE_CACHE_HOST": "172.20.21.202", "FAKE_SERVE": os.fspath(SERVE),
                           "FAKE_CACHE": os.fspath(self.cache), "FAKE_MIRRORS": os.fspath(self.mirrors),
                           "FAKE_RECEIPT": os.fspath(receipt), "FAKE_CURL": os.fspath(base / "fake-curl"),
                           "FAKE_BUCKETS": os.fspath(self.buckets), "FAKE_CURL_LOG": os.fspath(self.curl_log)})
        self.out = base / "job"
        self.out.mkdir()

    def tearDown(self) -> None:
        lf.SSH, env = self.saved
        os.environ.clear()
        os.environ.update(env)
        self.tmp.cleanup()

    def put(self, data: bytes, file: str = "spm-abc.tar.zst", ns: str = "macOS-ARM64",
            bucket: str = "ci-cache.cmux.com") -> str:
        (self.buckets / bucket / ns).mkdir(parents=True, exist_ok=True)
        (self.buckets / bucket / ns / file).write_bytes(data)
        return f"{bucket}/{ns}/{file}"

    def fetch(self, name: str, dest: str = "archive") -> tuple[int, dict, Path]:
        code, record = lf.fetch_cache(name, self.out / dest, lf.load_cache_config(self.conf))
        return code, record, self.out / dest

    def downloads(self) -> list[str]:
        return self.curl_log.read_text().splitlines() if self.curl_log.exists() else []

    def test_an_object_crosses_the_uplink_once(self):
        data = os.urandom(300000)
        name = self.put(data)
        code, record, dest = self.fetch(name)
        self.assertEqual((code, record["bytes"], record["address"]), (0, len(data), "172.20.21.202"), record)
        self.assertEqual(dest.read_bytes(), data)
        code, _, dest = self.fetch(name, "again")
        self.assertEqual((code, dest.read_bytes()), (0, data))
        self.assertEqual(self.downloads(), ["https://ci-cache.cmux.com/v1/macOS-ARM64/objects/spm-abc.tar.zst"])
        self.assertEqual(sorted(p.name for p in self.out.iterdir() if p.name.startswith(".")), [])

    def test_clients_waiting_on_one_fill_share_it_and_hear_its_progress(self):
        data = os.urandom(40960)
        name = self.put(data)
        os.environ["FAKE_CURL_DELAY"] = "0.3"  # ~3 s a fill: longer than one progress report
        results = []
        threads = [threading.Thread(target=lambda i=i: results.append(self.fetch(name, f"d{i}"))) for i in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(sorted(code for code, _, _ in results), [0, 0, 0], [r for _, r, _ in results])
        self.assertTrue(all(dest.read_bytes() == data for _, _, dest in results))
        self.assertEqual(len(self.downloads()), 1)

    def test_a_missing_object_is_a_miss_and_a_down_host_an_error(self):
        code, record, dest = self.fetch("ci-cache.cmux.com/macOS-ARM64/none.tar.zst")
        self.assertEqual((code, record["reason"]), (3, "not in the bucket"))
        self.assertFalse(dest.exists())
        os.environ["FAKE_CACHE_HOST"] = "10.0.0.1"
        name = self.put(b"x" * 100)
        code, record, dest = self.fetch(name)
        self.assertEqual(code, 1)
        self.assertFalse(dest.exists())
        self.assertEqual(lf.fetch_cache(name, self.out / "z", None)[0], 1)  # no config: download directly

    def test_only_cache_objects_of_known_buckets_are_asked(self):
        for parts in (("evil.example", "ns", "a.tar.zst"), ("ci-cache.cmux.com", "../x", "a.tar.zst"),
                      ("ci-cache.cmux.com", "ns", "a.txt"), ("ci-cache.cmux.com", "ns", "../a.tar.zst")):
            with self.subTest(parts=parts):
                self.assertIsNone(lf.cache_object(*parts))
        for request in ("cache-v1 evil.example ns a.tar.zst", "cache-v1 ci-cache.cmux.com ns ../a.tar.zst",
                        "git-upload-pack '../etc.git'", "product-v1 " + "a" * 64, "seed-list-v1"):
            with self.subTest(request=request):
                with self.assertRaises(serve.Refused):
                    serve.parse_request(request, "cache")

    def test_the_cache_role_needs_a_trusted_host(self):
        Path(os.environ["FAKE_RECEIPT"]).write_text(json.dumps({"member": {}}))
        code, record, _ = self.fetch(self.put(b"x" * 10))
        self.assertEqual(code, 1)
        self.assertIn("refused", record["error"])

    def test_the_prune_keeps_the_newest_and_the_one_just_filled(self):
        root = self.cache / "objects"
        root.mkdir(parents=True)
        names = [f"ci-cache.cmux.com/ns/{n}.tar.zst" for n in ("old", "mid", "new")]
        for index, name in enumerate(names):
            serve.stored(self.cache, name).write_bytes(b"x" * 100)
            os.utime(serve.stored(self.cache, name), (1000 + index, 1000 + index))
        serve.part_of(self.cache, names[1]).write_bytes(b"x" * 100)
        removed = serve.prune_cache(self.cache, names[0], budget=250, min_free=0)
        self.assertEqual(removed, 1)
        self.assertFalse(serve.stored(self.cache, names[1]).exists())
        self.assertTrue(serve.stored(self.cache, names[0]).exists() and serve.stored(self.cache, names[2]).exists())
        self.assertTrue(serve.part_of(self.cache, names[1]).exists())

    def test_names_differing_only_in_case_never_share_an_object(self):
        name = self.put(b"lower" * 100, file="a.tar.zst")
        self.assertEqual(self.fetch(name)[0], 0)
        # The test's own bucket folds case too, so it answers; what matters is that the host asked it.
        self.fetch("ci-cache.cmux.com/macOS-ARM64/A.tar.zst", "upper")
        self.assertEqual(len(self.downloads()), 2)
        self.assertNotEqual(serve.stored(self.cache, name), serve.stored(self.cache, name.replace("a.", "A.")))

    def test_progress_to_a_client_that_stopped_reading_is_dropped(self):
        script = (f"import fcntl, os, sys, importlib.machinery, importlib.util\n"
                  f"loader = importlib.machinery.SourceFileLoader('s', {os.fspath(SERVE)!r})\n"
                  "spec = importlib.util.spec_from_loader('s', loader); m = importlib.util.module_from_spec(spec)\n"
                  "loader.exec_module(m)\n"
                  f"part = m.Path({os.fspath(self.base / 'live-part')!r}); part.mkdir()\n"
                  "(part / m.STAGED).write_bytes(b'x')\n"
                  "for _ in range(20000): m.report(part)\n"
                  "sys.stderr.write('done')\n")
        read, write = os.pipe()  # never read: the pipe fills after a few KB
        proc = subprocess.run([sys.executable, "-c", script], stdout=write, stderr=subprocess.PIPE, timeout=30)
        os.close(write)
        os.close(read)
        self.assertEqual(proc.stderr, b"done")

    def test_git_is_served_only_from_a_ready_mirror(self):
        repo = self.mirrors / "cmux.git"
        subprocess.run(["git", "init", "-q", "--bare", os.fspath(repo)], check=True)
        self.assertEqual(serve.ready_mirrors(self.mirrors), {})
        (self.mirrors / "cmux.json").write_text(json.dumps({"ready": {"cmux": "a" * 40, "nope": "b" * 40,
                                                                      "../x": "c" * 40}}))
        self.assertEqual(serve.ready_mirrors(self.mirrors), {"cmux": "a" * 40})
        self.assertEqual(serve.parse_request("git-upload-pack 'cmux.git'", "cache"), ("git", "", ["cmux"]))
        self.assertEqual(serve.parse_request("git-upload-pack '/cmux.git'", "cache"), ("git", "", ["cmux"]))


    def test_an_object_over_the_cap_is_never_kept_or_served(self):
        name = self.put(b"y" * 5000)
        saved = serve.FILL_MAX_BYTES
        serve.FILL_MAX_BYTES = 1000
        self.addCleanup(setattr, serve, "FILL_MAX_BYTES", saved)
        # A fill that finished before the first poll: checked once more before it is kept.
        self.assertEqual(serve.fill(self.cache, name, curl=os.environ["FAKE_CURL"]), "larger than a cache object may be")
        self.assertFalse(serve.stored(self.cache, name).exists())
        serve.stored(self.cache, name).parent.mkdir(parents=True, exist_ok=True)
        serve.stored(self.cache, name).write_bytes(b"y" * 5000)  # one kept before the cap
        self.assertIsNone(serve.open_cached(self.cache, name))

    def test_the_transfer_is_staged_in_a_private_directory_and_never_seen_half_written(self):
        data = os.urandom(20000)
        name = self.put(data)
        real = lf.cache_transfer
        seen = []

        def look(config, address, name, fd, deadline, cancel):
            result = real(config, address, name, fd, deadline, cancel)
            stage = next(p for p in self.out.iterdir() if p.name.startswith(".archive.lan-"))
            seen.append((stat.S_IMODE(stage.lstat().st_mode), stage.is_dir(), (self.out / "archive").exists()))
            return result

        with mock.patch.object(lf, "cache_transfer", look):
            code, record, dest = self.fetch(name)
        self.assertEqual(code, 0, record)
        # private, and DEST absent until the file was whole (one stage per address tried: the first is down)
        self.assertEqual(set(seen), {(0o700, True, False)})
        self.assertEqual(dest.read_bytes(), data)
        self.assertEqual([p.name for p in self.out.iterdir()], ["archive"])  # the stage is removed

    def test_a_stage_directory_open_to_others_is_never_used(self):
        name = self.put(b"v" * 100)
        real_mkdir = os.mkdir

        def lax(path, mode=0o777, *, dir_fd=None):  # as if another user swapped in a directory anyone can write
            real_mkdir(path, mode, dir_fd=dir_fd)
            os.chmod(path, 0o777, dir_fd=dir_fd)

        with mock.patch.object(lf.os, "mkdir", lax):
            code, record, dest = self.fetch(name)
        self.assertEqual(code, 1, record)
        self.assertIn("PermissionError", record["error"])
        self.assertFalse(dest.exists())
        with mock.patch.object(serve.os, "mkdir", lax):
            self.assertEqual(serve.fill(self.cache, name, curl=os.environ["FAKE_CURL"]),
                             "the stage directory is not this fill's own")
        self.assertFalse(serve.stored(self.cache, name).exists())

    def test_a_redirect_is_never_kept_as_an_object(self):
        name = self.put(b"<html>moved</html>")
        curl = self.base / "fake-redirect-curl"
        curl.write_text(f"#!{sys.executable}\nimport sys\nargs = sys.argv[1:]\n"
                        "out = args[args.index('--output') + 1]\n"
                        "(open(1, 'wb', closefd=False) if out == '-' else open(out, 'wb')).write(b'<html>moved</html>')\n"
                        "sys.stderr.write('302')\n")
        curl.chmod(0o755)
        self.assertEqual(serve.fill(self.cache, name, curl=os.fspath(curl)), "curl exit 0 http 302")
        self.assertFalse(serve.stored(self.cache, name).exists())

    def test_running_fills_keep_room_for_each_other(self):
        name = self.put(b"z" * 100)
        objects = self.cache / "objects"
        objects.mkdir(parents=True, exist_ok=True)
        other = serve.part_of(self.cache, "ci-cache.cmux.com/ns/other.tar.zst")
        other.mkdir()
        (other / serve.STAGED).write_bytes(b"x" * 10)  # a fill running now
        free = serve.CACHE_MIN_FREE + serve.FILL_MAX_BYTES + 1000  # room for one fill, not two
        usage = shutil.disk_usage(self.cache)._replace(free=free)
        with mock.patch.object(serve.shutil, "disk_usage", lambda path: usage):
            self.assertEqual(serve.fill(self.cache, name, curl=os.environ["FAKE_CURL"]), "no room for a fill")
            os.utime(other / serve.STAGED, (0, 0))  # its fill died long ago: no longer holds room, and is removed
            os.utime(other, (0, 0))
            self.assertEqual(serve.fill(self.cache, name, curl=os.environ["FAKE_CURL"]), "")
        self.assertFalse(other.exists())

    def test_a_request_never_waits_behind_other_objects_fills(self):
        run_dir = Path(os.environ["FAKE_CACHE"] + ".run")
        run_dir.mkdir(parents=True, exist_ok=True)
        name = self.put(b"s" * 100)
        held = []
        for index in range(serve.FILL_SLOTS):  # every fill slot taken by other objects' fills
            fd = os.open(run_dir / f"fill-slot-{index}.lock", os.O_RDONLY | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            held.append(fd)
        try:
            started = time.monotonic()
            code, record, dest = self.fetch(name)
            took = time.monotonic() - started
        finally:
            for fd in held:
                os.close(fd)
        self.assertEqual(code, 1, record)
        self.assertIn("busy", record["error"])
        self.assertLess(took, serve.ADMIT_WAIT + 10)  # told busy, not kept alive by progress lines for the others
        self.assertFalse(dest.exists())
        self.assertEqual(self.downloads(), [])
        self.assertEqual(self.fetch(name, "later")[0], 0)  # once a slot is free it fills

    def test_a_request_behind_another_objects_fill_lock_is_told_busy(self):
        run_dir = Path(os.environ["FAKE_CACHE"] + ".run")
        run_dir.mkdir(parents=True, exist_ok=True)
        name = self.put(b"t" * 100)
        stripe = int(hashlib.sha256(name.encode()).hexdigest()[:24], 16) % serve.FILL_LOCKS
        fd = os.open(run_dir / f"fill-{stripe}.lock", os.O_RDONLY | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)  # held by a fill of another object in the same stripe: no part of ours
        try:
            started = time.monotonic()
            code, record, _ = self.fetch(name)
            took = time.monotonic() - started
        finally:
            os.close(fd)
        self.assertEqual(code, 1, record)
        self.assertIn("busy", record["error"])
        self.assertLess(took, serve.ADMIT_WAIT + 15)

    def test_progress_is_only_reported_for_a_running_fill_of_the_same_object(self):
        part = self.base / "part"
        self.assertIsNone(serve.live_fill(part))
        part.mkdir()
        (part / serve.STAGED).write_bytes(b"abc")
        self.assertEqual(serve.live_fill(part), 3)
        os.utime(part / serve.STAGED, (0, 0))  # a dead fill's part
        self.assertIsNone(serve.live_fill(part))
        shutil.rmtree(part)
        (self.base / "elsewhere").mkdir()
        (self.base / "elsewhere" / serve.STAGED).write_bytes(b"abcdef")
        part.symlink_to(self.base / "elsewhere")
        self.assertIsNone(serve.live_fill(part))  # a symlink is never followed

    def test_a_fill_never_follows_a_symlink(self):
        name = self.put(b"u" * 5000)
        objects = self.cache / "objects"
        objects.mkdir(parents=True, exist_ok=True)
        outside = self.base / "outside"
        outside.write_bytes(b"keep")
        serve.part_of(self.cache, name).symlink_to(outside)  # raced in before the fill
        self.assertEqual(serve.fill(self.cache, name, curl=os.environ["FAKE_CURL"]), "")
        self.assertFalse(os.path.lexists(serve.part_of(self.cache, name)))  # the stage is gone after success
        self.assertEqual(outside.read_bytes(), b"keep")
        self.assertEqual(serve.stored(self.cache, name).read_bytes(), b"u" * 5000)
        self.assertFalse(serve.stored(self.cache, name).is_symlink())
        missing = "ci-cache.cmux.com/macOS-ARM64/gone.tar.zst"
        serve.miss_of(self.cache, missing).symlink_to(self.base / "planted-miss")
        serve.touch_miss(self.cache, missing)
        self.assertFalse((self.base / "planted-miss").exists())
        # An objects directory that is a symlink is not ours: nothing is filled through it.
        other = self.base / "other-cache"
        (other).mkdir()
        (self.base / "elsewhere-objects").mkdir()
        (other / "objects").symlink_to(self.base / "elsewhere-objects")
        self.assertEqual(serve.fill(other, name, curl=os.environ["FAKE_CURL"]), "the cache directory is not ours")
        self.assertEqual(list((self.base / "elsewhere-objects").iterdir()), [])

    def test_a_miss_is_shared_by_the_requests_behind_it(self):
        name = "ci-cache.cmux.com/macOS-ARM64/none.tar.zst"
        self.assertEqual(self.fetch(name)[0], 3)
        self.assertEqual(self.fetch(name, "again")[0], 3)
        self.assertEqual(len(self.downloads()), 1)
        os.utime(serve.miss_of(self.cache, name), (0, 0))  # past MISS_TTL: the bucket is asked again
        self.assertEqual(self.fetch(name, "later")[0], 3)
        self.assertEqual(len(self.downloads()), 2)

    def test_an_interrupted_fill_stops_its_curl(self):
        name = self.put(b"q" * 40960)
        os.environ["FAKE_CURL_DELAY"] = "0.5"
        started = []
        real = serve.subprocess.Popen

        def popen(*args, **kwargs):
            started.append(real(*args, **kwargs))
            return started[-1]

        def interrupt(proc, *args):
            raise TimeoutError("the request's alarm")

        with mock.patch.object(serve.subprocess, "Popen", popen), mock.patch.object(serve, "watch_fill", interrupt):
            with self.assertRaises(TimeoutError):
                serve.fill(self.cache, name, curl=os.environ["FAKE_CURL"])
        self.assertIsNotNone(started[0].poll())
        self.assertFalse(serve.part_of(self.cache, name).exists())

    def test_the_mirror_list_is_admitted_and_bounded(self):
        self.assertEqual(self.fetch(self.put(b"x" * 10))[0], 0)  # makes the run dir
        run_dir = Path(os.environ["FAKE_CACHE"] + ".run")
        taken = [serve.admit(run_dir, None, queue="cache-queue", places=1)]
        saved = serve.CACHE_QUEUE
        serve.CACHE_QUEUE = 1
        self.addCleanup(setattr, serve, "CACHE_QUEUE", saved)
        with mock.patch.object(serve, "say"), mock.patch.object(serve.signal, "alarm"):
            self.assertEqual(serve.serve_mirrors(self.mirrors, run_dir, {}), 4)
        for fd in taken[0]:
            os.close(fd)

class LanFetchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(os.path.realpath(self.tmp.name))
        self.peers = {"172.20.21.197": base / "p1", "172.20.21.198": base / "p2"}
        for root in self.peers.values():
            (root / "objects").mkdir(parents=True)
        conf = base / "lan-mesh"
        conf.mkdir()
        (conf / "id_ed25519").write_text("k")
        (conf / "known_hosts").write_text("x")
        (conf / "config.json").write_text(json.dumps({"schema": 1, "user": "cmux", "peers": [
            {"name": "cmux13s-mac-mini", "address": "172.20.21.197"},
            {"name": "cmux14", "address": "172.20.21.198"}]}))
        self.conf = conf / "config.json"
        fake = base / "fake-ssh"
        fake.write_text(f"#!{sys.executable}\n" + FAKE_SSH)
        fake.chmod(0o755)
        self.log = base / "ssh.log"
        self.saved = (lf.SSH, lf.TRANSFER_TIMEOUT, dict(os.environ))
        lf.SSH = os.fspath(fake)
        os.environ.update({"FAKE_PEERS": json.dumps({a: os.fspath(r) for a, r in self.peers.items()}),
                           "FAKE_SERVE": os.fspath(SERVE), "FAKE_SSH_LOG": os.fspath(self.log)})
        self.out = base / "job"
        self.out.mkdir()

    def tearDown(self) -> None:
        lf.SSH, lf.TRANSFER_TIMEOUT, env = self.saved
        os.environ.clear()
        os.environ.update(env)
        self.tmp.cleanup()

    def fetch(self, sha: str, name: str = "app-host-products.tar.gz") -> tuple[int, dict, Path]:
        dest = self.out / name
        code, record = lf.fetch(sha, dest, lf.load_config(self.conf))
        return code, record, dest

    def leftovers(self) -> list[str]:
        return sorted(p.name for p in self.out.iterdir() if p.name.startswith("."))

    def test_a_peer_that_has_it_serves_a_verified_file(self):
        data = os.urandom(500000)
        sha, _ = put_product(self.peers["172.20.21.198"], data)
        code, record, dest = self.fetch(sha)
        self.assertEqual((code, record["peer"], record["bytes"]), (0, "cmux14", len(data)))
        self.assertEqual(dest.read_bytes(), data)
        self.assertEqual(self.leftovers(), [])

    def test_nobody_has_it_is_a_miss(self):
        code, record, dest = self.fetch("1" * 64)
        self.assertEqual((code, record["reason"]), (3, "no peer has it"))
        self.assertFalse(dest.exists())

    def test_a_peer_serving_wrong_bytes_never_reaches_dest(self):
        # A PR job on the peer rewrote its cache: metadata still names the digest, the bytes differ.
        data = os.urandom(10000)
        sha = hashlib.sha256(data).hexdigest()
        put_product(self.peers["172.20.21.197"], os.urandom(10000), digest=sha)
        code, record, dest = self.fetch(sha)
        self.assertEqual(code, 1)
        self.assertIn("sha256 mismatch", record["error"])
        self.assertFalse(dest.exists())
        self.assertEqual(self.leftovers(), [])
        # Another peer with the right bytes still serves it.
        put_product(self.peers["172.20.21.198"], data)
        code, record, dest = self.fetch(sha)
        self.assertEqual((code, record["peer"]), (0, "cmux14"))
        self.assertEqual(dest.read_bytes(), data)

    def test_short_silent_and_unreachable_peers_fall_back(self):
        data = os.urandom(5000)
        sha, _ = put_product(self.peers["172.20.21.197"], data)
        os.environ["FAKE_PEER_MODE_172_20_21_197"] = "short"
        code, record, dest = self.fetch(sha)
        self.assertEqual(code, 1)
        self.assertFalse(dest.exists())
        os.environ["FAKE_PEER_MODE_172_20_21_197"] = "silent"
        lf.TRANSFER_TIMEOUT = 1
        started = time.monotonic()
        code, record, dest = self.fetch(sha)
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(code, 1)
        self.assertIn("timed out", record["error"])
        os.environ["FAKE_PEER_MODE_172_20_21_197"] = "unreachable"
        self.assertEqual(self.fetch(sha)[0], 3)
        self.assertEqual(self.leftovers(), [])

    def test_a_stalled_peer_delays_a_miss_but_not_a_hit(self):
        data = os.urandom(5000)
        sha, _ = put_product(self.peers["172.20.21.198"], data)
        os.environ["FAKE_PEER_MODE_172_20_21_197"] = "stalled"  # listed first, never answers its lookup
        self.addCleanup(setattr, lf, "LOOKUP_TIMEOUT", lf.LOOKUP_TIMEOUT)
        lf.LOOKUP_TIMEOUT = 3
        started = time.monotonic()
        code, record, dest = self.fetch(sha)
        self.assertEqual((code, record["peer"]), (0, "cmux14"))
        self.assertEqual(dest.read_bytes(), data)
        self.assertLess(time.monotonic() - started, 2.5)
        started = time.monotonic()
        self.assertEqual(self.fetch("2" * 64, "other")[0], 3)
        self.assertGreaterEqual(time.monotonic() - started, 3)

    def test_dest_is_never_replaced(self):
        sha, _ = put_product(self.peers["172.20.21.197"], b"x" * 100)
        (self.out / "taken").write_text("mine")
        code, record, dest = self.fetch(sha, "taken")
        self.assertEqual(code, 1)
        self.assertEqual(dest.read_text(), "mine")
        self.assertEqual(lf.fetch(sha, Path("relative"), lf.load_config(self.conf))[0], 1)
        self.assertEqual(lf.fetch("not-a-sha", self.out / "x", lf.load_config(self.conf))[0], 1)

    def test_no_config_is_a_miss_and_bad_configs_are_refused(self):
        self.assertEqual(lf.fetch("1" * 64, self.out / "x", None)[0], 3)
        good = json.loads(self.conf.read_text())
        for bad in ({**good, "peers": [{"name": "a", "address": "-oProxyCommand=x"}]}, {**good, "peers": []},
                    {**good, "user": "a b"}, {**good, "schema": 2}):
            with self.subTest(bad=str(bad)[:40]):
                self.conf.write_text(json.dumps(bad))
                self.assertIsNone(lf.load_config(self.conf))

    def test_ssh_pins_each_peers_host_key_and_forwards_nothing(self):
        config = lf.load_config(self.conf)
        argv = lf.ssh_command(config, config["peers"][0], "ping-v1")
        self.assertEqual(argv[:4], [lf.SSH, "-F", "/dev/null", "-T"])
        for option in ("HostKeyAlias=cmux13s-mac-mini", "StrictHostKeyChecking=yes", "IdentityAgent=none",
                       "ClearAllForwardings=yes", "GlobalKnownHostsFile=/dev/null", "BatchMode=yes"):
            self.assertIn(option, argv)

    def test_a_peer_announcing_more_than_the_callers_bound_is_not_asked(self):
        data = os.urandom(5000)
        sha, _ = put_product(self.peers["172.20.21.197"], data)
        code, record = lf.fetch(sha, self.out / "small", lf.load_config(self.conf), max_bytes=4999)
        self.assertEqual((code, record["reason"]), (3, "no peer has it"))
        self.assertEqual(lf.fetch(sha, self.out / "exact", lf.load_config(self.conf), max_bytes=5000)[0], 0)
        self.assertEqual(lf.fetch(sha, self.out / "x", lf.load_config(self.conf), max_bytes=0)[0], 1)

    def test_at_most_two_peers_are_tried_and_one_deadline_bounds_the_request(self):
        data = os.urandom(3000)
        sha = hashlib.sha256(data).hexdigest()
        third = Path(self.tmp.name) / "p3"
        (third / "objects").mkdir(parents=True)
        peers = {**{a: os.fspath(r) for a, r in self.peers.items()}, "172.20.21.199": os.fspath(third)}
        os.environ["FAKE_PEERS"] = json.dumps(peers)
        conf = json.loads(self.conf.read_text())
        conf["peers"].append({"name": "cmux11s-mac-mini", "address": "172.20.21.199"})
        self.conf.write_text(json.dumps(conf))
        for root in (self.peers["172.20.21.197"], self.peers["172.20.21.198"], third):
            put_product(Path(root), os.urandom(3000), digest=sha)  # everyone serves wrong bytes
        code, record, _ = self.fetch(sha)
        self.assertEqual(code, 1)
        transfers = [json.loads(l)[-1] for l in self.log.read_text().splitlines() if "product-v1" in l]
        self.assertEqual(len(transfers), lf.MAX_OFFERS)
        # A deadline already spent: no lookups get to run, and nothing is transferred.
        started = time.monotonic()
        code, record = lf.fetch(sha, self.out / "late", lf.load_config(self.conf), deadline=time.monotonic() - 1)
        self.assertEqual(code, 3)
        self.assertLess(time.monotonic() - started, 5)
        self.assertLess(lf.REQUEST_DEADLINE, 200)  # cmux waits 200 s for the helper

    def test_partial_files_are_unique_per_thread(self):
        sha, _ = put_product(self.peers["172.20.21.197"], os.urandom(100))
        names = []
        real_open = lf.os.open

        def spy(path, flags, *args, **kwargs):
            if ".lan-" in os.fspath(path):
                names.append(Path(path).name)
            return real_open(path, flags, *args, **kwargs)

        with unittest.mock.patch.object(lf.os, "open", spy):
            self.fetch(sha, "one")
            self.fetch(sha, "two")
        self.assertEqual(len(names), 2)
        for name in names:
            self.assertRegex(name, rf"^\.(one|two)\.lan-{os.getpid()}-{threading.get_ident()}-[0-9a-f]{{12}}$")

    def start_broker(self):
        sock = Path(tempfile.mkdtemp(prefix="glf", dir="/tmp")) / "s" / "fetch.sock"  # AF_UNIX path limit
        saved = lf.LAN_CONFIG
        lf.LAN_CONFIG = self.conf
        self.addCleanup(setattr, lf, "LAN_CONFIG", saved)
        ready = threading.Event()
        threading.Thread(target=lf.serve_local, args=(sock, ready), daemon=True).start()
        self.assertTrue(ready.wait(10))
        return sock

    def test_an_idle_broker_reexecutes_itself_once_its_file_changes(self):
        copy = Path(self.tmp.name) / "glaeda-lan-fetch-copy"
        copy.write_text("v1")
        sock = Path(tempfile.mkdtemp(prefix="glf", dir="/tmp")) / "s" / "fetch.sock"
        saved = lf.RELOAD_CHECK_SECONDS
        lf.RELOAD_CHECK_SECONDS = 0.1
        self.addCleanup(setattr, lf, "RELOAD_CHECK_SECONDS", saved)
        calls, ready = [], threading.Event()
        broker = threading.Thread(target=lf.serve_local, kwargs={"sock_path": sock, "ready": ready, "me": copy,
                                  "reexec": lambda *argv: calls.append(argv)}, daemon=True)
        broker.start()
        self.assertTrue(ready.wait(10))
        time.sleep(0.5)
        self.assertEqual(calls, [])  # unchanged: keeps serving
        os.utime(copy, (time.time() + 5, time.time() + 5))
        broker.join(10)
        self.assertFalse(broker.is_alive())
        self.assertEqual(calls[0][1][-2:], [os.fspath(copy), "serve-local"])
        self.assertFalse(sock.exists())

    def test_the_broker_stops_a_fetch_whose_client_left(self):
        sha, _ = put_product(self.peers["172.20.21.197"], os.urandom(100))
        os.environ["FAKE_PEER_MODE_172_20_21_197"] = "silent"
        pidfile = Path(self.tmp.name) / "silent.pid"
        os.environ["FAKE_PIDFILE"] = os.fspath(pidfile)
        sock = self.start_broker()
        import socket
        client = socket.socket(socket.AF_UNIX)
        client.connect(os.fspath(sock))
        client.sendall((json.dumps({"verb": "product", "sha256": sha, "dest": os.fspath(self.out / "gone")}) + "\n").encode())
        deadline = time.time() + 20
        while not (pidfile.exists() and pidfile.read_text()) and time.time() < deadline:
            time.sleep(0.1)
        pid = int(pidfile.read_text())
        client.close()  # the job gave up
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.kill(pid, 9)
            self.fail("the transfer outlived its client")
        self.assertFalse((self.out / "gone").exists())

    def test_the_broker_answers_busy_past_its_connection_bound(self):
        sha, _ = put_product(self.peers["172.20.21.197"], os.urandom(100))
        os.environ["FAKE_PEER_MODE_172_20_21_197"] = "silent"
        with unittest.mock.patch.object(lf, "BROKER_THREADS", 1):
            sock = self.start_broker()
            import socket
            holder = socket.socket(socket.AF_UNIX)
            holder.connect(os.fspath(sock))
            holder.sendall((json.dumps({"verb": "product", "sha256": sha, "dest": os.fspath(self.out / "h")}) + "\n").encode())
            time.sleep(1)
            started = time.monotonic()
            code, record = lf.broker_request(sha, self.out / "second", sock)
            self.assertEqual((code, record.get("error")), (1, "broker busy"))
            self.assertLess(time.monotonic() - started, 5)
            holder.close()

    def test_the_broker_fetches_for_a_job_over_its_socket(self):
        data = os.urandom(20000)
        sha, _ = put_product(self.peers["172.20.21.197"], data)
        sock = Path(tempfile.mkdtemp(prefix="glf", dir="/tmp")) / "s" / "fetch.sock"  # AF_UNIX path limit
        saved = lf.LAN_CONFIG
        lf.LAN_CONFIG = self.conf
        ready = threading.Event()
        threading.Thread(target=lf.serve_local, args=(sock, ready), daemon=True).start()
        try:
            self.assertTrue(ready.wait(10))
            self.assertEqual(sock.parent.stat().st_mode & 0o777, 0o700)
            code, record = lf.broker_request(sha, self.out / "via-broker", sock)
            self.assertEqual((code, record.get("via"), record.get("peer")), (0, "broker", "cmux13s-mac-mini"))
            self.assertEqual((self.out / "via-broker").read_bytes(), data)
            self.assertEqual(lf.broker_request("2" * 64, self.out / "miss", sock)[0], 3)
            import socket
            with socket.socket(socket.AF_UNIX) as raw:
                raw.connect(os.fspath(sock))
                raw.sendall(b'{"verb": "shell", "cmd": "id"}\n')
                self.assertEqual(json.loads(raw.recv(4096))["code"], 1)
        finally:
            lf.LAN_CONFIG = saved
        self.assertIsNone(lf.broker_request(sha, self.out / "x", sock.parent / "absent.sock"))

    def test_the_cli_prints_one_record_and_exits_with_the_code(self):
        data = os.urandom(1000)
        sha, _ = put_product(self.peers["172.20.21.197"], data)
        script = f"""
import importlib.machinery, sys
from pathlib import Path
m = importlib.machinery.SourceFileLoader('lf', {os.fspath(ROOT / 'scripts/glaeda-lan-fetch')!r}).load_module()
m.SSH = {lf.SSH!r}; m.LAN_CONFIG = Path({os.fspath(self.conf)!r}); m.SOCKET = Path('/nonexistent/sock')
sys.exit(m.main(sys.argv[1:]))
"""
        proc = subprocess.run([sys.executable, "-c", script, "product", sha, os.fspath(self.out / "cli")],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["peer"], "cmux13s-mac-mini")
        proc = subprocess.run([sys.executable, "-c", script, "product", "3" * 64, os.fspath(self.out / "cli2")],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 3)

    def test_the_helper_runs_apples_python_isolated(self):
        first = (ROOT / "scripts/glaeda-lan-fetch").read_text().splitlines()[0]
        self.assertEqual(first, "#!/usr/bin/python3 -I")


def put_state(state: Path, k: int, stamp: dict, files: dict[str, bytes], slot: str = "kept") -> str:
    """A cmux kept compile-admission state for root K; returns its stamp's sha256."""
    store = state if k == 1 else state / f"cmux-ci-{k}"
    if slot != "kept":
        store = store / "pr-builds" / slot
    derived = store / "derived-data"
    for name, data in files.items():
        (derived / name).parent.mkdir(parents=True, exist_ok=True)
        (derived / name).write_bytes(data)
    derived.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(stamp, sort_keys=True).encode()
    (store / "stamp.json").write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


STAMP = {"fingerprint": "a" * 32 + "-owned-rec1", "merged_onto": "b" * 40, "pr": 13504,
         "pr_app_swift_files": ["Sources/A.swift"], "pr_app_swift_total": 1, "pr_package_interface": False}


class MeshStateTest(unittest.TestCase):
    """inventory-v1, state-v1 and the fetch_state pull (build mesh)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(os.path.realpath(self.tmp.name))
        self.peer_state = base / "peer-ci"
        self.peer_products = base / "peer-products"
        (self.peer_products / "objects").mkdir(parents=True)
        self.local = base / "local-ci"
        self.local.mkdir()
        conf = base / "lan-mesh"
        conf.mkdir()
        (conf / "id_ed25519").write_text("k")
        (conf / "known_hosts").write_text("x")
        (conf / "config.json").write_text(json.dumps({"schema": 1, "user": "cmux", "peers": [
            {"name": "cmux9s-mac-mini", "address": "172.20.21.192"}]}))
        self.config = lf.load_config(conf / "config.json")
        fake = base / "fake-ssh"
        fake.write_text(f"#!{sys.executable}\n" + FAKE_SSH)
        fake.chmod(0o755)
        self.saved = (lf.SSH, dict(os.environ))
        lf.SSH = os.fspath(fake)
        os.environ.update({"FAKE_PEERS": json.dumps({"172.20.21.192": os.fspath(self.peer_products)}),
                           "FAKE_STATES": json.dumps({"172.20.21.192": os.fspath(self.peer_state)}),
                           "FAKE_SERVE": os.fspath(SERVE), "FAKE_SSH_LOG": os.fspath(base / "ssh.log")})
        self.receipt = base / "receipt.json"

    def tearDown(self) -> None:
        lf.SSH, env = self.saved
        os.environ.clear()
        os.environ.update(env)
        self.tmp.cleanup()

    def pull(self, k: int, slot: str, want: str) -> tuple[int, dict]:
        return lf.fetch_state("cmux9s-mac-mini", k, slot, want, self.config, state_dir=self.local,
                              receipt=self.receipt)

    def test_inventory_lists_kept_and_parked_states_products_and_seeds(self):
        one = put_state(self.peer_state, 1, STAMP, {"Build/a.o": b"x" * 100, "Logs/big.log": b"y" * 5000})
        two = put_state(self.peer_state, 2, {**STAMP, "pr": 14772}, {"b.o": b"z" * 10}, slot="pr-14772")
        sha, _ = put_product(self.peer_products, b"product")
        key = "admission-derived-data-v1-macOS-ARM64-" + "a" * 32 + "-j14-" + "b" * 40
        (self.peer_state / "seeds" / key).mkdir(parents=True)
        (self.peer_state / "seeds" / key / serve.MANIFEST).write_text("{}")
        (self.peer_state / "cmux-ci-2" / "stamp.json").write_text("not json")  # a broken stamp is skipped
        index = lf.refresh_index(self.config, self.local / "index.json")
        inv = index["peers"]["cmux9s-mac-mini"]["inventory"]
        self.assertTrue(index["peers"]["cmux9s-mac-mini"]["ok"])
        roots = {(r["root"], r["slot"]): r for r in inv["roots"]}
        self.assertEqual(set(roots), {(1, "kept"), (2, "pr-14772")})
        self.assertEqual(roots[(1, "kept")]["stamp_sha256"], one)
        self.assertEqual(roots[(1, "kept")]["bytes"], 100)  # Logs left out
        self.assertEqual((roots[(1, "kept")]["pr"], roots[(2, "pr-14772")]["stamp_sha256"]), (13504, two))
        self.assertEqual((inv["products"], inv["seeds"]), ([sha], [key]))
        self.assertEqual(lf.read_index(self.local / "index.json")["peers"].keys(), {"cmux9s-mac-mini"})

    def test_a_pull_installs_the_peers_state_into_the_same_root(self):
        want = put_state(self.peer_state, 2, STAMP, {"Build/x.o": os.urandom(200000), "Index.noindex/i": b"i"})
        old = put_state(self.local, 2, {**STAMP, "pr": 1}, {"old.o": b"old"})
        (self.local / "cmux-ci-2" / "seeds").mkdir()  # other store entries stay
        code, record = self.pull(2, "kept", want)
        self.assertEqual(code, 0, record)
        store = self.local / "cmux-ci-2"
        self.assertEqual(hashlib.sha256((store / "stamp.json").read_bytes()).hexdigest(), want)
        self.assertEqual((store / "derived-data/Build/x.o").read_bytes(),
                         (self.peer_state / "cmux-ci-2/derived-data/Build/x.o").read_bytes())
        self.assertFalse((store / "derived-data/old.o").exists())
        self.assertFalse((store / "derived-data/Index.noindex").exists())
        self.assertTrue((store / "seeds").is_dir())
        self.assertNotEqual(old, want)
        leftovers = [p.name for p in self.local.iterdir() if p.name.startswith(".")]
        leftovers += [p.name for p in self.peer_state.iterdir() if p.name.startswith(".lan-serve")]
        self.assertEqual(leftovers, [])

    def test_a_changed_or_missing_state_is_a_miss_and_nothing_changes(self):
        put_state(self.peer_state, 1, STAMP, {"a.o": b"a"})
        kept = put_state(self.local, 1, {**STAMP, "pr": 7}, {"mine.o": b"m"})
        code, record = self.pull(1, "kept", "9" * 64)
        self.assertEqual(code, 3, record)
        self.assertEqual(self.pull(3, "kept", "9" * 64)[0], 3)
        self.assertEqual(hashlib.sha256((self.local / "stamp.json").read_bytes()).hexdigest(), kept)
        self.assertTrue((self.local / "derived-data/mine.o").exists())

    def test_a_trusted_mini_never_takes_pr_state(self):
        want = put_state(self.peer_state, 1, STAMP, {"a.o": b"a"})
        self.receipt.write_text(json.dumps({"member": {"trustedRef": "refs/heads/main"}}))
        code, record = self.pull(1, "kept", want)
        self.assertEqual(code, 1)
        self.assertIn("trusted", record["error"])
        self.assertFalse((self.local / "derived-data").exists())

    def test_bad_requests_are_refused(self):
        for k, slot, want in ((0, "kept", "a" * 64), (9, "kept", "a" * 64), (1, "../x", "a" * 64), (1, "kept", "x")):
            with self.subTest(k=k, slot=slot):
                self.assertEqual(self.pull(k, slot, want)[0], 1)
        self.assertEqual(lf.fetch_state("stranger", 1, "kept", "a" * 64, self.config, state_dir=self.local,
                                        receipt=self.receipt)[0], 1)
        for request in ("state-v1 1 kept " + "a" * 64, "state-v1 01 kept " + "a" * 64 + " tar",
                        "state-v1 1 ../kept " + "a" * 64 + " tar", "inventory-v1 x"):
            with self.subTest(request=request[:30]):
                env = {**os.environ, "SSH_ORIGINAL_COMMAND": request, "SSH_CLIENT": "172.20.21.196 1 22"}
                proc = subprocess.run([sys.executable, os.fspath(SERVE), "--role", "product", "--state",
                                       os.fspath(self.peer_state), "--run-dir", os.fspath(self.local / ".run"),
                                       "--products", os.fspath(self.peer_products)], env=env, capture_output=True)
                self.assertEqual(proc.returncode, 2)
        # The seed role never answers the mesh verbs.
        env = {**os.environ, "SSH_ORIGINAL_COMMAND": "inventory-v1", "SSH_CLIENT": "172.20.21.196 1 22"}
        proc = subprocess.run([sys.executable, os.fspath(SERVE), "--state", os.fspath(self.peer_state),
                               "--run-dir", os.fspath(self.local / ".run2"), "--receipt", "/nonexistent"],
                              env=env, capture_output=True)
        self.assertEqual(proc.returncode, 2)

    def test_a_keep_racing_the_snapshot_is_a_miss(self):
        want = put_state(self.peer_state, 1, STAMP, {"a.o": b"a"})
        real = serve.read_kept
        calls = []

        def racing(directory):
            calls.append(directory)
            got = real(directory)
            return got if len(calls) == 1 else (got[0], got[1] + 1, got[2])  # the inode moved: a keep ran
        with unittest.mock.patch.object(serve, "read_kept", racing):
            self.assertIsNone(serve.snapshot(self.peer_state, self.peer_state, want))
        self.assertEqual([p for p in self.peer_state.iterdir() if p.name.startswith(".lan-serve")], [])


if __name__ == "__main__":
    unittest.main()
