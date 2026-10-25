#!/usr/bin/env python3
"""The trace command (`ogstores.audit trace`, stores #374).

Part of epic #371. #372 reconciled the off-panel axis, #373 attributed its
101M-variant union to Analyses and publications and pinned a seeded sample,
and this slice follows that sample back to the raw rows the builder read, the
frequency it parsed from each of them, and the admission decision it made.

The fixture is not a hand-written Store: it is a real format-0.2.0 Hybrid Store
Release built through OpenGWASDB's own builder (`build_hybrid_from_vcf_manifest`)
from a ten-row GWAS-SSF file, one Analysis and one panel variant. That is the
point of this suite -- the defect the ticket exists for is a property of the
*builder*, so a reproducer that hand-built the Store could not show it. The
source file's rows state every frequency class at once:

```text
row     effect/other  eaf        source frequency class   admission        in Store
1:1     G/A           0.4        valid_common             valid_common     yes (panel)
1:1000  G/A           0.0        placeholder_zero         maf_missing      yes
1:2000  T/C           0.001      below_threshold          maf_below        no
1:3000  A/G           0.5        valid_common             valid_common     yes
1:4000  C/T           0.0049     below_threshold          maf_below        no
1:5000  G/T           0.005      valid_common             valid_common     yes (boundary)
1:6000  T/A           1.0        placeholder_one          maf_missing      yes
1:7000  A/C           (blank)    missing                  maf_missing      yes
1:8000  C/G           1.5        out_of_range             maf_missing      yes
1:10000 A/T           abc        malformed                maf_missing      yes
```

so the admitted rows are 8 (10 observed, 2 below the 0.005 threshold) and 5 of
them carry no usable frequency. The `1:6000` row is the flipped-orientation
case (its raw effect allele is `T`, the canonical A1 is `A`, so the builder
stores `1 - 1.0` -- still nothing), and the `1:1` row is the on-panel control
whose cell lives in the Dense Component.
"""

from __future__ import annotations

import contextlib
import csv
import gzip
import io
import json
import math
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import zarr

from opengwasdb.build.row_admission import admit_rows
from opengwasdb.encoding.plan import EAF_ABSENT
from opengwasdb.layouts.hybrid.build import build_hybrid_from_vcf_manifest
from opengwasdb.model.info_score_policy import InfoScorePolicy
from opengwasdb.model.maf_policy import parse_maf_policy
from opengwasdb.stats import parse_af

from ogstores import audit

STORE_ID = "OGS-00092"
ANALYSIS_ID = "GCST999001"
PUBLICATION_PMID = "41266372"

n_checks = 0


def record_check() -> None:
    global n_checks
    n_checks += 1


SOURCE_COLUMNS = (
    "chromosome",
    "base_pair_location",
    "effect_allele",
    "other_allele",
    "effect_allele_frequency",
    "beta",
    "standard_error",
    "p_value",
    "rsid",
    "variant_id",
)

#: `(chromosome, position, effect, other, eaf token, rsid)`. The module docstring
#: is this table's arithmetic; keep the two in step.
SOURCE_ROWS = (
    ("1", "1", "G", "A", "0.4", "rs0"),
    ("1", "1000", "G", "A", "0.0", "rs1"),
    ("1", "2000", "T", "C", "0.001", "rs2"),
    ("1", "3000", "A", "G", "0.5", "rs3"),
    ("1", "4000", "C", "T", "0.0049", "rs4"),
    ("1", "5000", "G", "T", "0.005", "rs5"),
    ("1", "6000", "T", "A", "1.0", "rs6"),
    ("1", "7000", "A", "C", "", "rs7"),
    ("1", "8000", "C", "G", "1.5", "rs8"),
    ("1", "10000", "A", "T", "abc", "rs9"),
)

#: The variant reference: one panel variant, with the source key the reader
#: yields for the `1:1` row (ref = other_allele `A`, alt = effect_allele `G`).
PANEL_LINES = (
    "#alid\tchromosome\tposition\ta1\ta2\trsid\tsource_keys",
    "1:1:A:G\t1\t1\tA\tG\trs0\t1:1:A:G",
)

