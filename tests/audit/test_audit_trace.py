#!/usr/bin/env python3
"""The trace command (`ogstores.audit trace`, stores #374).

Part of epic #371. #372 reconciled the off-panel axis, #373 attributed its
101M-variant union to Analyses and publications and pinned a seeded sample,
and this slice follows that sample back to the raw rows the builder read, the
frequency it parsed from each of them, and the admission decision it made.

The fixture is not a hand-written Store: it is a real format-0.2.0 Hybrid Store
Release built through OpenGWASDB's own builder (`build_hybrid_from_vcf_manifest`)
from a ten-row GWAS-SSF file, one Analysis and one panel variant. That is the
point of this suite -- the admission decision the ticket is about is a property
of the *builder*, so a reproducer that hand-built the Store could not show it.
The source file's rows state every frequency class at once:

```text
row     effect/other  eaf        source frequency class   admission        in Store
1:1     G/A           0.4        valid_common             valid_common     yes (panel)
1:1000  G/A           0.0        placeholder_zero         maf_missing      no
1:2000  T/C           0.001      below_threshold          maf_below        no
1:3000  A/G           0.5        valid_common             valid_common     yes
1:4000  C/T           0.0049     below_threshold          maf_below        no
1:5000  G/T           0.005      valid_common             valid_common     yes (lower boundary)
1:6000  T/A           1.0        placeholder_one          maf_missing      no
1:7000  A/C           (blank)    missing                  maf_missing      no
1:8000  C/G           1.5        out_of_range             maf_missing      no
1:10000 A/T           abc        malformed                maf_missing      no
1:11000 A/G           0.995      valid_common             valid_common     yes (upper boundary)
1:12000 C/T           0.9951     below_threshold          maf_below        no
1:13000 G/T           inf        nonfinite                maf_missing      no
1:14000 A/C           NaN        missing                  maf_missing      no
```

so under the pinned rule (`opengwasdb` `7308c6a`, core #331) the admitted rows
are 4: 14 observed, 3 dropped below the 0.005 threshold and 7 dropped for
carrying no usable frequency. Both MAF boundaries are inclusive: 0.005 and
0.995 pass, 0.0049 and 0.9951 do not. The `1:6000` row is the flipped-orientation
case (its raw effect allele is `T`, the canonical A1 is `A`, so the builder
reads `1 - 1.0` -- nothing on either count), and the `1:1` row is the on-panel
control whose cell lives in the Dense Component.

The two placeholder rows are the fix's own case. `admit_rows` judges an exact
`0.0`/`1.0` as a frequency whose MAF is 0 and drops it as below any positive
threshold when it is handed the number; the *reader* reports no stored EAF for
one (`parse_af`, ADR 0036), so what reaches the rule in a real build is NaN and
the build counts the row `maf_missing` -- dropped, under the corrected rule, for
having no frequency rather than kept unjudged as it was before core #331. Either
way the row leaves the Store, which is what #380 repins for.
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
    # The upper boundary is symmetric with the lower one, and a non-finite token
    # is a fifth way to report no frequency.
    ("1", "11000", "A", "G", "0.995", "rs10"),
    ("1", "12000", "C", "T", "0.9951", "rs11"),
    ("1", "13000", "G", "T", "inf", "rs12"),
    ("1", "14000", "A", "C", "NaN", "rs13"),
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

#: The sample #373 would emit for this Store, extended with the rows the MAF
#: filter drops: every source row is named so each frequency class is traced once.
#: A dropped row has no `variant_index` in the Store (it has no association), which
#: a sample drawn from the Store alone could not contain, and the report says so
#: rather than inventing one. `1:1:A:G` is the panel row.
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
    ("S10", "1:11000:A:G", "exclusive_analysis:0"),
    ("S11", "1:12000:C:T", "exclusive_analysis:0"),
    ("S12", "1:13000:G:T", "exclusive_analysis:0"),
    ("S13", "1:14000:A:C", "exclusive_analysis:0"),
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
    "S10": "valid_common",
    "S11": "maf_below_threshold",
    "S12": "maf_missing",
    "S13": "maf_missing",
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
    "S10": "valid_common",
    "S11": "below_threshold",
    "S12": "nonfinite",
    "S13": "missing",
}


def write_source(path: Path, rows=SOURCE_ROWS) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write("\t".join(SOURCE_COLUMNS) + "\n")
        for chromosome, position, effect, other, eaf, rsid in rows:
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


#: The declaration triple a numeric `info_score_threshold` requires (stores #175):
#: a column name, its kind, and independent provider evidence for it.
INFO_DECLARATION = {
    "imputation_score_column": "INFO",
    "imputation_score_kind": "imputation_info",
    "imputation_score_provenance": "fixture:provider",
}


def write_manifest(
    path: Path,
    source: Path,
    *,
    maf: str = "0.005",
    info: str = "NaN",
    extra: dict[str, str] | None = None,
) -> None:
    extra = extra or {}
    columns = (*MANIFEST_COLUMNS, *extra)
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
            *(extra[name] for name in extra),
        )
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write("\t".join(columns) + "\n")
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

    def build(self, *, maf: str = "0.005", info: str = "NaN", source_rows=SOURCE_ROWS) -> "Fixture":
        write_source(self.source, source_rows)
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
        self.write_build_record()
        write_sample(self.sample, self.store)
        return self

    def write_build_record(self) -> None:
        """The Store Release's own build record, which the CLI build writes beside it.

        The trace reads it to compare its engine's revision with the revision that
        wrote the Store; an in-process build does not write one, so the fixture
        states the revision the engine in this environment reports.
        """
        runtime = audit._runtime_revision()
        records = self.root / "records"
        records.mkdir(exist_ok=True)
        (records / "build.json").write_text(
            json.dumps(
                {
                    "step": "build",
                    "store_id": STORE_ID,
                    "exit_code": 0,
                    "success": True,
                    "argv": ["opengwasdb", "build-hybrid", str(self.manifest), str(self.store)],
                    "opengwasdb_rev": runtime["opengwasdb_rev"] or "fixture000",
                    "opengwasdb_version": runtime["opengwasdb_version"],
                }
            ),
            encoding="utf-8",
        )


def build_fixture(root: Path, **kwargs: object) -> Fixture:
    return Fixture(root).build(**kwargs)  # type: ignore[arg-type]


def build_info_fixture(root: Path) -> Fixture:
    """A Store whose Analysis declares a numeric INFO filter over a source `INFO` column.

    Three rows: an admitted panel row (INFO 0.95), a row with a perfectly usable
    frequency that only the declared INFO score drops (AF 0.4, INFO 0.5), and a
    placeholder row that INFO passes (AF 0.0, INFO 0.9). The middle row is the
    case a fixed column list would silently mis-read: its score column is not in
    GWAS-SSF's vocabulary, and without it the row looks unscoreable and is kept.
    """
    fixture = Fixture(root)
    rows = (
        ("1", "1", "G", "A", "0.4", "rs0", "0.95"),
        ("1", "1000", "G", "A", "0.4", "rs1", "0.5"),
        ("1", "2000", "T", "C", "0.0", "rs2", "0.9"),
    )
    with gzip.open(fixture.source, "wt", encoding="utf-8") as handle:
        handle.write("\t".join((*SOURCE_COLUMNS, "INFO")) + "\n")
        for chromosome, position, effect, other, eaf, rsid, info in rows:
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
                        info,
                    )
                )
                + "\n"
            )
    write_manifest(fixture.manifest, fixture.source, info="0.8", extra=INFO_DECLARATION)
    fixture.panel.write_text("\n".join(PANEL_LINES) + "\n", encoding="utf-8")
    build_hybrid_from_vcf_manifest(
        fixture.manifest,
        fixture.store,
        variant_reference=fixture.panel,
        store_id=STORE_ID,
        release_id=STORE_ID,
        n_workers=1,
    )
    fixture.write_build_record()
    write_sample(
        fixture.sample,
        fixture.store,
        (
            ("T00", "1:1:A:G", "on_panel_control"),
            ("T01", "1:1000:A:G", "exclusive_analysis:0"),
            ("T02", "1:2000:C:T", "exclusive_analysis:0"),
        ),
    )
    return fixture


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


class TestBuilderAdmissionRule(TraceTestCase):
    """The corrected rule, at the real builder entry point.

    A source row whose `effect_allele_frequency` is exactly `0.0` used to be
    admitted while a row whose frequency is `0.001` was dropped by the same 0.005
    MAF policy, because `parse_af` maps `0.0` to *missing* and the old rule kept a
    row it could not measure. At `opengwasdb` `7308c6a` (core #331) a frequency
    the rule cannot read is no evidence a common-variant Store may admit, so both
    rows are dropped -- the low one for its MAF, the placeholder one for having no
    frequency at all.
    """

    def test_the_builder_drops_a_zero_frequency_row_and_a_low_one_alike(self) -> None:
        indices = store_variants(self.fixture.store)
        record_check()

        # The low frequency is judged by the policy and dropped.
        self.assertNotIn("1:2000:C:T", indices)
        record_check()
        # Exactly zero is a frequency the reader reports as absent, so the rule has
        # nothing to judge and drops the row for that reason instead.
        self.assertNotIn("1:1000:A:G", indices)
        record_check()

        # What is admitted is the two genuine common rows and the two boundaries.
        self.assertEqual(
            sorted(indices),
            sorted(["1:1:A:G", "1:3000:A:G", "1:5000:G:T", "1:11000:A:G"]),
        )
        record_check()

        # And an admitted off-panel row still carries the frequency it was judged
        # by, so the drops above are the filter's doing and not a lost column.
        variants, analyses, codes = ragged_eaf_codes(self.fixture.store)
        admitted = np.flatnonzero((analyses == 0) & (variants == indices["1:3000:A:G"]))
        self.assertEqual(admitted.size, 1)
        record_check()
        self.assertFalse(
            is_absent(codes[admitted[0]]),
            f"the admitted 0.5 row carries eaf {codes[admitted[0]]!r}, the absent code",
        )
        record_check()

    def test_the_row_admission_rule_replays_the_two_cases_directly(self) -> None:
        # The reader's own parse is what turns the zero into "no frequency"; the
        # admission rule then drops a row it has no frequency for.
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
        self.assertEqual(admission.keep.tolist(), [False, False])
        self.assertEqual(admission.counts.maf_below_threshold, 1)
        self.assertEqual(admission.counts.maf_missing, 1)
        self.assertEqual(admission.counts.admitted, 0)
        record_check()
        # Given the number rather than the reader's absence, the zero is judged
        # and dropped as below any positive threshold (core #331).
        judged = admit_rows(
            np.array([np.nan], dtype="float64"),
            np.array(["undeclared"], dtype=object),
            np.array([0.0], dtype="float64"),
            info,
            maf,
        )
        record_check()
        self.assertEqual(judged.keep.tolist(), [False])
        self.assertEqual(judged.counts.maf_below_threshold, 1)
        self.assertEqual(judged.counts.maf_missing, 0)
        record_check()

    def test_the_maf_boundary_is_exclusive_below_and_inclusive_at_it(self) -> None:
        indices = store_variants(self.fixture.store)
        self.assertIn("1:5000:G:T", indices)  # maf exactly 0.005 passes
        self.assertNotIn("1:4000:C:T", indices)  # 0.0049 does not
        record_check()

    def test_a_placeholder_one_and_an_out_of_range_frequency_are_dropped_too(self) -> None:
        indices = store_variants(self.fixture.store)
        # Exactly 1.0, 1.5 and an unparseable token are all dropped: the reader
        # parses each as absent, and a `filtered` policy admits no row whose MAF it
        # cannot read (core #331).
        for alid in ("1:6000:A:T", "1:8000:C:G", "1:10000:A:T"):
            self.assertNotIn(alid, indices)
        record_check()

    def test_the_upper_maf_boundary_is_inclusive_and_just_above_is_dropped(self) -> None:
        indices = store_variants(self.fixture.store)
        # af = 0.995 has MAF exactly 0.005 and passes; 0.9951 has MAF 0.0049 and
        # does not. The filter is symmetric about 0.5, so the same equality rule
        # decides both ends.
        self.assertIn("1:11000:A:G", indices)
        self.assertNotIn("1:12000:C:T", indices)
        record_check()
        _, report = self.trace(output="upper-boundary.json")
        rows = self.by_id(report)
        self.assertEqual(rows["S10"]["trace_disposition"], "valid_common")
        self.assertAlmostEqual(rows["S10"]["admission"]["maf"], 0.005, places=9)
        self.assertFalse(rows["S10"]["store"]["eaf_absent"])
        record_check()
        self.assertEqual(rows["S11"]["trace_disposition"], "maf_below_threshold")
        self.assertAlmostEqual(rows["S11"]["admission"]["maf"], 0.0049, places=9)
        record_check()

    def test_a_non_finite_frequency_is_dropped_as_maf_missing(self) -> None:
        indices = store_variants(self.fixture.store)
        # `inf` is not a frequency any rule can judge, and a literal `NaN` is the
        # blank case spelled as a number: neither is admitted.
        self.assertNotIn("1:13000:G:T", indices)
        self.assertNotIn("1:14000:A:C", indices)
        record_check()
        _, report = self.trace(output="nonfinite.json")
        rows = self.by_id(report)
        self.assertEqual(rows["S12"]["frequency"]["class"], "nonfinite")
        self.assertIsNone(rows["S12"]["frequency"]["parsed_af"])
        self.assertEqual(rows["S12"]["trace_disposition"], "maf_missing")
        self.assertFalse(rows["S12"]["admission"]["keep"])
        self.assertIsNone(rows["S12"]["admission"]["maf"])
        record_check()
        self.assertEqual(rows["S13"]["frequency"]["class"], "missing")
        self.assertEqual(rows["S13"]["trace_disposition"], "maf_missing")
        self.assertFalse(rows["S13"]["admission"]["keep"])
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
                "valid_common": 4,
                "maf_below_threshold": 3,
                "maf_missing": 7,
                "info_below_threshold": 0,
                "unlocated_source": 0,
                "ambiguous_source": 0,
                "source_unavailable": 0,
            },
        )
        record_check()

    def test_the_zero_frequency_row_is_traced_to_its_raw_tokens_and_dropped(self) -> None:
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
        self.assertEqual(row["frequency"]["class"], "placeholder_zero")
        self.assertIsNone(row["frequency"]["parsed_af"])
        self.assertTrue(row["frequency"]["reader_treats_as_missing"])
        record_check()
        self.assertFalse(row["admission"]["keep"])
        self.assertEqual(row["admission"]["disposition"], "maf_missing")
        self.assertIsNone(row["admission"]["maf"])
        self.assertEqual(row["admission"]["maf_policy"], {"state": "filtered", "threshold": 0.005})
        self.assertEqual(row["admission"]["counts"]["maf_missing"], 1)
        self.assertEqual(row["admission"]["counts"]["maf_below_threshold"], 0)
        record_check()
        # Dropped rows name no `variant_index`, so there is no Store cell to read
        # and no agreement to claim: the trace says so rather than inventing one.
        self.assertIsNone(row["variant_index"])
        self.assertFalse(row["store"]["located"])
        self.assertIsNone(row["store"]["z_present"])
        self.assertIsNone(row["agreement"]["admission_matches_store"])
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
        # Hand-counted from the fixture table in the module docstring: 14 rows
        # observed, 3 dropped below the threshold, 7 dropped for no usable
        # frequency, and 4 admitted. `associations_missing` counts dropped rows
        # under a `filtered` policy since core #331 -- it is not a retained tally.
        self.assertEqual(
            analysis["store_recorded"],
            {
                "associations_observed": 14,
                "associations_retained": 4,
                "associations_missing": 7,
                "associations_below_threshold": 3,
            },
        )
        record_check()
        self.assertEqual(analysis["rows"], len(SAMPLE_ROWS))
        self.assertEqual(analysis["dispositions"]["maf_missing"], 7)
        self.assertEqual(analysis["source_frequency_classes"]["placeholder_zero"], 1)
        self.assertEqual(analysis["source_frequency_classes"]["placeholder_one"], 1)
        self.assertEqual(analysis["source_frequency_classes"]["out_of_range"], 1)
        record_check()
        placeholder = report["summary"]["placeholder_frequency"]
        self.assertEqual(placeholder["rows"], 2)  # 0.0 and 1.0
        self.assertEqual(placeholder["off_panel_rows"], 2)
        self.assertAlmostEqual(placeholder["fraction_of_off_panel"], 2 / 13, places=6)
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
            # The 0.001 row's ALID, pointed at the admitted 0.5 row: the Store holds
            # an association there, and the replay of this row drops it.
            f"Y00\t{ANALYSIS_ID}\t0\t1:2000:C:T\t{indices['1:5000:G:T']}\texclusive_analysis:0\n",
            encoding="utf-8",
        )
        code, report = self.trace(sample=sample, output="wrong-identity.json")
        record_check()
        self.assertEqual(code, 1)
        kinds = {entry["kind"] for entry in report["inconsistencies"]}
        self.assertIn("store_identity_disagrees_with_sample", kinds)
        self.assertIn("admission_disagrees_with_store", kinds)
        record_check()

    def test_the_output_cannot_replace_an_input(self) -> None:
        import hashlib

        def digest(path: Path) -> str:
            return hashlib.sha256(path.read_bytes()).hexdigest()

        sample_before = digest(self.fixture.sample)
        manifest_before = digest(self.fixture.manifest)
        source_before = digest(self.fixture.source)
        for named, target in (
            ("sample", self.fixture.sample),
            ("build manifest", self.fixture.manifest),
            ("raw source", self.fixture.source),
        ):
            code, _, err = self.run_audit(
                "trace",
                str(self.fixture.store),
                "--sample",
                str(self.fixture.sample),
                "--manifest",
                str(self.fixture.manifest),
                "--output",
                str(target),
            )
            record_check()
            self.assertEqual(code, 2, named)
            self.assertIn("refusing to write evidence", err)
        self.assertEqual(digest(self.fixture.sample), sample_before)
        self.assertEqual(digest(self.fixture.manifest), manifest_before)
        self.assertEqual(digest(self.fixture.source), source_before)
        record_check()

    def test_the_runtime_engine_revision_is_recorded_and_compared(self) -> None:
        _, report = self.trace(output="replay.json")
        replay = report["replay"]
        record_check()
        self.assertTrue(replay["engine"]["opengwasdb_version"])
        self.assertEqual(replay["build_recorded"]["path"], str(self.fixture.root / "records" / "build.json"))
        self.assertTrue(replay["build_recorded"]["available"])
        self.assertIs(replay["agrees"], True)
        record_check()
        kinds = {entry["kind"] for entry in report["inconsistencies"]}
        self.assertNotIn("replay_revision_differs", kinds)
        self.assertNotIn("build_revision_unrecorded", kinds)
        record_check()

    def test_a_recorded_revision_that_differs_is_a_finding_not_an_error(self) -> None:
        record = self.fixture.root / "records" / "build.json"
        original = record.read_text(encoding="utf-8")
        try:
            data = json.loads(original)
            data["opengwasdb_rev"] = "0" * 40
            record.write_text(json.dumps(data), encoding="utf-8")
            code, report = self.trace(output="revision-differs.json")
            record_check()
            self.assertEqual(code, 0)
            self.assertIs(report["replay"]["agrees"], False)
            kinds = {entry["kind"] for entry in report["inconsistencies"]}
            self.assertIn("replay_revision_differs", kinds)
            record_check()
        finally:
            record.write_text(original, encoding="utf-8")

    def test_a_missing_build_record_is_an_error(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="ogstores_audit_trace_norecord_"))
        try:
            fixture = build_fixture(root)
            (fixture.root / "records" / "build.json").unlink()
            target = fixture.evidence / "no-record.json"
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
            self.assertEqual(report["replay"]["build_recorded"]["problem"], "missing")
            kinds = {entry["kind"] for entry in report["inconsistencies"]}
            self.assertIn("build_revision_unrecorded", kinds)
            record_check()
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_an_ambiguous_source_row_keeps_its_candidates_and_is_an_error(self) -> None:
        # Two rows at one position with one allele pair and different frequencies:
        # the trace must not choose one by first-seen order.
        root = Path(tempfile.mkdtemp(prefix="ogstores_audit_trace_ambiguous_"))
        try:
            fixture = build_fixture(
                root, source_rows=(*SOURCE_ROWS, ("1", "3000", "A", "G", "0.9", "rs3b"))
            )
            target = fixture.evidence / "ambiguous.json"
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
            row = next(entry for entry in report["rows"] if entry["sample_id"] == "S03")
            record_check()
            self.assertEqual(code, 1, out + err)
            self.assertEqual(row["trace_disposition"], "ambiguous_source")
            self.assertEqual(row["location"]["candidate_rows"], 2)
            self.assertFalse(row["location"]["frequency_agreement"])
            self.assertEqual(len(row["location"]["candidates"]), 2)
            self.assertEqual(
                sorted(entry["canonical_af"] for entry in row["location"]["distinct_frequencies"]),
                [0.5, 0.9],
            )
            record_check()
            kinds = {entry["kind"] for entry in report["inconsistencies"]}
            self.assertIn("source_row_ambiguous", kinds)
            self.assertEqual(report["summary"]["denominators"]["rows_ambiguous"], 1)
            record_check()
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_a_declared_info_column_is_read_and_its_threshold_applied(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="ogstores_audit_trace_info_"))
        try:
            fixture = build_info_fixture(root)
            target = fixture.evidence / "info.json"
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
            rows = {row["sample_id"]: row for row in report["rows"]}
            record_check()
            self.assertEqual(code, 0, out + err)
            # The declared column is not GWAS-SSF's: the scan must be told about it,
            # or the row reads as unscored and is kept.
            self.assertIn("INFO", rows["T01"]["location"]["columns"])
            self.assertEqual(rows["T01"]["location"]["tokens"]["INFO"], "0.5")
            record_check()
            self.assertEqual(rows["T01"]["admission"]["info_policy"], {"state": "filtered", "threshold": 0.8})
            self.assertEqual(rows["T01"]["admission"]["info_score"]["token"], "0.5")
            self.assertEqual(rows["T01"]["trace_disposition"], "info_below_threshold")
            self.assertFalse(rows["T01"]["admission"]["keep"])
            record_check()
            # Its frequency is 0.4: usable, and above the MAF threshold. The class
            # must not be read off the disposition, or the MAF rule would be blamed
            # for the INFO rule's decision.
            self.assertEqual(rows["T01"]["frequency"]["class"], "valid_common")
            self.assertAlmostEqual(rows["T01"]["admission"]["maf"], 0.4)
            record_check()
            self.assertEqual(rows["T00"]["trace_disposition"], "valid_common")
            self.assertEqual(rows["T02"]["trace_disposition"], "maf_missing")
            record_check()
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_an_info_policy_threshold_that_differs_from_the_store_is_an_error(self) -> None:
        # The state alone is not the policy: 0.8 and 0.9 are both `filtered` and
        # keep different rows.
        root = Path(tempfile.mkdtemp(prefix="ogstores_audit_trace_info_threshold_"))
        try:
            fixture = build_info_fixture(root)
            other = fixture.evidence / "other-threshold.tsv"
            write_manifest(other, fixture.source, info="0.9", extra=INFO_DECLARATION)
            target = fixture.evidence / "info-threshold.json"
            code, out, err = self.run_audit(
                "trace",
                str(fixture.store),
                "--sample",
                str(fixture.sample),
                "--manifest",
                str(other),
                "--output",
                str(target),
                "--workers",
                "1",
            )
            report = json.loads(target.read_text(encoding="utf-8"))
            record_check()
            self.assertEqual(code, 1, out + err)
            kinds = {entry["kind"] for entry in report["inconsistencies"]}
            self.assertIn("declared_policy_disagrees_with_store", kinds)
            details = " ".join(entry["detail"] for entry in report["inconsistencies"])
            self.assertIn("info_score_threshold", details)
            record_check()
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_a_store_cell_that_cannot_be_read_is_an_error(self) -> None:
        # An audit that cannot read the cell it sampled must say so, never report
        # `ok` without having looked.
        root = Path(tempfile.mkdtemp(prefix="ogstores_audit_trace_unreadable_"))
        try:
            fixture = build_fixture(root)
            shutil.rmtree(fixture.store / "data.zarr" / "ragged" / "by_variant" / "z")
            target = fixture.evidence / "unreadable.json"
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
            self.assertFalse(report["ok"])
            kinds = {entry["kind"] for entry in report["inconsistencies"]}
            self.assertIn("store_cell_unreadable", kinds)
            record_check()
        finally:
            shutil.rmtree(root, ignore_errors=True)

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
    for case in (TestBuilderAdmissionRule, TestTraceReport, TestTraceFailLoudly):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(case))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful():
        sys.exit(1)
    print(f"ALL {n_checks} CHECKS PASSED")


if __name__ == "__main__":
    main()
