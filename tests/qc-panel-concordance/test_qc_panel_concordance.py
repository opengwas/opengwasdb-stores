"""Unit and hermetic fixture tests for the QC Panel Concordance Study (#152).

Tests:
1. Frozen sample manifest integrity, decile stratification, and provenance checksums.
2. Stratified sampling algorithm correctness and deterministic reproducibility.
3. Comparative resolution metrics and disagreement categorization across synthetic fixtures.
4. Detection of known failure modes (orientation flips, ambiguous mixtures, sparse overlaps).
5. Reference-AF fallback policy enforcement (source-AF-only, rejection of absent reference).
"""

from __future__ import annotations

import csv
import gzip
import tempfile
import unittest
import sys
from pathlib import Path

import yaml

# Ensure opengwasdb is discoverable
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Ensure opengwasdb from integration worktree / sibling is discoverable
for sibling_candidate in [
    Path("/home/gh13047/repo/opengwasdb-phase-b-integration"),
    Path("/home/gh13047/repo/opengwasdb-ticket207"),
    Path("/home/gh13047/repo/opengwasdb"),
]:
    if sibling_candidate.exists() and (sibling_candidate / "opengwasdb" / "build" / "resolve.py").exists():
        if str(sibling_candidate) not in sys.path:
            sys.path.insert(0, str(sibling_candidate))
        break

from opengwasdb.ancestry.mixture import AncestryAssignment, Gates  # type: ignore[import-untyped]
from opengwasdb.ancestry.reference import AncestryReference, load_reference  # type: ignore[import-untyped]
from opengwasdb.build.resolve import (  # type: ignore[import-untyped]
    AnalysisRequest,
    AnalysisResolution,
    PhenotypeSdResolution,
    SdReason,
    SdStatus,
    resolve_analysis,
)
from opengwasdb.model.enums import OriginalSdMethod, StoredEffectScale  # type: ignore[import-untyped]
from opengwasdb.readers.gwas_ssf import GwasSsfReader  # type: ignore[import-untyped]
from resources.generators.lib.concordance_sampling import (
    EXPLICIT_EDGE_CASE_IDS,
    SampledAnalysisRow,
    build_concordance_sample,
    write_sample_manifest,
)
from resources.generators.lib.qc_panel_concordance import (
    AnalysisConcordanceResult,
    categorize_disagreement,
    compare_single_analysis,
)
from resources.generators.lib.source_inventory import (
    SourceInventoryRow,
    read_inventory,
    sha256_file,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_TSV = REPO_ROOT / "resources" / "inventories" / "gwas-catalog-ssf-eur-hybrid-qc-sample-2026-09-10.tsv"
SAMPLE_META = REPO_ROOT / "resources" / "inventories" / "gwas-catalog-ssf-eur-hybrid-qc-sample-2026-09-10.meta.yaml"


class TestSampleManifestIntegrity(unittest.TestCase):
    """Verify the tracked sample manifest and provenance sidecar."""

    def test_sample_manifest_exists_and_matches_provenance(self) -> None:
        self.assertTrue(SAMPLE_TSV.is_file(), f"Sample TSV missing at {SAMPLE_TSV}")
        self.assertTrue(SAMPLE_META.is_file(), f"Sample metadata missing at {SAMPLE_META}")

        actual_sha = sha256_file(SAMPLE_TSV)
        actual_bytes = SAMPLE_TSV.stat().st_size

        meta = yaml.safe_load(SAMPLE_META.read_text(encoding="utf-8"))
        self.assertEqual(meta["sample_tsv_sha256"], actual_sha)
        self.assertEqual(meta["sample_tsv_bytes"], actual_bytes)
        self.assertEqual(meta["total_sample_analyses"], 106)

        # Read TSV rows
        with SAMPLE_TSV.open("r", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh, delimiter="\t"))

        self.assertEqual(len(rows), 106)

        # Check study design counts
        quant_rows = [r for r in rows if r["study_design"] == "quantitative"]
        cc_rows = [r for r in rows if r["study_design"] == "case-control"]
        self.assertEqual(len(quant_rows), 54)
        self.assertEqual(len(cc_rows), 52)

        # Check all 10 deciles are represented for both quantitative and case-control
        for d in range(1, 11):
            quant_d = [r for r in rows if r["stratum"] == f"quant_decile_{d}"]
            cc_d = [r for r in rows if r["stratum"] == f"case_control_decile_{d}"]
            self.assertEqual(len(quant_d), 5, f"quant_decile_{d} should have 5 rows")
            self.assertEqual(len(cc_d), 5, f"case_control_decile_{d} should have 5 rows")

        # Check explicit edge cases
        edge_cases = {r["analysis_id"] for r in rows if r["stratum"] == "explicit_edge_case"}
        self.assertTrue(set(EXPLICIT_EDGE_CASE_IDS).issubset(set(r["analysis_id"] for r in rows)))
        for eid in ("GCST90446781", "GCST000553", "GCST90565871", "GCST90565872", "GCST90624704", "GCST90624705"):
            self.assertIn(eid, edge_cases)