MANIFEST_COLUMNS = (
    "analysis_id",
    "source_file",
    "analysis_label",
    "sample_size",
    "stored_effect_scale",
    "original_sd_method",
    "source_reader_capability",
    "source_assembly",
    "maf_threshold",
    "info_score_threshold",
    "publication_pmid",
)

#: The sample #373 would emit for this Store: every off-panel admitted variant
#: plus the two the MAF filter dropped (which #373 could not have sampled, and
#: which the trace therefore names without a `variant_index`), plus the panel row.
SAMPLE_ROWS = (
    ("S00", "1:1:A:G", "on_panel_control"),
    ("S01", "1:1000:A:G", "exclusive_analysis:0"),
    ("S02", "1:2000:C:T", "exclusive_analysis:0"),
    ("S03", "1:3000:A:G", "exclusive_analysis:0"),
    ("S04", "1:4000:C:T", "exclusive_analysis:0"),
    ("S05", "1:5000:G:T", "exclusive_analysis:0"),
    ("S06", "1:6000:A:T", "exclusive_analysis:0"),
    ("S07", "1:7000:A:C", "exclusive_analysis:0"),
    ("S08", "1:8000:C:G", "exclusive_analysis:0"),
    ("S09", "1:10000:A:T", "exclusive_analysis:0"),
)

#: The dispositions the trace must assign, by sample id. See the module docstring.
EXPECTED_DISPOSITIONS = {
    "S00": "valid_common",
    "S01": "maf_missing",
    "S02": "maf_below_threshold",
    "S03": "valid_common",
    "S04": "maf_below_threshold",
    "S05": "valid_common",
    "S06": "maf_missing",
    "S07": "maf_missing",
    "S08": "maf_missing",
    "S09": "maf_missing",
}

#: The source-frequency class each sample row's raw token carries.
EXPECTED_CLASSES = {
    "S00": "valid_common",
    "S01": "placeholder_zero",
    "S02": "below_threshold",
    "S03": "valid_common",
    "S04": "below_threshold",
    "S05": "valid_common",
    "S06": "placeholder_one",
    "S07": "missing",
    "S08": "out_of_range",
    "S09": "malformed",
}


def write_source(path: Path) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write("\t".join(SOURCE_COLUMNS) + "\n")
        for chromosome, position, effect, other, eaf, rsid in SOURCE_ROWS:
            handle.write(
                "\t".join(
                    (
                        chromosome,
                        position,
                        effect,
                        other,
                        eaf,
                        "0.05",
                        "0.05",
                        "1e-4",
                        rsid,
                        f"{chromosome}:{position}:{effect}:{other}",
                    )
                )
                + "\n"
            )


