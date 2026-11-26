#!/usr/bin/env python3
"""The shared intent layer in the glaeda CLIs: parser-level, no network, no daemon.

Leo, 2026-11-23: "we need to have really powerful aliasing for intent." Each miss measured in this host's agent
transcripts (2026-11-16..23) runs as the form it meant and says so; a write with a typo is refused with its exact form
and nothing runs; a form that parses is untouched; the vendored copy of leo-workspace tools/intent.py and its table is
byte-identical to what intent.sha256 records (scripts/intent-sync refreshes both).
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def load_script(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, os.fspath(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    sys.modules[name] = module
    loader.exec_module(module)
    return module


gg = load_script("glaeda_gh_intent", SCRIPTS / "glaeda-gh")
gd = load_script("glaeda_disk_intent", SCRIPTS / "glaeda-disk")

def line(*argv: str) -> str:
    """ARGV as the layer prints it: shell-quoted, so it runs when pasted (`'o/r#1'`: `#` starts a comment)."""
    return shlex.join(["glaeda-gh", *argv])


def layer():
    module = gg.intent_layer()
    assert module is not None, "scripts/intent.py loads beside glaeda-gh"
    return module


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        intent = layer()
        self.table = json.loads((SCRIPTS / "intent_aliases.json").read_text())
        patches = [mock.patch.object(gg, "INTENT_ON", True), mock.patch.object(intent, "LOG",
                                                                               os.path.join(self.tmp.name, "log.jsonl")),
                   mock.patch.object(intent, "_table_cache", self.table), mock.patch.dict(os.environ, {}, clear=False)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("INTENT_OFF", None)
        os.environ.pop("INTENT_TABLE", None)

    def parse(self, argv: list[str]) -> tuple[argparse.Namespace | None, str, int | None]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), mock.patch.object(gg, "say_waiting",
                                                               lambda line: print(line, file=sys.stderr)):
            try:
                return gg.parse_with_intent(list(argv)), err.getvalue(), None
            except SystemExit as stop:
                return None, err.getvalue(), stop.code


class MeasuredMisses(Base):
    """Each measured miss runs as its intended form and says so, or (where nothing is safe to assume) is refused
    with the form that runs."""

    def test_get_pr_runs_as_status_pr(self) -> None:
        args, err, code = self.parse(["get", "pr", "o/r#1"])
        self.assertEqual((code, args.command, args.kind, args.target), (None, "status", "pr", ["o/r#1"]))
        self.assertIn(f"glaeda-gh: ran `{line('status', 'pr', 'o/r#1')}` for `{line('get', 'pr', 'o/r#1')}`", err)

    def test_log_with_the_kind_last_runs_as_log_job(self) -> None:
        args, err, code = self.parse(["log", "o/r", "123", "job"])
        self.assertEqual((code, args.kind, args.repo, args.job), (None, "job", "o/r", 123))
        self.assertIn(f"ran `{line('log', 'job', 'o/r', '123')}`", err)

    def test_log_with_no_kind_and_no_default_runs_the_best_read_and_names_the_others(self) -> None:
        """a805a3c4: several read-only meanings (log's kinds are glaeda-gh `safe`): the best runs, the others named."""
        self.table["cli"]["glaeda-gh"].pop("default")
        args, err, code = self.parse(["log", "o/r", "123"])
        self.assertEqual((code, args.kind), (None, "job"))
        self.assertIn("read-only forms; also", err)

    def test_log_with_no_kind_runs_as_log_job_by_the_table_default(self) -> None:
        args, err, code = self.parse(["log", "o/r", "123"])
        self.assertEqual((code, args.kind, args.job), (None, "job", 123))
        self.assertIn(f"ran `{line('log', 'job', 'o/r', '123')}`", err)

    def test_status_with_repo_names_the_repository_of_a_bare_number(self) -> None:
        args, err, code = self.parse(["status", "pr", "2467", "--repo", "o/r"])
        self.assertEqual((code, err), (None, ""), "a real flag now: no repair, nothing said")
        self.assertEqual(gg.repo_targets(args.kind, args.target, args.repo), ["o/r#2467"])
        self.assertEqual(gg.repo_targets("run", "5", "o/r"), "o/r/5")
        self.assertEqual(gg.repo_targets("pr", ["o/x#1", "#7"], "o/r"), ["o/x#1", "o/r#7"])
        with self.assertRaises(gg.WatchError):
            gg.repo_targets("pr", ["7"], "not-a-repo")
        args, err, code = self.parse(["status", "pr", "2467", "--repository", "o/r"])
        self.assertEqual((code, args.repo), (None, "o/r"))
        self.assertIn("ran `glaeda-gh status pr 2467 --repo o/r`", err)

    def test_land_status_glued_runs_as_land_status_and_says_so(self) -> None:
        """The table's alias `land-status` -> `land status` (60a81f74: a read under a write's word applies), run
        through intent.alias: the land status read runs, the land write never does."""
        args, err, code = self.parse(["land-status", "o/r#1"])
        self.assertEqual((code, args.command, args.words), (None, "land", ["status", "o/r#1"]))
        self.assertIn(f"ran `{line('land', 'status', 'o/r#1')}` for `{line('land-status', 'o/r#1')}`", err)
        out = io.StringIO()
        with contextlib.redirect_stderr(out), mock.patch.object(gg, "INTENT_ON", True), \
                mock.patch.object(gg, "cmd_land_status", return_value=0) as read, \
                mock.patch.object(gg, "cmd_land", side_effect=AssertionError("must not land")), \
                mock.patch.object(gg, "cmd_land_cancel", side_effect=AssertionError("must not cancel")):
            self.assertEqual(gg.main(["land-status", "o/r#1"]), 0)
        read.assert_called_once()

    def test_land_dash_dash_status_keeps_its_refusal(self) -> None:
        args, err, code = self.parse(["land", "--status", "o/r#1"])
        self.assertEqual((args, code), (None, 64))
        self.assertIn("unrecognized arguments: --status", err)

    def test_checks_without_a_commit_names_the_form_that_runs(self) -> None:
        with mock.patch.dict(os.environ, {"ACPMUX_SESSION_NAME": "lane-x"}):
            args, err, code = self.parse(["checks", "o/r"])
        self.assertEqual((args, code), (None, 64))
        self.assertIn('next: `glaeda-gh checks o/r "$(git rev-parse HEAD)" --consumer lane-x`', err)
        self.assertIn("`glaeda-gh status pr o/r#N`", err)


class Writes(Base):
    """A write never runs from a form nobody typed: the exact form is named and nothing runs."""

    def test_a_write_with_a_typo_is_refused_with_its_exact_form(self) -> None:
        for argv, exact in ((["land", "o/r#1", "--jsn"], line("land", "o/r#1", "--json")),
                            (["post", "comment", "o/r#1", "--bdy", "hi"], line("post", "comment", "o/r#1", "--body", "hi")),
                            (["restart", "--forse"], line("restart", "--force"))):
            with self.subTest(argv=argv):
                args, err, code = self.parse(argv)
                self.assertEqual((args, code), (None, 64))
                self.assertIn("is destructive or outward, so nothing ran: the exact form is needed", err)
                self.assertIn(f"  {exact}", err)

    def test_a_write_with_a_typo_is_refused_through_main_too(self) -> None:
        """2026-11-23: `land o/r#1 --jsn` queued a land: glaeda-gh's own flag_near_miss_synonym, which runs in
        main before any parse, read `--jsn` as `--json`. The whole call path is what must refuse."""
        for argv, exact, cmd in ((["land", "o/r#1", "--jsn"], "glaeda-gh land o/r#1 --json", "cmd_land"),
                                 (["post", "comment", "o/r#1", "--bdy", "hi"],
                                  "glaeda-gh post comment o/r#1 --body hi", "cmd_post"),
                                 (["restart", "--forse"], "glaeda-gh restart --force", "cmd_restart")):
            with self.subTest(argv=argv):
                err = io.StringIO()
                with contextlib.redirect_stderr(err), mock.patch.object(
                        gg, cmd, side_effect=AssertionError("must not run")):
                    self.assertEqual(gg.main(list(argv)), 64)
                self.assertIn("the exact form is needed", err.getvalue())
                self.assertIn(exact, err.getvalue())

    def test_a_read_under_a_write_command_still_takes_its_typo(self) -> None:
        fixed, refusal = gg.flag_near_miss_synonym(["post", "status", "ID", "--jsn"])
        self.assertEqual((fixed, refusal), (["post", "status", "ID", "--json"], []))

    def test_no_write_is_ever_a_read_only_best_guess(self) -> None:
        """a805a3c4 runs the best of several read-only meanings: no glaeda-gh write is ever read-only to it."""
        intent, parser = layer(), gg.parser()
        for argv in (["land", "o/r#1"], ["land", "cancel", "o/r#1"], ["post", "comment", "o/r#1", "--body", "hi"],
                     ["post", "close", "o/r#1"], ["restart"], ["serve"], ["on-event", "replay"]):
            with self.subTest(argv=argv):
                self.assertFalse(intent._read_only(parser, argv, self.table, "glaeda-gh"))
        args, err, code = self.parse(["land", "cancel", "o/r#1", "--jsn"])
        self.assertEqual((args, code), (None, 64))
        self.assertIn("nothing ran", err)

    def test_a_misspelled_write_command_is_refused_not_run(self) -> None:
        """2026-11-23: `lnad o/r#1` queued a land, read as `land` by command_spelling_synonym."""
        for typed, meant in (("lnad", "land"), ("pots", "post"), ("restrat", "restart")):
            with self.subTest(typed=typed):
                fixed, refusal = gg.command_spelling_synonym([typed, "o/r#1"])
                self.assertEqual(fixed, [typed, "o/r#1"])
                self.assertIn("the exact form is needed", refusal[0])
                self.assertEqual(refusal[1], f"next: glaeda-gh {meant} o/r#1")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), mock.patch.object(gg, "cmd_land",
                                                                side_effect=AssertionError("must not land")):
            self.assertEqual(gg.main(["lnad", "o/r#1"]), 64)
        self.assertIn("next: glaeda-gh land o/r#1", err.getvalue())

    def test_the_proposed_exact_entries_hold_the_writes_the_unsafe_words_miss(self) -> None:
        intent = layer()
        for path in (["on-event", "replay", "--run-file", "/tmp/x"], ["serve", "--foreground-ok"]):
            with self.subTest(path=path):
                self.assertIsNotNone(intent._unsafe(gg.parser(), path, self.table, "glaeda-gh"))  # noqa: SLF001
        for word in gg.WRITE_COMMANDS:
            self.assertIsNotNone(intent._unsafe(gg.parser(), [word], self.table, "glaeda-gh"), word)  # noqa: SLF001


