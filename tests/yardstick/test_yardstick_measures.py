#!/usr/bin/env python3
"""The yardstick-4 measures (#503): workload checks, new measures on the fixture Store, validator.

The fixture is Dense only, so a measure another layout answers is tested here
through its `not_applicable` path; the real layouts are checked by
`tools/yardstick_inputs.py` against the real Stores.

Run from the repository root:
    python3 tests/yardstick/test_yardstick_measures.py
"""

from __future__ import annotations

import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yardstick import fixture, measure, runner
from yardstick import result as result_spec
from yardstick import workload as workload_spec

NEW_MEASURES = [
    "analyses_table", "variants_table", "resolve", "region_100kb",
    *workload_spec.LOOKUP_SHAPES, "analysis_full_observed_only", "analysis_hm3",
    "top_hits", "range_by_analysis", "rho", "rho_row", "rho_matrix",
]


def lookup_input() -> dict:
    return {"variants": [f"1:{i}:C:T" for i in range(100)], "analyses": [f"a{i}" for i in range(100)]}


def full_entry(**extra: object) -> dict:
    """A Dense entry that lists every measure, as the committed Dense entries do."""
    return {
        "store_id": "OGS-00099", "layout": "dense", "coverage": "gwas", "status": "measured",
        "rsid": "rs1", "alid": "1:20000:C:T", "position": {"chromosome": "1", "position": 10000},
        "analysis_id": "fx1", "region": {"chromosome": "1", "start": 1, "end": 100000},
        "region_100kb": {"chromosome": "1", "start": 1, "end": 100000},
        "lookup": lookup_input(), "top_hits": {"analysis_id": "fx1", "threshold": 5e-8, "path": "scan"},
        "rho": False,
        "measures": ["open", "phewas_rsid", *NEW_MEASURES],
        "input_rows": {
            name: 1 for name in ("phewas_rsid", "phewas_alid", "phewas_position", "resolve", "region_100kb", "region",
                                 *workload_spec.LOOKUP_SHAPES, "analysis_full", "analysis_full_observed_only")
        } | {"top_hits": 0},
        **extra,
    }


def workload_of(*stores: dict) -> dict:
    return {"schema": 1, "workload_id": "t", "repetitions": {"default": 1}, "stores": list(stores)}


class WorkloadCheckTest(unittest.TestCase):
    def test_the_committed_workload_loads_and_is_yardstick_6(self) -> None:
        loaded = workload_spec.load()
        self.assertEqual(loaded["workload_id"], "yardstick-6")
        ids = [s["store_id"] for s in loaded["stores"]]
        self.assertEqual(ids, ["OGS-00009", "OGS-00011", "OGS-00027"])

    def test_a_full_entry_is_accepted(self) -> None:
        workload_spec.check(workload_of(full_entry()))

    def test_an_entry_missing_an_input_a_measure_needs_is_refused(self) -> None:
        for key, measure_name in (
            ("lookup", "lookup_1x100"), ("region_100kb", "region_100kb"),
            ("top_hits", "top_hits"), ("rho", "rho"),
        ):
            with self.subTest(key=key):
                entry = full_entry()
                del entry[key]
                with self.assertRaisesRegex(workload_spec.WorkloadError, f"{measure_name}.*{key}"):
                    workload_spec.check(workload_of(entry))

    def test_range_by_analysis_needs_its_window_only_on_a_ragged_entry(self) -> None:
        workload_spec.check(workload_of(full_entry()))  # Dense: no `cis`, and fine
        ragged = full_entry(layout="ragged")
        with self.assertRaisesRegex(workload_spec.WorkloadError, "range_by_analysis.*cis"):
            workload_spec.check(workload_of(ragged))
        ragged["cis"] = {"chromosome": "1", "start": 1, "end": 10}
        with self.assertRaisesRegex(workload_spec.WorkloadError, "range_by_analysis"):
            workload_spec.check(workload_of(ragged))  # a Ragged entry needs its row count too
        ragged["input_rows"]["range_by_analysis"] = 4
        workload_spec.check(workload_of(ragged))

    def test_a_lookup_list_of_the_wrong_size_is_refused(self) -> None:
        entry = full_entry()
        entry["lookup"]["variants"] = entry["lookup"]["variants"][:99]
        with self.assertRaisesRegex(workload_spec.WorkloadError, "lookup.variants"):
            workload_spec.check(workload_of(entry))

    def test_an_entry_without_a_row_count_for_a_listed_measure_is_refused(self) -> None:
        entry = full_entry()
        del entry["input_rows"]["resolve"]
        with self.assertRaisesRegex(workload_spec.WorkloadError, "row count for resolve"):
            workload_spec.check(workload_of(entry))
        no_counts = full_entry()
        del no_counts["input_rows"]
        with self.assertRaisesRegex(workload_spec.WorkloadError, "row count for"):
            workload_spec.check(workload_of(no_counts))

    def test_an_input_that_returned_no_rows_is_refused_but_empty_top_hits_is_not(self) -> None:
        workload_spec.check(workload_of(full_entry()))  # top_hits is 0 there
        entry = full_entry()
        entry["input_rows"]["lookup_1x100"] = 0
        with self.assertRaisesRegex(workload_spec.WorkloadError, "lookup_1x100.*no rows"):
            workload_spec.check(workload_of(entry))

    def test_rho_counts_are_required_only_where_a_rho_matrix_is_declared(self) -> None:
        entry = full_entry(rho={"ids": ["a", "b"], "analysis_id": "a"})
        with self.assertRaisesRegex(workload_spec.WorkloadError, "row count for rho"):
            workload_spec.check(workload_of(entry))

    def test_the_lookup_variants_of_each_store_are_scattered(self) -> None:
        for store in workload_spec.load()["stores"]:
            variants, _ = workload_spec.lookup_inputs(store["lookup"], 100, 100)
            ten, _ = workload_spec.lookup_inputs(store["lookup"], 10, 100)
            chromosomes = lambda ids: {v.split(":")[0] for v in ids}  # noqa: E731
            self.assertGreaterEqual(len(chromosomes(variants)), 10, store["store_id"])
            self.assertEqual(len(ten), 10)
            self.assertGreaterEqual(len(chromosomes(ten)), 5, store["store_id"])
            one, _ = workload_spec.lookup_inputs(store["lookup"], 1, 100)
            self.assertEqual(one, variants[:1])


