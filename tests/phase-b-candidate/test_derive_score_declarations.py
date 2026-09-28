#!/usr/bin/env python3
"""Unit tests for the reviewed imputation-score declaration derivation (#176).

The synonym table is a fixed, case-sensitive decision; these tests pin the
precedence rule, the case sensitivity and the explicit look-alike exclusions so
that a future edit cannot widen the declaration silently.
"""

from __future__ import annotations

import gzip
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from resources.generators.lib.source_inventory import SourceInventoryRow  # noqa: E402

SCRIPT = REPO_ROOT / "resources/generators/gwas-catalog-eur-hybrid/derive_score_declarations.py"
_spec = importlib.util.spec_from_file_location("derive_score_declarations", SCRIPT)
assert _spec and _spec.loader
derive = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = derive
_spec.loader.exec_module(derive)


def _row(analysis_id: str, data_file: str, *, readiness: str = "ok") -> SourceInventoryRow:
    return SourceInventoryRow(
        analysis_id=analysis_id,
        publication_pmid="12345678",
        trait="Trait",
        study_design="quantitative",
        sample_size="5000",
        readiness_status=readiness,
        data_url="https://example.invalid/x.h.tsv.gz",
        yaml_url="https://example.invalid/x.h.tsv.gz-meta.yaml",
        data_file=data_file,
        yaml_file="/mirror/x.h.tsv.gz-meta.yaml",
        data_bytes="100",
        yaml_bytes="10",
        sha256="a" * 64,
        error="",
    )


def _write_header(root: Path, name: str, columns: list[str]) -> str:
    path = root / name
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write("\t".join(columns) + "\n")
    return str(path)


class SynonymTableTests(unittest.TestCase):
    def test_first_match_in_listed_order_wins(self) -> None:
        # `info` is listed before `info_score`; every imputation_info synonym is
        # listed before every imputation_r2 synonym.
        self.assertEqual(derive.declaration_for_columns(["info_score", "info"])[0], "info")
        self.assertEqual(derive.declaration_for_columns(["R2", "INFO"])[0], "INFO")
        self.assertEqual(
            derive.declaration_for_columns(["imputed_r2", "info"])[0], "info"
        )
        # R2 alone is the only r2 spelling that matches; the kind follows.
        self.assertEqual(
            derive.declaration_for_columns(["imputed_r2"]),
            (
                "imputed_r2",
                "imputation_r2",
                derive.SYNONYM_PROVENANCE_TEMPLATE.format(header="imputed_r2"),
            ),
        )

    def test_matching_is_case_sensitive(self) -> None:
        for header in ("info", "INFO", "Info"):
            self.assertEqual(derive.declaration_for_header(header), ("imputation_info", derive._provenance(header)))
        for header in ("info_score", "variant_info_score", "INFO_UKB", "imputationInfo", "info_score_from_regenie", "mininfo"):
            self.assertEqual(derive.declaration_for_header(header)[0], "imputation_info")
        # Uppercase R2 is a synonym; lowercase r2 and mixed case are not.
        self.assertEqual(derive.declaration_for_header("R2")[0], "imputation_r2")
        for header in ("r2", "iNfo", "INFo", "Info2", "info2"):
            self.assertIsNone(derive.declaration_for_header(header), header)

    def test_excluded_look_alikes_are_never_declared(self) -> None:
        for header in (
            "additional_info",
            "lowQuality",
            "z_score",
            "zscore",
            "r2_iCOGS",
            "icogs2_r2",
            "R2_oncoarray",
            "Yrsq",
            "mmm_var_info_nonmissing",
        ):
            with self.subTest(header=header):
                self.assertIsNone(derive.declaration_for_header(header))
                self.assertIsNone(derive.declaration_for_columns([header]))

    def test_provenance_strings_are_exact(self) -> None:
        info_kind, info_provenance = derive.declaration_for_header("info")
        self.assertEqual(info_kind, "imputation_info")
        self.assertEqual(
            info_provenance,
            "GWAS Catalog summary statistics format (GWAS-SSF) defines the info field "
            "as 'Imputation information metric', a number between 0 and 1 "
            "(https://www.ebi.ac.uk/gwas/docs/summary-statistics-format, retrieved "
            "2026-09-28)",
        )
        kind, provenance = derive.declaration_for_header("INFO")
        self.assertEqual(kind, "imputation_info")
        self.assertEqual(
            provenance,
            "Operator-approved synonym of the GWAS-SSF info field "
            "(opengwasdb-stores#176, 2026-09-28); source header 'INFO'",
        )


class DeriveDeclarationsTests(unittest.TestCase):
    def test_derive_reads_every_ready_header_and_skips_non_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            info_path = _write_header(root, "info.h.tsv.gz", ["hm_beta", "info"])
            r2_path = _write_header(root, "r2.h.tsv.gz", ["R2", "hm_beta"])
            none_path = _write_header(root, "none.h.tsv.gz", ["additional_info", "beta"])
            not_ready_path = _write_header(root, "not_ready.h.tsv.gz", ["info"])
            rows = (
                _row("GCST000002", info_path),
                _row("GCST000003", r2_path),
                _row("GCST000001", none_path),
                _row("GCST000004", not_ready_path, readiness="header_rejected"),
            )
            declarations = derive.derive_declarations(rows)
            self.assertEqual(
                [(d.analysis_id, d.column, d.kind) for d in declarations],
                [
                    ("GCST000002", "info", "imputation_info"),
                    ("GCST000003", "R2", "imputation_r2"),
                ],
            )
            text = derive.render_declarations(declarations)
            lines = text.splitlines()
            self.assertEqual(
                lines[0],
                "analysis_id\timputation_score_column\timputation_score_kind\t"
                "imputation_score_provenance",
            )
            self.assertEqual(len(lines), 3)

    def test_declaration_columns_match_the_candidate_workflow_contract(self) -> None:
        from resources.generators.lib.candidate_workflow import SCORE_DECLARATION_COLUMNS

        self.assertEqual(derive.DECLARATION_COLUMNS, SCORE_DECLARATION_COLUMNS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
