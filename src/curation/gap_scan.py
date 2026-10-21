#!/usr/bin/env python3
"""Derive the unmapped Trait work queue from committed Release Manifests.

Trait Ontology Mapping is resolved before build and frozen into every Release
Manifest's `analyses.tsv` as Analytical Metadata (see `CONTEXT.md`,
"Trait Ontology Mapping" and "Canonical Trait Mapping Table"). A row whose
`trait_ontology_mapping_method` is `unmapped` is a gap: the Analysis has a Trait
but no EFO/MONDO/OBA/GO term. This stage turns those rows into the prioritised
work queue that Canonical Trait Mapping Table curation consumes, by scanning an
explicit set of committed Release Manifests -- it never writes a manifest, a
generator, or a bundle.

What it does
------------
- Accepts one or more Release Manifest paths: an `analyses.tsv` file, or a
  bundle directory containing one.
- Selects only rows whose `trait_ontology_mapping_method` is `unmapped`.
- Normalises each row's Trait label exactly as the Canonical Trait Mapping
  Table lookup does -- trim, then lowercase
  (`resources/generators/lib/metadata_resolvers/canonical_trait_table.R`,
  `.normalise_trait_label`) -- so labels differing only by case or surrounding
  whitespace collapse into one queue entry.
- Sums the occurrence count across every scanned Manifest and records the
  Store Families the label appeared in.
- Writes, in descending occurrence order, a three-column TSV to stdout or to
  `--output`: `trait_label`, `occurrence_count`, `store_families`.

A Manifest with no unmapped rows (for example a `gwas-ssf-ragged` bundle whose
GWAS Catalog source supplies an ontology term for every Analysis) contributes
nothing and the command still exits 0. A Manifest that predates the
`trait_ontology_mapping_method` column carries no mapping information at all and
is therefore skipped with a warning on stderr rather than failing the whole
scan -- it cannot contribute an unmapped row either way.

Store Family attribution
------------------------
The Store Family tier was retired (ADR 0028), so no single field names it today.
`derive_store_family` resolves the family for each Manifest with a documented
precedence: a `store_family_id` (or legacy `family`) in a sibling `release.yaml`
wins; otherwise the directory above a `releases/` path segment (the historical
`families/<family>/releases/<release>/` layout); otherwise the directory above a
`families/` segment; otherwise the bundle directory's own name (the
`stores/<store-id>/` layout, where the `OGS-` id is the only identifier). The
value is only ever an attribution label for the queue -- it does not restate a
retired registry tier.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import yaml

ANALYSES_FILENAME: str = "analyses.tsv"
RELEASE_FILENAME: str = "release.yaml"

MAPPING_METHOD_COLUMN: str = "trait_ontology_mapping_method"
UNMAPPED: str = "unmapped"

# The Canonical Trait Mapping Table is keyed on the Analysis's own trait label,
# which the generators write as `source_label` (GWAS-SSF's `DISEASE.TRAIT`,
# GWAS-VCF's field description, FinnGen's phenotype). The fallbacks keep the
# scan usable on simplified or fixture manifests that name the same concept
# differently, without changing which column a full Release Manifest uses.
TRAIT_LABEL_COLUMNS: tuple[str, ...] = ("source_label", "analysis_label", "trait_label")

OUTPUT_COLUMNS: tuple[str, ...] = ("trait_label", "occurrence_count", "store_families")


class GapScanError(ValueError):
    """Base error for a Release Manifest that cannot be scanned."""


class MissingManifestError(GapScanError):
    """Raised when a Manifest path does not exist or holds no analyses.tsv."""


class MissingTraitLabelColumnError(GapScanError):
    """Raised when an unmapped-capable Manifest names no recognisable trait label."""


class MalformedRowError(GapScanError):
    """Raised when a data row's field count differs from the header's.

    A short row would otherwise have its missing trait-label fields invented as
    blanks and quietly dropped from the queue, under-counting the work.
    """


@dataclass(frozen=True)
class QueueEntry:
    """One prioritised unmapped trait label in the work queue."""

    trait_label: str
    occurrence_count: int
    store_families: tuple[str, ...]

    def to_row(self) -> list[str]:
        return [
            self.trait_label,
            str(self.occurrence_count),
            ",".join(self.store_families),
        ]


def normalize_trait_label(label: str | None) -> str:
    """Normalise a trait label exactly as the canonical-table lookup does.

    `trimws(tolower(x))` in the R resolver: strip surrounding whitespace, then
    lowercase. Labels that differ only by case or surrounding whitespace are the
    same curation candidate.
    """
    return (label or "").strip().lower()


def resolve_manifest_path(path: Path | str) -> Path:
    """Resolve a Manifest argument to its `analyses.tsv` file.

    A file argument is used as given (the operator may pass the table directly);
    a directory argument is taken to be a Release Bundle and joined with
    `analyses.tsv`.
    """
    candidate = Path(path)
    if candidate.is_dir():
        manifest = candidate / ANALYSES_FILENAME
        if not manifest.is_file():
            raise MissingManifestError(
                f"{candidate} is a directory but contains no {ANALYSES_FILENAME}"
            )
        return manifest
    if not candidate.is_file():
        raise MissingManifestError(f"Manifest path does not exist: {candidate}")
    return candidate


def _store_family_from_release_yaml(release_path: Path) -> str | None:
    """Read the explicit family identifier from a sibling `release.yaml`, if any."""
    if not release_path.is_file():
        return None
    try:
        with open(release_path, "r", encoding="utf-8") as f:
            release = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise GapScanError(f"{release_path} is not valid YAML: {exc}") from exc
    if not isinstance(release, dict):
        return None
    for key in ("store_family_id", "family"):
        value = release.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _store_family_from_path(bundle_dir: Path) -> str | None:
    """Derive the family from the historical bundle path layout, if recognisable."""
    parts = bundle_dir.parts
    # families/<family>/releases/<release>/analyses.tsv -- the segment before
    # the nearest `releases` is the family.
    for index in range(len(parts) - 1, -1, -1):
        if parts[index] == "releases" and index > 0:
            return parts[index - 1]
    # families/<family>/... even without a `releases/` level.
    for index in range(len(parts) - 1):
        if parts[index] == "families":
            return parts[index + 1]
    return None


def derive_store_family(manifest_path: Path | str) -> str:
    """Attribute a Manifest to a Store Family for the queue's `store_families`.

    Precedence: explicit `store_family_id`/`family` in a sibling `release.yaml`;
    the directory above a `releases/` path segment; the directory after a
    `families/` segment; else the bundle directory's own name.
    """
    manifest = resolve_manifest_path(manifest_path)
    bundle_dir = manifest.parent

    from_metadata = _store_family_from_release_yaml(bundle_dir / RELEASE_FILENAME)
    if from_metadata:
        return from_metadata

    from_path = _store_family_from_path(bundle_dir)
    if from_path:
        return from_path

    return bundle_dir.name or str(bundle_dir)


def read_analyses(manifest: Path | str) -> tuple[list[str], list[dict[str, str]]]:
    """Read an `analyses.tsv` into its header and row dictionaries.

    Raises `MalformedRowError` when a data row's field count differs from the
    header's, so a ragged row cannot silently lose its trait label.
    """
    path = Path(manifest)
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f, delimiter="\t")
        header = next(reader, None)
        columns = list(header) if header is not None else []
        rows: list[dict[str, str]] = []
        for row_index, fields in enumerate(reader):
            if len(fields) != len(columns):
                raise MalformedRowError(
                    f"{path} data row {row_index} has {len(fields)} fields; "
                    f"header has {len(columns)}"
                )
            rows.append(dict(zip(columns, fields)))
    return columns, rows


def scan_manifest(manifest_path: Path | str) -> dict[str, tuple[int, set[str]]]:
    """Scan one Manifest, returning `{normalized_label: (count, {families})}`.

    A Manifest without the `trait_ontology_mapping_method` column carries no
    Trait Ontology Mapping and yields an empty result with a stderr warning;
    a Manifest with the column but no unmapped rows yields an empty result
    silently, which is the ordinary clean case.
    """
    manifest = resolve_manifest_path(manifest_path)
    columns, rows = read_analyses(manifest)

    if MAPPING_METHOD_COLUMN not in columns:
        print(
            f"gap-scan: warning: {manifest} has no {MAPPING_METHOD_COLUMN} column; "
            "skipping it (it carries no Trait Ontology Mapping)",
            file=sys.stderr,
        )
        return {}

    label_column = next((c for c in TRAIT_LABEL_COLUMNS if c in columns), None)
    if label_column is None:
        raise MissingTraitLabelColumnError(
            f"{manifest} has {MAPPING_METHOD_COLUMN} but none of "
            f"{', '.join(TRAIT_LABEL_COLUMNS)}; cannot identify a trait label"
        )

    family = derive_store_family(manifest)
    counts: dict[str, int] = defaultdict(int)
    families: dict[str, set[str]] = defaultdict(set)

    for row in rows:
        if normalize_trait_label(row.get(MAPPING_METHOD_COLUMN)) != UNMAPPED:
            continue
        label = normalize_trait_label(row.get(label_column))
        if not label:
            # An unmapped row with no label has nothing to curate; it is not a
            # work item. Skipping keeps absence distinct from a real zero.
            continue
        counts[label] += 1
        families[label].add(family)

    return {label: (counts[label], families[label]) for label in counts}


def scan_manifests(manifest_paths: Iterable[Path | str]) -> list[QueueEntry]:
    """Aggregate the unmapped work queue over every Manifest path.

    Collapses labels differing only by case or surrounding whitespace, sums
    their occurrence counts, unions their Store Families, and orders the queue
    descending by occurrence count with an alphabetical tie-break so the output
    is deterministic.
    """
    counts: dict[str, int] = defaultdict(int)
    families: dict[str, set[str]] = defaultdict(set)

    for manifest_path in manifest_paths:
        for label, (count, label_families) in scan_manifest(manifest_path).items():
            counts[label] += count
            families[label].update(label_families)

    entries = [
        QueueEntry(
            trait_label=label,
            occurrence_count=counts[label],
            store_families=tuple(sorted(families[label])),
        )
        for label in counts
    ]
    entries.sort(key=lambda entry: (-entry.occurrence_count, entry.trait_label))
    return entries


def format_queue_tsv(entries: Sequence[QueueEntry]) -> str:
    """Render the queue as the three-column output TSV (header always present)."""
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
        prog="gap-scan",
        description=(
            "Derive the unmapped Trait work queue from committed Release "
            "Manifests (analyses.tsv files or bundle directories)."
        ),
    )
    parser.add_argument(
        "manifests",
        nargs="+",
        metavar="MANIFEST",
        help="path to an analyses.tsv or to a bundle directory containing one",
    )
    parser.add_argument(
        "--output",
        default=None,
        metavar="TSV",
        help="write the queue TSV here instead of stdout",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        entries = scan_manifests(args.manifests)
    except GapScanError as exc:
        print(f"gap-scan: error: {exc}", file=sys.stderr)
        return 1

    text = format_queue_tsv(entries)
    if args.output:
        _write_text_atomically(text, Path(args.output))
    else:
        try:
            sys.stdout.write(text)
            sys.stdout.flush()
        except BrokenPipeError:
            # A downstream `| head` closed the pipe; exit quietly rather than
            # printing a traceback at interpreter shutdown.
            _silence_broken_pipe()
            return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        _silence_broken_pipe()
        sys.exit(1)