class DeclaredFactsTest(unittest.TestCase):
    """`not_applicable` follows the entry's layout and `rho` declaration; a missing method is `unavailable`."""

    def test_no_committed_store_declares_a_rho_matrix(self) -> None:
        self.assertEqual({s["rho"] for s in workload_spec.load()["stores"]}, {False})

    def test_a_declared_rho_matrix_is_measured(self) -> None:
        calls: list[str] = []

        class Q:
            def rho(self, *ids):
                calls.append("rho")
                import numpy as np
                return {"rho": np.ones(3)}

            def rho_row(self, analysis_id):
                calls.append("rho_row")
                import numpy as np
                return {"rho": np.ones(2)}

            def rho_matrix(self, ids=None):
                calls.append("rho_matrix")
                import numpy as np
                return {"rho": np.ones((2, 2))}

            def close(self) -> None:
                pass

        entry = {
            "store_id": "OGS-00022", "layout": "hybrid", "coverage": "gwas", "status": "measured",
            "rho": {"ids": ["a", "b", "c"], "analysis_id": "a"}, "measures": ["rho", "rho_row", "rho_matrix"],
        }
        with mock.patch("opengwasdb.query.query_store", return_value=Q()):
            out = measure.measure_store(Path("/nonexistent"), entry, {"default": 1})
        self.assertEqual(entry["rho"]["ids"], ["a", "b", "c"])
        self.assertEqual(sorted(out), ["rho", "rho_matrix", "rho_row"])
        self.assertEqual({r["status"] for r in out.values()}, {"measured"})
        self.assertEqual([out[k]["result_count"] for k in ("rho", "rho_row", "rho_matrix")], [3, 2, 4])
        self.assertEqual(set(calls), {"rho", "rho_row", "rho_matrix"})

    def test_a_missing_facade_method_on_a_layout_that_has_it_is_unavailable(self) -> None:
        class Q:  # a Ragged-declared Store whose facade lost `range_by_analysis`
            def close(self) -> None:
                pass

        entry = {"store_id": "x", "layout": "ragged", "coverage": "cis", "status": "measured",
                 "cis": {"chromosome": "1", "start": 1, "end": 2}, "measures": ["range_by_analysis"]}
        with mock.patch("opengwasdb.query.query_store", return_value=Q()):
            out = measure.measure_store(Path("/nonexistent"), entry, {"default": 1})
        self.assertEqual(out["range_by_analysis"]["status"], "unavailable")
        self.assertIn("AttributeError", out["range_by_analysis"]["error"])

    def test_an_input_count_of_zero_records_top_hits_not_applicable(self) -> None:
        entry = full_entry(measures=["top_hits"])
        with mock.patch("opengwasdb.query.query_store", return_value=mock.Mock()):
            out = measure.measure_store(Path("/nonexistent"), entry, {"default": 1})
        self.assertEqual(out["top_hits"]["status"], "not_applicable")
        entry["input_rows"]["top_hits"] = 7  # the count alone decides: now it runs, and the empty mock result fails
        with mock.patch("opengwasdb.query.query_store", return_value=mock.Mock()):
            out = measure.measure_store(Path("/nonexistent"), entry, {"default": 1})
        self.assertNotEqual(out["top_hits"]["status"], "not_applicable")

    def test_a_top_hits_path_outside_index_and_scan_is_refused(self) -> None:
        entry = full_entry()
        entry["top_hits"]["path"] = "cache"
        with self.assertRaisesRegex(workload_spec.WorkloadError, "top_hits.path"):
            workload_spec.check(workload_of(entry))

    def test_a_ragged_entry_never_needs_counts_for_measures_it_cannot_run(self) -> None:
        ragged = full_entry(layout="ragged", rho={"ids": ["a"], "analysis_id": "a"}, cis={"chromosome": "1", "start": 1, "end": 2})
        ragged["input_rows"]["range_by_analysis"] = 4
        workload_spec.check(workload_of(ragged))  # no rho counts, though rho is declared: a Ragged Store has none
        self.assertFalse(workload_spec.applies("rho", ragged))
        self.assertFalse(workload_spec.applies("analysis_hm3", ragged))
        self.assertTrue(workload_spec.applies("range_by_analysis", ragged))

    def test_top_hits_records_which_path_answered(self) -> None:
        import numpy as np

        class Q:
            def top_hits(self, **kw):
                return {"z": np.ones(3)}

            def close(self) -> None:
                pass

        entry = {"store_id": "x", "layout": "dense", "coverage": "gwas", "status": "measured",
                 "top_hits": {"analysis_id": "a", "threshold": 5e-8, "path": "index"}, "input_rows": {"top_hits": 3},
                 "measures": ["top_hits"]}
        with mock.patch("opengwasdb.query.query_store", return_value=Q()):
            out = measure.measure_store(Path("/nonexistent"), entry, {"default": 1})
        self.assertEqual((out["top_hits"]["status"], out["top_hits"]["path"]), ("measured", "index"))


