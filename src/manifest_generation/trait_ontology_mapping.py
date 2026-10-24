"""Trait Ontology Mapping: the shared step after verification (#286, ADR 0036 step 5).

Every Analysis gets one Trait Ontology Mapping and the method that produced it:

1. ``source_provided``: the Source Collection's own term, from the Source
   Metadata Table's ``source_trait_ontology_id``/``source_trait_ontology_label``
   (:func:`source_traits_from_source_metadata_table`), normalised to CURIEs and
   otherwise kept as the source gives it;
2. ``canonical_table_lookup``: otherwise, the Source Trait Label matched
   exactly (trimmed, lower-cased) against the Canonical Trait Mapping Table, a
   tracked Reference Resource read from a directory the caller names, so the
   lookup is offline and its version and sha256 are recorded;
3. ``unmapped``: otherwise, with the id and label left empty.

The lookup lives in this registry, not OpenGWASDB (ADR 0021). This module is
the only Trait Ontology Mapping step: it replaced
``resources/generators/lib/metadata_resolvers/canonical_trait_table.R`` for the
pipeline, and #350 deleted that file once curation's round-trip proof ran
through here instead.

**Normalisation.** A source term is split by the contract's one rule
(``ogstores.bundle.trait_ontology_id_terms``) and each term becomes a CURIE:
``http://www.ebi.ac.uk/efo/EFO_x`` -> ``EFO:x``,
``http://purl.obolibrary.org/obo/NS_local`` -> ``NS:local``,
``http://www.orpha.net/ORDO/Orphanet_N`` -> ``Orphanet:N``, and a CURIE stays
as it is. Any other form is refused, naming the Analysis and the term, rather
than passed through. Several terms are joined by ``", "`` in the source's order
(the Release Bundle contract's list form); the label is kept verbatim, because
a term's own label may contain a comma and cannot be split per term.

**Gene and authority guards (#141).** A source term with a gene- or
protein-shaped identifier in any term -- before or after normalisation, so a
PURL ``HGNC_1234`` is caught as ``HGNC:1234`` -- or whose label names an
identifier authority, is rejected: it falls through to the table, and the
rejection and its reason are recorded. A table row of either kind is a curation
error and fails the load. The guards are the Release Bundle contract's own
(``ogstores.bundle``).
"""

from __future__ import annotations

import csv
import hashlib
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, NamedTuple

import yaml

from manifest_generation.connector_tsv import raise_problems
from manifest_generation.source_metadata_table import SourceMetadataTable
from ogstores.bundle import (
    TRAIT_ONTOLOGY_ID_SEPARATOR,
    is_authority_name_trait_ontology_label,
    is_canonical_trait_ontology_id,
    is_gene_shaped_trait_ontology_id,
    is_trait_ontology_curie,
    trait_ontology_id_terms,
)


class TraitOntologyMappingMethod(StrEnum):
    """The Trait Ontology Mapping Method vocabulary (``trait_ontology_mapping_method``)."""

    SOURCE_PROVIDED = "source_provided"
    CANONICAL_TABLE_LOOKUP = "canonical_table_lookup"
    UNMAPPED = "unmapped"


#: The tracked Canonical Trait Mapping Table, relative to the repository root.
CANONICAL_TRAIT_MAPPING_RESOURCE = Path(
    "resources/reference-resources/canonical-trait-mapping-efo"
)
_RESOURCE_KIND = "trait_ontology_mapping"
_LOCATION_KIND = "tracked_file"
_LOOKUP_COLUMNS = ("trait_label", "trait_ontology_id", "trait_ontology_label")

#: URI prefix -> CURIE prefix, or None when the URI's own ``NS_`` names it.
_URI_FORMS: tuple[tuple[str, str | None], ...] = (
    ("http://www.ebi.ac.uk/efo/EFO_", "EFO"),
    ("http://www.orpha.net/ORDO/Orphanet_", "Orphanet"),
    ("http://purl.obolibrary.org/obo/", None),
)
_GENE_REASON = "a gene or protein identifier is not a Trait Ontology Mapping (#141)"


class TraitOntologyMappingError(ValueError):
    """A source term or Canonical Trait Mapping Table this step must refuse."""


@dataclass(frozen=True)
class SourceTrait:
    """One Analysis's trait columns from the Source Metadata Table, verbatim."""

    analysis_id: str
    #: ``source_trait_label``, the Source Trait Label: the table lookup's key.
    label: str
    #: ``source_trait_ontology_id``: a CURIE, a URI, or a comma-separated list.
    ontology_id: str
    #: ``source_trait_ontology_label``.
    ontology_label: str


