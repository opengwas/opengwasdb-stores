#!/usr/bin/env python3
"""Read-only Store Audit: reconcile a built Store Release against its own record.

Stores epic #371 asks a question no configured threshold can answer: a Hybrid
Store Release reports 101,098,177 off-panel axis entries, and the operator needs
the built artifact reconciled -- not the Build Recipe restated -- before any of
it can be attributed to a cause. This module is that reconciliation, as the one
command epic #371 names as its public seam:

    python3 -m ogstores.audit reconcile <store_path> --output <evidence.json>
    pixi run audit-store reconcile /data/opengwasdb/stores/OGS-00011/store.opengwasdb

What it reconciles, exactly, and from where:

* the shared variant axis, its Dense Component partition and the off-panel
  remainder, from `dense/dense_to_shared.npy` and the Store Variant Table;
* **observed occupancy**: how many axis entries carry at least one surviving
  observed association, and how many carry none, from the variant-major CSR
  offsets `data.zarr/ragged/by_variant/offsets` (ADR 0060) read in bands, so
  the working set is one band of `int64` offsets whatever the axis length;
* canonical identity: exact duplicate ALIDs and multiallelic sites in
  `variants.tsv.gz`, from the table itself plus the Store's own sorted ALID
  search index (which is what makes the duplicate count exact rather than
  sampled);
* the construction variant reference the build recorded, by identity (path,
  size, mtime, sha256, ALID count) and by membership: which axis rows it
  contains, and whether the Dense Component's partition is exactly that set.

Read-only is a property of this module, not a convention its callers keep: the
release is opened through `opengwasdb.store.open.open_store` and its arrays with
`arrays(mode="r")`, every array is loaded `mmap_mode="r"`, nothing is written
anywhere but the `--output` path, and that path may not be inside the Store.
Before and after, every file's size and mtime -- and its sha256, unless the
Store is too large for that to be affordable -- is recorded and compared, so a
report that claims "unchanged" carries the evidence for it.

The report separates two kinds of disagreement. `severity: "error"` means the
Store's own artifact and its own record do not describe the same thing, or a
required input was unreadable: the command exits 1 and nothing about it may be
read as a pass. `severity: "finding"` is an observation the audit is *for*,
such as axis entries with no surviving association; it exits 0.

Nothing here admits, filters or repairs. The vocabulary is the epic's --
Observed-Only, off-panel axis entry, observed occupancy -- and the decisions
behind the format it reads are ADR 0033 (MAF-filtered shared Stores), ADR 0037
(completion replaces a Store under its own id) and ADR 0060 (the variant-major
index), all in the sibling `opengwasdb` repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from opengwasdb.layouts.hybrid.layout import dense_component_path, dense_to_shared_path
from opengwasdb.layouts.ragged.by_variant import ByVariantReader
from opengwasdb.store.arrays import array_length
from opengwasdb.store.open import open_store
from opengwasdb.variants import iter_variant_records, read_variant_reference
from opengwasdb.variants.axis import (
    VariantAxis,
    variant_alid_bytes_path,
    variant_alid_rows_path,
    variant_table_path,
)

#: The report's contract version. Bumped when a field changes meaning; the
#: schema string is what a consumer matches, so adding a field does not bump it.
AUDIT_VERSION = "1"
REPORT_SCHEMA = "ogstores.audit.reconcile/1"

#: The engine interface that validated the variant-major index, recorded so a
#: reader knows the span/staleness check was not re-implemented here (ADR 0012).
BY_VARIANT_READER = "opengwasdb.layouts.ragged.by_variant.ByVariantReader"

#: Axis entries one band of the occupancy scan spans. The offsets are `int64`,
#: so a band is 32 MB at 4M; nothing about the answer depends on it (the suite
#: pins that by running the same fixture at one entry per band).
DEFAULT_BAND_VARIANTS = 4_000_000

#: Entries of the sorted ALID search index one comparison block spans. The
#: index is `|S32`, so this is 128 MB read and a 4 MB boolean temporary.
DEFAULT_ALID_BLOCK = 1 << 22

#: The width `opengwasdb` indexes an ALID at. A row whose ALID is wider is left
#: out of the search index (issue #127 there), so the duplicate scan has to
#: treat those rows separately and say so.
ALID_INDEX_WIDTH = 32

#: How much Store a sha256-before/after check may read. Above it the audit falls
#: back to size and mtime, which still catches a write, and says which it used.
HASH_VERIFY_LIMIT_BYTES = 512 * 1024 * 1024

#: Axis entries the report names as examples rather than listing every one.
EXAMPLE_LIMIT = 20

#: One in this many rows is fully re-derived from its ALID (`chrom:pos:a1:a2`)
#: as a cross-check on the columns: a census of that is a second full parse.
ALID_SAMPLE_STRIDE = 100_000

#: Support-histogram buckets, in associations per variant (`RANGE`), plus the
#: tail above the last bucket's upper bound.
SUPPORT_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("1", 1, 1),
    ("2", 2, 2),
    ("3-5", 3, 5),
    ("6-10", 6, 10),
    ("11-50", 11, 50),
    ("51-100", 51, 100),
    ("101-1000", 101, 1000),
    ("1001+", 1001, None),
)

#: What this slice deliberately does not measure. Absence is not zero, and the
#: epic's later tickets own these, so the report names them instead of leaving
#: a reader to infer completeness from a report that has no field for them.
NOT_MEASURED: tuple[dict[str, str], ...] = (
    {
        "item": "per-Analysis and per-publication attribution of off-panel associations",
        "why": "this slice reconciles the axis, not its contributors (stores #373)",
        "ticket": "#373",
    },
    {
        "item": "association-level support counts across Analyses",
        "why": "support is reported here per variant, from the CSR offsets only",
        "ticket": "#373",
    },
    {
        "item": "source frequency evidence (raw EAF against the 0.005-0.995 interval)",
        "why": "needs the source rows, not the Store (stores #374)",
        "ticket": "#374",
    },
    {
        "item": "duplicate rsid groups naming several ALIDs",
        "why": (
            "needs a global 114M-key index; the Store's rsid index is sorted by "
            "rsid but its rows are not grouped, so a group is not a contiguous run"
        ),
        "ticket": "#375",
    },
)


class AuditError(RuntimeError):
    """The Store cannot be audited at all: the required structures are absent.

    Raised only where continuing would produce a report about something other
    than the Store the caller named -- a missing manifest, a Hybrid artifact
    without its Dense Component mapping, an unreadable axis. Value-level
    disagreements the audit *can* describe are inconsistencies in the report
    instead, so "reconciled and disagreed" stays distinct from "could not
    reconcile", and the two exit differently (1 and 2).
    """


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Occupancy: banded scan of the variant-major CSR offsets
# ---------------------------------------------------------------------------


@dataclass
class _Totals:
    """Per-partition association counts accumulated one band at a time."""

    associations: int = 0
    observed: int = 0
    unobserved: int = 0
    single: int = 0
    largest: int = 0
    examples: list[int] = field(default_factory=list)

    def add(self, counts: np.ndarray, first_index: int, *, want_examples: bool) -> None:
        positive = counts > 0
        self.associations += int(counts.sum())
        self.observed += int(np.count_nonzero(positive))
        self.unobserved += int(counts.size) - int(np.count_nonzero(positive))
        self.single += int(np.count_nonzero(counts == 1))
        if counts.size:
            largest = int(counts.max())
            if largest > self.largest:
                self.largest = largest
        if want_examples and len(self.examples) < EXAMPLE_LIMIT:
            missing = np.flatnonzero(~positive)[: EXAMPLE_LIMIT - len(self.examples)]
            self.examples.extend(int(first_index) + int(offset) for offset in missing)

    def as_dict(self, *, with_examples: bool) -> dict[str, Any]:
        out: dict[str, Any] = {
            "associations": self.associations,
            "observed_variants": self.observed,
            "unobserved_variants": self.unobserved,
            "single_association_variants": self.single,
            "max_associations_per_variant": self.largest,
        }
        if with_examples:
            out["unobserved_examples"] = self.examples
        return out


def _support_histogram(
    histogram: np.ndarray, tail: int
) -> dict[str, int]:
    out: dict[str, int] = {}
    for label, low, high in SUPPORT_BUCKETS:
        if high is None:
            out[label] = int(histogram[low:].sum()) + tail
        else:
            out[label] = int(histogram[low : high + 1].sum())
    return out


def _scan_occupancy(
    offsets: Any,
    *,
    n_axis: int,
    dense_mask: np.ndarray | None,
    band_variants: int,
) -> dict[str, Any]:
    """Count associations per axis entry from `by_variant/offsets`, band by band.

    One band is `counts = diff(offsets[lo:hi+1])`, so the temporary is one band
    of `int64` values: `n_axis + 1` entries are never materialised at once.
    """
    axis = _Totals()
    dense = _Totals()
    off_panel = _Totals()
    histogram = np.zeros(SUPPORT_BUCKETS[-1][1], dtype="int64")
    tail = 0
    bands = 0
    negatives = 0
    starts_at_zero = True
    for low in range(0, n_axis, band_variants):
        high = min(low + band_variants, n_axis)
        block = np.asarray(offsets[low : high + 1], dtype="int64")
        if block.size != high - low + 1:
            raise AuditError(
                f"by_variant/offsets yielded {block.size} entries for variant band "
                f"[{low}, {high}); the array does not answer for the declared axis"
            )
        if low == 0 and int(block[0]) != 0:
            starts_at_zero = False
        counts = np.diff(block)
        if counts.size and int(counts.min()) < 0:
            negatives += 1
        bands += 1
        axis.add(counts, low, want_examples=False)
        if dense_mask is None:
            off_panel.add(counts, low, want_examples=True)
        else:
            band_mask = dense_mask[low:high]
            dense.add(counts[band_mask], low, want_examples=False)
            if bool(band_mask.any()):
                # The off-panel partition's axis positions, so an example names
                # the variant's own index rather than its position in the band.
                positions = np.flatnonzero(~band_mask) + low
                off_panel_counts = counts[~band_mask]
            else:
                positions = np.arange(low, high, dtype="int64")
                off_panel_counts = counts
            off_panel.add(off_panel_counts, 0, want_examples=False)
            if len(off_panel.examples) < EXAMPLE_LIMIT:
                for offset in np.flatnonzero(off_panel_counts == 0)[
                    : EXAMPLE_LIMIT - len(off_panel.examples)
                ]:
                    off_panel.examples.append(int(positions[offset]))
        capped = counts[counts <= SUPPORT_BUCKETS[-1][1] - 1]
        histogram += np.bincount(capped, minlength=histogram.size)
        tail += int(np.count_nonzero(counts >= SUPPORT_BUCKETS[-1][1]))
    return {
        "axis": axis.as_dict(with_examples=False),
        "dense": dense.as_dict(with_examples=False),
        "off_panel": off_panel.as_dict(with_examples=True),
        "support_histogram": _support_histogram(histogram, tail),
        "bands": bands,
        "bands_with_decreasing_offsets": negatives,
        "offset_starts_at_zero": starts_at_zero,
        "residual": int(dense.associations) + int(off_panel.associations) != axis.associations,
    }


# ---------------------------------------------------------------------------
# Canonical identity: the variant table, and the sorted ALID search index
# ---------------------------------------------------------------------------


@dataclass
class _IdentityScan:
    rows: int = 0
    variant_index_violations: int = 0
    position_order_violations: int = 0
    repeated_contig_blocks: int = 0
    non_canonical_rows: int = 0
    missing_rsid_rows: int = 0
    duplicate_extra_rows_in_table: int = 0
    positions_with_multiple_rows: int = 0
    multiallelic_sites: int = 0
    multiallelic_rows: int = 0
    indexable_rows: int = 0
    over_width_alids: dict[str, int] = field(default_factory=dict)
    reference_rows: np.ndarray | None = None
    reference_rows_seen: int = 0
    sample_checked: int = 0
    sample_mismatches: int = 0


def _scan_variant_table(
    table_path: Path,
    *,
    n_axis: int,
    reference: set[str] | None,
) -> _IdentityScan:
    """Stream the Store Variant Table once, for identity and partition facts.

    Duplicate ALIDs are counted here only where two rows share a position (the
    axis is position-ordered, so equal ALIDs are then adjacent); the exact
    count, which needs no ordering assumption, comes from the sorted ALID
    search index in `_scan_alid_index`. Both are reported.
    """
    scan = _IdentityScan()
    if reference is not None:
        scan.reference_rows = np.zeros(n_axis, dtype=bool)

    last_chromosome: str | None = None
    last_position = -1
    chromosomes_seen: set[str] = set()
    group_chromosome: str | None = None
    group_position: int | None = None
    group_rows = 0
    group_first = ("", "")
    group_pairs: list[tuple[str, str]] | None = None

    for record in iter_variant_records(table_path):
        scan.rows += 1
        index = record.variant_index
        if index != scan.rows - 1:
            scan.variant_index_violations += 1
        alid = record.alid
        effect = record.effect_allele
        other = record.other_allele
        chromosome = record.chromosome
        position = record.position

        if effect >= other:
            scan.non_canonical_rows += 1
        if record.rsid is None:
            scan.missing_rsid_rows += 1
        if len(alid) <= ALID_INDEX_WIDTH:
            scan.indexable_rows += 1
        else:
            scan.over_width_alids[alid] = scan.over_width_alids.get(alid, 0) + 1
        if reference is not None and scan.reference_rows is not None:
            if alid in reference:
                scan.reference_rows_seen += 1
                if 0 <= index < n_axis:
                    scan.reference_rows[index] = True

        if chromosome != last_chromosome:
            if chromosome in chromosomes_seen:
                scan.repeated_contig_blocks += 1
            chromosomes_seen.add(chromosome)
            last_chromosome = chromosome
            last_position = -1
        if position < last_position:
            scan.position_order_violations += 1
        last_position = position

        if chromosome != group_chromosome or position != group_position:
            if group_rows > 1:
                scan.positions_with_multiple_rows += 1
                pairs = group_pairs if group_pairs is not None else [group_first]
                distinct = set(pairs)
                scan.duplicate_extra_rows_in_table += group_rows - len(distinct)
                alleles = [allele for pair in distinct for allele in pair]
                if len(alleles) != len(set(alleles)):
                    scan.multiallelic_sites += 1
                    scan.multiallelic_rows += group_rows
            group_chromosome = chromosome
            group_position = position
            group_first = (effect, other)
            group_pairs = None
            group_rows = 1
        else:
            group_rows += 1
            if group_pairs is None:
                group_pairs = [group_first]
            group_pairs.append((effect, other))

        if scan.rows % ALID_SAMPLE_STRIDE == 1:
            scan.sample_checked += 1
            parts = alid.split(":")
            if (
                len(parts) != 4
                or parts[0] != chromosome
                or parts[1] != str(position)
                or parts[2] != effect
                or parts[3] != other
            ):
                scan.sample_mismatches += 1

    if group_rows > 1:
        scan.positions_with_multiple_rows += 1
        pairs = group_pairs if group_pairs is not None else [group_first]
        distinct = set(pairs)
        scan.duplicate_extra_rows_in_table += group_rows - len(distinct)
        alleles = [allele for pair in distinct for allele in pair]
        if len(alleles) != len(set(alleles)):
            scan.multiallelic_sites += 1
            scan.multiallelic_rows += group_rows
    return scan


@dataclass(frozen=True)
class _AlidIndexScan:
    entries: int
    sorted_ok: bool
    duplicate_groups: int
    duplicate_extra_rows: int


def _scan_alid_index(bytes_path: Path, rows_path: Path, *, block: int = DEFAULT_ALID_BLOCK) -> _AlidIndexScan:
    """Count adjacent equal ALIDs in the Store's sorted ALID search index.

    The index is sorted by ALID (`write_variant_axis`), so two rows with one
    canonical identity are adjacent: a run of `n` equal entries contributes
    `n - 1` extra rows and is one duplicate group. That makes the count exact
    without a dictionary of 114M identities in memory -- but only for the ALIDs
    the index holds, which is why rows wider than the index's fixed width are
    counted separately from the table.
    """
    index = np.load(bytes_path, mmap_mode="r")
    rows = np.load(rows_path, mmap_mode="r")
    entries = int(index.shape[0])
    if int(rows.shape[0]) != entries:
        raise AuditError(
            f"{bytes_path.name} holds {entries} ALIDs but {rows_path.name} holds "
            f"{int(rows.shape[0])} rows; the search index is half written"
        )
    sorted_ok = True
    duplicate_groups = 0
    duplicate_extra_rows = 0
    # Whether the pair that straddles the block boundary (`high - 1`, `high`)
    # is equal, so a run spanning a boundary is one group and not two.
    leading_equal = False
    for low in range(0, entries, block):
        high = min(low + block, entries)
        pairs = np.asarray(index[low : high + 1])
        if pairs.size < 2:
            leading_equal = False
            continue
        left = pairs[:-1]
        right = pairs[1:]
        equal = left == right
        if bool((left > right).any()):
            sorted_ok = False
        duplicate_extra_rows += int(np.count_nonzero(equal))
        previous = np.concatenate(([leading_equal], equal[:-1]))
        duplicate_groups += int(np.count_nonzero(equal & ~previous))
        leading_equal = bool(equal[-1])
    return _AlidIndexScan(
        entries=entries,
        sorted_ok=sorted_ok,
        duplicate_groups=duplicate_groups,
        duplicate_extra_rows=duplicate_extra_rows,
    )


# ---------------------------------------------------------------------------
# The construction variant reference
# ---------------------------------------------------------------------------


def _reference_facts(
    path: Path, *, read_alids: bool
) -> tuple[dict[str, Any], set[str] | None]:
    """The reference's identity, and its ALIDs when the partition needs them."""
    stat = path.stat()
    facts: dict[str, Any] = {
        "path": str(path),
        "role": (
            "construction variant reference (the build's own panel); "
            "not the LD panel a Completion uses (ADR 0037)"
        ),
        "available": True,
        "skipped": False,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": _sha256(path),
        "n_alids": None,
        "n_alids_over_index_width": None,
        "axis_rows_in_reference": None,
        "axis_rows_absent_from_reference": None,
        "reference_alids_missing_from_axis": None,
        "dense_mapping_not_in_reference": None,
        "reference_rows_not_in_dense_mapping": None,
    }
    if not read_alids:
        return facts, None
    reference = read_variant_reference(path)
    alids = reference.alids
    # Released before the set is built: `source_lookup` is twice the ALID count
    # of raw tuples, and nothing here needs it (measured 7.0 GB peak on
    # OGS-00011's 13.4M-ALID reference with it held, ~2.5 GB without).
    del reference
    facts["n_alids"] = len(alids)
    facts["n_alids_over_index_width"] = sum(1 for alid in alids if len(alid) > ALID_INDEX_WIDTH)
    return facts, set(alids)