class ValidatorTest(unittest.TestCase):
    def record(self, status: str, **extra: object) -> dict:
        return {"status": status, **extra}

    def test_every_new_measure_name_is_accepted_as_timed(self) -> None:
        timed = {"status": "measured", "first_read_ms": 1.0, "median_ms": 1.0, "p95_ms": 1.0, "reps": 1,
                 "result_count": 1, "first_result_count": 1}
        for name in NEW_MEASURES:
            if name == "analysis_hm3":
                continue
            with self.subTest(name=name):
                result_spec._measure(f"x.{name}", name, timed)
                result_spec._measure(f"x.{name}", name, {"status": "not_applicable", "reason": "r"})

    def test_a_timed_new_measure_needs_its_result_count(self) -> None:
        with self.assertRaisesRegex(result_spec.ResultError, "result_count"):
            result_spec._measure("x", "lookup_100x100", {"status": "measured", "first_read_ms": 1, "median_ms": 1, "p95_ms": 1, "reps": 1})

    def test_an_unknown_status_is_refused(self) -> None:
        with self.assertRaisesRegex(result_spec.ResultError, "status"):
            result_spec._measure("x", "top_hits", self.record("skipped"))

    def test_an_unknown_measure_is_refused(self) -> None:
        with self.assertRaisesRegex(result_spec.ResultError, "unknown measure"):
            result_spec._measure("x", "speed", self.record("measured"))


class MeasuresOnFixtureStoreTest(unittest.TestCase):
    """One real fixture build; every new measure runs against it."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        root = Path(cls.tmp.name)
        cls.store = fixture.build_dense_store(root / "store.opengwasdb", root / "src")
        entry = full_entry(measures=["open", *[m for m in NEW_MEASURES if m != "analysis_hm3"]])
        # The fixture holds 2 variants and one Analysis, so its lookup lists (100 long, as the workload requires)
        # repeat those identifiers; the lookups are measured, the scatter is tested on the committed workload.
        q = measure.open_store(cls.store)
        try:
            alids = [v["alid"] for v in q.variants_table().values()]
            analysis_ids = [a["analysis_id"] for a in q.analyses_table().values()]
        finally:
            q.close()
        entry["lookup"] = {
            "variants": (alids * 100)[:100], "analyses": (analysis_ids * 100)[:100],
        }
        entry["analysis_id"] = analysis_ids[0]
        entry["rsid"] = "rs1"
        entry["alid"] = alids[0]
        cls.entry = entry
        cls.out = measure.measure_store(cls.store, entry, {"default": 1})

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_dense_measures_are_measured_with_counts(self) -> None:
        for name in ("analyses_table", "variants_table", "resolve", "region_100kb", "analysis_full_observed_only", *workload_spec.LOOKUP_SHAPES):
            with self.subTest(name=name):
                record = self.out[name]
                self.assertEqual(record["status"], "measured", record)
                self.assertGreater(record["result_count"], 0)

    def test_a_layout_refusal_is_not_applicable_never_unavailable(self) -> None:
        for name in ("range_by_analysis", "rho", "rho_row", "rho_matrix", "top_hits"):
            with self.subTest(name=name):
                self.assertEqual(self.out[name]["status"], "not_applicable", self.out[name])
                self.assertTrue(self.out[name]["reason"])

    def test_the_result_validates(self) -> None:
        for name, record in self.out.items():
            result_spec._measure(f"fixture.{name}", name, record)

    def test_a_failure_that_is_not_a_refusal_stays_unavailable(self) -> None:
        entry = copy.deepcopy(self.entry)
        entry["measures"] = ["resolve"]
        entry["rsid"] = "rs-not-in-this-store"
        out = measure.measure_store(self.store, entry, {"default": 1})
        self.assertEqual(out["resolve"]["status"], "unavailable")
        self.assertIn("error", out["resolve"])


if __name__ == "__main__":
    unittest.main()
