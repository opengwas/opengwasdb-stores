#!/usr/bin/env python3
"""The yardstick (#390): workload loader, result validator, runner, Veeam flag.

The runner test builds a 2-variant Dense Store in a temporary directory with
`opengwasdb build-dense-vcf` (~6 s) and measures that, never a real Store.

Run from the repository root:
    python3 tests/yardstick/test_yardstick.py
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from yardstick import fixture, measure, proc, runner, veeam
from yardstick import result as result_spec
from yardstick import workload as workload_spec

REPO_ROOT = Path(__file__).resolve().parents[2]


def timed(**extra: object) -> dict:
    if "result_count" in extra:
        extra.setdefault("first_result_count", extra["result_count"])
    return {"status": "measured", "first_read_ms": 12.5, "median_ms": 3.0, "p95_ms": 4.0, "reps": 5, **extra}


HM3_FIELDS = {
    "subset": "hm3", "detail": "", "analysis_id": "a", "warm_result_count": 3,
    "subset_n_variants": 10, "subset_n_analyses": 2, "subset_requested_count": 12,
    "subset_resolved_count": 10, "subset_absent_count": 2, "subset_reference_assembly": "hg38",
    "subset_input_sha256": "ab" * 32,
}
HM3_COUNTS = tuple(k for k in HM3_FIELDS if k.endswith("_count") or k.startswith("subset_n_"))


def hm3_record(**extra: object) -> dict:
    return timed(result_count=3, **{**HM3_FIELDS, **extra})


def minimal_result() -> dict:
    return {
        "schema": 1,
        "workload_id": "w",
        "workload_sha256": "0" * 64,
        "opengwasdb": {"version": "0.3.0", "revision": "0d59f151c7618933747a59f825deeef6a51de9e1"},
        "ogstores": {
            "version": "0.1.0",
            "commit": "08bd4a57f0d4e7f1e3f2f0f0f0f0f0f0f0f0f0f0",
            "dirty": False,
        },
        "host": {"name": "h", "cpu_count": 4},
        "load_average": {"before": [0.1, 0.2, 0.3], "after": [0.2, 0.2, 0.3]},
        "started_utc": "2026-10-29T12:00:00Z",
        "ended_utc": "2026-10-29T12:01:00Z",
        "duration_s": 60.0,
        "local_utc_offset_minutes": 0,
        "veeam_window": False,
        "repetitions": {"default": 5},
        "stores": [
            {
                "store_id": "OGS-00099",
                "layout": "dense",
                "coverage": "gwas",
                "status": "measured",
                "measures": {
                    "open": timed(),
                    "phewas_rsid": timed(result_count=1),
                    "bytes_on_disk": {"status": "measured", "bytes": 1000, "source": "walk"},
                    "peak_rss": {"status": "measured", "baseline_mb": 100.0, "peak_mb": 120.0, "delta_mb": 20.0, "result_count": 2},
                },
            },
            {"store_id": "OGS-00011", "layout": "hybrid", "coverage": "gwas", "status": "pending", "reason": "rebuild"},
        ],
        "fixture_build": {"status": "measured", "layout": "dense", "seconds": 5.8},
    }


MINIMAL_WORKLOAD = {
    "schema": 1,
    "workload_id": "w",
    "stores": [
        {"store_id": "OGS-00099", "layout": "dense", "status": "measured", "measures": ["open", "phewas_rsid", "bytes_on_disk", "peak_rss"]},
        {"store_id": "OGS-00011", "layout": "hybrid", "status": "pending"},
    ],
}


class WorkloadFileTest(unittest.TestCase):
    def test_committed_workload_loads_with_the_expected_stores(self) -> None:
        loaded = workload_spec.load()
        self.assertEqual(
            [(s["store_id"], s["layout"], s["status"]) for s in loaded["stores"]],
            [
                ("OGS-00009", "dense", "measured"),
                ("OGS-00027", "ragged", "measured"),
                ("OGS-00006", "ragged", "measured"),
                ("OGS-00011", "hybrid", "pending"),
            ],
        )

    def test_hm3_is_measured_only_on_the_dense_store(self) -> None:
        by_id = {s["store_id"]: s for s in workload_spec.load()["stores"]}
        self.assertIn("analysis_hm3", by_id["OGS-00009"]["measures"])
        self.assertNotIn("analysis_hm3", by_id["OGS-00027"]["measures"])

    def test_unknown_measure_is_refused(self) -> None:
        bad = copy.deepcopy(workload_spec.load())
        bad["stores"][0]["measures"].append("speed")
        with self.assertRaisesRegex(workload_spec.WorkloadError, "speed"):
            workload_spec.check(bad)

    def test_pending_store_with_measures_is_refused(self) -> None:
        bad = copy.deepcopy(workload_spec.load())
        bad["stores"][3]["measures"] = ["open"]
        with self.assertRaisesRegex(workload_spec.WorkloadError, "OGS-00011"):
            workload_spec.check(bad)


class ResultValidatorTest(unittest.TestCase):
    def test_minimal_result_is_valid(self) -> None:
        result_spec.validate(minimal_result())

    def test_minimal_result_is_valid_against_its_workload(self) -> None:
        result_spec.validate(minimal_result(), MINIMAL_WORKLOAD)

    def test_missing_measure_is_refused_against_the_workload(self) -> None:
        r = minimal_result()
        del r["stores"][0]["measures"]["phewas_rsid"]
        result_spec.validate(r)  # structurally fine
        with self.assertRaisesRegex(result_spec.ResultError, "phewas_rsid"):
            result_spec.validate(r, MINIMAL_WORKLOAD)

    def test_wrong_schema_version_is_refused(self) -> None:
        r = minimal_result()
        r["schema"] = 2
        with self.assertRaisesRegex(result_spec.ResultError, "schema"):
            result_spec.validate(r)

    def test_negative_timing_is_refused(self) -> None:
        r = minimal_result()
        r["stores"][0]["measures"]["open"]["median_ms"] = -0.1
        with self.assertRaisesRegex(result_spec.ResultError, "median_ms"):
            result_spec.validate(r)

    def test_negative_bytes_is_refused(self) -> None:
        r = minimal_result()
        r["stores"][0]["measures"]["bytes_on_disk"]["bytes"] = -1
        with self.assertRaisesRegex(result_spec.ResultError, "bytes"):
            result_spec.validate(r)

    def test_unavailable_measure_needs_its_error(self) -> None:
        r = minimal_result()
        r["stores"][0]["measures"]["open"] = {"status": "unavailable"}
        with self.assertRaisesRegex(result_spec.ResultError, "error"):
            result_spec.validate(r)
        r["stores"][0]["measures"]["open"] = {"status": "unavailable", "error": "boom"}
        result_spec.validate(r)

    def refused(self, mutate, pattern: str) -> None:
        r = minimal_result()
        mutate(r)
        with self.assertRaisesRegex(result_spec.ResultError, pattern):
            result_spec.validate(r)

    def test_store_object_needs_store_id_layout_coverage_and_status(self) -> None:
        for key in ("store_id", "layout", "coverage", "status"):
            with self.subTest(key=key):
                self.refused(lambda r, key=key: r["stores"][0].pop(key), key)

    def test_a_value_of_the_wrong_shape_is_refused_by_path_not_raised_around(self) -> None:
        """Every nested value is checked before it is read, so a bad shape is a `ResultError`.

        `"stores": [null]` used to reach `store.get('store_id')` and raise
        `AttributeError`, which made the whole Records source unavailable rather
        than refusing one file (#390 review, item 1; #389).
        """
        cases = {
            "a null Store object": (lambda r: r["stores"].__setitem__(0, None), "stores[0] must be an object"),
            "a Store that is a string": (lambda r: r["stores"].__setitem__(0, "OGS-00099"), "stores[0] must be an object"),
            "stores that is an object": (lambda r: r.__setitem__("stores", {"OGS-00099": {}}), "stores must be a list"),
            "measures that is a list": (lambda r: r["stores"][0].__setitem__("measures", [1]), "stores[OGS-00099].measures must be an object"),
            "a measure that is a string": (
                lambda r: r["stores"][0]["measures"].__setitem__("open", "fast"),
                "stores[OGS-00099].open must be an object",
            ),
            "a null measure": (
                lambda r: r["stores"][0]["measures"].__setitem__("peak_rss", None),
                "stores[OGS-00099].peak_rss must be an object",
            ),
            "opengwasdb that is a string": (lambda r: r.__setitem__("opengwasdb", "0.3.0"), "opengwasdb must be an object"),
            "a null ogstores": (lambda r: r.__setitem__("ogstores", None), "ogstores must be an object"),
            "host that is a list": (lambda r: r.__setitem__("host", []), "host must be an object"),
            "load_average that is a string": (lambda r: r.__setitem__("load_average", "quiet"), "load_average must be an object"),
            "a fixture_build that is a string": (lambda r: r.__setitem__("fixture_build", "5.8 s"), "fixture_build must be an object"),
        }
        for name, (mutate, where) in cases.items():
            with self.subTest(case=name):
                result = minimal_result()
                mutate(result)
                with self.assertRaises(result_spec.ResultError) as caught:
                    result_spec.validate(result)
                self.assertIn(where, str(caught.exception))

    def test_something_that_is_not_a_result_at_all_is_refused(self) -> None:
        for value in (None, [], "result", 7):
            with self.subTest(value=value):
                with self.assertRaisesRegex(result_spec.ResultError, "result must be an object"):
                    result_spec.validate(value)

    def test_timed_record_other_than_open_needs_result_count(self) -> None:
        self.refused(lambda r: r["stores"][0]["measures"]["phewas_rsid"].pop("result_count"), "result_count")
        r = minimal_result()
        r["stores"][0]["measures"]["open"].pop("result_count", None)
        result_spec.validate(r)

    def test_hm3_record_needs_every_measure_hm3_extract_field(self) -> None:
        for key in HM3_FIELDS:
            with self.subTest(key=key):
                def mutate(r, key=key):
                    record = hm3_record()
                    del record[key]
                    r["stores"][0]["measures"]["analysis_hm3"] = record
                self.refused(mutate, key)

    def test_complete_hm3_record_is_valid(self) -> None:
        r = minimal_result()
        r["stores"][0]["measures"]["analysis_hm3"] = hm3_record()
        result_spec.validate(r)

    def test_hm3_counts_must_not_be_negative(self) -> None:
        for key in HM3_COUNTS:
            with self.subTest(key=key):
                self.refused(
                    lambda r, key=key: r["stores"][0]["measures"].__setitem__("analysis_hm3", hm3_record(**{key: -1})), key
                )

    def test_first_result_count_is_checked_like_result_count(self) -> None:
        self.refused(lambda r: r["stores"][0]["measures"]["phewas_rsid"].pop("first_result_count"), "first_result_count")
        self.refused(lambda r: r["stores"][0]["measures"]["phewas_rsid"].__setitem__("first_result_count", -1), "first_result_count")

    def test_each_load_array_is_exactly_three_non_negative_numbers(self) -> None:
        for key in ("before", "after"):
            for bad in ([0.1, 0.2], [0.1, 0.2, 0.3, 0.4], [0.1, 0.2, -0.3], [0.1, "x", 0.3]):
                with self.subTest(key=key, bad=bad):
                    self.refused(lambda r, key=key, bad=bad: r["load_average"].__setitem__(key, bad), key)

    def test_over_budget_must_be_boolean_when_present(self) -> None:
        r = minimal_result()
        r["over_budget"] = False
        result_spec.validate(r)
        self.refused(lambda r: r.__setitem__("over_budget", 0), "over_budget")

    def test_measured_fixture_build_needs_layout(self) -> None:
        self.refused(lambda r: r["fixture_build"].pop("layout"), "layout")

    def test_revision_must_be_forty_hex_characters(self) -> None:
        for bad in ("0d59f151", "G" * 40, "0D59F151C7618933747A59F825DEEF6A51DE9E1F"):
            with self.subTest(bad=bad):
                self.refused(lambda r, bad=bad: r["opengwasdb"].__setitem__("revision", bad), "revision")

    def test_timestamps_must_be_utc_iso(self) -> None:
        for key in ("started_utc", "ended_utc"):
            for bad in ("2026-10-29 12:00:00", "2026-10-29T12:00:00+01:00", "yesterday"):
                with self.subTest(key=key, bad=bad):
                    self.refused(lambda r, key=key, bad=bad: r.__setitem__(key, bad), key)

    def test_load_arrays_must_not_be_empty(self) -> None:
        for key in ("before", "after"):
            with self.subTest(key=key):
                self.refused(lambda r, key=key: r["load_average"].__setitem__(key, []), key)

    def test_guard_violations_are_recorded_as_store_and_problem(self) -> None:
        r = minimal_result()
        r["guard_violations"] = [{"store_id": "OGS-00099", "problem": "new entry x"}]
        result_spec.validate(r)
        r["guard_violations"] = ["x"]
        with self.assertRaisesRegex(result_spec.ResultError, "guard_violations"):
            result_spec.validate(r)

    def test_an_ogstores_commit_must_be_forty_hex_characters(self) -> None:
        """The commit, not the version, says which code ran (#386)."""
        for bad in ("08bd4a57", "G" * 40, "08BD4A57F0D4E7F1E3F2F0F0F0F0F0F0F0F0F0F0"):
            with self.subTest(bad=bad):
                self.refused(lambda r, bad=bad: r["ogstores"].__setitem__("commit", bad), "ogstores.commit")

    def test_the_ogstores_dirty_flag_must_be_boolean(self) -> None:
        self.refused(lambda r: r["ogstores"].__setitem__("dirty", "no"), "ogstores.dirty")

    def test_veeam_flag_must_be_boolean(self) -> None:
        r = minimal_result()
        r["veeam_window"] = "yes"
        with self.assertRaisesRegex(result_spec.ResultError, "veeam_window"):
            result_spec.validate(r)


def local(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=timezone(timedelta(hours=1)))


class OgstoresIdentityTest(unittest.TestCase):
    """What a run records about the `ogstores` it ran (#386).

    An editable install's version is frozen at the commit it was built from, so
    the version alone can name code that did not run. The commit cannot.
    """

    def test_the_identity_names_this_checkouts_commit(self) -> None:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=workload_spec.REPO_ROOT,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(runner.ogstores_identity()["commit"], head)

    def test_the_identity_keeps_the_installed_version(self) -> None:
        from importlib.metadata import version

        self.assertEqual(runner.ogstores_identity()["version"], version("ogstores"))

    def test_the_dirty_flag_follows_the_working_tree(self) -> None:
        for porcelain, expected in (("", False), (" M src/ogstores/run.py\n", True)):
            with self.subTest(porcelain=porcelain):
                with mock.patch.object(runner, "_git_output", return_value=porcelain):
                    self.assertIs(runner.ogstores_identity()["dirty"], expected)


class VeeamWindowTest(unittest.TestCase):
    def test_run_inside_the_window(self) -> None:
        self.assertTrue(veeam.overlaps_window(local(29, 3), local(29, 3, 8)))

    def test_run_after_the_window(self) -> None:
        self.assertFalse(veeam.overlaps_window(local(29, 11), local(29, 11, 8)))
        self.assertFalse(veeam.overlaps_window(local(29, 14), local(29, 23, 59)))

    def test_run_that_starts_just_before_eleven(self) -> None:
        self.assertTrue(veeam.overlaps_window(local(29, 10, 58), local(29, 11, 5)))

    def test_run_across_midnight_into_the_window(self) -> None:
        self.assertTrue(veeam.overlaps_window(local(28, 23, 55), local(29, 0, 4)))

    def test_run_that_ends_exactly_at_midnight_touches_the_window(self) -> None:
        self.assertTrue(veeam.overlaps_window(local(28, 23, 55), local(29, 0, 0)))

    def test_run_that_ends_before_midnight(self) -> None:
        self.assertFalse(veeam.overlaps_window(local(28, 22, 0), local(28, 23, 59)))

    def test_run_spanning_a_whole_window(self) -> None:
        self.assertTrue(veeam.overlaps_window(local(28, 12), local(30, 12)))


class AbsenceIsNeverATimingTest(unittest.TestCase):
    def test_an_empty_first_read_is_refused(self) -> None:
        calls = iter([{"z": []}, {"z": [1]}, {"z": [1]}])
        with self.assertRaisesRegex(RuntimeError, "no rows"):
            measure.time_calls(lambda: next(calls), 2, count=lambda r: len(r["z"]))

    def test_an_empty_warm_read_in_the_middle_is_refused(self) -> None:
        calls = iter([{"z": [1]}, {"z": []}, {"z": [1]}])
        with self.assertRaisesRegex(RuntimeError, "no rows"):
            measure.time_calls(lambda: next(calls), 2, count=lambda r: len(r["z"]))

    def test_every_nonempty_call_is_measured_with_the_first_count(self) -> None:
        record = measure.time_calls(lambda: {"z": [1, 2, 3]}, 2, count=lambda r: len(r["z"]))
        self.assertEqual((record["status"], record["result_count"], record["first_result_count"]), ("measured", 3, 3))

    def test_peak_rss_of_an_empty_read_is_refused(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "no rows"):
            measure.sample_peak(lambda: {"z": []})

    def test_peak_rss_of_a_read_counts_its_rows(self) -> None:
        self.assertEqual(measure.sample_peak(lambda: {"z": [1, 2]})["result_count"], 2)

    def test_a_walk_error_is_raised_not_summed_around(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            locked = Path(tmp) / "locked"
            locked.mkdir()
            (locked / "f").write_bytes(b"x" * 10)
            os.chmod(locked, 0)
            try:
                if os.access(locked, os.R_OK):
                    self.skipTest("permissions are not enforced for this user")
                with self.assertRaises(OSError):
                    measure.bytes_on_disk(Path(tmp))
            finally:
                os.chmod(locked, 0o700)


class StoreGuardTest(unittest.TestCase):
    def test_snapshot_compares_equal_when_nothing_changed(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            store = Path(tmp)
            (store / "index.sqlite").write_bytes(b"abc")
            (store / "manifest.json").write_text("{}")
            before = runner.snapshot(store)
            self.assertEqual(runner.compare(before, runner.snapshot(store)), [])

    def test_a_new_top_level_entry_is_a_violation(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            store = Path(tmp)
            (store / "index.sqlite").write_bytes(b"abc")
            before = runner.snapshot(store)
            (store / "index.sqlite-journal").write_bytes(b"")
            self.assertEqual(
                runner.compare(before, runner.snapshot(store)),
                ["new top-level entry: index.sqlite-journal"],
            )

    def test_a_changed_index_is_a_violation(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            store = Path(tmp)
            (store / "index.sqlite").write_bytes(b"abc")
            before = runner.snapshot(store)
            (store / "index.sqlite").write_bytes(b"abcd")
            self.assertEqual(runner.compare(before, runner.snapshot(store)), ["index.sqlite changed"])

    def test_a_removed_index_is_a_violation(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            store = Path(tmp)
            (store / "index.sqlite").write_bytes(b"abc")
            before = runner.snapshot(store)
            (store / "index.sqlite").unlink()
            self.assertIn("index.sqlite changed", runner.compare(before, runner.snapshot(store)))


GOOD_MEASURES = {
    name: timed(result_count=1) for name in ("open", "phewas_rsid", "phewas_alid", "phewas_position", "analysis_full", "region")
}
GOOD_MEASURES["open"] = timed()
GOOD_MEASURES["bytes_on_disk"] = {"status": "measured", "bytes": 5, "source": "walk"}


def stub_worker(calls: list, before=None):
    """A `_worker` that answers without a query; `before(mode, store_path)` may meddle with the Store."""

    def worker(mode, store_path, entry, workload, scratch, **kw):
        calls.append(mode)
        if before:
            before(mode, store_path)
        if mode == "rss":
            return {"status": "measured", "baseline_mb": 1.0, "peak_mb": 2.0, "delta_mb": 1.0, "result_count": 1}
        return dict(GOOD_MEASURES)

    return worker


class ProcessGroupTest(unittest.TestCase):
    def test_timeout_kills_the_whole_group_including_a_child_that_ignores_sigterm(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            pid_file = Path(tmp) / "child.pid"
            script = (
                "import subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', "
                "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])\n"
                f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
                "time.sleep(60)\n"
            )
            with self.assertRaises(subprocess.TimeoutExpired):
                proc.run_group([sys.executable, "-c", script], timeout=0.8)
            pid = int(pid_file.read_text())
            for _ in range(40):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                os.kill(pid, 9)
                self.fail("the worker's child survived the timeout")


class RunnerGuardTest(unittest.TestCase):
    """The guard and the budget, at the seam, with no query run."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        cls.root = Path(cls.tmp.name)
        cls.store = cls.root / "store.opengwasdb"
        cls.store.mkdir()
        (cls.store / "index.sqlite").write_bytes(b"abc")
        (cls.store / "manifest.json").write_text("{}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def copy(self) -> Path:
        dest = Path(tempfile.mkdtemp(dir=self.root)) / "store.opengwasdb"
        shutil.copytree(self.store, dest)
        return dest

    def go(self, store: Path, **kwargs: object) -> dict:
        scratch = Path(tempfile.mkdtemp(dir=self.root))
        return runner.run(fixture_workload(), store_paths={"OGS-00099": store}, scratch=scratch, **kwargs)

    def test_a_worker_that_writes_beside_the_store_fails_the_run(self) -> None:
        store, calls = self.copy(), []
        with mock.patch.object(runner, "_worker", stub_worker(calls, lambda m, p: (p / "stray.tmp").write_text("x"))):
            result = self.go(store)
        self.assertEqual(
            result["guard_violations"],
            [{"store_id": "OGS-00099", "problem": "new top-level entry: stray.tmp"}],
        )
        self.assertEqual(calls, ["timed"])  # the violation stops the Store's remaining workers
        self.assertEqual(result["stores"][0]["measures"]["peak_rss"]["status"], "unavailable")
        result_spec.validate(result, fixture_workload())
        out = Path(tempfile.mkdtemp(dir=self.root))
        with mock.patch.object(runner, "run", return_value=result), mock.patch.object(
            workload_spec, "load", return_value=fixture_workload()
        ):
            self.assertEqual(runner.main(["--output-dir", str(out)]), 3)
        self.assertEqual(len(list(out.glob("*.json"))), 1)

    def test_a_timed_worker_that_removes_the_index_stops_the_rss_worker(self) -> None:
        store, calls = self.copy(), []
        with mock.patch.object(runner, "_worker", stub_worker(calls, lambda m, p: (p / "index.sqlite").unlink())):
            result = self.go(store)
        self.assertEqual(calls, ["timed"])
        self.assertEqual(result["guard_violations"], [{"store_id": "OGS-00099", "problem": "index.sqlite changed"}])
        rss = result["stores"][0]["measures"]["peak_rss"]
        self.assertEqual(rss["status"], "unavailable")
        self.assertRegex(rss["error"], "index.sqlite")
        result_spec.validate(result, fixture_workload())

    def test_an_index_missing_before_the_rss_worker_is_not_opened(self) -> None:
        store, calls = self.copy(), []

        def remove_after_timed(mode, path):
            if mode == "timed":
                (path / "index.sqlite").unlink()

        with mock.patch.object(runner, "_worker", stub_worker(calls, remove_after_timed)):
            self.go(store)
        self.assertEqual(calls, ["timed"])

    def test_a_worker_that_prints_garbage_makes_its_measures_unavailable(self) -> None:
        store = self.copy()
        junk = Path(tempfile.mkdtemp(dir=self.root)) / "yardstick_junk.py"
        junk.write_text("print('garbage')\n")
        with mock.patch.object(runner, "WORKER_MODULE", "yardstick_junk"), mock.patch.dict(
            os.environ, {"PYTHONPATH": str(junk.parent)}
        ):
            result = self.go(store)
        for record in result["stores"][0]["measures"].values():
            self.assertEqual(record["status"], "unavailable")
            self.assertTrue(record["error"])
        self.assertEqual(result["guard_violations"], [])
        result_spec.validate(result, fixture_workload())

    def test_a_worker_that_exits_nonzero_makes_its_measures_unavailable(self) -> None:
        store = self.copy()
        with mock.patch.object(runner, "_worker", side_effect=RuntimeError("boom")):
            result = self.go(store)
        self.assertEqual({m["error"] for m in result["stores"][0]["measures"].values()}, {"boom"})

    def test_a_hung_worker_is_killed_at_the_budget(self) -> None:
        store = self.copy()
        hang = Path(tempfile.mkdtemp(dir=self.root)) / "yardstick_hang.py"
        hang.write_text("import time\ntime.sleep(60)\n")
        with mock.patch.object(runner, "WORKER_MODULE", "yardstick_hang"), mock.patch.dict(
            os.environ, {"PYTHONPATH": str(hang.parent)}
        ):
            start = time.monotonic()
            result = self.go(store, budget_s=0.1)
        self.assertLess(time.monotonic() - start, 10)
        self.assertRegex(result["stores"][0]["measures"]["open"]["error"], r"^timeout after ")

    def test_a_tiny_budget_times_out_every_measure_and_still_writes_a_result(self) -> None:
        workload = fixture_workload(fixture_build={"layout": "dense"})
        result = runner.run(
            workload, store_paths={"OGS-00099": self.copy()}, scratch=Path(tempfile.mkdtemp(dir=self.root)), budget_s=0.001
        )
        result_spec.validate(result, workload)
        for record in result["stores"][0]["measures"].values():
            self.assertEqual(record["status"], "unavailable")
            self.assertRegex(record["error"], r"^timeout after ")
        self.assertEqual(result["fixture_build"]["status"], "unavailable")
        self.assertRegex(result["fixture_build"]["error"], r"^timeout after ")

    def test_fixture_build_is_timed(self) -> None:
        def stub(store_path: Path, source_dir: Path, *, timeout: float | None = None) -> Path:
            return store_path

        workload = fixture_workload(fixture_build={"layout": "dense"})
        with mock.patch.object(fixture, "build_dense_store", stub), mock.patch.object(
            runner, "_worker", stub_worker([])
        ):
            result = runner.run(
                workload, store_paths={"OGS-00099": self.copy()}, scratch=Path(tempfile.mkdtemp(dir=self.root))
            )
        self.assertEqual(result["fixture_build"]["status"], "measured")
        self.assertEqual(result["fixture_build"]["layout"], "dense")
        result_spec.validate(result, workload)

    def test_over_budget_is_decided_on_the_unrounded_time_and_exits_2(self) -> None:
        pending_only = {**fixture_workload(), "stores": [dict(PENDING)]}
        for elapsed, over, code in ((600.01, True, 2), (600.0, False, 0)):
            with self.subTest(elapsed=elapsed):
                clock = iter([0.0])
                with mock.patch.object(runner, "_now", lambda: next(clock, elapsed)):
                    result = runner.run(pending_only, scratch=Path(tempfile.mkdtemp(dir=self.root)))
                self.assertEqual((result["over_budget"], result["duration_s"]), (over, 600.0))
                out = Path(tempfile.mkdtemp(dir=self.root))
                with mock.patch.object(runner, "run", return_value=result), mock.patch.object(
                    workload_spec, "load", return_value=pending_only
                ):
                    self.assertEqual(runner.main(["--output-dir", str(out)]), code)


ENTRY = {
    "store_id": "OGS-00099",
    "layout": "dense",
    "coverage": "gwas",
    "status": "measured",
    "rsid": "rs1",
    "alid": "1:20000:C:T",
    "position": {"chromosome": "1", "position": 10000},
    "analysis_id": "fx1",
    "region": {"chromosome": "1", "start": 1, "end": 100000},
    "measures": [
        "open", "phewas_rsid", "phewas_alid", "phewas_position",
        "analysis_full", "region", "bytes_on_disk", "peak_rss",
    ],
}
PENDING = {"store_id": "OGS-00011", "layout": "hybrid", "coverage": "gwas", "status": "pending", "reason": "x"}


def fixture_workload(**extra: object) -> dict:
    return {
        "schema": 1,
        "workload_id": "fixture",
        "repetitions": {"default": 2, "analysis_full": 2},
        "region_width_bp": 100000,
        "stores": [copy.deepcopy(ENTRY), dict(PENDING)],
        **extra,
    }


class RunnerOnFixtureStoreTest(unittest.TestCase):
    """The one real build, and the one real end-to-end run."""

    def test_runner_result_validates_and_has_every_listed_measure(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            root = Path(tmp)
            store = fixture.build_dense_store(root / "store.opengwasdb", root / "src")
            workload = fixture_workload()
            result = runner.run(workload, store_paths={"OGS-00099": store}, scratch=root / "scratch")
        result_spec.validate(result, workload)
        measures = result["stores"][0]["measures"]
        self.assertEqual(sorted(measures), sorted(ENTRY["measures"]))
        self.assertEqual({m["status"] for m in measures.values()}, {"measured"})
        self.assertEqual(measures["phewas_rsid"]["result_count"], 1)
        self.assertEqual(measures["analysis_full"]["result_count"], 2)
        self.assertEqual(measures["phewas_rsid"]["reps"], 2)
        self.assertGreater(measures["bytes_on_disk"]["bytes"], 1000)
        self.assertEqual(result["stores"][1]["status"], "pending")
        self.assertEqual(result["guard_violations"], [])
        self.assertIs(result["over_budget"], False)
        self.assertIs(json.loads(json.dumps(result))["veeam_window"], result["veeam_window"])


if __name__ == "__main__":
    unittest.main()
