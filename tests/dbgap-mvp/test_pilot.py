#!/usr/bin/env python3
"""Hermetic tests for the bounded MVP metric-resolution pilot (#221).

No real association body or reference panel is opened: the resolver is a fake
CLI, and every fixture lives in a temp directory. Tests cover the metadata-only
selection/provenance rules, the fail-closed execution gates, the resource
monitor (wall timeout killing the whole process group, RSS breach), and the
alias/protected-root refusals.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PILOT_DIR = REPO_ROOT / "resources" / "generators" / "dbgap-mvp"
for extra in (str(PILOT_DIR), str(REPO_ROOT / "src"), str(REPO_ROOT / "resources" / "generators" / "lib")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import pilot.cli as cli  # noqa: E402
import pilot.selection as selection  # noqa: E402
import pilot.wrapper as wrapper  # noqa: E402
import metadata_content_sha as content_sha  # noqa: E402

FAKE_RESOLVER = REPO_ROOT / "tests" / "dbgap-mvp" / "fixtures" / "fake_resolver.py"

# Small per-bucket source metadata; the ids match the reviewed pilot table.
PILOT_META = {
    "EUR": [
        ("GCST90475091", "quantitative", "hemoglobin A1c (maximum, inv-norm transformed)", 338857, 0, 0),
        ("GCST90475094", "quantitative", "hemoglobin A1c (mean, inv-norm transformed)", 338848, 0, 0),
        ("GCST90475169", "case-control", "Breast Cancer", 315668, 1649, 314019),
    ],
    "AFR": [
        ("GCST90475090", "quantitative", "hemoglobin A1c (maximum, inv-norm transformed)", 98772, 0, 0),
        ("GCST90475093", "quantitative", "hemoglobin A1c (mean, inv-norm transformed)", 98735, 0, 0),
        ("GCST90475184", "case-control", "Prostate Cancer", 48011, 5764, 42247),
    ],
    "AMR": [
        ("GCST90475089", "quantitative", "hemoglobin A1c (maximum, inv-norm transformed)", 49229, 0, 0),
        ("GCST90475092", "quantitative", "hemoglobin A1c (mean, inv-norm transformed)", 49225, 0, 0),
        ("GCST90475183", "case-control", "Prostate Cancer", 26428, 1678, 24750),
    ],
    "EAS": [
        ("GCST90475346", "quantitative", "high density lipoprotein cholesterol (maximum, inv-norm transformed)", 6005, 0, 0),
        ("GCST90475350", "quantitative", "high density lipoprotein cholesterol (mean, inv-norm transformed)", 6005, 0, 0),
        ("GCST90475203", "case-control", "High Cholesterol", 3547, 1998, 1549),
    ],
    "META": [
        ("GCST90479500", "quantitative", "hemoglobin A1c (maximum, inv-norm transformed)", 492335, 0, 0),
        ("GCST90479501", "quantitative", "hemoglobin A1c (mean, inv-norm transformed)", 492283, 0, 0),
        ("GCST90479527", "case-control", "Takes medication for colon cancer", 371173, 1893, 369280),
    ],
}


def write_tsv(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    import csv

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def make_pilot_fixture(tmp: Path) -> tuple[Path, Path]:
    meta = tmp / "meta"
    (meta / "manifests").mkdir(parents=True)
    (meta / "gwas-ssf").mkdir(parents=True)
    import yaml

    mapping_rows: list[dict[str, str]] = []
    manifest_rows: dict[str, list[dict[str, str]]] = {}
    for bucket, entries in PILOT_META.items():
        for analysis_id, design, variable, n, cases, controls in entries:
            directory = meta / "gwas-ssf" / f"bucket-{bucket}" / analysis_id
            directory.mkdir(parents=True)
            data = directory / f"{analysis_id}.tsv.gz"
            data.symlink_to(f"/fixture/data/{analysis_id}.tsv.gz")
            sidecar = directory / f"{analysis_id}.tsv.gz-meta.yaml"
            sidecar.write_text(
                yaml.safe_dump(
                    {
                        "gwas_id": analysis_id,
                        "data_file_name": data.name,
                        "genome_assembly": "GRCh38",
                    }
                ),
                encoding="utf-8",
            )
            mapping_rows.append(
                {
                    "analysis_id": analysis_id,
                    "ancestry": bucket,
                    "study_design": design,
                    "sample_size": str(n),
                    "n_cases": str(cases),
                    "n_controls": str(controls),
                    "analyzed_variable": variable,
                }
            )
            manifest_rows.setdefault(bucket, []).append(
                {
                    "analysis_id": analysis_id,
                    "status": "ok",
                    "sha256": "a" * 64,
                    "data_file": str(data),
                    "yaml_file": str(sidecar),
                    "genome_assembly": "GRCh38",
                    "sample_size": str(n),
                    "data_bytes": "123456",
                }
            )
    mapping_path = tmp / "mapping.tsv"
    write_tsv(mapping_path, list(mapping_rows[0].keys()), mapping_rows)
    columns = [
        "analysis_id",
        "status",
        "sha256",
        "data_file",
        "yaml_file",
        "genome_assembly",
        "sample_size",
        "data_bytes",
    ]
    for bucket, rows in manifest_rows.items():
        write_tsv(meta / "manifests" / f"{bucket.lower()}.tsv", columns, rows)
    write_tsv(
        meta / "refresh-receipt.tsv",
        ["analysis_id", "note"],
        [{"analysis_id": row["analysis_id"], "note": "fixture"} for row in mapping_rows],
    )
    return meta, mapping_path


class TestSelection(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.tmp = Path(self.td.name)
        self.meta, self.mapping = make_pilot_fixture(self.tmp)
        self.content_sha, _ = content_sha.metadata_content_sha256(self.meta)

    def tearDown(self) -> None:
        self.td.cleanup()

    def test_classify_effect_scale_provenance(self) -> None:
        case, method, provenance, status = selection.classify_effect_scale(
            {"study_design": "case-control", "n_cases": "10", "analyzed_variable": "X"}
        )
        self.assertEqual((case, method, status), ("log_or", "binary_trait", "resolved"))
        sd, method, provenance, status = selection.classify_effect_scale(
            {"study_design": "quantitative", "n_cases": "0", "analyzed_variable": "trait (inv-norm transformed)"}
        )
        self.assertEqual((sd, method, status), ("sd", "declared_standardised", "resolved"))
        unknown, _, provenance, status = selection.classify_effect_scale(
            {"study_design": "quantitative", "n_cases": "0", "analyzed_variable": "raw units"}
        )
        self.assertEqual((unknown, status, provenance), ("", "pending", "unknown_pending"))
        self.assertNotEqual(unknown, "sd", "unknown effect scale must not default to sd")

    def test_build_selection_meta_empty_and_pending_excluded(self) -> None:
        rows = selection.build_selection(
            metadata_root=self.meta,
            mapping_path=self.mapping,
            metadata_content_sha256=self.content_sha,
        )
        self.assertEqual(len(rows), 15)
        manifest = selection.render_resolver_manifest(rows)
        self.assertEqual(manifest.count("\n") - 1, 15)
        # META is canonicalised to empty assigned_ancestry in the manifest.
        for line in manifest.splitlines()[1:]:
            fields = line.split("\t")
            if fields[1].startswith("GCST90479"):
                self.assertEqual(fields[MANIFEST_ASSIGNED_ANCESTRY], "")
        summary = selection.selection_summary(rows, self.content_sha)
        self.assertEqual(summary["pending"], 0)


MANIFEST_ASSIGNED_ANCESTRY = selection.MANIFEST_COLUMNS.index("assigned_ancestry")


class TestPreflight(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.tmp = Path(self.td.name)

    def tearDown(self) -> None:
        self.td.cleanup()

    def test_refuses_existing_root(self) -> None:
        existing = self.tmp / "out"
        existing.mkdir()
        with self.assertRaisesRegex(wrapper.WrapperError, "reuse"):
            wrapper.preflight_output_root(existing)

    def test_refuses_symlinked_parent(self) -> None:
        real = self.tmp / "real"
        real.mkdir()
        link = self.tmp / "link"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(wrapper.WrapperError, "symlinked"):
            wrapper.preflight_output_root(link / "child")

    def test_refuses_stores_component(self) -> None:
        with self.assertRaisesRegex(wrapper.WrapperError, "stores"):
            wrapper.preflight_output_root(self.tmp / "stores" / "child")

    def test_refuses_protected_root(self) -> None:
        with self.assertRaisesRegex(wrapper.WrapperError, "protected"):
            wrapper.preflight_output_root(self.tmp / "child", protected_roots=[self.tmp])


class TestCliGates(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.tmp = Path(self.td.name)
        self.meta, self.mapping = make_pilot_fixture(self.tmp)
        self.content_sha, _ = content_sha.metadata_content_sha256(self.meta)

    def tearDown(self) -> None:
        self.td.cleanup()
        os.environ.pop("FAKE_RESOLVER_MODE", None)

    def _args(self, mode: str, out: Path, extra: list[str] | None = None) -> list[str]:
        base = [mode, "--metadata-root", str(self.meta), "--mapping", str(self.mapping), "--out-root", str(out)]
        return base + (extra or [])

    def test_plan_is_metadata_only(self) -> None:
        out = self.tmp / "plan"
        self.assertEqual(cli.main(self._args("plan", out)), 0)
        self.assertTrue((out / "selection" / "pilot-analyses.tsv").is_file())
        self.assertTrue((out / "selection" / "plan-receipt.json").is_file())
        self.assertFalse((out / "receipt").exists())

    def test_run_without_execute_is_refused(self) -> None:
        out = self.tmp / "no-exec"
        rc = cli.main(self._args("run", out, ["--approved-metadata-sha", self.content_sha]))
        self.assertEqual(rc, 2)
        self.assertFalse(out.exists())

    def test_run_without_start_receipt_is_refused(self) -> None:
        out = self.tmp / "no-receipt"
        rc = cli.main(self._args("run", out, ["--execute", "--approved-metadata-sha", self.content_sha]))
        self.assertEqual(rc, 2)

    def _run(self, out: Path, mode: str, extra: list[str] | None = None) -> int:
        receipt = self.tmp / "start-receipt.json"
        receipt.write_text(
            json.dumps({"approved_metadata_sha256": self.content_sha, "authorized": True}),
            encoding="utf-8",
        )
        os.environ["FAKE_RESOLVER_MODE"] = mode
        return cli.main(
            self._args(
                "run",
                out,
                [
                    "--execute",
                    "--approved-metadata-sha",
                    self.content_sha,
                    "--start-receipt",
                    str(receipt),
                    "--resolver-executable",
                    str(FAKE_RESOLVER),
                    *(extra or []),
                ],
            )
        )

    def test_run_ok_commits(self) -> None:
        out = self.tmp / "ok"
        self.assertEqual(self._run(out, "ok"), 0)
        self.assertTrue((out / "COMMITTED").is_file())
        receipt = json.loads((out / "receipt" / "resolution-receipt.json").read_text())
        self.assertTrue(receipt["outcome"]["ok"])
        self.assertEqual(receipt["source_evidence_scope"], "capped_pilot_diagnostic")

    def test_run_wall_timeout_kills_whole_group(self) -> None:
        out = self.tmp / "timeout"
        rc = self._run(out, "sleep", ["--wall-limit-s", "1"])
        self.assertEqual(rc, 2)
        receipt = json.loads((out / "receipt" / "resolution-receipt.json").read_text())
        self.assertTrue(receipt["outcome"]["timed_out"])
        self.assertEqual(receipt["outcome"]["escaped_pids"], [])
        self.assertFalse((out / "COMMITTED").exists())

    def test_run_rss_breach_stops(self) -> None:
        out = self.tmp / "memory"
        rc = self._run(out, "memory", ["--rss-limit-mb", "100", "--wall-limit-s", "20"])
        self.assertEqual(rc, 2)
        receipt = json.loads((out / "receipt" / "resolution-receipt.json").read_text())
        self.assertTrue(receipt["outcome"]["rss_breached"])
        self.assertFalse((out / "COMMITTED").exists())

    def test_run_failure_does_not_commit(self) -> None:
        out = self.tmp / "fail"
        self.assertEqual(self._run(out, "fail"), 2)
        self.assertFalse((out / "COMMITTED").exists())


if __name__ == "__main__":
    unittest.main()
