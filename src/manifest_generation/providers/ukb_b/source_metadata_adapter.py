#!/usr/bin/env python3
"""ukb-b metadata adapter: the declared selection plus provider JSONs -> Source Metadata Table (#289).

ukb-b publishes no manifest of its own that this host can read. Its membership
is the registry's **declared selection**
(``resources/inventories/ukb-b-restored-2026-10-11.declared-selection.tsv``,
2,514 Analyses, whose ``inclusion_reason`` cites the OpenGWAS IGD
``data/ukb-b/manifest.tsv``), and its per-Analysis study metadata is the
**provider JSON sidecar** beside each VCF (``<id>.json``): the same
study-level fields OpenGWAS's ``/api/gwasinfo`` served, which the registry can
no longer ask for because the stored token expired on 2026-08-21. This adapter
joins the two into the canonical Source Metadata Table the shared freeze reads
(``manifest_generation.freeze``), so ukb-b needs only an Acquisition Manifest
and a release config beside it.

It reuses the resolution the restored inventory already recorded
(``providers.ukb_b.provider_metadata``): ``resolve_record`` applies the live
resolver's contract to one provider JSON with the exact-ID check (``json.id``
must be the frozen analysis id) and the batch check (``ukb-b``), and
``build_resolution_rows`` derives the study design, the total sample size and
the case/control counts from it. What the adapter adds is the *canonical*
projection and the refusals:

- **Intent is the declared selection.** Every declared Analysis must have a
  provider JSON, and no other Analysis is written. A declared id listed twice,
  a provider JSON whose ``id`` or ``batch`` does not match, a provider record
  with neither a usable ``ncase``/``ncontrol`` pair nor a usable
  ``sample_size``, and a declared Source Trait Label that disagrees with the
  provider's are each refused by name, so the table never shrinks or invents
  silently.
- **No effect scale is declared.** Every ukb-b Analysis is on the SD scale
  (the provider's ``units`` is ``SD`` for all 2,514), including the 1,846
  binary traits, which is what OGS-00009 declares. The release config's
  ``defaults.by_study_design`` tiers decide ``stored_effect_scale``,
  ``original_effect_scale``, ``original_sd_method`` and ``sample_size_kind``
  (``sd`` / ``declared_standardised``), and the adapter writes no
  ``declared_effect_scale`` that could drift from them (#287).
- **Collection-wide facts are not written.** The GRCh37 build, the UK Biobank
  terms of access, ``MRC-IEU`` and the first author are the same for every
  Analysis, so the release config's ``defaults`` supply them (#287). The Store's
  ``analysis_group_id`` (``ukb-b``) is a release default too.
- **The Source Ancestry Label** is the provider's own ``population`` label
  (``European`` for all 2,514), carried verbatim, and translated to
  ``source_superpopulation`` through the tracked map
  (``source_ancestry.load_source_label_translation``); a label the map cannot
  translate is refused by name rather than left blank. It is a source label,
  never an Assigned Ancestry: ``af_assigned`` is not claimed here or anywhere
  else in Manifest Generation.
- **Study design** is ``case-control`` when the provider declares a usable
  case/control pair and ``quantitative`` otherwise, and the counts are carried
  only for a ``case-control`` row -- a zero count where counts do not apply is
  the absence-vs-zero confusion ``bundle.check()`` refuses (#133).

Usage:
    python3 -m manifest_generation.providers.ukb_b.source_metadata_adapter \\
        --declared-selection resources/inventories/ukb-b-restored-2026-10-11.declared-selection.tsv \\
        --source-dir /data/opengwasdb/raw/gwas-vcf/ukb-b \\
        --out <source-metadata.tsv>
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Mapping, Sequence

from manifest_generation import REPO_ROOT
from manifest_generation.connector_tsv import listed, raise_problems, write_connector
from manifest_generation.providers.ukb_b.provider_metadata import (
    build_resolution_rows,
    read_provider_records,
)
from manifest_generation.source_ancestry import (
    SourceLabelTranslation,
    UnknownSourceAncestryLabel,
    load_source_label_translation,
)
from manifest_generation.source_inventory import InventoryError
from manifest_generation.source_metadata_table import (
    SOURCE_METADATA_TABLE,
    load_source_metadata_table,
)

#: The declared selection's columns the adapter reads. ``analysis_id`` and
#: ``source_analysis_id`` are the same id in this file; ``source_label`` is the
#: trait label the declaration carries, cross-checked against the provider's.
ADAPTER_INPUT_COLUMNS: tuple[str, ...] = (
    "analysis_id",
    "source_analysis_id",
    "source_label",
)

#: The canonical columns the adapter writes.
OUTPUT_COLUMNS: tuple[str, ...] = (
    "source_analysis_id",
    "source_trait_label",
    "source_trait_id",
    "source_ancestry_label",
    "source_superpopulation",
    "study_design",
    "sample_size",
    "n_cases",
    "n_controls",
)

#: The declared selection's Analysis id columns, in the order the adapter reads
#: them: this file spells the id both ways.
DECLARED_SOURCE_ANALYSIS_ID = "source_analysis_id"
DECLARED_ANALYSIS_ID = "analysis_id"


class AdapterError(InventoryError):
    """The declared selection and its provider JSONs cannot be converted into a valid table."""


def read_declared_selection(path: Path) -> list[dict[str, str]]:
    """The declared selection's rows, refusing a missing column and a duplicate Analysis."""
    path = Path(path)
    if not path.is_file():
        raise AdapterError(f"ukb-b declared selection not found: {path}")
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        missing = [
            column for column in ADAPTER_INPUT_COLUMNS if column not in (reader.fieldnames or [])
        ]
        if missing:
            raise AdapterError(
                f"ukb-b declared selection {path} is missing column(s): {', '.join(missing)}"
            )
        rows = [dict(row) for row in reader]
    if not rows:
        raise AdapterError(f"ukb-b declared selection {path} has no rows")
    ids = [declared_analysis_id(row) for row in rows]
    problems: list[str] = []
    if any(not analysis_id for analysis_id in ids):
        problems.append("a declared row names no analysis_id")
    repeated = sorted({analysis_id for analysis_id in ids if ids.count(analysis_id) > 1})
    if repeated:
        problems.append(f"duplicate declared analysis_id: {listed(repeated)}")
    if problems:
        raise_problems(AdapterError, f"ukb-b declared selection {path}", problems)
    return rows