class CanonicalTraitTerm(NamedTuple):
    """One Canonical Trait Mapping Table entry's ontology term."""

    trait_ontology_id: str
    trait_ontology_label: str


@dataclass(frozen=True)
class TraitOntologyMapping:
    """One Analysis's Trait Ontology Mapping and how it was produced."""

    trait_ontology_id: str
    trait_ontology_label: str
    method: TraitOntologyMappingMethod
    #: Why a source term was rejected, or why nothing mapped; empty otherwise.
    note: str = ""
    #: The source term the #141 guards rejected, verbatim; empty otherwise.
    rejected_source_term: str = ""


@dataclass(frozen=True)
class CanonicalTraitMappingTable:
    """A loaded Canonical Trait Mapping Table and the identity it was loaded at."""

    resource_id: str
    version: int
    location: str
    sha256: str
    #: Normalised trait label -> its ontology term.
    entries: Mapping[str, CanonicalTraitTerm] = field(repr=False)

    def provenance(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "version": self.version,
            "location": self.location,
            "sha256": self.sha256,
            "rows": len(self.entries),
        }


@dataclass(frozen=True)
class TraitOntologyMappingRun:
    """Every Analysis's mapping from one run, and the table it used."""

    mappings: Mapping[str, TraitOntologyMapping]
    sources: Mapping[str, SourceTrait] = field(repr=False)
    table: CanonicalTraitMappingTable

    def provenance(self) -> dict[str, Any]:
        """What validation.yaml records under ``trait_ontology_mapping``: plain data."""
        counts = Counter(mapping.method for mapping in self.mappings.values())
        return {
            "canonical_trait_mapping_table": self.table.provenance(),
            "method_counts": {
                method.value: counts.get(method, 0) for method in TraitOntologyMappingMethod
            },
            # Kept as source_provided, but a reviewer should see how many carry
            # no label (#286).
            "source_id_without_label": sum(
                1
                for mapping in self.mappings.values()
                if mapping.method is TraitOntologyMappingMethod.SOURCE_PROVIDED
                and not mapping.trait_ontology_label
            ),
            "rejected_source_terms": [
                {
                    "analysis_id": analysis_id,
                    "source_trait_ontology_id": self.sources[analysis_id].ontology_id,
                    "source_trait_ontology_label": self.sources[analysis_id].ontology_label,
                    "reason": mapping.note,
                }
                for analysis_id, mapping in sorted(self.mappings.items())
                if mapping.rejected_source_term
            ],
        }


def _normalise_label(label: str) -> str:
    """The lookup key: trimmed and lower-cased."""
    return label.strip().lower()


def _table_path(resource_dir: Path, declaration_path: Path, declaration: Mapping[str, Any]) -> Path:
    """Resolve the declared ``location`` to a file in ``resource_dir``, or refuse.

    A ``tracked_file`` location is repository-relative
    (``resources/reference-resources/<id>/mapping.tsv``), and the resource
    directory is where it lives, so the location's directory must be the
    resource directory's own trailing path. A location naming another
    directory describes some other resource, and is refused.
    """
    location_kind = declaration.get("location_kind")
    if location_kind is not None and location_kind != _LOCATION_KIND:
        raise TraitOntologyMappingError(
            f"{declaration_path} declares location_kind {location_kind!r}; a Canonical "
            f"Trait Mapping Table is a {_LOCATION_KIND!r}"
        )
    location = declaration.get("location")
    if not isinstance(location, str) or not location:
        raise TraitOntologyMappingError(f"{declaration_path} declares no location")
    declared = PurePosixPath(location)
    directory_parts = declared.parent.parts
    if (
        declared.is_absolute()
        or not directory_parts
        or resource_dir.resolve().parts[-len(directory_parts):] != directory_parts
    ):
        raise TraitOntologyMappingError(
            f"{declaration_path} declares location {location!r}, which is not a file in "
            f"its own resource directory {resource_dir}"
        )
    return resource_dir / declared.name