# ---------------------------------------------------------------------------
# Read-only verification
# ---------------------------------------------------------------------------


def _store_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def _verify_method(root: Path, requested: str) -> str:
    """The read-only proof to use: `sha256` per file, or `size+mtime_ns`.

    `auto` hashes a Store small enough for it to be affordable and falls back to
    metadata above that, because a second full read of a 10 GB Store would cost
    more than the audit it is proving. What was actually used is in the report.
    """
    if requested == "metadata":
        return "size+mtime_ns"
    if requested == "sha256":
        return "sha256"
    return "sha256" if _store_bytes(root) <= HASH_VERIFY_LIMIT_BYTES else "size+mtime_ns"


def _inventory(root: Path, method: str) -> dict[str, tuple[int, int, str | None]]:
    inventory: dict[str, tuple[int, int, str | None]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        stat = path.stat()
        digest = _sha256(path) if method == "sha256" else None
        inventory[str(path.relative_to(root))] = (stat.st_size, stat.st_mtime_ns, digest)
    return inventory


def _compare(inventory: dict[str, tuple[int, int, str | None]], after: dict[str, tuple[int, int, str | None]]) -> dict[str, Any]:
    changed = sorted(name for name in inventory.keys() & after.keys() if inventory[name] != after[name])
    return {
        "files": len(inventory),
        "changed": changed,
        "added": sorted(after.keys() - inventory.keys()),
        "removed": sorted(inventory.keys() - after.keys()),
    }


# ---------------------------------------------------------------------------
# The reconciliation
# ---------------------------------------------------------------------------


def _array(group: Any, path: str) -> Any:
    node = group
    for part in path.split("/"):
        try:
            node = node[part]
        except (KeyError, TypeError):
            raise AuditError(f"the Store's data.zarr holds no {path}") from None
    if not hasattr(node, "shape"):
        raise AuditError(f"data.zarr/{path} is a group, not an array")
    return node


def _recorded(manifest_provenance: dict[str, Any]) -> dict[str, Any]:
    hybrid = manifest_provenance.get("hybrid") or {}
    ragged = manifest_provenance.get("ragged") or {}
    by_variant = (ragged.get("by_variant") or {}) if isinstance(ragged, dict) else {}
    return {
        "n_variants": manifest_provenance.get("n_variants"),
        "n_analyses": manifest_provenance.get("n_analyses"),
        "n_panel": hybrid.get("n_panel") if isinstance(hybrid, dict) else None,
        "n_off_panel": hybrid.get("n_off_panel") if isinstance(hybrid, dict) else None,
        "n_overflow_associations": hybrid.get("n_overflow_associations") if isinstance(hybrid, dict) else None,
        "ragged_by_variant_n_axis": by_variant.get("n_axis") if isinstance(by_variant, dict) else None,
        "ragged_by_variant_n_rows": by_variant.get("n_rows") if isinstance(by_variant, dict) else None,
        "variant_reference": manifest_provenance.get("variant_reference"),
    }


def _build_revision(store_path: Path) -> dict[str, Any]:
    """The exact `opengwasdb` revision that wrote this Store, when recorded.

    The manifest does not carry it; the Store Release's build record beside it
    does (`records/build.json`). Absent is recorded as absent -- a revision
    nobody wrote down is not the revision this audit guessed.
    """
    record = store_path.parent / "records" / "build.json"
    if not record.is_file():
        return {"opengwasdb_rev": None, "recorded_in": None}
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"opengwasdb_rev": None, "recorded_in": str(record), "unreadable": True}
    return {
        "opengwasdb_rev": data.get("opengwasdb_rev"),
        "recorded_in": str(record),
        "start_time": data.get("start_time"),
        "end_time": data.get("end_time"),
        "argv": data.get("argv"),
    }


