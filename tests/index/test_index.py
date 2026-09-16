#!/usr/bin/env python3
"""Tests for ogstores.index: stores.tsv master list, STORES.md, and by-label symlinks (Issue #118).

Verifies the central contracts of ADR 0022, ADR 0023, and ADR 0024:
1. `stores.tsv`, `STORES.md`, and `by-label/` symlink trees generate correctly from bundles alone.
2. `build_command` is derived purely by `plan(bundle)`, never stored or hand-maintained.
3. Observed columns are extracted exclusively from `validation.yaml` in git, never from the artifact root.
4. Strict seam compliance: index reads git, never opening or inspecting artifact roots (proven via tripwires).
5. `docs/store-catalog.md` is deleted and subsumed by the generated views.
6. CI clean tree check: regenerating index against repository matches committed files with no drift.
"""

from __future__ import annotations

import builtins
import csv
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
SRC_DIR: Path = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from ogstores import bundle, index, paths
from ogstores.bundle import Bundle
from ogstores.index import (
    COLUMNS,
    build_index,
    generate_by_label_symlinks,
    generate_index,
    regenerate_index,
    render_stores_md,
    render_stores_row,
    render_stores_tsv,
)
from ogstores.plan import plan


def create_mock_bundle(
    stores_dir: Path,
    store_id: str,
    *,
    label: str,
    family: str,
    layout: str = "dense",
    completion_state: str = "observed_only",
    status: str = "built",
    derived_from: str | None = None,
    validation_data: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
) -> Bundle:
    """Create a minimal mock Release Bundle in stores_dir/<store_id>."""
    store_dir = stores_dir / store_id
    store_dir.mkdir(parents=True, exist_ok=True)

    rel_dict = {
        "store_id": store_id,
        "label": label,
        "family": family,
        "status": status,
        "source_collection_id": "test-col",
        "association_coverage": "full_gwas",
        "derived_from": derived_from,
        "created_at": "2026-08-18T08:51:59Z",
        "description": "Mock release",
        "source_snapshot_id": "mock-snap",
        "release_kind": "pilot",
        "generator": {"command": f"generate.py --config={label}.yaml"},
    }

    bld_dict: dict[str, Any] = {
        "store_id": store_id,
        "layout": layout,
        "completion_state": completion_state,
        "post": {"top_hits": False, "rho": False, "overview": True, "validate": True},
        "artifacts": {"root": "/data/opengwasdb/stores"},
    }
    if completion_state == "reference_completed":
        bld_dict["complete"] = {"command": "complete-dense", "options": options or {"ancestry": "EUR"}}
    else:
        bld_dict["build"] = {"command": "build-dense-vcf", "options": options or {"source-assembly": "hg38"}}

    with open(store_dir / "release.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(rel_dict, f)
    with open(store_dir / "build.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(bld_dict, f)

    (store_dir / "analyses.tsv").write_text("analysis_id\tsource_file\nana1\tfile1.vcf.gz\n")

    if validation_data is not None:
        with open(store_dir / "validation.yaml", "w", encoding="utf-8") as f:
            yaml.safe_dump(validation_data, f)

    return bundle.load(store_id, registry_root=stores_dir)


class TestIndexGenerationAndColumns(unittest.TestCase):
    """Test 19-column stores.tsv, STORES.md, and by-label generation."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)
        self.stores_dir = self.td / "stores"
        self.stores_dir.mkdir()
        self.docs_dir = self.td / "docs"
        self.docs_dir.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_canonical_19_columns_present_in_order(self) -> None:
        """stores.tsv contains exactly the 19 canonical columns in defined order."""
        self.assertEqual(len(COLUMNS), 19)
        expected = (
            "store_id",
            "label",
            "family",
            "layout",
            "completion_state",
            "status",
            "derived_from",
            "store_uri",
            "created_at",
            "opengwasdb_rev",
            "generator_command",
            "build_command",
            "format_version",
            "n_analyses",
            "n_variants",
            "n_associations",
            "store_bytes",
            "build_elapsed_s",
            "validate_status",
        )
        self.assertEqual(COLUMNS, expected)

    def test_build_command_is_rendered_purely_by_plan(self) -> None:
        """build_command is derived from plan(bundle)[0].argv, not stored or hard-maintained."""
        b = create_mock_bundle(
            self.stores_dir,
            "OGS-00010",
            label="custom-options-pilot",
            family="test-fam",
            options={"source-assembly": "hg38", "n-workers": 16, "chunk-variants": 5000},
        )
        row = render_stores_row(b)
        self.assertTrue(row["build_command"].startswith("opengwasdb build-dense-vcf"))
        # The published command names the derived build manifest, not the bundle's
        # audit analyses.tsv (ADR 0025).
        self.assertIn("/data/opengwasdb/stores/OGS-00010/work/analyses.tsv", row["build_command"])
        self.assertNotIn("stores/OGS-00010/analyses.tsv", row["build_command"])
        self.assertIn("--n-workers 16", row["build_command"])
        self.assertIn("--chunk-variants 5000", row["build_command"])
        self.assertIn("--source-assembly hg38", row["build_command"])

    def test_observed_columns_extracted_from_validation_yaml_only(self) -> None:
        """Observed columns come from validation.yaml; unbuilt releases have empty observed columns."""
        val_data = {
            "status": "passed",
            "validated_at": "2026-09-14T12:00:00Z",
            "observed": {
                "format_version": "1.0",
                "n_analyses": 20,
                "n_variants": 10000,
                "n_associations": 200000,
                "store_bytes": 5242880,
                "build_elapsed_s": 42.5,
                "validate_status": "passed",
            },
        }
        b_built = create_mock_bundle(
            self.stores_dir,
            "OGS-00011",
            label="built-release",
            family="test-fam",
            status="built",
            validation_data=val_data,
        )
        b_cand = create_mock_bundle(
            self.stores_dir,
            "OGS-00012",
            label="candidate-release",
            family="test-fam",
            status="candidate",
            validation_data=None,  # No validation.yaml
        )

        row_built = render_stores_row(b_built)
        self.assertEqual(row_built["format_version"], "1.0")
        self.assertEqual(row_built["n_analyses"], "20")
        self.assertEqual(row_built["n_variants"], "10000")
        self.assertEqual(row_built["n_associations"], "200000")
        self.assertEqual(row_built["store_bytes"], "5242880")
        self.assertEqual(row_built["build_elapsed_s"], "42.5")
        self.assertEqual(row_built["validate_status"], "passed")

        row_cand = render_stores_row(b_cand)
        self.assertEqual(row_cand["format_version"], "")
        self.assertEqual(row_cand["n_analyses"], "")
        self.assertEqual(row_cand["n_variants"], "")
        self.assertEqual(row_cand["n_associations"], "")
        self.assertEqual(row_cand["store_bytes"], "")
        self.assertEqual(row_cand["build_elapsed_s"], "")
        self.assertEqual(row_cand["validate_status"], "")

    def test_stores_tsv_and_md_and_by_label_generation_end_to_end(self) -> None:
        """generate_index creates stores.tsv, STORES.md, by-label symlinks, and deletes store-catalog.md."""
        # Create mock store-catalog.md in docs/
        store_cat_p = self.docs_dir / "store-catalog.md"
        store_cat_p.write_text("# Stale Store Catalog\n")
        self.assertTrue(store_cat_p.is_file())

        b1 = create_mock_bundle(self.stores_dir, "OGS-00001", label="pilot-1", family="fam-a")
        b2 = create_mock_bundle(self.stores_dir, "OGS-00002", label="pilot-2", family="fam-b")

        mock_artifact_root = self.td / "artifacts"
        mock_artifact_root.mkdir()

        tsv_p, md_p = generate_index(
            registry_root=self.stores_dir,
            repo_root=self.td,
            artifact_root=mock_artifact_root,
        )

        self.assertTrue(tsv_p.is_file())
        self.assertTrue(md_p.is_file())

        # Assert store-catalog.md was deleted
        self.assertFalse(store_cat_p.exists(), "docs/store-catalog.md must be deleted by generate_index")

        # Verify stores.tsv structure and rows
        with open(tsv_p, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter="\t")
            self.assertEqual(tuple(reader.fieldnames or ()), COLUMNS)
            rows = list(reader)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["store_id"], "OGS-00001")
            self.assertEqual(rows[1]["store_id"], "OGS-00002")

        # Verify STORES.md content
        md_text = md_p.read_text(encoding="utf-8")
        self.assertIn("# OpenGWASDB Store Releases", md_text)
        self.assertIn("| `OGS-00001` | pilot-1 | fam-a |", md_text)
        self.assertIn("| `OGS-00002` | pilot-2 | fam-b |", md_text)

        # Verify stores/by-label/ symlinks
        by_label_dir = self.stores_dir / "by-label"
        self.assertTrue(by_label_dir.is_dir())
        link1 = by_label_dir / "pilot-1"
        link2 = by_label_dir / "pilot-2"
        self.assertTrue(link1.is_symlink())
        self.assertTrue(link2.is_symlink())
        self.assertEqual(os.readlink(link1), "../OGS-00001")
        self.assertEqual(os.readlink(link2), "../OGS-00002")

        # Verify artifact_root/by-label/ symlinks
        art_by_label_dir = mock_artifact_root / "by-label"
        self.assertTrue(art_by_label_dir.is_dir())
        art_link1 = art_by_label_dir / "pilot-1"
        self.assertTrue(art_link1.is_symlink())
        self.assertEqual(os.readlink(art_link1), "../OGS-00001")


class TestIndexStrictSeamAndTripwires(unittest.TestCase):
    """Verify index reads git and never inspects or opens artifact root / Store files (ADR 0023)."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.td = Path(self.temp_dir.name)
        self.stores_dir = self.td / "stores"
        self.stores_dir.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_tripwire_index_opens_no_artifact_root_files_or_store(self) -> None:
        """Strict seam proof (ADR 0023): index reads git only, opening zero artifact files."""
        create_mock_bundle(self.stores_dir, "OGS-00021", label="seam-pilot", family="fam-seam")

        artifact_root = self.td / "fake_data_opengwasdb"
        artifact_root.mkdir()
        store_file = artifact_root / "OGS-00021" / "store.opengwasdb"
        store_file.mkdir(parents=True)
        (store_file / "manifest.json").write_text('{"store_id": "illegal"}')

        real_open = builtins.open
        real_read_bytes = Path.read_bytes
        real_read_text = Path.read_text
        real_path_open = Path.open
        illegal_opens: list[str] = []

        def check_path(file: Any) -> None:
            path_str = str(file)
            if str(artifact_root) in path_str and not path_str.endswith("by-label"):
                illegal_opens.append(path_str)
                raise AssertionError(f"Tripwire: index illegally accessed artifact file {path_str}")

        def guarded_open(file: Any, *args: Any, **kwargs: Any) -> Any:
            check_path(file)
            return real_open(file, *args, **kwargs)

        def guarded_read_bytes(self_path: Path, *args: Any, **kwargs: Any) -> bytes:
            check_path(self_path)
            return real_read_bytes(self_path, *args, **kwargs)

        def guarded_read_text(self_path: Path, *args: Any, **kwargs: Any) -> str:
            check_path(self_path)
            return real_read_text(self_path, *args, **kwargs)

        def guarded_path_open(self_path: Path, *args: Any, **kwargs: Any) -> Any:
            check_path(self_path)
            return real_path_open(self_path, *args, **kwargs)

        try:
            builtins.open = guarded_open  # type: ignore[assignment]
            Path.read_bytes = guarded_read_bytes  # type: ignore[assignment]
            Path.read_text = guarded_read_text  # type: ignore[assignment]
            Path.open = guarded_path_open  # type: ignore[assignment]
            generate_index(
                registry_root=self.stores_dir,
                repo_root=self.td,
                artifact_root=artifact_root,
            )
        finally:
            builtins.open = real_open  # type: ignore[assignment]
            Path.read_bytes = real_read_bytes  # type: ignore[assignment]
            Path.read_text = real_read_text  # type: ignore[assignment]
            Path.open = real_path_open  # type: ignore[assignment]

        self.assertEqual(illegal_opens, [], "index opened artifact files violating ADR 0023")


class TestRepoIndexCleanTree(unittest.TestCase):
    """Verify regenerating index on the current repository matches committed stores.tsv and STORES.md."""

    def test_current_repo_index_matches_committed_files_with_clean_tree(self) -> None:
        """Regenerating index on repo matches stores.tsv, STORES.md, and by-label symlinks exactly."""
        tsv_path = REPO_ROOT / "stores.tsv"
        md_path = REPO_ROOT / "STORES.md"
        by_label_dir = REPO_ROOT / "stores" / "by-label"

        self.assertTrue(tsv_path.is_file(), "stores.tsv must exist in repository root")
        self.assertTrue(md_path.is_file(), "STORES.md must exist in repository root")
        self.assertTrue(by_label_dir.is_dir(), "stores/by-label/ must exist")

        # Generate index in a temp location from repo's actual stores/
        with tempfile.TemporaryDirectory() as td:
            temp_repo = Path(td)
            gen_tsv, gen_md = generate_index(
                registry_root=REPO_ROOT / "stores",
                repo_root=temp_repo,
            )
            # Compare contents
            self.assertEqual(
                gen_tsv.read_text(encoding="utf-8"),
                tsv_path.read_text(encoding="utf-8"),
                "stores.tsv is dirty or out of date. Run 'pixi run index' to regenerate.",
            )
            self.assertEqual(
                gen_md.read_text(encoding="utf-8"),
                md_path.read_text(encoding="utf-8"),
                "STORES.md is dirty or out of date. Run 'pixi run index' to regenerate.",
            )

        # Verify all bundles in repo stores/ have matching symlinks in stores/by-label/
        repo_stores = [
            d.name for d in (REPO_ROOT / "stores").iterdir()
            if d.is_dir() and paths.is_valid_store_id(d.name) and (d / "release.yaml").is_file()
        ]
        self.assertGreater(len(repo_stores), 0)

        symlink_count = 0
        for sid in repo_stores:
            b = bundle.load(sid, registry_root=REPO_ROOT / "stores")
            if b.label:
                link = by_label_dir / b.label
                self.assertTrue(link.is_symlink(), f"Missing symlink for {b.label} in stores/by-label/")
                self.assertEqual(os.readlink(link), f"../{sid}")
                symlink_count += 1

        all_symlinks = [p for p in by_label_dir.iterdir() if p.is_symlink()]
        self.assertEqual(len(all_symlinks), symlink_count)


if __name__ == "__main__":
    unittest.main()
