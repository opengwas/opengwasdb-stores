#!/usr/bin/env python3
"""Tests for ogstores.manifest and the build-manifest seam in plan() (ADR 0025).

The user-visible contract under test is that an `exclude_from_build: true` audit
row retained in the bundle never reaches the builder. The regression test
asserts that symptom directly -- the manifest path a planned build step consumes
is a derived file, and reading it back shows the excluded `analysis_id` absent --
rather than asserting an internal detail.

Verifies:
- plan() resolves the `analyses` token (positional and `--analyses`) and the
  step's declared inputs to the derived build manifest, so Snakemake builds it
  first; completion commands consume no analyses manifest.
- materialise_build_manifest() drops `exclude_from_build: true` rows
  case-insensitively and whitespace-trimmed, keeps every other column and the
  surviving rows in original order, and re-densifies `analysis_index` 0..n-1.
- A sidecar JSON names every dropped analysis_id and its `inclusion_reason`.
- Fail loudly on a malformed `exclude_from_build` value, an all-excluded
  manifest, a header-only manifest, and a missing `analysis_id` column.
- A bundle with no excluded rows passes through byte-for-byte unchanged.
"""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
SRC_DIR: Path = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import yaml

from ogstores import bundle, manifest, paths
from ogstores.manifest import (
    BuildManifestResult,
    EmptyBuildManifestError,
    MalformedExclusionError,
    MalformedRowError,
    MissingManifestColumnError,
    materialise_build_manifest,
)
from ogstores.plan import plan


