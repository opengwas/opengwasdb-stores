#!/usr/bin/env python3
"""dbGaP MVP metadata adapter: acquisition mapping + deposit metadata -> Source Metadata Table (#290).

MVP ``phs002453`` (PMID 39024449, VA Million Veteran Program) is one Source
Collection under the dbGaP Source Provider (ADR 0036). The acquisition binds
each dbGaP member to one GWAS-SSF Analysis in ``mapping.tsv``; this adapter
joins that binding with the deposit's own ``*.dbGaP.metadata.txt`` and writes
the canonical Source Metadata Table the shared freeze reads.

What it translates, and only this:

- **The authoritative metadata is the deposit's.** The analyzed variable, the
  trait type and the sample size are read from the metadata file. Where the
  acquisition mapping disagrees, the adapter refuses rather than choosing: a
  mapping that no longer describes the deposit is a re-map, not a silent edit.
- **The ancestry label is the deposit's bucket**, spelled as the provider's
  public label (``European``, ``African``, ``Hispanic or Latin American``,
  ``East Asian``) and ``META`` for the cross-ancestry bucket. The tracked
  ``source_label_map.tsv`` translates the four precise labels to their
  super-population codes; ``META`` is listed in ``source_label_imprecise.tsv``
  and therefore has no single super-population (a blank cell, never a guess).
- **The store key is the post-split one.** META members still share the
  legacy European candidate key upstream; the adapter writes the precise
  provider-side grouping (``dense__pmid-39024449__Multi-ancestry`` for META),
  so five Selections can name one canonical ``store_key`` each. This applies
  the split the retired partition made, without a new column or rule form.
- **Case counts apply only to case-control Analyses.** A quantitative study's
  mapping writes ``0``/``0``; a zero count is not an absence, so the adapter
  writes blank (#133).
- **Collection-wide facts are the release config's.** The GRCh38 build, the
  licence and the DOI are not per-Analysis facts in the deposit, so they are
  not written; the release config's ``defaults`` supply them.

Usage:
    python3 -m manifest_generation.providers.dbgap.mvp.source_metadata_adapter \\
        --mapping mapping.tsv --extracted GIA/extracted --out source-metadata-table.tsv
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Mapping, Sequence

from manifest_generation import REPO_ROOT
from manifest_generation.connector_tsv import raise_problems, write_connector
from manifest_generation.providers.dbgap.metadata import (
    METADATA_ANALYZED_VARIABLE,
    METADATA_SAMPLE_SIZE,
    METADATA_TRAIT_TYPE,
    DbgapError,
    metadata_path_for,
    parse_metadata,
    parse_sample_size,
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

#: The publication the Source Collection is.
PUBMED_ID: str = "39024449"

#: The deposit's ancestry buckets, as they appear in a member name.
ANCESTRIES: tuple[str, ...] = ("AFR", "AMR", "EAS", "EUR", "META")

#: The provider's public Source Ancestry Label for each bucket. ``META`` is the
#: deliberately imprecise cross-ancestry label (#290).
SOURCE_ANCESTRY_LABEL: Mapping[str, str] = {
    "EUR": "European",
    "AFR": "African",
    "AMR": "Hispanic or Latin American",
    "EAS": "East Asian",
    "META": "META",
}

#: The precise post-split Store grouping per bucket. The legacy candidate key
#: folded META into the European one; this is the partition's split made
#: explicit on a canonical column so a Selection can name one bucket.
STORE_KEY_BY_ANCESTRY: Mapping[str, str] = {
    "EUR": "dense__pmid-39024449__European",
    "AFR": "dense__pmid-39024449__African",
    "AMR": "dense__pmid-39024449__Hispanic-or-Latin-American",
    "EAS": "dense__pmid-39024449__East-Asian",
    "META": "dense__pmid-39024449__Multi-ancestry",
}

#: The mapping columns the adapter reads; any other column is ignored.
MAPPING_COLUMNS: tuple[str, ...] = (
    "analysis_id",
    "member",
    "ancestry",
    "study_design",
    "sample_size",
    "n_cases",
    "n_controls",
    "dbgap_trait_code",
    "analyzed_variable",
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
    "publication_pmid",
    "store_key",
)


class AdapterError(InventoryError):
    """The mapping and the deposit's metadata cannot be read as one Source Metadata Table."""


def ancestry_of_member(member: str) -> str:
    """The ancestry bucket from ``MVP_R4.1000G_AGR.<TRAIT>.<ANCESTRY>.GIA.dbGaP.txt.gz``."""
    name = Path(member).name
    if not name.endswith(".dbGaP.txt.gz"):
        raise AdapterError(f"dbGaP member {member!r} does not end in .dbGaP.txt.gz")
    stem = name[: -len(".dbGaP.txt.gz")]
    parts = stem.split(".")
    if len(parts) != 5 or parts[0] != "MVP_R4" or parts[4] != "GIA":
        raise AdapterError(f"dbGaP member {member!r} is not MVP_R4.1000G_AGR.<TRAIT>.<ANCESTRY>.GIA.dbGaP")
    ancestry = parts[3]
    if ancestry not in ANCESTRIES:
        raise AdapterError(
            f"dbGaP member {member!r} names ancestry {ancestry!r}, not one of {', '.join(ANCESTRIES)}"
        )
    return ancestry