def _runtime() -> dict[str, Any]:
    from importlib.metadata import PackageNotFoundError, version

    try:
        core = version("opengwasdb")
    except PackageNotFoundError:  # pragma: no cover - the engine is a hard dependency
        core = None
    return {"opengwasdb_version": core, "python": sys.version.split()[0], "numpy": np.__version__}


def reconcile(
    store_path: str | Path,
    *,
    reference_path: str | Path | None = None,
    skip_reference: bool = False,
    scan_identities: bool = True,
    band_variants: int = DEFAULT_BAND_VARIANTS,
    verify: str = "auto",
    argv: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Reconcile one Hybrid Store Release and return the report as a dict.

    The report is the evidence; the caller decides where it goes. Every claim in
    it is either read from the Store's own arrays and tables, read from the
    Store's own record (`manifest.json` and `records/build.json`), or read from
    the construction reference the record names -- never restated from a Build
    Recipe.
    """
    started = time.time()
    started_at = _now()
    path = Path(store_path)
    if not (path / "manifest.json").is_file():
        raise AuditError(f"{path} holds no manifest.json; it is not a Store Release")

    method = _verify_method(path, verify)
    before = _inventory(path, method)

    inconsistencies: list[dict[str, Any]] = [
        {
            "kind": "scope_not_measured",
            "severity": "finding",
            "detail": entry["item"],
            "ticket": entry["ticket"],
            "counts": {"why": entry["why"]},
        }
        for entry in NOT_MEASURED
    ]

    def inconsistency(kind: str, detail: str, *, severity: str = "error", **counts: Any) -> None:
        inconsistencies.append(
            {"kind": kind, "severity": severity, "detail": detail, "counts": counts}
        )

    store = open_store(path)
    manifest = store.manifest
    if manifest.primary_layout.value != "hybrid":
        raise AuditError(
            f"{path} is a {manifest.primary_layout.value} Store Release; this audit "
            "reconciles a hybrid Store's shared axis, Dense Component and off-panel "
            "remainder, and a Store without all three has nothing here to reconcile"
        )
    group = store.arrays(mode="r")
    recorded = _recorded(manifest.provenance)

    # -- the axis, and the Dense Component's membership of it ---------------
    axis_handle = VariantAxis(path)
    try:
        n_axis = axis_handle.n_variants
    finally:
        axis_handle.close()
    mapping_path = dense_to_shared_path(path)
    if not mapping_path.is_file():
        raise AuditError(f"{path} is a hybrid Store with no {mapping_path.name}")
    dense_to_shared = np.load(mapping_path, mmap_mode="r")
    dense_handle = VariantAxis(dense_component_path(path))
    try:
        n_dense_axis = dense_handle.n_variants
    finally:
        dense_handle.close()

    mapping = {
        "path": str(mapping_path),
        "dtype": str(dense_to_shared.dtype),
        "entries": int(dense_to_shared.shape[0]),
        "distinct_entries": int(np.unique(np.asarray(dense_to_shared)).size),
        "strictly_increasing": bool(
            dense_to_shared.shape[0] < 2
            or bool(np.all(np.diff(np.asarray(dense_to_shared).astype("int64")) > 0))
        ),
        "out_of_range": int(
            np.count_nonzero(
                (np.asarray(dense_to_shared) < 0) | (np.asarray(dense_to_shared) >= n_axis)
            )
        ),
        "min": int(dense_to_shared.min()) if int(dense_to_shared.shape[0]) else None,
        "max": int(dense_to_shared.max()) if int(dense_to_shared.shape[0]) else None,
    }
    dense_mask = np.zeros(n_axis, dtype=bool)
    in_range = np.asarray(dense_to_shared)
    in_range = in_range[(in_range >= 0) & (in_range < n_axis)]
    dense_mask[in_range] = True
    n_dense = int(np.count_nonzero(dense_mask))
    n_off_panel = n_axis - n_dense
    mapping["duplicate_entries"] = mapping["entries"] - mapping["distinct_entries"]
    if mapping["out_of_range"]:
        inconsistency(
            "dense_mapping_out_of_range",
            f"dense/dense_to_shared.npy names {mapping['out_of_range']} shared rows "
            f"outside the {n_axis}-entry shared axis",
            out_of_range=mapping["out_of_range"],
            n_axis=n_axis,
        )
    if mapping["duplicate_entries"]:
        inconsistency(
            "dense_mapping_duplicates",
            "dense/dense_to_shared.npy maps two Dense Component rows to one shared row",
            duplicate_entries=mapping["duplicate_entries"],
        )
    if not mapping["strictly_increasing"]:
        inconsistency(
            "dense_mapping_not_sorted",
            "dense/dense_to_shared.npy is not strictly increasing; the Dense Component's "
            "rows no longer follow the shared axis order",
        )
    if mapping["entries"] != n_dense_axis:
        inconsistency(
            "dense_axis_disagrees_with_mapping",
            f"the Dense Component holds {n_dense_axis} rows but dense_to_shared.npy has "
            f"{mapping['entries']} entries",
            dense_axis_rows=n_dense_axis,
            mapping_entries=mapping["entries"],
        )
    if recorded["n_panel"] is not None and int(recorded["n_panel"]) != n_dense:
        inconsistency(
            "recorded_panel_disagrees",
            f"the Store records {recorded['n_panel']} Dense-axis variants but "
            f"dense_to_shared.npy places {n_dense}",
            recorded_n_panel=int(recorded["n_panel"]),
            observed_n_panel=n_dense,
        )
    if recorded["n_variants"] is not None and int(recorded["n_variants"]) != n_axis:
        inconsistency(
            "recorded_axis_length_disagrees",
            f"the Store records {recorded['n_variants']} shared-axis variants but its "
            f"Store Variant Table holds {n_axis}",
            recorded_n_variants=int(recorded["n_variants"]),
            observed_n_axis=n_axis,
        )
    if recorded["n_off_panel"] is not None and int(recorded["n_off_panel"]) != n_off_panel:
        inconsistency(
            "recorded_off_panel_disagrees",
            f"the Store records {recorded['n_off_panel']} off-panel variants but its "
            f"axis minus its Dense Component is {n_off_panel}",
            recorded_n_off_panel=int(recorded["n_off_panel"]),
            observed_n_off_panel=n_off_panel,
        )
    reconciled = not any(
        entry["severity"] == "error"
        for entry in inconsistencies
        if entry["kind"]
        in {
            "dense_mapping_out_of_range",
            "dense_mapping_duplicates",
            "dense_axis_disagrees_with_mapping",
            "recorded_panel_disagrees",
            "recorded_axis_length_disagrees",
            "recorded_off_panel_disagrees",
        }
    )

    # -- the variant-major index, validated by the engine -------------------
    if array_length(_array(group, "ragged/offsets")) < 2:
        raise AuditError(f"{path} holds a Ragged Overflow with no Analysis offsets")
    ragged_offsets = np.asarray(_array(group, "ragged/offsets")[:], dtype="int64")
    n_analyses = int(ragged_offsets.size) - 1
    reader = ByVariantReader(path, manifest.encoding, n_axis=n_axis)
    by_variant_n_axis = int(reader.n_axis)
    by_variant_n_rows = int(reader.n_rows)
    del reader
    by_variant_offsets = _array(group, "ragged/by_variant/offsets")
    by_variant_entries = array_length(by_variant_offsets)
    ragged_variant_index_entries = array_length(_array(group, "ragged/variant_index"))
    by_variant_rows_entries = array_length(_array(group, "ragged/by_variant/analysis_index"))

    if by_variant_entries != n_axis + 1:
        raise AuditError(
            f"ragged/by_variant/offsets holds {by_variant_entries} entries, not the "
            f"{n_axis + 1} a {n_axis}-variant axis needs; the index does not span the "
            "axis this audit reconciles"
        )
    if ragged_offsets[0] != 0 or bool((np.diff(ragged_offsets) < 0).any()):
        inconsistency(
            "ragged_offsets_not_monotone",
            "data.zarr/ragged/offsets does not start at 0 and increase, so the "
            "Analysis-major association counts are not a partition",
            first=int(ragged_offsets[0]),
        )
    total_associations = int(ragged_offsets[-1])
    analyses_tsv_rows = _count_rows(path / "analyses.tsv")
    if recorded["n_analyses"] is not None and int(recorded["n_analyses"]) != n_analyses:
        inconsistency(
            "recorded_analysis_count_disagrees",
            f"the Store records {recorded['n_analyses']} Analyses but its Ragged "
            f"component is partitioned into {n_analyses}",
            recorded=int(recorded["n_analyses"]),
            observed=n_analyses,
        )
    if analyses_tsv_rows is not None and analyses_tsv_rows != n_analyses:
        inconsistency(
            "analyses_tsv_disagrees_with_component",
            f"analyses.tsv holds {analyses_tsv_rows} Analyses but the Ragged "
            f"component is partitioned into {n_analyses}; the release's membership "
            "table and its association bodies are two accounts of the same release",
            analyses_tsv_rows=analyses_tsv_rows,
            component_analyses=n_analyses,
        )
    for name, entries in (
        ("ragged/variant_index", ragged_variant_index_entries),
        ("ragged/by_variant/analysis_index", by_variant_rows_entries),
    ):
        if entries != total_associations:
            inconsistency(
                "association_row_count_disagrees",
                f"{name} holds {entries} rows but data.zarr/ragged/offsets ends at "
                f"{total_associations}",
                array=name,
                entries=entries,
                offsets_last=total_associations,
            )

    # -- occupancy ---------------------------------------------------------
    occupancy = _scan_occupancy(
        by_variant_offsets,
        n_axis=n_axis,
        dense_mask=dense_mask,
        band_variants=band_variants,
    )
    if not occupancy["offset_starts_at_zero"]:
        inconsistency(
            "by_variant_offsets_do_not_start_at_zero",
            "ragged/by_variant/offsets does not start at 0; the first variant's row "
            "range is not a row range",
        )
    if occupancy["bands_with_decreasing_offsets"]:
        inconsistency(
            "by_variant_offsets_decrease",
            f"{occupancy['bands_with_decreasing_offsets']} band(s) of "
            "ragged/by_variant/offsets decrease, so a variant's association count is "
            "negative",
            bands_with_decreasing_offsets=occupancy["bands_with_decreasing_offsets"],
        )
    if occupancy["residual"]:
        inconsistency(
            "partition_occupancy_residual",
            "the Dense-axis and off-panel partitions do not add up to the axis total",
        )
    if occupancy["axis"]["associations"] != total_associations:
        inconsistency(
            "association_total_disagrees",
            f"the variant-major index accounts for "
            f"{occupancy['axis']['associations']} associations but the Analysis-major "
            f"component holds {total_associations}",
            variant_major=occupancy["axis"]["associations"],
            analysis_major=total_associations,
        )
    if recorded["n_overflow_associations"] is not None and int(
        recorded["n_overflow_associations"]
    ) != total_associations:
        inconsistency(
            "recorded_overflow_associations_disagree",
            f"the Store records {recorded['n_overflow_associations']} Overflow "
            f"associations but its Ragged component holds {total_associations}",
            recorded=int(recorded["n_overflow_associations"]),
            observed=total_associations,
        )
    if recorded["ragged_by_variant_n_axis"] is not None and int(
        recorded["ragged_by_variant_n_axis"]
    ) != by_variant_n_axis:
        inconsistency(
            "recorded_variant_index_span_disagrees",
            f"the Store records a variant-major index over "
            f"{recorded['ragged_by_variant_n_axis']} variants but it spans "
            f"{by_variant_n_axis}",
        )
    if recorded["ragged_by_variant_n_rows"] is not None and int(
        recorded["ragged_by_variant_n_rows"]
    ) != by_variant_n_rows:
        inconsistency(
            "recorded_variant_index_rows_disagree",
            f"the Store records {recorded['ragged_by_variant_n_rows']} variant-major "
            f"rows but the index holds {by_variant_n_rows}",
        )
    dense_with_associations = occupancy["dense"]["associations"]
    if dense_with_associations:
        inconsistency(
            "dense_axis_rows_carry_overflow_associations",
            f"{occupancy['dense']['observed_variants']} Dense-axis variants carry "
            f"{dense_with_associations} Overflow associations; the Dense Component and "
            "the Ragged Overflow must partition the axis, so an on-panel association "
            "in the Overflow is a disjoint-partition violation",
            dense_axis_associated_variants=occupancy["dense"]["observed_variants"],
            dense_axis_associations=dense_with_associations,
        )
    # Unobserved *off-panel* entries are the epic's question: an axis entry with
    # no surviving observed association would be a pre-filter identity the axis
    # carries and nothing backs. The Dense-axis entries' zero Overflow occupancy
    # is the disjoint partition working as designed, so it is context in the
    # finding, never a finding of its own.
    unobserved_off_panel = occupancy["off_panel"]["unobserved_variants"]
    occupancy["note"] = (
        "Dense-axis entries carry no Overflow association by the disjoint partition "
        "(their associations are in the Dense Component), so an unobserved entry is "
        "only meaningful on the off-panel axis"
    )
    if unobserved_off_panel:
        inconsistency(
            "unobserved_axis_entries",
            f"{unobserved_off_panel} of the {n_off_panel} off-panel axis entries carry "
            "no surviving observed association; they exist on the axis and nothing "
            "observes them",
            severity="finding",
            unobserved_off_panel_variants=unobserved_off_panel,
            unobserved_dense_variants=occupancy["dense"]["unobserved_variants"],
            n_off_panel=n_off_panel,
            examples=occupancy["off_panel"]["unobserved_examples"],
        )

    # -- canonical identity ------------------------------------------------
    identities: dict[str, Any] = {"scanned": False}
    reference_facts: dict[str, Any]
    reference_set: set[str] | None = None
    reference_source = reference_path
    if reference_source is None and isinstance(recorded["variant_reference"], str):
        reference_source = recorded["variant_reference"]
    if not skip_reference:
        if reference_source is None:
            reference_facts = {
                "path": None,
                "available": False,
                "skipped": False,
                "n_alids": None,
            }
            inconsistency(
                "reference_unknown",
                "the Store records no construction variant reference and --reference "
                "was not given, so the off-panel partition cannot be reconciled against "
                "the reference it was built from",
            )
        elif not Path(reference_source).is_file():
            reference_facts = {
                "path": str(reference_source),
                "available": False,
                "skipped": False,
                "n_alids": None,
            }
            inconsistency(
                "reference_unavailable",
                f"the construction variant reference {reference_source} the Store "
                "records is not readable here; the off-panel partition cannot be "
                "reconciled against it, which is a failure and not a pass",
            )
        else:
            reference_facts, reference_set = _reference_facts(
                Path(reference_source), read_alids=scan_identities
            )
    else:
        reference_facts = {
            "path": None if reference_path is None else str(reference_path),
            "available": False,
            "skipped": True,
            "n_alids": None,
        }

    if scan_identities:
        table = variant_table_path(path)
        if not table.is_file():
            raise AuditError(f"{path} holds no variants.tsv.gz")
        scan = _scan_variant_table(table, n_axis=n_axis, reference=reference_set)
        index_scan: _AlidIndexScan | None = None
        bytes_path = variant_alid_bytes_path(path)
        rows_path = variant_alid_rows_path(path)
        if bytes_path.is_file() and rows_path.is_file():
            index_scan = _scan_alid_index(bytes_path, rows_path)
        else:
            inconsistency(
                "alid_search_index_absent",
                "the Store holds no ALID search index, so duplicate ALIDs are counted "
                "only within one position's rows rather than over the whole axis",
            )
        over_width_extra = sum(count - 1 for count in scan.over_width_alids.values())
        duplicate_extra = (
            (index_scan.duplicate_extra_rows if index_scan is not None else 0) + over_width_extra
        )
        duplicate_groups = (index_scan.duplicate_groups if index_scan is not None else 0) + sum(
            1 for count in scan.over_width_alids.values() if count > 1
        )
        identities = {
            "scanned": True,
            "rows_read": scan.rows,
            "distinct_alids": scan.rows - duplicate_extra,
            "duplicate_alid_groups": duplicate_groups,
            "duplicate_alid_extra_rows": duplicate_extra,
            "duplicate_alid_source": (
                "the Store's sorted ALID search index (exact, no ordering assumption) "
                "plus the rows wider than its fixed width, counted from the table"
            ),
            "duplicate_alid_rows_within_a_position": scan.duplicate_extra_rows_in_table,
            "alid_index": {
                "entries": index_scan.entries if index_scan is not None else None,
                "sorted": index_scan.sorted_ok if index_scan is not None else None,
                "table_rows_indexable": scan.indexable_rows,
                "table_rows_over_index_width": scan.rows - scan.indexable_rows,
                "over_width_distinct_alids": len(scan.over_width_alids),
                "agrees_with_table": (
                    index_scan.entries == scan.indexable_rows if index_scan is not None else None
                ),
            },
            "non_canonical_rows": scan.non_canonical_rows,
            "variant_index_in_order": scan.variant_index_violations == 0,
            "variant_index_violations": scan.variant_index_violations,
            "position_order_violations": scan.position_order_violations,
            "repeated_contig_blocks": scan.repeated_contig_blocks,
            "positions_with_multiple_rows": scan.positions_with_multiple_rows,
            "multiallelic_sites": scan.multiallelic_sites,
            "multiallelic_rows": scan.multiallelic_rows,
            "missing_rsid_rows": scan.missing_rsid_rows,
            "alid_consistency_sample": {
                "stride": ALID_SAMPLE_STRIDE,
                "checked": scan.sample_checked,
                "mismatches": scan.sample_mismatches,
            },
        }
        if scan.rows != n_axis:
            inconsistency(
                "variant_table_row_count_disagrees",
                f"the Store Variant Table holds {scan.rows} rows but the axis is "
                f"{n_axis} variants",
                rows=scan.rows,
                n_axis=n_axis,
            )
        if scan.variant_index_violations:
            inconsistency(
                "variant_index_not_in_order",
                f"{scan.variant_index_violations} table rows do not carry their own "
                "position's variant_index",
                violations=scan.variant_index_violations,
            )
        if duplicate_extra:
            inconsistency(
                "duplicate_alid",
                f"{duplicate_extra} shared-axis rows repeat a canonical identity "
                f"({duplicate_groups} duplicate group(s)); the axis is meant to be one "
                "row per canonical ALID",
                duplicate_alid_groups=duplicate_groups,
                duplicate_alid_extra_rows=duplicate_extra,
            )
        if scan.non_canonical_rows:
            inconsistency(
                "non_canonical_alid_rows",
                f"{scan.non_canonical_rows} rows store an ALID whose alleles are not in "
                "canonical order, so one identity can occupy two rows",
                rows=scan.non_canonical_rows,
            )
        if scan.repeated_contig_blocks:
            inconsistency(
                "contig_blocks_repeat",
                f"{scan.repeated_contig_blocks} contig(s) appear in more than one block; "
                "the axis is not in tabix order",
                repeats=scan.repeated_contig_blocks,
            )
        if reference_facts.get("available") and reference_set is not None:
            reference_rows = scan.reference_rows
            facts = reference_facts
            facts["axis_rows_in_reference"] = int(scan.reference_rows_seen)
            facts["axis_rows_absent_from_reference"] = int(n_axis - scan.reference_rows_seen)
            facts["reference_alids_missing_from_axis"] = int(
                int(facts["n_alids"] or 0) - scan.reference_rows_seen
            )
            facts["dense_mapping_not_in_reference"] = int(
                np.count_nonzero(dense_mask & ~reference_rows)
            )
            facts["reference_rows_not_in_dense_mapping"] = int(
                np.count_nonzero(reference_rows & ~dense_mask)
            )
            if facts["reference_alids_missing_from_axis"]:
                inconsistency(
                    "reference_alids_missing_from_axis",
                    f"{facts['reference_alids_missing_from_axis']} ALIDs of the "
                    "construction reference have no shared-axis row",
                    missing=facts["reference_alids_missing_from_axis"],
                )
            if (
                facts["dense_mapping_not_in_reference"]
                or facts["reference_rows_not_in_dense_mapping"]
            ):
                inconsistency(
                    "panel_partition_disagrees_with_reference",
                    "the Dense Component's row set is not exactly the construction "
                    f"reference's: {facts['dense_mapping_not_in_reference']} Dense-axis "
                    f"rows are absent from the reference and "
                    f"{facts['reference_rows_not_in_dense_mapping']} reference variants "
                    "are off the Dense Component",
                    dense_rows_not_in_reference=facts["dense_mapping_not_in_reference"],
                    reference_rows_not_dense=facts["reference_rows_not_in_dense_mapping"],
                )
            if facts["axis_rows_in_reference"] != n_dense:
                inconsistency(
                    "reference_overlap_disagrees_with_panel",
                    f"{facts['axis_rows_in_reference']} axis rows are in the "
                    f"construction reference but the Dense Component covers {n_dense}",
                    axis_rows_in_reference=facts["axis_rows_in_reference"],
                    n_dense=n_dense,
                )
    elif reference_facts.get("available"):
        # The distribution without the pass that would measure it: recorded,
        # not inferred, and visibly so.
        for key in (
            "axis_rows_in_reference",
            "axis_rows_absent_from_reference",
            "reference_alids_missing_from_axis",
            "dense_mapping_not_in_reference",
            "reference_rows_not_in_dense_mapping",
        ):
            reference_facts[key] = None

    # -- read-only proof ----------------------------------------------------
    after = _inventory(path, method)
    unchanged = _compare(before, after)
    unchanged["method"] = method
    if unchanged["changed"] or unchanged["added"] or unchanged["removed"]:
        inconsistency(
            "store_modified",
            "the Store changed while it was being audited: "
            f"{len(unchanged['changed'])} file(s) differ, {len(unchanged['added'])} "
            f"appeared and {len(unchanged['removed'])} disappeared",
            changed=unchanged["changed"],
            added=unchanged["added"],
            removed=unchanged["removed"],
        )

    errors = [entry for entry in inconsistencies if entry["severity"] == "error"]
    finished = time.time()
    return {
        "schema": REPORT_SCHEMA,
        "audit_version": AUDIT_VERSION,
        "argv": list(argv) if argv is not None else sys.argv[1:],
        "started_at": started_at,
        "finished_at": _now(),
        "wall_seconds": round(finished - started, 3),
        "runtime": _runtime(),
        "store": {
            "path": str(path),
            "manifest_sha256": _sha256(path / "manifest.json"),
            "store_id": manifest.store_id,
            "release_id": manifest.release_id,
            "format_version": manifest.format_version,
            "primary_layout": manifest.primary_layout.value,
            "completion_state": manifest.completion_state.value,
            "association_coverage": manifest.association_coverage.value,
            "reference_assembly": manifest.reference_assembly,
            "created_at": manifest.created_at,
            "opened_mode": "r",
            "build_revision": _build_revision(path),
            "recorded": recorded,
            "analyses_tsv_rows": analyses_tsv_rows,
        },
        "reference": reference_facts,
        "axis": {
            "n_axis": n_axis,
            "n_dense": n_dense,
            "n_off_panel": n_off_panel,
            "dense_axis_rows": n_dense_axis,
            "dense_to_shared": mapping,
            "reconciled": reconciled,
        },
        "occupancy": occupancy,
        "components": {
            "n_analyses": n_analyses,
            "n_analyses_recorded": recorded["n_analyses"],
            "ragged_offsets_last": total_associations,
            "ragged_variant_index_entries": ragged_variant_index_entries,
            "by_variant_n_axis": by_variant_n_axis,
            "by_variant_n_rows": by_variant_n_rows,
            "by_variant_offsets_entries": by_variant_entries,
            "by_variant_analysis_index_entries": by_variant_rows_entries,
            "index_validated_by": BY_VARIANT_READER,
        },
        "identities": identities,
        "inconsistencies": inconsistencies,
        "ok": not errors,
        "error_count": len(errors),
        "finding_count": len(inconsistencies) - len(errors),
        "unchanged": unchanged,
        "resources": {
            "band_variants": band_variants,
            "bands": occupancy["bands"],
            "max_rss_kb": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),
            "peak_rss_includes": "the construction reference's ALID set",
        },
        "scope": {
            "measured": [
                "shared-axis / Dense-axis / off-panel counts, reconciled against the Store's own record",
                "observed occupancy per axis partition, from the variant-major CSR offsets",
                "exact duplicate ALIDs and multiallelic sites in the Store Variant Table",
                "construction-reference identity and the Dense Component's membership of it",
                "read-only proof: no Store file changed",
            ],
            "not_measured": list(NOT_MEASURED),
        },
    }


def _count_rows(path: Path) -> int | None:
    """Data rows of a headerless-or-headered TSV, or None when it is absent."""
    if not path.is_file():
        return None
    rows = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip() and not line.startswith("#"):
                rows += 1
    # A Store Release's analyses.tsv always carries its header row.
    return max(rows - 1, 0)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m ogstores.audit",
        description=(
            "Read-only Store Audit (stores #372, epic #371): reconcile a built Hybrid "
            "Store Release's axis, occupancy and identity against what the Store records."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    reconcile = commands.add_parser(
        "reconcile",
        help="reconcile one Hybrid Store Release and write the evidence JSON",
        description=(
            "Reconcile the shared axis, its Dense Component and the off-panel remainder "
            "of one Hybrid Store Release. Exit 0 when every disagreement is a finding, "
            "1 when the Store's artifact and its record disagree, 2 when the Store "
            "cannot be audited at all."
        ),
    )
    reconcile.add_argument("store", help="path to a Store Release directory (store.opengwasdb)")
    reconcile.add_argument(
        "--output",
        required=True,
        help="evidence JSON to write; must be outside the Store",
    )
    reference = reconcile.add_mutually_exclusive_group()
    reference.add_argument(
        "--reference",
        default=None,
        help=(
            "construction variant reference to reconcile the off-panel partition "
            "against; defaults to the one the Store records"
        ),
    )
    reference.add_argument(
        "--skip-reference",
        action="store_true",
        help=(
            "do not reconcile against a construction reference at all; recorded "
            "explicitly as skipped rather than passing silently"
        ),
    )
    reconcile.add_argument(
        "--no-identities",
        action="store_true",
        help="skip the variant-table pass (duplicate ALIDs, multiallelics, reference membership)",
    )
    reconcile.add_argument(
        "--band-variants",
        type=int,
        default=DEFAULT_BAND_VARIANTS,
        help=f"axis entries one band of the occupancy scan spans (default {DEFAULT_BAND_VARIANTS})",
    )
    reconcile.add_argument(
        "--verify",
        choices=("auto", "sha256", "metadata"),
        default="auto",
        help=(
            "how to prove the Store did not change: sha256 every file, compare size and "
            f"mtime only, or auto (sha256 up to {HASH_VERIFY_LIMIT_BYTES} bytes)"
        ),
    )
    return parser


def _inside(path: Path, root: Path) -> bool:
    try:
        resolved = Path(os.path.realpath(path))
        resolved_root = Path(os.path.realpath(root))
    except OSError:  # pragma: no cover - realpath does not raise for a missing tail
        return False
    return resolved == resolved_root or resolved_root in resolved.parents


def _summary(report: dict[str, Any], output: Path) -> str:
    axis = report["axis"]
    occupancy = report["occupancy"]
    identities = report["identities"]
    duplicates = identities.get("duplicate_alid_extra_rows")
    return (
        f"{report['store']['store_id']}: axis {axis['n_axis']:,} = dense "
        f"{axis['n_dense']:,} + off-panel {axis['n_off_panel']:,} | off-panel "
        f"associations {occupancy['off_panel']['associations']:,}, observed "
        f"{occupancy['off_panel']['observed_variants']:,}, unobserved "
        f"{occupancy['off_panel']['unobserved_variants']:,} | duplicate ALID rows "
        f"{'not measured' if duplicates is None else f'{duplicates:,}'} | errors "
        f"{report['error_count']}, findings {report['finding_count']}\n"
        f"evidence: {output}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        # argparse exits 2 on a bad invocation and 0 on --help; a caller that
        # embedded this command gets the code rather than a SystemExit.
        return int(exc.code or 0)
    if args.command != "reconcile":  # pragma: no cover - argparse refuses anything else
        raise SystemExit(2)
    store_path = Path(args.store)
    output = Path(args.output)
    if args.band_variants < 1:
        print("audit: --band-variants must be at least 1", file=sys.stderr)
        return 2
    if _inside(output, store_path):
        print(
            f"audit: refusing to write evidence inside the Store ({output} is under "
            f"{store_path}); a read-only audit writes nothing there",
            file=sys.stderr,
        )
        return 2
    try:
        report = reconcile(
            store_path,
            reference_path=args.reference,
            skip_reference=args.skip_reference,
            scan_identities=not args.no_identities,
            band_variants=args.band_variants,
            verify=args.verify,
            argv=argv,
        )
    except AuditError as exc:
        print(f"audit: {exc}", file=sys.stderr)
        return 2
    output.parent.mkdir(parents=True, exist_ok=True)
    staged = output.with_name(f"{output.name}.tmp")
    staged.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    staged.replace(output)
    print(_summary(report, output))
    return 1 if report["error_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
