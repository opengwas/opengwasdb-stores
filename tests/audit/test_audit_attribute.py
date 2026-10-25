#!/usr/bin/env python3
"""The attribute command (`ogstores.audit attribute`, stores #373).

Part of epic #371. The epic's first slice reconciled the off-panel axis (#372);
this one attributes the 101M-variant union to the Analyses and publications that
observe it, and pins a seeded sample for the raw-row trace (#374). The tests are
CLI-level for the same reason #372's are: the report and the sample TSV are the
externally visible evidence, so the assertions are on the command's exit code,
its JSON, its TSV and the state of the input Store.

The fixture is a real format-0.2.0 Hybrid Store Release of a handful of
variants, written through OpenGWASDB's own writers, whose Ragged body is built
from an explicit list of `(variant_index, analysis_index)` pairs. That is the
point of this suite: unlike #372's round-robin fixture, the *assignment* is the
thing under test, so every expected count below is arithmetic done by hand in
the fixture's own docstring.

```text
variants 0..7, Dense rows {0, 1}, so off-panel = 2..7
A0 (pmid 1001, Barnes DR)  -> v2, v3, v7          3 associations
A1 (pmid 1001, Barnes DR)  -> v3                  1
A2 (pmid 1002, Chen J)     -> v4, v5, v7          3
A3 (pmid 1003, Williams AT)-> v5, v6, v7          3
```

So: exclusive to one Analysis is 3 (v2/A0, v4/A2, v6/A3); exclusive to one
publication is 4 (v2 and v3 to 1001, v4 to 1002, v6 to 1003), of which v3 is
shared by two Analyses of one publication; and the union is 6 variants over 10
associations.
"""

from __future__ import annotations

import contextlib
import gzip
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import zarr

from opengwasdb.encoding.plan import Z_MISSING
from opengwasdb.model.manifest import StoreManifest
from opengwasdb.variants.axis import write_variant_axis
from opengwasdb.variants.normalise import CanonicalVariant

from ogstores import audit

STORE_ID = "OGS-00092"

n_checks = 0


def record_check() -> None:
    global n_checks
    n_checks += 1


# ---------------------------------------------------------------------------
# Fixtures: a Hybrid Store Release whose Ragged body is stated, not generated
# ---------------------------------------------------------------------------

#: The Analyses' publications, as `analyses.tsv` names them.
PUBLICATIONS = [
    ("1001", "Barnes DR"),
    ("1001", "Barnes DR"),
    ("1002", "Chen J"),
    ("1003", "Williams AT"),
]

#: One allele pair per axis row: a SNP, an indel and a complex substitution, so
#: the allele-class breakdown has something to separate.
ALLELES = [
    ("A", "G"),
    ("A", "G"),
    ("A", "G"),
    ("A", "G"),
    ("C", "T"),
    ("A", "G"),
    ("A", "AT"),
    ("AC", "GT"),
]

#: The off-panel association body, as explicit pairs. See the module docstring.
PAIRS = [
    (2, 0),
    (3, 0),
    (3, 1),
    (4, 2),
    (5, 2),
    (5, 3),
    (6, 3),
    (7, 0),
    (7, 2),
    (7, 3),
]

DENSE_ROWS = [0, 1]


def axis_variants() -> list[CanonicalVariant]:
    """Eight canonical variants in tabix order, seven on contig 1 and one on 2."""
    out = []
    for row, (effect, other) in enumerate(ALLELES):
        chromosome = "2" if row == 7 else "1"
        out.append(
            CanonicalVariant(
                chromosome=chromosome,
                position=100 + 10 * row,
                effect_allele=effect,
                other_allele=other,
            )
        )
    return out


def hybrid_provenance(reference: Path, *, n_variants: int, n_analyses: int, n_panel: int, n_rows: int) -> dict:
    return {
        "builder": "fixture.hybrid",
        "chain_file": "none",
        "variant_reference": str(reference),
        "n_analyses": n_analyses,
        "n_variants": n_variants,
        "hybrid": {
            "dense_component": "dense",
            "n_panel": n_panel,
            "n_off_panel": n_variants - n_panel,
            "n_overflow_associations": n_rows,
        },
        "ragged": {"by_variant": {"group": "by_variant", "n_axis": n_variants, "n_rows": n_rows}},
    }


ANALYSES_COLUMNS = (
    "analysis_index",
    "analysis_id",
    "analysis_label",
    "stored_effect_scale",
    "assigned_ancestry",
    "ancestry_assignment_method",
    "sample_size_kind",
    "sample_size_scope",
    "sample_size",
    "original_effect_scale",
    "original_sd_method",
    "publication_doi",
    "publication_pmid",
    "consortium",
    "first_author",
)


def write_analyses(
    store: Path,
    publications: list[tuple[str, str]],
    *,
    store_id: str = STORE_ID,
    row_order: list[int] | None = None,
) -> None:
    """Write `analyses.tsv` in the shape a real Store Release carries.

    `row_order` writes the rows in that order of `analysis_index`, so a suite can
    prove that metadata is read by `analysis_index` rather than by file order.
    """
    lines = ["\t".join(ANALYSES_COLUMNS) + "\n"]
    order = list(range(len(publications))) if row_order is None else list(row_order)
    for index in order:
        pmid, author = publications[index]
        row = {
            "analysis_index": str(index),
            "analysis_id": f"GCST{900000 + index}",
            "analysis_label": f"Trait {index}",
            "stored_effect_scale": "sd",
            "assigned_ancestry": "EUR",
            "ancestry_assignment_method": "af_assigned",
            "sample_size_kind": "total",
            "sample_size_scope": "analysis_level",
            "sample_size": "10000",
            "original_effect_scale": "sd",
            "original_sd_method": "estimated_from_source_maf",
            "publication_doi": "",
            "publication_pmid": pmid,
            "consortium": "",
            "first_author": author,
        }
        lines.append("\t".join(row[column] for column in ANALYSES_COLUMNS) + "\n")
    (store / "analyses.tsv").write_text("".join(lines), encoding="utf-8")


