#!/usr/bin/env python3
"""Regression tests for the owner-authorized Store id reorder (#221).

Pins the FinnGen OGS-00016 -> OGS-00017 migration and the MVP META allocation at
OGS-00016, so a later bundle cannot silently drift back.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))


class TestIdReorder(unittest.TestCase):
    def release(self, store_id: str) -> dict:
        path = REPO_ROOT / "stores" / store_id / "release.yaml"
        self.assertTrue(path.is_file(), f"missing {path}")
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    def test_finngen_migrated_to_ogs_00017(self) -> None:
        release = self.release("OGS-00017")
        self.assertEqual(release["store_id"], "OGS-00017")
        self.assertEqual(release["label"], "r13-full")
        migration = release.get("migration") or {}
        self.assertEqual(migration.get("previous_store_id"), "OGS-00016")
        self.assertIn("Owner-authorized", str(migration.get("reason", "")))
        self.assertIn("OGS-00016", str(release.get("notes", "")))

    def test_old_ogs_00016_is_not_finngen(self) -> None:
        self.assertFalse((REPO_ROOT / "stores" / "OGS-00016").exists())
        for bundle in sorted((REPO_ROOT / "stores").glob("OGS-*")):
            if not bundle.is_dir() or not (bundle / "release.yaml").is_file():
                continue
            release = yaml.safe_load((bundle / "release.yaml").read_text(encoding="utf-8"))
            if release.get("label") == "r13-full":
                self.assertEqual(release.get("store_id"), "OGS-00017")

    def test_build_recipe_carries_new_identity(self) -> None:
        build = yaml.safe_load(
            (REPO_ROOT / "stores" / "OGS-00017" / "build.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(build["store_id"], "OGS-00017")

    def test_by_label_symlink_points_at_new_id(self) -> None:
        link = REPO_ROOT / "stores" / "by-label" / "r13-full"
        self.assertTrue(link.is_symlink(), "r13-full by-label symlink missing")
        self.assertEqual((link.parent / link.readlink()).resolve(), (REPO_ROOT / "stores" / "OGS-00017").resolve())

    def test_membership_table_unchanged_size(self) -> None:
        rows = (REPO_ROOT / "stores" / "OGS-00017" / "analyses.tsv").read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertEqual(len(rows) - 1, 2754)

    def test_mvp_config_allocates_meta_16_and_finngen_17(self) -> None:
        config = yaml.safe_load(
            (REPO_ROOT / "resources" / "generators" / "dbgap-mvp" / "config-candidate.yaml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(config["stores"]["META"]["observed_store_id"], "OGS-00016")
        self.assertEqual(config["finngen"]["current_store_id"], "OGS-00017")
        self.assertEqual(config["finngen"]["previous_store_id"], "OGS-00016")
        self.assertTrue(config.get("owner_ordering_approved"))

    def test_tracked_definitions_carry_new_allocation(self) -> None:
        definitions = yaml.safe_load(
            (REPO_ROOT / "resources" / "generators" / "dbgap-mvp" / "candidates.yaml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(definitions["stores"]["META"]["observed"]["store_id"], "OGS-00016")
        self.assertEqual(definitions["finngen"]["current_store_id"], "OGS-00017")

    def test_adr_0022_records_the_amendment(self) -> None:
        text = (REPO_ROOT / "docs" / "adr" / "0022-flat-opaque-store-ids.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("owner-authorized renumbering", text.lower())
        self.assertIn("OGS-00016", text)
        self.assertIn("OGS-00017", text)

    def test_migration_evidence_doc_exists(self) -> None:
        doc = REPO_ROOT / "docs" / "ogs-00016-to-ogs-00017-migration.md"
        self.assertTrue(doc.is_file())
        text = doc.read_text(encoding="utf-8")
        self.assertIn("5a3ac4e159ecf33437cd848fef44deb78d0a8dd11ab19ba0ab818065001fd1c0", text)

    def test_bundle_test_registered_list_uses_new_id(self) -> None:
        text = (REPO_ROOT / "tests" / "bundle" / "test_bundle.py").read_text(encoding="utf-8")
        self.assertIn("[*range(1, 12), 15, 17]", text)
        self.assertNotIn("[*range(1, 12), 16]", text)

    def test_bundle_golden_keys_use_new_id(self) -> None:
        golden = yaml.safe_load(
            (REPO_ROOT / "tests" / "bundle" / "golden" / "summaries.yaml").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn("OGS-00017", golden)
        self.assertNotIn("OGS-00016", golden)
        self.assertEqual(golden["OGS-00017"]["n_manifest"], 2754)


if __name__ == "__main__":
    unittest.main()
