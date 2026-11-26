#!/usr/bin/env python3
"""Tests for glaeda-disk-registry: the shipped registry parses, the loader refuses what it cannot read exactly, the LRU
plan, and eviction is off without GLAEDA_DISK_REGISTRY=1 and never follows a symlink or touches a fresh unit."""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("glaeda_disk_registry", str(ROOT / "scripts" / "glaeda-disk-registry"))
spec = importlib.util.spec_from_loader(loader.name, loader)
reg = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = reg
loader.exec_module(reg)

DAY = 86400.0
MIB = 1024 * 1024


def doc(**cache) -> dict:
    entry = {"name": "dd", "path": "/tmp/x/{stream}", "owner": "t", "regenerates": True, "budget_gib": 1}
    entry.update(cache)
    return {"schema": reg.SCHEMA, "cache": [entry]}


class LoaderTest(unittest.TestCase):
    def test_shipped_registry_parses_with_the_designs_caches(self):
        registry = reg.load(ROOT / "config" / "disk-registry.toml", host="mini-a")
        names = [c.name for c in registry.caches]
        self.assertEqual(names, ["derived-data", "compile-cache", "simulator-runtimes", "toolchains", "git-mirror"])
        dd = reg.cache_by_name(registry, "derived-data")
        self.assertTrue(dd.per_stream and dd.regenerates)
        self.assertEqual(dd.budget_gib, 120)
        self.assertFalse(reg.cache_by_name(registry, "simulator-runtimes").regenerates)
        self.assertEqual(registry.base_gib, 120)

    def test_host_override_applies_to_that_host_only(self):
        d = doc()
        d["hosts"] = {"mini-a": {"cache": {"dd": {"budget_gib": 5}}}}
        self.assertEqual(reg.parse(d, "mini-a").caches[0].budget_gib, 5)
        self.assertEqual(reg.parse(d, "mini-b").caches[0].budget_gib, 1)

    def test_checker_threshold_defaults_reads_and_overrides_per_host(self):
        self.assertEqual(reg.load(ROOT / "config" / "disk-registry.toml").undeclared_alarm_gib, 5)
        self.assertEqual(reg.parse(doc(), "h").undeclared_alarm_gib, reg.CHECKER_DEFAULTS["undeclared_alarm_gib"])
        d = doc()
        d["checker"] = {"undeclared_alarm_gib": 8}
        d["hosts"] = {"mini-a": {"checker": {"undeclared_alarm_gib": 2}}}
        self.assertEqual(reg.parse(d, "mini-a").undeclared_alarm_gib, 2)
        self.assertEqual(reg.parse(d, "mini-b").undeclared_alarm_gib, 8)
        for bad in ({"nope": 1}, {"undeclared_alarm_gib": "x"}, "x"):
            d = doc()
            d["checker"] = bad
            with self.assertRaises(reg.RegistryError):
                reg.parse(d, "h")

    def test_refuses_what_it_cannot_read_exactly(self):
        bad = [
            {"schema": "other/v1", "cache": doc()["cache"]},
            doc(path="relative/dir"),
            doc(path="/tmp/../etc"),
            doc(path="/x"),
            doc(path="/tmp/{stream}/sub"),
            doc(regenerates="yes"),
            doc(budget_gib=-1),
            doc(min_gib=5),
            doc(evict="fifo"),
            doc(surprise=1),
            doc(name="Bad Name"),
            {"schema": reg.SCHEMA, "cache": []},
        ]
        for d in bad:
            with self.subTest(d=d), self.assertRaises(reg.RegistryError):
                reg.parse(d, "h")

    def test_slot_path_is_a_plain_stream_under_the_template(self):
        cache = reg.parse(doc(), "h").caches[0]
        self.assertEqual(reg.slot_path(cache, "feat-cmux-next"), Path("/tmp/x/feat-cmux-next"))
        for stream in ("..", "a/b", "", "-x"):
            with self.subTest(stream=stream), self.assertRaises(reg.RegistryError):
                reg.slot_path(cache, stream)


class PlanTest(unittest.TestCase):
    def cache(self, **kw) -> "reg.Cache":
        base = dict(name="c", paths=["/r"], owner="t", regenerates=True, budget_gib=0.0, min_gib=0.0,
                    idle_shrink_days=3.0, in_use_hours=6.0)
        base.update(kw)
        return reg.Cache(**base)

    def unit(self, name: str, gib: float, age_days: float, now: float) -> "reg.Unit":
        return reg.Unit(path=Path("/r") / name, root=Path("/r"), bytes=int(gib * reg.GIB), last_use=now - age_days * DAY)

    def test_over_budget_evicts_least_recently_used_first_until_under(self):
        now = 1_000_000_000.0
        found = [self.unit("new", 4, 1, now), self.unit("old", 4, 2.5, now), self.unit("mid", 4, 2, now)]
        p = reg.plan(self.cache(budget_gib=5), now, found)
        self.assertEqual([u.path.name for u, _ in p.evict], ["old", "mid"])

    def test_a_unit_used_within_in_use_hours_is_never_evicted(self):
        now = 1_000_000_000.0
        found = [self.unit("busy", 10, 0.1, now)]
        p = reg.plan(self.cache(budget_gib=1), now, found)
        self.assertEqual(p.evict, [])
        self.assertIn("kept although over budget", p.notes[0])

    def test_idle_units_shrink_toward_min_even_under_budget(self):
        now = 1_000_000_000.0
        found = [self.unit("stale", 1, 10, now), self.unit("warm", 1, 1, now)]
        p = reg.plan(self.cache(budget_gib=100), now, found)
        self.assertEqual([u.path.name for u, _ in p.evict], ["stale"])
        self.assertEqual(reg.plan(self.cache(budget_gib=100, min_gib=2), now, found).evict, [])

    def test_a_cache_that_does_not_regenerate_alarms_and_is_never_evicted(self):
        now = 1_000_000_000.0
        found = [self.unit("runtime", 50, 30, now)]
        p = reg.plan(self.cache(regenerates=False, budget_gib=40), now, found)
        self.assertEqual(p.evict, [])
        self.assertIn("held, not evicted", p.alarm)


class EvictTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "dd"
        self.root.mkdir()
        self.outside = self.base / "outside"
        self.outside.mkdir()
        (self.outside / "keep").write_bytes(b"k" * MIB)
        old = time.time() - 30 * DAY
        for name in ("main", "feat"):
            slot = self.root / name
            slot.mkdir()
            (slot / "blob").write_bytes(b"x" * (2 * MIB))
            os.utime(slot / "blob", (old, old))
            os.utime(slot, (old, old))
        (self.root / "link").symlink_to(self.outside)
        os.utime(self.root / "link", (old, old), follow_symlinks=False)
        self.registry = self.base / "registry.toml"
        self.registry.write_text(
            f'schema = "{reg.SCHEMA}"\n[[cache]]\nname = "derived-data"\npath = "{self.root}/{{stream}}"\n'
            'owner = "t"\nregenerates = true\nbudget_gib = 0\n')
        self.lock = self.base / "lock"

    def run_cli(self, *argv: str, flag: str | None) -> tuple[int, dict]:
        env = {k: v for k, v in os.environ.items() if k != reg.FLAG_ENV}
        if flag is not None:
            env[reg.FLAG_ENV] = flag
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(reg, "LOCK_PATH", str(self.lock)), \
                contextlib.redirect_stdout(out):
            code = reg.main(["--registry", str(self.registry), "--host", "h", *argv])
        return code, json.loads(out.getvalue())

    def test_off_by_default_deletes_nothing_even_with_apply(self):
        code, result = self.run_cli("evict", "--apply", "--json", flag=None)
        self.assertEqual(code, 0)
        self.assertIn("off by default", result["mode"])
        self.assertEqual(sorted(Path(e["path"]).name for e in result["caches"][0]["evict"]), ["feat", "link", "main"])
        self.assertTrue((self.root / "main" / "blob").exists() and (self.root / "feat").exists())

    def test_flag_without_apply_is_a_dry_run(self):
        _code, result = self.run_cli("evict", "--json", flag="1")
        self.assertIn("--apply not given", result["mode"])
        self.assertTrue((self.root / "main").exists())

    def test_flag_and_apply_evict_stale_slots_but_never_follow_a_symlink(self):
        _code, result = self.run_cli("evict", "--apply", "--json", flag="1")
        self.assertEqual(result["mode"], "applied")
        self.assertFalse((self.root / "main").exists() or (self.root / "feat").exists())
        self.assertTrue((self.outside / "keep").exists(), "a symlinked unit's target must survive")
        self.assertTrue(os.path.islink(self.root / "link"))
        self.assertTrue(any("a symlink, kept" in k for k in result["kept"]))
        self.assertTrue(self.root.is_dir(), "the cache root itself is never removed")

    def test_one_pass_deletes_at_most_max_units(self):
        _code, result = self.run_cli("evict", "--apply", "--json", "--max-units", "1", flag="1")
        self.assertEqual(result["deferred"], 2)
        left = [name for name in ("feat", "link", "main") if os.path.lexists(self.root / name)]
        self.assertEqual(len(left), 2, "one unit goes per pass at --max-units 1; the rest wait")

    def test_a_unit_used_since_the_plan_is_kept(self):
        cache = reg.parse({"schema": reg.SCHEMA, "cache": [{"name": "dd", "path": f"{self.root}/{{stream}}",
                                                            "owner": "t", "regenerates": True, "budget_gib": 0}]},
                          "h").caches[0]
        p = reg.plan(cache)
        unit = next(u for u, _ in p.evict if u.path.name == "main")
        (self.root / "main" / "fresh").write_bytes(b"y")
        self.assertIn("used since the plan", reg.remove_unit(unit, cache))
        self.assertTrue((self.root / "main").exists())

    def test_status_reports_sizes_and_is_read_only(self):
        code, result = self.run_cli("status", "--json", flag="1")
        self.assertEqual(code, 0)
        self.assertEqual(result["caches"][0]["units"], 3)
        self.assertTrue((self.root / "main").exists())


if __name__ == "__main__":
    unittest.main()
