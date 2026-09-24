#!/usr/bin/env python3
"""Harvest the source-provided Trait Ontology Mapping validation set (issue #165).

Trait Ontology Mapping is frozen into every Release Manifest's `analyses.tsv`.
Rows whose `trait_ontology_mapping_method` is `source_provided` already carry
the Source Collection's own ontology term for the Trait. That makes them a
*ground truth*: a set of ``(trait_label -> ontology_id)`` pairs that retrieval
can be scored against, without a curator ever having to hand-label anything.

This module turns an explicit set of committed Release Manifests into that
validation set. It never writes a manifest, a generator, or a bundle.

What it does
------------
- Accepts one or more Release Manifest paths: an `analyses.tsv` file, or a
  bundle directory containing one.
- Selects only rows whose `trait_ontology_mapping_method` is `source_provided`.
- Extracts the unique
  ``(trait_label, source_ontology_id, source_ontology_label, stratum)`` pairs,
  unioning the Store Families each pair was seen in.
- Assigns each pair a stratum:

  ``analyte_measurement``
      A term from an analyte/measurement Store Family (metabolome, pqtl,
      proteome, ...) or an EFO/OBA measurement term.
  ``disease``
      A MONDO term, or a disease trait from a disease family such as the GWAS
      Catalog hybrid.
  ``other``
      A source-provided term that fits neither stratum. It is kept rather than
      dropped so the validation set is honest about what it covers.

- Cross-references each term against the pinned ontology release's retrieval
  index (from :mod:`curation.ontology`), when one is supplied, and flags
  obsolete terms. A term absent from the index falls back to the source
  label's ``obsolete_`` convention; absence alone never invents an obsolete
  flag.

Output
------
A six-column TSV to stdout or `--output`::

    trait_label  ontology_id  ontology_label  stratum  store_families  is_obsolete

The obsolete flag is a boolean string (``true``/``false``). Obsolete pairs are
*flagged*, not dropped here, because scoring decides whether to exclude them
(see :mod:`curation.recall`).

CLI
---
::

    python3 -m curation.harvest <manifest>... [--index <index.json>] \\
        [--output <validation.tsv>]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from curation.gap_scan import (
    MAPPING_METHOD_COLUMN,
    TRAIT_LABEL_COLUMNS,
    MalformedRowError,
    MissingManifestError,
    MissingTraitLabelColumnError,
    derive_store_family,
    read_analyses,
    resolve_manifest_path,
)
from curation.ontology import OntologyIndex

SOURCE_PROVIDED: str = "source_provided"

ONTOLOGY_ID_COLUMN: str = "trait_ontology_id"
ONTOLOGY_LABEL_COLUMN: str = "trait_ontology_label"

# The two strata the recall report is built around, plus an honest catch-all.
STRATUM_ANALYTE_MEASUREMENT: str = "analyte_measurement"
STRATUM_DISEASE: str = "disease"
STRATUM_OTHER: str = "other"
STRATUM_ORDER: tuple[str, ...] = (
    STRATUM_ANALYTE_MEASUREMENT,
    STRATUM_DISEASE,
    STRATUM_OTHER,
)

# Ontology prefixes that unambiguously place a term. OBA is the Ontology of
# Biological Attributes (measurements); MONDO is the disease ontology.
ANALYTE_ONTOLOGY_PREFIXES: tuple[str, ...] = ("OBA:",)
DISEASE_ONTOLOGY_PREFIXES: tuple[str, ...] = ("MONDO:",)

# Store Family name fragments. Families are resolved by
# `curation.gap_scan.derive_store_family`; matching is a case-insensitive
# substring test because the retired Store Family tier leaves no single field
# (ADR 0028).
ANALYTE_FAMILY_KEYWORDS: tuple[str, ...] = (
    "metabolome",
    "metabolomic",
    "pqtl",
    "proteome",
    "proteomic",
    "olink",
    "analyte",
)
DISEASE_FAMILY_KEYWORDS: tuple[str, ...] = (
    "gwas-catalog",
    "gwas_catalog",
    "finngen",
    "disease",
    "hybrid",
)

# Measurement terms are EFO/OBA terms whose label names a measurement. The
# parent label is included because it is what distinguishes a measurement term
# from a disease term of the same name.
_MEASUREMENT_MARKERS: tuple[str, ...] = ("measurement", "measured", "level")

OUTPUT_COLUMNS: tuple[str, ...] = (
    "trait_label",
    "ontology_id",
    "ontology_label",
    "stratum",
    "store_families",
    "is_obsolete",
)


@dataclass(frozen=True)
class HarvestEntry:
    """One unique source-provided ``(trait_label, ontology_id)`` validation pair."""

    trait_label: str
    ontology_id: str
    ontology_label: str
    stratum: str
    store_families: tuple[str, ...]
    is_obsolete: bool

    def key(self) -> tuple[str, str, str, str]:
        """The uniqueness key: the pair plus its stratum."""
        return (self.trait_label, self.ontology_id, self.ontology_label, self.stratum)

    def to_row(self) -> list[str]:
        return [
            self.trait_label,
            self.ontology_id,
            self.ontology_label,
            self.stratum,
            ",".join(self.store_families),
            "true" if self.is_obsolete else "false",
        ]


def _normalise_text(value: str | None) -> str:
    """Lowercase and collapse whitespace for keyword matching only."""
    return " ".join((value or "").strip().lower().split())


def _family_matches(families: Sequence[str], keywords: Sequence[str]) -> bool:
    text = " ".join(families).lower()
    return any(keyword in text for keyword in keywords)


def categorize_stratum(
    ontology_id: str,
    store_families: Sequence[str],
    ontology_label: str = "",
    parent_label: str = "",
) -> str:
    """Place a source-provided pair in one of the validation strata.

    Precedence is deliberate and documented so the same pair always lands in
    the same stratum:

    1. A MONDO identifier is a disease; an OBA identifier is a measurement.
    2. An analyte/measurement Store Family (metabolome, pqtl, ...) is a
       measurement regardless of the ontology identifier.
    3. An EFO/other term whose own or parent label names a measurement is a
       measurement.
    4. A disease Store Family (GWAS Catalog hybrid, FinnGen, ...) is a disease.
    5. Anything else is ``other``.
    """
    identifier = (ontology_id or "").strip().upper()
    if identifier.startswith(DISEASE_ONTOLOGY_PREFIXES):
        return STRATUM_DISEASE
    if identifier.startswith(ANALYTE_ONTOLOGY_PREFIXES):
        return STRATUM_ANALYTE_MEASUREMENT

    if _family_matches(store_families, ANALYTE_FAMILY_KEYWORDS):
        return STRATUM_ANALYTE_MEASUREMENT

    label_text = _normalise_text(f"{ontology_label} {parent_label}")
    if any(marker in label_text for marker in _MEASUREMENT_MARKERS):
        return STRATUM_ANALYTE_MEASUREMENT

    if _family_matches(store_families, DISEASE_FAMILY_KEYWORDS):
        return STRATUM_DISEASE

    return STRATUM_OTHER


def detect_obsolete(
    ontology_id: str,
    ontology_label: str,
    index_by_id: Mapping[str, object] | None,
) -> bool:
    """Flag a term obsolete using the pinned index, then the source label.

    A term found in the pinned index reports the index's own obsolete flag. A
    term absent from the index is *not* assumed obsolete; the only fallback is
    the Source Collection's ``obsolete_`` label convention, which is explicit
    in the data rather than guessed.
    """
    if index_by_id:
        term = index_by_id.get(ontology_id)
        if term is not None:
            return bool(getattr(term, "is_obsolete", False))
    return _normalise_text(ontology_label).startswith("obsolete")


def scan_manifest(
    manifest_path: Path | str,
    index_by_id: Mapping[str, object] | None = None,
) -> list[HarvestEntry]:
    """Harvest one Manifest's source-provided validation pairs.

    A Manifest without the ``trait_ontology_mapping_method`` column carries no
    Trait Ontology Mapping and is skipped with a stderr warning; a Manifest
    with the column but no ``trait_ontology_id`` column cannot supply a ground
    truth and is skipped with a warning. Neither is an error.
    """
    manifest = resolve_manifest_path(manifest_path)
    columns, rows = read_analyses(manifest)

    if MAPPING_METHOD_COLUMN not in columns:
        print(
            f"harvest: warning: {manifest} has no {MAPPING_METHOD_COLUMN} column; "
            "skipping it (it carries no Trait Ontology Mapping)",
            file=sys.stderr,
        )
        return []

    label_column = next((c for c in TRAIT_LABEL_COLUMNS if c in columns), None)
    if label_column is None:
        raise MissingTraitLabelColumnError(
            f"{manifest} has {MAPPING_METHOD_COLUMN} but none of "
            f"{', '.join(TRAIT_LABEL_COLUMNS)}; cannot identify a trait label"
        )

    if ONTOLOGY_ID_COLUMN not in columns:
        print(
            f"harvest: warning: {manifest} has no {ONTOLOGY_ID_COLUMN} column; "
            "skipping it (it carries no source-provided ontology term)",
            file=sys.stderr,
        )
        return []

    family = derive_store_family(manifest)
    entries: list[HarvestEntry] = []

    for row in rows:
        if _normalise_text(row.get(MAPPING_METHOD_COLUMN)) != SOURCE_PROVIDED:
            continue
        trait_label = (row.get(label_column) or "").strip()
        ontology_id = (row.get(ONTOLOGY_ID_COLUMN) or "").strip()
        ontology_label = (row.get(ONTOLOGY_LABEL_COLUMN) or "").strip()
        if not trait_label or not ontology_id:
            # A source-provided row with no label or no identifier has nothing
            # to validate; skipping keeps absence distinct from a real pair.
            continue
        stratum = categorize_stratum(
            ontology_id, (family,), ontology_label=ontology_label
        )
        entries.append(
            HarvestEntry(
                trait_label=trait_label,
                ontology_id=ontology_id,
                ontology_label=ontology_label,
                stratum=stratum,
                store_families=(family,),
                is_obsolete=detect_obsolete(ontology_id, ontology_label, index_by_id),
            )
        )
    return entries


def harvest(
    manifest_paths: Iterable[Path | str],
    index: OntologyIndex | None = None,
) -> list[HarvestEntry]:
    """Harvest and aggregate the validation set over every Manifest path.

    Pairs are unique on ``(trait_label, ontology_id, ontology_label, stratum)``;
    their Store Families are unioned and the obsolete flag is the OR across
    occurrences. The result is ordered by stratum, then trait label, then
    ontology id, so the output is deterministic.
    """
    index_by_id = index.by_id() if index is not None else None
    families: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
    obsolete: dict[tuple[str, str, str, str], bool] = {}
    fields: dict[tuple[str, str, str, str], HarvestEntry] = {}

    for manifest_path in manifest_paths:
        for entry in scan_manifest(manifest_path, index_by_id):
            key = entry.key()
            fields[key] = entry
            families[key].update(entry.store_families)
            obsolete[key] = obsolete.get(key, False) or entry.is_obsolete

    stratum_rank = {stratum: n for n, stratum in enumerate(STRATUM_ORDER)}
    entries = [
        HarvestEntry(
            trait_label=key[0],
            ontology_id=key[1],
            ontology_label=key[2],
            stratum=key[3],
            store_families=tuple(sorted(families[key])),
            is_obsolete=obsolete[key],
        )
        for key in fields
    ]
    entries.sort(
        key=lambda entry: (
            stratum_rank.get(entry.stratum, len(stratum_rank)),
            entry.trait_label,
            entry.ontology_id,
        )
    )
    return entries


def format_harvest_tsv(entries: Sequence[HarvestEntry]) -> str:
    """Render the validation set as the six-column TSV (header always present)."""
    lines = ["\t".join(OUTPUT_COLUMNS)]
    lines.extend("\t".join(entry.to_row()) for entry in entries)
    return "\n".join(lines) + "\n"


def _write_text_atomically(text: str, dest_path: Path) -> None:
    """Atomically write text via a temp file + os.replace, matching run.py/manifest.py."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = dest_path.with_name(
        f".{dest_path.name}.tmp.{os.getpid()}.{time.time_ns()}"
    )
    with open(temp_path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, dest_path)


