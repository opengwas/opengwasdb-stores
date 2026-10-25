#!/usr/bin/env python3
"""The read-only Store Audit CLI (`ogstores.audit reconcile`, stores #372).

Part of epic #371, whose first slice is one read-only command that reconciles a
built Hybrid Store Release against what the Store itself records. The tests are
CLI-level on purpose: what the epic asks for is an externally visible,
replayable report, so the assertions are on the command's exit code, its JSON
evidence and the state of the input Store -- not on private helpers.

The fixtures are real format-0.2.0 Hybrid Store Releases of a few dozen
variants, written through OpenGWASDB's own writers (`write_variant_axis`, the
`ragged/by_variant/` group, `manifest.json`), so the audit reads exactly the
layout it reads on a production Store. Their association bodies are a
controlled permutation: this slice reads the CSR offsets and the variant table,
never a statistic, so a fixture's per-Analysis assignment only has to agree with
its own offsets.

The production Store is audited by the command itself (see
`docs/spec/store-audit.md`); `OGSTORES_AUDIT_PRODUCTION_STORE` opts this suite
into also proving, against the real Store, that a full audit leaves every file
byte-identical. It is off by default because this suite must stay data-free on
CI.
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

from opengwasdb.model.manifest import StoreManifest
from opengwasdb.variants.axis import write_variant_axis
from opengwasdb.variants.normalise import CanonicalVariant

from ogstores import audit

STORE_ID = "OGS-00090"
DENSE_STORE_ID = "OGS-00091"

n_checks = 0


def record_check() -> None:
    global n_checks
    n_checks += 1


# ---------------------------------------------------------------------------
# Fixtures: a real Hybrid Store Release, small enough to assert on by hand
# ---------------------------------------------------------------------------


def axis_variants(n: int, *, chromosome: str = "1", start: int = 100) -> list[CanonicalVariant]:
    """`n` canonical variants on one contig, 10 bp apart, alleles A/G."""
    return [
        CanonicalVariant(chromosome=chromosome, position=start + 10 * i, effect_allele="A", other_allele="G")
        for i in range(n)
    ]


def hybrid_provenance(
    reference: Path,
    *,
    n_variants: int,
    n_analyses: int,
    n_panel: int,
    n_overflow: int,
) -> dict[str, object]:
    """The provenance block a Hybrid build records, in the production shape.

    Mirrors the keys `opengwasdb`'s Hybrid builder writes -- `n_variants`,
    `n_analyses`, `hybrid.{n_panel,n_off_panel,n_overflow_associations}`,
    `ragged.by_variant.{n_axis,n_rows}` and `variant_reference` -- because the
    audit reconciles the Store against exactly those claims.
    """
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
            "n_overflow_associations": n_overflow,
        },
        "ragged": {"by_variant": {"group": "by_variant", "n_axis": n_variants, "n_rows": n_overflow}},
        "maf": {"analyses": []},
        "info_score": {"analyses": []},
    }


def write_analyses(store: Path, n_analyses: int, *, store_id: str = STORE_ID) -> None:
    """Write `analyses.tsv`, the release's own membership table.

    `build_hybrid_store` writes it because the audit reconciles the Ragged
    component's Analysis partition against it: a Store whose membership table
    is missing or disagrees is a broken release, not a Store without an
    accounting.
    """
    lines = ["analysis_id\tancestry\tbuild\n"]
    lines += [f"{store_id}-A{i:03d}\tEUR\tGRCh38\n" for i in range(n_analyses)]
    (store / "analyses.tsv").write_text("".join(lines), encoding="utf-8")


def write_build_record(
    root: Path,
    *,
    store_id: str = STORE_ID,
    revision: str = "fixture0rev",
) -> Path:
    """Write `records/build.json` beside the Store, as Store Release Build does.

    It is the only place a Store's exact `opengwasdb` revision is recorded, so
    an audit that cannot read it cannot say which code wrote the Store.
    """
    records = root / store_id / "records"
    records.mkdir(parents=True, exist_ok=True)
    path = records / "build.json"
    path.write_text(
        json.dumps(
            {
                "step": "build",
                "store_id": store_id,
                "exit_code": 0,
                "success": True,
                "start_time": "2026-01-01T00:00:00Z",
                "end_time": "2026-01-01T01:00:00Z",
                "opengwasdb_rev": revision,
                "argv": ["opengwasdb", "build-hybrid", "store"],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def rewrite_position(store: Path, row: int, position: int) -> None:
    """Overwrite one table row's position column, leaving its ALID intact.

    A controlled contradiction between a row's columns and its canonical
    identity -- what a mis-written row looks like -- so the audit's sampled
    ALID/column cross-check has something to catch.
    """
    import pysam

    table = store / "variants.tsv.gz"
    with pysam.BGZFile(str(table), "r") as handle:
        # pysam's iterator yields each line without its terminator.
        lines = [line.decode("utf-8").rstrip("\n") for line in handle]
    seen = -1
    for index, line in enumerate(lines):
        if line.startswith("#"):
            continue
        seen += 1
        if seen == row:
            fields = line.split("\t")
            fields[1] = str(position)
            lines[index] = "\t".join(fields)
            break
    with pysam.BGZFile(str(table), "w") as handle:
        for line in lines:
            handle.write((line + "\n").encode("utf-8"))
    pysam.tabix_index(
        str(table), seq_col=0, start_col=1, end_col=1, meta_char="#", zerobased=False, force=True
    )


@contextlib.contextmanager
def hold_release_lock(root: Path, store_id: str = STORE_ID):
    """Hold a release's exclusive run lock, as a release entry point does."""
    import fcntl

    locks = root / ".release-locks"
    locks.mkdir(exist_ok=True)
    path = locks / f"{store_id}.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        os.pwrite(fd, f"pid {os.getpid()}, started by the audit suite\n".encode(), 0)
        yield path
    finally:
        os.close(fd)


