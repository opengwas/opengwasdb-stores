#!/usr/bin/env python3
"""The GWAS Catalog Acquisition Manifest writer: download passes -> one manifest (#283).

The EBI downloader writes one status manifest per acquisition pass. The writer
overlays them, earliest first -- a later pass wins for every Analysis it
covers, and may not turn a ready Analysis into a non-ready one -- into one
canonical Acquisition Manifest, and records the passes it read beside it.

Run from the repository root:
    python3 tests/freeze/test_gwas_catalog_acquisition_manifest.py
"""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

import yaml

from manifest_generation.acquisition_manifest import load_acquisition_manifest
from manifest_generation.providers.gwas_catalog.write_acquisition_manifest import (
    AcquisitionPass,
    main,
    parse_freeze_inputs,
    parse_pass_arguments,
    read_acquisition_pass,
    write_from_passes,
)
from manifest_generation.source_inventory import InventoryError, PreflightConfigError, sha256_file

PASS_HEADER = (
    "analysis_id", "publication_pmid", "trait", "study_design", "sample_size", "status",
    "data_url", "yaml_url", "data_file", "yaml_file", "data_bytes", "yaml_bytes",
    "sha256", "seconds", "error",
)


def ready(analysis_id: str, sha: str) -> dict[str, str]:
    return {
        "analysis_id": analysis_id, "publication_pmid": "1", "trait": "Height",
        "study_design": "quantitative", "sample_size": "100", "status": "ok",
        "data_url": f"https://ftp/{analysis_id}.h.tsv.gz", "yaml_url": f"https://ftp/{analysis_id}.h.tsv.gz-meta.yaml",
        "data_file": f"/mirror/{analysis_id}.h.tsv.gz", "yaml_file": f"/mirror/{analysis_id}.h.tsv.gz-meta.yaml",
        "data_bytes": "1000", "yaml_bytes": "800", "sha256": sha * 64, "seconds": "3.1",
    }


def failed(analysis_id: str) -> dict[str, str]:
    return {
        "analysis_id": analysis_id, "publication_pmid": "1", "trait": "Height",
        "study_design": "quantitative", "sample_size": "100", "status": "data_failed",
        "data_url": f"https://ftp/{analysis_id}.h.tsv.gz", "seconds": "0.4",
        "error": "curl: (28) timed out",
    }


class AcquisitionManifestWriterTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.out = self.tmp / "acquisition-manifest.tsv"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_pass(self, name: str, rows) -> Path:
        path = self.tmp / f"{name}.tsv"
        lines = ["\t".join(PASS_HEADER)] + ["\t".join(row.get(c, "") for c in PASS_HEADER) for row in rows]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_a_later_pass_wins_for_the_analyses_it_covers(self) -> None:
        base = self.write_pass("base", [failed("GCST1"), ready("GCST2", "b")])
        retry = self.write_pass("retry", [ready("GCST1", "a")])
        lineage = write_from_passes(
            (AcquisitionPass("base", base), AcquisitionPass("retry_transient", retry)), self.out
        )
        manifest = load_acquisition_manifest(self.out)
        self.assertEqual(
            [(entry.source_analysis_id, entry.status) for entry in manifest.entries],
            [("GCST1", "ok"), ("GCST2", "ok")],
        )
        entry = manifest.entry("GCST1")
        self.assertEqual(
            (entry.location, entry.bytes, entry.sha256, entry.source_url, entry.sidecar_location, entry.sidecar_bytes),
            ("/mirror/GCST1.h.tsv.gz", 1000, "a" * 64, "https://ftp/GCST1.h.tsv.gz",
             "/mirror/GCST1.h.tsv.gz-meta.yaml", 800),
        )
        # Intent columns and per-run timing stay out of the manifest.
        header = self.out.read_text(encoding="utf-8").splitlines()[0].split("\t")
        self.assertEqual(
            header,
            ["source_analysis_id", "status", "location", "bytes", "sha256", "source_url",
             "sidecar_location", "sidecar_url", "sidecar_bytes", "error"],
        )
        self.assertEqual(lineage, self.tmp / "acquisition-manifest.meta.yaml")
        document = yaml.safe_load(lineage.read_text(encoding="utf-8"))
        self.assertEqual(document["sha256"], sha256_file(self.out))
        self.assertEqual(
            [(p["role"], p["path"], p["sha256"], p["rows"], p["overrides"]) for p in document["passes"]],
            [("base", str(base), sha256_file(base), 2, 0), ("retry_transient", str(retry), sha256_file(retry), 1, 1)],
        )

    def test_a_later_pass_may_not_regress_a_ready_analysis(self) -> None:
        base = self.write_pass("base", [ready("GCST1", "a")])
        retry = self.write_pass("retry", [failed("GCST1")])
        with self.assertRaises(InventoryError) as caught:
            write_from_passes((AcquisitionPass("base", base), AcquisitionPass("retry", retry)), self.out)
        self.assertIn("GCST1", str(caught.exception))
        self.assertFalse(self.out.exists())

    def test_the_command_names_its_own_flag_in_errors(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["--pass", "no-equals-sign", "--out", str(self.out)])
        self.assertEqual(code, 1)
        self.assertIn("--pass expects ROLE=PATH", err.getvalue())

    def test_a_pass_with_a_duplicate_or_unknown_row_is_refused(self) -> None:
        duplicate = self.write_pass("dup", [ready("GCST1", "a"), ready("GCST1", "b")])
        with self.assertRaises(InventoryError) as caught:
            read_acquisition_pass(duplicate)
        self.assertIn("duplicate analysis_id: GCST1", str(caught.exception))
        unknown = self.write_pass("unknown", [{**ready("GCST1", "a"), "status": "teleported"}])
        with self.assertRaises(InventoryError) as caught:
            read_acquisition_pass(unknown)
        self.assertIn("unknown readiness status: teleported", str(caught.exception))