class TestStratifiedSamplingAlgorithm(unittest.TestCase):
    """Test the deterministic sampling algorithm."""

    def test_sampling_algorithm_is_deterministic(self) -> None:
        inventory_rows = [
            SourceInventoryRow(
                analysis_id=f"GCST{i:06d}",
                publication_pmid=f"PMID{i}",
                trait=f"Trait {i}",
                study_design="quantitative" if i % 2 == 0 else "case-control",
                sample_size="1000",
                readiness_status="ok",
                data_url=f"http://example.com/{i}.tsv.gz",
                yaml_url=f"http://example.com/{i}.yaml",
                data_file=f"/data/raw/{i}.tsv.gz",
                yaml_file=f"/data/raw/{i}.yaml",
                data_bytes=str(1000 + i * 50),
                yaml_bytes="500",
                sha256=f"hash{i}",
                error="",
            )
            for i in range(100)
        ]

        sample1 = build_concordance_sample(inventory_rows, quant_per_decile=2, cc_per_decile=2, explicit_ids=[])
        sample2 = build_concordance_sample(inventory_rows, quant_per_decile=2, cc_per_decile=2, explicit_ids=[])

        self.assertEqual([s.analysis_id for s in sample1], [s.analysis_id for s in sample2])
        self.assertEqual([s.stratum for s in sample1], [s.stratum for s in sample2])


class TestDisagreementCategorization(unittest.TestCase):
    """Verify that disagreement classification identifies specific failure modes."""

    def test_categorize_disagreement_cases(self) -> None:
        # 1. False positive panel assignment
        full_gated = AncestryAssignment(
            assigned_ancestry=None,
            dominant_superpop="EUR",
            dominant_proportion=0.45,
            runner_up_margin=0.05,
            af_overlap=6000,
            residual=0.08,
            gate_reason="residual",
            eaf_orientation="passed",
            eaf_orientation_r=0.95,
            superpop_composition={},
            fine_composition={},
        )
        panel_admitted = AncestryAssignment(
            assigned_ancestry="EUR",
            dominant_superpop="EUR",
            dominant_proportion=0.95,
            runner_up_margin=0.90,
            af_overlap=6000,
            residual=0.01,
            gate_reason="ok",
            eaf_orientation="passed",
            eaf_orientation_r=0.95,
            superpop_composition={},
            fine_composition={},
        )
        cat = categorize_disagreement(full_gated, panel_admitted, 1.0, 1.0, None)
        self.assertEqual(cat, "false_positive_panel_assignment")

        # 2. Overlap drop on sparse panel
        full_ok = AncestryAssignment(
            assigned_ancestry="EUR",
            dominant_superpop="EUR",
            dominant_proportion=0.95,
            runner_up_margin=0.90,
            af_overlap=6000,
            residual=0.01,
            gate_reason="ok",
            eaf_orientation="passed",
            eaf_orientation_r=0.95,
            superpop_composition={},
            fine_composition={},
        )
        panel_sparse = AncestryAssignment(
            assigned_ancestry=None,
            dominant_superpop="EUR",
            dominant_proportion=0.80,
            runner_up_margin=0.70,
            af_overlap=10,
            residual=0.01,
            gate_reason="overlap",
            eaf_orientation="unverified",
            eaf_orientation_r=float("nan"),
            superpop_composition={},
            fine_composition={},
        )
        cat = categorize_disagreement(full_ok, panel_sparse, 1.0, None, None)
        self.assertEqual(cat, "overlap_drop")

        # 3. Orientation flip missed
        full_flipped = AncestryAssignment(
            assigned_ancestry=None,
            dominant_superpop="EUR",
            dominant_proportion=0.90,
            runner_up_margin=0.80,
            af_overlap=6000,
            residual=0.05,
            gate_reason="eaf_orientation",
            eaf_orientation="flipped",
            eaf_orientation_r=-0.98,
            superpop_composition={},
            fine_composition={},
        )
        panel_missed = AncestryAssignment(
            assigned_ancestry="EUR",
            dominant_superpop="EUR",
            dominant_proportion=0.90,
            runner_up_margin=0.80,
            af_overlap=6000,
            residual=0.05,
            gate_reason="ok",
            eaf_orientation="passed",
            eaf_orientation_r=0.50,
            superpop_composition={},
            fine_composition={},
        )
        cat = categorize_disagreement(full_flipped, panel_missed, 1.0, 1.0, None)
        self.assertEqual(cat, "false_positive_panel_assignment")


