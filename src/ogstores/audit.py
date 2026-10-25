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

Epic #371's later slices are commands beside this one, each still read-only and
each still an evidence file: `attribute` (#373) attributes the off-panel union
to the Analyses and publications that observe it and writes the seeded sample a
raw-row trace starts from, and `trace` (#374) follows those sampled rows back to
their raw source rows, the frequency each source reports, and the admission
`opengwasdb.build.row_admission.admit_rows` gives the row under the Analysis's
own declared MAF and INFO policies.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import platform
import re
import resource
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pysam

from opengwasdb.build.row_admission import admit_rows
from opengwasdb.encoding.plan import EAF_ABSENT, EAF_EXCEPTION, Z_MISSING
from opengwasdb.model.info_score_policy import parse_info_score_policy
from opengwasdb.model.maf_policy import parse_maf_policy
from opengwasdb.readers.interface import parse_imputation_score
from opengwasdb.stats import parse_af
from opengwasdb.variants.axis import parse_canonical_alid
from opengwasdb.variants.normalise import VariantNormalisationError, normalise_chromosome
from opengwasdb.layouts.hybrid.layout import dense_component_path, dense_to_shared_path
from opengwasdb.layouts.ragged.by_variant import ByVariantReader
from opengwasdb.model.analyses import read_analysis_records
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
        "why": (
            "this slice reconciles the axis, not its contributors; the "
            "`ogstores.audit attribute` command measures them (stores #373)"
        ),
        "ticket": "#373",
    },
    {
        "item": "association-level support counts across Analyses",
        "why": (
            "support is reported here per variant, from the CSR offsets only; "
            "`ogstores.audit attribute` counts distinct observers (stores #373)"
        ),
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


#: What each stage measures, keyed by the stage a flag can skip. `scope.measured`
#: is built from this minus the skipped ones, so a run that skipped a stage never
#: lists it as measured.
MEASURED_SCOPE: tuple[tuple[str, str], ...] = (
    ("axis", "shared-axis / Dense-axis / off-panel counts, reconciled against the Store's own record"),
    ("occupancy", "observed occupancy per axis partition, from the variant-major CSR offsets"),
    ("identities", "exact duplicate ALIDs and multiallelic sites in the Store Variant Table"),
    ("reference", "construction-reference identity and the Dense Component's membership of it"),
    ("read_only", "read-only proof: no Store file changed"),
)

#: The stages `--no-identities` and `--skip-reference` switch off. Skipping one
#: is allowed, but the run then reports `ok: false` and names the stage: an audit
#: that did not measure a required partition did not close the reconciliation.
SKIPPABLE_SCOPE: dict[str, str] = {
    "identities": "canonical-identity scan of the variant table",
    "reference": "construction reference partition",
}

#: The provenance keys this audit reconciles against. A key that is absent, or
#: present and not an integer, is missing evidence: the partition it describes
#: then has nothing to be reconciled with, which is an `error`, not a pass.
RECORDED_COUNTS: tuple[str, ...] = (
    "n_variants",
    "n_analyses",
    "n_panel",
    "n_off_panel",
    "n_overflow_associations",
    "ragged_by_variant_n_axis",
    "ragged_by_variant_n_rows",
)


#: Axis entries one band of the attribution counters and sample scans spans.
#: The scan only ever holds one band of `int64` offsets plus one band of
#: `uint16` support planes, so the answer is independent of it.
DEFAULT_ATTRIBUTION_BAND_VARIANTS = 8_000_000

#: Association rows one band of the support scan spans. The ragged body is 2.9B
#: rows on OGS-00011, so one band is 16M `int32` indices (64 MB) rather than the
#: whole plane (11.6 GB).
DEFAULT_ATTRIBUTION_BAND_ROWS = 16_000_000

#: The pinned seed the sample's choices are derived from. `--seed` changes it,
#: and every other answer in the report is seed-independent.
DEFAULT_ATTRIBUTION_SEED = 42

#: Contributors the sample draws from, and the number of rows each stratum
#: keeps. `--top` and `--sample-per-stratum` change them.
DEFAULT_ATTRIBUTION_TOP = 5
DEFAULT_SAMPLE_PER_STRATUM = 20

#: Contributors listed in the report's ranking tables, whether or not they are
#: sampled. A ranking is evidence; the sample is a tracing aid.
DEFAULT_ATTRIBUTION_RANKING = 20

#: Candidates one stratum holds before the sample picks from them. The pool is
#: larger than the sample so that each allele class can be represented even
#: when it is rare among a stratum's candidates.
SAMPLE_POOL_MULTIPLIER = 8

#: Analyses named on each on-panel control row, from the Analyses the off-panel
#: sample already names. The Dense Component holds one cell per (panel variant,
#: Analysis), so the control names a cell rather than a stratum.
DEFAULT_ON_PANEL_ANALYSES = 5

#: Distinct-Analyses (and distinct-publications) support buckets, per off-panel
#: variant. `--band-variants` does not change them.
ATTRIBUTION_SUPPORT_BUCKETS: tuple[str, ...] = ("1", "2", "3-10", "11-100", "101+")

#: `np.digitize` bins for `ATTRIBUTION_SUPPORT_BUCKETS`: a support of 1 lands in
#: bucket 0, 2 in bucket 1, 3-10 in 2, 11-100 in 3, 101+ in 4.
ATTRIBUTION_SUPPORT_EDGES: tuple[int, ...] = (2, 3, 11, 101)

#: Allele classes the off-panel axis is broken down by. `snp` is one base
#: against one base, `indel` one length against another, and `complex` two
#: equal-length alleles longer than one base (a multi-nucleotide substitution).
ALLELE_CLASSES: tuple[str, ...] = ("snp", "indel", "complex")

#: The attribute command's evidence schema. Adding a field does not bump it.
ATTRIBUTION_REPORT_SCHEMA = "ogstores.audit.attribute/1"

#: The sample TSV's columns, in order. The first nine are the contract #374
#: reads; the rest are context a raw-row trace uses without a second query.
SAMPLE_COLUMNS: tuple[str, ...] = (
    "sample_id",
    "analysis_id",
    "analysis_index",
    "alid",
    "rsid",
    "stratum",
    "on_panel",
    "publication_pmid",
    "source_row_lookup_hint",
    "allele_class",
    "chromosome",
    "variant_index",
    "n_analyses",
    "n_publications",
)

#: The full per-Analysis contributor table written beside the evidence JSON.
ANALYSIS_TABLE_COLUMNS: tuple[str, ...] = (
    "analysis_index",
    "analysis_id",
    "analysis_label",
    "publication_key",
    "publication_key_kind",
    "publication_pmid",
    "publication_doi",
    "first_author",
    "consortium",
    "off_panel_associations",
    "distinct_off_panel_variants",
    "exclusive_variants",
    "exclusive_associations",
)

#: The full per-publication contributor table written beside the evidence JSON.
PUBLICATION_TABLE_COLUMNS: tuple[str, ...] = (
    "publication_key",
    "publication_key_kind",
    "publication_pmid",
    "publication_doi",
    "first_author",
    "n_analyses",
    "off_panel_associations",
    "distinct_off_panel_variants",
    "exclusive_variants",
    "exclusive_associations",
    "exclusive_variants_observed_by_several_analyses",
)

#: The sentinel a `uint16` support plane carries where an Analysis or a
#: publication code has not been assigned (support 0, or an unreached entry).
NO_CODE = 0xFFFF

#: What this slice does not measure, with the ticket that owns each.
ATTRIBUTION_NOT_MEASURED: tuple[dict[str, str], ...] = (
    {
        "item": "source frequency evidence (raw EAF against the 0.005-0.995 interval)",
        "why": ("attribution names the contributors; whether an association was admitted "
                "needs its source row, not the Store (stores #374)"),
        "ticket": "#374",
    },
    {
        "item": "duplicate rsid groups naming several ALIDs",
        "why": (
            "needs a global 114M-key index; the Store's rsid index is sorted by rsid "
            "but its rows are not grouped, so a group is not a contiguous run"
        ),
        "ticket": "#375",
    },
    {
        "item": "the admission mechanism itself (why these variants were kept)",
        "why": (
            "attribution measures who contributed the off-panel union, not which "
            "threshold or filter admitted it (stores #374)"
        ),
        "ticket": "#374",
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


def _close_position_group(
    scan: _IdentityScan, rows: int, pairs: list[tuple[str, str]] | None, first: tuple[str, str]
) -> None:
    """Account for one position's rows once that position's run has ended.

    Called only for a position holding more than one row, which is why the
    ordinary one-variant-per-position case costs one comparison and no call.
    """
    scan.positions_with_multiple_rows += 1
    members = pairs if pairs is not None else [first]
    distinct = set(members)
    scan.duplicate_extra_rows_in_table += rows - len(distinct)
    alleles = [allele for pair in distinct for allele in pair]
    if len(alleles) != len(set(alleles)):
        scan.multiallelic_sites += 1
        scan.multiallelic_rows += rows


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
                _close_position_group(scan, group_rows, group_pairs, group_first)
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
        _close_position_group(scan, group_rows, group_pairs, group_first)
    return scan


@dataclass(frozen=True)
class _AlidIndexScan:
    entries: int
    rows_entries: int
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

    Every contradiction the index can carry is reported to the caller rather
    than raised or ignored: an index that is not sorted, one whose two arrays
    disagree, and one that disagrees with the table's own count of indexable
    rows are all `error`s in the report, because a silently mis-sorted index is
    a lookup that answers with another variant's row.
    """
    index = np.load(bytes_path, mmap_mode="r")
    rows = np.load(rows_path, mmap_mode="r")
    entries = int(index.shape[0])
    rows_entries = int(rows.shape[0])
    sorted_ok = True
    duplicate_groups = 0
    duplicate_extra_rows = 0
    # Whether the pair that straddles the block boundary (`high - 1`, `high`)
    # is equal, so a run spanning a boundary is one group and not two.
    leading_equal = False
    for low in range(0, min(entries, rows_entries), block):
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
        rows_entries=rows_entries,
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
        "unchanged": not (changed or after.keys() - inventory.keys() or inventory.keys() - after.keys()),
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


def _count(recorded: dict[str, Any], key: str) -> int | None:
    """A recorded count, or None when the Store did not record one as an integer."""
    value = recorded.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


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
    """The exact `opengwasdb` revision that wrote this Store, from its build record.

    The manifest does not carry it; the Store Release's build record beside it
    does (`records/build.json`). A revision nobody recorded is not the revision
    this audit guessed (CONTRIBUTING: fail loudly rather than substitute), so a
    missing record, an unreadable one, and one with no revision are each
    reported with the `problem` that makes them an `error` in the report.
    """
    record = store_path.parent / "records" / "build.json"
    facts: dict[str, Any] = {
        "opengwasdb_rev": None,
        "path": str(record),
        "recorded_in": None,
        "available": False,
        "problem": None,
    }
    if not record.is_file():
        facts["problem"] = "missing"
        return facts
    facts["recorded_in"] = str(record)
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        facts["problem"] = "unreadable"
        facts["error"] = str(exc)
        return facts
    if not isinstance(data, dict):
        facts["problem"] = "unreadable"
        facts["error"] = "the build record is not a JSON object"
        return facts
    revision = data.get("opengwasdb_rev")
    facts["available"] = True
    facts["opengwasdb_rev"] = revision
    facts["start_time"] = data.get("start_time")
    facts["end_time"] = data.get("end_time")
    facts["argv"] = data.get("argv")
    if not isinstance(revision, str) or not revision.strip():
        facts["problem"] = "no_revision"
    return facts


def _release_lock(store_path: Path, store_id: str) -> dict[str, Any]:
    """Whether a live run holds this release's lock, read and never created.

    `ogstores.release.lock_holder` probes the lock with a non-blocking shared
    `flock`, so a held lock is visible without taking anything and a stale lock
    file whose holder is gone reads as free. The lock lives outside the Store's
    own directory (`ogstores.paths.release_lock_path`), so asking about it
    cannot change a file of the release this audit calls unchanged.
    """
    facts: dict[str, Any] = {
        "store_id": store_id,
        "path": None,
        "held_by": None,
        "checked": False,
    }
    if len(store_path.parents) < 2:
        return facts
    from ogstores import paths as registry_paths
    from ogstores.release import lock_holder

    root = store_path.parents[1]
    lock_path = registry_paths.release_lock_path(store_id, root=root)
    facts["path"] = str(lock_path)
    facts["checked"] = True
    facts["held_by"] = lock_holder(store_id, root)
    return facts


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
    # The directory's identity, before any work: a Reference Completion replaces
    # a Store under its own id (ADR 0037), so an audit that straddled a swap
    # would reconcile one Store's arrays with another's tables.
    start_stat = path.stat()
    start_parent_stat = path.parent.stat()

    skipped: list[dict[str, str]] = []
    if skip_reference:
        skipped.append(
            {
                "stage": "reference",
                "item": SKIPPABLE_SCOPE["reference"],
                "why": "--skip-reference was given",
            }
        )
    if not scan_identities:
        skipped.append(
            {
                "stage": "identities",
                "item": SKIPPABLE_SCOPE["identities"],
                "why": "--no-identities was given",
            }
        )

    skipped_stages = {entry["stage"] for entry in skipped}

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

    for entry in skipped:
        inconsistency(
            "skipped_evidence",
            f"{entry['item']} was skipped ({entry['why']}); this run did not measure "
            "it and must not be read as an exact, closed reconciliation",
            stage=entry["stage"],
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
    release_lock = _release_lock(path, manifest.store_id)
    if release_lock["held_by"]:
        inconsistency(
            "release_lock_held",
            f"a run holds this release's lock ({release_lock['held_by']}); the Store "
            "may be replaced under this audit, so the view is recorded by directory "
            "identity and verified again at the end rather than assumed stable",
            severity="finding",
            store_id=release_lock["store_id"],
            held_by=release_lock["held_by"],
        )

    counts = {key: _count(recorded, key) for key in RECORDED_COUNTS}
    missing_recorded = [key for key, value in counts.items() if value is None]
    if missing_recorded:
        inconsistency(
            "recorded_counts_incomplete",
            "the Store's provenance does not record "
            f"{', '.join(missing_recorded)}; those partitions cannot be reconciled "
            "against anything, so this audit cannot close",
            missing=missing_recorded,
        )

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
    if counts["n_panel"] is not None and counts["n_panel"] != n_dense:
        inconsistency(
            "recorded_panel_disagrees",
            f"the Store records {counts['n_panel']} Dense-axis variants but "
            f"dense_to_shared.npy places {n_dense}",
            recorded_n_panel=counts["n_panel"],
            observed_n_panel=n_dense,
        )
    if counts["n_variants"] is not None and counts["n_variants"] != n_axis:
        inconsistency(
            "recorded_axis_length_disagrees",
            f"the Store records {counts['n_variants']} shared-axis variants but its "
            f"Store Variant Table holds {n_axis}",
            recorded_n_variants=counts["n_variants"],
            observed_n_axis=n_axis,
        )
    if counts["n_off_panel"] is not None and counts["n_off_panel"] != n_off_panel:
        inconsistency(
            "recorded_off_panel_disagrees",
            f"the Store records {counts['n_off_panel']} off-panel variants but its "
            f"axis minus its Dense Component is {n_off_panel}",
            recorded_n_off_panel=counts["n_off_panel"],
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
    analyses_tsv = path / "analyses.tsv"
    analyses_tsv_rows: int | None = None
    if not analyses_tsv.is_file():
        inconsistency(
            "analyses_tsv_unavailable",
            f"{analyses_tsv} is absent; the release's membership table is the other "
            "account of how many Analyses it holds, and this audit cannot check the "
            "Ragged partition against it",
            path=str(analyses_tsv),
            problem="missing",
        )
    else:
        try:
            analyses_tsv_rows = _count_rows(analyses_tsv)
        except (OSError, UnicodeDecodeError) as exc:
            inconsistency(
                "analyses_tsv_unavailable",
                f"{analyses_tsv} cannot be read ({exc}); an undecodable membership "
                "table is missing evidence, not an empty release",
                path=str(analyses_tsv),
                problem="unreadable",
            )
    if counts["n_analyses"] is not None and counts["n_analyses"] != n_analyses:
        inconsistency(
            "recorded_analysis_count_disagrees",
            f"the Store records {counts['n_analyses']} Analyses but its Ragged "
            f"component is partitioned into {n_analyses}",
            recorded=counts["n_analyses"],
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
    if counts["n_overflow_associations"] is not None and (
        counts["n_overflow_associations"] != total_associations
    ):
        inconsistency(
            "recorded_overflow_associations_disagree",
            f"the Store records {counts['n_overflow_associations']} Overflow "
            f"associations but its Ragged component holds {total_associations}",
            recorded=counts["n_overflow_associations"],
            observed=total_associations,
        )
    if counts["ragged_by_variant_n_axis"] is not None and (
        counts["ragged_by_variant_n_axis"] != by_variant_n_axis
    ):
        inconsistency(
            "recorded_variant_index_span_disagrees",
            f"the Store records a variant-major index over "
            f"{counts['ragged_by_variant_n_axis']} variants but it spans "
            f"{by_variant_n_axis}",
        )
    if counts["ragged_by_variant_n_rows"] is not None and (
        counts["ragged_by_variant_n_rows"] != by_variant_n_rows
    ):
        inconsistency(
            "recorded_variant_index_rows_disagree",
            f"the Store records {counts['ragged_by_variant_n_rows']} variant-major "
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
    build_revision = _build_revision(path)
    if build_revision["problem"] == "missing":
        inconsistency(
            "build_record_unavailable",
            f"no build record at {build_revision['path']}; the Store's exact "
            "opengwasdb revision is then unknown, and a revision nobody recorded is "
            "not one this audit may infer",
            path=build_revision["path"],
        )
    elif build_revision["problem"] == "unreadable":
        inconsistency(
            "build_record_unreadable",
            f"the build record at {build_revision['path']} is not readable JSON "
            f"({build_revision.get('error')})",
            path=build_revision["path"],
        )
    elif build_revision["problem"] == "no_revision":
        inconsistency(
            "build_revision_unrecorded",
            f"the build record at {build_revision['path']} names no "
            "opengwasdb_rev, so which code wrote this Store is unrecorded",
            path=build_revision["path"],
        )

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
                "rows_entries": index_scan.rows_entries if index_scan is not None else None,
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
        if index_scan is not None and index_scan.rows_entries != index_scan.entries:
            inconsistency(
                "alid_index_half_written",
                f"variant_alid_bytes.npy holds {index_scan.entries} ALIDs but "
                f"variant_alid_rows.npy holds {index_scan.rows_entries} rows; the "
                "search index is half written and resolves nothing reliably",
                entries=index_scan.entries,
                rows_entries=index_scan.rows_entries,
            )
        if index_scan is not None and not index_scan.sorted_ok:
            inconsistency(
                "alid_index_not_sorted",
                "the ALID search index is not sorted by ALID, so a lookup can answer "
                "with another variant's row and a duplicate run is not contiguous",
            )
        if index_scan is not None and index_scan.entries != scan.indexable_rows:
            inconsistency(
                "alid_index_disagrees_with_table",
                f"the ALID search index holds {index_scan.entries} entries but the "
                f"variant table holds {scan.indexable_rows} rows whose ALID is narrow "
                "enough to be indexed; one of the two lost rows",
                index_entries=index_scan.entries,
                table_indexable_rows=scan.indexable_rows,
            )
        if scan.sample_mismatches:
            inconsistency(
                "alid_sample_mismatch",
                f"{scan.sample_mismatches} of the {scan.sample_checked} sampled rows "
                "carry an ALID that contradicts their own chromosome, position or "
                "allele columns, so one identity can name two variants",
                sampled=scan.sample_checked,
                mismatches=scan.sample_mismatches,
            )
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

    # -- stable view: one directory, not a moving target --------------------
    release_lock_at_end = _release_lock(path, manifest.store_id)
    swapped = False
    vanished = False
    try:
        end_stat = path.stat()
        end_parent_stat = path.parent.stat()
    except FileNotFoundError:
        vanished = True
        swapped = True
        end_stat = start_stat
        end_parent_stat = start_parent_stat
    else:
        swapped = (start_stat.st_dev, start_stat.st_ino) != (
            end_stat.st_dev,
            end_stat.st_ino,
        ) or (start_parent_stat.st_dev, start_parent_stat.st_ino) != (
            end_parent_stat.st_dev,
            end_parent_stat.st_ino,
        )
    if vanished:
        inconsistency(
            "store_vanished_during_audit",
            f"{path} no longer exists at the end of the audit; the report describes a "
            "Store that was replaced while it was being read",
            path=str(path),
        )
    elif swapped:
        inconsistency(
            "store_replaced_during_audit",
            f"the Store directory's identity changed during the audit (device/inode "
            f"{start_stat.st_dev}/{start_stat.st_ino} -> {end_stat.st_dev}/"
            f"{end_stat.st_ino}); a Reference Completion replaced it under its own id "
            "(ADR 0037) and this report spans two Stores",
            before_device=start_stat.st_dev,
            before_inode=start_stat.st_ino,
            after_device=end_stat.st_dev,
            after_inode=end_stat.st_ino,
        )
    if release_lock_at_end["held_by"] and not release_lock["held_by"]:
        inconsistency(
            "release_started_during_audit",
            "a run took this release's lock while the audit was reading it "
            f"({release_lock_at_end['held_by']}); the Store may be replaced under a "
            "report that already claimed to describe it",
            held_by=release_lock_at_end["held_by"],
        )
    stable_view = {
        "dev": start_stat.st_dev,
        "ino": start_stat.st_ino,
        "parent_dev": start_parent_stat.st_dev,
        "parent_ino": start_parent_stat.st_ino,
        "swapped": swapped,
        "release_lock": {**release_lock_at_end, "held_by_at_start": release_lock["held_by"]},
        "note": (
            "a Release Completion replaces a Store under its own id (ADR 0037), so the "
            "directory's device and inode are compared before and after, and the "
            "release lock is probed (read-only) at both ends; a lock held throughout is "
            "recorded as a finding because a long-lived orchestrator can hold one "
            "without touching this Store"
        ),
    }

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
            "build_revision": build_revision,
            "stable_view": stable_view,
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
            "n_analyses_recorded": counts["n_analyses"],
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
            "measured": [text for stage, text in MEASURED_SCOPE if stage not in skipped_stages],
            "not_measured": [
                *NOT_MEASURED,
                *[
                    {
                        "item": entry["item"],
                        "why": f"{entry['why']}; this run reports ok: false",
                        "ticket": "#372",
                    }
                    for entry in skipped
                ],
            ],
            "skipped": skipped,
        },
    }


def _count_rows(path: Path) -> int:
    """Data rows of a headerless-or-headered TSV.

    Raises rather than returning 0 for a file it cannot read: a membership table
    nobody can decode is missing evidence, not an empty release.
    """
    rows = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip() and not line.startswith("#"):
                rows += 1
    # A Store Release's analyses.tsv always carries its header row.
    return max(rows - 1, 0)


# ---------------------------------------------------------------------------
# Attribution: which Analyses and publications the off-panel union belongs to
# ---------------------------------------------------------------------------


def _mix64(values: np.ndarray, seed: int) -> np.ndarray:
    """A deterministic 64-bit hash of each variant index, mixed with `seed`.

    Sampling has to be reproducible from `--seed` alone, so a candidate's rank
    is a hash of its own axis index -- never of iteration order, band size or
    anything read from the clock. SplitMix64's finaliser is used because it
    spreads consecutive indices uniformly without carrying state.
    """
    x = np.ascontiguousarray(values).astype(np.uint64)
    x += np.uint64((int(seed) * 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF)
    x ^= x >> np.uint64(30)
    x *= np.uint64(0xBF58476D1CE4E5B9)
    x ^= x >> np.uint64(27)
    x *= np.uint64(0x94D049BB133111EB)
    x ^= x >> np.uint64(31)
    return x


def _allele_class(effect: str, other: str) -> str:
    """Classify one Store Variant Table row's allele pair.

    A variant whose two alleles are one base each is a SNP; two alleles of
    different lengths are an indel; two equal-length alleles longer than one
    base are a complex (multi-nucleotide) substitution. Whether the *site* is
    multiallelic is a property of the position's row group, not of the row, and
    is counted separately so the two facts cannot be confused.
    """
    if len(effect) == 1 and len(other) == 1:
        return "snp"
    if len(effect) != len(other):
        return "indel"
    return "complex"


def _publication_coding(analyses_path: Path) -> dict[str, Any]:
    """Code each Analysis's publication, by `analysis_index` and never by row order.

    `analyses.tsv` is the Attribute Metadata a Store Release carries, so it is
    the only source for who published an Analysis. A publication is the pmid
    when the file records one, the DOI when it does not, and an Analysis with
    neither is kept as its own explicitly unknown group -- it is never merged
    into another publication and never dropped, because a coalesced "unknown"
    would report two unrelated Analyses as one publication's. One publication
    whose Analyses disagree about its DOI or first author is ambiguous and is an
    error rather than a value picked from the disagreement (CONTRIBUTING: fail
    loudly rather than substitute).

    Every array this returns is indexed by `analysis_index`. A Store whose
    `analyses.tsv` lists its rows in another order is then still attributed to
    the Analysis the Ragged partition names, and the file order is never read as
    an index.
    """
    records = read_analysis_records(analyses_path)
    analyses: list[dict[str, Any]] = []
    publications: list[dict[str, Any]] = []
    by_key: dict[str, int] = {}
    problems: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    for position, record in enumerate(records):
        index = record.analysis_index
        if index is None:
            index = position
        index = int(index)
        pmid = (record.publication_pmid or "").strip()
        doi = (record.publication_doi or "").strip()
        author = (record.first_author or "").strip()
        if pmid:
            key, key_kind = f"pmid:{pmid}", "pmid"
        elif doi:
            key, key_kind = f"doi:{doi}", "doi"
        else:
            key, key_kind = f"unknown:{record.analysis_id}", "unknown"
            unknown.append({"analysis_index": index, "analysis_id": record.analysis_id})
        code = by_key.get(key)
        if code is None:
            code = len(publications)
            by_key[key] = code
            publications.append(
                {
                    "publication_key": key,
                    "publication_key_kind": key_kind,
                    "publication_pmid": pmid or None,
                    "publication_doi": doi or None,
                    "first_author": author or None,
                    "analysis_indices": [],
                    "_dois": set(),
                    "_authors": set(),
                }
            )
        publication = publications[code]
        publication["analysis_indices"].append(index)
        if doi:
            publication["_dois"].add(doi)
        if author:
            publication["_authors"].add(author)
        analyses.append(
            {
                "analysis_index": index,
                "analysis_id": record.analysis_id,
                "analysis_label": record.analysis_label,
                "publication_pmid": pmid or None,
                "publication_doi": doi or None,
                "first_author": author or None,
                "consortium": (record.consortium or "").strip() or None,
                "publication_code": code,
                "publication_key": key,
            }
        )
    if unknown:
        # Retained, named and counted: an unknown publication is a group this
        # attribution reports, not one it refuses to answer about.
        problems.append(
            {
                "kind": "publication_metadata_unknown",
                "severity": "finding",
                "detail": (
                    f"{len(unknown)} Analyses record neither a publication_pmid nor a "
                    "publication_doi; each is retained as its own unknown publication group "
                    "rather than merged into one"
                ),
                "counts": {
                    "analyses": len(unknown),
                    "analysis_indices": [entry["analysis_index"] for entry in unknown],
                    "analysis_ids": [entry["analysis_id"] for entry in unknown],
                },
            }
        )
    for publication in publications:
        dois = publication.pop("_dois")
        authors = publication.pop("_authors")
        publication["n_analyses"] = len(publication["analysis_indices"])
        if len(dois) > 1:
            problems.append(
                {
                    "kind": "publication_metadata_ambiguous",
                    "severity": "error",
                    "detail": (
                        f"{publication['publication_key']} is recorded with "
                        f"{len(dois)} different DOIs; one publication cannot be two"
                    ),
                    "counts": {
                        "publication_key": publication["publication_key"],
                        "dois": sorted(dois),
                    },
                }
            )
        if len(authors) > 1:
            problems.append(
                {
                    "kind": "publication_metadata_ambiguous",
                    "severity": "error",
                    "detail": (
                        f"{publication['publication_key']} is recorded with "
                        f"{len(authors)} different first authors across its Analyses"
                    ),
                    "counts": {
                        "publication_key": publication["publication_key"],
                        "first_authors": sorted(authors),
                    },
                }
            )
        if not publication["first_author"] and authors:
            publication["first_author"] = sorted(authors)[0]
    analyses.sort(key=lambda entry: entry["analysis_index"])
    indices = [entry["analysis_index"] for entry in analyses]
    if indices != list(range(len(analyses))):
        problems.append(
            {
                "kind": "analysis_index_disagrees_with_position",
                "severity": "error",
                "detail": (
                    "analyses.tsv does not number its Analyses 0..n-1, so the Ragged "
                    "component's Analysis-major partition names no Analysis this table can be "
                    "read against; attribution would name the wrong Analysis"
                ),
                "counts": {
                    "analyses_tsv_rows": len(analyses),
                    "duplicate_or_missing_indices": len(analyses) - len(set(indices)),
                },
            }
        )
    pub_codes = np.full(len(analyses), NO_CODE, dtype="int32")
    for entry in analyses:
        if 0 <= entry["analysis_index"] < pub_codes.size:
            pub_codes[entry["analysis_index"]] = entry["publication_code"]
    return {
        "analyses": analyses,
        "publications": publications,
        "pub_codes": pub_codes,
        "unknown_publications": unknown,
        "problems": problems,
    }


def _attribution_support(
    variant_index: Any,
    ragged_offsets: np.ndarray,
    pub_codes: np.ndarray,
    *,
    n_axis: int,
    band_rows: int,
) -> dict[str, Any]:
    """Fill the per-axis support planes from the Analysis-major association rows.

    For every off-panel axis entry this answers two questions exactly: how many
    distinct Analyses observe it, and how many distinct publications do. The
    answer is built by streaming each Analysis's variant list -- which is
    `ragged/variant_index` over that Analysis's row range -- in bands, so the
    working set is one band of indices plus the planes themselves, never the
    2.9B-row body.

    Counting is deduplicated by a stamp plane rather than by assuming the rows
    are ordered: each block is deduplicated within itself, and a block's entries
    are compared against the stamp the last Analysis (or publication) left, so a
    repeated or unsorted variant list is counted once and reported instead of
    silently double counted.

    Analyses are visited in **publication order**, not index order, and that is
    load-bearing rather than tidy: the publication stamp means "already counted
    for this publication", so a publication's Analyses have to be contiguous. In
    index order an Analysis of publication B can stamp an entry between two
    Analyses of publication A, and A's second Analysis then counts that entry a
    second time. Publications are visited by code and Analyses ascending within
    one, so the first Analysis to stamp an entry is a named, deterministic
    observing Analysis -- the one a sample row is attributed to.
    """
    n_analyses = int(ragged_offsets.size) - 1
    n_publications = int(pub_codes.max()) + 1 if pub_codes.size else 0
    support = np.zeros(n_axis, dtype="uint16")
    observer = np.full(n_axis, NO_CODE, dtype="uint16")
    pub_support = np.zeros(n_axis, dtype="uint16")
    pub_observer = np.full(n_axis, NO_CODE, dtype="uint16")
    seen_analysis = np.full(n_axis, NO_CODE, dtype="uint16")
    seen_publication = np.full(n_axis, NO_CODE, dtype="uint16")
    per_analysis_distinct = np.zeros(n_analyses, dtype="int64")
    per_publication_distinct = np.zeros(max(n_publications, 1), dtype="int64")
    per_publication_associations = np.zeros(max(n_publications, 1), dtype="int64")
    facts: dict[str, Any] = {
        "association_rows": 0,
        "repeated_variant_rows": 0,
        "analyses_not_strictly_increasing": 0,
        "bands_read": 0,
        "n_analyses": n_analyses,
        "n_publications": n_publications,
    }
    order = np.argsort(pub_codes[:n_analyses], kind="stable")
    for index in (int(value) for value in order):
        low = int(ragged_offsets[index])
        high = int(ragged_offsets[index + 1])
        publication = int(pub_codes[index])
        per_publication_associations[publication] += high - low
        ordered = True
        previous_last: int | None = None
        for block_low in range(low, high, band_rows):
            block_high = min(block_low + band_rows, high)
            block = np.asarray(variant_index[block_low:block_high], dtype="int64")
            facts["bands_read"] += 1
            facts["association_rows"] += int(block.size)
            if block.size > 1 and not bool(np.all(block[1:] > block[:-1])):
                ordered = False
                # Deduplicated inside the block before the stamp is consulted:
                # two rows for one variant inside one band are two stamps that
                # both read as new, and the entry would be counted twice.
                unique = np.unique(block)
                facts["repeated_variant_rows"] += int(block.size - unique.size)
                block = unique
            if previous_last is not None and block.size and int(block[0]) <= previous_last:
                ordered = False
            if block.size:
                previous_last = int(block[-1])
            fresh = block[seen_analysis[block] != index]
            if fresh.size != block.size:
                facts["repeated_variant_rows"] += int(block.size - fresh.size)
            seen_analysis[fresh] = index
            per_analysis_distinct[index] += int(fresh.size)
            first_observer = support[fresh] == 0
            observer[fresh[first_observer]] = index
            support[fresh] += 1
            fresh_publication = fresh[seen_publication[fresh] != publication]
            seen_publication[fresh_publication] = publication
            per_publication_distinct[publication] += int(fresh_publication.size)
            first_publisher = pub_support[fresh_publication] == 0
            pub_observer[fresh_publication[first_publisher]] = publication
            pub_support[fresh_publication] += 1
        if not ordered:
            facts["analyses_not_strictly_increasing"] += 1
    del seen_analysis
    del seen_publication
    return {
        "support": support,
        "observer": observer,
        "pub_support": pub_support,
        "pub_observer": pub_observer,
        "per_analysis_distinct": per_analysis_distinct,
        "per_publication_distinct": per_publication_distinct,
        "per_publication_associations": per_publication_associations,
        "facts": facts,
    }


def _attribution_counters(
    by_variant_offsets: Any,
    support: np.ndarray,
    observer: np.ndarray,
    pub_support: np.ndarray,
    pub_observer: np.ndarray,
    dense_mask: np.ndarray,
    *,
    n_axis: int,
    n_analyses: int,
    n_publications: int,
    band_variants: int,
) -> dict[str, Any]:
    """Turn the per-axis support planes into per-contributor counts.

    One band of the axis at a time: the association count per entry from
    `by_variant/offsets`, then the exclusive and support-distribution counts
    from the support planes. Every count here is exact; nothing is estimated
    from a sample.
    """
    per_analysis_exclusive = np.zeros(max(n_analyses, 1), dtype="int64")
    per_analysis_exclusive_associations = np.zeros(max(n_analyses, 1), dtype="int64")
    per_publication_exclusive = np.zeros(max(n_publications, 1), dtype="int64")
    per_publication_exclusive_associations = np.zeros(max(n_publications, 1), dtype="int64")
    per_publication_exclusive_multi = np.zeros(max(n_publications, 1), dtype="int64")
    histogram_analyses = np.zeros(len(ATTRIBUTION_SUPPORT_BUCKETS), dtype="int64")
    histogram_publications = np.zeros(len(ATTRIBUTION_SUPPORT_BUCKETS), dtype="int64")
    totals = {
        "off_panel_axis_entries": 0,
        "dense_axis_entries": 0,
        "off_panel_associations": 0,
        "dense_associations": 0,
        "observed_off_panel_variants": 0,
        "unobserved_off_panel_variants": 0,
        "observed_dense_variants": 0,
        "exclusive_to_one_analysis_variants": 0,
        "exclusive_to_one_publication_variants": 0,
        "exclusive_to_one_publication_multi_analysis_variants": 0,
        "shared_across_publications_variants": 0,
        "single_observer_variants_not_carrying_one_association": 0,
        "unattributed_single_observer_variants": 0,
        "unattributed_single_publication_variants": 0,
        "bands": 0,
    }
    for low in range(0, n_axis, band_variants):
        high = min(low + band_variants, n_axis)
        block = np.asarray(by_variant_offsets[low : high + 1], dtype="int64")
        if block.size != high - low + 1:
            raise AuditError(
                f"by_variant/offsets yielded {block.size} entries for variant band "
                f"[{low}, {high}); the axis the attribution reads is not the axis it declared"
            )
        counts = np.diff(block)
        totals["bands"] += 1
        dense = np.asarray(dense_mask[low:high], dtype=bool)
        off_panel = ~dense
        band_support = np.asarray(support[low:high])
        band_pub_support = np.asarray(pub_support[low:high])
        totals["dense_axis_entries"] += int(np.count_nonzero(dense))
        totals["off_panel_axis_entries"] += int(np.count_nonzero(off_panel))
        totals["dense_associations"] += int(counts[dense].sum())
        totals["off_panel_associations"] += int(counts[off_panel].sum())
        observed = off_panel & (band_support > 0)
        totals["observed_off_panel_variants"] += int(np.count_nonzero(observed))
        totals["unobserved_off_panel_variants"] += int(np.count_nonzero(off_panel & (band_support == 0)))
        totals["observed_dense_variants"] += int(np.count_nonzero(dense & (band_support > 0)))
        if observed.any():
            histogram_analyses += np.bincount(
                np.digitize(band_support[observed], ATTRIBUTION_SUPPORT_EDGES),
                minlength=len(ATTRIBUTION_SUPPORT_BUCKETS),
            )
        observed_publications = off_panel & (band_pub_support > 0)
        if observed_publications.any():
            histogram_publications += np.bincount(
                np.digitize(band_pub_support[observed_publications], ATTRIBUTION_SUPPORT_EDGES),
                minlength=len(ATTRIBUTION_SUPPORT_BUCKETS),
            )

        single_analysis = observed & (band_support == 1)
        totals["exclusive_to_one_analysis_variants"] += int(np.count_nonzero(single_analysis))
        totals["single_observer_variants_not_carrying_one_association"] += int(
            np.count_nonzero(single_analysis & (counts != 1))
        )
        if single_analysis.any():
            codes = np.asarray(observer[low:high])[single_analysis].astype("int64")
            unknown = int(np.count_nonzero(codes >= n_analyses))
            totals["unattributed_single_observer_variants"] += unknown
            known_analyses = codes < n_analyses
            per_analysis_exclusive += np.bincount(
                codes[known_analyses], minlength=max(n_analyses, 1)
            )
            # Associations counted, not inferred from the variant count: they
            # are the same number unless a Store lists one variant twice for one
            # Analysis, in which case the exclusive variant count is still right
            # and the association count keeps the duplicate.
            per_analysis_exclusive_associations += np.bincount(
                codes[known_analyses],
                weights=counts[single_analysis][known_analyses].astype("float64"),
                minlength=max(n_analyses, 1),
            ).astype("int64")

        single_publication = observed & (band_pub_support == 1)
        totals["exclusive_to_one_publication_variants"] += int(np.count_nonzero(single_publication))
        if single_publication.any():
            codes = np.asarray(pub_observer[low:high])[single_publication].astype("int64")
            unknown = int(np.count_nonzero(codes >= n_publications))
            totals["unattributed_single_publication_variants"] += unknown
            known = codes < n_publications
            per_publication_exclusive += np.bincount(
                codes[known], minlength=max(n_publications, 1)
            )
            per_publication_exclusive_associations += np.bincount(
                codes[known],
                weights=counts[single_publication][known].astype("float64"),
                minlength=max(n_publications, 1),
            ).astype("int64")
        multi_analysis = single_publication & (band_support > 1)
        totals["exclusive_to_one_publication_multi_analysis_variants"] += int(
            np.count_nonzero(multi_analysis)
        )
        if multi_analysis.any():
            codes = np.asarray(pub_observer[low:high])[multi_analysis].astype("int64")
            per_publication_exclusive_multi += np.bincount(
                codes[codes < n_publications], minlength=max(n_publications, 1)
            )
        totals["shared_across_publications_variants"] += int(
            np.count_nonzero(observed & (band_pub_support >= 2))
        )
    return {
        "totals": totals,
        "per_analysis_exclusive": per_analysis_exclusive,
        "per_analysis_exclusive_associations": per_analysis_exclusive_associations,
        "per_publication_exclusive": per_publication_exclusive,
        "per_publication_exclusive_associations": per_publication_exclusive_associations,
        "per_publication_exclusive_multi_analysis": per_publication_exclusive_multi,
        "histogram_analyses": {
            label: int(value)
            for label, value in zip(ATTRIBUTION_SUPPORT_BUCKETS, histogram_analyses)
        },
        "histogram_publications": {
            label: int(value)
            for label, value in zip(ATTRIBUTION_SUPPORT_BUCKETS, histogram_publications)
        },
    }


def _merge_candidates(
    pool: list[tuple[int, int]],
    hashes: np.ndarray,
    positions: np.ndarray,
    limit: int,
    *,
    offset: int = 0,
) -> None:
    """Add one band's candidates to a pool, keeping its `limit` best hashes.

    `positions` are indexes into the band's own arrays; the pool stores axis
    indices, so `offset` (the band's first axis entry) is added before anything
    is kept. Without it a candidate chosen in the second band would be recorded
    as the first band's entry, which is the one bug this pass can hide for a
    whole run.
    """
    if not positions.size:
        return
    block = hashes[positions]
    indices = positions + offset
    if positions.size > limit:
        keep = np.argpartition(block, limit - 1)[:limit]
        block = block[keep]
        indices = indices[keep]
    pool.extend(zip(block.tolist(), indices.tolist()))
    if len(pool) > limit:
        pool.sort()
        del pool[limit:]


def _choose_trace_candidates(
    support: np.ndarray,
    pub_support: np.ndarray,
    observer: np.ndarray,
    pub_observer: np.ndarray,
    dense_mask: np.ndarray,
    *,
    n_axis: int,
    analyses: Sequence[int],
    publications: Sequence[tuple[int, str]],
    per_stratum: int,
    seed: int,
    band_variants: int,
) -> dict[str, list[tuple[int, int]]]:
    """Pick each stratum's best-hashed axis entries, one band of the axis at a time.

    A stratum is a question the ticket names: the largest exclusive
    contributors, the single-publication versus multi-publication split, the
    random off-panel background, and the on-panel control. Rank is the seeded
    hash of the axis index, so the answer is a property of `--seed` and the
    Store, not of the band size, the iteration order or the machine.
    """
    pool_size = max(per_stratum * SAMPLE_POOL_MULTIPLIER, per_stratum)
    candidates: dict[str, list[tuple[int, int]]] = {}

    def pool(name: str) -> list[tuple[int, int]]:
        return candidates.setdefault(name, [])

    for low in range(0, n_axis, band_variants):
        high = min(low + band_variants, n_axis)
        indices = np.arange(low, high, dtype="int64")
        hashes = _mix64(indices, seed)
        dense = np.asarray(dense_mask[low:high], dtype=bool)
        band_support = np.asarray(support[low:high])
        band_observer = np.asarray(observer[low:high])
        off_panel = ~dense
        observed = off_panel & (band_support > 0)

        def positions(mask: np.ndarray) -> np.ndarray:
            """A band mask's entries, as indexes into the band's own arrays."""
            return np.flatnonzero(mask)

        _merge_candidates(
            pool("random_off_panel"), hashes, positions(observed), pool_size, offset=low
        )
        _merge_candidates(
            pool("on_panel_control"), hashes, positions(dense), pool_size, offset=low
        )
        _merge_candidates(
            pool("shared_multi_publication"),
            hashes,
            positions(observed & (np.asarray(pub_support[low:high]) >= 2)),
            pool_size,
            offset=low,
        )
        single_analysis = observed & (band_support == 1)
        if single_analysis.any():
            for analysis in analyses:
                name = f"exclusive_analysis:{int(analysis)}"
                _merge_candidates(
                    pool(name),
                    hashes,
                    positions(single_analysis & (band_observer == analysis)),
                    pool_size,
                    offset=low,
                )
        single_publication = observed & (np.asarray(pub_support[low:high]) == 1) & (band_support > 1)
        if single_publication.any():
            band_pub_observer = np.asarray(pub_observer[low:high])
            for code, label in publications:
                name = f"exclusive_publication:{label}"
                _merge_candidates(
                    pool(name),
                    hashes,
                    positions(single_publication & (band_pub_observer == code)),
                    pool_size,
                    offset=low,
                )
    return candidates


def _stratified_pick(
    candidates: Sequence[dict[str, Any]], per_stratum: int
) -> list[dict[str, Any]]:
    """Take `per_stratum` of them, round-robin over the allele classes.

    The pool is ranked by hash, so taking the same allele class first would fill
    a stratum with SNPs whenever SNPs are common. Round-robin makes a rare class
    reach the sample while leaving the per-class choice hash-ranked, which is
    what makes an indel trace possible at all.
    """
    buckets: dict[str, list[dict[str, Any]]] = {name: [] for name in ALLELE_CLASSES}
    for candidate in candidates:
        buckets.setdefault(candidate["allele_class"], []).append(candidate)
    taken: list[dict[str, Any]] = []
    while len(taken) < per_stratum:
        progressed = False
        for name in ALLELE_CLASSES:
            if len(taken) >= per_stratum:
                break
            bucket = buckets.get(name) or []
            if bucket:
                taken.append(bucket.pop(0))
                progressed = True
        if not progressed:
            break
    return taken


def _trace_rows(
    variant_index: Any,
    ragged_offsets: np.ndarray,
    wanted: dict[int, set[int]],
) -> tuple[dict[tuple[int, int], dict[str, Any]], int]:
    """Locate each sample row's Analysis-major row, exactly.

    The sample is for raw-row tracing (#374), so a hint that needs a scan of a
    44M-row Analysis is worth less than the row itself. `ragged/variant_index`
    within one Analysis is ordered in every Store measured so far, which makes
    the row a binary search; when it is not, the block is scanned and the hint
    says so. Either way the row is read back and checked, and a variant the
    Analysis's own row range does not hold is reported as absent rather than
    guessed at.
    """
    located: dict[tuple[int, int], dict[str, Any]] = {}
    rows_read = 0
    for analysis in sorted(wanted):
        low = int(ragged_offsets[analysis])
        high = int(ragged_offsets[analysis + 1])
        block = np.asarray(variant_index[low:high])
        rows_read += int(block.size)
        ordered = block.size < 2 or bool(np.all(block[1:] > block[:-1]))
        for variant in sorted(wanted[analysis]):
            if ordered:
                position = int(np.searchsorted(block, variant))
                found = position if position < block.size and int(block[position]) == variant else None
                mode = "binary_search"
            else:
                hits = np.flatnonzero(block == variant)
                found = int(hits[0]) if hits.size else None
                mode = "scan"
            located[(analysis, variant)] = {
                "analysis_major_row": None if found is None else low + found,
                "analysis_major_range": [low, high],
                "analysis_rows_ordered": ordered,
                "mode": mode,
                "verified": found is not None,
            }
    return located, rows_read


def _on_panel_controls(
    dense_z: Any,
    dense_to_shared: np.ndarray,
    rows: Sequence[dict[str, Any]],
    control_analyses: Sequence[int],
    pub_codes: np.ndarray,
    analyses_by_index: dict[int, dict[str, Any]],
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Name the Dense Component cells an on-panel control variant occupies.

    The Dense Component and the Ragged Overflow partition the axis, so an
    on-panel variant carries no Overflow association and has no Analysis to be
    attributed to from the ragged body. Its Analyses are in `dense/z` instead:
    one cell per (panel variant, Analysis), read here a row at a time. Naming
    them makes the control comparable with the off-panel sample -- the same
    Analyses, a variant the panel holds -- instead of a bare variant list.

    Every row this returns names an Analysis, because a row no trace can
    attribute is not a starting point: the contributors of interest are named
    where they hold the cell, and where none of them does, the lowest-indexed
    Analysis holding it is named instead and counted as a fallback. A panel
    variant no Analysis holds at all is skipped and counted, never emitted with
    a blank where its owner should be.
    """
    facts: dict[str, Any] = {
        "available": dense_z is not None,
        "rows": 0,
        "panel_variants": len(rows),
        "panel_variants_skipped": 0,
        "rows_naming_a_fallback_analysis": 0,
    }
    controls: list[dict[str, Any]] = []
    if dense_z is None:
        # Without the plane the cells cannot be read, and inventing an Analysis
        # for a panel variant would be exactly the wrong answer that looks like
        # a right one. The report says the plane was unavailable.
        facts["panel_variants_skipped"] = len(rows)
        return controls, facts
    mapping = np.asarray(dense_to_shared)
    for row in rows:
        variant = int(row["variant_index"])
        dense_row = int(np.searchsorted(mapping, variant))
        if dense_row >= mapping.size or int(mapping[dense_row]) != variant:
            facts["panel_variants_skipped"] += 1
            continue
        plane = np.asarray(dense_z[dense_row, :])
        observed = np.flatnonzero(plane != Z_MISSING).astype("int64")
        if not observed.size:
            facts["panel_variants_skipped"] += 1
            continue
        observed_set = set(int(value) for value in observed)
        observed_publications = int(
            np.unique(pub_codes[observed[observed < pub_codes.size]]).size
        )
        named = [analysis for analysis in control_analyses if analysis in observed_set]
        fallback = not named
        if fallback:
            # The cell is held, so the control is still a cell: name the
            # lowest-indexed Analysis that holds it rather than nothing.
            named = [int(observed[0])]
            facts["rows_naming_a_fallback_analysis"] += len(named)
        for analysis in named[:limit]:
            entry = analyses_by_index.get(int(analysis)) or {}
            controls.append(
                {
                    **row,
                    "analysis_index": int(analysis),
                    "analysis_id": entry.get("analysis_id"),
                    "n_analyses": int(observed.size),
                    "n_publications": observed_publications,
                    "source_row_lookup_hint": (
                        f"dense_row={dense_row};dense_plane=z;analysis_index={int(analysis)}"
                    ),
                }
            )
    facts["rows"] = len(controls)
    return controls, facts


def _write_tsv(
    rows: Sequence[dict[str, Any]],
    output: Path,
    store_path: Path,
    *,
    columns: Sequence[str] = SAMPLE_COLUMNS,
) -> str:
    """Write one evidence TSV atomically and return the sha256 of what was written.

    Same discipline as the evidence JSON: an unguessable staged name in the
    output's own directory, checked against the Store before a byte is written,
    renamed onto the output so a reader never sees half a table.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    handle, staged_name = tempfile.mkstemp(dir=output.parent, prefix=f".{output.name}.", suffix=".tmp")
    staged = Path(staged_name)
    try:
        if _inside(staged, store_path):
            raise AuditError(
                f"refusing to write a sample inside the Store ({staged} is under {store_path}); "
                "a read-only audit writes nothing there"
            )
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write("\t".join(columns) + "\n")
            for row in rows:
                stream.write(
                    "\t".join(
                        "" if row.get(column) is None else str(row[column]) for column in columns
                    )
                    + "\n"
                )
            stream.flush()
            os.fsync(stream.fileno())
        staged.replace(output)
        staged = None
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
    return _sha256(output)


def _rank(
    values: Sequence[float], indices: Sequence[int] | np.ndarray, limit: int
) -> list[int]:
    """The `limit` largest by value, ties broken by the entity's own index."""
    order = sorted(
        range(len(values)), key=lambda i: (-float(values[i]), int(indices[i]))
    )
    return [int(indices[i]) for i in order[:limit]]


def _union_share(
    variant_index: Any,
    ragged_offsets: np.ndarray,
    lists: Sequence[Sequence[int]],
    *,
    n_axis: int,
    checkpoints: Sequence[int],
) -> dict[str, Any]:
    """The exact union of the first `n` contributors' off-panel variant lists.

    A ranking says how much each contributor holds; the epic asks how much of
    the union the leaders explain, which is not the sum -- two Barnes Analyses
    share most of their variants. The union is accumulated in one boolean plane
    over the axis and counted at each checkpoint, so the shares are exact.
    """
    mask = np.zeros(n_axis, dtype=bool)
    counts: dict[str, int] = {}
    rows_read = 0
    wanted = set(int(value) for value in checkpoints)
    for position, analyses in enumerate(lists, start=1):
        for analysis in analyses:
            low = int(ragged_offsets[analysis])
            high = int(ragged_offsets[analysis + 1])
            block = np.asarray(variant_index[low:high])
            rows_read += int(block.size)
            mask[block] = True
        # The last prefix is always recorded: a Store with fewer contributors
        # than a checkpoint asked for still has a total to report.
        if position in wanted or position == len(lists):
            counts[str(position)] = int(np.count_nonzero(mask))
    del mask
    return {"union_variants_by_prefix": counts,
            "analyses_in_prefix": [list(entry) for entry in lists],
            "rows_read": rows_read}


def _scan_variant_breakdown(table_path: Path, dense_mask: np.ndarray) -> dict[str, Any]:
    """Chromosome and allele class of the off-panel axis, from one table stream.

    The Store's variant table is position-ordered, so one pass answers both
    questions and the position grouping is a comparison against the previous
    row rather than an index. Only off-panel entries are counted: the panel's
    composition is the Dense Component's question, not this one.
    """
    chromosomes: dict[str, dict[str, int]] = {}
    allele_totals = {name: 0 for name in ALLELE_CLASSES}
    multi_row_positions = 0
    rows_at_multi_row_positions = 0
    off_panel_rows = 0
    rows = 0
    group_chromosome: str | None = None
    group_position: int | None = None
    group_rows = 0
    def close_group() -> None:
        nonlocal multi_row_positions, rows_at_multi_row_positions
        if group_rows > 1:
            multi_row_positions += 1
            rows_at_multi_row_positions += group_rows
    for record in iter_variant_records(table_path):
        rows += 1
        index = record.variant_index
        if index < 0 or index >= dense_mask.size or dense_mask[index]:
            group_chromosome = None
            group_position = None
            group_rows = 0
            continue
        off_panel_rows += 1
        if record.chromosome == group_chromosome and record.position == group_position:
            group_rows += 1
        else:
            close_group()
            group_chromosome = record.chromosome
            group_position = record.position
            group_rows = 1
        name = _allele_class(record.effect_allele, record.other_allele)
        allele_totals[name] += 1
        per_chromosome = chromosomes.setdefault(
            record.chromosome, {key: 0 for key in ALLELE_CLASSES}
        )
        per_chromosome[name] += 1
    close_group()
    return {
        "rows_read": rows,
        "off_panel_rows": off_panel_rows,
        "allele_classes": allele_totals,
        "chromosomes": {name: dict(sorted(value.items())) for name, value in sorted(chromosomes.items())},
        "positions_with_multiple_off_panel_rows": multi_row_positions,
        "rows_at_multi_row_positions": rows_at_multi_row_positions,
    }


def _contributor_tables(
    analyses: Sequence[dict[str, Any]],
    publications: Sequence[dict[str, Any]],
    per_analysis_associations: np.ndarray,
    per_analysis_distinct: np.ndarray,
    per_analysis_exclusive: np.ndarray,
    per_analysis_exclusive_associations: np.ndarray,
    per_publication_associations: np.ndarray,
    per_publication_distinct: np.ndarray,
    per_publication_exclusive: np.ndarray,
    per_publication_exclusive_associations: np.ndarray,
    per_publication_exclusive_multi: np.ndarray,
    *,
    limit: int,
) -> dict[str, Any]:
    """Both rankings, by each measure the ticket asks for."""
    analysis_indices = [entry["analysis_index"] for entry in analyses]
    analysis_by_index = {entry["analysis_index"]: entry for entry in analyses}
    publication_indices = list(range(len(publications)))

    def analysis_row(index: int) -> dict[str, Any]:
        entry = analysis_by_index[index]
        return {
            "analysis_index": index,
            "analysis_id": entry["analysis_id"],
            "analysis_label": entry["analysis_label"],
            "publication_key": entry["publication_key"],
            "publication_key_kind": publications[entry["publication_code"]]["publication_key_kind"],
            "publication_pmid": entry["publication_pmid"],
            "publication_doi": entry["publication_doi"],
            "first_author": entry["first_author"],
            "consortium": entry["consortium"],
            "off_panel_associations": int(per_analysis_associations[index]),
            "distinct_off_panel_variants": int(per_analysis_distinct[index]),
            "exclusive_variants": int(per_analysis_exclusive[index]),
            "exclusive_associations": int(per_analysis_exclusive_associations[index]),
        }

    def publication_row(code: int) -> dict[str, Any]:
        entry = publications[code]
        return {
            "publication_key": entry["publication_key"],
            "publication_key_kind": entry["publication_key_kind"],
            "publication_pmid": entry["publication_pmid"],
            "publication_doi": entry["publication_doi"],
            "first_author": entry["first_author"],
            "n_analyses": int(entry["n_analyses"]),
            "off_panel_associations": int(per_publication_associations[code]),
            "distinct_off_panel_variants": int(per_publication_distinct[code]),
            "exclusive_variants": int(per_publication_exclusive[code]),
            "exclusive_associations": int(per_publication_exclusive_associations[code]),
            "exclusive_variants_observed_by_several_analyses": int(
                per_publication_exclusive_multi[code]
            ),
        }

    every_analysis = [analysis_row(index) for index in sorted(analysis_by_index)]
    every_publication = [publication_row(code) for code in publication_indices]
    return {
        "full_analyses": every_analysis,
        "full_publications": every_publication,
        "analyses": {
            "by_exclusive_variants": [
                analysis_row(index)
                for index in _rank(per_analysis_exclusive, analysis_indices, limit)
            ],
            "by_off_panel_associations": [
                analysis_row(index)
                for index in _rank(per_analysis_associations, analysis_indices, limit)
            ],
            "by_distinct_off_panel_variants": [
                analysis_row(index)
                for index in _rank(per_analysis_distinct, analysis_indices, limit)
            ],
        },
        "publications": {
            "by_exclusive_variants": [
                publication_row(code)
                for code in _rank(per_publication_exclusive, publication_indices, limit)
            ],
            "by_off_panel_associations": [
                publication_row(code)
                for code in _rank(per_publication_associations, publication_indices, limit)
            ],
            "by_distinct_off_panel_variants": [
                publication_row(code)
                for code in _rank(per_publication_distinct, publication_indices, limit)
            ],
        },
    }


def attribute(
    store_path: str | Path,
    *,
    sample_output: str | Path | None = None,
    tables_dir: str | Path | None = None,
    seed: int = DEFAULT_ATTRIBUTION_SEED,
    top: int = DEFAULT_ATTRIBUTION_TOP,
    sample_per_stratum: int = DEFAULT_SAMPLE_PER_STRATUM,
    band_variants: int = DEFAULT_ATTRIBUTION_BAND_VARIANTS,
    band_rows: int = DEFAULT_ATTRIBUTION_BAND_ROWS,
    verify: str = "auto",
    argv: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Attribute a Hybrid Store Release's off-panel union to its contributors.

    What this answers, exactly: for every off-panel axis entry, which Analyses
    and which publications observe it; how many of them are observed by one
    Analysis or one publication and nothing else; how many contributors each
    entry has; the chromosome and allele class of the union; and whether the
    attributed union is the census the Store records. A seeded sample names rows
    a raw-row trace can start from (#374).

    Nothing here admits, filters or repairs, and nothing is written inside the
    Store: the support planes are read-only views of `ragged/` and `by_variant/`,
    and the only files written are the evidence JSON and the sample TSV.
    """
    started = time.time()
    started_at = _now()
    path = Path(store_path)
    if not (path / "manifest.json").is_file():
        raise AuditError(f"{path} holds no manifest.json; it is not a Store Release")
    sample_path = None if sample_output is None else Path(sample_output)

    method = _verify_method(path, verify)
    before = _inventory(path, method)
    start_stat = path.stat()

    inconsistencies: list[dict[str, Any]] = [
        {
            "kind": "scope_not_measured",
            "severity": "finding",
            "detail": entry["item"],
            "ticket": entry["ticket"],
            "counts": {"why": entry["why"]},
        }
        for entry in ATTRIBUTION_NOT_MEASURED
    ]

    def inconsistency(kind: str, detail: str, *, severity: str = "error", **counts: Any) -> None:
        inconsistencies.append(
            {"kind": kind, "severity": severity, "detail": detail, "counts": counts}
        )

    store = open_store(path)
    manifest = store.manifest
    if manifest.primary_layout.value != "hybrid":
        raise AuditError(
            f"{path} is a {manifest.primary_layout.value} Store Release; attribution answers "
            "who contributed a Hybrid Store's Ragged Overflow, and a Store without one has "
            "nothing here to attribute"
        )
    group = store.arrays(mode="r")
    recorded = _recorded(manifest.provenance)
    release_lock = _release_lock(path, manifest.store_id)
    if release_lock["held_by"]:
        inconsistency(
            "release_lock_held",
            f"a run holds this release's lock ({release_lock['held_by']}); the Store may be "
            "replaced under this audit",
            severity="finding",
            store_id=release_lock["store_id"],
            held_by=release_lock["held_by"],
        )

    axis_handle = VariantAxis(path)
    try:
        n_axis = axis_handle.n_variants
    finally:
        pass
    dense_to_shared = np.load(dense_to_shared_path(path), mmap_mode="r")
    dense_mask = np.zeros(n_axis, dtype=bool)
    mapped = np.asarray(dense_to_shared)
    mapped = mapped[(mapped >= 0) & (mapped < n_axis)]
    dense_mask[mapped] = True
    n_dense = int(np.count_nonzero(dense_mask))
    n_off_panel = n_axis - n_dense
    del mapped

    ragged_offsets = np.asarray(_array(group, "ragged/offsets")[:], dtype="int64")
    if ragged_offsets.size < 2:
        raise AuditError(f"{path} holds a Ragged Overflow with no Analysis offsets")
    n_analyses = int(ragged_offsets.size) - 1
    total_analyses_associations = int(ragged_offsets[-1])

    analyses_tsv = path / "analyses.tsv"
    if not analyses_tsv.is_file():
        raise AuditError(
            f"{analyses_tsv} is absent; without the release's own membership table nothing can "
            "be attributed to an Analysis or a publication"
        )
    try:
        coding = _publication_coding(analyses_tsv)
    except (OSError, UnicodeDecodeError) as exc:
        raise AuditError(f"{analyses_tsv} cannot be read ({exc}); attribution needs it") from None
    analyses = coding["analyses"]
    publications = coding["publications"]
    pub_codes = coding["pub_codes"]
    for entry in coding["problems"]:
        inconsistency(entry["kind"], entry["detail"], severity=entry["severity"], **entry["counts"])
    if len(analyses) != n_analyses:
        inconsistency(
            "analyses_tsv_disagrees_with_component",
            f"analyses.tsv holds {len(analyses)} Analyses but the Ragged component is "
            f"partitioned into {n_analyses}; attribution would name the wrong Analysis",
            analyses_tsv_rows=len(analyses),
            component_analyses=n_analyses,
        )
    if int(pub_codes.size) != n_analyses:
        raise AuditError(
            f"publication coding covers {pub_codes.size} of the {n_analyses} Analyses the Ragged "
            "component holds; attribution cannot name a publication for the rest"
        )
    if bool((pub_codes < 0).any()) or bool((pub_codes >= len(publications)).any()):
        raise AuditError(
            "analyses.tsv does not number its Analyses 0..n-1, so its publication codes cannot "
            "be read against the Ragged component's Analysis-major partition; naming a "
            "publication for those rows would be a guess"
        )

    by_variant_offsets = _array(group, "ragged/by_variant/offsets")
    if array_length(by_variant_offsets) != n_axis + 1:
        raise AuditError(
            f"ragged/by_variant/offsets holds {array_length(by_variant_offsets)} entries, not the "
            f"{n_axis + 1} a {n_axis}-variant axis needs"
        )

    # -- who observes what --------------------------------------------------
    scan = _attribution_support(
        _array(group, "ragged/variant_index"),
        ragged_offsets,
        pub_codes,
        n_axis=n_axis,
        band_rows=band_rows,
    )
    facts = scan["facts"]
    if facts["association_rows"] != total_analyses_associations:
        inconsistency(
            "association_rows_disagree",
            f"the Ragged body yielded {facts['association_rows']} association rows but its "
            f"own offsets end at {total_analyses_associations}",
            rows_read=facts["association_rows"],
            offsets_last=total_analyses_associations,
        )
    if facts["analyses_not_strictly_increasing"]:
        inconsistency(
            "analysis_variant_lists_not_increasing",
            f"{facts['analyses_not_strictly_increasing']} Analysis variant lists in "
            "ragged/variant_index are not strictly increasing; counts here are deduplicated "
            "arithmetic rather than a sorted-run shortcut",
            severity="finding",
            analyses=facts["analyses_not_strictly_increasing"],
        )
    if facts["repeated_variant_rows"]:
        inconsistency(
            "repeated_variant_rows_in_one_analysis",
            f"{facts['repeated_variant_rows']} rows repeat a variant already listed by the same "
            "Analysis; support counts it once and the stored association count keeps it twice",
            severity="finding",
            rows=facts["repeated_variant_rows"],
        )

    counters = _attribution_counters(
        by_variant_offsets,
        scan["support"],
        scan["observer"],
        scan["pub_support"],
        scan["pub_observer"],
        dense_mask,
        n_axis=n_axis,
        n_analyses=n_analyses,
        n_publications=len(publications),
        band_variants=band_variants,
    )
    totals = counters["totals"]
    if totals["dense_associations"]:
        inconsistency(
            "dense_axis_rows_carry_overflow_associations",
            f"{totals['observed_dense_variants']} Dense-axis variants carry "
            f"{totals['dense_associations']} Overflow associations; the Dense Component and the "
            "Ragged Overflow must partition the axis",
            dense_axis_associations=totals["dense_associations"],
        )
    if totals["off_panel_associations"] != total_analyses_associations:
        inconsistency(
            "off_panel_associations_disagree",
            f"the off-panel partition carries {totals['off_panel_associations']} associations but "
            f"the Analysis-major component holds {total_analyses_associations}",
            off_panel=totals["off_panel_associations"],
            component=total_analyses_associations,
        )
    if totals["single_observer_variants_not_carrying_one_association"]:
        inconsistency(
            "single_observer_variants_carry_several_associations",
            f"{totals['single_observer_variants_not_carrying_one_association']} variants observed "
            "by exactly one Analysis carry more than one association, so an exclusive variant "
            "count and an exclusive association count are not the same number here; the counts "
            "are kept apart rather than one standing in for the other",
            severity="finding",
            variants=totals["single_observer_variants_not_carrying_one_association"],
        )
    for key in (
        "unattributed_single_observer_variants",
        "unattributed_single_publication_variants",
    ):
        if totals[key]:
            inconsistency(
                "support_without_an_observer",
                f"{totals[key]} entries have a support of one but no recorded observer; the "
                "support plane and the observer plane disagree",
                entries=totals[key],
            )
    census_recorded = _count(recorded, "n_off_panel")
    census_observed = totals["observed_off_panel_variants"]
    if census_recorded is not None and census_recorded != n_off_panel:
        inconsistency(
            "recorded_off_panel_disagrees",
            f"the Store records {census_recorded} off-panel variants but its axis minus its Dense "
            f"Component is {n_off_panel}",
            recorded_n_off_panel=census_recorded,
            observed_n_off_panel=n_off_panel,
        )
    if totals["unobserved_off_panel_variants"]:
        inconsistency(
            "unobserved_off_panel_entries",
            f"{totals['unobserved_off_panel_variants']} off-panel axis entries are observed by no "
            "Analysis, so they belong to no contributor and to no raw row",
            severity="finding",
            unobserved=totals["unobserved_off_panel_variants"],
            n_off_panel=n_off_panel,
        )

    per_analysis_associations = np.diff(ragged_offsets)
    per_analysis_exclusive = counters["per_analysis_exclusive"]
    per_analysis_distinct = scan["per_analysis_distinct"]
    per_publication_exclusive = counters["per_publication_exclusive"]
    exclusive_analysis_sum = int(per_analysis_exclusive.sum())
    exclusive_publication_sum = int(per_publication_exclusive.sum())
    if exclusive_analysis_sum != totals["exclusive_to_one_analysis_variants"]:
        inconsistency(
            "exclusive_analysis_attribution_residual",
            f"the per-Analysis exclusive counts add up to {exclusive_analysis_sum} but "
            f"{totals['exclusive_to_one_analysis_variants']} variants have one observer",
            per_analysis=exclusive_analysis_sum,
            observed=totals["exclusive_to_one_analysis_variants"],
        )
    if exclusive_publication_sum != totals["exclusive_to_one_publication_variants"]:
        inconsistency(
            "exclusive_publication_attribution_residual",
            f"the per-publication exclusive counts add up to {exclusive_publication_sum} but "
            f"{totals['exclusive_to_one_publication_variants']} variants have one publication",
            per_publication=exclusive_publication_sum,
            observed=totals["exclusive_to_one_publication_variants"],
        )
    if not (exclusive_analysis_sum <= census_observed <= totals["off_panel_associations"]):
        inconsistency(
            "attribution_arithmetic_violated",
            "the exclusivity arithmetic does not hold: exclusive variants must not exceed the "
            "attributed union, which must not exceed the association count",
            exclusive_analysis=exclusive_analysis_sum,
            union=census_observed,
            associations=totals["off_panel_associations"],
        )

    # -- rankings -----------------------------------------------------------
    tables = _contributor_tables(
        analyses,
        publications,
        per_analysis_associations,
        per_analysis_distinct,
        per_analysis_exclusive,
        counters["per_analysis_exclusive_associations"],
        scan["per_publication_associations"],
        scan["per_publication_distinct"],
        per_publication_exclusive,
        counters["per_publication_exclusive_associations"],
        counters["per_publication_exclusive_multi_analysis"],
        limit=DEFAULT_ATTRIBUTION_RANKING,
    )

    # -- the full contributor tables ---------------------------------------
    full_analyses = tables.pop("full_analyses")
    full_publications = tables.pop("full_publications")
    tables["note"] = (
        "the rankings below are the leaders; the full per-Analysis and per-publication "
        "tables are written beside the evidence JSON"
    )
    tables["tables"] = {}
    if tables_dir is not None:
        for name, rows_for_table, columns in (
            ("analyses", full_analyses, ANALYSIS_TABLE_COLUMNS),
            ("publications", full_publications, PUBLICATION_TABLE_COLUMNS),
        ):
            table_path = Path(tables_dir) / f"{name}.tsv"
            digest = _write_tsv(rows_for_table, table_path, path, columns=columns)
            tables["tables"][name] = {
                "path": str(table_path),
                "sha256": digest,
                "rows": len(rows_for_table),
                "columns": list(columns),
            }

    # -- how much of the union the leaders explain --------------------------
    # Two rankings, two questions. `concentration` asks who holds the union, so
    # it ranks by distinct off-panel variants; the sample asks whose exclusive
    # variants to trace, so it ranks by exclusive variants -- a wide contributor
    # that shares everything it holds is not the same contributor as one whose
    # variants nobody else observes.
    top_analyses = [row["analysis_index"] for row in tables["analyses"]["by_distinct_off_panel_variants"][:top]]
    top_publications = [row["publication_key"] for row in tables["publications"]["by_distinct_off_panel_variants"][:top]]
    exclusive_analyses = [row["analysis_index"] for row in tables["analyses"]["by_exclusive_variants"][:top]]
    exclusive_publications = [row["publication_key"] for row in tables["publications"]["by_exclusive_variants"][:top]]
    publication_analyses = {
        entry["publication_key"]: sorted(entry["analysis_indices"]) for entry in publications
    }
    checkpoints = tuple(sorted({1, 2, 5, 10, top}))
    variant_index_array = _array(group, "ragged/variant_index")
    analysis_union = _union_share(
        variant_index_array,
        ragged_offsets,
        [[index] for index in top_analyses],
        n_axis=n_axis,
        checkpoints=checkpoints,
    )
    publication_union = _union_share(
        variant_index_array,
        ragged_offsets,
        [publication_analyses[key] for key in top_publications],
        n_axis=n_axis,
        checkpoints=checkpoints,
    )

    def shares(union: dict[str, Any]) -> dict[str, Any]:
        out = {
            "contributors": union["analyses_in_prefix"],
            "union_variants_by_prefix": union["union_variants_by_prefix"],
            "share_of_union_by_prefix": {
                key: (round(value / census_observed, 6) if census_observed else None)
                for key, value in union["union_variants_by_prefix"].items()
            },
            "rows_read": union["rows_read"],
        }
        return out

    concentration = {
        "top_analyses": shares(analysis_union),
        "top_publications": shares(publication_union),
        "numerator": "distinct off-panel variants of the prefix's union, not the sum of theirs",
    }

    # -- the sample ---------------------------------------------------------
    sample_facts: dict[str, Any] = {"written": False}
    sample_rows: list[dict[str, Any]] = []
    if sample_path is not None:
        candidate_analyses = exclusive_analyses
        publication_code = {entry["publication_key"]: code for code, entry in enumerate(publications)}
        candidate_publications = [(publication_code[key], key) for key in exclusive_publications]
        candidates = _choose_trace_candidates(
            scan["support"],
            scan["pub_support"],
            scan["observer"],
            scan["pub_observer"],
            dense_mask,
            n_axis=n_axis,
            analyses=candidate_analyses,
            publications=candidate_publications,
            per_stratum=sample_per_stratum,
            seed=seed,
            band_variants=band_variants,
        )
        wanted = sorted({variant for entries in candidates.values() for _, variant in entries})
        try:
            records = axis_handle.by_indices(wanted)
        except ValueError as exc:
            raise AuditError(f"the Store Variant Table cannot resolve a sampled row ({exc})") from None
        missing = [index for index in wanted if index not in records]
        if missing:
            inconsistency(
                "sample_variant_unresolvable",
                f"{len(missing)} sampled axis entries have no Store Variant Table row",
                entries=len(missing),
                examples=missing[:EXAMPLE_LIMIT],
            )
        support_planes = scan["support"]
        pub_support_planes = scan["pub_support"]
        publication_by_code = {code: entry for code, entry in enumerate(publications)}
        analysis_by_index = {entry["analysis_index"]: entry for entry in analyses}
        picked: list[dict[str, Any]] = []
        for stratum in sorted(candidates):
            prepared = []
            for candidate_hash, variant in sorted(candidates[stratum]):
                record = records.get(variant)
                if record is None:
                    continue
                prepared.append(
                    {
                        "hash": candidate_hash,
                        "variant_index": int(variant),
                        "chromosome": record.chromosome,
                        "allele_class": _allele_class(record.effect_allele, record.other_allele),
                        "alid": record.alid,
                        "rsid": record.rsid,
                    }
                )
            for candidate in _stratified_pick(prepared, sample_per_stratum):
                variant = candidate["variant_index"]
                on_panel = bool(dense_mask[variant])
                observation = None if on_panel else int(scan["observer"][variant])
                picked.append(
                    {
                        "stratum": stratum,
                        "variant_index": variant,
                        "chromosome": candidate["chromosome"],
                        "allele_class": candidate["allele_class"],
                        "alid": candidate["alid"],
                        "rsid": candidate["rsid"],
                        "on_panel": on_panel,
                        "analysis_index": observation,
                        "analysis_id": (
                            analysis_by_index[observation]["analysis_id"]
                            if observation is not None
                            else None
                        ),
                        "n_analyses": int(support_planes[variant]),
                        "n_publications": int(pub_support_planes[variant]),
                        "publication_pmid": (
                            analysis_by_index[observation]["publication_pmid"]
                            if observation is not None
                            else None
                        ),
                    }
                )
        del records
        off_panel_picked = [row for row in picked if not row["on_panel"]]
        wanted_rows: dict[int, set[int]] = {}
        for row in off_panel_picked:
            wanted_rows.setdefault(int(row["analysis_index"]), set()).add(int(row["variant_index"]))
        located, trace_rows_read = _trace_rows(variant_index_array, ragged_offsets, wanted_rows)
        for row in off_panel_picked:
            hint = located.get((int(row["analysis_index"]), int(row["variant_index"])))
            if hint is None or hint["analysis_major_row"] is None:
                row["source_row_lookup_hint"] = "analysis_major_row=absent"
                continue
            row["source_row_lookup_hint"] = (
                f"analysis_major_row={hint['analysis_major_row']}"
                f";analysis_major_range={hint['analysis_major_range'][0]}-{hint['analysis_major_range'][1]}"
                f";variant_index={row['variant_index']}"
                f";search={hint['mode']}"
            )
        if any(
            hint["analysis_major_row"] is None for hint in located.values()
        ):
            inconsistency(
                "sample_trace_row_absent",
                "a sampled variant is not in the Analysis its own support plane names, so no raw "
                "row can be traced to it",
                entries=sum(1 for hint in located.values() if hint["analysis_major_row"] is None),
            )
        on_panel_picked = [row for row in picked if row["on_panel"]]
        # The Analyses the control compares against: everyone the off-panel
        # sample names, plus the Analyses behind the publication strata it drew
        # from. A control on an Analysis the sample never mentions would compare
        # nothing with nothing.
        # Ordered by who matters, not by index: the leading exclusive
        # contributors first, then the Analyses behind the publication strata,
        # then whoever else the sample named. A control on an Analysis the
        # sample is not about would compare nothing with nothing.
        control_order: list[int] = []
        for index in exclusive_analyses:
            control_order.append(int(index))
        for code, _label in candidate_publications:
            control_order.extend(
                int(index) for index in sorted(publications[code]["analysis_indices"])
            )
        for row in off_panel_picked:
            if row["analysis_index"] is not None:
                control_order.append(int(row["analysis_index"]))
        control_analyses: list[int] = []
        for index in control_order:
            if index not in control_analyses:
                control_analyses.append(index)
        control_analyses = control_analyses[:DEFAULT_ON_PANEL_ANALYSES]
        dense_z = None
        try:
            dense_store = open_store(dense_component_path(path))
            dense_arrays = dense_store.arrays(mode="r")
            dense_z = dense_arrays["z"] if "z" in dense_arrays else None
        except (AuditError, KeyError, OSError):
            dense_z = None
        controls, control_facts = _on_panel_controls(
            dense_z,
            dense_to_shared,
            [
                {
                    "stratum": row["stratum"],
                    "variant_index": row["variant_index"],
                    "chromosome": row["chromosome"],
                    "allele_class": row["allele_class"],
                    "alid": row["alid"],
                    "rsid": row["rsid"],
                    "on_panel": True,
                    "n_analyses": row["n_analyses"],
                    "n_publications": row["n_publications"],
                }
                for row in on_panel_picked
            ],
            control_analyses,
            pub_codes,
            analysis_by_index,
            limit=DEFAULT_ON_PANEL_ANALYSES,
        )
        for row in controls:
            analysis_index = row.get("analysis_index")
            entry = analysis_by_index.get(analysis_index) if analysis_index is not None else None
            row["analysis_id"] = entry["analysis_id"] if entry else None
            row["publication_pmid"] = entry["publication_pmid"] if entry else None
        unlabelled = [
            row for row in (*off_panel_picked, *controls) if not row.get("analysis_id")
        ]
        if unlabelled:
            inconsistency(
                "sample_row_without_an_analysis",
                f"{len(unlabelled)} sample rows name no Analysis; a row a trace cannot attribute "
                "is not a starting point",
                rows=len(unlabelled),
            )
        sample_rows = off_panel_picked + controls
        for position, row in enumerate(
            sorted(
                sample_rows,
                key=lambda entry: (
                    entry["stratum"],
                    int(entry["variant_index"]),
                    -1 if entry.get("analysis_index") is None else int(entry["analysis_index"]),
                ),
            )
        ):
            row["sample_id"] = f"S{position:05d}"
        sample_rows.sort(key=lambda entry: entry["sample_id"])
        digest = _write_tsv(sample_rows, sample_path, path)
        counts_by_stratum: dict[str, int] = {}
        for row in sample_rows:
            counts_by_stratum[row["stratum"]] = counts_by_stratum.get(row["stratum"], 0) + 1
        sample_facts = {
            "written": True,
            "path": str(sample_path),
            "sha256": digest,
            "rows": len(sample_rows),
            "columns": list(SAMPLE_COLUMNS),
            "seed": int(seed),
            "sample_per_stratum": int(sample_per_stratum),
            "pool_per_stratum": int(sample_per_stratum) * SAMPLE_POOL_MULTIPLIER,
            "rows_by_stratum": dict(sorted(counts_by_stratum.items())),
            "selection": "seeded SplitMix64 hash of the axis index, smallest first, one allele class at a time",
            "strata_source": (
                "the leading contributors by exclusive variants, so an exclusive stratum is the "
                "one whose variants nobody else observes"
            ),
            "exclusive_analyses": [int(index) for index in exclusive_analyses],
            "exclusive_publications": list(exclusive_publications),
            "analysis_major_rows_read": trace_rows_read,
            "on_panel_controls": control_facts,
            "control_analyses": control_analyses,
            "allele_classes": {
                name: sum(1 for row in sample_rows if row["allele_class"] == name)
                for name in ALLELE_CLASSES
            },
        }

    # -- the shape of the union --------------------------------------------
    breakdown = _scan_variant_breakdown(variant_table_path(path), dense_mask)

    # -- stable, read-only view ---------------------------------------------
    release_lock_at_end = _release_lock(path, manifest.store_id)
    try:
        end_stat = path.stat()
    except FileNotFoundError:
        inconsistency(
            "store_vanished_during_audit",
            f"{path} no longer exists at the end of the audit",
            path=str(path),
        )
        end_stat = start_stat
    if (start_stat.st_dev, start_stat.st_ino) != (end_stat.st_dev, end_stat.st_ino):
        inconsistency(
            "store_replaced_during_audit",
            "the Store directory's identity changed during the attribution",
            before_device=start_stat.st_dev,
            after_device=end_stat.st_dev,
        )
    if release_lock_at_end["held_by"] and not release_lock["held_by"]:
        inconsistency(
            "release_started_during_audit",
            f"a run took this release's lock while attribution was reading it "
            f"({release_lock_at_end['held_by']})",
            held_by=release_lock_at_end["held_by"],
        )
    after = _inventory(path, method)
    unchanged = _compare(before, after)
    unchanged["method"] = method
    if unchanged["changed"] or unchanged["added"] or unchanged["removed"]:
        inconsistency(
            "store_modified",
            "the Store changed while it was being attributed: "
            f"{len(unchanged['changed'])} file(s) differ, {len(unchanged['added'])} appeared and "
            f"{len(unchanged['removed'])} disappeared",
            changed=unchanged["changed"],
            added=unchanged["added"],
            removed=unchanged["removed"],
        )

    measured = [
        "per-Analysis and per-publication attribution of the off-panel union, from the Ragged body",
        "distinct-Analyses and distinct-publications support per off-panel variant",
        "exclusive variants per Analysis and per publication",
        "chromosome and allele class of the off-panel axis",
        "read-only proof: no Store file changed",
    ]
    if sample_path is not None:
        measured.append("seeded stratified sample for raw-row tracing")
    epoch = _now()
    errors = [entry for entry in inconsistencies if entry["severity"] == "error"]
    finished = time.time()
    return {
        "schema": ATTRIBUTION_REPORT_SCHEMA,
        "audit_version": AUDIT_VERSION,
        "argv": list(argv) if argv is not None else sys.argv[1:],
        "started_at": started_at,
        "finished_at": epoch,
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
            "opened_mode": "r",
            "recorded": recorded,
            "analyses_tsv_rows": len(analyses),
            "stable_view": {
                "dev": start_stat.st_dev,
                "ino": start_stat.st_ino,
                "swapped": (start_stat.st_dev, start_stat.st_ino)
                != (end_stat.st_dev, end_stat.st_ino),
                "release_lock": {**release_lock_at_end, "held_by_at_start": release_lock["held_by"]},
            },
        },
        "axis": {
            "n_axis": n_axis,
            "n_dense": n_dense,
            "n_off_panel": n_off_panel,
            "association_rows": total_analyses_associations,
        },
        "census": {
            "note": (
                "the attributed union is the number of off-panel axis entries at least one "
                "Analysis observes; it is reconciled against the Dense Component's complement "
                "and the count the Store records"
            ),
            "off_panel_variants_recorded": census_recorded,
            "off_panel_variants": n_off_panel,
            "attributed_union_variants": census_observed,
            "unobserved_off_panel_variants": totals["unobserved_off_panel_variants"],
            "agrees": None if census_recorded is None else census_recorded == census_observed,
            "associations": totals["off_panel_associations"],
            "association_rows_read": facts["association_rows"],
        },
        "support": {
            "note": (
                "each off-panel variant counted once per distinct observing Analysis "
                "(and once per distinct publication), from the Ragged body itself"
            ),
            "analyses": counters["histogram_analyses"],
            "publications": counters["histogram_publications"],
            "buckets": list(ATTRIBUTION_SUPPORT_BUCKETS),
            "exclusive_to_one_analysis_variants": totals["exclusive_to_one_analysis_variants"],
            "exclusive_to_one_publication_variants": totals["exclusive_to_one_publication_variants"],
            "exclusive_to_one_publication_multi_analysis_variants": totals[
                "exclusive_to_one_publication_multi_analysis_variants"
            ],
            "shared_across_publications_variants": totals["shared_across_publications_variants"],
            "sum_of_analysis_exclusive_variants": exclusive_analysis_sum,
            "sum_of_publication_exclusive_variants": exclusive_publication_sum,
        },
        "allele_classes": {
            **breakdown["allele_classes"],
            "multiallelic_positions": breakdown["positions_with_multiple_off_panel_rows"],
            "rows_at_multiallelic_positions": breakdown["rows_at_multi_row_positions"],
            "definition": (
                "snp: one base against one base; indel: alleles of different lengths; "
                "complex: two equal-length alleles longer than one base"
            ),
        },
        "chromosomes": breakdown["chromosomes"],
        "contributors": {
            "n_analyses": n_analyses,
            "n_publications": len(publications),
            "publication_key": (
                "publication_pmid, or publication_doi where no pmid is recorded; an Analysis with "
                "neither is its own explicitly unknown group (key_kind `unknown`), never merged "
                "with another"
            ),
            "n_unknown_publications": sum(
                1 for entry in publications if entry["publication_key_kind"] == "unknown"
            ),
            **tables,
        },
        "concentration": concentration,
        "sample": sample_facts,
        "inconsistencies": inconsistencies,
        "ok": not errors,
        "error_count": len(errors),
        "finding_count": len(inconsistencies) - len(errors),
        "unchanged": unchanged,
        "resources": {
            "band_variants": band_variants,
            "band_rows": band_rows,
            "axis_bands": counters["totals"]["bands"],
            "support_bands_read": facts["bands_read"],
            "max_rss_kb": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),
            "peak_rss_includes": (
                "one uint16 support plane per axis entry for Analyses and for publications, their "
                "observer planes, and the deduplication stamps"
            ),
            "variant_table_rows_read": breakdown["rows_read"],
        },
        "scope": {
            "measured": measured,
            "not_measured": list(ATTRIBUTION_NOT_MEASURED),
            "skipped": [] if sample_path is not None else ["seeded sample for raw-row tracing"],
        },
    }


def _attribution_summary(report: dict[str, Any], output: Path) -> str:
    census = report["census"]
    support = report["support"]
    sample = report["sample"]
    sample_text = (
        f", sample {sample['rows']} rows -> {sample['path']}" if sample.get("written") else ""
    )
    return (
        f"{report['store']['store_id']}: off-panel {report['axis']['n_off_panel']:,} variants / "
        f"{census['associations']:,} associations | attributed union "
        f"{census['attributed_union_variants']:,} ({'agrees' if census['agrees'] else 'DISAGREES'} "
        f"with the recorded census) | exclusive to one Analysis "
        f"{support['exclusive_to_one_analysis_variants']:,}, one publication "
        f"{support['exclusive_to_one_publication_variants']:,} | errors {report['error_count']}, "
        f"findings {report['finding_count']}{sample_text}\n"
        f"evidence: {output} ({'ok' if report['ok'] else 'NOT OK'})"
    )


# ---------------------------------------------------------------------------
# Trace (stores #374): replay sampled rows through source frequency admission
# ---------------------------------------------------------------------------

#: The source columns a trace records verbatim, as GWAS-SSF declares them. Every
#: one is recorded when the source carries it, and its absence is recorded too,
#: because "the file has no `effect_allele_frequency` column" and "the file has
#: one and this row is blank in it" are different facts about the same row.
TRACE_TOKEN_COLUMNS: tuple[str, ...] = (
    "chromosome",
    "base_pair_location",
    "effect_allele",
    "other_allele",
    "effect_allele_frequency",
    "beta",
    "standard_error",
    "p_value",
    "odds_ratio",
    "ci_lower",
    "ci_upper",
    "rsid",
    "variant_id",
)

#: The closed vocabulary a traced row's source frequency is classified by. It
#: separates the four ways a source reports *no usable frequency* -- which the
#: admission rule cannot tell apart and must not be reported as one thing --
#: from a frequency it read and the MAF policy then judged.
TRACE_AF_CLASSES: tuple[str, ...] = (
    "valid_common",
    "below_threshold",
    "placeholder_zero",
    "placeholder_one",
    "missing",
    "malformed",
    "nonfinite",
    "out_of_range",
)

#: The closed vocabulary one traced row's disposition is classified by. Every
#: sampled row carries exactly one of these, so the tally sums to the sample.
TRACE_DISPOSITIONS: tuple[str, ...] = (
    "valid_common",
    "maf_below_threshold",
    "maf_missing",
    "info_below_threshold",
    "unlocated_source",
    "ambiguous_source",
    "source_unavailable",
)

#: The tokens the readers treat as "the source reported no frequency"
#: (`opengwasdb.stats.parse_af`); repeated here only to *classify* the raw token,
#: never to decide anything: the admission replay goes through `parse_af` itself.
TRACE_MISSING_TOKENS = frozenset({"", ".", "NA", "#NA", "NaN", "nan", "None", "N/A"})

#: Source files one trace scans in parallel. Each source is read exactly once,
#: start to finish, whatever rows the sample draws from it, so this is the number
#: of *files* in flight, not a per-row optimisation.
DEFAULT_TRACE_SOURCE_WORKERS = min(8, os.cpu_count() or 1)

#: Where `analyses.tsv`'s `source_url` is expected to land locally when its
#: `source_file` is gone: the EBI GWAS Catalog mirror's own directory tree.
DEFAULT_RAW_ROOT = Path("/data/opengwasdb/raw/ebi-gwas-catalog")

#: What a trace does not measure. Stated in the report rather than left to a
#: reader to infer: a sampled trace is not a census, and the Store's stored
#: frequency is a lossy code that corroborates a disposition but never decides one.
TRACE_NOT_MEASURED: tuple[dict[str, str], ...] = (
    {
        "item": "whole-file frequency census",
        "ticket": "#374",
        "why": (
            "the trace reads each source once but records only the sampled rows; the "
            "per-Analysis admission counts the Store records are quoted as the "
            "population the sample is drawn from, not recomputed here"
        ),
    },
    {
        "item": "stored frequency decoded to a float",
        "ticket": "#374",
        "why": (
            "the trace reports the plane's own stored code and whether it is the "
            "codec's absent sentinel; decoding an int8 residual needs the whole "
            "exception table and a lossy value must not decide a boundary question"
        ),
    },
    {
        "item": "source content checksum",
        "ticket": "#374",
        "why": (
            "the source files' declared checksums are recorded from the build "
            "manifest and their sizes are verified; re-hashing tens of gigabytes is "
            "not affordable per run and the build already recorded them"
        ),
    },
    {
        "item": "cohort frequency as population frequency",
        "ticket": "#374",
        "why": (
            "a source's `effect_allele_frequency` is the cohort's, not the EUR "
            "panel's; the trace reports what the source declared and claims nothing "
            "about population frequency"
        ),
    },
)


def _frequency_class(token: str | None) -> dict[str, Any]:
    """Classify one raw `effect_allele_frequency` token, and keep its value.

    This classification exists so the report can separate the four ways a source
    says "no usable frequency" from a frequency the MAF policy judged: a
    placeholder `0.0`, a placeholder `1.0`, a blank, an unparseable token, a
    non-finite one and an out-of-range one all reach the admission rule as the
    same NaN, and reporting them as one tally would hide which of them the sample
    is made of. What the *reader* does with the token is not decided here:
    `parse_af` answers that, and the admission replay calls it. Nor does this
    classification decide the disposition: a `filtered` MAF policy drops a
    placeholder and an unusable token alike, but for different reasons -- a
    judged MAF of 0 against no comparison at all.
    """
    if token is None:
        return {"class": "missing", "token": None, "value": None, "column": "absent"}
    text = token.strip()
    if text in TRACE_MISSING_TOKENS:
        return {"class": "missing", "token": token, "value": None, "column": "present"}
    try:
        value = float(text)
    except ValueError:
        return {"class": "malformed", "token": token, "value": None, "column": "present"}
    if not math.isfinite(value):
        return {"class": "nonfinite", "token": token, "value": value, "column": "present"}
    if value < 0.0 or value > 1.0:
        return {"class": "out_of_range", "token": token, "value": value, "column": "present"}
    if value == 0.0:
        return {"class": "placeholder_zero", "token": token, "value": value, "column": "present"}
    if value == 1.0:
        return {"class": "placeholder_one", "token": token, "value": value, "column": "present"}
    return {"class": "numeric", "token": token, "value": value, "column": "present"}


def _chromosome_key(value: str) -> str:
    """A chromosome label comparable between a source row and a Store ALID."""
    try:
        return normalise_chromosome(value)
    except (VariantNormalisationError, ValueError):
        return str(value).strip().upper()


def _allele_key(value: str) -> str:
    return str(value).strip().upper()


def _allele_orientation(
    source_effect: str, source_other: str, canonical_effect: str, canonical_other: str
) -> str:
    """How a source row's alleles map onto the Store's canonical ALID.

    A Store ALID is `chromosome:position:a1:a2` with `a1` the alphabetically
    first allele (`opengwasdb.variants.normalise.orient_to_canonical`), so a
    source row's own `effect_allele` is either that `a1` (same orientation, the
    frequency is already the stored effect allele's) or its `a2` (flipped, and
    the reader/ builder turns the frequency into `1 - af`). Anything else is a
    different variant at the same position -- a multiallelic neighbour -- and is
    recorded rather than silently accepted.
    """
    effect = _allele_key(source_effect)
    other = _allele_key(source_other)
    a1 = _allele_key(canonical_effect)
    a2 = _allele_key(canonical_other)
    if effect == a1 and other == a2:
        return "same"
    if effect == a2 and other == a1:
        return "flipped"
    return "mismatch"


def _read_build_manifest(path: Path) -> dict[str, dict[str, Any]]:
    """The build's own `analyses.tsv`, indexed by Analysis.

    The Store's copy of `analyses.tsv` is Analytical Metadata: it deliberately
    does not carry where the source came from. The *build manifest* the Store was
    built from does (`source_file`, `source_url`, `checksum`, and the declared
    policy columns), and it is the only faithful record of which file each
    Analysis was read from and under which MAF and INFO thresholds.
    """
    if not path.is_file():
        raise AuditError(
            f"{path} is not a file; a trace needs the build manifest the Store was built "
            "from, because the Store's own analyses.tsv does not record source locations"
        )
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            fieldnames = list(reader.fieldnames or [])
            rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise AuditError(f"build manifest {path} cannot be read ({exc})") from None
    if not rows:
        raise AuditError(f"build manifest {path} lists no Analyses")
    for required in ("analysis_id",):
        if required not in fieldnames:
            raise AuditError(f"build manifest {path} has no {required!r} column")
    if not any(
        column in fieldnames for column in ("source_file", "source_url", "downloaded_file")
    ):
        raise AuditError(
            f"build manifest {path} records no source location (source_file, source_url or "
            "downloaded_file); the sampled rows cannot be traced to a raw source"
        )
    manifest: dict[str, dict[str, Any]] = {}
    for row in rows:
        analysis_id = (row.get("analysis_id") or "").strip()
        if not analysis_id:
            continue
        manifest[analysis_id] = {key: (value or "") for key, value in row.items()}
    return manifest


def _resolve_source_file(
    row: dict[str, str], raw_root: Path
) -> tuple[str | None, str]:
    """The local source file an Analysis was read from, and how it was resolved.

    Preference is the local path the build recorded, then the EBI URL's own tail
    mapped into the mirror (`.../summary_statistics/<bucket>/<analysis>/...`), then
    any other recorded local column -- returned even when it does not exist, so the
    caller reports a *missing* file rather than inventing a reason.
    """
    for column in ("source_file", "downloaded_file", "filtered_file", "file_path"):
        value = (row.get(column) or "").strip()
        if value and Path(value).is_file():
            return value, column
    url = (row.get("source_url") or "").strip()
    marker = "/summary_statistics/"
    if marker in url:
        tail = url.split(marker, 1)[1]
        candidate = raw_root / tail
        if candidate.is_file():
            return str(candidate), "source_url"
    for column in ("source_file", "downloaded_file", "filtered_file", "file_path"):
        value = (row.get(column) or "").strip()
        if value:
            return value, column
    return None, "absent"


def _scan_source_sites(
    task: tuple[str, list[tuple[str, str, int, str, str]], list[str]]
) -> dict[str, Any]:
    """Locate every requested site in one source file, in a single pass.

    Runs in a worker process, so it takes and returns plain data. The pass reads
    the gzip stream once -- the same cost however many rows the sample draws from
    the file -- and only splits the lines whose *position* token matches one it
    was asked for, which is what keeps a 99M-row, 2.5 GB harmonised file to about
    a minute.

    `extra_columns` are the Analysis's declared score columns (its
    `imputation_score_column`): they are not GWAS-SSF's fixed vocabulary, so the
    scan has to be told about them or the declared INFO filter would be replayed
    against a token nobody read.

    Every row at a requested position whose allele pair is the sample's is kept,
    not just the first. A multiallelic site holds several rows and two rows can
    carry the same alleles with different frequencies; the candidates are
    returned whole and their frequencies compared here, so an ambiguous row is
    flagged rather than resolved by first-seen order (CONTRIBUTING: never
    silently substitute).
    """
    path, targets, extra_columns = task
    wanted: dict[tuple[str, int], list[tuple[str, str, str]]] = {}
    for sample_id, chromosome, position, a1, a2 in targets:
        wanted.setdefault((_chromosome_key(chromosome), int(position)), []).append(
            (sample_id, a1, a2)
        )
    facts: dict[str, Any] = {
        "path": path,
        "requested_sites": len(wanted),
        "lines_scanned": 0,
        "rows_matched": 0,
        "header": [],
        "extra_columns": list(extra_columns),
        "sites": {},
        "error": None,
    }
    if not wanted:
        return facts
    positions = sorted({str(position) for _chrom, position in wanted})
    pattern = re.compile(
        b"\t(?:" + b"|".join(re.escape(position.encode()) for position in positions) + b")\t"
    )
    try:
        with pysam.BGZFile(path, "rb") as handle:
            header_line = handle.readline()
            if not header_line:
                facts["error"] = "source file is empty"
                return facts
            header = header_line.decode("utf-8", "replace").rstrip("\r\n").split("\t")
            facts["header"] = header
            index = {name: position for position, name in enumerate(header)}
            for required in ("chromosome", "base_pair_location"):
                if required not in index:
                    facts["error"] = (
                        f"source header has no {required!r} column; the sampled sites cannot "
                        "be located in it"
                    )
                    return facts
            effects = [index.get("effect_allele"), index.get("allele1")]
            others = [index.get("other_allele"), index.get("allele2")]
            effect_index = next((value for value in effects if value is not None), None)
            other_index = next((value for value in others if value is not None), None)
            token_indices = {
                name: index[name]
                for name in (*TRACE_TOKEN_COLUMNS, *extra_columns)
                if name in index
            }
            found: dict[str, list[dict[str, Any]]] = {}
            candidates: dict[tuple[str, int], int] = {}
            mismatch: dict[tuple[str, int], int] = {}
            for line in handle:
                facts["lines_scanned"] += 1
                if not pattern.search(line):
                    continue
                fields = line.rstrip(b"\r\n").split(b"\t")
                if len(fields) < 2:
                    continue
                chromosome = _chromosome_key(
                    fields[index["chromosome"]].decode("utf-8", "replace")
                )
                try:
                    position = int(fields[index["base_pair_location"]])
                except ValueError:
                    continue
                site = (chromosome, position)
                if site not in wanted:
                    continue
                facts["rows_matched"] += 1
                candidates[site] = candidates.get(site, 0) + 1
                source_effect = (
                    ""
                    if effect_index is None or effect_index >= len(fields)
                    else fields[effect_index].decode("utf-8", "replace")
                )
                source_other = (
                    ""
                    if other_index is None or other_index >= len(fields)
                    else fields[other_index].decode("utf-8", "replace")
                )
                tokens = {
                    name: fields[position_index].decode("utf-8", "replace")
                    for name, position_index in token_indices.items()
                    if position_index < len(fields)
                }
                for sample_id, a1, a2 in wanted[site]:
                    orientation = _allele_orientation(source_effect, source_other, a1, a2)
                    if orientation == "mismatch":
                        mismatch[site] = mismatch.get(site, 0) + 1
                        continue
                    found.setdefault(sample_id, []).append(
                        {
                            "line_number": facts["lines_scanned"] + 1,
                            "data_row": facts["lines_scanned"],
                            "orientation": orientation,
                            "tokens": tokens,
                            "source_effect_allele": source_effect,
                            "source_other_allele": source_other,
                        }
                    )
    except (OSError, ValueError, RuntimeError) as exc:
        # pysam raises its own `SamtoolsError` (a `RuntimeError`) for a file that is
        # not a BGZF/TSV source; a scan that names the failure beats a traceback.
        facts["error"] = f"{type(exc).__name__}: {exc}"
        return facts
    for (chromosome, position), entries in wanted.items():
        site = (chromosome, position)
        for sample_id, _a1, _a2 in entries:
            matched = found.get(sample_id, [])
            if not matched:
                facts["sites"][sample_id] = {
                    "located": False,
                    "raw_rows_at_position": candidates.get(site, 0),
                    "allele_mismatch_rows": mismatch.get(site, 0),
                    "candidates": [],
                }
                continue
            # The candidates describe the same site and allele pair, so their
            # frequencies are directly comparable once oriented to the canonical
            # effect allele. Two rows that agree are one frequency reported twice;
            # two that disagree are an ambiguity this scan must not resolve.
            distinct: dict[str, dict[str, Any]] = {}
            for candidate in matched:
                orientation = candidate["orientation"]
                token = candidate["tokens"].get("effect_allele_frequency")
                klass = _frequency_class(token)["class"]
                parsed = parse_af(token)
                canonical = None if parsed is None else (
                    1.0 - parsed if orientation == "flipped" else parsed
                )
                distinct.setdefault(
                    f"{klass}|{canonical!r}", {"class": klass, "canonical_af": canonical}
                )
            entry = dict(matched[0])
            entry.update(
                {
                    "located": True,
                    "candidate_rows": len(matched),
                    "candidates": matched,
                    "frequency_agreement": len(distinct) == 1,
                    "distinct_frequencies": [
                        distinct[key] for key in sorted(distinct)
                    ],
                    "raw_rows_at_position": candidates.get(site, 0),
                    "allele_mismatch_rows": mismatch.get(site, 0),
                }
            )
            facts["sites"][sample_id] = entry
    return facts


def _replay_admission(
    af: float | None,
    info_token: str | None,
    info_declared: bool,
    maf_policy: Any,
    info_policy: Any,
) -> dict[str, Any]:
    """Put one sampled row through the builder's own admission rule.

    The decision is `opengwasdb.build.row_admission.admit_rows`'s -- the same call
    the Hybrid builder makes over a column of rows -- applied to a batch of one,
    with the source frequency the reader parsed (NaN where it parsed none) and the
    declared INFO score the reader parsed from the declared column. Nothing here
    re-derives the rule; the MAF is computed alongside only to *report* the value
    the rule compared, and only a frequency the rule can read at all (finite, in
    `[0, 1]`) has one. A placeholder `0.0`/`1.0` arrives here as the reader's
    absence rather than as the number (ADR 0036), so it is reported with no MAF
    like a blank; an out-of-range value is reported with none because no
    threshold was ever compared against it (opengwasdb#331).
    """
    score = parse_imputation_score(info_token, declared=info_declared)
    scores = np.array([np.nan if score.value is None else score.value], dtype="float64")
    statuses = np.array([score.status.value], dtype=object)
    frequencies = np.array([np.nan if af is None else af], dtype="float64")
    admission = admit_rows(scores, statuses, frequencies, info_policy, maf_policy)
    keep = bool(admission.keep[0])
    counts = admission.counts
    usable = af is not None and math.isfinite(af) and 0.0 <= af <= 1.0
    maf = min(af, 1.0 - af) if usable else None
    # The rule drops a row for exactly three reasons, and the disposition names
    # which one it was. A `filtered` MAF policy drops the rows it cannot judge as
    # well as those below its threshold (opengwasdb#331), so `maf_missing` here
    # means "dropped for carrying no usable frequency" -- and under `disabled`
    # or `unavailable` the same class means the rule kept it unjudged. The
    # frequency class beside it says which token it was.
    if counts.info.below_threshold:
        disposition = "info_below_threshold"
    elif counts.maf_below_threshold:
        disposition = "maf_below_threshold"
    elif not usable:
        disposition = "maf_missing"
    else:
        disposition = "valid_common"
    return {
        "keep": keep,
        "disposition": disposition,
        "maf": maf,
        "counts": {
            "admitted": counts.admitted,
            "maf_below_threshold": counts.maf_below_threshold,
            "maf_missing": counts.maf_missing,
            "info_below_threshold": counts.info.below_threshold,
        },
        "info_score": {
            "declared": info_declared,
            "token": info_token,
            "status": score.status.value,
            "value": None if score.value is None else score.value,
        },
    }


def _read_trace_sample(path: Path) -> list[dict[str, Any]]:
    """The sample TSV #373 wrote, validated and normalised for tracing."""
    if not path.is_file():
        raise AuditError(f"sample {path} is not a file; the trace traces a written sample")
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            fieldnames = list(reader.fieldnames or [])
            raw_rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise AuditError(f"sample {path} cannot be read ({exc})") from None
    for required in ("sample_id", "analysis_id", "analysis_index", "alid", "stratum"):
        if required not in fieldnames:
            raise AuditError(f"sample {path} has no {required!r} column")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_rows:
        sample_id = (raw.get("sample_id") or "").strip()
        if not sample_id or sample_id in seen:
            raise AuditError(
                f"sample {path} names {sample_id!r} more than once (or not at all); a trace "
                "keys its evidence by sample id"
            )
        seen.add(sample_id)
        alid = (raw.get("alid") or "").strip()
        parsed = parse_canonical_alid(alid)
        if parsed is None:
            raise AuditError(f"sample row {sample_id} carries {alid!r}, which is not a canonical ALID")
        try:
            analysis_index = int((raw.get("analysis_index") or "").strip())
        except ValueError:
            raise AuditError(
                f"sample row {sample_id} carries analysis_index "
                f"{(raw.get('analysis_index') or '')!r}, which is not an integer"
            ) from None
        variant_index = (raw.get("variant_index") or "").strip()
        rows.append(
            {
                "sample_id": sample_id,
                "analysis_id": (raw.get("analysis_id") or "").strip(),
                "analysis_index": analysis_index,
                "alid": alid,
                "parsed_alid": parsed,
                "stratum": (raw.get("stratum") or "").strip(),
                "on_panel": (raw.get("on_panel") or "").strip().lower() == "true",
                "publication_pmid": (raw.get("publication_pmid") or "").strip() or None,
                "allele_class": (raw.get("allele_class") or "").strip() or None,
                "chromosome": (raw.get("chromosome") or "").strip() or None,
                "variant_index": int(variant_index) if variant_index else None,
                "source_row_lookup_hint": (raw.get("source_row_lookup_hint") or "").strip() or None,
            }
        )
    if not rows:
        raise AuditError(f"sample {path} holds no rows; there is nothing to trace")
    return rows


def _recorded_admissions(manifest_provenance: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The per-Analysis admission counts and policies the Store itself records.

    These are the denominators a sample represents: `associations_missing` is the
    rows the build's `filtered` MAF policy dropped for having no usable frequency
    (opengwasdb#331 made `maf_rows_missing` count dropped rows, not retained ones),
    and `associations_below_threshold` the rows it dropped for a MAF below the
    threshold. They are quoted, never recomputed -- a sample cannot count a whole
    file and the report must not pretend it did.
    """
    maf = {
        entry.get("analysis_id"): entry
        for entry in (manifest_provenance.get("maf") or {}).get("analyses") or []
    }
    info = {
        entry.get("analysis_id"): entry
        for entry in (manifest_provenance.get("info_score") or {}).get("analyses") or []
    }
    recorded: dict[str, dict[str, Any]] = {}
    for analysis_id, entry in maf.items():
        if analysis_id is None:
            continue
        recorded[analysis_id] = {
            "maf_state": entry.get("maf_state"),
            "maf_threshold": entry.get("maf_threshold"),
            "associations_missing": entry.get("associations_missing"),
            "associations_below_threshold": entry.get("associations_below_threshold"),
            "info_score_state": (info.get(analysis_id) or {}).get("info_score_state"),
            "info_score_threshold": (info.get(analysis_id) or {}).get("info_score_threshold"),
            "associations_observed": (info.get(analysis_id) or {}).get("associations_observed"),
            "associations_retained": (info.get(analysis_id) or {}).get("associations_retained"),
        }
    return recorded


def _runtime_revision() -> dict[str, Any]:
    """The `opengwasdb` this process actually executes, and how that is known.

    The epic's acceptance asks the trace to replay the *build's* revision rather
    than silently substitute current code. This module cannot install a historical
    wheel at run time, so it records both revisions and their agreement instead of
    pretending they are one: the installed distribution's own `direct_url.json`
    carries the Git commit it was built from, and `records/build.json` beside the
    Store carries the commit that wrote it. A difference is a `finding` with the
    admission the trace therefore replays, never a silent substitution.
    """
    facts: dict[str, Any] = {
        "opengwasdb_version": None,
        "opengwasdb_rev": None,
        "recorded_by": None,
        "vcs_url": None,
        "python": platform.python_version(),
        "problem": None,
    }
    try:
        import importlib.metadata as metadata

        distribution = metadata.distribution("opengwasdb")
    except Exception as exc:  # pragma: no cover - a broken install is loud elsewhere
        facts["problem"] = f"the installed opengwasdb distribution cannot be read ({exc})"
        return facts
    facts["opengwasdb_version"] = distribution.version
    try:
        text = distribution.read_text("direct_url.json")
    except (OSError, ValueError):  # pragma: no cover - read_text does not raise for absence
        text = None
    if not text:
        facts["problem"] = (
            "the installed distribution records no direct_url.json, so the commit it was built "
            "from is unknown"
        )
        return facts
    try:
        data = json.loads(text)
    except ValueError:
        facts["problem"] = "the installed distribution's direct_url.json is not JSON"
        return facts
    vcs = data.get("vcs_info") or {}
    facts["vcs_url"] = data.get("url")
    facts["opengwasdb_rev"] = vcs.get("commit_id")
    facts["recorded_by"] = "direct_url.json"
    if not isinstance(facts["opengwasdb_rev"], str) or not facts["opengwasdb_rev"].strip():
        facts["opengwasdb_rev"] = None
        facts["problem"] = "the installed distribution records no VCS commit"
    return facts


def _declared_policies(
    manifest_row: dict[str, str], recorded: dict[str, Any], analysis_id: str
) -> dict[str, Any]:
    """The MAF and INFO policies the build manifest declared for one Analysis.

    Parsed by the same functions the build parses them with, then checked against
    what the Store recorded: a manifest that no longer describes the build that
    wrote the Store is a configurable threshold the trace would silently apply to
    the wrong rows, so a disagreement is an error, not a preference.
    """
    capability = (manifest_row.get("source_reader_capability") or "").strip() or None
    try:
        maf_policy = parse_maf_policy(manifest_row)
        info_policy = parse_info_score_policy(manifest_row, reader_capability=capability)
    except ValueError as exc:
        raise AuditError(
            f"Analysis {analysis_id}: the build manifest's declared MAF/INFO policy cannot "
            f"be read ({exc}), so the admission the build applied to this Analysis cannot be "
            "replayed"
        ) from None
    declared = {
        "maf_state": maf_policy.state.value,
        "maf_threshold": maf_policy.maf_threshold,
        "info_score_state": info_policy.state.value,
        "info_score_threshold": info_policy.info_score_threshold,
        "imputation_score_column": (manifest_row.get("imputation_score_column") or "").strip() or None,
    }
    disagreements: list[str] = []
    if recorded.get("maf_state") not in (None, declared["maf_state"]):
        disagreements.append(
            f"maf_state manifest {declared['maf_state']} vs store {recorded.get('maf_state')}"
        )
    recorded_threshold = recorded.get("maf_threshold")
    if (
        recorded_threshold is not None
        and declared["maf_threshold"] is not None
        and float(recorded_threshold) != float(declared["maf_threshold"])
    ):
        disagreements.append(
            f"maf_threshold manifest {declared['maf_threshold']} vs store {recorded_threshold}"
        )
    recorded_info = recorded.get("info_score_state")
    if recorded_info is not None and declared["info_score_state"] != recorded_info:
        if not (
            declared["info_score_state"] == "unavailable" and recorded_info in ("legacy_absent", "unavailable")
        ):
            disagreements.append(
                f"info_score_state manifest {declared['info_score_state']} vs store {recorded_info}"
            )
    # The *threshold* is compared too, not just the state: two FILTERED policies at
    # 0.8 and 0.9 agree on their state and disagree on which rows they keep, and a
    # replay under the wrong number would drop or keep the wrong rows silently.
    recorded_info_threshold = recorded.get("info_score_threshold")
    declared_info_threshold = declared["info_score_threshold"]
    if recorded_info_threshold is not None and declared_info_threshold is not None:
        if float(recorded_info_threshold) != float(declared_info_threshold):
            disagreements.append(
                "info_score_threshold manifest "
                f"{declared_info_threshold} vs store {recorded_info_threshold}"
            )
    return {
        "maf_policy": maf_policy,
        "info_policy": info_policy,
        "declared": declared,
        "disagreements": disagreements,
        "analysis_id": analysis_id,
    }


def _dense_row_of(mapping: np.ndarray, variant_index: int | None) -> int | None:
    """The Dense row a shared axis entry occupies, or None for an off-panel entry.

    `dense_to_shared` is ascending (the Dense Component's rows are the panel's
    ALIDs in axis order), so membership is one `searchsorted` rather than a
    13.4M-entry dictionary.
    """
    if variant_index is None:
        return None
    found = int(np.searchsorted(mapping, int(variant_index)))
    if found >= mapping.size or int(mapping[found]) != int(variant_index):
        return None
    return found


def _stored_frequency_facts(code: Any, eaf_encoding: Any) -> dict[str, Any]:
    """One stored frequency cell, as the plane's own code and its sentinel meaning.

    `int8_residual` reserves `EAF_ABSENT` and `EAF_EXCEPTION`; a `float16` plane
    spells absence as NaN and has no exception code. Decoding a residual to a
    float needs the whole exception table and is lossy, so it is not done: the
    sentinel answers the only question a boundary decision can safely ask of the
    stored value -- is there a frequency here at all.
    """
    if eaf_encoding.is_residual:
        value = int(code)
        return {
            "eaf_code": value,
            "eaf_absent": value == EAF_ABSENT,
            "eaf_exception": value == EAF_EXCEPTION,
        }
    number = float(code)
    return {
        "eaf_code": number,
        "eaf_absent": not math.isfinite(number),
        "eaf_exception": False,
    }


def _store_store_facts(
    axis: VariantAxis,
    ragged_group: Any,
    dense_mapping: np.ndarray | None,
    row: dict[str, Any],
    eaf_encoding: Any,
) -> tuple[dict[str, Any], str | None]:
    """The Store's own side of one sampled row: identity, presence, stored codes.

    Identity is the Store Variant Table row at the sample's `variant_index`; an
    on-panel variant has no Ragged Overflow row and its cell is read from the
    Dense Component instead. Presence is the encoding's own sentinel: a `z` code
    of `Z_MISSING` is an absent association, and `eaf`'s absent code is a missing
    frequency -- the two facts the trace compares a raw row's disposition with.

    A cell the Store *does not hold* is `z_present: False`, a definite answer. A
    plane that cannot be read at all is a `problem`, not a silent `None`: an audit
    that cannot read the cell it sampled must say so, never report `ok` without
    having looked (CONTRIBUTING: absence is not a value).
    """
    variant_index = row["variant_index"]
    facts: dict[str, Any] = {
        "variant_index": variant_index,
        "located": False,
        "alid": None,
        "alid_agrees": None,
        "chromosome": None,
        "position": None,
        "effect_allele": None,
        "other_allele": None,
        "plane": None,
        "dense_row": _dense_row_of(dense_mapping, variant_index) if dense_mapping is not None else None,
        "on_panel": None,
        "n_associations_at_variant": None,
        "associations_for_analysis": None,
        "z_code": None,
        "z_present": None,
        "se_code": None,
        "eaf_code": None,
        "eaf_absent": None,
        "eaf_exception": None,
        "problem": None,
    }
    if variant_index is None:
        return facts, None
    record = axis.by_index(int(variant_index))
    if record is None:
        return facts, "the Store Variant Table has no row at this variant index"
    facts.update(
        {
            "located": True,
            "alid": record.alid,
            "alid_agrees": record.alid == row["alid"],
            "chromosome": record.chromosome,
            "position": record.position,
            "effect_allele": record.effect_allele,
            "other_allele": record.other_allele,
            "plane": "ragged" if facts["dense_row"] is None else "dense",
            "on_panel": facts["dense_row"] is not None,
        }
    )
    if facts["plane"] == "dense":
        return facts, None
    if ragged_group is None:
        return facts, "the Store's data.zarr holds no ragged/by_variant group"
    try:
        pair = np.asarray(
            ragged_group["offsets"][int(variant_index) : int(variant_index) + 2]
        )
        start, end = int(pair[0]), int(pair[1])
        facts["n_associations_at_variant"] = end - start
        if end <= start:
            # The Store holds no association for this variant at all: a definite
            # "not held", not an unreadable cell.
            facts["z_present"] = False
            return facts, None
        analyses = np.asarray(ragged_group["analysis_index"][start:end])
        matches = np.flatnonzero(analyses == int(row["analysis_index"]))
        facts["associations_for_analysis"] = int(matches.size)
        if not matches.size:
            facts["z_present"] = False
            return facts, None
        at = start + int(matches[0])
        z_code = int(np.asarray(ragged_group["z"][at])[()])
        facts["z_code"] = z_code
        facts["z_present"] = z_code != Z_MISSING
        facts["se_code"] = float(np.asarray(ragged_group["se"][at])[()])
        facts.update(_stored_frequency_facts(np.asarray(ragged_group["eaf"][at])[()], eaf_encoding))
    except (KeyError, IndexError, TypeError, ValueError, OSError) as exc:
        return facts, f"the Store's ragged/by_variant arrays cannot be read ({type(exc).__name__}: {exc})"
    return facts, None


def _dense_store_fact(
    dense_arrays: Any,
    dense_row: int,
    analysis_index: int,
    eaf_encoding: Any,
) -> tuple[dict[str, Any], str | None]:
    """The Dense Component's side of one on-panel sampled row."""
    try:
        z_code = int(np.asarray(dense_arrays["z"][dense_row, analysis_index])[()])
        fact: dict[str, Any] = {
            "z_code": z_code,
            "z_present": z_code != Z_MISSING,
            "se_code": float(np.asarray(dense_arrays["se"][dense_row, analysis_index])[()]),
        }
        fact.update(
            _stored_frequency_facts(
                np.asarray(dense_arrays["eaf"][dense_row, analysis_index])[()], eaf_encoding
            )
        )
    except (KeyError, IndexError, TypeError, ValueError, OSError) as exc:
        return {}, f"the Store's dense/data.zarr planes cannot be read ({type(exc).__name__}: {exc})"
    return fact, None


def _tally(rows: Sequence[dict[str, Any]], predicate: Any = None) -> dict[str, Any]:
    """The disposition and source-frequency tally of a group of traced rows."""
    dispositions = {name: 0 for name in TRACE_DISPOSITIONS}
    af_classes = {name: 0 for name in TRACE_AF_CLASSES}
    selected = [row for row in rows if predicate is None or predicate(row)]
    for row in selected:
        dispositions[row["trace_disposition"]] += 1
        af_class = row["frequency"]["class"]
        if af_class in af_classes:
            af_classes[af_class] += 1
    counts = {
        "rows": len(selected),
        "dispositions": dispositions,
        "source_frequency_classes": af_classes,
    }
    return counts


def trace(
    store_path: str | Path,
    *,
    sample_path: str | Path,
    manifest_path: str | Path,
    raw_root: str | Path = DEFAULT_RAW_ROOT,
    limit: int | None = None,
    workers: int = DEFAULT_TRACE_SOURCE_WORKERS,
    verify: str = "auto",
    argv: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Trace a sample of one Hybrid Store Release back through frequency admission.

    For every sampled row: the Analysis's build manifest row, the raw source file
    and the exact source row (by chromosome, position and allele pair, read in one
    pass per file), that row's declared columns and raw tokens, the frequency the
    reader parsed from them, the admission `opengwasdb.build.row_admission.admit_rows`
    gives the row under the Analysis's own declared MAF and INFO policies, the
    Store's identity for the variant, and the stored `z`/`eaf` codes the build
    wrote. The last is corroboration -- the codec's sentinel answers *absent*, and
    a lossy residual never decides a boundary question.

    This is a read-only trace: it writes nothing but the evidence JSON, and it
    proves the Store unchanged by size and mtime (and sha256, where affordable)
    before and after.
    """
    started = time.time()
    started_at = _now()
    path = Path(store_path)
    if not (path / "manifest.json").is_file():
        raise AuditError(f"{path} holds no manifest.json; it is not a Store Release")
    sample = Path(sample_path)
    manifest_file = Path(manifest_path)

    inconsistencies: list[dict[str, Any]] = []

    def inconsistency(kind: str, detail: str, *, severity: str = "error", **counts: Any) -> None:
        inconsistencies.append(
            {"kind": kind, "severity": severity, "detail": detail, "counts": counts}
        )

    for entry in TRACE_NOT_MEASURED:
        inconsistency(
            "scope_not_measured",
            entry["item"],
            severity="finding",
            ticket=entry["ticket"],
            why=entry["why"],
        )

    build_manifest = _read_build_manifest(manifest_file)
    sample_rows = _read_trace_sample(sample)
    if limit is not None:
        sample_rows = sample_rows[: max(0, limit)]

    store = open_store(path)
    manifest = store.manifest
    if manifest.primary_layout.value != "hybrid":
        raise AuditError(
            f"{path} is a {manifest.primary_layout.value} Store Release; a trace follows a "
            "Hybrid Store's Ragged Overflow (and its Dense Component's controls) back to "
            "the raw rows the builder admitted"
        )
    group = store.arrays(mode="r")
    recorded_admissions = _recorded_admissions(manifest.provenance)
    eaf_encoding = manifest.encoding.eaf
    reference_assembly = manifest.reference_assembly

    # -- which engine this replay actually is --------------------------------
    runtime = _runtime_revision()
    build_record = _build_revision(path)
    replay_agrees = None
    if build_record["problem"] is not None:
        inconsistency(
            "build_revision_unrecorded"
            if build_record["problem"] in ("missing", "no_revision")
            else "build_revision_unreadable",
            f"{build_record['path']} does not name the opengwasdb revision that wrote this "
            f"Store ({build_record['problem']}"
            + (f": {build_record['error']}" if build_record.get("error") else "")
            + "); the trace cannot say which revision's admission semantics it replayed",
            path=build_record["path"],
        )
    if runtime["problem"] is not None:
        inconsistency(
            "replay_engine_unrecorded",
            f"the installed opengwasdb cannot name its own revision ({runtime['problem']}); "
            "the trace records the version it ran and cannot verify it against the build",
            severity="finding",
            opengwasdb_version=runtime["opengwasdb_version"],
        )
    if runtime["opengwasdb_rev"] and build_record["opengwasdb_rev"]:
        replay_agrees = runtime["opengwasdb_rev"] == build_record["opengwasdb_rev"]
        if not replay_agrees:
            inconsistency(
                "replay_revision_differs",
                "the trace replays the installed opengwasdb "
                f"{runtime['opengwasdb_rev']}"
                + (f" (version {runtime['opengwasdb_version']})" if runtime["opengwasdb_version"] else "")
                + f", but the Store was built by {build_record['opengwasdb_rev']}. Row parsing "
                "and admission are therefore the installed revision's semantics, not "
                "necessarily the build's; the reproducer pins the same rule in this engine",
                severity="finding",
                installed=runtime["opengwasdb_rev"],
                build_recorded=build_record["opengwasdb_rev"],
            )

    with (path / "analyses.tsv").open(newline="", encoding="utf-8") as handle:
        store_analyses = list(csv.DictReader(handle, delimiter="\t"))
    analysis_ids = {int(entry["analysis_index"]): entry["analysis_id"] for entry in store_analyses}

    method = _verify_method(path, verify)
    before = _inventory(path, method)

    # -- identity of every sampled row, and the source file it came from ------
    source_files: dict[str, dict[str, Any]] = {}
    policies: dict[str, dict[str, Any]] = {}
    for row in sample_rows:
        analysis_id = row["analysis_id"]
        if analysis_ids.get(row["analysis_index"]) != analysis_id:
            inconsistency(
                "sample_analysis_disagrees_with_store",
                f"sample row {row['sample_id']} names Analysis {analysis_id!r} at index "
                f"{row['analysis_index']}, but the Store's analyses.tsv has "
                f"{analysis_ids.get(row['analysis_index'])!r} there",
                sample_id=row["sample_id"],
            )
        if analysis_id not in build_manifest:
            inconsistency(
                "analysis_absent_from_build_manifest",
                f"sample row {row['sample_id']} names Analysis {analysis_id!r}, which the "
                "build manifest does not list; the raw source it was read from is unknown",
                sample_id=row["sample_id"],
            )
            continue
        if analysis_id in policies:
            continue
        manifest_row = build_manifest[analysis_id]
        declared = _declared_policies(manifest_row, recorded_admissions.get(analysis_id) or {}, analysis_id)
        for disagreement in declared["disagreements"]:
            inconsistency(
                "declared_policy_disagrees_with_store",
                f"Analysis {analysis_id}: {disagreement}; the trace would replay a policy the "
                "Store was not built under",
                analysis_id=analysis_id,
            )
        policies[analysis_id] = declared
        source_file, resolved_by = _resolve_source_file(manifest_row, Path(raw_root))
        source = {
            "analysis_id": analysis_id,
            "path": source_file,
            "resolved_by": resolved_by,
            "declared_sha256": (manifest_row.get("checksum") or "").strip() or None,
            "declared_size_bytes": None,
            "declared_build": (manifest_row.get("source_genome_build") or "").strip() or None,
            "assembly_agrees": None,
            "size_bytes": None,
            "mtime": None,
            "available": False,
        }
        try:
            source["declared_size_bytes"] = int((manifest_row.get("size_bytes") or "").strip())
        except ValueError:
            source["declared_size_bytes"] = None
        if source["declared_build"] is not None:
            source["assembly_agrees"] = (
                source["declared_build"].replace("GRCh", "").replace("hg", "")
                == str(reference_assembly).replace("GRCh", "").replace("hg", "")
            )
            if not source["assembly_agrees"]:
                inconsistency(
                    "source_assembly_differs_from_store",
                    f"Analysis {analysis_id} declares source build {source['declared_build']!r} "
                    f"but the Store is {reference_assembly}; a position match needs a lift "
                    "the trace does not perform, so this Analysis's rows are not traced",
                    analysis_id=analysis_id,
                )
        if source_file and Path(source_file).is_file():
            stat = Path(source_file).stat()
            source["size_bytes"] = stat.st_size
            source["mtime"] = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
            source["available"] = True
            if (
                source["declared_size_bytes"] is not None
                and source["declared_size_bytes"] != stat.st_size
            ):
                inconsistency(
                    "source_size_differs_from_manifest",
                    f"Analysis {analysis_id}: {source_file} is {stat.st_size} bytes but the "
                    f"build manifest recorded {source['declared_size_bytes']}; the sampled "
                    "row may not be the build's raw row",
                    analysis_id=analysis_id,
                )
        else:
            inconsistency(
                "source_file_missing",
                f"Analysis {analysis_id}: the build manifest's source "
                f"{source_file!r} ({resolved_by}) is not a readable file, so its sampled rows "
                "cannot be traced",
                analysis_id=analysis_id,
            )
        source_files[analysis_id] = source

    # -- one pass per source file, in parallel --------------------------------
    tasks: dict[str, tuple[str, list[tuple[str, str, int, str, str]], list[str]]] = {}
    for row in sample_rows:
        source = source_files.get(row["analysis_id"])
        if source is None or not source["available"]:
            continue
        if source["assembly_agrees"] is False:
            continue
        parsed = row["parsed_alid"]
        policy = policies.get(row["analysis_id"]) or {}
        declared_column = (policy.get("declared") or {}).get("imputation_score_column")
        extra_columns = [declared_column] if declared_column else []
        tasks.setdefault(source["path"], (source["path"], [], extra_columns))[1].append(
            (
                row["sample_id"],
                parsed.chromosome,
                parsed.position,
                parsed.effect_allele,
                parsed.other_allele,
            )
        )
    scans: dict[str, dict[str, Any]] = {}
    if tasks:
        n_workers = max(1, min(int(workers), len(tasks)))
        if n_workers == 1:
            for task in tasks.values():
                scans[task[0]] = _scan_source_sites(task)
        else:
            with ProcessPoolExecutor(max_workers=n_workers) as pool:
                for facts in pool.map(_scan_source_sites, list(tasks.values())):
                    scans[facts["path"]] = facts
    for scan_path, facts in scans.items():
        if facts.get("error"):
            for analysis_id, source in source_files.items():
                if source["path"] == scan_path:
                    inconsistency(
                        "source_unreadable",
                        f"{scan_path} could not be scanned ({facts['error']}); no sampled row "
                        "from it can be traced",
                        analysis_id=analysis_id,
                    )

    # -- the Store's own side -------------------------------------------------
    axis = VariantAxis(path)
    ragged_group = None
    try:
        ragged_group = group["ragged"]["by_variant"]
    except (KeyError, TypeError):
        ragged_group = None
    dense_to_shared = np.load(dense_to_shared_path(path), mmap_mode="r")
    dense_arrays = None
    dense_store = None
    if Path(dense_component_path(path)).is_dir():
        try:
            dense_store = open_store(dense_component_path(path))
            dense_arrays = dense_store.arrays(mode="r")
        except (AuditError, KeyError, OSError):
            dense_arrays = None
    dense_mapping = np.asarray(dense_to_shared)

    rows: list[dict[str, Any]] = []
    try:
        for row in sample_rows:
            analysis_id = row["analysis_id"]
            source = source_files.get(analysis_id)
            policy = policies.get(analysis_id)
            scan = scans.get(source["path"]) if source and source["available"] else None
            located = None if scan is None else scan["sites"].get(row["sample_id"])
            record: dict[str, Any] = {
                "sample_id": row["sample_id"],
                "stratum": row["stratum"],
                "on_panel": row["on_panel"],
                "analysis_id": analysis_id,
                "analysis_index": row["analysis_index"],
                "publication_pmid": row["publication_pmid"],
                "allele_class": row["allele_class"],
                "alid": row["alid"],
                "variant_index": row["variant_index"],
                "source_row_lookup_hint": row["source_row_lookup_hint"],
            }
            if source is not None:
                record["source"] = {
                    key: source[key]
                    for key in (
                        "path",
                        "resolved_by",
                        "declared_sha256",
                        "declared_build",
                        "assembly_agrees",
                        "size_bytes",
                        "mtime",
                    )
                }
            else:
                record["source"] = None
            location = {
                "located": bool(located and located.get("located")),
                "line_number": None if not located else located.get("line_number"),
                "data_row": None if not located else located.get("data_row"),
                "orientation": None if not located else located.get("orientation"),
                "candidate_rows": None if not located else located.get("candidate_rows"),
                "frequency_agreement": None if not located else located.get("frequency_agreement"),
                "distinct_frequencies": None if not located else located.get("distinct_frequencies"),
                "candidates": None if not located else located.get("candidates"),
                "raw_rows_at_position": None if not located else located.get("raw_rows_at_position"),
                "allele_mismatch_rows": None if not located else located.get("allele_mismatch_rows"),
                "columns": None,
                "tokens": None if not located else located.get("tokens"),
            }
            if scan is not None and scan.get("header"):
                recorded_columns = (*TRACE_TOKEN_COLUMNS, *(scan.get("extra_columns") or ()))
                location["columns"] = {
                    name: position
                    for position, name in enumerate(scan["header"])
                    if name in recorded_columns
                }
            record["location"] = location

            chosen_class = None
            admission = None
            frequency: dict[str, Any] = {
                "token": None,
                "class": None,
                "value": None,
                "parsed_af": None,
                "reader_treats_as_missing": None,
                "canonical_af": None,
            }
            # Two rows at one site with the same alleles and different frequencies
            # cannot be replayed as one row: the report keeps every candidate and
            # the row's admission is left unreplayed rather than guessed.
            ambiguous = bool(location["located"]) and location["frequency_agreement"] is False
            if location["located"] and not ambiguous:
                tokens = location["tokens"] or {}
                info_token = None
                if policy is not None:
                    column = policy["declared"].get("imputation_score_column")
                    if column:
                        info_token = tokens.get(column)
                frequency = _frequency_class(tokens.get("effect_allele_frequency"))
                parsed_af = parse_af(tokens.get("effect_allele_frequency"))
                orientation = location["orientation"]
                canonical_af = parsed_af
                if parsed_af is not None and orientation == "flipped":
                    canonical_af = 1.0 - parsed_af
                frequency.update(
                    {
                        "parsed_af": parsed_af,
                        "reader_treats_as_missing": parsed_af is None,
                        "canonical_af": canonical_af,
                    }
                )
                if policy is not None:
                    admission = _replay_admission(
                        canonical_af,
                        info_token,
                        bool(policy["declared"].get("imputation_score_column")),
                        policy["maf_policy"],
                        policy["info_policy"],
                    )
                # A numeric frequency is reported as the class the MAF threshold made
                # of it. It must not be read off the disposition: a row INFO dropped
                # carries a perfectly usable frequency, and calling that
                # `below_threshold` would blame the MAF filter for another rule.
                token_class = frequency["class"]
                if token_class == "numeric":
                    maf = None if admission is None else admission["maf"]
                    threshold = None if policy is None else policy["declared"]["maf_threshold"]
                    token_class = (
                        "below_threshold"
                        if maf is not None and threshold is not None and maf < float(threshold)
                        else "valid_common"
                    )
                chosen_class = token_class
            frequency["class"] = chosen_class
            record["frequency"] = frequency
            record["admission"] = None if admission is None else {
                "maf_policy": {
                    "state": policy["declared"]["maf_state"],
                    "threshold": policy["declared"]["maf_threshold"],
                },
                "info_policy": {
                    "state": policy["declared"]["info_score_state"],
                    "threshold": policy["declared"]["info_score_threshold"],
                },
                "keep": admission["keep"],
                "disposition": admission["disposition"],
                "maf": admission["maf"],
                "counts": admission["counts"],
                "info_score": admission["info_score"],
            }

            # -- the Store's identity and stored codes for this row -------------
            store_facts, store_problem = _store_store_facts(
                axis,
                ragged_group,
                dense_mapping,
                row,
                eaf_encoding,
            )
            if store_facts["plane"] == "dense" and store_problem is None:
                if dense_arrays is None:
                    store_problem = "the Store's Dense Component cannot be opened"
                else:
                    dense_fact, store_problem = _dense_store_fact(
                        dense_arrays,
                        int(store_facts["dense_row"]),
                        int(row["analysis_index"]),
                        eaf_encoding,
                    )
                    store_facts.update(dense_fact)
            if store_problem is not None:
                store_facts["problem"] = store_problem
            record["store"] = store_facts

            # -- the disposition, and whether the Store agrees with it ----------
            if ambiguous:
                disposition = "ambiguous_source"
            elif admission is None:
                if source is None or not source["available"]:
                    disposition = "source_unavailable"
                elif policy is None:
                    disposition = "source_unavailable"
                else:
                    disposition = "unlocated_source"
            else:
                disposition = admission["disposition"]
            record["trace_disposition"] = disposition
            agreement = {
                "store_holds_association": bool(store_facts["z_present"]),
                "admission_matches_store": None,
                "admission_matches_store_why": None,
                "stored_frequency_absent": store_facts["eaf_absent"],
                "stored_frequency_agrees": None,
            }
            if admission is not None and store_facts["z_present"] is not None:
                held = bool(store_facts["z_present"])
                kept = bool(admission["keep"])
                agreement["admission_matches_store"] = held == kept
                if held != kept:
                    agreement["admission_matches_store_why"] = (
                        f"the replay {'keeps' if kept else 'drops'} this row but the Store "
                        f"{'holds' if held else 'has no'} association for it"
                    )
                if store_facts["eaf_absent"] is not None:
                    if disposition == "maf_missing":
                        agreement["stored_frequency_agrees"] = bool(store_facts["eaf_absent"])
                    elif disposition == "valid_common":
                        agreement["stored_frequency_agrees"] = not bool(store_facts["eaf_absent"])
            record["agreement"] = agreement
            rows.append(record)
    finally:
        axis.close()

    # -- checks the trace exists to make --------------------------------------
    for record in rows:
        location = record["location"]
        store_facts = record["store"]
        if location["located"] and store_facts["located"] and store_facts["alid_agrees"] is False:
            inconsistency(
                "store_identity_disagrees_with_sample",
                f"sample row {record['sample_id']} names {record['alid']} at variant "
                f"{record['variant_index']}, but the Store's table has {store_facts['alid']!r} there",
                sample_id=record["sample_id"],
            )
        if store_facts["located"] and store_facts["on_panel"] != record["on_panel"]:
            inconsistency(
                "sample_panel_membership_disagrees_with_store",
                f"sample row {record['sample_id']} says on_panel={record['on_panel']}, but the "
                f"Store's Dense Component {'holds' if store_facts['on_panel'] else 'does not hold'} it",
                sample_id=record["sample_id"],
            )
        if store_facts.get("problem") is not None:
            inconsistency(
                "store_cell_unreadable",
                f"sample row {record['sample_id']} ({record['analysis_id']}, {record['alid']}): "
                f"{store_facts['problem']}; the trace cannot compare its admission with a cell "
                "it did not read",
                sample_id=record["sample_id"],
            )
        elif record["variant_index"] is not None and not store_facts["located"]:
            inconsistency(
                "store_identity_unreadable",
                f"sample row {record['sample_id']} names variant {record['variant_index']}, "
                "which the Store Variant Table does not hold; the row's Store identity cannot "
                "be confirmed",
                sample_id=record["sample_id"],
            )
        agreement = record["agreement"]
        if agreement["admission_matches_store"] is False:
            inconsistency(
                "admission_disagrees_with_store",
                f"sample row {record['sample_id']} ({record['analysis_id']}, {record['alid']}): "
                f"{agreement['admission_matches_store_why']}",
                sample_id=record["sample_id"],
                disposition=record["trace_disposition"],
            )
        if agreement["stored_frequency_agrees"] is False:
            inconsistency(
                "stored_frequency_disagrees_with_admission",
                f"sample row {record['sample_id']}: the admission replay says "
                f"{record['trace_disposition']} but the stored frequency code says "
                f"{'absent' if agreement['stored_frequency_absent'] else 'present'}",
                sample_id=record["sample_id"],
            )
        if record["trace_disposition"] == "ambiguous_source":
            inconsistency(
                "source_row_ambiguous",
                f"sample row {record['sample_id']} ({record['analysis_id']}, {record['alid']}) "
                f"matches {location['candidate_rows']} raw rows at its position and allele pair "
                "whose frequencies disagree ("
                + ", ".join(
                    f"{entry['class']}={entry['canonical_af']!r}"
                    for entry in location["distinct_frequencies"]
                )
                + "); the row's admission cannot be replayed as one row",
                sample_id=record["sample_id"],
                candidate_rows=location["candidate_rows"],
                line_numbers=[candidate["line_number"] for candidate in location["candidates"]],
            )
        if record["trace_disposition"] == "unlocated_source":
            inconsistency(
                "source_row_not_located",
                f"sample row {record['sample_id']} ({record['analysis_id']}, {record['alid']}) "
                "has no raw row at its position with its allele pair",
                sample_id=record["sample_id"],
                raw_rows_at_position=location["raw_rows_at_position"],
                allele_mismatch_rows=location["allele_mismatch_rows"],
            )

    # A Store that holds a row its own declared policy drops is already an
    # `admission_disagrees_with_store` error on that row, and the sample's
    # placeholder and invalid-frequency tallies are in `summary`. There is no
    # separate finding here: after opengwasdb#331 a `filtered` policy drops a
    # placeholder (`0.0`/`1.0`) and an unreadable frequency alike, so "the rule
    # admitted a placeholder" is no longer an observation the trace can make of a
    # Store built under the corrected rule -- it is a disagreement, and it is an
    # error.

    after = _inventory(path, method)
    unchanged = _compare(before, after)
    if not unchanged["unchanged"]:
        inconsistency(
            "store_changed_during_trace",
            "the Store's files differ after the trace; this report describes a Store that no "
            "longer exists in that state",
        )

    error_count = sum(1 for entry in inconsistencies if entry["severity"] == "error")
    finding_count = sum(1 for entry in inconsistencies if entry["severity"] == "finding")
    report = {
        "command": "trace",
        "audit_version": AUDIT_VERSION,
        "schema": "ogstores.audit.trace/1",
        "generated_at": started_at,
        "elapsed_seconds": round(time.time() - started, 3),
        "argv": list(argv) if argv is not None else None,
        "store": {
            "path": str(path),
            "store_id": manifest.store_id,
            "release_id": manifest.release_id,
            "format_version": manifest.format_version,
            "reference_assembly": reference_assembly,
            "eaf_encoding": {
                "kind": eaf_encoding.kind,
                "residual_range": eaf_encoding.residual_range,
                "absent_code": EAF_ABSENT if eaf_encoding.is_residual else None,
                "exception_code": EAF_EXCEPTION if eaf_encoding.is_residual else None,
            },
            "verify": method,
        },
        "replay": {
            "engine": runtime,
            "build_recorded": build_record,
            "agrees": replay_agrees,
            "admission_rule": "opengwasdb.build.row_admission.admit_rows",
            "frequency_parser": "opengwasdb.stats.parse_af",
            "policies_parsed_by": [
                "opengwasdb.model.maf_policy.parse_maf_policy",
                "opengwasdb.model.info_score_policy.parse_info_score_policy",
            ],
        },
        "sample": {
            "path": str(sample),
            "sha256": _sha256(sample),
            "rows": len(sample_rows),
        },
        "build_manifest": {
            "path": str(manifest_file),
            "sha256": _sha256(manifest_file),
            "analyses": len(build_manifest),
            "raw_root": str(raw_root),
        },
        "scope": {
            "measured": [
                "the raw source row behind every sampled row, located by chromosome, position and allele pair, with every matching candidate row retained",
                "the declared source columns (including a declared imputation_score_column) and the raw tokens of each located row",
                "the frequency opengwasdb.stats.parse_af reads from the row's effect_allele_frequency",
                "the admission opengwasdb.build.row_admission.admit_rows gives the row under the Analysis's declared MAF and INFO policies",
                "whether the Store holds the association, and its stored z/eaf codes",
                "the per-Analysis admission counts the Store records, quoted as the sample's population",
            ],
            "not_measured": list(TRACE_NOT_MEASURED),
            "sources": sorted(source_files.values(), key=lambda entry: entry["analysis_id"]),
        },
        "rows": rows,
        "summary": {},
        "inconsistencies": inconsistencies,
        "error_count": error_count,
        "finding_count": finding_count,
        "ok": error_count == 0,
        "store_unchanged": unchanged,
    }
    report["summary"] = _trace_summary(rows, recorded_admissions, policies, source_files)
    return report


def _trace_summary(
    rows: Sequence[dict[str, Any]],
    recorded_admissions: dict[str, dict[str, Any]],
    policies: dict[str, dict[str, Any]],
    source_files: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """The denominators the trace stands on, and the tallies they close."""
    overall = _tally(rows)
    off_panel = _tally(
        rows, lambda row: row["stratum"] != "on_panel_control" and not row["on_panel"]
    )
    controls = _tally(rows, lambda row: row["on_panel"])
    by_stratum: dict[str, Any] = {}
    for row in rows:
        by_stratum.setdefault(row["stratum"], []).append(row)
    by_analysis: list[dict[str, Any]] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["analysis_id"], []).append(row)
    for analysis_id in sorted(grouped):
        entries = grouped[analysis_id]
        tally = _tally(entries)
        recorded = recorded_admissions.get(analysis_id) or {}
        source = source_files.get(analysis_id) or {}
        tally.update(
            {
                "analysis_id": analysis_id,
                "analysis_index": entries[0]["analysis_index"],
                "publication_pmid": entries[0]["publication_pmid"],
                "source_path": source.get("path"),
                "source_available": source.get("available"),
                "declared_policy": (policies.get(analysis_id) or {}).get("declared"),
                "store_recorded": {
                    key: recorded.get(key)
                    for key in (
                        "associations_observed",
                        "associations_retained",
                        "associations_missing",
                        "associations_below_threshold",
                    )
                },
            }
        )
        by_analysis.append(tally)
    by_publication: list[dict[str, Any]] = []
    publication_groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = row["publication_pmid"] or "unknown"
        publication_groups.setdefault(key, []).append(row)
    for pmid in sorted(publication_groups):
        entries = publication_groups[pmid]
        tally = _tally(entries)
        tally.update(
            {
                "publication_pmid": None if pmid == "unknown" else pmid,
                "analyses": sorted({entry["analysis_id"] for entry in entries}),
            }
        )
        by_publication.append(tally)
    located = sum(1 for row in rows if row["location"]["located"])
    ambiguous = sum(1 for row in rows if row["trace_disposition"] == "ambiguous_source")
    classified = located - ambiguous
    placeholders = sum(
        1
        for row in rows
        if row["frequency"]["class"] in ("placeholder_zero", "placeholder_one")
    )
    placeholder_off_panel = sum(
        1
        for row in rows
        if row["frequency"]["class"] in ("placeholder_zero", "placeholder_one")
        and row["stratum"] != "on_panel_control"
        and not row["on_panel"]
    )
    return {
        "denominators": {
            "rows": len(rows),
            "rows_located": located,
            "rows_unlocated": len(rows) - located,
            "rows_ambiguous": ambiguous,
            "rows_with_frequency_class": classified,
            "dispositions_close": sum(overall["dispositions"].values()) == len(rows),
            "frequency_classes_close": sum(overall["source_frequency_classes"].values()) == classified,
            "off_panel_rows": off_panel["rows"],
            "on_panel_control_rows": controls["rows"],
        },
        "overall": overall,
        "off_panel": off_panel,
        "on_panel_controls": controls,
        "placeholder_frequency": {
            "rows": placeholders,
            "off_panel_rows": placeholder_off_panel,
            "fraction_of_located": None if not located else round(placeholders / located, 6),
            "fraction_of_classified": None
            if not classified
            else round(placeholders / classified, 6),
            "fraction_of_off_panel": None
            if not off_panel["rows"]
            else round(placeholder_off_panel / off_panel["rows"], 6),
        },
        "by_stratum": [
            {"stratum": stratum, **_tally(entries)} for stratum, entries in sorted(by_stratum.items())
        ],
        "by_analysis": by_analysis,
        "by_publication": by_publication,
    }


def _trace_summary_line(report: dict[str, Any], output: Path) -> str:
    summary = report["summary"]
    denominators = summary["denominators"]
    placeholders = summary["placeholder_frequency"]
    return (
        f"{report['store']['store_id']}: traced {denominators['rows']} sample rows "
        f"({denominators['rows_located']} located, {denominators['rows_unlocated']} not) | "
        f"off-panel {denominators['off_panel_rows']}, controls "
        f"{denominators['on_panel_control_rows']} | placeholder frequency "
        f"{placeholders['off_panel_rows']}/{denominators['off_panel_rows']} off-panel rows | "
        f"errors {report['error_count']}, findings {report['finding_count']}\n"
        f"evidence: {output} ({'ok' if report['ok'] else 'NOT OK'})"
    )


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
    attribute_parser = commands.add_parser(
        "attribute",
        help="attribute the off-panel union to its Analyses and publications",
        description=(
            "Attribute one Hybrid Store Release's off-panel union to the Analyses and "
            "publications that observe it, with exclusive variants, support across "
            "contributors, chromosome and allele class, and a seeded stratified sample "
            "for raw-row tracing. Exit 0 when every disagreement is a finding, 1 when the "
            "Store's artifact and its record disagree, 2 when the Store cannot be audited."
        ),
    )
    attribute_parser.add_argument(
        "store", help="path to a Store Release directory (store.opengwasdb)"
    )
    attribute_parser.add_argument(
        "--output", required=True, help="evidence JSON to write; must be outside the Store"
    )
    attribute_parser.add_argument(
        "--sample-output",
        default=None,
        help=(
            "seeded stratified sample TSV to write for downstream raw-row tracing; must be "
            "outside the Store. Omitted, the run writes no sample and says so"
        ),
    )
    attribute_parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_ATTRIBUTION_SEED,
        help=f"seed the sample's choices derive from (default {DEFAULT_ATTRIBUTION_SEED})",
    )
    attribute_parser.add_argument(
        "--top",
        type=int,
        default=DEFAULT_ATTRIBUTION_TOP,
        help=(
            "leading contributors the sample draws exclusive variants from and whose union "
            f"share is measured (default {DEFAULT_ATTRIBUTION_TOP})"
        ),
    )
    attribute_parser.add_argument(
        "--sample-per-stratum",
        type=int,
        default=DEFAULT_SAMPLE_PER_STRATUM,
        help=f"rows each sample stratum keeps (default {DEFAULT_SAMPLE_PER_STRATUM})",
    )
    attribute_parser.add_argument(
        "--band-variants",
        type=int,
        default=DEFAULT_ATTRIBUTION_BAND_VARIANTS,
        help=(
            "axis entries one band of the attribution and sample scans spans "
            f"(default {DEFAULT_ATTRIBUTION_BAND_VARIANTS}); the answer does not depend on it"
        ),
    )
    attribute_parser.add_argument(
        "--band-rows",
        type=int,
        default=DEFAULT_ATTRIBUTION_BAND_ROWS,
        help=(
            "association rows one band of the support scan spans "
            f"(default {DEFAULT_ATTRIBUTION_BAND_ROWS}); the answer does not depend on it"
        ),
    )
    attribute_parser.add_argument(
        "--verify",
        choices=("auto", "sha256", "metadata"),
        default="auto",
        help=(
            "how to prove the Store did not change: sha256 every file, compare size and "
            f"mtime only, or auto (sha256 up to {HASH_VERIFY_LIMIT_BYTES} bytes)"
        ),
    )
    trace_parser = commands.add_parser(
        "trace",
        help="trace sampled rows back through raw-source frequency admission",
        description=(
            "Trace every row of a written sample (#373) to its raw source row, its parsed "
            "frequency, the admission `admit_rows` gives it under the Analysis's declared "
            "MAF and INFO policies, and the Store's own identity and stored frequency "
            "codes for the variant. Exit 0 when every disagreement is a finding, 1 when "
            "the trace contradicts the Store, 2 when it cannot be traced at all."
        ),
    )
    trace_parser.add_argument(
        "store", help="path to a Store Release directory (store.opengwasdb)"
    )
    trace_parser.add_argument(
        "--sample",
        required=True,
        help=(
            "sample TSV to trace (`ogstores.audit attribute --sample-output` wrote one); "
            "must be outside the Store"
        ),
    )
    trace_parser.add_argument(
        "--manifest",
        required=True,
        help=(
            "the build manifest the Store was built from: its `source_file`/`source_url` "
            "is the only record of which raw file each Analysis was read from, and its "
            "`maf_threshold`/`info_score_threshold` columns are the policies replayed"
        ),
    )
    trace_parser.add_argument(
        "--output", required=True, help="evidence JSON to write; must be outside the Store"
    )
    trace_parser.add_argument(
        "--raw-root",
        default=str(DEFAULT_RAW_ROOT),
        help=(
            "where a manifest `source_url` is expected to land locally when its "
            f"`source_file` is gone (default {DEFAULT_RAW_ROOT})"
        ),
    )
    trace_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "trace at most this many sample rows (default: all of them); a limited run "
            "reports the rows it read and never pretends to close the sample"
        ),
    )
    trace_parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_TRACE_SOURCE_WORKERS,
        help=(
            "source files to scan in parallel; each file is read exactly once whatever "
            f"the sample draws from it (default {DEFAULT_TRACE_SOURCE_WORKERS})"
        ),
    )
    trace_parser.add_argument(
        "--verify",
        choices=("auto", "sha256", "metadata"),
        default="auto",
        help=(
            "how to prove the Store did not change: sha256 every file, compare size and "
            f"mtime only, or auto (sha256 up to {HASH_VERIFY_LIMIT_BYTES} bytes)"
        ),
    )
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


def _same_path(left: Path, right: Path) -> bool:
    """Whether two names resolve to the same file, whether or not it exists yet."""
    try:
        return Path(os.path.realpath(left)) == Path(os.path.realpath(right))
    except OSError:  # pragma: no cover - realpath does not raise for a missing tail
        return False


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
    skipped_stages = [entry["stage"] for entry in report["scope"]["skipped"]]
    skipped = f", skipped {', '.join(skipped_stages)}" if skipped_stages else ""
    return (
        f"{report['store']['store_id']}: axis {axis['n_axis']:,} = dense "
        f"{axis['n_dense']:,} + off-panel {axis['n_off_panel']:,} | off-panel "
        f"associations {occupancy['off_panel']['associations']:,}, observed "
        f"{occupancy['off_panel']['observed_variants']:,}, unobserved "
        f"{occupancy['off_panel']['unobserved_variants']:,} | duplicate ALID rows "
        f"{'not measured' if duplicates is None else f'{duplicates:,}'} | errors "
        f"{report['error_count']}, findings {report['finding_count']}"
        f"{skipped}\n"
        f"evidence: {output} ({'ok' if report['ok'] else 'NOT OK'})"
    )


def _write_report(report: dict[str, Any], output: Path, store_path: Path) -> None:
    """Write the evidence JSON atomically, through a name nobody can predict.

    The staged file is created by `tempfile.mkstemp` in the output's own
    directory, so it cannot be an existing file or a symlink a third party
    planted for this run to truncate (a fixed `output.with_name(".tmp")` can
    be both), and it is renamed onto the output with `os.replace`, so a reader
    sees either the previous evidence or the new one and never a half-written
    file. The staged path is checked against the Store before a byte is written
    to it: the Store is read-only for this command, wherever the caller asked
    for its evidence.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    handle, staged_name = tempfile.mkstemp(
        dir=output.parent, prefix=f".{output.name}.", suffix=".tmp"
    )
    staged = Path(staged_name)
    try:
        if _inside(staged, store_path):
            raise AuditError(
                f"refusing to write evidence inside the Store ({staged} is under "
                f"{store_path}); a read-only audit writes nothing there"
            )
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        staged.replace(output)
        staged = None
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        # argparse exits 2 on a bad invocation and 0 on --help; a caller that
        # embedded this command gets the code rather than a SystemExit.
        return int(exc.code or 0)
    if args.command not in {"reconcile", "attribute", "trace"}:  # pragma: no cover
        raise SystemExit(2)
    store_path = Path(args.store)
    output = Path(args.output)
    if getattr(args, "band_variants", 1) < 1:
        print("audit: --band-variants must be at least 1", file=sys.stderr)
        return 2
    if _inside(output, store_path):
        print(
            f"audit: refusing to write evidence inside the Store ({output} is under "
            f"{store_path}); a read-only audit writes nothing there",
            file=sys.stderr,
        )
        return 2
    if args.command == "attribute":
        return _main_attribute(args, store_path, output, argv)
    if args.command == "trace":
        return _main_trace(args, store_path, output, argv)
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
    try:
        _write_report(report, output, store_path)
    except AuditError as exc:
        print(f"audit: {exc}", file=sys.stderr)
        return 2
    print(_summary(report, output))
    return 0 if report["ok"] else 1


def _main_attribute(args: argparse.Namespace, store_path: Path, output: Path, argv: Sequence[str] | None) -> int:
    """Run `attribute`, writing its evidence and its sample."""
    sample_output = None if args.sample_output is None else Path(args.sample_output)
    if args.band_rows < 1:
        print("audit: --band-rows must be at least 1", file=sys.stderr)
        return 2
    if args.top < 1 or args.sample_per_stratum < 1:
        print("audit: --top and --sample-per-stratum must be at least 1", file=sys.stderr)
        return 2
    if sample_output is not None and _inside(sample_output, store_path):
        print(
            f"audit: refusing to write a sample inside the Store ({sample_output} is under "
            f"{store_path}); a read-only audit writes nothing there",
            file=sys.stderr,
        )
        return 2
    try:
        report = attribute(
            store_path,
            sample_output=sample_output,
            tables_dir=output.parent,
            seed=args.seed,
            top=args.top,
            sample_per_stratum=args.sample_per_stratum,
            band_variants=args.band_variants,
            band_rows=args.band_rows,
            verify=args.verify,
            argv=argv,
        )
        _write_report(report, output, store_path)
    except AuditError as exc:
        print(f"audit: {exc}", file=sys.stderr)
        return 2
    print(_attribution_summary(report, output))
    return 0 if report["ok"] else 1


def _main_trace(
    args: argparse.Namespace, store_path: Path, output: Path, argv: Sequence[str] | None
) -> int:
    """Run `trace`, writing its evidence."""
    sample_path = Path(args.sample)
    manifest_path = Path(args.manifest)
    if args.limit is not None and args.limit < 1:
        print("audit: --limit must be at least 1", file=sys.stderr)
        return 2
    if args.workers < 1:
        print("audit: --workers must be at least 1", file=sys.stderr)
        return 2
    for named, source in (("sample", sample_path), ("build manifest", manifest_path)):
        if _inside(source, store_path):
            print(
                f"audit: refusing to read the {named} from inside the Store ({source} is "
                f"under {store_path}); the inputs a trace reads are outside the artifact",
                file=sys.stderr,
            )
            return 2
        if _same_path(output, source):
            print(
                f"audit: refusing to write evidence over the {named} ({output} is the same "
                "file); a trace reads its inputs and never replaces them",
                file=sys.stderr,
            )
            return 2
    try:
        report = trace(
            store_path,
            sample_path=sample_path,
            manifest_path=manifest_path,
            raw_root=args.raw_root,
            limit=args.limit,
            workers=args.workers,
            verify=args.verify,
            argv=argv,
        )
        for entry in report["scope"]["sources"]:
            source_path = entry.get("path")
            if source_path and _same_path(output, Path(source_path)):
                print(
                    f"audit: refusing to write evidence over a raw source file ({output} is "
                    f"{source_path}); the sources a trace reads are never written to",
                    file=sys.stderr,
                )
                return 2
        _write_report(report, output, store_path)
    except AuditError as exc:
        print(f"audit: {exc}", file=sys.stderr)
        return 2
    print(_trace_summary_line(report, output))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
