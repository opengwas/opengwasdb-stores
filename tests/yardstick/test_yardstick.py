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
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from yardstick import fixture, runner, veeam
from yardstick import result as result_spec
from yardstick import workload as workload_spec

REPO_ROOT = Path(__file__).resolve().parents[2]


def timed(**extra: object) -> dict:
    return {"status": "measured", "first_read_ms": 12.5, "median_ms": 3.0, "p95_ms": 4.0, "reps": 5, **extra}


def minimal_result() -> dict:
    return {
        "schema": 1,
        "workload_id": "w",
        "workload_sha256": "0" * 64,
        "opengwasdb": {"version": "0.3.0", "revision": "0d59f151c7618933747a59f825deeef6a51de9e1"},
        "ogstores": {"version": "0.1.0"},
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
            {"store_id": "OGS-00011", "layout": "hybrid", "coverage": "full", "status": "pending", "reason": "rebuild"},
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

    def test_veeam_flag_must_be_boolean(self) -> None:
        r = minimal_result()
        r["veeam_window"] = "yes"
        with self.assertRaisesRegex(result_spec.ResultError, "veeam_window"):
            result_spec.validate(r)


def local(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=timezone(timedelta(hours=1)))


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


class RunnerOnFixtureStoreTest(unittest.TestCase):
    def test_runner_result_validates_and_has_every_listed_measure(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            tmp_path = Path(tmp)
            store = fixture.build_dense_store(tmp_path / "store.opengwasdb", tmp_path / "src")
            workload = {
                "schema": 1,
                "workload_id": "fixture",
                "repetitions": {"default": 2, "analysis_full": 2},
                "region_width_bp": 100000,
                "fixture_build": {"layout": "dense"},
                "stores": [
                    {
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
                    },
                    {"store_id": "OGS-00011", "layout": "hybrid", "coverage": "full", "status": "pending", "reason": "x"},
                ],
            }
            result = runner.run(workload, store_paths={"OGS-00099": store}, scratch=tmp_path / "scratch")
        result_spec.validate(result, workload)
        measures = result["stores"][0]["measures"]
        self.assertEqual(sorted(measures), sorted(workload["stores"][0]["measures"]))
        self.assertEqual(measures["phewas_rsid"]["result_count"], 1)
        self.assertEqual(measures["analysis_full"]["result_count"], 2)
        self.assertEqual(measures["phewas_rsid"]["reps"], 2)
        self.assertGreater(measures["bytes_on_disk"]["bytes"], 1000)
        self.assertEqual(result["stores"][1]["status"], "pending")
        self.assertEqual(result["fixture_build"]["status"], "measured")
        self.assertIs(json.loads(json.dumps(result))["veeam_window"], result["veeam_window"])


if __name__ == "__main__":
    unittest.main()