def read_mapping(path: Path) -> list[dict[str, str]]:
    path = Path(path)
    if not path.is_file():
        raise AdapterError(f"dbGaP mapping not found: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        missing = [column for column in MAPPING_COLUMNS if column not in (reader.fieldnames or [])]
        if missing:
            raise AdapterError(f"dbGaP mapping {path} is missing column(s): {', '.join(missing)}")
        rows = [{column: (row.get(column) or "").strip() for column in MAPPING_COLUMNS} for row in reader]
    if not rows:
        raise AdapterError(f"dbGaP mapping {path} has no rows")
    seen: set[str] = set()
    repeated: list[str] = []
    for row in rows:
        if row["analysis_id"] in seen:
            repeated.append(row["analysis_id"])
        seen.add(row["analysis_id"])
    if repeated:
        raise AdapterError(f"dbGaP mapping {path} names Analysis/Analyses twice: {', '.join(sorted(repeated))}")
    return rows


def convert_mapping(
    mapping: Path,
    extracted: Path,
    out: Path,
    *,
    repo_root: Path = REPO_ROOT,
) -> None:
    """Write the canonical Source Metadata Table, then load it back as the gate."""
    translation = load_source_label_translation(repo_root)
    mapping, extracted, out = Path(mapping), Path(extracted), Path(out)
    if not extracted.is_dir():
        raise AdapterError(f"dbGaP extracted metadata root not found: {extracted}")
    rows: list[dict[str, str]] = []
    problems: list[str] = []
    for raw in read_mapping(mapping):
        try:
            row = _convert_row(raw, extracted, translation)
        except (AdapterError, DbgapError) as exc:
            problems.append(f"{raw['analysis_id']}: {exc}")
            continue
        rows.append(row)
    if problems:
        raise_problems(AdapterError, f"dbGaP mapping {mapping}", problems)
    write_connector(out, SOURCE_METADATA_TABLE, OUTPUT_COLUMNS, rows)
    try:
        load_source_metadata_table(out)
    except InventoryError:
        out.unlink()
        raise


def _convert_row(
    raw: Mapping[str, str],
    extracted: Path,
    translation: SourceLabelTranslation,
) -> dict[str, str]:
    analysis_id = raw["analysis_id"]
    if not analysis_id:
        raise AdapterError("analysis_id is empty")
    member = raw["member"]
    ancestry = ancestry_of_member(member)
    if raw["ancestry"] and raw["ancestry"].upper() != ancestry:
        raise AdapterError(
            f"mapping ancestry {raw['ancestry']!r} disagrees with member ancestry {ancestry!r}"
        )
    metadata_path = metadata_path_for(extracted, member)
    if metadata_path is None:
        raise AdapterError(f"no unique metadata file describes {member!r}")
    info = parse_metadata(metadata_path)
    for field_name in (METADATA_ANALYZED_VARIABLE, METADATA_SAMPLE_SIZE, METADATA_TRAIT_TYPE):
        if field_name not in info:
            raise AdapterError(f"metadata {metadata_path} has no {field_name!r} row")

    analyzed_variable = info[METADATA_ANALYZED_VARIABLE]
    if raw["analyzed_variable"] and analyzed_variable != raw["analyzed_variable"]:
        raise AdapterError(
            f"analyzed variable {analyzed_variable!r} disagrees with the mapping's "
            f"{raw['analyzed_variable']!r}"
        )
    trait_type = info[METADATA_TRAIT_TYPE].strip().lower()
    if trait_type not in ("binary trait", "quantitative trait"):
        raise AdapterError(f"unrecognised phenotypic trait type {info[METADATA_TRAIT_TYPE]!r}")
    study_design = "case-control" if trait_type == "binary trait" else "quantitative"
    if raw["study_design"] and raw["study_design"] != study_design:
        raise AdapterError(
            f"study design {study_design!r} disagrees with the mapping's {raw['study_design']!r}"
        )
    sample_size, n_cases, n_controls = parse_sample_size(info[METADATA_SAMPLE_SIZE], metadata_path)
    if raw["sample_size"] and int(raw["sample_size"]) != sample_size:
        raise AdapterError(
            f"sample size {sample_size} disagrees with the mapping's {raw['sample_size']!r}"
        )
    if raw["n_cases"] and raw["n_cases"] != "0" and int(raw["n_cases"]) != n_cases:
        raise AdapterError(f"case count {n_cases} disagrees with the mapping's {raw['n_cases']!r}")
    if raw["n_controls"] and raw["n_controls"] != "0" and int(raw["n_controls"]) != n_controls:
        raise AdapterError(
            f"control count {n_controls} disagrees with the mapping's {raw['n_controls']!r}"
        )

    label = SOURCE_ANCESTRY_LABEL[ancestry]
    try:
        superpopulation = translation.superpopulation(label)
    except UnknownSourceAncestryLabel as exc:
        raise AdapterError(str(exc)) from exc

    return {
        "source_analysis_id": analysis_id,
        "source_trait_label": analyzed_variable,
        "source_trait_id": raw["dbgap_trait_code"],
        "source_ancestry_label": label,
        "source_superpopulation": superpopulation,
        "study_design": study_design,
        "sample_size": str(sample_size),
        # A count applies only to a case-control study; zero is not absence.
        "n_cases": str(n_cases) if study_design == "case-control" else "",
        "n_controls": str(n_controls) if study_design == "case-control" else "",
        "publication_pmid": PUBMED_ID,
        "store_key": STORE_KEY_BY_ANCESTRY[ancestry],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mapping", required=True, type=Path, help="the acquisition's mapping.tsv")
    parser.add_argument("--extracted", required=True, type=Path, help="the deposit's GIA/extracted root")
    parser.add_argument("--out", required=True, type=Path, help="the Source Metadata Table to write")
    args = parser.parse_args(argv)
    try:
        convert_mapping(args.mapping, args.extracted, args.out)
    except InventoryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Source Metadata Table {args.out} written from {args.mapping} and {args.extracted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