def write_build_record(root: Path, *, store_id: str = STORE_ID) -> None:
    records = root / store_id / "records"
    records.mkdir(parents=True, exist_ok=True)
    (records / "build.json").write_text(
        json.dumps(
            {"step": "build", "opengwasdb_rev": "fixture0rev", "argv": ["opengwasdb", "build"]},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def write_reference(path: Path, alids: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for alid in alids:
            handle.write(f"{alid}\n")


def write_ragged(store: Path, pairs: list[tuple[int, int]], n_variants: int, n_analyses: int) -> None:
    """Write both CSR planes of the Ragged Overflow from one association list.

    Analysis-major (`ragged/`) is sorted by `(analysis, variant)` and
    variant-major (`ragged/by_variant/`) by `(variant, analysis)`, which is the
    relation a real build writes: one row per (Analysis, variant).
    """
    by_variant = sorted(pairs, key=lambda pair: (pair[0], pair[1]))
    by_analysis = sorted(pairs, key=lambda pair: (pair[1], pair[0]))
    total = len(pairs)
    variant_index = np.asarray([pair[0] for pair in by_analysis], dtype="int32")
    analysis_index = np.asarray([pair[1] for pair in by_variant], dtype="int32")
    analysis_counts = np.bincount([pair[1] for pair in by_analysis], minlength=n_analyses)
    variant_counts = np.bincount([pair[0] for pair in by_variant], minlength=n_variants)
    analysis_offsets = np.concatenate([[0], np.cumsum(analysis_counts)]).astype("int64")
    variant_offsets = np.concatenate([[0], np.cumsum(variant_counts)]).astype("int64")

    group = zarr.open_group(str(store / "data.zarr"), mode="w", zarr_format=3)
    ragged = group.require_group("ragged")
    ragged.create_array("offsets", shape=(n_analyses + 1,), dtype="int64", chunks=(n_analyses + 1,))
    ragged.create_array("variant_index", shape=(total,), dtype="int32", chunks=(max(1, min(1000, total)),))
    ragged["offsets"][:] = analysis_offsets
    ragged["variant_index"][:] = variant_index
    by_variant_group = ragged.require_group("by_variant")
    by_variant_group.create_array(
        "offsets", shape=(n_variants + 1,), dtype="int64", chunks=(n_variants + 1,)
    )
    by_variant_group.create_array(
        "analysis_index", shape=(total,), dtype="int32", chunks=(max(1, min(1000, total)),)
    )
    by_variant_group.create_array("z", shape=(total,), dtype="int16", chunks=(max(1, min(1000, total)),))
    by_variant_group.create_array("se", shape=(total,), dtype="float16", chunks=(max(1, min(1000, total)),))
    by_variant_group["offsets"][:] = variant_offsets
    by_variant_group["analysis_index"][:] = analysis_index
    by_variant_group["z"][:] = np.zeros((total,), dtype="int16")
    by_variant_group["se"][:] = np.zeros((total,), dtype="float16")


def write_dense_component(
    store: Path,
    variants: list[CanonicalVariant],
    dense_rows: list[int],
    n_analyses: int,
    *,
    observed: dict[tuple[int, int], int] | None = None,
    store_id: str = STORE_ID,
) -> Path:
    """Write the Dense Component, including a `z` plane when asked for.

    The Dense Component and the Ragged Overflow partition the axis, so an
    on-panel variant's Analyses are `dense/z`'s cells: one column per Analysis,
    `Z_MISSING` where that Analysis holds nothing there.
    """
    dense = store / "dense"
    dense.mkdir()
    write_variant_axis(dense, [variants[row] for row in dense_rows], {})
    np.save(dense / "dense_to_shared.npy", np.asarray(dense_rows, dtype="int32"))
    if observed is None:
        return dense
    plane = np.full((len(dense_rows), n_analyses), Z_MISSING, dtype="int16")
    for (row, analysis), value in observed.items():
        plane[row, analysis] = value
    group = zarr.open_group(str(dense / "data.zarr"), mode="w", zarr_format=3)
    group.create_array(
        "z",
        shape=plane.shape,
        dtype="int16",
        chunks=(max(1, len(dense_rows)), max(1, n_analyses)),
    )
    group["z"][:] = plane
    manifest = StoreManifest.from_dict(
        {
            "store_id": f"{store_id}-dense",
            "release_id": store_id,
            "format_version": "0.2.0",
            "primary_layout": "dense",
            "association_coverage": "full",
            "completion_state": "observed_only",
            "reference_assembly": "GRCh38",
            "created_at": "2026-01-01T00:00:00+00:00",
            "encoding": {
                "version": 3,
                "z": {"kind": "int16_fixed", "scale": 1024},
                "se": {"kind": "float16"},
                "eaf": {"kind": "absent"},
            },
            "provenance": {"builder": "fixture.hybrid.dense"},
        }
    )
    (dense / "manifest.json").write_text(
        json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return dense


def write_manifest(store: Path, provenance: dict, *, store_id: str = STORE_ID) -> None:
    manifest = StoreManifest.from_dict(
        {
            "store_id": store_id,
            "release_id": store_id,
            "format_version": "0.2.0",
            "primary_layout": "hybrid",
            "association_coverage": "full",
            "completion_state": "observed_only",
            "reference_assembly": "GRCh38",
            "created_at": "2026-01-01T00:00:00+00:00",
            "encoding": {
                "version": 3,
                "z": {"kind": "int16_fixed", "scale": 1024},
                "se": {"kind": "float16"},
                "eaf": {"kind": "absent"},
            },
            "provenance": provenance,
        }
    )
    (store / "manifest.json").write_text(
        json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def build_store(
    root: Path,
    *,
    variants: list[CanonicalVariant] | None = None,
    pairs: list[tuple[int, int]] | None = None,
    publications: list[tuple[str, str]] | None = None,
    dense_rows: list[int] | None = None,
    dense_observed: dict[tuple[int, int], int] | None = None,
    analysis_row_order: list[int] | None = None,
    store_id: str = STORE_ID,
) -> tuple[Path, Path]:
    """Build one Hybrid Store Release plus the construction reference it records."""
    variants = axis_variants() if variants is None else variants
    pairs = list(PAIRS) if pairs is None else list(pairs)
    publications = PUBLICATIONS if publications is None else publications
    dense_rows = DENSE_ROWS if dense_rows is None else dense_rows
    if dense_observed is None:
        # Every Analysis holds every panel variant unless the test says
        # otherwise, so an on-panel control has cells to name.
        dense_observed = {
            (row, analysis): 100 + analysis
            for row in range(len(dense_rows))
            for analysis in range(len(publications))
        }
    store = root / store_id / "store.opengwasdb"
    store.mkdir(parents=True)
    write_variant_axis(store, variants, {})
    reference = root / f"{store_id}-reference-EUR-variants.txt.gz"
    write_reference(reference, [variants[row].alid for row in dense_rows])
    write_dense_component(
        store, variants, dense_rows, len(publications), observed=dense_observed, store_id=store_id
    )
    write_ragged(store, pairs, len(variants), len(publications))
    write_analyses(store, publications, store_id=store_id, row_order=analysis_row_order)
    write_build_record(root, store_id=store_id)
    write_manifest(
        store,
        hybrid_provenance(
            reference,
            n_variants=len(variants),
            n_analyses=len(publications),
            n_panel=len(dense_rows),
            n_rows=len(pairs),
        ),
        store_id=store_id,
    )
    return store, reference


def tree_digests(root: Path) -> dict[str, tuple[int, int, str]]:
    import hashlib

    out: dict[str, tuple[int, int, str]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        stat = path.stat()
        out[str(path.relative_to(root))] = (
            stat.st_size,
            stat.st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    return out


class AttributeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ogstores_audit_attribute_"))
        self.evidence = self.tmp / "evidence"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_audit(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = audit.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def attribute(
        self,
        store: Path,
        *extra: str,
        output: str = "attribution.json",
        sample: str | None = "sample.tsv",
    ) -> tuple[int, dict, list[dict[str, str]]]:
        target = self.evidence / output
        argv = ["attribute", str(store), "--output", str(target), *extra]
        if sample is not None:
            argv += ["--sample-output", str(self.evidence / sample)]
        code, out, err = self.run_audit(*argv)
        report = json.loads(target.read_text(encoding="utf-8")) if target.is_file() else {}
        self.assertTrue(report or code != 0, out + err)
        rows: list[dict[str, str]] = []
        sample_path = self.evidence / sample if sample is not None else None
        if sample_path is not None and sample_path.is_file():
            lines = sample_path.read_text(encoding="utf-8").splitlines()
            header = lines[0].split("\t")
            rows = [dict(zip(header, line.split("\t"))) for line in lines[1:]]
        return code, report, rows

    def store(self, **kwargs: object) -> tuple[Path, Path]:
        return build_store(self.tmp, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Seam 1: the report, and the attribution arithmetic it carries
# ---------------------------------------------------------------------------


class TestAttributionReport(AttributeTestCase):
    """`ogstores.audit attribute <store> --output <json>` writes the report."""

    def test_attribute_writes_the_documented_report(self) -> None:
        store, _ = self.store()
        code, report, _ = self.attribute(store)
        record_check()

        self.assertEqual(code, 0, json.dumps(report.get("inconsistencies"), indent=1))
        self.assertEqual(report["schema"], audit.ATTRIBUTION_REPORT_SCHEMA)
        self.assertEqual(report["audit_version"], audit.AUDIT_VERSION)
        for key in (
            "schema",
            "argv",
            "started_at",
            "finished_at",
            "wall_seconds",
            "runtime",
            "store",
            "axis",
            "census",
            "support",
            "allele_classes",
            "chromosomes",
            "contributors",
            "concentration",
            "sample",
            "inconsistencies",
            "ok",
            "unchanged",
            "resources",
            "scope",
        ):
            self.assertIn(key, report, f"{key} missing from the report")
        record_check()
        self.assertEqual(report["store"]["opened_mode"], "r")
        self.assertEqual(report["axis"]["n_axis"], 8)
        self.assertEqual(report["axis"]["n_dense"], 2)
        self.assertEqual(report["axis"]["n_off_panel"], 6)
        self.assertEqual(report["contributors"]["n_analyses"], 4)
        self.assertEqual(report["contributors"]["n_publications"], 3)
        record_check()

    def test_the_census_reconciles_with_the_attributed_union(self) -> None:
        store, _ = self.store()
        code, report, _ = self.attribute(store)
        census = report["census"]
        record_check()

        self.assertEqual(code, 0)
        self.assertEqual(census["off_panel_variants_recorded"], 6)
        self.assertEqual(census["attributed_union_variants"], 6)
        self.assertEqual(census["unobserved_off_panel_variants"], 0)
        self.assertTrue(census["agrees"])
        self.assertEqual(census["associations"], 10)
        self.assertEqual(census["association_rows_read"], 10)
        record_check()

    def test_per_analysis_attribution_is_exact(self) -> None:
        store, _ = self.store()
        _, report, _ = self.attribute(store)
        rankings = report["contributors"]["analyses"]
        record_check()

        by_index = {row["analysis_index"]: row for row in rankings["by_exclusive_variants"]}
        self.assertEqual(by_index[0]["exclusive_variants"], 1)
        self.assertEqual(by_index[1]["exclusive_variants"], 0)
        self.assertEqual(by_index[2]["exclusive_variants"], 1)
        self.assertEqual(by_index[3]["exclusive_variants"], 1)
        record_check()

        associations = {
            row["analysis_index"]: row["off_panel_associations"]
            for row in rankings["by_off_panel_associations"]
        }
        self.assertEqual(associations, {0: 3, 1: 1, 2: 3, 3: 3})
        distinct = {
            row["analysis_index"]: row["distinct_off_panel_variants"]
            for row in rankings["by_distinct_off_panel_variants"]
        }
        self.assertEqual(distinct, {0: 3, 1: 1, 2: 3, 3: 3})
        record_check()

        # The leading Analysis by exclusive variants is A0; ties are broken by
        # the Analysis's own index, so the order is A0, A2, A3 and then A1.
        self.assertEqual(
            [row["analysis_index"] for row in rankings["by_exclusive_variants"]],
            [0, 2, 3, 1],
        )
        record_check()

    def test_per_publication_attribution_is_exact(self) -> None:
        store, _ = self.store()
        _, report, _ = self.attribute(store)
        rankings = report["contributors"]["publications"]
        record_check()

        by_pmid = {row["publication_pmid"]: row for row in rankings["by_exclusive_variants"]}
        # 1001 publishes v2 and v3 -- v3 is exclusive to it although two of its
        # Analyses observe it -- while 1002 publishes v4 and 1003 publishes v6.
        self.assertEqual(by_pmid["1001"]["exclusive_variants"], 2)
        self.assertEqual(by_pmid["1001"]["exclusive_variants_observed_by_several_analyses"], 1)
        self.assertEqual(by_pmid["1002"]["exclusive_variants"], 1)
        self.assertEqual(by_pmid["1003"]["exclusive_variants"], 1)
        record_check()

        by_associations = {
            row["publication_pmid"]: row["off_panel_associations"]
            for row in rankings["by_off_panel_associations"]
        }
        self.assertEqual(by_associations, {"1001": 4, "1002": 3, "1003": 3})
        by_distinct = {
            row["publication_pmid"]: row["distinct_off_panel_variants"]
            for row in rankings["by_distinct_off_panel_variants"]
        }
        self.assertEqual(by_distinct, {"1001": 3, "1002": 3, "1003": 3})
        self.assertEqual(by_pmid["1001"]["n_analyses"], 2)
        record_check()

    def test_exclusivity_arithmetic_holds(self) -> None:
        store, _ = self.store()
        _, report, _ = self.attribute(store)
        support = report["support"]
        census = report["census"]
        record_check()

        analysis_exclusive = support["exclusive_to_one_analysis_variants"]
        publication_exclusive = support["exclusive_to_one_publication_variants"]
        union = census["attributed_union_variants"]
        self.assertEqual(analysis_exclusive, 3)
        self.assertEqual(publication_exclusive, 4)
        self.assertEqual(support["exclusive_to_one_publication_multi_analysis_variants"], 1)
        self.assertEqual(support["shared_across_publications_variants"], 2)
        record_check()

        # The seam the ticket names: exclusive <= union <= associations, and the
        # per-contributor sums are the same quantities counted the other way.
        self.assertLessEqual(analysis_exclusive, union)
        self.assertLessEqual(publication_exclusive, union)
        self.assertLessEqual(union, census["associations"])
        self.assertEqual(support["sum_of_analysis_exclusive_variants"], analysis_exclusive)
        self.assertEqual(support["sum_of_publication_exclusive_variants"], publication_exclusive)
        record_check()

    def test_support_distribution_is_exact(self) -> None:
        store, _ = self.store()
        _, report, _ = self.attribute(store)
        support = report["support"]
        record_check()

        # v2, v4, v6 are observed by one Analysis; v3 and v5 by two; v7 by three.
        self.assertEqual(
            support["analyses"],
            {"1": 3, "2": 2, "3-10": 1, "11-100": 0, "101+": 0},
        )
        # Across publications: 1001 observes v2, v3, v7 (three Analyses, two of
        # the seven variants are its alone); by publication, v2/v3/v4/v6 are
        # single-publication, v5 is two, v7 is three.
        self.assertEqual(
            support["publications"],
            {"1": 4, "2": 1, "3-10": 1, "11-100": 0, "101+": 0},
        )
        record_check()

    def test_allele_classes_and_chromosomes_are_exact(self) -> None:
        store, _ = self.store()
        _, report, _ = self.attribute(store)
        record_check()

        classes = report["allele_classes"]
        # Off-panel is rows 2..7: four SNPs (2, 3, 4, 5), one indel (6) and one
        # complex substitution (7).
        self.assertEqual(classes["snp"], 4)
        self.assertEqual(classes["indel"], 1)
        self.assertEqual(classes["complex"], 1)
        self.assertEqual(classes["multiallelic_positions"], 0)
        record_check()

        chromosomes = report["chromosomes"]
        self.assertEqual(chromosomes["1"], {"snp": 4, "indel": 1, "complex": 0})
        self.assertEqual(chromosomes["2"], {"snp": 0, "indel": 0, "complex": 1})
        record_check()

    def test_the_concentration_of_the_union_is_measured_not_summed(self) -> None:
        store, _ = self.store()
        _, report, _ = self.attribute(store)
        concentration = report["concentration"]
        record_check()

        # Ranked by distinct variants, ties broken by index: A0 (3), A2 (3),
        # A3 (3), then A1 (1). A0 and A2 share v7, so the union of the top two
        # is 5 and not the 6 their counts add up to.
        union = concentration["top_analyses"]["union_variants_by_prefix"]
        self.assertEqual(union["1"], 3)
        self.assertEqual(union["2"], 5)
        self.assertEqual(union["4"], 6)
        shares = concentration["top_analyses"]["share_of_union_by_prefix"]
        self.assertEqual(shares["1"], 0.5)
        self.assertEqual(shares["2"], round(5 / 6, 6))
        self.assertEqual(shares["4"], 1.0)
        record_check()

        # Publications: 1001 holds v2, v3 and v7 alone; 1002 adds v4 and v5.
        publications = concentration["top_publications"]["union_variants_by_prefix"]
        self.assertEqual(publications["1"], 3)
        self.assertEqual(publications["2"], 5)
        self.assertEqual(publications["3"], 6)
        record_check()

    def test_the_band_size_does_not_change_the_answer(self) -> None:
        store, _ = self.store()
        _, whole, _ = self.attribute(
            store, "--band-variants", "1000", "--band-rows", "1000", output="whole.json", sample="whole.tsv"
        )
        _, bands, _ = self.attribute(
            store, "--band-variants", "1", "--band-rows", "1", output="bands.json", sample="bands.tsv"
        )
        record_check()

        self.assertEqual(whole["census"], bands["census"])
        self.assertEqual(whole["support"], bands["support"])
        self.assertEqual(whole["contributors"], bands["contributors"])
        self.assertEqual(whole["chromosomes"], bands["chromosomes"])
        record_check()

        # One entry per band means a candidate selected in the third band is
        # chosen at axis index 2, not at position 0 of its band: the sample is a
        # property of the Store and the seed, and a band is an implementation
        # detail of reading it.
        self.assertEqual(
            (self.evidence / "whole.tsv").read_bytes(), (self.evidence / "bands.tsv").read_bytes()
        )
        record_check()


# ---------------------------------------------------------------------------
# Seam 2: counting is exact when the metadata is not convenient
# ---------------------------------------------------------------------------


class TestDeduplication(AttributeTestCase):
    """Nothing is counted twice because of how the Store happens to be laid out."""

    def test_interleaved_publications_are_counted_once(self) -> None:
        # The two Analyses of 1001 (0 and 2) straddle one of 1002 (1), which is
        # what a publication-ordered traversal exists for: visiting Analyses in
        # index order lets 1002 stamp v2 between 1001's two Analyses, and 1001's
        # second Analysis then counts v2 and v3 a second time.
        publications = [("1001", "A"), ("1002", "B"), ("1001", "A"), ("1002", "B")]
        pairs = [(2, 0), (2, 1), (2, 2), (3, 0), (3, 2), (4, 1)]
        store, _ = self.store(publications=publications, pairs=pairs)
        code, report, _ = self.attribute(store)
        record_check()

        self.assertEqual(code, 0, json.dumps(report.get("inconsistencies"), indent=1))
        support = report["support"]
        # v2 is observed by three Analyses of two publications; v3 by two
        # Analyses of one; v4 by one.
        self.assertEqual(support["analyses"], {"1": 1, "2": 1, "3-10": 1, "11-100": 0, "101+": 0})
        self.assertEqual(support["publications"], {"1": 2, "2": 1, "3-10": 0, "11-100": 0, "101+": 0})
        record_check()

        publications_table = {
            row["publication_pmid"]: row
            for row in report["contributors"]["publications"]["by_distinct_off_panel_variants"]
        }
        self.assertEqual(publications_table["1001"]["distinct_off_panel_variants"], 2)
        self.assertEqual(publications_table["1002"]["distinct_off_panel_variants"], 2)
        self.assertEqual(publications_table["1001"]["off_panel_associations"], 4)
        self.assertEqual(publications_table["1002"]["off_panel_associations"], 2)
        record_check()

        # 1001 is v3's only publication although two Analyses observe it; 1002
        # is v4's only publication and only 1002's Analysis 1 observes it.
        self.assertEqual(support["exclusive_to_one_publication_variants"], 2)
        self.assertEqual(support["exclusive_to_one_publication_multi_analysis_variants"], 1)
        self.assertEqual(support["shared_across_publications_variants"], 1)
        self.assertEqual(support["sum_of_publication_exclusive_variants"], 2)
        record_check()

    def test_a_variant_repeated_by_one_analysis_is_counted_once(self) -> None:
        # Two rows for one variant in one Analysis: an intra-band repeat, which a
        # stamp alone cannot catch because both rows read as new.
        pairs = [(2, 0), (2, 0), (3, 0), (4, 2), (5, 3)]
        store, _ = self.store(pairs=pairs)
        code, report, _ = self.attribute(store)
        record_check()

        self.assertEqual(code, 0, json.dumps(report.get("inconsistencies"), indent=1))
        self.assertEqual(report["support"]["analyses"], {"1": 4, "2": 0, "3-10": 0, "11-100": 0, "101+": 0})
        record_check()

        kinds = [entry["kind"] for entry in report["inconsistencies"]]
        self.assertIn("repeated_variant_rows_in_one_analysis", kinds)
        repeated = next(
            entry for entry in report["inconsistencies"] if entry["kind"] == "repeated_variant_rows_in_one_analysis"
        )
        self.assertEqual(repeated["counts"]["rows"], 1)
        self.assertEqual(repeated["severity"], "finding")
        record_check()

        # The exclusive counts stay apart: two variants, three associations.
        analysis_zero = next(
            row
            for row in report["contributors"]["analyses"]["by_exclusive_variants"]
            if row["analysis_index"] == 0
        )
        self.assertEqual(analysis_zero["exclusive_variants"], 2)
        self.assertEqual(analysis_zero["exclusive_associations"], 3)
        self.assertEqual(analysis_zero["distinct_off_panel_variants"], 2)
        self.assertEqual(analysis_zero["off_panel_associations"], 3)
        self.assertIn(
            "single_observer_variants_carry_several_associations", kinds
        )
        record_check()

    def test_metadata_is_read_by_analysis_index_not_by_file_order(self) -> None:
        # The rows are written 2, 0, 3, 1: file order is not `analysis_index`.
        store, _ = self.store(analysis_row_order=[2, 0, 3, 1])
        code, report, rows = self.attribute(store)
        record_check()

        self.assertEqual(code, 0, json.dumps(report.get("inconsistencies"), indent=1))
        by_index = {
            row["analysis_index"]: row["publication_pmid"]
            for row in report["contributors"]["analyses"]["by_distinct_off_panel_variants"]
        }
        self.assertEqual(by_index, {0: "1001", 1: "1001", 2: "1002", 3: "1003"})
        record_check()

        table = (self.evidence / "analyses.tsv").read_text(encoding="utf-8").splitlines()
        header = table[0].split("\t")
        records = [dict(zip(header, line.split("\t"))) for line in table[1:]]
        self.assertEqual([row["analysis_index"] for row in records], ["0", "1", "2", "3"])
        self.assertEqual([row["publication_pmid"] for row in records], ["1001", "1001", "1002", "1003"])
        record_check()

        # And the sample agrees: an off-panel row naming Analysis 2 names 1002.
        named = [row for row in rows if row["analysis_index"] == "2" and row["on_panel"] == "False"]
        self.assertTrue(named)
        self.assertTrue(all(row["publication_pmid"] == "1002" for row in named))
        record_check()

    def test_the_full_contributor_tables_are_written_beside_the_evidence(self) -> None:
        store, _ = self.store()
        _, report, _ = self.attribute(store)
        tables = report["contributors"]["tables"]
        record_check()

        self.assertEqual(set(tables), {"analyses", "publications"})
        analyses_table = self.evidence / "analyses.tsv"
        publications_table = self.evidence / "publications.tsv"
        self.assertTrue(analyses_table.is_file())
        self.assertTrue(publications_table.is_file())
        self.assertEqual(tables["analyses"]["rows"], 4)
        self.assertEqual(tables["publications"]["rows"], 3)
        record_check()

        import hashlib

        self.assertEqual(
            tables["analyses"]["sha256"], hashlib.sha256(analyses_table.read_bytes()).hexdigest()
        )
        header = analyses_table.read_text(encoding="utf-8").splitlines()[0].split("\t")
        self.assertEqual(tuple(header), audit.ANALYSIS_TABLE_COLUMNS)
        header = publications_table.read_text(encoding="utf-8").splitlines()[0].split("\t")
        self.assertEqual(tuple(header), audit.PUBLICATION_TABLE_COLUMNS)
        record_check()

        rows = [
            dict(zip(audit.ANALYSIS_TABLE_COLUMNS, line.split("\t")))
            for line in analyses_table.read_text(encoding="utf-8").splitlines()[1:]
        ]
        self.assertEqual([row["analysis_index"] for row in rows], ["0", "1", "2", "3"])
        # Every Analysis is in the full table, not only the ranked leaders.
        self.assertTrue(all(row["analysis_id"] for row in rows))
        record_check()


# ---------------------------------------------------------------------------
# Seam 2: the sample is deterministic, stratified and traceable
# ---------------------------------------------------------------------------


class TestDeterministicSample(AttributeTestCase):
    """A seed alone decides the sample, and every row names a reachable row."""

    def test_the_same_seed_writes_a_byte_identical_sample(self) -> None:
        store, _ = self.store()
        self.attribute(store, "--seed", "42", output="a.json", sample="a.tsv")
        self.attribute(store, "--seed", "42", output="b.json", sample="b.tsv")
        record_check()

        first = (self.evidence / "a.tsv").read_bytes()
        second = (self.evidence / "b.tsv").read_bytes()
        self.assertTrue(first)
        self.assertEqual(first, second)
        record_check()

    def test_a_different_seed_chooses_different_rows(self) -> None:
        store, _ = self.store()
        _, _, one = self.attribute(
            store, "--seed", "42", "--sample-per-stratum", "1", output="a.json", sample="a.tsv"
        )
        _, _, other = self.attribute(
            store, "--seed", "7", "--sample-per-stratum", "1", output="b.json", sample="b.tsv"
        )
        record_check()

        picked_one = sorted((row["stratum"], row["variant_index"]) for row in one)
        picked_other = sorted((row["stratum"], row["variant_index"]) for row in other)
        self.assertNotEqual(picked_one, picked_other)
        record_check()

    def test_the_sample_carries_the_documented_columns(self) -> None:
        store, _ = self.store()
        _, report, rows = self.attribute(store)
        header = (self.evidence / "sample.tsv").read_text(encoding="utf-8").splitlines()[0].split("\t")
        record_check()

        self.assertEqual(tuple(header), audit.SAMPLE_COLUMNS)
        for required in (
            "sample_id",
            "analysis_id",
            "analysis_index",
            "alid",
            "rsid",
            "stratum",
            "on_panel",
            "publication_pmid",
            "source_row_lookup_hint",
        ):
            self.assertIn(required, header)
        self.assertTrue(rows)
        record_check()

        self.assertEqual(report["sample"]["rows"], len(rows))
        self.assertEqual(report["sample"]["seed"], 42)
        self.assertTrue(report["sample"]["sha256"])
        self.assertEqual(
            report["sample"]["sha256"],
            __import__("hashlib").sha256((self.evidence / "sample.tsv").read_bytes()).hexdigest(),
        )
        record_check()

    def test_every_documented_stratum_is_present_and_labelled(self) -> None:
        store, _ = self.store()
        _, _, rows = self.attribute(store)
        record_check()

        strata = {row["stratum"] for row in rows}
        self.assertIn("random_off_panel", strata)
        self.assertIn("shared_multi_publication", strata)
        self.assertIn("on_panel_control", strata)
        self.assertTrue(any(name.startswith("exclusive_analysis:") for name in strata))
        self.assertTrue(any(name.startswith("exclusive_publication:") for name in strata))
        record_check()

        off_panel = [row for row in rows if row["stratum"] != "on_panel_control"]
        self.assertTrue(all(row["on_panel"] == "False" for row in off_panel))
        self.assertTrue(all(row["analysis_index"] != "" for row in off_panel))
        self.assertTrue(all(row["publication_pmid"] in {"1001", "1002", "1003"} for row in off_panel))
        record_check()

        # Allele classes reach the sample rather than the commonest class
        # filling every stratum.
        self.assertIn("indel", {row["allele_class"] for row in rows})
        record_check()

    def test_every_hint_names_the_row_it_says_it_does(self) -> None:
        store, _ = self.store()
        _, _, rows = self.attribute(store)
        record_check()

        import zarr

        group = zarr.open_group(str(store / "data.zarr"), mode="r")
        variant_index = np.asarray(group["ragged/variant_index"][:])
        offsets = np.asarray(group["ragged/offsets"][:])
        checked = 0
        for row in rows:
            if row["on_panel"] == "True":
                # With no Dense `z` plane in this fixture the hint says the plane
                # is absent rather than inventing a cell; a Store that has one
                # names the cell.
                self.assertIn("dense_", row["source_row_lookup_hint"])
                continue
            hint = dict(part.split("=", 1) for part in row["source_row_lookup_hint"].split(";"))
            self.assertEqual(int(hint["variant_index"]), int(row["variant_index"]))
            position = int(hint["analysis_major_row"])
            self.assertEqual(int(variant_index[position]), int(row["variant_index"]))
            analysis = int(row["analysis_index"])
            self.assertGreaterEqual(position, int(offsets[analysis]))
            self.assertLess(position, int(offsets[analysis + 1]))
            checked += 1
        self.assertGreater(checked, 0)
        record_check()

    def test_on_panel_controls_name_the_analyses_of_the_same_store(self) -> None:
        store, _ = self.store(dense_observed={(0, 0): 100, (0, 2): 200, (1, 1): 300})
        _, report, rows = self.attribute(store)
        controls = [row for row in rows if row["stratum"] == "on_panel_control"]
        record_check()

        self.assertTrue(report["sample"]["on_panel_controls"]["available"])
        self.assertEqual(report["sample"]["on_panel_controls"]["panel_variants_skipped"], 0)
        self.assertTrue(controls)
        self.assertTrue(all(row["on_panel"] == "True" for row in controls))
        named = {(int(row["variant_index"]), row["analysis_index"]) for row in controls if row["analysis_index"]}
        self.assertIn((0, "0"), named)
        self.assertIn((0, "2"), named)
        self.assertIn((1, "1"), named)
        record_check()

        for row in controls:
            if not row["analysis_index"]:
                continue
            self.assertIn("dense_row=", row["source_row_lookup_hint"])
            self.assertIn("dense_plane=z", row["source_row_lookup_hint"])
            self.assertIn(f"analysis_index={row['analysis_index']}", row["source_row_lookup_hint"])
        record_check()

    def test_every_row_names_an_analysis_and_its_id(self) -> None:
        store, _ = self.store()
        _, report, rows = self.attribute(store)
        record_check()

        self.assertTrue(rows)
        for row in rows:
            self.assertTrue(row["analysis_id"], row["sample_id"])
            self.assertTrue(row["analysis_index"], row["sample_id"])
            self.assertIn(row["analysis_index"], {
                str(entry["analysis_index"])
                for entry in report["contributors"]["analyses"]["by_distinct_off_panel_variants"]
            } | {str(index) for index in range(4)})
        record_check()

        # on_panel=True and False alike: an on-panel control names a Dense cell.
        self.assertEqual(
            {row["on_panel"] for row in rows}, {"True", "False"}
        )
        self.assertTrue(all(row["analysis_id"] for row in rows if row["on_panel"] == "True"))
        record_check()

    def test_the_exclusive_strata_follow_the_exclusive_ranking(self) -> None:
        # A1 holds the most variants but shares them; A2 holds fewer and nobody
        # else observes them. The sample asks for exclusive variants, so A2 is
        # the stratum `--top 1` selects, while `concentration` (who holds the
        # union) still leads with A1.
        publications = [("1001", "A"), ("1002", "B"), ("1003", "C")]
        pairs = [(2, 0), (2, 1), (3, 0), (3, 1), (4, 0), (4, 1), (5, 1), (6, 2), (7, 2)]
        store, _ = self.store(publications=publications, pairs=pairs)
        _, report, rows = self.attribute(store, "--top", "1")
        record_check()

        self.assertEqual(report["sample"]["exclusive_analyses"], [2])
        strata = {row["stratum"] for row in rows}
        self.assertIn("exclusive_analysis:2", strata)
        self.assertNotIn("exclusive_analysis:1", strata)
        self.assertNotIn("exclusive_analysis:0", strata)
        record_check()

        self.assertEqual(report["concentration"]["top_analyses"]["contributors"], [[1]])
        distinct = [
            row["analysis_index"]
            for row in report["contributors"]["analyses"]["by_distinct_off_panel_variants"]
        ]
        exclusive = [
            row["analysis_index"]
            for row in report["contributors"]["analyses"]["by_exclusive_variants"]
        ]
        self.assertEqual(distinct[0], 1)
        self.assertEqual(exclusive[0], 2)
        record_check()

    def test_no_sample_output_writes_no_sample(self) -> None:
        store, _ = self.store()
        code, report, rows = self.attribute(store, sample=None)
        record_check()

        self.assertEqual(code, 0, json.dumps(report.get("inconsistencies"), indent=1))
        self.assertFalse(report["sample"]["written"])
        self.assertEqual(rows, [])
        self.assertEqual(report["scope"]["skipped"], ["seeded sample for raw-row tracing"])
        self.assertFalse((self.evidence / "sample.tsv").exists())
        record_check()


# ---------------------------------------------------------------------------
# Seam 3: what cannot be attributed is reported, not guessed
# ---------------------------------------------------------------------------


class TestFailLoudly(AttributeTestCase):
    """Missing or ambiguous evidence is an error, never a zero."""

    def test_ambiguous_publication_metadata_is_an_error(self) -> None:
        publications = [("1001", "Barnes DR"), ("1001", "Someone Else"), ("1002", "Chen J"), ("1003", "Williams AT")]
        store, _ = self.store(publications=publications)
        code, report, _ = self.attribute(store)
        record_check()

        self.assertEqual(code, 1)
        self.assertFalse(report["ok"])
        kinds = [entry["kind"] for entry in report["inconsistencies"]]
        self.assertIn("publication_metadata_ambiguous", kinds)
        entry = next(e for e in report["inconsistencies"] if e["kind"] == "publication_metadata_ambiguous")
        self.assertEqual(entry["severity"], "error")
        self.assertEqual(entry["counts"]["publication_key"], "pmid:1001")
        record_check()

    def test_an_analysis_with_no_publication_is_retained_as_an_unknown_group(self) -> None:
        publications = [("1001", "Barnes DR"), ("", "Barnes DR"), ("1002", "Chen J"), ("1003", "Williams AT")]
        store, _ = self.store(publications=publications)
        code, report, rows = self.attribute(store)
        record_check()

        self.assertEqual(code, 0, json.dumps(report.get("inconsistencies"), indent=1))
        self.assertTrue(report["ok"])
        finding = next(
            entry for entry in report["inconsistencies"] if entry["kind"] == "publication_metadata_unknown"
        )
        self.assertEqual(finding["severity"], "finding")
        self.assertEqual(finding["counts"]["analysis_indices"], [1])
        record_check()

        # Its own group, never merged into another publication.
        self.assertEqual(report["contributors"]["n_publications"], 4)
        self.assertEqual(report["contributors"]["n_unknown_publications"], 1)
        unknown = [
            row
            for row in report["contributors"]["publications"]["by_distinct_off_panel_variants"]
            if row["publication_key_kind"] == "unknown"
        ]
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0]["publication_pmid"], None)
        self.assertEqual(unknown[0]["n_analyses"], 1)
        record_check()

        # And its sample rows still name an Analysis.
        self.assertTrue(rows)
        self.assertTrue(all(row["analysis_id"] for row in rows))
        record_check()

    def test_missing_analyses_tsv_cannot_be_attributed(self) -> None:
        store, _ = self.store()
        (store / "analyses.tsv").unlink()
        code, _, err = self.run_audit(
            "attribute", str(store), "--output", str(self.evidence / "attribution.json")
        )
        record_check()

        self.assertEqual(code, 2)
        self.assertIn("analyses.tsv", err)
        record_check()

    def test_refuses_to_write_evidence_or_sample_inside_the_store(self) -> None:
        store, _ = self.store()
        code, _, err = self.run_audit(
            "attribute", str(store), "--output", str(store / "attribution.json")
        )
        record_check()
        self.assertEqual(code, 2)
        self.assertIn("inside the Store", err)

        code, _, err = self.run_audit(
            "attribute",
            str(store),
            "--output",
            str(self.evidence / "attribution.json"),
            "--sample-output",
            str(store / "sample.tsv"),
        )
        record_check()
        self.assertEqual(code, 2)
        self.assertIn("inside the Store", err)
        self.assertFalse((store / "sample.tsv").exists())
        record_check()

    def test_a_dense_axis_entry_with_an_overflow_association_is_an_error(self) -> None:
        # v0 is on panel, so the disjoint partition says it carries no Overflow
        # association; a fixture that gives it one is a broken Store.
        pairs = list(PAIRS) + [(0, 0)]
        store, _ = self.store(pairs=pairs)
        code, report, _ = self.attribute(store)
        record_check()

        self.assertEqual(code, 1)
        kinds = [entry["kind"] for entry in report["inconsistencies"]]
        self.assertIn("dense_axis_rows_carry_overflow_associations", kinds)
        record_check()

    def test_the_store_is_unchanged_by_an_attribution(self) -> None:
        store, _ = self.store()
        before = tree_digests(store)
        code, report, _ = self.attribute(store)
        after = tree_digests(store)
        record_check()

        self.assertEqual(code, 0)
        self.assertEqual(before, after)
        self.assertEqual(report["unchanged"]["changed"], [])
        self.assertEqual(report["unchanged"]["added"], [])
        self.assertEqual(report["unchanged"]["removed"], [])
        record_check()


def main() -> None:
    suite = unittest.TestSuite()
    for case in (
        TestAttributionReport,
        TestDeduplication,
        TestDeterministicSample,
        TestFailLoudly,
    ):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(case))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful():
        sys.exit(1)
    print(f"ALL {n_checks} CHECKS PASSED")


if __name__ == "__main__":
    main()
