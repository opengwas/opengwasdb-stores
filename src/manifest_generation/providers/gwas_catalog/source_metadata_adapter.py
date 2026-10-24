#!/usr/bin/env python3
"""GWAS Catalog metadata adapter: candidate table -> Source Metadata Table (#283, ADR 0036).

``ebi-studies.r``, beside this module, curates the GWAS Catalog studies into a
candidate table (``STUDY.ACCESSION``, ``DISEASE.TRAIT``, ``MAPPED_TRAIT_URI``,
``store_key``, ...), one row per full-summary-statistics Analysis. This adapter
converts that table, as written, into the canonical Source Metadata Table the
shared freeze reads (``manifest_generation.freeze``). It is shared by every
GWAS Catalog collection, so Chen 2023 and Sun 2018 style releases differ only
in their Selection. The R script is not changed.

The conversion is a column mapping. Values are carried as written, with these
documented exceptions (``docs/spec/source-connectors.md``):

- **Missing values.** ``ebi-studies.r`` writes with ``data.table::fwrite``'s
  default ``na = ""``: an R ``NA`` is an empty field, and an empty string is
  ``""``. Both read as empty, which is the only missing value. A literal
  ``NA``/``nan`` is not a missing token here; it is carried and the canonical
  loader refuses it where a number is due.
- **Case counts for a quantitative study are absent, not zero.** The candidate
  table writes ``0``/``0`` for a resolved quantitative study; a zero count is a
  real count, so the adapter writes blank (#133). A non-zero count on a
  quantitative study, or any count on an Analysis with no study design,
  contradicts itself and is refused.
- **Padding is trimmed** exactly as the candidate-table freeze trimmed it
  (``str.strip``, which also removes the non-breaking spaces GWAS Catalog
  leaves on a few trait labels), so the shared freeze reproduces its inventory.
- **The source's super-population** is the tracked translation of
  ``ancestry_group`` (``source_ancestry.load_source_label_translation``): a
  mapped label gives its code, a label listed as deliberately imprecise
  (``Multiple/Mixed``, ``NR/Unknown``, ...) gives blank, and any other label is
  refused by name. The label itself is kept verbatim as provenance.

The per-Analysis metadata sidecar (a GWAS-SSF ``-meta.yaml``) is read here and
only here (#347). With ``--acquisition-manifest``, each Analysis acquisition
marked ready, with a sidecar recorded, contributes two facts:

- ``genotyping_technology``: the sidecar's list, ``;``-joined in source order,
  blank when the sidecar records none. Which technologies are exempt from the
  MAF floor is release policy, not this column's.
- ``source_genome_build``: the sidecar's ``genome_assembly``, blank when it
  declares none.

A recorded sidecar that is missing, unparseable, not a mapping or malformed in
these fields is refused by name, never read as "no technology".

The EBI GWAS Catalog terms of use are not per-Analysis source metadata in the
candidate table, so the adapter does not write them: the release config's
defaults supply them (#287). Mapped ontology terms are carried verbatim, URIs
and comma-separated lists included: normalising them is Trait Ontology
Mapping's job (#286).

Usage:
    python3 -m manifest_generation.providers.gwas_catalog.source_metadata_adapter \\
        --candidates resources/data/derived/store-candidates-analyses.tsv \\
        --acquisition-manifest <manifest.tsv> --out <table.tsv>
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Mapping, Sequence

import yaml

from manifest_generation import REPO_ROOT
from manifest_generation.acquisition_manifest import AcquisitionManifest, load_acquisition_manifest
from manifest_generation.connector_tsv import raise_problems, write_connector
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

#: The candidate-table columns the adapter reads; any other column is ignored.
ADAPTER_INPUT_COLUMNS: tuple[str, ...] = (
    "STUDY.ACCESSION",
    "PUBMED.ID",
    "FIRST.AUTHOR",
    "DISEASE.TRAIT",
    "MAPPED_TRAIT",
    "MAPPED_TRAIT_URI",
    "ancestry_group",
    "store_key",
    "study_design",
    "n_cases",
    "n_controls",
    "sample_size",
)

#: The canonical columns the adapter writes.
OUTPUT_COLUMNS: tuple[str, ...] = (
    "source_analysis_id",
    "source_trait_label",
    "source_trait_ontology_id",
    "source_trait_ontology_label",
    "source_ancestry_label",
    "source_superpopulation",
    "study_design",
    "sample_size",
    "n_cases",
    "n_controls",
    "source_genome_build",
    "genotyping_technology",
    "publication_pmid",
    "first_author",
    "store_key",
)


class AdapterError(InventoryError):
    """The candidate table cannot be converted into a valid Source Metadata Table."""


def read_sidecar_facts(sidecar: Path) -> tuple[str, str]:
    """A GWAS-SSF sidecar's ``(genotyping_technology, source_genome_build)``, blank where absent.

    The technologies are ``;``-joined in the sidecar's order. A sidecar that
    cannot be read, or whose fields are not what GWAS-SSF writes, raises
    :class:`AdapterError`: an unreadable sidecar must not look like an Analysis
    with no technology, which the MAF exemption would read as "not exempt".
    """
    try:
        document = yaml.safe_load(sidecar.read_text(encoding="utf-8", errors="replace"))
    except (OSError, yaml.YAMLError) as exc:
        raise AdapterError(f"metadata sidecar {sidecar} is unreadable ({exc.__class__.__name__})") from exc
    if not isinstance(document, dict):
        raise AdapterError(f"metadata sidecar {sidecar} is not a YAML mapping")
    raw = document.get("genotyping_technology")
    if raw is None:
        values: list[object] = []
    elif isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, list):
        values = raw
    else:
        raise AdapterError(f"metadata sidecar {sidecar}: genotyping_technology is neither a string nor a list")
    technologies: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise AdapterError(f"metadata sidecar {sidecar}: genotyping_technology entry {value!r} is not text")
        if ";" in value:
            raise AdapterError(f"metadata sidecar {sidecar}: genotyping_technology entry {value!r} contains ';'")
        if value.strip():
            technologies.append(value.strip())
    assembly = document.get("genome_assembly")
    if assembly is not None and not isinstance(assembly, str):
        raise AdapterError(f"metadata sidecar {sidecar}: genome_assembly {assembly!r} is not text")
    return ";".join(technologies), (assembly or "").strip()


def _sidecar_facts(
    manifest: AcquisitionManifest | None, analysis_id: str, problems: list[str]
) -> tuple[str, str]:
    if manifest is None or analysis_id not in manifest:
        return "", ""
    entry = manifest.entry(analysis_id)
    if not entry.ready or not entry.sidecar_location:
        return "", ""
    try:
        return read_sidecar_facts(Path(entry.sidecar_location))
    except AdapterError as exc:
        problems.append(f"{analysis_id}: {exc}")
        return "", ""


def convert_candidate_table(
    candidates: Path,
    out: Path,
    *,
    acquisition_manifest: Path | None = None,
    repo_root: Path = REPO_ROOT,
) -> None:
    """Convert ``candidates`` into a canonical Source Metadata Table at ``out``, then load it back.

    ``acquisition_manifest`` locates each ready Analysis's metadata sidecar,
    which gives ``genotyping_technology`` and ``source_genome_build``.

    Loading the written table back through the canonical loader is the gate:
    a table it refuses is removed, so the adapter never leaves one behind for
    the shared freeze.
    """
    translation = load_source_label_translation(repo_root)
    candidates, out = Path(candidates), Path(out)
    if not candidates.is_file():
        raise AdapterError(f"GWAS Catalog candidate table not found: {candidates}")
    manifest = load_acquisition_manifest(Path(acquisition_manifest)) if acquisition_manifest else None
    rows: list[dict[str, str]] = []
    problems: list[str] = []
    with candidates.open(newline="", encoding="utf-8") as fh:
        # fwrite quotes a field holding a quote or separator and writes an empty
        # string as "": read it with CSV quoting, as the candidate-table freeze did.
        reader = csv.DictReader(fh, delimiter="\t")
        missing = [column for column in ADAPTER_INPUT_COLUMNS if column not in (reader.fieldnames or [])]
        if missing:
            raise AdapterError(f"candidate table {candidates} is missing column(s): {', '.join(missing)}")
        for line_number, raw in enumerate(reader, start=2):
            row, row_problems = _convert_row(raw, translation)
            problems.extend(f"line {line_number} ({row['source_analysis_id']}): {p}" for p in row_problems)
            row["genotyping_technology"], row["source_genome_build"] = _sidecar_facts(
                manifest, row["source_analysis_id"], problems
            )
            rows.append(row)
    if problems:
        raise_problems(AdapterError, f"candidate table {candidates}", problems)
    write_connector(out, SOURCE_METADATA_TABLE, OUTPUT_COLUMNS, rows)
    try:
        load_source_metadata_table(out)
    except InventoryError:
        out.unlink()
        raise


def _convert_row(raw: Mapping[str, str | None], translation: SourceLabelTranslation) -> tuple[dict[str, str], list[str]]:
    def text(column: str) -> str:
        return (raw.get(column) or "").strip()

    problems: list[str] = []
    study_design = text("study_design")
    n_cases, n_controls = text("n_cases"), text("n_controls")
    if study_design == "quantitative":
        nonzero = [
            f"{column}={value}"
            for column, value in (("n_cases", n_cases), ("n_controls", n_controls))
            if value and value != "0"
        ]
        if nonzero:
            problems.append(f"a quantitative study with case counts {', '.join(nonzero)}")
        else:
            # ebi-studies.r's documented 0/0 for a resolved quantitative study.
            n_cases = n_controls = ""
    elif not study_design and (n_cases or n_controls):
        problems.append(f"case counts n_cases={n_cases or 'empty'}, n_controls={n_controls or 'empty'} "
                        "with no study_design")
    ancestry_label = text("ancestry_group")
    try:
        superpopulation = translation.superpopulation(ancestry_label)
    except UnknownSourceAncestryLabel as exc:
        problems.append(str(exc))
        superpopulation = ""
    row = {
        "source_analysis_id": text("STUDY.ACCESSION"),
        "source_trait_label": text("DISEASE.TRAIT"),
        "source_trait_ontology_id": text("MAPPED_TRAIT_URI"),
        "source_trait_ontology_label": text("MAPPED_TRAIT"),
        "source_ancestry_label": ancestry_label,
        "source_superpopulation": superpopulation,
        "study_design": study_design,
        "sample_size": text("sample_size"),
        "n_cases": n_cases,
        "n_controls": n_controls,
        "source_genome_build": "",
        "genotyping_technology": "",
        "publication_pmid": text("PUBMED.ID"),
        "first_author": text("FIRST.AUTHOR"),
        "store_key": text("store_key"),
    }
    return row, problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--candidates", required=True, type=Path, help="ebi-studies.r's store-candidates-analyses.tsv")
    parser.add_argument("--acquisition-manifest", type=Path,
                        help="the Acquisition Manifest locating each ready Analysis's metadata sidecar")
    parser.add_argument("--out", required=True, type=Path, help="the Source Metadata Table to write")
    args = parser.parse_args(argv)
    try:
        convert_candidate_table(args.candidates, args.out, acquisition_manifest=args.acquisition_manifest)
    except InventoryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Source Metadata Table {args.out} written from {args.candidates}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