class Untouched(Base):
    def test_a_form_that_parses_is_untouched_and_silent(self) -> None:
        for argv in (["status", "pr", "o/r#1"], ["log", "job", "o/r", "1"], ["land", "o/r#1"],
                     ["checks", "o/r", "a" * 40, "--consumer", "x"]):
            with self.subTest(argv=argv):
                args, err, code = self.parse(argv)
                self.assertEqual((code, err), (None, ""))
                self.assertEqual(args.command, argv[0])

    def test_off_when_not_the_program_when_intent_off_and_when_the_layer_breaks(self) -> None:
        for patch in (mock.patch.object(gg, "INTENT_ON", False), mock.patch.dict(os.environ, {"INTENT_OFF": "1"})):
            with self.subTest(patch=patch), patch:
                args, err, code = self.parse(["get", "pr", "o/r#1"])
                self.assertEqual((args, code), (None, 64))
                self.assertIn("invalid choice: 'get'", err)
                self.assertNotIn("ran `", err)
        broken = Path(self.tmp.name) / "broken"
        broken.mkdir()
        (broken / "intent.py").write_text("raise RuntimeError('broken layer')\n")
        with mock.patch.object(gg, "INTENT_DIRS", (broken,)), mock.patch.object(gg, "_intent_module", []):
            args, err, code = self.parse(["get", "pr", "o/r#1"])
        self.assertEqual((args, code), (None, 64), "a layer that fails to load costs the call nothing")
        self.assertIn("invalid choice: 'get'", err)


