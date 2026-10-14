#!/usr/bin/env python3
"""Generator-side default Indexed Variant Subset resolution (stores #210, epic #214).

Covers the shared resolver every Dense and Hybrid ``build.yaml`` writer calls:

* the default ``hm3`` declaration is read from the one maintained Reference
  Resource (``resources/reference-resources/hapmap3-hg38/resource.yaml``), so no
  writer copies the variant-list path, assembly or checksum;
* a Ragged layout resolves to ``None`` (emit no key at all);
* a reviewed opt-out is an explicit empty list plus a written reason;
* an empty list without a reason, a null value, a malformed entry, or a Ragged
  declaration is a generator error rather than a silent omission.

The R mirror is exercised by the R writer suites; this suite pins the Python
half and the one-source-of-truth values both must agree on.
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

from resources.generators.lib.indexed_subsets import (  # noqa: E402
    IndexedSubsetConfigError,
    default_hm3_declaration,
    resolve_indexed_subsets,
)

EXPECTED_HM3 = {
    "name": "hm3",
    "resource_id": "hapmap3-hg38",
    "variant_list": "/data/opengwasdb/reference/hapmap3-hg38/hm3.grch38.alid.txt",
    "reference_assembly": "GRCh38",
    "checksum": "4a7c82aa625a006ff437dcaa2769b00e056413d54c72e98f7378327c69513398",
}


class GeneratorDefaultTests(unittest.TestCase):
    def test_default_hm3_comes_from_the_maintained_resource(self) -> None:
        self.assertEqual(default_hm3_declaration(REPO_ROOT), EXPECTED_HM3)

    def test_dense_and_hybrid_default_to_hm3(self) -> None:
        for layout in ("dense", "hybrid"):
            with self.subTest(layout=layout):
                declarations, note = resolve_indexed_subsets({}, layout, REPO_ROOT)
                self.assertEqual(declarations, [EXPECTED_HM3])
                self.assertIsNone(note)

    def test_ragged_emits_no_key(self) -> None:
        declarations, note = resolve_indexed_subsets({}, "ragged", REPO_ROOT)
        self.assertIsNone(declarations)
        self.assertIsNone(note)

    def test_reviewed_opt_out_is_empty_list_plus_reason(self) -> None:
        declarations, note = resolve_indexed_subsets(
            {"indexed_subsets": [], "indexed_subsets_opt_out": "  predates the list  "},
            "hybrid",
            REPO_ROOT,
        )
        self.assertEqual(declarations, [])
        self.assertEqual(note, "predates the list")

    def test_empty_without_a_reason_is_an_error(self) -> None:
        for document in (
            {"indexed_subsets": []},
            {"indexed_subsets": [], "indexed_subsets_opt_out": ""},
            {"indexed_subsets": [], "indexed_subsets_opt_out": "   "},
        ):
            with self.subTest(document=document):
                with self.assertRaisesRegex(IndexedSubsetConfigError, "record why"):
                    resolve_indexed_subsets(document, "dense", REPO_ROOT)

    def test_null_is_malformed_not_omitted(self) -> None:
        with self.assertRaisesRegex(IndexedSubsetConfigError, "not null"):
            resolve_indexed_subsets({"indexed_subsets": None}, "hybrid", REPO_ROOT)
        with self.assertRaisesRegex(IndexedSubsetConfigError, "not null"):
            resolve_indexed_subsets({"indexed_subsets": None}, "ragged", REPO_ROOT)

    def test_ragged_may_never_declare_one(self) -> None:
        with self.assertRaisesRegex(IndexedSubsetConfigError, "Ragged"):
            resolve_indexed_subsets(
                {"indexed_subsets": [EXPECTED_HM3]}, "ragged", REPO_ROOT
            )

    def test_malformed_entries_are_errors(self) -> None:
        cases = [
            ([{"name": "hm3"}], "requires a non-empty string"),
            (["not-a-mapping"], "must be a mapping"),
            ([{**EXPECTED_HM3, "checksum": "zz"}], "64-character hex sha256"),
        ]
        for declared, message in cases:
            with self.subTest(declared=declared):
                with self.assertRaisesRegex(IndexedSubsetConfigError, message):
                    resolve_indexed_subsets(
                        {"indexed_subsets": declared}, "dense", REPO_ROOT
                    )

    def test_opt_out_reason_contradicts_a_non_empty_list(self) -> None:
        with self.assertRaisesRegex(IndexedSubsetConfigError, "contradicts"):
            resolve_indexed_subsets(
                {"indexed_subsets": [EXPECTED_HM3], "indexed_subsets_opt_out": "why"},
                "dense",
                REPO_ROOT,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
