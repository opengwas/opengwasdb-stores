#!/usr/bin/env python3
"""Hermetic tests for the bounded MVP metric-resolution pilot (#221).

No real association body or reference panel is opened: the resolver is a fake
CLI and every fixture is a temp directory. Covers the effect-scale provenance
rules, the fail-closed execution gates and limit validation, the resource
monitor (wall timeout killing the whole group, RSS breach), the read/write
guard, post-run record verification, and alias/protected-root refusals.
"""

from __future__ import annotations

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
FIXTURE_CODE_SHA = "b" * 40

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

ALL_IDS = sorted(a for entries in PILOT_META.values() for a, *_ in entries)


def write_tsv(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    import csv

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def make_pilot_fixture(tmp: Path) -> tuple[Path, Path, Path, Path]:
    import yaml

    meta = tmp / "meta"
    (meta / "manifests").mkdir(parents=True)
    (meta / "gwas-ssf").mkdir(parents=True)
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
                yaml.safe_dump({"gwas_id": analysis_id, "data_file_name": data.name, "genome_assembly": "GRCh38"}),
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
                    "dbgap_trait_type": "binary" if design == "case-control" else "quantitative",
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
    manifest_columns = [
        "analysis_id", "status", "sha256", "data_file", "yaml_file", "genome_assembly", "sample_size", "data_bytes",
    ]
    for bucket, rows in manifest_rows.items():
        write_tsv(meta / "manifests" / f"{bucket.lower()}.tsv", manifest_columns, rows)
    write_tsv(
        meta / "refresh-receipt.tsv",
        ["analysis_id", "note"],
        [{"analysis_id": row["analysis_id"], "note": "fixture"} for row in mapping_rows],
    )
    effect_path = tmp / "effect-provenance.tsv"
    write_tsv(
        effect_path,
        ["analysis_id", "stored_effect_scale", "original_effect_scale", "original_sd_method", "effect_scale_provenance"],
        [
            {
                "analysis_id": analysis_id,
                "stored_effect_scale": "log_or" if design == "case-control" else "sd",
                "original_effect_scale": "log_or" if design == "case-control" else "sd",
                "original_sd_method": "binary_trait" if design == "case-control" else "declared_standardised",
                "effect_scale_provenance": "owner_dbgap_source_declaration",
            }
            for analysis_id, design, *_ in (e for entries in PILOT_META.values() for e in entries)
        ],
    )
    candidate_path = tmp / "candidates.tsv"
    write_tsv(candidate_path, ["STUDY.ACCESSION"], [{"STUDY.ACCESSION": a} for a in ALL_IDS])
    content, _ = content_sha.metadata_content_sha256(meta)
    (meta / "parent-run-summary.json").write_text(
        json.dumps(
            {
                "code_sha": FIXTURE_CODE_SHA,
                "output_metadata_content_sha256": content,
                "membership": len(ALL_IDS),
                "association_opens": 0,
                "promotion": False,
                "store_operations": False,
                "metadata_idempotent": True,
                "source_metadata_unchanged": True,
                "refresh_exit_codes": [0, 0],
            }
        ),
        encoding="utf-8",
    )
    return meta, mapping_path, effect_path, candidate_path


class TestEffectScaleProvenance(unittest.TestCase):
    def test_design_alone_is_not_provenance(self) -> None:
        scale, method, original, provenance, status = selection.classify_effect_scale(
            {"study_design": "case-control", "n_cases": "100"}, {}
        )
        self.assertEqual(status, "pending")
        self.assertEqual((scale, method), ("", ""))
        self.assertNotEqual(scale, "log_or")

    def test_point_is_not_inverse_normal(self) -> None:
        scale, _, _, _, status = selection.classify_effect_scale(
            {"dbgap_trait_type": "quantitative", "analyzed_variable": "POINT measurement"}, {}
        )
        self.assertEqual(status, "pending")
        self.assertNotEqual(scale, "sd")

    def test_authoritative_dbgap_inv_norm_resolves_declared_standardised(self) -> None:
        scale, method, original, provenance, status = selection.classify_effect_scale(
            {"dbgap_trait_type": "quantitative", "analyzed_variable": "trait (inv-norm transformed)"}, {}
        )
        self.assertEqual((scale, method, original, status), ("sd", "declared_standardised", "sd", "resolved"))
        self.assertEqual(provenance, "dbgap_analyzed_variable_inv_norm_transformed")
        self.assertNotEqual(method, "", "a resolved sd row must not emit an empty original_sd_method")

    def test_authoritative_dbgap_binary_resolves_no_sd(self) -> None:
        scale, method, original, provenance, status = selection.classify_effect_scale(
            {"dbgap_trait_type": "binary", "analyzed_variable": "Breast Cancer"}, {}
        )
        self.assertEqual((scale, method, original, status), ("log_or", "binary_trait", "log_or", "resolved"))
        self.assertEqual(provenance, "dbgap_trait_type_binary")

    def test_explicit_original_sd_method_passthrough(self) -> None:
        scale, method, original, provenance, status = selection.classify_effect_scale(
            {},
            {
                "stored_effect_scale": "sd",
                "original_effect_scale": "sd",
                "original_sd_method": "source_provided",
                "effect_scale_provenance": "owner_x",
            },
        )
        self.assertEqual((scale, method, original, status), ("sd", "source_provided", "sd", "resolved"))

    def test_explicit_provenance_resolves(self) -> None:
        scale, method, original, provenance, status = selection.classify_effect_scale(
            {}, {"stored_effect_scale": "log_or", "effect_scale_provenance": "owner_x"}
        )
        self.assertEqual((scale, method, status), ("log_or", "binary_trait", "resolved"))


class TestSelection(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.tmp = Path(self.td.name)
        self.meta, self.mapping, self.effect, self.candidates = make_pilot_fixture(self.tmp)

    def tearDown(self) -> None:
        self.td.cleanup()

    def test_with_provenance_all_resolved(self) -> None:
        rows = selection.build_selection(
            metadata_root=self.meta,
            mapping_path=self.mapping,
            metadata_content_sha256="0" * 64,
            effect_provenance_path=self.effect,
        )
        self.assertEqual(len(rows), 15)
        self.assertEqual(len([r for r in rows if r.is_resolved]), 15)
        manifest = selection.render_resolver_manifest(rows)
        self.assertEqual(manifest.count("\n") - 1, 15)
        header = manifest.splitlines()[0].split("\t")
        sd_index = header.index("original_sd_method")
        for line in manifest.splitlines()[1:]:
            self.assertNotEqual(line.split("\t")[sd_index], "", "empty original_sd_method emitted")

    def test_authoritative_dbgap_resolves_without_effect_file(self) -> None:
        rows = selection.build_selection(
            metadata_root=self.meta,
            mapping_path=self.mapping,
            metadata_content_sha256="0" * 64,
        )
        self.assertEqual(len([r for r in rows if r.is_resolved]), 15)
        self.assertEqual(selection.render_resolver_manifest(rows).count("\n") - 1, 15)

    def test_unknown_provenance_stays_pending_and_excluded(self) -> None:
        import csv as _csv

        mapping_path = self.tmp / "unknown-mapping.tsv"
        with self.mapping.open(newline="", encoding="utf-8") as handle:
            reader = _csv.DictReader(handle, delimiter="\t")
            rows = list(reader)
            fields = list(reader.fieldnames or [])
        for row in rows:
            if row["analysis_id"] == "GCST90479501":
                row["dbgap_trait_type"] = ""
                row["analyzed_variable"] = "raw units"
        write_tsv(mapping_path, fields, rows)
        selected = selection.build_selection(
            metadata_root=self.meta,
            mapping_path=mapping_path,
            metadata_content_sha256="0" * 64,
        )
        pending = [r for r in selected if not r.is_resolved]
        self.assertEqual([r.analysis_id for r in pending], ["GCST90479501"])
        self.assertEqual(selection.render_resolver_manifest(selected).count("\n") - 1, 14)


class TestLimitsAndPreflight(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.tmp = Path(self.td.name)

    def tearDown(self) -> None:
        self.td.cleanup()

    def test_rejects_nonfinite_and_over_limits(self) -> None:
        for limits in (
            wrapper.ResourceLimits(wall_limit_s=float("nan")),
            wrapper.ResourceLimits(wall_limit_s=float("inf")),
            wrapper.ResourceLimits(wall_limit_s=-1),
            wrapper.ResourceLimits(rss_limit_mb=0),
            wrapper.ResourceLimits(rss_limit_mb=20_000),
            wrapper.ResourceLimits(max_workers=0),
            wrapper.ResourceLimits(max_workers=9),
        ):
            with self.assertRaises(wrapper.WrapperError):
                wrapper.validate_limits(limits)

    def test_preflight_refusals(self) -> None:
        existing = self.tmp / "out"
        existing.mkdir()
        with self.assertRaisesRegex(wrapper.WrapperError, "reuse"):
            wrapper.preflight_output_root(existing)
        real = self.tmp / "real"
        real.mkdir()
        link = self.tmp / "link"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(wrapper.WrapperError, "symlinked"):
            wrapper.preflight_output_root(link / "child")
        with self.assertRaisesRegex(wrapper.WrapperError, "stores"):
            wrapper.preflight_output_root(self.tmp / "stores" / "child")
        with self.assertRaisesRegex(wrapper.WrapperError, "protected"):
            wrapper.preflight_output_root(self.tmp / "child", protected_roots=[self.tmp])


class TestCliGates(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.tmp = Path(self.td.name)
        self.meta, self.mapping, self.effect, self.candidates = make_pilot_fixture(self.tmp)
        self.content_sha, _ = content_sha.metadata_content_sha256(self.meta)

    def tearDown(self) -> None:
        self.td.cleanup()
        os.environ.pop("FAKE_RESOLVER_MODE", None)

    def test_plan_is_metadata_only(self) -> None:
        out = self.tmp / "plan"
        rc = cli.main(
            [
                "plan", "--metadata-root", str(self.meta), "--mapping", str(self.mapping),
                "--candidate-table", str(self.candidates), "--effect-provenance", str(self.effect),
                "--out-root", str(out),
            ]
        )
        self.assertEqual(rc, 0)
        self.assertTrue((out / "selection" / "pilot-analyses.tsv").is_file())
        self.assertFalse((out / "receipt").exists())

    def test_run_without_execute_is_refused_before_mkdir(self) -> None:
        out = self.tmp / "no-exec"
        rc = cli.main(["run", "--metadata-root", str(self.meta), "--mapping", str(self.mapping), "--out-root", str(out)])
        self.assertEqual(rc, 2)
        self.assertFalse(out.exists())

    def test_run_rejects_bad_limit_before_mkdir(self) -> None:
        out = self.tmp / "bad-limit"
        rc = cli.main(
            [
                "run", "--metadata-root", str(self.meta), "--mapping", str(self.mapping), "--out-root", str(out),
                "--execute", "--approved-metadata-sha", self.content_sha, "--start-receipt", str(self._receipt()),
                "--rss-limit-mb", "nan",
            ]
        )
        self.assertEqual(rc, 2)
        self.assertFalse(out.exists())

    def test_run_rejects_unpinned_core_commit(self) -> None:
        out = self.tmp / "bad-core"
        rc = cli.main(
            [
                "run", "--metadata-root", str(self.meta), "--mapping", str(self.mapping), "--out-root", str(out),
                "--execute", "--approved-metadata-sha", self.content_sha, "--start-receipt", str(self._receipt()),
                "--core-commit", "0" * 40,
            ]
        )
        self.assertEqual(rc, 2)
        self.assertFalse(out.exists())

    def _receipt(self) -> Path:
        receipt = self.tmp / "start-receipt.json"
        receipt.write_text(
            json.dumps(
                {
                    "authorized": True,
                    "owner": "operator",
                    "scope": "15 bodies + reference loads",
                    "code_head": "c" * 40,
                    "core_commit": wrapper.PILOT_CORE_COMMIT,
                    "resolver_executable": str(FAKE_RESOLVER),
                    "analysis_ids": ALL_IDS,
                    "reference_paths": [str(wrapper.ANCESTRY_REFERENCE)],
                    "caps": {"rows": wrapper.ROW_CAP, "ancestry": wrapper.ANCESTRY_CAP},
                    "start_identity": "parent",
                    "approved_metadata_sha256": self.content_sha,
                }
            ),
            encoding="utf-8",
        )
        return receipt

    def _runtime_identity(self) -> Path:
        identity = self.tmp / "runtime-identity.json"
        identity.write_text(
            json.dumps({"core_commit": wrapper.PILOT_CORE_COMMIT, "executable": str(FAKE_RESOLVER)}),
            encoding="utf-8",
        )
        return identity

    def _run(self, out: Path, mode: str, extra: list[str] | None = None) -> int:
        os.environ["FAKE_RESOLVER_MODE"] = mode
        return cli.main(
            [
                "run", "--metadata-root", str(self.meta), "--mapping", str(self.mapping),
                "--candidate-table", str(self.candidates), "--effect-provenance", str(self.effect),
                "--out-root", str(out), "--execute", "--approved-metadata-sha", self.content_sha,
                "--start-receipt", str(self._receipt()), "--resolver-executable", str(FAKE_RESOLVER),
                "--runtime-identity", str(self._runtime_identity()),
                *(extra or []),
            ]
        )

    def test_run_ok_commits(self) -> None:
        out = self.tmp / "ok"
        self.assertEqual(self._run(out, "ok"), 0)
        self.assertTrue((out / "COMMITTED").is_file())
        receipt = json.loads((out / "receipt" / "resolution-receipt.json").read_text())
        self.assertTrue(receipt["outcome"]["ok"])
        self.assertEqual(receipt["outcome"]["escaped_pids"], [])
        self.assertIn("records", receipt)
        self.assertEqual(receipt["summary"]["pending"], 0)
        self.assertEqual(len(receipt["records"]["statuses"]), 15)

    def test_run_accounts_pending_rows_and_excludes_their_records(self) -> None:
        # One META row has no authoritative effect provenance: it stays pending,
        # is excluded from the resolver manifest, and no core record is faked.
        import csv as _csv

        with self.mapping.open(newline="", encoding="utf-8") as handle:
            reader = _csv.DictReader(handle, delimiter="\t")
            rows = list(reader)
            fields = list(reader.fieldnames or [])
        for row in rows:
            if row["analysis_id"] == "GCST90479501":
                row["dbgap_trait_type"] = ""
                row["analyzed_variable"] = "raw units"
        mapping_path = self.tmp / "pending-mapping.tsv"
        write_tsv(mapping_path, fields, rows)
        out = self.tmp / "pending"
        os.environ["FAKE_RESOLVER_MODE"] = "ok"
        rc = cli.main(
            [
                "run", "--metadata-root", str(self.meta), "--mapping", str(mapping_path),
                "--candidate-table", str(self.candidates),
                "--out-root", str(out), "--execute", "--approved-metadata-sha", self.content_sha,
                "--start-receipt", str(self._receipt()), "--resolver-executable", str(FAKE_RESOLVER),
                "--runtime-identity", str(self._runtime_identity()),
            ]
        )
        self.assertEqual(rc, 0)
        receipt = json.loads((out / "receipt" / "resolution-receipt.json").read_text())
        self.assertEqual(receipt["summary"]["pending"], 1)
        self.assertEqual(receipt["summary"]["pending_rows"][0]["analysis_id"], "GCST90479501")
        self.assertFalse((out / "records" / "GCST90479501.json").exists())

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

    def test_run_detached_child_is_contained(self) -> None:
        out = self.tmp / "detach"
        pid_file = self.tmp / "child.pid"
        os.environ["FAKE_CHILD_PID_FILE"] = str(pid_file)
        os.environ.pop("FAKE_RESOLVER_MODE", None)
        rc = cli.main(
            [
                "run", "--metadata-root", str(self.meta), "--mapping", str(self.mapping),
                "--candidate-table", str(self.candidates), "--effect-provenance", str(self.effect),
                "--out-root", str(out), "--execute", "--approved-metadata-sha", self.content_sha,
                "--start-receipt", str(self._receipt()), "--resolver-executable", str(FAKE_RESOLVER),
                "--runtime-identity", str(self._runtime_identity()), "--wall-limit-s", "1",
            ]
        )
        os.environ.pop("FAKE_CHILD_PID_FILE", None)
        self.assertIn(rc, (0, 2))
        receipt = json.loads((out / "receipt" / "resolution-receipt.json").read_text())
        self.assertEqual(receipt["outcome"]["escaped_pids"], [])
        if pid_file.is_file():
            child_pid = int(pid_file.read_text().strip())
            self.assertFalse(Path(f"/proc/{child_pid}").exists())

    def test_run_guard_denied_read_does_not_commit(self) -> None:
        out = self.tmp / "guard"
        rc = self._run(out, "read_denied")
        self.assertEqual(rc, 2)
        self.assertFalse((out / "COMMITTED").exists())


if __name__ == "__main__":
    unittest.main()