class Disk(Base):
    """glaeda-disk installs the layer for every argparse parser of its process (only as the program)."""

    def run_disk(self, argv: list[str]) -> tuple[argparse.Namespace | None, str, int | None]:
        intent = layer()
        caught: list = []

        class Parsed(Exception):
            pass

        gd.install_intent("glaeda-disk")
        loaded = sys.modules.get("intent")  # install_intent loads its own copy: the one whose argparse hooks run
        self.addCleanup(lambda: [m.uninstall() for m in {id(x): x for x in (intent, loaded) if x}.values()])
        wrapped = argparse.ArgumentParser.parse_args

        def capture(parser, args=None, namespace=None):
            caught.append(wrapped(parser, args, namespace))
            raise Parsed

        err = io.StringIO()
        with contextlib.redirect_stderr(err), mock.patch.object(argparse.ArgumentParser, "parse_args", capture), \
                mock.patch.dict(os.environ, {"HOME": self.tmp.name}):
            try:
                gd._main_impl(list(argv))  # noqa: SLF001
            except Parsed:
                return caught[0], err.getvalue(), None
            except SystemExit as stop:
                return None, err.getvalue(), stop.code
        return None, err.getvalue(), None

    def test_status_runs_as_report(self) -> None:
        args, err, code = self.run_disk(["status"])
        self.assertEqual((code, args.command), (None, "report"))
        self.assertIn("glaeda-disk: ran `glaeda-disk report` for `glaeda-disk status`", err)

    def test_apply_with_a_typo_is_refused(self) -> None:
        args, err, code = self.run_disk(["keepup", "--aply"])
        self.assertEqual((args, code), (None, 2))
        self.assertIn("  glaeda-disk keepup --apply", err)
        self.assertIn("the exact form is needed", err)