def load_canonical_trait_mapping_table(resource_dir: Path) -> CanonicalTraitMappingTable:
    """Load the Canonical Trait Mapping Table in ``resource_dir``.

    ``resource.yaml`` gives the identity, the version and the table's location.
    Only the three lookup columns are read; the provenance columns are for
    reviewers. A blank lookup value, a non-CURIE term, a gene-shaped term, an
    authority-name label or a label listed twice is refused: the table is
    curated, so a bad row is an error to fix rather than a row to skip.
    """
    resource_dir = Path(resource_dir)
    declaration_path = resource_dir / "resource.yaml"
    try:
        declaration = yaml.safe_load(declaration_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise TraitOntologyMappingError(
            f"cannot read Canonical Trait Mapping Table declaration {declaration_path}: {exc}"
        ) from exc
    if not isinstance(declaration, Mapping):
        raise TraitOntologyMappingError(f"{declaration_path} is not a mapping")
    if declaration.get("kind") != _RESOURCE_KIND:
        raise TraitOntologyMappingError(
            f"{declaration_path} declares kind {declaration.get('kind')!r}, "
            f"not {_RESOURCE_KIND!r}"
        )
    resource_id = declaration.get("resource_id")
    version = declaration.get("version")
    if not isinstance(resource_id, str) or not resource_id:
        raise TraitOntologyMappingError(f"{declaration_path} declares no resource_id")
    if not isinstance(version, int) or isinstance(version, bool):
        raise TraitOntologyMappingError(
            f"{declaration_path} declares no integer version: {version!r}"
        )
    table_path = _table_path(resource_dir, declaration_path, declaration)

    try:
        data = table_path.read_bytes()
    except OSError as exc:
        raise TraitOntologyMappingError(
            f"cannot read Canonical Trait Mapping Table {table_path}: {exc}"
        ) from exc
    reader = csv.DictReader(
        data.decode("utf-8").splitlines(), delimiter="\t", quoting=csv.QUOTE_NONE
    )
    missing = [column for column in _LOOKUP_COLUMNS if column not in (reader.fieldnames or [])]
    if missing:
        raise TraitOntologyMappingError(f"{table_path} is missing lookup column(s) {missing}")

    entries: dict[str, CanonicalTraitTerm] = {}
    problems: list[str] = []
    for line, row in enumerate(reader, start=2):
        label, curie, term_label = (row.get(column) or "" for column in _LOOKUP_COLUMNS)
        where = f"line {line}"
        if not (label.strip() and curie and term_label.strip()):
            problems.append(f"{where}: a lookup value is blank")
        elif is_gene_shaped_trait_ontology_id(curie):
            problems.append(f"{where}: gene-shaped trait_ontology_id {curie!r} (#141)")
        elif is_authority_name_trait_ontology_label(term_label):
            problems.append(f"{where}: authority-name trait_ontology_label {term_label!r} (#141)")
        elif not is_canonical_trait_ontology_id(curie):
            problems.append(f"{where}: trait_ontology_id {curie!r} is not a CURIE")
        elif _normalise_label(label) in entries:
            problems.append(f"{where}: trait_label {label!r} is listed twice")
        else:
            entries[_normalise_label(label)] = CanonicalTraitTerm(curie, term_label)
    if problems:
        raise_problems(
            TraitOntologyMappingError, f"Canonical Trait Mapping Table {table_path}", problems
        )
    return CanonicalTraitMappingTable(
        resource_id=resource_id,
        version=version,
        location=declaration["location"],
        sha256=hashlib.sha256(data).hexdigest(),
        entries=entries,
    )


def source_traits_from_source_metadata_table(
    table: SourceMetadataTable, analysis_ids: Iterable[str]
) -> list[SourceTrait]:
    """The step's input for ``analysis_ids``, from the Source Metadata Table (#283).

    A column the table does not carry is blank: the source supplied no value.
    An Analysis the table does not intend is refused.
    """
    wanted = sorted(set(analysis_ids))
    absent = [analysis_id for analysis_id in wanted if analysis_id not in table]
    if absent:
        raise TraitOntologyMappingError(
            f"the Source Metadata Table {table.path} has no row for {len(absent)} "
            f"Analysis/Analyses: {', '.join(absent[:20])}"
        )
    sources: list[SourceTrait] = []
    for analysis_id in wanted:
        row = table.row(analysis_id)
        sources.append(
            SourceTrait(
                analysis_id=analysis_id,
                label=row.get("source_trait_label", ""),
                ontology_id=row.get("source_trait_ontology_id", ""),
                ontology_label=row.get("source_trait_ontology_label", ""),
            )
        )
    return sources


def _curie(term: str) -> str | None:
    """One source term as a CURIE, or None for a form this step does not know."""
    for prefix, namespace in _URI_FORMS:
        if term.startswith(prefix):
            tail = term[len(prefix):]
            if namespace is None:
                namespace, _, tail = tail.partition("_")
            candidate = f"{namespace}:{tail}"
            return candidate if is_trait_ontology_curie(candidate) else None
    if "://" not in term and is_trait_ontology_curie(term):
        return term
    return None


def normalise_trait_ontology_id(value: str, *, analysis_id: str) -> str:
    """``value``'s terms as CURIEs joined by ``", "``, in order; refuse any other form."""
    curies: list[str] = []
    for term in trait_ontology_id_terms(value):
        curie = _curie(term)
        if curie is None:
            raise TraitOntologyMappingError(
                f"{analysis_id}: source trait_ontology_id term {term!r} in {value!r} is "
                "not a CURIE, an EFO or ORDO URI, or an OBO Library PURL"
            )
        curies.append(curie)
    return TRAIT_ONTOLOGY_ID_SEPARATOR.join(curies)


def _accepted_source_term(source: SourceTrait) -> tuple[str, str]:
    """``(normalised id, "")`` for an acceptable source term, or ``("", reason)``."""
    rejected = f"source term {source.ontology_id!r} rejected: "
    # Before normalising, so a bare Ensembl id is rejected rather than refused
    # as an unknown form; and after, so a PURL HGNC_1234 is caught as HGNC:1234.
    if is_gene_shaped_trait_ontology_id(source.ontology_id):
        return "", rejected + _GENE_REASON
    normalised = normalise_trait_ontology_id(source.ontology_id, analysis_id=source.analysis_id)
    if is_gene_shaped_trait_ontology_id(normalised):
        return "", rejected + _GENE_REASON
    if is_authority_name_trait_ontology_label(source.ontology_label):
        return "", (
            rejected + f"its label {source.ontology_label!r} names an identifier "
            "authority, not a Trait (#141)"
        )
    return normalised, ""


def map_trait_ontology(
    source: SourceTrait, table: CanonicalTraitMappingTable
) -> TraitOntologyMapping:
    """One Analysis's mapping: the source term, else the table, else unmapped."""
    note = ""
    if source.ontology_id:
        normalised, note = _accepted_source_term(source)
        if not note:
            return TraitOntologyMapping(
                trait_ontology_id=normalised,
                trait_ontology_label=source.ontology_label,
                method=TraitOntologyMappingMethod.SOURCE_PROVIDED,
            )
    rejected = source.ontology_id if note else ""

    hit = table.entries.get(_normalise_label(source.label)) if source.label.strip() else None
    if hit is not None:
        return TraitOntologyMapping(
            trait_ontology_id=hit.trait_ontology_id,
            trait_ontology_label=hit.trait_ontology_label,
            method=TraitOntologyMappingMethod.CANONICAL_TABLE_LOOKUP,
            note=note,
            rejected_source_term=rejected,
        )
    reason = (
        f"Source Trait Label {source.label!r} is not in the Canonical Trait Mapping Table"
        if source.label.strip()
        else "no Source Trait Label to look up"
    )
    return TraitOntologyMapping(
        trait_ontology_id="",
        trait_ontology_label="",
        method=TraitOntologyMappingMethod.UNMAPPED,
        note=f"{note}; {reason}" if note else f"no acceptable source term; {reason}",
        rejected_source_term=rejected,
    )


def map_trait_ontologies(
    sources: Iterable[SourceTrait], table: CanonicalTraitMappingTable
) -> TraitOntologyMappingRun:
    """Map every Analysis in ``sources``; an Analysis named twice is refused."""
    by_id: dict[str, SourceTrait] = {}
    for source in sources:
        if source.analysis_id in by_id:
            raise TraitOntologyMappingError(
                f"{source.analysis_id}: named twice in one Trait Ontology Mapping run"
            )
        by_id[source.analysis_id] = source
    mappings = {
        analysis_id: map_trait_ontology(source, table)
        for analysis_id, source in sorted(by_id.items())
    }
    return TraitOntologyMappingRun(mappings=mappings, sources=by_id, table=table)


__all__ = [
    "CANONICAL_TRAIT_MAPPING_RESOURCE",
    "CanonicalTraitMappingTable",
    "CanonicalTraitTerm",
    "SourceTrait",
    "TraitOntologyMapping",
    "TraitOntologyMappingError",
    "TraitOntologyMappingMethod",
    "TraitOntologyMappingRun",
    "load_canonical_trait_mapping_table",
    "map_trait_ontologies",
    "map_trait_ontology",
    "normalise_trait_ontology_id",
    "source_traits_from_source_metadata_table",
]