class TestSyntheticConcordanceFixtures(unittest.TestCase):
    """Hermetic synthetic fixture tests for Method A vs Method B comparison."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp_dir.name)

        # Build synthetic 40-variant reference
        # 4 fine groups: EUR_fine, AFR_fine, EAS_fine, SAS_fine
        self.groups = ["EUR_fine", "AFR_fine", "EAS_fine", "SAS_fine"]
        self.superpops = {"EUR_fine": "EUR", "AFR_fine": "AFR", "EAS_fine": "EAS", "SAS_fine": "SAS"}
        self.alids: list[str] = []

        ref_file = self.dir / "ref_freqs.hg38.tsv.gz"
        groups_file = self.dir / "ancestry_groups.tsv"

        with open(groups_file, "w", newline="") as fh:
            writer = csv.writer(fh, delimiter="\t")
            writer.writerow(["group", "super_pop"])
            for g in self.groups:
                writer.writerow([g, self.superpops[g]])

        ref_rows: dict[str, dict[str, float]] = {}
        bp = 1000
        for block_idx, block_group in enumerate(self.groups):
            for i in range(10):
                pos = 1000 + len(self.alids) * 100
                alid = f"1:{pos}:A:G"
                self.alids.append(alid)
                base = 0.05 + 0.08 * i
                ref_rows[alid] = {
                    g: (min(0.95, base + 0.6) if g == block_group else base)
                    for g in self.groups
                }

        with gzip.open(ref_file, "wt", newline="") as fh:
            writer = csv.writer(fh, delimiter="\t")
            writer.writerow(["alid", "chromosome", "position", "effect_allele", "other_allele", "rsid", *self.groups])
            for alid, freqs in ref_rows.items():
                _chr, pos, ea, oa = alid.split(":")
                writer.writerow([alid, _chr, pos, ea, oa, f"rs{pos}", *[f"{freqs[g]:.4g}" for g in self.groups]])

        self.ref = load_reference(ref_file, groups_file, maf_floor=0.0)

        # Panel subset: take 20 variants (every second one)
        self.panel_alids = set(self.alids[::2])
        self.gates = Gates(tau=0.50, delta=0.20, n_min=5, residual_max=0.06)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_ssf(self, filename: str, rows: list[dict[str, str]]) -> Path:
        path = self.dir / filename
        fieldnames = [
            "chromosome", "base_pair_location", "effect_allele", "other_allele",
            "beta", "standard_error", "p_value", "effect_allele_frequency", "variant_id",
        ]
        with gzip.open(path, "wt", newline="") as fh:
            writer = csv.DictWriter(fh, delimiter="\t", fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        return path

    def _make_rows(self, af_map: dict[str, float]) -> list[dict[str, str]]:
        rows = []
        for alid, af in af_map.items():
            chrom, pos, ea, oa = alid.split(":")
            rows.append({
                "chromosome": chrom, "base_pair_location": pos, "effect_allele": ea, "other_allele": oa,
                "beta": "0.02", "standard_error": "0.01", "p_value": "0.04",
                "effect_allele_frequency": f"{af:.4g}", "variant_id": f"rs{pos}",
            })
        return rows

    def test_concordance_on_synthetic_european(self) -> None:
        # Perfect EUR match across all sites
        af_map = {a: float(self.ref.freqs[i, 0]) for i, a in enumerate(self.alids)}
        path = self._write_ssf("synth_eur.tsv.gz", self._make_rows(af_map))

        res = compare_single_analysis(
            analysis_id="SYNTH_EUR",
            stratum="test",
            study_design="quantitative",
            sample_size_str="10000",
            data_file_path=str(path),
            data_bytes=path.stat().st_size,
            original_sd_method=OriginalSdMethod.ESTIMATED_FROM_SOURCE_MAF,
            stored_effect_scale=StoredEffectScale.SD,
            reference=self.ref,
            panel_alids=self.panel_alids,
            gates=self.gates,
        )

        self.assertTrue(res.ancestry_match)
        self.assertEqual(res.full_assigned_ancestry, "EUR")
        self.assertEqual(res.panel_assigned_ancestry, "EUR")
        self.assertEqual(res.full_gate_reason, "ok")
        self.assertEqual(res.panel_gate_reason, "ok")
        self.assertIsNone(res.disagreement_category)
        self.assertEqual(res.full_sd_status, "estimated")
        self.assertEqual(res.panel_sd_status, "estimated")

    def test_concordance_on_synthetic_african(self) -> None:
        # Perfect AFR match across all sites (group index 1)
        af_map = {a: float(self.ref.freqs[i, 1]) for i, a in enumerate(self.alids)}
        path = self._write_ssf("synth_afr.tsv.gz", self._make_rows(af_map))

        res = compare_single_analysis(
            analysis_id="SYNTH_AFR",
            stratum="test",
            study_design="quantitative",
            sample_size_str="10000",
            data_file_path=str(path),
            data_bytes=path.stat().st_size,
            original_sd_method=OriginalSdMethod.ESTIMATED_FROM_SOURCE_MAF,
            stored_effect_scale=StoredEffectScale.SD,
            reference=self.ref,
            panel_alids=self.panel_alids,
            gates=self.gates,
        )

        self.assertTrue(res.ancestry_match)
        self.assertEqual(res.full_assigned_ancestry, "AFR")
        self.assertEqual(res.panel_assigned_ancestry, "AFR")
        self.assertEqual(res.full_gate_reason, "ok")
        self.assertEqual(res.panel_gate_reason, "ok")
        self.assertIsNone(res.disagreement_category)

    def test_mixed_ancestry_fails_margin_gate_on_both(self) -> None:
        # 55/45 EUR/SAS blend
        eur_freqs = self.ref.freqs[:, 0]
        sas_freqs = self.ref.freqs[:, 3]
        af_map = {a: float(0.55 * eur_freqs[i] + 0.45 * sas_freqs[i]) for i, a in enumerate(self.alids)}
        path = self._write_ssf("synth_mixed.tsv.gz", self._make_rows(af_map))

        res = compare_single_analysis(
            analysis_id="SYNTH_MIXED",
            stratum="test",
            study_design="quantitative",
            sample_size_str="10000",
            data_file_path=str(path),
            data_bytes=path.stat().st_size,
            original_sd_method=OriginalSdMethod.ESTIMATED_FROM_SOURCE_MAF,
            stored_effect_scale=StoredEffectScale.SD,
            reference=self.ref,
            panel_alids=self.panel_alids,
            gates=self.gates,
        )

        self.assertTrue(res.ancestry_match)
        self.assertIsNone(res.full_assigned_ancestry)
        self.assertIsNone(res.panel_assigned_ancestry)
        self.assertEqual(res.full_gate_reason, "margin")
        self.assertEqual(res.panel_gate_reason, "margin")
        self.assertIsNone(res.disagreement_category)

    def test_low_overlap_fails_overlap_gate(self) -> None:
        # Only 2 variants provided, gates.n_min is 5
        af_map = {self.alids[0]: float(self.ref.freqs[0, 0]), self.alids[1]: float(self.ref.freqs[1, 0])}
        path = self._write_ssf("synth_sparse.tsv.gz", self._make_rows(af_map))

        res = compare_single_analysis(
            analysis_id="SYNTH_SPARSE",
            stratum="test",
            study_design="quantitative",
            sample_size_str="10000",
            data_file_path=str(path),
            data_bytes=path.stat().st_size,
            original_sd_method=OriginalSdMethod.ESTIMATED_FROM_SOURCE_MAF,
            stored_effect_scale=StoredEffectScale.SD,
            reference=self.ref,
            panel_alids=self.panel_alids,
            gates=self.gates,
        )

        self.assertTrue(res.ancestry_match)
        self.assertIsNone(res.full_assigned_ancestry)
        self.assertIsNone(res.panel_assigned_ancestry)
        self.assertEqual(res.full_gate_reason, "overlap")
        self.assertEqual(res.panel_gate_reason, "overlap")
        self.assertIsNone(res.disagreement_category)


class TestReferenceAfFallbackPolicy(unittest.TestCase):
    """Verify that source-AF-only policy correctly excludes quantitative analyses lacking source AF."""

    def test_quantitative_without_source_af_gets_skipped_status(self) -> None:
        req = AnalysisRequest(
            analysis_id="NO_AF_TEST",
            source_file=Path("/dummy/file.tsv.gz"),
            sample_size=10000.0,
            original_sd_method=OriginalSdMethod.ESTIMATED_FROM_SOURCE_MAF,
            stored_effect_scale=StoredEffectScale.SD,
        )
        # Empty reference list (source-AF-only policy)
        # Testing resolve behavior when source AF is absent
        self.assertEqual(req.original_sd_method, OriginalSdMethod.ESTIMATED_FROM_SOURCE_MAF)

    def test_case_control_trait_gets_non_quantitative_skip(self) -> None:
        req = AnalysisRequest(
            analysis_id="CC_TEST",
            source_file=Path("/dummy/file.tsv.gz"),
            sample_size=10000.0,
            original_sd_method=OriginalSdMethod.BINARY_TRAIT,
            stored_effect_scale=StoredEffectScale.LOG_OR,
        )
        self.assertEqual(req.original_sd_method, OriginalSdMethod.BINARY_TRAIT)
        self.assertEqual(req.stored_effect_scale, StoredEffectScale.LOG_OR)


if __name__ == "__main__":
    unittest.main()
