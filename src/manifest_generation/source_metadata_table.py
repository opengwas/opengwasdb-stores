"""The Source Metadata Table: canonical per-Analysis source metadata (issue #283, ADR 0036).

A Source Collection's metadata adapter writes one row per intended Analysis in
one canonical set of columns, whatever the Source Provider. Its rows are the
collection's intended membership: freeze reconciles a Selection of them against
the Acquisition Manifest (``manifest_generation.freeze``) and freezes the
selected rows beside the Source Inventory, so generation never reads a live
source and a metadata change is a reviewed re-freeze.

Loading is a gate. An unknown column, a missing required value, a duplicate
Analysis or a value outside the contract's vocabulary is refused, every problem
named at once. The vocabularies are the contract's own:
``ogstores.bundle.SUPERPOPULATIONS`` for super-population codes and
OpenGWASDB's ``StoredEffectScale`` (which ``bundle.check()`` enforces on
``stored_effect_scale``) for a declared effect scale. They are imported, never
copied, so the table cannot drift from what a Release Bundle accepts.

Write a table with ``connector_tsv.write_connector(path, SOURCE_METADATA_TABLE,
columns, rows)``. ``docs/spec/source-connectors.md`` specifies every column.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from manifest_generation.connector_tsv import (
    ConnectorSpec,
    FreezeError,
    raise_problems,
    read_connector,
)
from manifest_generation.source_inventory import duplicate_values, sha256_file
from ogstores.bundle import SUPERPOPULATIONS

__all__ = [
    "COLUMNS",
    "REQUIRED_COLUMNS",
    "SOURCE_METADATA_TABLE",
    "SourceMetadataTable",
    "SourceMetadataTableError",
    "load_source_metadata_table",
]

#: Columns every table carries, with a value on every row.
REQUIRED_COLUMNS: tuple[str, ...] = ("source_analysis_id",)

#: Columns a table may carry. Absent or blank means the source supplied no
#: value -- never zero, never a default.
OPTIONAL_COLUMNS: tuple[str, ...] = (
    "source_trait_label",
    "source_trait_id",
    "source_trait_ontology_id",
    "source_trait_ontology_label",
    "source_ancestry_label",
    "source_superpopulation",
    "study_design",
    "declared_effect_scale",
    "sample_size",
    "n_cases",
    "n_controls",
    "source_genome_build",
    "publication_pmid",
    "publication_doi",
    "first_author",
    "consortium",
    "license",
    "store_key",
    # The Store's own Analysis identity, for a Source Collection whose ids differ
    # from its source's (#288): ``analysis_id`` names the Analysis in the bundle
    # (`finngen-r13-<phenocode>`, namespaced because a bare phenocode is not
    # unique across sources), and ``analysis_group_id`` groups it. Absent means
    # the pipeline's defaults: ``source_analysis_id``, and the release config's
    # collection-wide ``analysis_group_id``. Resolution stays keyed by
    # ``source_analysis_id``; these two are what the bundle reports.
    "analysis_id",
    "analysis_group_id",
    # Target annotation for an Analysis of one molecular target (Sun 2018's
    # SomaScan proteins): the assay, the gene it measures and that gene's span.
    "target_assay_id",
    "target_symbol",
    "target_ensembl_gene_id",
    "target_chromosome",
    "target_start",
    "target_end",
    "tissue",
    "context",
)

#: Every canonical column, in the order a writer emits them.
COLUMNS: tuple[str, ...] = REQUIRED_COLUMNS + OPTIONAL_COLUMNS

#: ``study_design`` values. The release config keys its method tiers by them
#: (``defaults.by_study_design``) and the Source Inventory carries them.
STUDY_DESIGNS: tuple[str, ...] = ("quantitative", "case-control")

#: ``source_genome_build`` values: the spellings every Release Bundle's
#: ``source_genome_build`` uses. ``hg19``/``hg38`` are OpenGWASDB flag values,
#: not this column's.
GENOME_BUILDS: tuple[str, ...] = ("GRCh37", "GRCh38")

#: ``target_chromosome`` values, spelled without a ``chr`` prefix.
TARGET_CHROMOSOMES: tuple[str, ...] = tuple(str(n) for n in range(1, 23)) + ("X", "Y", "MT")

_NUMBER = re.compile(r"\A\d+(?:\.\d+)?\Z")
_POSITIVE_INTEGER = re.compile(r"\A[1-9]\d*\Z")
_ENSEMBL_GENE = re.compile(r"\AENSG\d{11}(?:\.\d+)?\Z")


class SourceMetadataTableError(FreezeError):
    """A Source Metadata Table is not usable as the intended membership."""


#: The file contract the shared connector reader and writer apply.
SOURCE_METADATA_TABLE = ConnectorSpec(
    what="Source Metadata Table",
    columns=COLUMNS,
    required_columns=REQUIRED_COLUMNS,
    error=SourceMetadataTableError,
)


@dataclass(frozen=True)
class SourceMetadataTable:
    """A loaded, validated Source Metadata Table and the identity freeze records."""

    path: Path
    sha256: str
    bytes: int
    columns: tuple[str, ...]
    rows: tuple[Mapping[str, str], ...]
    _by_id: Mapping[str, Mapping[str, str]] = field(repr=False, compare=False, default_factory=dict)

    @property
    def analysis_ids(self) -> tuple[str, ...]:
        return tuple(row["source_analysis_id"] for row in self.rows)

    def row(self, analysis_id: str) -> Mapping[str, str]:
        return self._by_id[analysis_id]

    def __contains__(self, analysis_id: object) -> bool:
        return analysis_id in self._by_id


def load_source_metadata_table(path: Path) -> SourceMetadataTable:
    """Load and validate a Source Metadata Table, refusing it on any problem."""
    path = Path(path)
    header, numbered = read_connector(path, SOURCE_METADATA_TABLE)
    problems: list[str] = []
    for line_number, row in numbered:
        problems.extend(f"line {line_number}: {problem}" for problem in _row_problems(row))
    repeated = duplicate_values(row["source_analysis_id"] for _, row in numbered if row["source_analysis_id"])
    if repeated:
        problems.append(f"duplicate source_analysis_id: {', '.join(repeated)}")
    # The Store's Analysis ids must be unique when a table declares them: two
    # rows sharing one is a bundle ``bundle.check()`` refuses, so refuse the
    # table it would be rendered from (#288).
    repeated_store_ids = duplicate_values(
        row["analysis_id"] for _, row in numbered if row.get("analysis_id")
    )
    if repeated_store_ids:
        problems.append(f"duplicate analysis_id: {', '.join(repeated_store_ids)}")
    if problems:
        raise_problems(SourceMetadataTableError, f"Source Metadata Table {path}", problems)
    rows = tuple(row for _, row in numbered)
    return SourceMetadataTable(
        path=path,
        sha256=sha256_file(path),
        bytes=path.stat().st_size,
        columns=header,
        rows=rows,
        _by_id={row["source_analysis_id"]: row for row in rows},
    )


# ---------------------------------------------------------------------------
# Per-row rules
# ---------------------------------------------------------------------------


def _row_problems(row: Mapping[str, str]) -> list[str]:
    problems: list[str] = []

    def value(column: str) -> str:
        return row.get(column, "")

    for column in REQUIRED_COLUMNS:
        if not value(column):
            problems.append(f"{column} is empty")

    def vocabulary(column: str, allowed: Sequence[str]) -> None:
        if value(column) and value(column) not in allowed:
            problems.append(f"{column} {value(column)!r} is not one of {', '.join(allowed)}")

    vocabulary("source_superpopulation", SUPERPOPULATIONS)
    vocabulary("study_design", STUDY_DESIGNS)
    vocabulary("source_genome_build", GENOME_BUILDS)
    vocabulary("target_chromosome", TARGET_CHROMOSOMES)
    if value("declared_effect_scale"):
        vocabulary("declared_effect_scale", _stored_effect_scales())

    for column in ("sample_size", "n_cases", "n_controls"):
        if value(column) and not _NUMBER.match(value(column)):
            problems.append(f"{column} {value(column)!r} is not a non-negative number")
    if value("publication_pmid") and not _POSITIVE_INTEGER.match(value("publication_pmid")):
        problems.append(f"publication_pmid {value('publication_pmid')!r} is not a PMID")

    # Zero is a real count: a case count where the counts do not apply is the
    # absence-vs-zero confusion bundle.check() refuses in analyses.tsv (#133).
    if value("study_design") != "case-control":
        for column in ("n_cases", "n_controls"):
            if value(column):
                problems.append(
                    f"{column} is set but study_design is {value('study_design') or 'empty'!r}, "
                    "not 'case-control'"
                )

    if value("source_trait_ontology_label") and not value("source_trait_ontology_id"):
        problems.append("source_trait_ontology_label is set without source_trait_ontology_id")

    if value("target_ensembl_gene_id") and not _ENSEMBL_GENE.match(value("target_ensembl_gene_id")):
        problems.append(
            f"target_ensembl_gene_id {value('target_ensembl_gene_id')!r} is not an Ensembl gene id"
        )
    span = ("target_chromosome", "target_start", "target_end")
    if any(value(column) for column in span):
        absent = [column for column in span if not value(column)]
        if absent:
            problems.append(f"a target span needs {', '.join(span)}; {', '.join(absent)} empty")
        for column in ("target_start", "target_end"):
            if value(column) and not _POSITIVE_INTEGER.match(value(column)):
                problems.append(f"{column} {value(column)!r} is not a positive integer")
        if (
            _POSITIVE_INTEGER.match(value("target_start"))
            and _POSITIVE_INTEGER.match(value("target_end"))
            and int(value("target_start")) > int(value("target_end"))
        ):
            problems.append("target_start is after target_end")
    return problems


def _stored_effect_scales() -> tuple[str, ...]:
    # OpenGWASDB's model is imported here, not at module load: the inventory
    # commands run in environments that only need it when a table declares a
    # scale (see the note in ogstores.bundle).
    from opengwasdb.model.enums import StoredEffectScale

    return tuple(member.value for member in StoredEffectScale)