def write_manifest(path: Path, source: Path, *, maf: str = "0.005", info: str = "NaN") -> None:
    rows = [
        (
            ANALYSIS_ID,
            str(source),
            "Fixture trait",
            "1000",
            "sd",
            "declared_standardised",
            "opengwasdb.gwas-ssf",
            "GRCh38",
            maf,
            info,
            PUBLICATION_PMID,
        )
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write("\t".join(MANIFEST_COLUMNS) + "\n")
        for row in rows:
            handle.write("\t".join(row) + "\n")


def store_variants(store: Path) -> dict[str, int]:
    """Every ALID the Store's variant table names, with its `variant_index`."""
    out: dict[str, int] = {}
    with gzip.open(store / "variants.tsv.gz", "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("#"):
                continue
            fields = line.rstrip("\n").split("\t")
            out[fields[5]] = int(fields[2])
    return out


def ragged_eaf_codes(store: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The Overflow's `(variant_index, analysis_index, eaf code)` per row."""
    group = zarr.open_group(store / "data.zarr", mode="r")["ragged"]["by_variant"]
    offsets = np.asarray(group["offsets"][:], dtype="int64")
    analyses = np.asarray(group["analysis_index"][:])
    codes = np.asarray(group["eaf"][:])
    variants = np.repeat(np.arange(offsets.size - 1), np.diff(offsets))
    return variants, analyses, codes


def dense_eaf_codes(store: Path) -> np.ndarray:
    group = zarr.open_group(store / "dense" / "data.zarr", mode="r")
    return np.asarray(group["eaf"][:])


def is_absent(code: object) -> bool:
    if isinstance(code, (int, np.integer)):
        return int(code) == EAF_ABSENT
    return not math.isfinite(float(code))


def write_sample(path: Path, store: Path, rows=SAMPLE_ROWS) -> None:
    indices = store_variants(store)
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(
            "\t".join(
                (
                    "sample_id",
                    "analysis_id",
                    "analysis_index",
                    "alid",
                    "variant_index",
                    "stratum",
                    "on_panel",
                    "publication_pmid",
                )
            )
            + "\n"
        )
        for sample_id, alid, stratum in rows:
            handle.write(
                "\t".join(
                    (
                        sample_id,
                        ANALYSIS_ID,
                        "0",
                        alid,
                        "" if alid not in indices else str(indices[alid]),
                        stratum,
                        "True" if stratum == "on_panel_control" else "False",
                        PUBLICATION_PMID,
                    )
                )
                + "\n"
            )


class Fixture:
    """One built Store, its source, its build manifest and a written sample."""

    def __init__(self, root: Path):
        self.root = root
        self.source = root / "source.h.tsv.gz"
        self.manifest = root / "analyses.tsv"
        self.panel = root / "panel.tsv"
        self.store = root / "store.opengwasdb"
        self.sample = root / "sample.tsv"
        self.evidence = root / "evidence"
        self.evidence.mkdir()

    def build(self, *, maf: str = "0.005", info: str = "NaN") -> "Fixture":
        write_source(self.source)
        write_manifest(self.manifest, self.source, maf=maf, info=info)
        self.panel.write_text("\n".join(PANEL_LINES) + "\n", encoding="utf-8")
        build_hybrid_from_vcf_manifest(
            self.manifest,
            self.store,
            variant_reference=self.panel,
            store_id=STORE_ID,
            release_id=STORE_ID,
            n_workers=1,
        )
        write_sample(self.sample, self.store)
        return self


def build_fixture(root: Path, **kwargs: object) -> Fixture:
    return Fixture(root).build(**kwargs)  # type: ignore[arg-type]


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


class TraceTestCase(unittest.TestCase):
    fixture_kwargs: dict[str, object] = {}

    @classmethod
    def setUpClass(cls) -> None:
        cls._root = Path(tempfile.mkdtemp(prefix="ogstores_audit_trace_"))
        cls.fixture = build_fixture(cls._root, **cls.fixture_kwargs)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls._root, ignore_errors=True)

    def run_audit(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = audit.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def trace(
        self,
        *extra: str,
        output: str = "trace.json",
        sample: Path | None = None,
        manifest: Path | None = None,
    ) -> tuple[int, dict]:
        target = self.fixture.evidence / output
        code, out, err = self.run_audit(
            "trace",
            str(self.fixture.store),
            "--sample",
            str(sample or self.fixture.sample),
            "--manifest",
            str(manifest or self.fixture.manifest),
            "--output",
            str(target),
            "--workers",
            "1",
            *extra,
        )
        report = json.loads(target.read_text(encoding="utf-8")) if target.is_file() else {}
        self.assertTrue(report or code == 2, out + err)
        return code, report

    def by_id(self, report: dict) -> dict[str, dict]:
        return {row["sample_id"]: row for row in report["rows"]}


# ---------------------------------------------------------------------------
# Seam 1: the minimal reproducer, at the real builder entry point
# ---------------------------------------------------------------------------


class TestBuilderAdmissionDefect(TraceTestCase):
    """The defect the ticket exists for, minimised to the two rows that show it.

    A source row whose `effect_allele_frequency` is exactly `0.0` is admitted,
    and a source row whose frequency is `0.001` is dropped, by the same 0.005
    MAF policy. Nothing about the two rows differs except the value the reader
    parses from the frequency column -- `parse_af` maps `0.0` to *missing*, so
    the row never reaches the MAF comparison at all.
    """

    def test_the_builder_admits_a_zero_frequency_row_it_drops_a_low_one_for(self) -> None:
        indices = store_variants(self.fixture.store)
        record_check()

        # The low frequency is judged by the policy and dropped.
        self.assertNotIn("1:2000:C:T", indices)
        record_check()
        # Exactly zero is not a frequency the reader has an opinion about, so the
        # row is retained as `maf_missing`.
        self.assertIn("1:1000:A:G", indices)
        record_check()

        variants, analyses, codes = ragged_eaf_codes(self.fixture.store)
        admitted = np.flatnonzero(
            (analyses == 0) & (variants == indices["1:1000:A:G"])
        )
        self.assertEqual(admitted.size, 1)
        record_check()
        code = codes[admitted[0]]
        self.assertTrue(
            is_absent(code),
            f"the admitted placeholder row carries stored eaf {code!r}, not the absent code",
        )
        record_check()

    def test_the_row_admission_rule_replays_the_two_cases_directly(self) -> None:
        # The reader's own parse is what turns the zero into "no frequency"; the
        # admission rule then has nothing to judge and keeps the row.
        self.assertIsNone(parse_af("0.0"))
        self.assertEqual(parse_af("0.001"), 0.001)
        record_check()

        maf = parse_maf_policy({"maf_threshold": "0.005"})
        info = InfoScorePolicy()
        frequencies = np.array(
            [np.nan if parse_af(token) is None else parse_af(token) for token in ("0.001", "0.0")],
            dtype="float64",
        )
        admission = admit_rows(
            np.array([np.nan, np.nan], dtype="float64"),
            np.array(["undeclared", "undeclared"], dtype=object),
            frequencies,
            info,
            maf,
        )
        record_check()
        self.assertEqual(admission.keep.tolist(), [False, True])
        self.assertEqual(admission.counts.maf_below_threshold, 1)
        self.assertEqual(admission.counts.maf_missing, 1)
        self.assertEqual(admission.counts.admitted, 1)
        record_check()

    def test_the_maf_boundary_is_exclusive_below_and_inclusive_at_it(self) -> None:
        indices = store_variants(self.fixture.store)
        self.assertIn("1:5000:G:T", indices)  # maf exactly 0.005 passes
        self.assertNotIn("1:4000:C:T", indices)  # 0.0049 does not
        record_check()

    def test_a_placeholder_one_and_an_out_of_range_frequency_are_admitted_too(self) -> None:
        indices = store_variants(self.fixture.store)
        # Exactly 1.0, 1.5 and an unparseable token are all admitted as
        # `maf_missing`: the reader parses each as absent and the rule keeps a
        # row with no usable frequency, never refusing it.
        for alid in ("1:6000:A:T", "1:8000:C:G", "1:10000:A:T"):
            self.assertIn(alid, indices)
        record_check()


# ---------------------------------------------------------------------------
# Seam 2: the trace report itself
# ---------------------------------------------------------------------------


class TestTraceReport(TraceTestCase):
    """`ogstores.audit trace <store> --sample <tsv> --manifest <tsv> --output`."""

    def test_trace_writes_the_documented_report(self) -> None:
        code, report = self.trace()
        record_check()
        self.assertEqual(code, 0, json.dumps(report.get("inconsistencies"), indent=1))
        self.assertEqual(report["schema"], "ogstores.audit.trace/1")
        self.assertEqual(report["audit_version"], audit.AUDIT_VERSION)
        for key in (
            "command",
            "schema",
            "store",
            "sample",
            "build_manifest",
            "scope",
            "rows",
            "summary",
            "inconsistencies",
            "store_unchanged",
            "ok",
        ):
            self.assertIn(key, report, f"{key} missing from the report")
        record_check()
        self.assertEqual(report["command"], "trace")
        self.assertEqual(report["store"]["store_id"], STORE_ID)
        self.assertEqual(report["sample"]["rows"], len(SAMPLE_ROWS))
        self.assertEqual(report["build_manifest"]["analyses"], 1)
        record_check()

    def test_every_sampled_row_carries_one_disposition_and_the_tally_closes(self) -> None:
        _, report = self.trace()
        rows = self.by_id(report)
        record_check()

        self.assertEqual(sorted(rows), sorted(sample for sample, _, _ in SAMPLE_ROWS))
        record_check()
        for sample_id, expected in EXPECTED_DISPOSITIONS.items():
            self.assertEqual(rows[sample_id]["trace_disposition"], expected, sample_id)
        record_check()
        for sample_id, expected in EXPECTED_CLASSES.items():
            self.assertEqual(rows[sample_id]["frequency"]["class"], expected, sample_id)
        record_check()

        denominators = report["summary"]["denominators"]
        self.assertTrue(denominators["dispositions_close"])
        self.assertTrue(denominators["frequency_classes_close"])
        self.assertEqual(denominators["rows"], len(SAMPLE_ROWS))
        self.assertEqual(denominators["rows_located"], len(SAMPLE_ROWS))
        self.assertEqual(denominators["rows_unlocated"], 0)
        record_check()
        self.assertEqual(
            report["summary"]["overall"]["dispositions"],
            {
                "valid_common": 3,
                "maf_below_threshold": 2,
                "maf_missing": 5,
                "info_below_threshold": 0,
                "unlocated_source": 0,
                "source_unavailable": 0,
            },
        )
        record_check()

    def test_the_zero_frequency_row_is_traced_to_its_raw_tokens_and_admitted(self) -> None:
        _, report = self.trace()
        row = self.by_id(report)["S01"]
        record_check()

        self.assertTrue(row["location"]["located"])
        self.assertEqual(row["location"]["data_row"], 2)
        self.assertEqual(row["location"]["orientation"], "flipped")
        self.assertEqual(row["location"]["tokens"]["effect_allele_frequency"], "0.0")
        self.assertEqual(row["location"]["tokens"]["beta"], "0.05")
        self.assertEqual(row["location"]["tokens"]["standard_error"], "0.05")
        self.assertEqual(row["location"]["tokens"]["p_value"], "1e-4")
        record_check()
        self.assertEqual(row["source"]["path"], str(self.fixture.source))
        self.assertEqual(row["source"]["resolved_by"], "source_file")
        self.assertEqual(row["source"]["size_bytes"], self.fixture.source.stat().st_size)
        record_check()
        self.assertIsNone(row["frequency"]["parsed_af"])
        self.assertTrue(row["frequency"]["reader_treats_as_missing"])
        record_check()
        self.assertTrue(row["admission"]["keep"])
        self.assertEqual(row["admission"]["disposition"], "maf_missing")
        self.assertEqual(row["admission"]["maf_policy"], {"state": "filtered", "threshold": 0.005})
        self.assertEqual(row["admission"]["counts"]["maf_missing"], 1)
        record_check()
        self.assertTrue(row["store"]["z_present"])
        self.assertTrue(row["store"]["eaf_absent"])
        self.assertTrue(row["agreement"]["admission_matches_store"])
        self.assertTrue(row["agreement"]["stored_frequency_agrees"])
        record_check()

    def test_a_below_threshold_row_is_traced_and_the_store_holds_no_association(self) -> None:
        _, report = self.trace()
        row = self.by_id(report)["S02"]
        record_check()
        self.assertEqual(row["frequency"]["token"], "0.001")
        self.assertEqual(row["frequency"]["parsed_af"], 0.001)
        self.assertEqual(row["admission"]["disposition"], "maf_below_threshold")
        self.assertFalse(row["admission"]["keep"])
        self.assertEqual(row["admission"]["counts"]["maf_below_threshold"], 1)
        record_check()
        # #373 could not have sampled a dropped row: it has no variant_index in
        # the Store, and the report says so rather than inventing one.
        self.assertIsNone(row["variant_index"])
        self.assertFalse(row["store"]["located"])
        self.assertFalse(row["agreement"]["store_holds_association"])
        record_check()

    def test_the_on_panel_control_is_read_from_the_dense_component(self) -> None:
        _, report = self.trace()
        row = self.by_id(report)["S00"]
        record_check()
        self.assertEqual(row["store"]["plane"], "dense")
        self.assertEqual(row["store"]["dense_row"], 0)
        self.assertTrue(row["store"]["z_present"])
        record_check()
        # The raw row's effect allele is G, the canonical A1 is A, so the reader
        # flips it and the builder stores 1 - 0.4.
        self.assertEqual(row["location"]["orientation"], "flipped")
        self.assertAlmostEqual(row["frequency"]["parsed_af"], 0.4)
        self.assertAlmostEqual(row["frequency"]["canonical_af"], 0.6)
        self.assertFalse(row["store"]["eaf_absent"])
        code = dense_eaf_codes(self.fixture.store)[0, 0]
        self.assertAlmostEqual(float(code), 0.6, places=5)
        record_check()
        self.assertEqual(row["trace_disposition"], "valid_common")
        self.assertTrue(row["agreement"]["admission_matches_store"])
        record_check()

    def test_the_declared_policy_is_replayed_and_matches_the_store(self) -> None:
        _, report = self.trace()
        analysis = report["summary"]["by_analysis"][0]
        record_check()
        self.assertEqual(
            analysis["declared_policy"],
            {
                "maf_state": "filtered",
                "maf_threshold": 0.005,
                "info_score_state": "unavailable",
                "info_score_threshold": None,
                "imputation_score_column": None,
            },
        )
        kinds = {entry["kind"] for entry in report["inconsistencies"]}
        self.assertNotIn("declared_policy_disagrees_with_store", kinds)
        record_check()

    def test_the_quoted_store_admission_counts_are_the_sample_population(self) -> None:
        _, report = self.trace()
        analysis = report["summary"]["by_analysis"][0]
        record_check()
        # Hand-counted from the fixture table in the module docstring: 10 rows
        # observed, 2 dropped below the threshold, 8 admitted, 5 of them with no
        # usable frequency.
        self.assertEqual(
            analysis["store_recorded"],
            {
                "associations_observed": 10,
                "associations_retained": 8,
                "associations_missing": 5,
                "associations_below_threshold": 2,
            },
        )
        record_check()
        self.assertEqual(analysis["rows"], len(SAMPLE_ROWS))
        self.assertEqual(analysis["dispositions"]["maf_missing"], 5)
        self.assertEqual(analysis["source_frequency_classes"]["placeholder_zero"], 1)
        self.assertEqual(analysis["source_frequency_classes"]["placeholder_one"], 1)
        self.assertEqual(analysis["source_frequency_classes"]["out_of_range"], 1)
        record_check()
        placeholder = report["summary"]["placeholder_frequency"]
        self.assertEqual(placeholder["rows"], 2)  # 0.0 and 1.0
        self.assertEqual(placeholder["off_panel_rows"], 2)
        self.assertAlmostEqual(placeholder["fraction_of_off_panel"], 2 / 9, places=6)
        record_check()

    def test_a_limited_run_traces_only_the_rows_it_read(self) -> None:
        code, report = self.trace("--limit", "3", output="limited.json")
        record_check()
        self.assertEqual(code, 0)
        self.assertEqual(len(report["rows"]), 3)
        self.assertEqual(report["summary"]["denominators"]["rows"], 3)
        self.assertTrue(report["summary"]["denominators"]["dispositions_close"])
        record_check()

    def test_a_manifest_that_declares_another_threshold_is_an_error(self) -> None:
        # A policy the Store was not built under must not be replayed silently.
        other = self.fixture.evidence / "other-analyses.tsv"
        write_manifest(other, self.fixture.source, maf="0.01")
        code, report = self.trace(manifest=other, output="other-policy.json")
        record_check()
        self.assertEqual(code, 1)
        kinds = {entry["kind"] for entry in report["inconsistencies"]}
        self.assertIn("declared_policy_disagrees_with_store", kinds)
        record_check()


# ---------------------------------------------------------------------------
# Seam 3: fail loudly, and prove the Store read-only
# ---------------------------------------------------------------------------


class TestTraceFailLoudly(TraceTestCase):
    def test_refuses_to_write_evidence_inside_the_store(self) -> None:
        code, _, err = self.run_audit(
            "trace",
            str(self.fixture.store),
            "--sample",
            str(self.fixture.sample),
            "--manifest",
            str(self.fixture.manifest),
            "--output",
            str(self.fixture.store / "trace.json"),
        )
        record_check()
        self.assertEqual(code, 2)
        self.assertIn("inside the Store", err)
        self.assertFalse((self.fixture.store / "trace.json").exists())
        record_check()

    def test_a_sample_that_does_not_exist_cannot_be_traced(self) -> None:
        code, report = self.trace(sample=self.fixture.root / "absent.tsv", output="absent.json")
        record_check()
        self.assertEqual(code, 2)
        self.assertEqual(report, {})
        record_check()

    def test_a_sample_naming_an_unknown_analysis_is_an_error(self) -> None:
        sample = self.fixture.evidence / "unknown-analysis.tsv"
        sample.write_text(
            "sample_id\tanalysis_id\tanalysis_index\talid\tstratum\n"
            f"X00\tGCST000000\t0\t1:1000:A:G\texclusive_analysis:0\n",
            encoding="utf-8",
        )
        code, report = self.trace(sample=sample, output="unknown-analysis.json")
        record_check()
        self.assertEqual(code, 1)
        kinds = {entry["kind"] for entry in report["inconsistencies"]}
        self.assertIn("analysis_absent_from_build_manifest", kinds)
        self.assertEqual(report["rows"][0]["trace_disposition"], "source_unavailable")
        record_check()

    def test_a_missing_source_file_is_an_error(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="ogstores_audit_trace_missing_"))
        try:
            fixture = build_fixture(root)
            fixture.source.unlink()
            target = fixture.evidence / "missing-source.json"
            code, out, err = self.run_audit(
                "trace",
                str(fixture.store),
                "--sample",
                str(fixture.sample),
                "--manifest",
                str(fixture.manifest),
                "--output",
                str(target),
                "--workers",
                "1",
            )
            report = json.loads(target.read_text(encoding="utf-8"))
            record_check()
            self.assertEqual(code, 1, out + err)
            kinds = {entry["kind"] for entry in report["inconsistencies"]}
            self.assertIn("source_file_missing", kinds)
            self.assertTrue(
                all(row["trace_disposition"] == "source_unavailable" for row in report["rows"])
            )
            record_check()
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_a_sample_whose_stored_identity_disagrees_is_an_error(self) -> None:
        sample = self.fixture.evidence / "wrong-identity.tsv"
        indices = store_variants(self.fixture.store)
        sample.write_text(
            "sample_id\tanalysis_id\tanalysis_index\talid\tvariant_index\tstratum\n"
            # The 0.001 row's ALID, pointed at the admitted placeholder's row.
            f"Y00\t{ANALYSIS_ID}\t0\t1:2000:C:T\t{indices['1:1000:A:G']}\texclusive_analysis:0\n",
            encoding="utf-8",
        )
        code, report = self.trace(sample=sample, output="wrong-identity.json")
        record_check()
        self.assertEqual(code, 1)
        kinds = {entry["kind"] for entry in report["inconsistencies"]}
        self.assertIn("store_identity_disagrees_with_sample", kinds)
        self.assertIn("admission_disagrees_with_store", kinds)
        record_check()

    def test_the_store_is_unchanged_by_a_trace(self) -> None:
        before = tree_digests(self.fixture.store)
        code, report = self.trace(output="readonly.json")
        after = tree_digests(self.fixture.store)
        record_check()
        self.assertEqual(code, 0)
        self.assertEqual(before, after)
        self.assertTrue(report["store_unchanged"]["unchanged"])
        self.assertEqual(report["store_unchanged"]["changed"], [])
        self.assertEqual(report["store_unchanged"]["added"], [])
        self.assertEqual(report["store_unchanged"]["removed"], [])
        record_check()


def main() -> None:
    suite = unittest.TestSuite()
    for case in (TestBuilderAdmissionDefect, TestTraceReport, TestTraceFailLoudly):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(case))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful():
        sys.exit(1)
    print(f"ALL {n_checks} CHECKS PASSED")


if __name__ == "__main__":
    main()