def declared_analysis_id(row: Mapping[str, str]) -> str:
    """A declared row's Analysis id: ``source_analysis_id``, else ``analysis_id``."""
    return (row.get(DECLARED_SOURCE_ANALYSIS_ID) or row.get(DECLARED_ANALYSIS_ID) or "").strip()


def convert(
    declared_selection: Path,
    source_dir: Path,
    out: Path,
    *,
    repo_root: Path = REPO_ROOT,
) -> None:
    """Write the canonical Source Metadata Table at ``out``, then load it back.

    Loading the written table through the canonical loader is the gate: a table
    it refuses is removed, so the adapter never leaves one behind for the shared
    freeze.
    """
    translation = load_source_label_translation(repo_root)
    declared_selection, source_dir, out = Path(declared_selection), Path(source_dir), Path(out)
    declared = read_declared_selection(declared_selection)
    if not source_dir.is_dir():
        raise AdapterError(f"ukb-b source directory not found: {source_dir}")
    declared_by_id = {declared_analysis_id(row): row for row in declared}
    records, problems = read_provider_records(sorted(declared_by_id), source_dir)
    if problems:
        raise_problems(
            AdapterError, f"ukb-b provider JSON sidecars in {source_dir}", problems
        )
    unresolved = sorted(
        analysis_id
        for analysis_id, record in records.items()
        if record.resolution_status != "resolved"
    )
    if unresolved:
        raise_problems(
            AdapterError,
            f"ukb-b provider JSON sidecars in {source_dir}",
            [
                f"{analysis_id}: provider JSON resolves no study design or sample size"
                for analysis_id in unresolved
            ],
        )
    rows = convert_rows(declared_by_id, records, translation)
    write_connector(out, SOURCE_METADATA_TABLE, OUTPUT_COLUMNS, rows)
    try:
        load_source_metadata_table(out)
    except InventoryError:
        out.unlink()
        raise


def convert_rows(
    declared_by_id: Mapping[str, Mapping[str, str]],
    records: Mapping[str, object],
    translation: SourceLabelTranslation,
) -> list[dict[str, str]]:
    """The canonical rows for ``declared_by_id``'s Analyses, in declared order."""
    resolved = {
        row["analysis_id"]: row
        for row in build_resolution_rows(
            records,  # type: ignore[arg-type]
            inclusion_reason="all ukb-b traits in the OpenGWAS IGD collection (data/ukb-b/manifest.tsv)",
        )
    }
    rows: list[dict[str, str]] = []
    problems: list[str] = []
    for analysis_id, declared in declared_by_id.items():
        resolution = resolved.get(analysis_id)
        if resolution is None:  # pragma: no cover - read_provider_records covers every id
            problems.append(f"{analysis_id}: no provider record")
            continue
        declared_label = (declared.get("source_label") or "").strip()
        provider_label = resolution["source_label"].strip()
        if declared_label and declared_label != provider_label:
            problems.append(
                f"{analysis_id}: declared Source Trait Label {declared_label!r} is not the "
                f"provider's {provider_label!r}"
            )
            continue
        ancestry_label = resolution["source_ancestry_label"].strip()
        try:
            superpopulation = translation.superpopulation(ancestry_label)
        except UnknownSourceAncestryLabel as exc:
            problems.append(f"{analysis_id}: {exc}")
            continue
        if not superpopulation:
            problems.append(
                f"{analysis_id}: provider population {ancestry_label or '(empty)'!r} translates "
                "to no super-population"
            )
            continue
        study_design = (
            "case-control" if resolution["sample_size_kind"] == "case_control" else "quantitative"
        )
        rows.append(
            {
                "source_analysis_id": analysis_id,
                "source_trait_label": provider_label,
                "source_trait_id": analysis_id,
                "source_ancestry_label": ancestry_label,
                "source_superpopulation": superpopulation,
                "study_design": study_design,
                "sample_size": resolution["sample_size"],
                "n_cases": resolution["n_cases"] if study_design == "case-control" else "",
                "n_controls": resolution["n_controls"] if study_design == "case-control" else "",
            }
        )
    if problems:
        raise_problems(AdapterError, "ukb-b provider JSON sidecars", problems)
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--declared-selection",
        required=True,
        type=Path,
        help="the registry's declared ukb-b selection (2,514 rows)",
    )
    parser.add_argument(
        "--source-dir", required=True, type=Path, help="the directory holding the provider JSON sidecars"
    )
    parser.add_argument("--out", required=True, type=Path, help="the Source Metadata Table to write")
    parser.add_argument(
        "--repo-root",
        default=str(REPO_ROOT),
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    try:
        convert(
            args.declared_selection,
            args.source_dir,
            args.out,
            repo_root=Path(args.repo_root),
        )
    except InventoryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        f"Source Metadata Table {args.out} written from {args.declared_selection} and the "
        f"provider JSONs in {args.source_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