def _silence_broken_pipe() -> None:
    """Point stdout at /dev/null so interpreter shutdown does not re-raise SIGPIPE."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError):
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harvest",
        description=(
            "Harvest the source-provided Trait Ontology Mapping validation set "
            "from committed Release Manifests."
        ),
    )
    parser.add_argument(
        "manifests",
        nargs="+",
        metavar="MANIFEST",
        help="path to an analyses.tsv or to a bundle directory containing one",
    )
    parser.add_argument(
        "--index",
        default=None,
        metavar="JSON",
        help=(
            "retrieval index built from the pinned ontology release, used to "
            "flag obsolete terms"
        ),
    )
    parser.add_argument(
        "--output",
        default=None,
        metavar="TSV",
        help="write the validation set TSV here instead of stdout",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    index: OntologyIndex | None = None
    if args.index:
        # Imported lazily so the module's core remains standard-library-only.
        from curation.ontology import IndexFormatError, load_index

        try:
            index = load_index(args.index)
        except IndexFormatError as exc:
            print(f"harvest: error: {exc}", file=sys.stderr)
            return 1

    try:
        entries = harvest(args.manifests, index)
    except (MissingManifestError, MissingTraitLabelColumnError, MalformedRowError) as exc:
        print(f"harvest: error: {exc}", file=sys.stderr)
        return 1

    text = format_harvest_tsv(entries)
    if args.output:
        _write_text_atomically(text, Path(args.output))
    else:
        try:
            sys.stdout.write(text)
            sys.stdout.flush()
        except BrokenPipeError:
            _silence_broken_pipe()
            return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        _silence_broken_pipe()
        sys.exit(1)