class FreezeInputsTest(unittest.TestCase):
    """The ordered ``{role, path}`` passes a config or the command line declares (moved from #151's tests, #347)."""

    CONFIG = Path("config.yaml")

    def test_preserves_the_declared_order(self) -> None:
        passes = parse_freeze_inputs(
            [{"role": "rescue", "path": "/m/rescue.tsv"}, {"role": "base", "path": "/m/base.tsv"}],
            self.CONFIG,
        )
        self.assertEqual([entry.role for entry in passes], ["rescue", "base"])
        self.assertEqual(passes[0].path, Path("/m/rescue.tsv"))
        self.assertEqual(passes[1].path, Path("/m/base.tsv"))

    def test_rejects_an_empty_pass_list(self) -> None:
        with self.assertRaises(PreflightConfigError) as caught:
            parse_freeze_inputs([], self.CONFIG)
        self.assertIn("non-empty ordered list", str(caught.exception))

    def test_rejects_a_duplicate_role(self) -> None:
        with self.assertRaises(PreflightConfigError) as caught:
            parse_freeze_inputs(
                [{"role": "base", "path": "/m/a.tsv"}, {"role": "base", "path": "/m/b.tsv"}], self.CONFIG
            )
        self.assertIn("declares role 'base' twice", str(caught.exception))

    def test_rejects_an_entry_missing_role_or_path(self) -> None:
        for entry in (
            {"path": "somewhere.tsv"},
            {"role": "base"},
            {"role": "", "path": "somewhere.tsv"},
            {"role": "base", "path": ""},
        ):
            with self.subTest(entry=entry):
                with self.assertRaises(PreflightConfigError) as caught:
                    parse_freeze_inputs([entry], self.CONFIG)
                self.assertIn("non-empty 'role' and 'path'", str(caught.exception))

    def test_rejects_a_non_mapping_entry(self) -> None:
        with self.assertRaises(PreflightConfigError) as caught:
            parse_freeze_inputs(["base-manifest.tsv"], self.CONFIG)
        self.assertIn("is not a mapping", str(caught.exception))

    def test_command_line_passes_keep_their_order_and_refuse_a_repeated_role(self) -> None:
        passes = parse_pass_arguments(["rescue=/m/r.tsv", "base=/m/b.tsv"], flag="--pass")
        self.assertEqual([(entry.role, entry.path) for entry in passes],
                         [("rescue", Path("/m/r.tsv")), ("base", Path("/m/b.tsv"))])
        with self.assertRaises(PreflightConfigError) as caught:
            parse_pass_arguments(["base=/m/a.tsv", "base=/m/b.tsv"], flag="--pass")
        self.assertIn("--pass declares role 'base' twice", str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
