#!/usr/bin/env python3
"""Tests for the release-side HM3 benchmark report (stores #212, epic #214).

The report's job is to pass core HM3 statuses through unchanged and never let a
non-measurement look like a failure or a zero. These tests use a stub core
checkout rather than a multi-gigabyte Store, so they are data-only, network-free
and CI-safe:

1. a missing or partial ``--opengwasdb-checkout`` is refused loudly;
2. ``missing`` / ``not_applicable`` / ``unsupported`` / ``not_queryable`` and
   ``no_anchor`` pass through with no timing keys and never become shape errors;
3. a ``measured`` record's timing fields survive;
4. the generated Markdown states that HM3 benchmark absence is operational
   evidence, not a Store validation failure;
5. the first ``analysis_id`` anchor is located by column name, not position.

Run from repository root:
    pixi run python tests/hm3-benchmark-report/test_release_benchmark_report.py
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "tools" / "benchmarks" / "release-benchmark-report.py"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "release_benchmark_report", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


report_mod = _load_module()


# --------------------------------------------------------------------------- #
# Stub core
# --------------------------------------------------------------------------- #


class _State:
    def __init__(self, status: str, detail: str = "") -> None:
        self.status = status
        self.detail = detail


class _FakeQueryShapes:
    """Stands in for benchmarks._query_shapes, keyed by Store id."""

    def __init__(self, states: dict[str, _State], openable: set[str]) -> None:
        self._states = states
        self._openable = openable

    @staticmethod
    def _store_id(store_path: Any) -> str:
        return Path(store_path).parent.name

    def open_benchmark_store(self, path: Any) -> tuple[Any, Any]:
        if self._store_id(path) in self._openable:
            return SimpleNamespace(close=lambda: None), None
        raise RuntimeError("Store carries no variant index (ragged/by_variant)")

    def measure_hm3_extract(
        self,
        query: Any,
        analysis_id: str,
        *,
        store_path: Any = None,
        reps: int = 5,
    ) -> dict[str, Any]:
        assert query is None or self._store_id(store_path) in self._openable
        state = self._states[self._store_id(store_path)]
        record: dict[str, Any] = {
            "subset": "hm3",
            "status": state.status,
            "detail": state.detail,
        }
        if state.status == "measured":
            record.update(
                {
                    "analysis_id": analysis_id,
                    "first_read_ms": 2.5,
                    "median_ms": 1.5,
                    "p95_ms": 3.5,
                    "reps": reps,
                    "result_count": 7,
                    "subset_n_variants": 1_206_903,
                }
            )
        return record


class _FakeHm3:
    def __init__(self, states: dict[str, _State]) -> None:
        self._states = states

    def classify_hm3(self, store_path: Any) -> _State:
        return self._states[Path(store_path).parent.name]


def _fake_core(
    states: dict[str, _State], openable: set[str] | None = None
) -> Any:
    return SimpleNamespace(
        checkout=Path("/fake/opengwasdb"),
        revision="deadbeef",
        opengwasdb_path="/fake/opengwasdb/opengwasdb",
        opengwasdb_fingerprint="fingerprint",
        provenance={},
        query_shapes=_FakeQueryShapes(states, openable or set()),
        hm3=_FakeHm3(states),
    )


def _store(store_id: str, layout: str, analysis_id: str | None = "a0") -> Any:
    return report_mod.StoreRef(
        store_id=store_id,
        layout=layout,
        bundle_dir=Path("/registry") / store_id,
        analysis_id=analysis_id,
    )


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


class CheckoutTest(unittest.TestCase):
    def test_empty_checkout_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(report_mod.CheckoutError):
                report_mod.load_core(Path(tmp))

    def test_partial_checkout_lists_the_missing_pieces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "opengwasdb").mkdir()
            (root / "opengwasdb" / "__init__.py").write_text("")
            with self.assertRaises(report_mod.CheckoutError) as ctx:
                report_mod.load_core(root)
            message = str(ctx.exception)
            self.assertIn("benchmarks/_query_shapes.py", message)
            self.assertIn("benchmarks/_hm3.py", message)


class Hm3StatusPassthroughTest(unittest.TestCase):
    def _measure(self, store_id: str, layout: str, state: _State, openable: bool) -> dict:
        core = _fake_core({store_id: state}, {store_id} if openable else set())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / store_id / "store.opengwasdb"
            return report_mod.measure_hm3(
                core, _store(store_id, layout), path, reps=3
            )

    def test_missing_is_non_failing_and_carries_no_timing(self) -> None:
        record = self._measure(
            "OGS-00003", "dense", _State("missing", "no hm3 subset"), False
        )
        self.assertEqual(record["status"], "missing")
        self.assertNotIn("median_ms", record)
        self.assertNotIn("result_count", record)

    def test_not_applicable_is_non_failing(self) -> None:
        record = self._measure(
            "OGS-00001", "ragged", _State("not_applicable", "Ragged-only"), False
        )
        self.assertEqual(record["status"], "not_applicable")
        self.assertNotIn("median_ms", record)

    def test_unsupported_is_non_failing(self) -> None:
        record = self._measure(
            "OGS-00008", "dense", _State("unsupported", "future schema"), False
        )
        self.assertEqual(record["status"], "unsupported")
        self.assertNotIn("median_ms", record)

    def test_valid_subset_on_unqueryable_store_is_not_queryable(self) -> None:
        record = self._measure(
            "OGS-00004", "hybrid", _State("measured", "subset present"), False
        )
        self.assertEqual(record["status"], "not_queryable")
        self.assertEqual(record["core_status"], "measured")
        self.assertNotIn("median_ms", record)

    def test_measured_record_keeps_its_timing(self) -> None:
        record = self._measure(
            "OGS-00009", "dense", _State("measured", ""), True
        )
        self.assertEqual(record["status"], "measured")
        self.assertEqual(record["median_ms"], 1.5)
        self.assertEqual(record["result_count"], 7)

    def test_missing_analysis_id_is_no_anchor(self) -> None:
        core = _fake_core({"OGS-00003": _State("missing")})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "OGS-00003" / "store.opengwasdb"
            record = report_mod.measure_hm3(
                core, _store("OGS-00003", "dense", analysis_id=None), path, reps=3
            )
        self.assertEqual(record["status"], "no_anchor")
        self.assertNotIn("median_ms", record)


class ReportBuildTest(unittest.TestCase):
    def test_report_non_measured_never_becomes_a_shape_error(self) -> None:
        states = {
            "OGS-00003": _State("missing", "no hm3 subset"),
            "OGS-00001": _State("not_applicable", "Ragged-only"),
        }
        core = _fake_core(states)
        stores = [_store("OGS-00001", "ragged"), _store("OGS-00003", "dense")]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "stores"
            for store in stores:
                (root / store.store_id / "store.opengwasdb").mkdir(parents=True)
            report = report_mod.build_report(
                core,
                stores,
                artifact_root=root,
                anchors={},
                reps=2,
                hm3_only=True,
                work_dir=Path(tmp) / "work",
            )
            markdown = report_mod.render_markdown(report)

        self.assertEqual(report["shape_errors"], [])
        self.assertEqual(report["summary"], {"missing": 1, "not_applicable": 1})
        self.assertIn("not_applicable", markdown)
        self.assertIn("missing", markdown)
        self.assertIn(
            "HM3 benchmark absence is operational evidence, not Store validation",
            markdown,
        )
        # A non-measurement must not be rendered as a zero.
        self.assertNotIn("| missing | 0.000 |", markdown)

    def test_absent_artifact_is_recorded_non_failing(self) -> None:
        core = _fake_core({})
        store = _store("OGS-00009", "dense")
        with tempfile.TemporaryDirectory() as tmp:
            report = report_mod.build_report(
                core,
                [store],
                artifact_root=Path(tmp) / "empty",
                anchors={},
                reps=1,
                hm3_only=True,
                work_dir=Path(tmp) / "work",
            )
        self.assertEqual(report["stores"][0]["hm3_extract"]["status"], "artifact_absent")
        self.assertEqual(report["shape_errors"], [])

    def test_ragged_store_reports_not_applicable_shapes(self) -> None:
        core = _fake_core({"OGS-00001": _State("not_applicable")})
        store = _store("OGS-00001", "ragged")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "stores"
            (root / store.store_id / "store.opengwasdb").mkdir(parents=True)
            report = report_mod.build_report(
                core,
                [store],
                artifact_root=root,
                anchors={"OGS-00001": report_mod.Anchor(exposure="x")},
                reps=1,
                hm3_only=False,
                work_dir=Path(tmp) / "work",
            )
        self.assertEqual(report["stores"][0]["shapes"]["status"], "not_applicable")


class RegistryDiscoveryTest(unittest.TestCase):
    def test_first_analysis_id_is_found_by_column_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            analyses = Path(tmp) / "analyses.tsv"
            analyses.write_text(
                "analysis_index\tanalysis_id\tsource_label\n"
                "0\tGCST003566\tMultiple sclerosis\n"
            )
            self.assertEqual(
                report_mod.first_analysis_id(analyses), "GCST003566"
            )

    def test_registered_bundles_reads_layout_and_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "stores"
            bundle = root / "OGS-00042"
            bundle.mkdir(parents=True)
            (bundle / "release.yaml").write_text("store_id: OGS-00042\n")
            (bundle / "build.yaml").write_text("layout: hybrid\n")
            (bundle / "analyses.tsv").write_text(
                "analysis_id\tsource_label\nGCST1\tTrait\n"
            )
            stores = report_mod.registered_bundles(root)
        self.assertEqual(len(stores), 1)
        self.assertEqual(stores[0].store_id, "OGS-00042")
        self.assertTrue(stores[0].dense_component)
        self.assertEqual(stores[0].analysis_id, "GCST1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