class Drift(unittest.TestCase):
    def test_vendored_copies_match_the_recorded_sha256(self) -> None:
        """scripts/intent.py and intent_aliases.json are leo-workspace's tools/ copies byte for byte: a hand edit
        here fails this; `scripts/intent-sync LEO_WORKSPACE_CHECKOUT` refreshes both files and the record."""
        recorded = dict(reversed(line.split()) for line in (SCRIPTS / "intent.sha256").read_text().splitlines()
                        if line.strip())
        for name in ("intent.py", "intent_aliases.json", "intent_vectors.json"):
            self.assertEqual(hashlib.sha256((SCRIPTS / name).read_bytes()).hexdigest(), recorded.get(name), name)
        done = subprocess.run([sys.executable, os.fspath(SCRIPTS / "intent-sync"), "--check"], capture_output=True,
                              text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_synced_table_carries_the_glaeda_entries(self) -> None:
        """They live upstream (leo-workspace tools/intent_aliases.json) and arrive by scripts/intent-sync only."""
        cli = json.loads((SCRIPTS / "intent_aliases.json").read_text())["cli"]
        self.assertIn({"from": ["land-status"], "to": ["land", "status"]}, cli["glaeda-gh"]["aliases"])
        self.assertEqual(cli["glaeda-gh"]["default"], {"log": "job"})
        self.assertTrue({"on-event", "serve"} <= set(cli["glaeda-gh"]["exact"]))
        self.assertIn("keepup", cli["glaeda-disk"]["exact"])


if __name__ == "__main__":
    unittest.main()