def write_manifest(
    store: Path,
    provenance: dict[str, object],
    *,
    store_id: str = STORE_ID,
    layout: str = "hybrid",
) -> None:
    manifest = StoreManifest.from_dict(
        {
            "store_id": store_id,
            "release_id": store_id,
            "format_version": "0.2.0",
            "primary_layout": layout,
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


def write_reference(path: Path, alids: list[str]) -> None:
    """Write a construction variant reference in the real one-ALID-per-line form."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for alid in alids:
            handle.write(f"{alid}\n")


def write_ragged(store: Path, counts: list[int], n_analyses: int) -> None:
    """Write the Ragged Overflow's CSR planes for `counts` associations per variant.

    `ragged/` is Analysis-major and `ragged/by_variant/` is variant-major; both
    are written here, from one association list, so their row counts and
    offsets agree by construction. Only the lengths and offsets are read by the
    audit, so the Analysis each row belongs to is assigned round-robin.
    """
    total = int(sum(counts))
    variant_index = np.repeat(np.arange(len(counts), dtype="int32"), np.asarray(counts, dtype="int64"))
    variant_index = np.ascontiguousarray(variant_index, dtype="int32")
    analysis_index = (np.arange(total, dtype="int64") % n_analyses).astype("int32")
    order = np.argsort(analysis_index, kind="stable")
    variant_index_by_analysis = variant_index[order]
    analysis_rows = np.bincount(analysis_index, minlength=n_analyses).astype("int64")
    analysis_offsets = np.concatenate([[0], np.cumsum(analysis_rows)])
    variant_offsets = np.concatenate([[0], np.cumsum(np.asarray(counts, dtype="int64"))])

    group = zarr.open_group(str(store / "data.zarr"), mode="w", zarr_format=3)
    ragged = group.require_group("ragged")
    chunk = (max(1, min(1000, total)),)
    ragged.create_array("offsets", shape=(n_analyses + 1,), dtype="int64", chunks=(n_analyses + 1,))
    ragged.create_array("variant_index", shape=(total,), dtype="int32", chunks=chunk)
    ragged["offsets"][:] = analysis_offsets
    ragged["variant_index"][:] = variant_index_by_analysis

    by_variant = ragged.require_group("by_variant")
    by_variant.create_array("offsets", shape=(len(counts) + 1,), dtype="int64", chunks=(len(counts) + 1,))
    by_variant.create_array("analysis_index", shape=(total,), dtype="int32", chunks=chunk)
    by_variant.create_array("z", shape=(total,), dtype="int16", chunks=chunk)
    by_variant.create_array("se", shape=(total,), dtype="float16", chunks=chunk)
    by_variant["offsets"][:] = variant_offsets
    by_variant["analysis_index"][:] = analysis_index
    by_variant["z"][:] = np.zeros((total,), dtype="int16")
    by_variant["se"][:] = np.zeros((total,), dtype="float16")


def build_hybrid_store(
    root: Path,
    *,
    variants: list[CanonicalVariant],
    counts: list[int],
    dense_rows: list[int] | None = None,
    reference_alids: list[str] | None = None,
    n_analyses: int = 3,
    provenance: dict[str, object] | None = None,
    store_id: str = STORE_ID,
    layout: str = "hybrid",
    dense_to_shared: list[int] | None = None,
    rsids: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    """Build one Hybrid Store Release plus the reference it was built against.

    Returns `(store_path, reference_path)`. `dense_rows` (on by default: the
    first two rows) are the shared rows the Dense Component covers, and the
    reference defaults to exactly their ALIDs -- the production relation.
    `dense_to_shared` overrides the written mapping without changing which rows
    are treated as on-panel, so an inconsistent mapping can be injected.
    """
    if len(counts) != len(variants):
        raise ValueError("counts must be one per variant")
    if dense_rows is None:
        dense_rows = list(range(min(2, len(variants))))
    for row in dense_rows:
        if row < 0 or row >= len(variants):
            raise ValueError(f"dense row {row} is outside the axis")

    store = root / store_id / "store.opengwasdb"
    store.mkdir(parents=True)
    rsids = rsids or {}
    write_variant_axis(store, variants, rsids)

    reference = root / f"{store_id}-reference-EUR-variants.txt.gz"
    if reference_alids is None:
        reference_alids = [variants[row].alid for row in dense_rows]
    write_reference(reference, reference_alids)

    dense_dir = store / "dense"
    dense_dir.mkdir()
    write_variant_axis(dense_dir, [variants[row] for row in dense_rows], rsids)
    mapping = dense_to_shared if dense_to_shared is not None else dense_rows
    np.save(dense_dir / "dense_to_shared.npy", np.asarray(mapping, dtype="int32"))

    write_ragged(store, counts, n_analyses)
    write_analyses(store, n_analyses, store_id=store_id)
    write_build_record(root, store_id=store_id)
    if provenance is None:
        provenance = hybrid_provenance(
            reference,
            n_variants=len(variants),
            n_analyses=n_analyses,
            n_panel=len(dense_rows),
            n_overflow=int(sum(counts)),
        )
    write_manifest(store, provenance, store_id=store_id, layout=layout)
    return store, reference


def tree_digests(root: Path) -> dict[str, tuple[int, int, str]]:
    """`{relative path: (size, mtime_ns, sha256)}` for every file under `root`."""
    import hashlib

    out: dict[str, tuple[int, int, str]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        stat = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        out[str(path.relative_to(root))] = (stat.st_size, stat.st_mtime_ns, digest)
    return out


class AuditTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ogstores_audit_"))
        self.evidence = self.tmp / "evidence"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers ----------------------------------------------------------

    def run_audit(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = audit.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def reconcile(self, store: Path, *extra: str, output: str = "reconciliation.json") -> tuple[int, dict]:
        target = self.evidence / output
        code, out, err = self.run_audit("reconcile", str(store), "--output", str(target), *extra)
        report = json.loads(target.read_text(encoding="utf-8")) if target.is_file() else {}
        self.assertTrue(report or code != 0, out + err)
        return code, report

    def default_store(self, **kwargs: object) -> tuple[Path, Path]:
        """Six variants, two on panel, no unobserved off-panel entry.

        The two Dense-axis rows carry no Overflow association -- that is the
        disjoint partition, not an absence -- and the four off-panel rows carry
        5, 3, 7 and 1, so nothing off-panel is unobserved here.
        """
        variants = axis_variants(6)
        counts = [0, 0, 5, 3, 7, 1]
        return build_hybrid_store(
            self.tmp,
            variants=variants,
            counts=counts,
            **kwargs,  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Seam 1: the CLI entry point and the report it writes
# ---------------------------------------------------------------------------


class TestReconcileReport(AuditTestCase):
    """`ogstores.audit reconcile <store> --output <json>` writes the report."""

    def test_reconcile_writes_the_documented_report(self) -> None:
        store, reference = self.default_store()
        code, report = self.reconcile(store)
        record_check()

        self.assertEqual(code, 0, json.dumps(report.get("inconsistencies"), indent=1))
        self.assertEqual(report["schema"], audit.REPORT_SCHEMA)
        self.assertEqual(report["audit_version"], audit.AUDIT_VERSION)
        for key in (
            "schema",
            "audit_version",
            "argv",
            "started_at",
            "finished_at",
            "wall_seconds",
            "runtime",
            "store",
            "reference",
            "axis",
            "occupancy",
            "components",
            "identities",
            "inconsistencies",
            "unchanged",
            "resources",
            "scope",
        ):
            self.assertIn(key, report, f"{key} missing from the report")
        record_check()

        # What the Store records, and what this audit opened.
        self.assertEqual(report["store"]["opened_mode"], "r")
        self.assertEqual(report["store"]["recorded"]["n_variants"], 6)
        self.assertEqual(report["store"]["recorded"]["n_panel"], 2)
        self.assertEqual(report["store"]["recorded"]["n_off_panel"], 4)
        self.assertEqual(report["store"]["recorded"]["n_overflow_associations"], 16)
        record_check()

        # The three partition counts the epic asks to reconcile.
        self.assertEqual(report["axis"]["n_axis"], 6)
        self.assertEqual(report["axis"]["n_dense"], 2)
        self.assertEqual(report["axis"]["n_off_panel"], 4)
        self.assertTrue(report["axis"]["reconciled"])
        record_check()

        # Occupancy: every off-panel entry carries at least one association, and
        # the only axis entries with none are the two the Dense Component holds.
        self.assertEqual(report["occupancy"]["axis"]["associations"], 16)
        self.assertEqual(report["occupancy"]["axis"]["observed_variants"], 4)
        self.assertEqual(report["occupancy"]["axis"]["unobserved_variants"], 2)
        self.assertEqual(report["occupancy"]["axis"]["single_association_variants"], 1)
        self.assertEqual(report["occupancy"]["axis"]["max_associations_per_variant"], 7)
        self.assertEqual(report["occupancy"]["off_panel"]["associations"], 16)
        self.assertEqual(report["occupancy"]["off_panel"]["observed_variants"], 4)
        self.assertEqual(report["occupancy"]["off_panel"]["unobserved_variants"], 0)
        self.assertEqual(report["occupancy"]["off_panel"]["unobserved_examples"], [])
        self.assertEqual(report["occupancy"]["dense"]["associations"], 0)
        self.assertEqual(report["occupancy"]["dense"]["unobserved_variants"], 2)
        record_check()

        # The construction reference: 2 ALIDs, both on the axis and both mapped
        # by the Dense Component, and the other 4 rows absent from it.
        self.assertEqual(report["reference"]["path"], str(reference))
        self.assertEqual(report["reference"]["n_alids"], 2)
        self.assertEqual(report["reference"]["axis_rows_in_reference"], 2)
        self.assertEqual(report["reference"]["axis_rows_absent_from_reference"], 4)
        self.assertEqual(report["reference"]["reference_alids_missing_from_axis"], 0)
        self.assertEqual(report["reference"]["dense_mapping_not_in_reference"], 0)
        self.assertEqual(report["reference"]["reference_rows_not_in_dense_mapping"], 0)
        record_check()

        # The variant table: every ALID distinct, one position per row.
        self.assertEqual(report["identities"]["rows_read"], 6)
        self.assertEqual(report["identities"]["distinct_alids"], 6)
        self.assertEqual(report["identities"]["duplicate_alid_extra_rows"], 0)
        self.assertEqual(report["identities"]["multiallelic_sites"], 0)
        self.assertTrue(report["identities"]["variant_index_in_order"])
        record_check()

    def test_reconcile_reports_the_components_it_validated(self) -> None:
        store, _ = self.default_store()
        code, report = self.reconcile(store)
        record_check()

        self.assertEqual(code, 0)
        components = report["components"]
        self.assertEqual(components["n_analyses"], 3)
        self.assertEqual(components["ragged_offsets_last"], 16)
        self.assertEqual(components["by_variant_n_axis"], 6)
        self.assertEqual(components["by_variant_n_rows"], 16)
        self.assertEqual(components["ragged_variant_index_entries"], 16)
        # The engine validated the variant-major index's span, not this module.
        self.assertEqual(components["index_validated_by"], audit.BY_VARIANT_READER)
        record_check()

    def test_reconcile_records_an_audit_summary_line(self) -> None:
        store, _ = self.default_store()
        target = self.evidence / "reconciliation.json"
        code, out, err = self.run_audit("reconcile", str(store), "--output", str(target))
        record_check()

        self.assertEqual(code, 0, err)
        self.assertIn(f"evidence: {target}", out)
        self.assertIn("off-panel 4", out)

    def test_reconcile_band_size_does_not_change_the_answer(self) -> None:
        store, _ = self.default_store()
        _, whole = self.reconcile(store, "--band-variants", "1000", output="whole.json")
        _, bands = self.reconcile(store, "--band-variants", "1", output="bands.json")
        record_check()

        for partition in ("axis", "dense", "off_panel"):
            self.assertEqual(
                whole["occupancy"][partition], bands["occupancy"][partition], partition
            )
        self.assertEqual(bands["resources"]["bands"], 6)


# ---------------------------------------------------------------------------
# Seam 2: unobserved axis entries and the other discrepancies the audit detects
# ---------------------------------------------------------------------------


class TestUnobservedAxisEntries(AuditTestCase):
    """The SciPy-shaped question the epic asks: how much of the axis is unused."""

    def test_unobserved_off_panel_entries_are_counted_and_named(self) -> None:
        variants = axis_variants(8)
        # Rows 1 and 3 are on panel and row 4 is not: the two Dense-axis rows and
        # one off-panel entry have no surviving association.
        counts = [5, 0, 3, 0, 0, 9, 1, 2]
        store, _ = build_hybrid_store(
            self.tmp, variants=variants, counts=counts, dense_rows=[1, 3]
        )
        code, report = self.reconcile(store)
        record_check()

        off_panel = report["occupancy"]["off_panel"]
        self.assertEqual(off_panel["observed_variants"], 5)
        self.assertEqual(off_panel["unobserved_variants"], 1)
        self.assertEqual(off_panel["associations"], 20)
        self.assertEqual(off_panel["unobserved_examples"], [4])
        self.assertEqual(report["occupancy"]["axis"]["unobserved_variants"], 3)
        self.assertEqual(code, 0, "unobserved axis entries are a finding, not a failure")
        record_check()

        kinds = [entry["kind"] for entry in report["inconsistencies"]]
        self.assertIn("unobserved_axis_entries", kinds)
        finding = next(e for e in report["inconsistencies"] if e["kind"] == "unobserved_axis_entries")
        self.assertEqual(finding["severity"], "finding")
        self.assertEqual(finding["counts"]["unobserved_off_panel_variants"], 1)
        self.assertEqual(finding["counts"]["unobserved_dense_variants"], 2)
        self.assertEqual(finding["counts"]["n_off_panel"], 6)
        record_check()

    def test_a_fully_observed_off_panel_axis_is_reported_as_such(self) -> None:
        variants = axis_variants(5)
        # Dense-axis rows are unobserved in the Overflow by the disjoint
        # partition; that is not one of the findings the epic asks about.
        store, _ = build_hybrid_store(
            self.tmp, variants=variants, counts=[0, 0, 2, 3, 4], dense_rows=[0, 1]
        )
        _, report = self.reconcile(store)
        record_check()

        self.assertEqual(report["occupancy"]["off_panel"]["unobserved_variants"], 0)
        self.assertEqual(report["occupancy"]["axis"]["unobserved_variants"], 2)
        self.assertNotIn(
            "unobserved_axis_entries", [entry["kind"] for entry in report["inconsistencies"]]
        )
        self.assertTrue(report["axis"]["reconciled"])

    def test_a_dense_axis_row_with_observed_associations_is_a_partition_error(self) -> None:
        variants = axis_variants(4)
        store, _ = build_hybrid_store(
            self.tmp, variants=variants, counts=[0, 3, 2, 1], dense_rows=[0, 1]
        )
        code, report = self.reconcile(store)
        record_check()

        self.assertEqual(code, 1)
        self.assertTrue(
            any(
                entry["kind"] == "dense_axis_rows_carry_overflow_associations"
                for entry in report["inconsistencies"]
            ),
            json.dumps(report["inconsistencies"], indent=1),
        )

    def test_duplicate_alids_are_counted_exactly(self) -> None:
        variants = axis_variants(4)
        # Two rows with the same canonical identity: the same variant twice.
        variants = [variants[0], variants[1], variants[1], variants[2]]
        store, _ = build_hybrid_store(self.tmp, variants=variants, counts=[0, 2, 3, 4], dense_rows=[0])
        code, report = self.reconcile(store)
        record_check()

        self.assertEqual(report["identities"]["duplicate_alid_groups"], 1)
        self.assertEqual(report["identities"]["duplicate_alid_extra_rows"], 1)
        self.assertEqual(report["identities"]["distinct_alids"], 3)
        self.assertEqual(code, 1, "a duplicated canonical identity must fail the audit")
        self.assertIn(
            "duplicate_alid", [entry["kind"] for entry in report["inconsistencies"]]
        )

    def test_multiallelic_sites_are_counted_without_calling_them_duplicates(self) -> None:
        variants = [
            CanonicalVariant("1", 100, "A", "G"),
            CanonicalVariant("1", 100, "A", "T"),  # shares an allele: multiallelic
            CanonicalVariant("1", 200, "C", "G"),
            CanonicalVariant("1", 200, "A", "T"),  # disjoint alleles: not multiallelic
        ]
        store, _ = build_hybrid_store(self.tmp, variants=variants, counts=[0, 1, 1, 1], dense_rows=[0])
        _, report = self.reconcile(store)
        record_check()

        self.assertEqual(report["identities"]["duplicate_alid_extra_rows"], 0)
        self.assertEqual(report["identities"]["positions_with_multiple_rows"], 2)
        self.assertEqual(report["identities"]["multiallelic_sites"], 1)
        self.assertEqual(report["identities"]["multiallelic_rows"], 2)


class TestReconciledAgainstWhatTheStoreRecords(AuditTestCase):
    """A Store whose recorded counts its own arrays contradict fails the audit."""

    def test_a_recorded_variant_count_the_axis_contradicts_is_an_error(self) -> None:
        variants = axis_variants(5)
        provenance = hybrid_provenance(
            self.tmp / f"{STORE_ID}-reference-EUR-variants.txt.gz",
            n_variants=99,
            n_analyses=2,
            n_panel=2,
            n_overflow=10,
        )
        store, _ = build_hybrid_store(
            self.tmp,
            variants=variants,
            counts=[1, 2, 3, 4, 5],
            dense_rows=[0, 1],
            provenance=provenance,
        )
        code, report = self.reconcile(store)
        record_check()

        self.assertEqual(code, 1)
        kinds = [entry["kind"] for entry in report["inconsistencies"]]
        self.assertIn("recorded_axis_length_disagrees", kinds)
        self.assertIn("recorded_off_panel_disagrees", kinds)
        self.assertFalse(report["axis"]["reconciled"])

    def test_an_inconsistent_dense_mapping_is_named(self) -> None:
        variants = axis_variants(5)
        store, reference = build_hybrid_store(
            self.tmp,
            variants=variants,
            counts=[1, 2, 3, 4, 5],
            dense_rows=[0, 1, 2],
            dense_to_shared=[0, 1, 99],
        )
        code, report = self.reconcile(store)
        record_check()

        self.assertEqual(code, 1)
        mapping = report["axis"]["dense_to_shared"]
        self.assertEqual(mapping["out_of_range"], 1)
        kinds = [entry["kind"] for entry in report["inconsistencies"]]
        self.assertIn("dense_mapping_out_of_range", kinds)
        self.assertEqual(report["reference"]["path"], str(reference))

    def test_a_dense_mapping_that_names_another_reference_row_is_named(self) -> None:
        variants = axis_variants(6)
        # The reference is rows 0-2; the Dense Component maps row 5 instead of 2.
        store, _ = build_hybrid_store(
            self.tmp,
            variants=variants,
            counts=[1, 1, 1, 1, 1, 1],
            dense_rows=[0, 1, 5],
            reference_alids=[variants[0].alid, variants[1].alid, variants[2].alid],
        )
        code, report = self.reconcile(store)
        record_check()

        self.assertEqual(report["reference"]["dense_mapping_not_in_reference"], 1)
        self.assertEqual(report["reference"]["reference_rows_not_in_dense_mapping"], 1)
        kinds = [entry["kind"] for entry in report["inconsistencies"]]
        self.assertIn("panel_partition_disagrees_with_reference", kinds)
        self.assertEqual(code, 1)

    def test_a_recorded_analysis_count_the_component_contradicts_is_an_error(self) -> None:
        variants = axis_variants(4)
        provenance = hybrid_provenance(
            self.tmp / f"{STORE_ID}-reference-EUR-variants.txt.gz",
            n_variants=4,
            n_analyses=7,
            n_panel=1,
            n_overflow=6,
        )
        store, _ = build_hybrid_store(
            self.tmp,
            variants=variants,
            counts=[0, 1, 2, 3],
            dense_rows=[0],
            n_analyses=3,
            provenance=provenance,
        )
        code, report = self.reconcile(store)
        record_check()

        self.assertEqual(report["components"]["n_analyses"], 3)
        self.assertEqual(report["components"]["n_analyses_recorded"], 7)
        self.assertEqual(code, 1)
        self.assertIn(
            "recorded_analysis_count_disagrees",
            [entry["kind"] for entry in report["inconsistencies"]],
        )

    def test_an_analyses_tsv_that_disagrees_with_the_component_is_an_error(self) -> None:
        variants = axis_variants(4)
        store, _ = build_hybrid_store(
            self.tmp, variants=variants, counts=[0, 1, 2, 3], dense_rows=[0], n_analyses=3
        )
        # The membership table is the axis's other account of how many Analyses
        # the release holds; a ragged component that lost one must not pass.
        (store / "analyses.tsv").write_text(
            "analysis_id\nA\nB\n", encoding="utf-8"
        )
        code, report = self.reconcile(store)
        record_check()

        self.assertEqual(report["store"]["analyses_tsv_rows"], 2)
        self.assertEqual(report["components"]["n_analyses"], 3)
        self.assertEqual(code, 1)
        self.assertIn(
            "analyses_tsv_disagrees_with_component",
            [entry["kind"] for entry in report["inconsistencies"]],
        )

    def test_a_missing_reference_is_a_visible_failure(self) -> None:
        store, reference = self.default_store()
        reference.unlink()
        code, report = self.reconcile(store)
        record_check()

        self.assertEqual(code, 1, "an unavailable required input must not pass")
        self.assertFalse(report["reference"]["available"])
        kinds = [entry["kind"] for entry in report["inconsistencies"]]
        self.assertIn("reference_unavailable", kinds)

    def test_skipping_the_reference_is_an_explicit_choice_not_a_pass(self) -> None:
        store, _ = self.default_store()
        code, report = self.reconcile(store, "--skip-reference")
        record_check()

        # The command may be asked to skip a stage, but it may never then report
        # closure: `ok` is false, the stage is named as skipped, and the run
        # exits non-zero.
        self.assertEqual(code, 1)
        self.assertFalse(report["ok"])
        self.assertFalse(report["reference"]["available"])
        self.assertTrue(report["reference"]["skipped"])
        self.assertEqual(report["reference"]["n_alids"], None)
        self.assertIn("skipped_evidence", [e["kind"] for e in report["inconsistencies"]])
        skipped = [entry["item"] for entry in report["scope"]["not_measured"]]
        self.assertIn("construction reference partition", skipped)
        self.assertNotIn(
            "construction-reference identity and the Dense Component's membership of it",
            report["scope"]["measured"],
        )

    def test_asking_for_both_a_reference_and_no_reference_is_refused(self) -> None:
        store, reference = self.default_store()
        target = self.evidence / "both.json"
        code, out, err = self.run_audit(
            "reconcile",
            str(store),
            "--reference",
            str(reference),
            "--skip-reference",
            "--output",
            str(target),
        )
        record_check()

        self.assertEqual(code, 2, out + err)
        self.assertIn("--skip-reference", err)
        self.assertFalse(target.exists())

    def test_an_explicit_reference_replaces_the_one_the_store_records(self) -> None:
        store, recorded = self.default_store()
        other = self.tmp / "other-reference.txt.gz"
        write_reference(other, ["1:100:A:G", "1:999999:C:T"])
        code, report = self.reconcile(store, "--reference", str(other))
        record_check()

        self.assertEqual(report["reference"]["path"], str(other))
        self.assertEqual(report["reference"]["n_alids"], 2)
        self.assertEqual(report["reference"]["axis_rows_in_reference"], 1)
        self.assertEqual(report["reference"]["reference_alids_missing_from_axis"], 1)
        self.assertEqual(report["reference"]["path"] != str(recorded), True)
        self.assertEqual(code, 1)

    def test_the_identity_pass_can_be_skipped_and_says_so(self) -> None:
        store, _ = self.default_store()
        code, report = self.reconcile(store, "--no-identities")
        record_check()

        self.assertEqual(code, 1)
        self.assertFalse(report["ok"])
        self.assertFalse(report["identities"]["scanned"])
        self.assertIsNone(report["reference"]["axis_rows_in_reference"])
        self.assertIsNone(report["reference"]["dense_mapping_not_in_reference"])
        self.assertIn("skipped_evidence", [e["kind"] for e in report["inconsistencies"]])
        self.assertIn(
            "canonical-identity scan of the variant table",
            [entry["item"] for entry in report["scope"]["not_measured"]],
        )

    def test_a_band_size_of_zero_is_refused_before_any_work(self) -> None:
        store, _ = self.default_store()
        target = self.evidence / "zero.json"
        code, _, err = self.run_audit(
            "reconcile", str(store), "--band-variants", "0", "--output", str(target)
        )
        record_check()

        self.assertEqual(code, 2)
        self.assertIn("--band-variants", err)
        self.assertFalse(target.exists())


class TestFailLoudlyOnMissingOrContradictedEvidence(AuditTestCase):
    """A Store whose own account is unreadable or contradicted never passes.

    Every case here is a Store Release whose artifact looks whole and whose
    evidence is not: a missing build record, a missing membership table, an
    incomplete provenance block, an ALID index that contradicts the table, or a
    row whose columns contradict its own canonical identity. Each must be an
    `error` (exit 1), because a passing report about one of these is exactly the
    wrong answer that looks like a right one.
    """

    def test_a_missing_build_record_is_a_visible_failure(self) -> None:
        store, _ = self.default_store()
        (self.tmp / STORE_ID / "records" / "build.json").unlink()
        code, report = self.reconcile(store)
        record_check()

        self.assertEqual(code, 1)
        self.assertFalse(report["ok"])
        self.assertIn("build_record_unavailable", [e["kind"] for e in report["inconsistencies"]])

    def test_a_corrupt_build_record_is_a_visible_failure(self) -> None:
        store, _ = self.default_store()
        (self.tmp / STORE_ID / "records" / "build.json").write_text("{not json", encoding="utf-8")
        code, report = self.reconcile(store)
        record_check()

        self.assertEqual(code, 1)
        self.assertIn("build_record_unreadable", [e["kind"] for e in report["inconsistencies"]])

    def test_a_build_record_without_a_revision_is_a_visible_failure(self) -> None:
        store, _ = self.default_store()
        record = self.tmp / STORE_ID / "records" / "build.json"
        data = json.loads(record.read_text(encoding="utf-8"))
        data.pop("opengwasdb_rev")
        record.write_text(json.dumps(data), encoding="utf-8")
        code, report = self.reconcile(store)
        record_check()

        self.assertEqual(code, 1)
        self.assertIn(
            "build_revision_unrecorded", [e["kind"] for e in report["inconsistencies"]]
        )

    def test_a_missing_analyses_tsv_is_a_visible_failure(self) -> None:
        store, _ = self.default_store()
        (store / "analyses.tsv").unlink()
        code, report = self.reconcile(store)
        record_check()

        self.assertEqual(code, 1)
        self.assertIsNone(report["store"]["analyses_tsv_rows"])
        self.assertIn("analyses_tsv_unavailable", [e["kind"] for e in report["inconsistencies"]])

    def test_an_unreadable_analyses_tsv_is_a_visible_failure(self) -> None:
        store, _ = self.default_store()
        (store / "analyses.tsv").write_bytes(b"analysis_id\n\xff\xfe\n")
        code, report = self.reconcile(store)
        record_check()

        self.assertEqual(code, 1)
        self.assertIn("analyses_tsv_unavailable", [e["kind"] for e in report["inconsistencies"]])

    def test_recorded_counts_the_store_does_not_claim_are_a_visible_failure(self) -> None:
        variants = axis_variants(5)
        # A provenance block that records the axis but says nothing about the
        # Dense Component or the Overflow: the audit has nothing to reconcile
        # those against, which is a failure of the Store's own evidence.
        provenance = {
            "builder": "fixture.hybrid",
            "variant_reference": str(self.tmp / f"{STORE_ID}-reference-EUR-variants.txt.gz"),
            "n_variants": 5,
        }
        store, _ = build_hybrid_store(
            self.tmp,
            variants=variants,
            counts=[0, 1, 2, 3, 4],
            dense_rows=[0],
            provenance=provenance,
        )
        code, report = self.reconcile(store)
        record_check()

        self.assertEqual(code, 1)
        self.assertIn(
            "recorded_counts_incomplete", [e["kind"] for e in report["inconsistencies"]]
        )
        incomplete = next(
            e for e in report["inconsistencies"] if e["kind"] == "recorded_counts_incomplete"
        )
        self.assertIn("n_panel", incomplete["counts"]["missing"])
        self.assertIn("n_overflow_associations", incomplete["counts"]["missing"])

    def test_an_unsorted_alid_index_is_a_visible_failure(self) -> None:
        store, _ = self.default_store()
        bytes_path = store / "variant_alid_bytes.npy"
        rows_path = store / "variant_alid_rows.npy"
        np.save(bytes_path, np.load(bytes_path)[::-1])
        np.save(rows_path, np.load(rows_path)[::-1])
        code, report = self.reconcile(store)
        record_check()

        self.assertEqual(code, 1)
        self.assertFalse(report["identities"]["alid_index"]["sorted"])
        self.assertIn("alid_index_not_sorted", [e["kind"] for e in report["inconsistencies"]])

    def test_an_alid_index_that_lost_rows_is_a_visible_failure(self) -> None:
        store, _ = self.default_store()
        bytes_path = store / "variant_alid_bytes.npy"
        rows_path = store / "variant_alid_rows.npy"
        np.save(bytes_path, np.load(bytes_path)[:-1])
        np.save(rows_path, np.load(rows_path)[:-1])
        code, report = self.reconcile(store)
        record_check()

        self.assertEqual(code, 1)
        self.assertFalse(report["identities"]["alid_index"]["agrees_with_table"])
        self.assertIn(
            "alid_index_disagrees_with_table", [e["kind"] for e in report["inconsistencies"]]
        )

    def test_a_half_written_alid_index_is_a_visible_failure(self) -> None:
        store, _ = self.default_store()
        rows_path = store / "variant_alid_rows.npy"
        np.save(rows_path, np.load(rows_path)[:-1])
        code, report = self.reconcile(store)
        record_check()

        self.assertEqual(code, 1, "a half-written index must not pass as unauditable")
        self.assertIn("alid_index_half_written", [e["kind"] for e in report["inconsistencies"]])

    def test_a_row_whose_columns_contradict_its_alid_is_a_visible_failure(self) -> None:
        store, _ = self.default_store()
        # Row 1 is the first row the sampled cross-check reads; moving its
        # position leaves the axis ordered and its ALID intact.
        rewrite_position(store, 0, 105)
        code, report = self.reconcile(store)
        record_check()

        self.assertEqual(code, 1)
        self.assertEqual(report["identities"]["alid_consistency_sample"]["mismatches"], 1)
        self.assertIn("alid_sample_mismatch", [e["kind"] for e in report["inconsistencies"]])


class TestStableReadOnlyView(AuditTestCase):
    """The Store a report describes is one directory, not a moving target."""

    def test_the_report_records_the_directory_identity(self) -> None:
        store, _ = self.default_store()
        _, report = self.reconcile(store)
        record_check()

        stat = store.stat()
        view = report["store"]["stable_view"]
        self.assertEqual(view["dev"], stat.st_dev)
        self.assertEqual(view["ino"], stat.st_ino)
        self.assertEqual(view["parent_dev"], store.parent.stat().st_dev)
        self.assertEqual(view["parent_ino"], store.parent.stat().st_ino)
        self.assertFalse(view["swapped"])
        self.assertIsNone(view["release_lock"]["held_by"])

    def test_a_held_release_lock_is_recorded_and_the_view_is_still_verified(self) -> None:
        store, _ = self.default_store()
        with hold_release_lock(self.tmp) as lock_path:
            before = tree_digests(store)
            code, report = self.reconcile(store)
            after = tree_digests(store)
        record_check()

        self.assertEqual(code, 0, "a held lock is a stability finding, not a failed audit")
        self.assertIsNotNone(report["store"]["stable_view"]["release_lock"]["held_by"])
        self.assertEqual(report["store"]["stable_view"]["release_lock"]["path"], str(lock_path))
        self.assertIn("release_lock_held", [e["kind"] for e in report["inconsistencies"]])
        self.assertFalse(report["store"]["stable_view"]["swapped"])
        self.assertEqual(before, after, "probing a lock must not change the Store")
        self.assertTrue(lock_path.is_file(), "the audit reads a lock, never removes it")


class TestTheEvidenceFileIsWrittenSafely(AuditTestCase):
    def test_a_symlink_at_the_output_path_is_replaced_not_followed(self) -> None:
        store, _ = self.default_store()
        victim = self.tmp / "victim.txt"
        victim.write_text("do not truncate me\n", encoding="utf-8")
        self.evidence.mkdir(parents=True, exist_ok=True)
        target = self.evidence / "reconciliation.json"
        target.symlink_to(victim)
        code, out, err = self.run_audit("reconcile", str(store), "--output", str(target))
        record_check()

        self.assertEqual(code, 0, out + err)
        self.assertEqual(victim.read_text(encoding="utf-8"), "do not truncate me\n")
        self.assertFalse(target.is_symlink())
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["schema"], audit.REPORT_SCHEMA)

    def test_no_temporary_file_is_left_beside_the_evidence(self) -> None:
        store, _ = self.default_store()
        self.reconcile(store)
        code, _ = self.reconcile(store, output="second.json")
        record_check()

        self.assertEqual(code, 0)
        names = sorted(path.name for path in self.evidence.iterdir())
        self.assertEqual(names, ["reconciliation.json", "second.json"])

    def test_an_existing_evidence_file_is_replaced_atomically(self) -> None:
        store, _ = self.default_store()
        (self.evidence).mkdir(parents=True, exist_ok=True)
        target = self.evidence / "reconciliation.json"
        target.write_text("stale\n", encoding="utf-8")
        code, _ = self.reconcile(store)
        record_check()

        self.assertEqual(code, 0)
        self.assertNotIn("stale", target.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Seam 3: the audit is read-only, and leaves the Store byte-identical
# ---------------------------------------------------------------------------

class TestTheStoreIsUnchanged(AuditTestCase):
    def test_a_full_audit_leaves_every_store_file_byte_identical(self) -> None:
        store, _ = self.default_store()
        before = tree_digests(store)
        code, report = self.reconcile(store)
        after = tree_digests(store)
        record_check()

        self.assertEqual(code, 0)
        self.assertEqual(before, after)
        self.assertEqual(report["unchanged"]["method"], "sha256")
        self.assertEqual(report["unchanged"]["changed"], [])
        self.assertEqual(report["unchanged"]["added"], [])
        self.assertEqual(report["unchanged"]["removed"], [])
        self.assertEqual(report["unchanged"]["files"], len(before))
        record_check()

    def test_a_large_store_is_verified_by_size_and_mtime(self) -> None:
        store, _ = self.default_store()
        _, report = self.reconcile(store, "--verify", "metadata")
        record_check()

        self.assertEqual(report["unchanged"]["method"], "size+mtime_ns")
        self.assertEqual(report["unchanged"]["changed"], [])

    def test_the_audit_refuses_to_write_its_evidence_inside_the_store(self) -> None:
        store, _ = self.default_store()
        before = tree_digests(store)
        target = store / "reconciliation.json"
        code, out, err = self.run_audit("reconcile", str(store), "--output", str(target))
        record_check()

        self.assertEqual(code, 2)
        self.assertIn("inside the Store", err)
        self.assertFalse(target.exists())
        self.assertEqual(tree_digests(store), before)

    def test_an_unusable_input_fails_visibly_instead_of_reporting_success(self) -> None:
        empty = self.tmp / "not-a-store"
        empty.mkdir()
        code, _, err = self.run_audit(
            "reconcile", str(empty), "--output", str(self.evidence / "x.json")
        )
        record_check()

        self.assertEqual(code, 2)
        self.assertIn("manifest.json", err)
        self.assertFalse((self.evidence / "x.json").exists())

    def test_a_non_hybrid_store_is_refused_by_name(self) -> None:
        variants = axis_variants(3)
        store, _ = build_hybrid_store(
            self.tmp, variants=variants, counts=[1, 1, 1], dense_rows=[0], layout="ragged"
        )
        code, _, err = self.run_audit(
            "reconcile", str(store), "--output", str(self.evidence / "y.json")
        )
        record_check()

        self.assertEqual(code, 2)
        self.assertIn("hybrid", err)


# ---------------------------------------------------------------------------
# The production Store, optionally: the same command, the immutable real input
# ---------------------------------------------------------------------------


PRODUCTION_STORE = Path(
    os.environ.get(
        "OGSTORES_AUDIT_PRODUCTION_STORE",
        "/data/opengwasdb/stores/OGS-00011/store.opengwasdb",
    )
)


@unittest.skipUnless(
    os.environ.get("OGSTORES_AUDIT_PRODUCTION_STORE"),
    "set OGSTORES_AUDIT_PRODUCTION_STORE to audit the production Store (minutes, 10 GB)",
)
class TestTheProductionStoreIsUnchanged(unittest.TestCase):
    def test_the_production_store_is_byte_identical_after_a_full_audit(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="ogstores_audit_production_"))
        try:
            before = tree_digests(PRODUCTION_STORE)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = audit.main(
                    [
                        "reconcile",
                        str(PRODUCTION_STORE),
                        "--output",
                        str(tmp / "reconciliation.json"),
                    ]
                )
            after = tree_digests(PRODUCTION_STORE)
            report = json.loads((tmp / "reconciliation.json").read_text(encoding="utf-8"))
            record_check()
            self.assertIn(code, (0, 1), out.getvalue() + err.getvalue())
            self.assertEqual(before, after)
            self.assertEqual(report["unchanged"]["changed"], [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    suite = unittest.TestSuite()
    for case in (
        TestReconcileReport,
        TestUnobservedAxisEntries,
        TestReconciledAgainstWhatTheStoreRecords,
        TestFailLoudlyOnMissingOrContradictedEvidence,
        TestStableReadOnlyView,
        TestTheEvidenceFileIsWrittenSafely,
        TestTheStoreIsUnchanged,
        TestTheProductionStoreIsUnchanged,
    ):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(case))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful():
        sys.exit(1)
    print(f"ALL {n_checks} CHECKS PASSED")


if __name__ == "__main__":
    main()
