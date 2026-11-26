#!/usr/bin/env python3
"""Focused contract tests for the idle repair queue."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("idle_repair", str(ROOT / "scripts/glaeda-idle-repair"))
spec = importlib.util.spec_from_loader("idle_repair", loader)
idle = importlib.util.module_from_spec(spec)
loader.exec_module(idle)


class IdleRepairTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        idle.HOME = home
        idle.STATE_DIR = home / ".local/state/glaeda/idle-repair"
        idle.STATE = idle.STATE_DIR / "queue.json"
        idle.RECEIPT = idle.STATE_DIR / "receipts.jsonl"
        idle.LOCK = idle.STATE_DIR / "run.lock"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_systemd_unit_counts_a_queued_run_as_success(self) -> None:
        # A queued run (busy host, or another run holds the lock) is the queue working; the unit must not show as
        # failed for it, or a failed-units detector pages for every busy minute (2026-11-22).
        unit = (ROOT / "ops/systemd/glaeda-idle-repair.service").read_text()
        statuses = [line.split("=", 1)[1].split() for line in unit.splitlines() if line.startswith("SuccessExitStatus=")]
        self.assertIn(str(idle.QUEUED_EXIT), [code for line in statuses for code in line])

    def test_busy_host_is_queued_without_running_repairs(self) -> None:
        disk = mock.Mock()
        disk.host_busy.return_value = "a build is running"
        with mock.patch.object(idle, "load_disk_module", return_value=disk), \
             mock.patch.object(idle, "run") as run:
            self.assertEqual(idle.main(["--apply"]), idle.QUEUED_EXIT)
        run.assert_not_called()
        state = json.loads(idle.STATE.read_text())
        self.assertEqual(state["state"], "queued")
        self.assertEqual(state["reason"], "a build is running")

    def test_idle_host_runs_disk_then_health_and_records_receipt(self) -> None:
        disk = mock.Mock()
        disk.host_busy.return_value = ""
        with mock.patch.dict(idle.os.environ, {"GLAEDA_ROLE": "devbox"}), \
             mock.patch.object(idle, "load_disk_module", return_value=disk), \
             mock.patch.object(idle, "run", side_effect=[(0, "disk ok", ""), (0, "health ok", "")]) as run:
            self.assertEqual(idle.main(["--apply"]), 0)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0][2:4], ["--role", "devbox"])
        state = json.loads(idle.STATE.read_text())
        self.assertEqual(state["state"], "idle")
        self.assertEqual([a["exit"] for a in state["actions"]], [0, 0])
        self.assertEqual([call.args[1] for call in run.call_args_list], [660, 180])

    def test_successful_repair_waits_until_next_attempt(self) -> None:
        disk = mock.Mock()
        disk.host_busy.return_value = ""
        with mock.patch.object(idle, "load_disk_module", return_value=disk) as load, \
             mock.patch.object(idle, "run", side_effect=[(0, "disk ok", ""), (0, "health ok", "")]) as run, \
             mock.patch.object(idle.time, "time", side_effect=[1000, 1001]):
            self.assertEqual(idle.main(["--apply"]), 0)
            self.assertEqual(idle.main(["--apply"]), 0)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(load.call_count, 1, "the not-due tick never loads glaeda-disk")

    def test_tick_before_next_attempt_loads_nothing_and_keeps_the_schedule(self) -> None:
        """14 of 15 ticks are not due: they must not import glaeda-disk (~730 KB) or run its host gate, and a
        busy host must not turn the 15-minute schedule into "queued, due in 60 s"."""
        idle.write_state({"state": "idle", "lastAttemptAt": 1000, "nextAttemptAt": 1900})
        before = idle.STATE.read_bytes()
        with mock.patch.object(idle, "load_disk_module") as load, \
             mock.patch.object(idle, "run") as run, \
             mock.patch.object(idle.time, "time", return_value=1500):
            self.assertEqual(idle.main(["--apply"]), 0)
        load.assert_not_called()
        run.assert_not_called()
        self.assertEqual(idle.STATE.read_bytes(), before)
        self.assertFalse(idle.RECEIPT.exists())

    def test_queued_repair_is_due_on_the_next_tick_despite_launchd_jitter(self) -> None:
        disk = mock.Mock()
        disk.host_busy.side_effect = ["a build is running", ""]
        with mock.patch.object(idle, "load_disk_module", return_value=disk), \
             mock.patch.object(idle, "run", side_effect=[(0, "disk ok", ""), (0, "health ok", "")]) as run, \
             mock.patch.object(idle.time, "time", side_effect=[1000, 1059]):
            self.assertEqual(idle.main(["--apply"]), idle.QUEUED_EXIT)
            self.assertEqual(idle.main(["--apply"]), 0)
        self.assertEqual(disk.host_busy.call_count, 2)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(json.loads(idle.STATE.read_text())["nextAttemptAt"], 1059 + 900)

    def test_timed_out_repair_waits_a_full_interval_after_it_was_stopped(self) -> None:
        disk = mock.Mock()
        disk.host_busy.return_value = ""
        with mock.patch.object(idle, "load_disk_module", return_value=disk), \
             mock.patch.object(idle, "run", return_value=(idle.TIMED_OUT, "", "timed out")), \
             mock.patch.object(idle.time, "time", return_value=1000):
            self.assertEqual(idle.main(["--apply"]), 1)
        state = json.loads(idle.STATE.read_text())
        self.assertEqual((state["state"], state["nextAttemptAt"]), ("retry", 1000 + 660 + idle.REPAIR_EVERY_S))

    def test_other_failure_retries_on_the_next_tick(self) -> None:
        disk = mock.Mock()
        disk.host_busy.return_value = ""
        with mock.patch.object(idle, "load_disk_module", return_value=disk), \
             mock.patch.object(idle, "run", return_value=(1, "", "boom")), \
             mock.patch.object(idle.time, "time", return_value=1000):
            self.assertEqual(idle.main(["--apply"]), 1)
        self.assertEqual(json.loads(idle.STATE.read_text())["nextAttemptAt"], 1000 + idle.RETRY_AFTER_S)

    def test_status_is_read_only(self) -> None:
        idle.write_state({"state": "queued", "reason": "reservation"})
        with mock.patch.object(idle, "load_disk_module") as load:
            self.assertEqual(idle.main(["--status"]), 0)
        load.assert_not_called()

    def test_scheduled_log_keeps_the_newest_whole_records_and_receipts_the_released_bytes(self) -> None:
        path = idle.HOME / "Library/Logs/glaeda-idle-repair.jsonl"
        path.parent.mkdir(parents=True)
        rows = [json.dumps({"schema": idle.SCHEMA, "state": "idle", "actions": [], "lastAttemptAt": n,
                            "nextAttemptAt": n + 900}).encode() + b"\n" for n in range(200)]
        path.write_bytes(b"".join(rows))
        prior = path.stat()
        disk = mock.Mock()
        disk.host_busy.return_value = "test build running"
        source_descriptors = []

        def witness(fd):
            self.assertEqual(idle.log_identity(os.fstat(fd)), idle.log_identity(prior))
            source_descriptors.append(fd)
            return "clear"

        def free_bytes(_parent):
            if not source_descriptors:
                return (1024, 8192)
            with self.assertRaises(OSError):
                os.fstat(source_descriptors[0])
            return (5120, 8192)

        disk.free_bytes.side_effect = free_bytes
        with mock.patch.object(idle, "LOG_MAX_BYTES", 4096), mock.patch.object(idle, "LOG_KEEP_BYTES", 1024), \
                mock.patch.object(disk, "descriptor_holder_status", side_effect=witness), \
                mock.patch.object(idle, "load_disk_module", return_value=disk):
            self.assertEqual(idle.main(["--apply", "--scheduled-log"]), idle.QUEUED_EXIT)
            kept = path.read_bytes()
            self.assertEqual(idle.main(["--apply", "--scheduled-log"]), 0)
            self.assertEqual(path.read_bytes(), kept, "a not-due tick must not replay the last state")
        lines = kept.splitlines(keepends=True)
        self.assertLess(len(kept), 4096)
        self.assertEqual(lines[:-1], rows[-len(lines) + 1:])
        self.assertEqual(json.loads(lines[-1])["state"], "queued")
        record = json.loads(idle.RECEIPT.read_bytes().splitlines()[-1])
        after = path.stat()
        self.assertEqual(record["outcome"], "log-compacted")
        self.assertEqual(record["logicalReleasedBytes"], prior.st_size - after.st_size)
        self.assertEqual(record["allocatedReleasedBytes"], (prior.st_blocks - after.st_blocks) * 512)
        self.assertEqual(record["physicalFreeDeltaBytes"], 4096)

    def test_only_log_reader_requires_complete_own_descriptor_witness(self) -> None:
        path = idle.HOME / "log"
        with mock.patch.dict(os.environ, {"GLAEDA_DISK": str(ROOT / "scripts/glaeda-disk")}):
            disk = idle.load_disk_module()
        with path.open("wb") as source, mock.patch.object(disk, "_owned_query_split") as probe:
            fd = source.fileno()
            st = os.fstat(fd)
            node = f"tREG\nD{st.st_dev:#x}\ni{st.st_ino}\n"
            own = f"p{os.getpid()}\nf{fd}\n{node}"
            probe.return_value = 0, own + "p999999\nf1\nD0x0\ni1\n", ""
            self.assertEqual(disk.descriptor_holder_status(fd), "clear")
            self.assertEqual(probe.call_args.args[0][1:], ["-b", "-w", "-nP", "-FpfDit"])
            for code, output, error, outcome in [(0, "", "", "blocked:lsof-incomplete"),
                    (0, f"p{os.getpid()}\nf{fd}\n", "", "blocked:lsof-incomplete"),
                    (0, own + f"f{fd + 1}\n{node}", "", "changed:in-use"),
                    (0, own + f"f{fd + 1}\ntREG\nD{st.st_dev:#x}\n", "", "blocked:lsof-incomplete"),
                    (0, own + f"p999999\nf1\n{node}", "", "changed:in-use"),
                    (0, own, "lsof: partial listing\n", "blocked:lsof-incomplete"),
                    (1, own, "", "blocked:lsof-incomplete")]:
                with self.subTest(code=code, output=output, error=error):
                    probe.return_value = code, output, error
                    self.assertEqual(disk.descriptor_holder_status(fd), outcome)
            for partial in [f"tREG\nD{st.st_dev:#x}\n", f"tREG\ni{st.st_ino}\n", "tREG\n", "",
                            f"D{st.st_dev:#x}\n", f"i{st.st_ino}\n", f"D{st.st_dev:#x}\ni{st.st_ino}\n"]:
                probe.return_value = 0, own + "p999999\nf1\n" + partial, ""
                for native in ["blocked:lsof-incomplete", "changed:in-use", "clear"]:
                    with self.subTest(partial=partial, native=native), \
                            mock.patch.object(disk, "corroborate_descriptor_holders", return_value=native) as check:
                        self.assertEqual(disk.descriptor_holder_status(fd), native)
                        check.assert_called_once_with(fd)
            # A different known inode or device rules out a match even when another field is missing.
            probe.return_value = 0, own + f"p999999\nf1\ntREG\ni{st.st_ino + 1}\nf2\nD{st.st_dev + 1:#x}\n", ""
            with mock.patch.object(disk, "corroborate_descriptor_holders") as check:
                self.assertEqual(disk.descriptor_holder_status(fd), "clear")
                check.assert_not_called()
            own_pid = str(os.getpid())
            with mock.patch.object(disk, "DARWIN", True), \
                    mock.patch.object(disk.fcntl, "fcntl", return_value=os.fsencode(path) + b"\0"):
                for result, outcome in [((0, own_pid, ""), "clear"),
                        ((0, own_pid + " 999999", ""), "changed:in-use"),
                        ((1, own_pid, ""), "blocked:lsof-incomplete"),
                        ((0, "999999", ""), "blocked:lsof-incomplete"),
                        ((0, "", ""), "blocked:lsof-incomplete"),
                        ((0, own_pid, "warning"), "blocked:lsof-incomplete")]:
                    with self.subTest(native_result=result):
                        probe.return_value = result
                        self.assertEqual(disk.corroborate_descriptor_holders(fd), outcome)
            for failure, outcome in [(OSError(), "blocked:lsof-incomplete"),
                                     (subprocess.TimeoutExpired("lsof", 10), "blocked:query-timeout")]:
                probe.side_effect = failure
                self.assertEqual(disk.descriptor_holder_status(fd), outcome)

    def test_scheduled_log_keeps_foreign_mixed_linked_open_and_changing_legacy_files(self) -> None:
        path = idle.HOME / "Library/Logs/glaeda-idle-repair.jsonl"
        path.parent.mkdir(parents=True)
        value = {"state": "idle", "actions": [], "lastAttemptAt": 1, "nextAttemptAt": 901}
        row = (json.dumps({"schema": idle.SCHEMA, **value}) + "\n").encode()
        disk = mock.Mock()
        disk.free_bytes.return_value = (8192, 8192)
        with mock.patch.object(idle, "LOG_MAX_BYTES", 1), mock.patch.object(idle, "LOG_KEEP_BYTES", 1), \
                mock.patch.object(disk, "descriptor_holder_status", return_value="changed:in-use") as witness:
            for raw in (b"someone else's log\n", row + b"someone else's log\n" + row, row.rstrip()):
                path.write_bytes(raw)
                idle.write_scheduled_log(value, disk)
                self.assertEqual(path.read_bytes(), raw)
            path.write_bytes(row)
            other = path.parent / "existing-log"
            os.link(path, other)
            idle.write_scheduled_log(value, disk)
            self.assertEqual(path.read_bytes(), row)
            path.unlink()
            path.symlink_to(other)
            with self.assertRaises(OSError):
                idle.write_scheduled_log(value, disk)
            self.assertEqual(other.read_bytes(), row)
            path.unlink()
            path.write_bytes(row)
            idle.write_scheduled_log(value, disk)
            self.assertEqual(path.read_bytes(), row)

            def late_write(_path):
                with path.open("ab") as appender:
                    appender.write(row)
                return (8192, 8192)

            witness.return_value = "clear"
            disk.free_bytes.side_effect = late_write
            idle.write_scheduled_log(value, disk)
            self.assertEqual(path.read_bytes(), row * 2)
        self.assertFalse(idle.RECEIPT.exists())


class RunTest(unittest.TestCase):
    def test_timeout_stops_the_whole_process_group(self) -> None:
        """A timed-out repair's children (glaeda-disk's git and du) stop with it; only the group run() made."""
        import os
        import sys
        import time
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "child.pid"
            script = ("import subprocess, sys, time\n"
                      "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                      f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
                      "time.sleep(60)\n")
            started = time.monotonic()
            code, _, err = idle.run([sys.executable, "-c", script], timeout=2)
            self.assertEqual((code, err), (idle.TIMED_OUT, "timed out"))
            self.assertLess(time.monotonic() - started, 20)
            child = int(pid_file.read_text())
            import subprocess
            for _ in range(50):
                try:
                    os.kill(child, 0)
                except ProcessLookupError:
                    break
                stat = subprocess.run(["ps", "-o", "stat=", "-p", str(child)], capture_output=True, text=True).stdout
                if not stat.strip() or stat.strip().startswith("Z"):  # a zombie a container's pid 1 never reaps
                    break
                time.sleep(0.1)
            else:
                self.fail(f"grandchild {child} outlived the timed-out repair")

    def test_output_and_exit_are_returned(self) -> None:
        import sys
        code, out, err = idle.run([sys.executable, "-c", "import sys; print('ok'); sys.stderr.write('e'); sys.exit(3)"], 30)
        self.assertEqual((code, out, err), (3, "ok\n", "e"))


class LoadDiskModuleTest(unittest.TestCase):
    # The tests above mock load_disk_module, so none of them saw it fail on Python 3.15 (SourceFileLoader.load_module
    # was removed): from 10-18 every run failed there, 70,782 tracebacks on one host and nothing alarmed.
    def test_it_loads_an_extensionless_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            disk = Path(tmp) / "glaeda-disk"
            disk.write_text("def host_busy():\n    return None\n")
            with mock.patch.dict("os.environ", {"GLAEDA_DISK": str(disk)}):
                module = idle.load_disk_module()
        self.assertIsNone(module.host_busy())

    def test_no_tracked_file_calls_the_removed_load_module(self) -> None:
        import subprocess
        removed = ".load" + "_module("
        hits = subprocess.run(["git", "-C", str(ROOT), "grep", "-n", "-F", removed, "--", "scripts", ".github"],
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(hits, "", "SourceFileLoader's load_module is gone in Python 3.15; use "
                         "importlib.util.module_from_spec(spec_from_loader(name, loader)) and loader.exec_module:\n"
                         + hits)

    def test_it_loads_a_script_whose_dataclasses_look_up_their_module(self) -> None:
        # 11-17: glaeda-disk grew a @dataclass(frozen=True) under `from __future__ import annotations`; dataclasses
        # reads sys.modules[cls.__module__] while it makes the class, so the unregistered module failed every minute
        # with "'NoneType' object has no attribute '__dict__'".
        with tempfile.TemporaryDirectory() as tmp:
            disk = Path(tmp) / "glaeda-disk"
            disk.write_text("from __future__ import annotations\nfrom dataclasses import dataclass\n"
                            "from typing import ClassVar\n\n\n@dataclass(frozen=True)\nclass Probe:\n"
                            "    kind: ClassVar[str] = 'x'\n    busy: str = ''\n\n\n"
                            "def host_busy():\n    return Probe().busy\n")
            with mock.patch.dict("os.environ", {"GLAEDA_DISK": str(disk)}):
                module = idle.load_disk_module()
        self.assertEqual(module.host_busy(), "")

    def test_it_loads_the_real_glaeda_disk(self) -> None:
        with mock.patch.dict("os.environ", {"GLAEDA_DISK": str(ROOT / "scripts/glaeda-disk")}):
            module = idle.load_disk_module()
        self.assertTrue(callable(module.host_busy))

    def test_every_script_registers_the_module_it_executes(self) -> None:
        # The detector for the class: a script that runs loader.exec_module(m) puts m in sys.modules first, or the
        # first dataclass in the module it loads breaks it. Tests are exempt: their own run shows a miss.
        import re
        import subprocess
        files = subprocess.run(["git", "-C", str(ROOT), "grep", "-l", "-F", ".exec_module(", "--", "scripts", "glaeda"],
                               capture_output=True, text=True).stdout.split()
        missing = []
        for name in files:
            if Path(name).name.startswith("test-"):
                continue
            lines = (ROOT / name).read_text().splitlines()
            for i, line in enumerate(lines):
                if re.match(r"\s*[\w.]+\.exec_module\(\w+\)", line) and \
                        not any("sys.modules[" in prior for prior in lines[max(0, i - 3):i]):
                    missing.append(f"{name}:{i + 1}: {line.strip()}")
            # The registration names sys, so the script imports it (a bare `sys.modules[...]` raises NameError
            # at load, which compiling never shows: cmux_fleet_operator_summary.py, #2129).
            # An import counts at the top level, or earlier at no deeper indent (a function's own import).
            imports = [(i, len(line) - len(line.lstrip())) for i, line in enumerate(lines)
                       if re.match(r"\s*import\s+(?:[\w.]+\s*,\s*)*sys\b", line)]
            for i, line in enumerate(lines):
                if "sys.modules[" in line and not line.lstrip().startswith("#"):
                    indent = len(line) - len(line.lstrip())
                    if not any(depth == 0 or (at < i and depth <= indent) for at, depth in imports):
                        missing.append(f"{name}:{i + 1}: sets sys.modules[...] with no `import sys` in scope")
        self.assertEqual(missing, [], "set sys.modules[loader.name] = module before loader.exec_module(module):\n"
                         + "\n".join(missing))


class BootHoldTest(unittest.TestCase):
    """cosmic-gray, 2026-11-24: after a panic every RunAtLoad job started in the same second (load 214). With
    BOOT_SPREAD_S the first pass after boot or wake waits a fixed 0..S s, seeded by the launchd label and boot."""

    def test_the_wait_is_fixed_per_label_and_boot_and_spreads_labels(self) -> None:
        up = 1_800_000_000.0
        env = {"BOOT_SPREAD_S": "300", "XPC_SERVICE_NAME": "com.teamleaderleo.glaeda.idle-repair"}
        first = idle.boot_hold(env, now=up + 1, up=up)
        self.assertGreater(first, 0)
        self.assertLessEqual(first, 300)
        self.assertAlmostEqual(idle.boot_hold(env, now=up + 11, up=up), max(0.0, first - 10), places=6)
        self.assertEqual(idle.boot_hold(env, now=up + 301, up=up), 0.0, "past the spread it runs")
        waits = {round(idle.boot_hold({**env, "XPC_SERVICE_NAME": f"l{i}"}, now=up, up=up)) for i in range(20)}
        self.assertGreater(len(waits), 10, "labels land apart, not together")

    def test_no_spread_an_unknown_boot_or_a_bad_value_waits_nothing(self) -> None:
        up = 1_800_000_000.0
        self.assertEqual(idle.boot_hold({}, now=up + 1, up=up), 0.0)
        self.assertEqual(idle.boot_hold({"BOOT_SPREAD_S": "x"}, now=up + 1, up=up), 0.0)
        self.assertEqual(idle.boot_hold({"BOOT_SPREAD_S": "300"}, now=up + 1, up=None), 0.0)
        self.assertEqual(idle.boot_hold({"BOOT_SPREAD_S": "300"}, now=up - 5, up=up), 0.0)

    def test_boot_and_wake_are_read_from_one_sysctl(self) -> None:
        out = "{ sec = 1000, usec = 999999 } Tue\n{ sec = 5000, usec = 1 } Tue\n"
        run = mock.Mock(return_value=mock.Mock(stdout=out))
        with mock.patch.object(idle.sys, "platform", "darwin"):
            self.assertEqual(idle.came_up_at(now=6000, run=run), 5000.0)
            run.return_value = mock.Mock(stdout="{ sec = 1000, usec = 5 }\n{ sec = 0, usec = 0 }\n")
            self.assertEqual(idle.came_up_at(now=6000, run=run), 1000.0, "a Mac that never slept: its boot")
        self.assertEqual(run.call_count, 2)

    def test_a_held_tick_exits_before_the_lock_or_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(idle, "STATE_DIR", Path(tmp) / "state"), \
                mock.patch.object(idle, "boot_hold", return_value=42.0), \
                mock.patch.object(idle, "load_disk_module", side_effect=AssertionError("loaded")):
            self.assertEqual(idle.main(["--apply", "--scheduled-log"]), 0)
            self.assertFalse((Path(tmp) / "state").exists())


if __name__ == "__main__":
    unittest.main()
