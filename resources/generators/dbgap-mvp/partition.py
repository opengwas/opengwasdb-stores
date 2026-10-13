#!/usr/bin/env python3
"""Partition the PMID 39024449 MVP candidates into disjoint ancestry Stores (#221).

The dbGaP-acquisition mapping marks each Analysis with the ancestry parsed from
its member name, including `META` for cross-ancestry members. The legacy
candidate table still groups those META rows under the European ``store_key``
until the multi-ancestry split is applied, so this module makes the partition
explicit and fail-closed:

* every Analysis is classified into exactly one of EUR / AFR / AMR / EAS / META;
* the five sets are disjoint and their union is the whole intended membership;
* the META set is reported separately and never folded back into EUR.

It reads regular tabular metadata only -- never an association file, genotype
or LD panel -- and writes nothing outside its declared output.
"""

from __future__ import annotations

import csv
import io
from collections import OrderedDict
from pathlib import Path
from typing import Mapping, Sequence

#: The publication these candidates belong to.
MVP_PMID: str = "39024449"

#: The controlled ancestry buckets, in report order. `META` is the
#: cross-ancestry bucket and is always last.
ANCESTRY_ORDER: tuple[str, ...] = ("EUR", "AFR", "AMR", "EAS", "META")

#: The metadata-verified partition this ticket must reproduce. These are the
#: accepted immutable counts from the dbGaP mapping plus the candidate table;
#: `validate_partition` refuses any drift rather than silently accepting a new
#: membership.
EXPECTED_COUNTS: Mapping[str, int] = {
    "EUR": 1_669,
    "AFR": 1_368,
    "AMR": 1_053,
    "EAS": 241,
    "META": 1_691,
}

#: The intended candidate store key for each bucket. EUR/AFR/AMR/EAS already
#: have one in the candidate table; the META key is the pinned post-split name.
STORE_KEY_BY_ANCESTRY: Mapping[str, str] = {
    "EUR": "dense__pmid-39024449__European",
    "AFR": "dense__pmid-39024449__African",
    "AMR": "dense__pmid-39024449__Hispanic-or-Latin-American",
    "EAS": "dense__pmid-39024449__East-Asian",
    "META": "dense__pmid-39024449__Multi-ancestry",
}

#: The legacy candidate-table key that the META members currently share with EUR.
LEGACY_EUROPEAN_STORE_KEY: str = "dense__pmid-39024449__European"


class PartitionError(ValueError):
    """The metadata cannot be partitioned into the accepted disjoint sets."""


def read_mapping(path: Path | str) -> list[dict[str, str]]:
    """Read the dbGaP mapping TSV, requiring ``analysis_id`` and ``ancestry``."""
    mapping_path = Path(path)
    if not mapping_path.is_file():
        raise PartitionError(f"mapping TSV not found: {mapping_path}")
    with mapping_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        fieldnames = tuple(reader.fieldnames or ())
        for column in ("analysis_id", "ancestry"):
            if column not in fieldnames:
                raise PartitionError(
                    f"{mapping_path} is missing required column {column!r}; "
                    f"present: {fieldnames}"
                )
        rows = [{key: (value or "").strip() for key, value in row.items()} for row in reader]
    if not rows:
        raise PartitionError(f"mapping TSV has no data rows: {mapping_path}")
    return rows


def normalise_ancestry(raw: str) -> str:
    """Map a mapping ``ancestry`` value onto the controlled bucket, or fail."""
    value = (raw or "").strip().upper()
    if value in ANCESTRY_ORDER:
        return value
    # The dbGaP member names spell the cross-ancestry bucket `META`; anything
    # else is a new vocabulary item and must be reconciled, not guessed.
    raise PartitionError(
        f"unrecognised mapping ancestry {raw!r}; expected one of {ANCESTRY_ORDER}"
    )


def partition_mapping(rows: Sequence[Mapping[str, str]]) -> "OrderedDict[str, tuple[str, ...]]":
    """Return ``{ancestry: (analysis_id, ...)}`` with duplicates rejected."""
    grouped: dict[str, list[str]] = {ancestry: [] for ancestry in ANCESTRY_ORDER}
    seen: dict[str, str] = {}
    for row in rows:
        analysis_id = (row.get("analysis_id") or "").strip()
        if not analysis_id:
            raise PartitionError("mapping row has a blank analysis_id")
        ancestry = normalise_ancestry(row.get("ancestry") or "")
        if analysis_id in seen:
            raise PartitionError(
                f"analysis {analysis_id!r} appears more than once "
                f"({seen[analysis_id]} and {ancestry})"
            )
        seen[analysis_id] = ancestry
        grouped[ancestry].append(analysis_id)
    return OrderedDict((ancestry, tuple(grouped[ancestry])) for ancestry in ANCESTRY_ORDER)


def validate_partition(
    partition: Mapping[str, Sequence[str]],
    expected: Mapping[str, int] = EXPECTED_COUNTS,
) -> None:
    """Fail if the partition drifts from the accepted counts or is not disjoint."""
    missing = set(expected) - set(partition)
    if missing:
        raise PartitionError(f"partition is missing bucket(s): {sorted(missing)}")
    for ancestry in ANCESTRY_ORDER:
        actual = len(partition.get(ancestry, ()))
        want = expected[ancestry]
        if actual != want:
            raise PartitionError(
                f"ancestry {ancestry}: expected {want} Analyses, found {actual}"
            )
    all_ids = [analysis_id for ancestry in ANCESTRY_ORDER for analysis_id in partition[ancestry]]
    if len(all_ids) != len(set(all_ids)):
        raise PartitionError("partition buckets overlap; the ancestry sets are not disjoint")
    total = len(all_ids)
    expected_total = sum(expected.values())
    if total != expected_total:
        raise PartitionError(f"partition total {total} != accepted total {expected_total}")


def legacy_european_count(partition: Mapping[str, Sequence[str]]) -> int:
    """EUR + META, i.e. the legacy ``dense__pmid-39024449__European`` bucket."""
    return len(partition.get("EUR", ())) + len(partition.get("META", ()))


def render_partition_tsv(partition: Mapping[str, Sequence[str]]) -> str:
    """Render ``analysis_id, ancestry, store_key`` in a stable order."""
    output = io.StringIO()
    writer = csv.writer(output, delimiter="\t", lineterminator="\n")
    writer.writerow(("analysis_id", "ancestry", "store_key"))
    for ancestry in ANCESTRY_ORDER:
        store_key = STORE_KEY_BY_ANCESTRY[ancestry]
        for analysis_id in partition.get(ancestry, ()):
            writer.writerow((analysis_id, ancestry, store_key))
    return output.getvalue()


__all__ = [
    "ANCESTRY_ORDER",
    "EXPECTED_COUNTS",
    "LEGACY_EUROPEAN_STORE_KEY",
    "MVP_PMID",
    "STORE_KEY_BY_ANCESTRY",
    "PartitionError",
    "legacy_european_count",
    "normalise_ancestry",
    "partition_mapping",
    "read_mapping",
    "render_partition_tsv",
    "validate_partition",
]
