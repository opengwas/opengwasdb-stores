#!/usr/bin/env python3
"""The ukb-b declared selection lives with Source Inventories (#252).

`families/ukb-b/releases/dense-observed-vcf-c128/analyses.tsv` was never only an
archived bundle: it is the declared Selection behind the restored UKB-B
inventory (`ukb-b-restored-2026-10-11`), so a Source Inventory receipt pins its
sha256, the provider metadata step compares against it, and two ukb-b generator
configs read it. Retiring `families/` therefore moved the file, and only the
file, into `resources/inventories/`.

The frozen inventory's evidence is the content hash, not the path: this suite
pins both. The bytes must still hash to the hash the receipt, the provenance
record and the inventory document all recorded in October 2026, and every
recorded path must now resolve to the inventory's own directory.

Run from the repository root:

    python3 tests/ukb-b/test_declared_selection_home.py
"""

from __future__ import annotations

import csv
import hashlib
import json
import unittest
from pathlib import Path

import yaml

from manifest_generation.providers.ukb_b import inventory as ukb

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_ID = "ukb-b-restored-2026-10-11"
NEW_DECLARED_SELECTION = f"resources/inventories/{SNAPSHOT_ID}.declared-selection.tsv"

# The hash frozen on 2026-10-12T00:48:51Z, recorded by the Source Inventory
# receipt, its provenance metadata and docs/ukb-b-restored-inventory-2026-10-11.md.
FROZEN_SHA256 = "0ae5c2d74d46dfcea7b2fcb36f3032e1ca4ae27117a1d6ac6915400cde060284"
FROZEN_ROWS = 2514


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestDeclaredSelectionHome(unittest.TestCase):
    """One file, moved, with its bytes and every recorded path kept honest."""

    def setUp(self) -> None:
        self.path = REPO_ROOT / NEW_DECLARED_SELECTION
        self.receipt = json.loads(
            (REPO_ROOT / "resources" / "inventories" / f"{SNAPSHOT_ID}.receipt.json").read_text(
                encoding="utf-8"
            )
        )
        self.provenance_path = REPO_ROOT / "resources" / "inventories" / f"{SNAPSHOT_ID}.meta.yaml"
        self.provenance = yaml.safe_load(self.provenance_path.read_text(encoding="utf-8"))

    def test_the_declared_selection_is_a_source_inventory_input(self) -> None:
        self.assertTrue(self.path.is_file(), f"{NEW_DECLARED_SELECTION} is missing")
        self.assertEqual(self.path.parent, REPO_ROOT / "resources" / "inventories")

    def test_the_content_hash_is_unchanged(self) -> None:
        self.assertEqual(sha256_file(self.path), FROZEN_SHA256)
        self.assertEqual(self.receipt["declared_selection_sha256"], FROZEN_SHA256)
        self.assertEqual(self.provenance["declared_selection"]["sha256"], FROZEN_SHA256)

    def test_the_frozen_membership_is_unchanged(self) -> None:
        with self.path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh, delimiter="\t"))
        self.assertEqual(len(rows), FROZEN_ROWS)
        self.assertEqual(self.provenance["declared_selection"]["rows"], FROZEN_ROWS)
        selection = ukb.read_declared_selection(self.path)
        self.assertEqual(len(selection.rows), FROZEN_ROWS)
        self.assertEqual(selection.sha256, FROZEN_SHA256)
        self.assertEqual(len({row.analysis_id for row in selection.rows}), FROZEN_ROWS)

    def test_the_receipt_path_resolves_inside_the_repository(self) -> None:
        recorded = self.receipt["declared_selection_path"]
        self.assertEqual(recorded, NEW_DECLARED_SELECTION)
        self.assertFalse(Path(recorded).is_absolute(), "the receipt records a repo-relative path")
        self.assertTrue((REPO_ROOT / recorded).is_file())

    def test_the_provenance_path_resolves_to_the_same_file(self) -> None:
        recorded = self.provenance["declared_selection"]["path"]
        resolved = Path(recorded) if Path(recorded).is_absolute() else REPO_ROOT / recorded
        self.assertTrue(resolved.is_file(), f"provenance records {recorded}, which does not exist")
        self.assertEqual(resolved.resolve(), self.path.resolve())

    def test_the_receipt_still_pins_its_own_provenance_record(self) -> None:
        self.assertEqual(self.receipt["provenance_path"], str(self.provenance_path.relative_to(REPO_ROOT)))
        self.assertEqual(self.receipt["provenance_sha256"], sha256_file(self.provenance_path))

    def test_the_generator_configs_read_the_new_location(self) -> None:
        candidate = yaml.safe_load(
            (REPO_ROOT / "resources" / "generators" / "ukb-b" / "config-candidate.yaml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(candidate["declared"], NEW_DECLARED_SELECTION)
        pilot = yaml.safe_load(
            (REPO_ROOT / "resources" / "generators" / "ukb-b" / "config-pilot-10.yaml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(pilot["inputs"]["candidates"], NEW_DECLARED_SELECTION)

    def test_the_inventory_tool_defaults_to_the_new_location(self) -> None:
        self.assertEqual(ukb.DEFAULT_DECLARED_SELECTION, NEW_DECLARED_SELECTION)
        args = ukb.parse_args(["reconcile", "--source-dir", "/tmp/does-not-matter"])
        self.assertEqual(args.declared, NEW_DECLARED_SELECTION)


if __name__ == "__main__":
    unittest.main()
