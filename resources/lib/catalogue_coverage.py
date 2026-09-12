#!/usr/bin/env python3
"""Derive the route-catalogue coverage table from a release's sources (issue #104).

The catalogue-routed pre-build path is ``assign-ancestry`` -> ``route-catalogue``
-> ``build-hybrid-from-catalogue``. ``route-catalogue`` decides each Analysis's
final routing with a genome-wide coverage gate, but it reads that coverage from a
*separate* TSV (``trait_id``, ``total_variants``, ``n_autosomes``,
``frac_largest_chrom``) rather than from the Catalogue. This module derives that
table, family-free, from the release's own selected sources.

It reads every source through the release's configured Source Reader Capability
(the same ``opengwasdb.readers.registry`` seam the builders and ancestry
assignment use), so the counts describe the variants OpenGWASDB will actually
build, not a re-implemented per-format parser. Derivation is deterministic: rows
are read in the manifest's order and each source is counted independently, so the
same sources always produce the same table regardless of worker count.

The result is deliberately not a committed fixed input: it is a derived work
artifact (ADR 0015), written under the release's ``work/`` directory and bound
into the route phase's completion record, so a changed source invalidates
routing exactly as it invalidates the build.
"""
from __future__ import annotations

import csv
import multiprocessing
import os
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from opengwasdb.model.enums import StoredEffectScale
from opengwasdb.readers.registry import resolve_reader
from opengwasdb.variants.normalise import normalise_chromosome

#: The columns ``opengwasdb route-catalogue`` reads from its coverage TSV.
COVERAGE_COLUMNS: tuple[str, ...] = (
    "trait_id",
    "total_variants",
    "n_autosomes",
    "frac_largest_chrom",
)

#: Human autosomes. The coverage gate wants a genome-wide store, so a source
#: observed only on the sex chromosomes or on too few autosomes is not eligible.
AUTOSOMES: frozenset[str] = frozenset(str(number) for number in range(1, 23))


def source_coverage(trait_id: str, file_path: str | Path, capability: str) -> dict[str, str]:
    """Count one source's variants and their chromosome distribution.

    ``total_variants`` is every variant the reader yields; ``n_autosomes`` is how
    many distinct autosomes it spans; ``frac_largest_chrom`` is the largest
    single-chromosome share. A source the reader yields no variants for is
    reported as zero variants over zero autosomes with ``frac_largest_chrom`` 1,
    which ``opengwasdb.ancestry.routing`` treats as low-coverage rather than as
    an error.
    """
    reader = resolve_reader(capability, file_path, StoredEffectScale.SD)
    counts: dict[str, int] = {}
    total = 0
    for variant in reader.stream_variants():
        chromosome = normalise_chromosome(variant.chromosome)
        counts[chromosome] = counts.get(chromosome, 0) + 1
        total += 1
    n_autosomes = sum(1 for chromosome in counts if chromosome in AUTOSOMES)
    frac_largest = (max(counts.values()) / total) if total else 1.0
    return {
        "trait_id": trait_id,
        "total_variants": str(total),
        "n_autosomes": str(n_autosomes),
        "frac_largest_chrom": f"{frac_largest:.10g}",
    }


def _coverage_task(task: tuple[str, str, str]) -> dict[str, str]:
    trait_id, file_path, capability = task
    return source_coverage(trait_id, file_path, capability)


def derive_coverage_rows(
    rows: Mapping[str, str] | Iterable[Mapping[str, str]],
    *,
    capability: str,
    n_workers: int = 1,
) -> list[dict[str, str]]:
    """The coverage rows for a release's source-manifest rows, in manifest order."""
    tasks = [
        (str(row["trait_id"]), str(row["file_path"]), capability)
        for row in rows
    ]
    if n_workers > 1 and len(tasks) > 1:
        context = multiprocessing.get_context("fork")
        with ProcessPoolExecutor(max_workers=n_workers, mp_context=context) as pool:
            return list(pool.map(_coverage_task, tasks))
    return [_coverage_task(task) for task in tasks]


def write_coverage(rows: Sequence[Mapping[str, str]], out_path: str | Path) -> Path:
    """Write the coverage table as a tab-separated TSV."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = out_path.with_name(out_path.name + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=list(COVERAGE_COLUMNS), delimiter="\t", lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows({column: row.get(column, "") for column in COVERAGE_COLUMNS} for row in rows)
    os.replace(temporary, out_path)
    return out_path


def derive_coverage(
    rows: Mapping[str, str] | Iterable[Mapping[str, str]],
    out_path: str | Path,
    *,
    capability: str,
    n_workers: int = 1,
) -> list[dict[str, str]]:
    """Derive the coverage rows and write them to ``out_path``."""
    coverage = derive_coverage_rows(rows, capability=capability, n_workers=n_workers)
    write_coverage(coverage, out_path)
    return coverage