def write_tsv(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    """Write a plain tab-separated table with the given column order."""
    lines = ["\t".join(columns)]
    for row in rows:
        lines.append("\t".join(row.get(col, "") for col in columns))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_tsv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        return list(reader.fieldnames or []), list(reader)


def create_bundle(
    registry_root: Path,
    store_id: str,
    columns: list[str],
    rows: list[dict[str, str]],
    *,
    layout: str = "dense",
    completion_state: str = "observed_only",
    command: str = "build-dense-vcf",
    artifacts_root: Path,
) -> bundle.Bundle:
    """Create a minimal valid Release Bundle for planning."""
    store_dir = registry_root / store_id
    store_dir.mkdir(parents=True, exist_ok=True)

    release = {
        "store_id": store_id,
        "label": f"fixture-{store_id}",
        "family": "fixture-family",
        "status": "candidate",
        "source_collection_id": "fixture-collection",
        "association_coverage": "full_gwas",
        "derived_from": None,
        "created_at": "2026-08-18T08:51:59Z",
        "source_snapshot_id": "fixture-snapshot",
        "release_kind": "pilot",
        "generator": {"command": "fixture-generator"},
    }
    phase = "complete" if completion_state == "reference_completed" else "build"
    build = {
        "store_id": store_id,
        "layout": layout,
        "completion_state": completion_state,
        "post": {"top_hits": False, "rho": False, "overview": False, "validate": True},
        "artifacts": {"root": str(artifacts_root)},
    }
    build[phase] = {"command": command, "options": {}}
    if completion_state == "reference_completed":
        release["derived_from"] = "OGS-00001"

    (store_dir / "release.yaml").write_text(yaml.safe_dump(release), encoding="utf-8")
    (store_dir / "build.yaml").write_text(yaml.safe_dump(build), encoding="utf-8")
    write_tsv(store_dir / "analyses.tsv", columns, rows)
    return bundle.load(store_id, registry_root=registry_root)


def builder_manifest_path(step) -> Path:
    """The `analyses.tsv` a planned build step consumes."""
    candidates = [p for p in step.inputs if p.name == "analyses.tsv"]
    assert len(candidates) == 1, f"expected exactly one analyses input, got {step.inputs}"
    return candidates[0]


BASE_COLUMNS = [
    "analysis_index",
    "analysis_id",
    "inclusion_reason",
    "exclude_from_build",
    "source_file",
]


def base_rows() -> list[dict[str, str]]:
    return [
        {
            "analysis_index": "0",
            "analysis_id": "KEPT_A",
            "inclusion_reason": "first_case_control_eur",
            "exclude_from_build": "",
            "source_file": "/raw/kept-a.tsv.gz",
        },
        {
            "analysis_index": "1",
            "analysis_id": "EXCLUDED_1",
            "inclusion_reason": "excluded_2026-08-22: EAF reported against the other allele; opengwasdb#115",
            "exclude_from_build": "true",
            "source_file": "/raw/excluded-1.tsv.gz",
        },
        {
            "analysis_index": "2",
            "analysis_id": "KEPT_B",
            "inclusion_reason": "first_case_control_eur",
            "exclude_from_build": "FALSE",
            "source_file": "/raw/kept-b.tsv.gz",
        },
        {
            "analysis_index": "3",
            "analysis_id": "EXCLUDED_2",
            "inclusion_reason": "excluded: duplicate of KEPT_B",
            "exclude_from_build": "  True  ",
            "source_file": "/raw/excluded-2.tsv.gz",
        },
        {
            "analysis_index": "4",
            "analysis_id": "KEPT_C",
            "inclusion_reason": "first_case_control_eur",
            "exclude_from_build": "",
            "source_file": "/raw/kept-c.tsv.gz",
        },
    ]


class TestBuildManifestSeam(unittest.TestCase):
    """The builder consumes the derived manifest, and excluded rows never reach it."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)
        self.registry_root = self.td / "stores"
        self.registry_root.mkdir()
        self.artifact_root = self.td / "artifacts"
        self.artifact_root.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_excluded_analysis_id_never_reaches_builder_manifest(self) -> None:
        """Regression: the manifest a build step consumes must not carry an excluded analysis_id."""
        store_id = "OGS-00077"
        b = create_bundle(
            self.registry_root,
            store_id,
            BASE_COLUMNS,
            base_rows(),
            artifacts_root=self.artifact_root,
        )

        build_step = plan(b, artifact_root=self.artifact_root)[0]
        consumed = builder_manifest_path(build_step)

        # The builder must not be pointed at the bundle's audit table, which
        # still holds EXCLUDED_1 and EXCLUDED_2.
        self.assertNotEqual(
            consumed.resolve(),
            b.analyses_path.resolve(),
            "build step consumes the bundle's audit analyses.tsv; excluded rows reach the builder",
        )
        self.assertEqual(consumed, paths.build_manifest_path(store_id, root=self.artifact_root))

        result = materialise_build_manifest(
            b.analyses_path,
            consumed,
            paths.build_manifest_sidecar_path(store_id, root=self.artifact_root),
        )
        self.assertEqual(result.n_dropped_rows, 2)

        _, built_rows = read_tsv(consumed)
        built_ids = [row["analysis_id"] for row in built_rows]
        self.assertNotIn("EXCLUDED_1", built_ids)
        self.assertNotIn("EXCLUDED_2", built_ids)
        self.assertEqual(built_ids, ["KEPT_A", "KEPT_B", "KEPT_C"])

        # The bundle still carries the audit rows.
        _, audit_rows = read_tsv(b.analyses_path)
        self.assertEqual(len(audit_rows), 5)

    def test_besd_analyses_flag_points_at_derived_manifest(self) -> None:
        """build-ragged-besd's `--analyses` flag also names the derived manifest."""
        store_id = "OGS-00078"
        b = create_bundle(
            self.registry_root,
            store_id,
            BASE_COLUMNS,
            base_rows(),
            layout="ragged",
            command="build-ragged-besd",
            artifacts_root=self.artifact_root,
        )
        b = bundle.Bundle(
            store_id=b.store_id,
            root=b.root,
            release={**b.release, "source_snapshot": {"besd_prefix": "/raw/besd-sample"}},
            build=b.build,
            analyses_path=b.analyses_path,
        )
        build_step = plan(b, artifact_root=self.artifact_root)[0]
        idx = build_step.argv.index("--analyses")
        self.assertEqual(
            build_step.argv[idx + 1],
            str(paths.build_manifest_path(store_id, root=self.artifact_root)),
        )
        self.assertEqual(builder_manifest_path(build_step), paths.build_manifest_path(store_id, root=self.artifact_root))

    def test_completion_command_consumes_no_analyses_manifest(self) -> None:
        """complete-* commands take only a parent Store; no manifest step applies."""
        store_id = "OGS-00079"
        b = create_bundle(
            self.registry_root,
            store_id,
            BASE_COLUMNS,
            base_rows(),
            layout="ragged",
            completion_state="reference_completed",
            command="complete-ragged",
            artifacts_root=self.artifact_root,
        )
        steps = plan(b, artifact_root=self.artifact_root)
        for step in steps:
            self.assertFalse(
                any(p.name == "analyses.tsv" for p in step.inputs),
                f"completion step {step.name} should not consume an analyses manifest",
            )


class TestMaterialiseBuildManifest(unittest.TestCase):
    """Filtering, re-densification, pass-through, and loud failure modes."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)
        self.source = self.td / "analyses.tsv"
        self.dest = self.td / "work" / "analyses.tsv"
        self.sidecar = self.td / "work" / "analyses.exclusions.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def materialise(self, columns: list[str], rows: list[dict[str, str]]) -> BuildManifestResult:
        write_tsv(self.source, columns, rows)
        return materialise_build_manifest(self.source, self.dest, self.sidecar)

    def test_excluded_rows_dropped_and_index_redensified(self) -> None:
        """Excluded rows vanish and analysis_index is re-densified 0..n-1 over survivors."""
        result = self.materialise(BASE_COLUMNS, base_rows())

        self.assertEqual(result.n_source_rows, 5)
        self.assertEqual(result.n_kept_rows, 3)
        self.assertEqual(result.n_dropped_rows, 2)
        self.assertTrue(result.redensified_analysis_index)

        columns, rows = read_tsv(self.dest)
        self.assertEqual(columns, BASE_COLUMNS)
        self.assertEqual([r["analysis_id"] for r in rows], ["KEPT_A", "KEPT_B", "KEPT_C"])
        self.assertEqual([r["analysis_index"] for r in rows], ["0", "1", "2"])

    def test_other_columns_and_row_order_survive(self) -> None:
        """Every other column and the surviving row order are unchanged."""
        result = self.materialise(BASE_COLUMNS, base_rows())
        self.assertEqual(result.n_kept_rows, 3)

        _, rows = read_tsv(self.dest)
        self.assertEqual(
            [r["source_file"] for r in rows],
            ["/raw/kept-a.tsv.gz", "/raw/kept-b.tsv.gz", "/raw/kept-c.tsv.gz"],
        )
        self.assertEqual(
            [r["inclusion_reason"] for r in rows],
            ["first_case_control_eur", "first_case_control_eur", "first_case_control_eur"],
        )
        # Surviving rows carry their own (blank/false) exclusion decision, unchanged.
        self.assertEqual(rows[0]["exclude_from_build"], "")
        self.assertEqual(rows[1]["exclude_from_build"], "FALSE")

    def test_exclusion_decisions_are_not_written_into_other_columns(self) -> None:
        """inclusion_reason and other columns are never rewritten with an exclusion decision."""
        result = self.materialise(BASE_COLUMNS, base_rows())
        _, rows = read_tsv(self.dest)
        for row in rows:
            self.assertNotIn("excluded", row["inclusion_reason"].lower())
        _, audit_rows = read_tsv(self.source)
        self.assertEqual(audit_rows[1]["inclusion_reason"], base_rows()[1]["inclusion_reason"])

    def test_no_analysis_index_column_is_not_invented(self) -> None:
        """re-densification happens only when analysis_index exists."""
        columns = ["analysis_id", "inclusion_reason", "exclude_from_build"]
        rows = [
            {"analysis_id": "A", "inclusion_reason": "kept", "exclude_from_build": ""},
            {"analysis_id": "B", "inclusion_reason": "kept", "exclude_from_build": "true"},
            {"analysis_id": "C", "inclusion_reason": "kept", "exclude_from_build": ""},
        ]
        result = self.materialise(columns, rows)
        self.assertFalse(result.redensified_analysis_index)
        out_columns, out_rows = read_tsv(self.dest)
        self.assertEqual(out_columns, columns)
        self.assertEqual([r["analysis_id"] for r in out_rows], ["A", "C"])

    def test_sidecar_names_dropped_analysis_ids_and_reasons(self) -> None:
        """The sidecar JSON names each dropped analysis_id and its inclusion_reason."""
        self.materialise(BASE_COLUMNS, base_rows())
        sidecar = json.loads(self.sidecar.read_text(encoding="utf-8"))
        self.assertEqual(sidecar["n_source_rows"], 5)
        self.assertEqual(sidecar["n_kept_rows"], 3)
        self.assertEqual(sidecar["n_dropped_rows"], 2)
        self.assertEqual(
            [d["analysis_id"] for d in sidecar["dropped"]], ["EXCLUDED_1", "EXCLUDED_2"]
        )
        self.assertIn("excluded_2026-08-22", sidecar["dropped"][0]["inclusion_reason"])
        self.assertEqual(sidecar["dropped"][1]["row_index"], 3)

    def test_no_excluded_rows_passes_through_unchanged(self) -> None:
        """A bundle with no excluded rows is written byte-for-byte unchanged."""
        columns = ["analysis_index", "analysis_id", "inclusion_reason", "exclude_from_build"]
        rows = [
            {"analysis_index": "0", "analysis_id": "A", "inclusion_reason": "kept", "exclude_from_build": ""},
            {"analysis_index": "1", "analysis_id": "B", "inclusion_reason": "kept", "exclude_from_build": "false"},
            {"analysis_index": "2", "analysis_id": "C", "inclusion_reason": "kept", "exclude_from_build": ""},
        ]
        before = None
        write_tsv(self.source, columns, rows)
        before = self.source.read_text(encoding="utf-8")

        result = materialise_build_manifest(self.source, self.dest, self.sidecar)
        self.assertEqual(result.n_dropped_rows, 0)
        self.assertEqual(result.dropped, [])
        self.assertEqual(self.dest.read_text(encoding="utf-8"), before)
        sidecar = json.loads(self.sidecar.read_text(encoding="utf-8"))
        self.assertEqual(sidecar["dropped"], [])

    def test_missing_exclude_column_passes_through(self) -> None:
        """A manifest without exclude_from_build has nothing to exclude."""
        columns = ["analysis_id", "inclusion_reason"]
        rows = [
            {"analysis_id": "A", "inclusion_reason": "kept"},
            {"analysis_id": "B", "inclusion_reason": "kept"},
        ]
        result = self.materialise(columns, rows)
        self.assertEqual(result.n_kept_rows, 2)
        _, out_rows = read_tsv(self.dest)
        self.assertEqual([r["analysis_id"] for r in out_rows], ["A", "B"])

    def test_malformed_exclude_value_raises(self) -> None:
        """A non blank/true/false exclusion value names the offending row and value."""
        columns = ["analysis_id", "inclusion_reason", "exclude_from_build"]
        rows = [
            {"analysis_id": "A", "inclusion_reason": "kept", "exclude_from_build": ""},
            {"analysis_id": "B", "inclusion_reason": "kept", "exclude_from_build": "maybe"},
        ]
        with self.assertRaises(MalformedExclusionError) as ctx:
            self.materialise(columns, rows)
        message = str(ctx.exception)
        self.assertIn("data row 1", message)
        self.assertIn("B", message)
        self.assertIn("'maybe'", message)

    def test_row_with_too_many_fields_raises(self) -> None:
        """An overflowing row is refused rather than having its extra field discarded."""
        self.source.write_text(
            "analysis_index\tanalysis_id\tnote\n"
            "0\tA1\tfine\n"
            "1\tA2\textra\tSURPRISE_EXTRA_FIELD\n"
            "2\tA3\tok\n",
            encoding="utf-8",
        )
        with self.assertRaises(MalformedRowError) as ctx:
            materialise_build_manifest(self.source, self.dest, self.sidecar)
        message = str(ctx.exception)
        self.assertIn(str(self.source), message)
        self.assertIn("data row 1", message)
        self.assertIn("A2", message)
        self.assertIn("has 4 fields; expected 3", message)
        self.assertFalse(self.dest.exists(), "no build manifest should be written")
        self.assertFalse(self.sidecar.exists(), "no sidecar should be written")

    def test_row_with_too_few_fields_raises(self) -> None:
        """A short row is refused rather than having its missing field invented as blank."""
        self.source.write_text(
            "analysis_index\tanalysis_id\tnote\n"
            "0\tA1\tfine\n"
            "1\tA2\n"
            "2\tA3\tok\n",
            encoding="utf-8",
        )
        with self.assertRaises(MalformedRowError) as ctx:
            materialise_build_manifest(self.source, self.dest, self.sidecar)
        message = str(ctx.exception)
        self.assertIn(str(self.source), message)
        self.assertIn("data row 1", message)
        self.assertIn("A2", message)
        self.assertIn("has 2 fields; expected 3", message)
        self.assertFalse(self.dest.exists(), "no build manifest should be written")
        self.assertFalse(self.sidecar.exists(), "no sidecar should be written")

    def test_blank_trailing_field_in_well_formed_row_is_accepted(self) -> None:
        """A genuinely blank trailing field is well-formed and must still pass."""
        self.source.write_text(
            "analysis_index\tanalysis_id\tnote\n"
            "0\tA1\tfine\n"
            "1\tA2\t\n"
            "2\tA3\tok\n",
            encoding="utf-8",
        )
        result = materialise_build_manifest(self.source, self.dest, self.sidecar)
        self.assertEqual(result.n_kept_rows, 3)
        _, rows = read_tsv(self.dest)
        self.assertEqual([r["analysis_id"] for r in rows], ["A1", "A2", "A3"])
        self.assertEqual(rows[1]["note"], "")

    def test_all_excluded_raises(self) -> None:
        """An all-excluded manifest refuses to write an empty build manifest."""
        columns = ["analysis_id", "inclusion_reason", "exclude_from_build"]
        rows = [
            {"analysis_id": "A", "inclusion_reason": "kept", "exclude_from_build": "true"},
            {"analysis_id": "B", "inclusion_reason": "kept", "exclude_from_build": "TRUE"},
        ]
        with self.assertRaises(EmptyBuildManifestError):
            self.materialise(columns, rows)
        self.assertFalse(self.dest.exists(), "no build manifest should be written")

    def test_header_only_manifest_raises(self) -> None:
        """A manifest with no data rows raises rather than producing an empty table."""
        with self.assertRaises(EmptyBuildManifestError):
            self.materialise(["analysis_id", "exclude_from_build"], [])

    def test_missing_required_analysis_id_column_raises(self) -> None:
        """A manifest without analysis_id raises a named error."""
        columns = ["inclusion_reason", "exclude_from_build"]
        rows = [{"inclusion_reason": "kept", "exclude_from_build": ""}]
        with self.assertRaises(MissingManifestColumnError) as ctx:
            self.materialise(columns, rows)
        self.assertIn("analysis_id", str(ctx.exception))

    def test_no_temp_files_left_behind(self) -> None:
        """Atomic writes leave only the manifest and sidecar in the work directory."""
        self.materialise(BASE_COLUMNS, base_rows())
        leftovers = [p.name for p in self.dest.parent.iterdir() if ".tmp." in p.name]
        self.assertEqual(leftovers, [])
        self.assertTrue(self.dest.is_file())
        self.assertTrue(self.sidecar.is_file())


class TestManifestPaths(unittest.TestCase):
    """Paths for the derived manifest are pure functions of the store id."""

    def test_paths_are_pure_functions_of_store_id(self) -> None:
        self.assertEqual(
            paths.build_manifest_path("OGS-00004"),
            paths.DEFAULT_ARTIFACT_ROOT / "OGS-00004" / "work" / "analyses.tsv",
        )
        self.assertEqual(
            paths.build_manifest_sidecar_path("OGS-00004", root="/tmp/artifacts"),
            Path("/tmp/artifacts/OGS-00004/work/analyses.exclusions.json"),
        )
        self.assertEqual(
            paths.build_manifest_path("OGS-00004", root="/tmp/artifacts"),
            paths.work_dir("OGS-00004", root="/tmp/artifacts") / "analyses.tsv",
        )


if __name__ == "__main__":
    unittest.main()
