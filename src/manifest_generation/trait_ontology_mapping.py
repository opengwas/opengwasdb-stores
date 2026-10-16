"""Trait Ontology Mapping: the shared step after verification (#286, ADR 0036 step 5).

Every Analysis gets one Trait Ontology Mapping and the method that produced it:

1. ``source_provided``: the Source Collection's own term, from the Source
   Metadata Table's ``source_trait_ontology_id``/``source_trait_ontology_label``,
   normalised to CURIEs and otherwise kept as the source gives it;
2. ``canonical_table_lookup``: otherwise, the Source Trait Label matched
   exactly (trimmed, lower-cased) against the Canonical Trait Mapping Table, a
   tracked Reference Resource read from a directory the caller names, so the
   lookup is offline and its version and sha256 are recorded;
3. ``unmapped``: otherwise, with the id and label left empty.

The lookup lives in this registry, not OpenGWASDB (ADR 0021). This module
replaces ``resources/generators/lib/metadata_resolvers/canonical_trait_table.R``
for the Python pipeline; the R generators keep using that file until they
retire (epic #282).

**Normalisation.** A source term is split on commas and each term becomes a
CURIE: ``http://www.ebi.ac.uk/efo/EFO_x`` -> ``EFO:x``,
``http://purl.obolibrary.org/obo/NS_local`` -> ``NS:local``,
``http://www.orpha.net/ORDO/Orphanet_N`` -> ``Orphanet:N``, and a CURIE stays
as it is. Any other form is refused, naming the Analysis and the term, rather
than passed through. Several terms are joined by ``", "`` in the source's order
(the Release Bundle contract's list form); the label is kept verbatim, because
a term's own label may contain a comma and cannot be split per term.

**Gene and authority guards (#141).** A source term with a gene- or
protein-shaped identifier in any term, or an identifier-authority name for a
label, is rejected: it falls through to the table, and the rejection and its
reason are recorded. A table row of either kind is a curation error and fails
the load. The guards are the Release Bundle contract's own
(``ogstores.bundle``).
"""

from __future__ import annotations

import csv
import hashlib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from ogstores.bundle import (
    TRAIT_ONTOLOGY_ID_SEPARATOR,
    is_authority_name_trait_ontology_label,
    is_gene_shaped_trait_ontology_id,
    is_trait_ontology_curie,
    split_trait_ontology_id,
)

#: The Trait Ontology Mapping Method vocabulary (``trait_ontology_mapping_method``).
SOURCE_PROVIDED = "source_provided"
CANONICAL_TABLE_LOOKUP = "canonical_table_lookup"
UNMAPPED = "unmapped"
TRAIT_ONTOLOGY_MAPPING_METHODS: tuple[str, ...] = (
    SOURCE_PROVIDED,
    CANONICAL_TABLE_LOOKUP,
    UNMAPPED,
)

#: The tracked Canonical Trait Mapping Table, relative to the repository root.
CANONICAL_TRAIT_MAPPING_RESOURCE = Path(
    "resources/reference-resources/canonical-trait-mapping-efo"
)
_RESOURCE_KIND = "trait_ontology_mapping"
_LOOKUP_COLUMNS = ("trait_label", "trait_ontology_id", "trait_ontology_label")

#: URI prefix -> CURIE prefix, or None when the URI's own ``NS_`` names it.
_URI_FORMS: tuple[tuple[str, str | None], ...] = (
    ("http://www.ebi.ac.uk/efo/EFO_", "EFO"),
    ("http://www.orpha.net/ORDO/Orphanet_", "Orphanet"),
    ("http://purl.obolibrary.org/obo/", None),
)


class TraitOntologyMappingError(ValueError):
    """A source term or Canonical Trait Mapping Table this step must refuse."""


@dataclass(frozen=True)
class SourceTrait:
    """One Analysis's trait columns from the Source Metadata Table, verbatim."""

    analysis_id: str
    #: The Source Trait Label: the table lookup's key.
    label: str
    #: ``source_trait_ontology_id``: a CURIE, a URI, or a comma-separated list.
    ontology_id: str
    #: ``source_trait_ontology_label``.
    ontology_label: str


@dataclass(frozen=True)
class TraitOntologyMapping:
    """One Analysis's Trait Ontology Mapping and how it was produced."""

    trait_ontology_id: str
    trait_ontology_label: str
    method: str
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
    #: Normalised trait label -> (trait_ontology_id, trait_ontology_label).
    entries: Mapping[str, tuple[str, str]] = field(repr=False)

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
    table: CanonicalTraitMappingTable | None

    def provenance(self) -> dict[str, Any]:
        """What release.yaml records under ``trait_ontology_mapping``."""
        counts = Counter(mapping.method for mapping in self.mappings.values())
        return {
            "canonical_trait_mapping_table": (
                self.table.provenance() if self.table is not None else None
            ),
            "method_counts": {method: counts.get(method, 0) for method in TRAIT_ONTOLOGY_MAPPING_METHODS},
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
    """The lookup key: trimmed and lower-cased, as the R resolver does."""
    return label.strip().lower()


def load_canonical_trait_mapping_table(resource_dir: Path) -> CanonicalTraitMappingTable:
    """Load the Canonical Trait Mapping Table in ``resource_dir``.

    ``resource.yaml`` gives the identity and version; the table is the file its
    ``location`` names, read from ``resource_dir`` itself. Only the three lookup
    columns are read; the provenance columns are for reviewers. A blank lookup
    value, a non-CURIE term, a gene-shaped term, an authority-name label or a
    label listed twice is refused: the table is curated, so a bad row is an
    error to fix rather than a row to skip.
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
    location = declaration.get("location")
    if not isinstance(resource_id, str) or not resource_id:
        raise TraitOntologyMappingError(f"{declaration_path} declares no resource_id")
    if not isinstance(version, int) or isinstance(version, bool):
        raise TraitOntologyMappingError(
            f"{declaration_path} declares no integer version: {version!r}"
        )
    if not isinstance(location, str) or not location:
        raise TraitOntologyMappingError(f"{declaration_path} declares no location")

    table_path = resource_dir / Path(location).name
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

    entries: dict[str, tuple[str, str]] = {}
    problems: list[str] = []
    for line, row in enumerate(reader, start=2):
        label, curie, term_label = (row.get(column) or "" for column in _LOOKUP_COLUMNS)
        where = f"{table_path}:{line}"
        if not (label.strip() and curie and term_label.strip()):
            problems.append(f"{where}: a lookup value is blank")
            continue
        if is_gene_shaped_trait_ontology_id(curie):
            problems.append(f"{where}: gene-shaped trait_ontology_id {curie!r} (#141)")
            continue
        if is_authority_name_trait_ontology_label(term_label):
            problems.append(f"{where}: authority-name trait_ontology_label {term_label!r} (#141)")
            continue
        if not all(map(is_trait_ontology_curie, split_trait_ontology_id(curie))):
            problems.append(f"{where}: trait_ontology_id {curie!r} is not a CURIE")
            continue
        key = _normalise_label(label)
        if key in entries:
            problems.append(f"{where}: trait_label {label!r} is listed twice")
            continue
        entries[key] = (curie, term_label)
    if problems:
        raise TraitOntologyMappingError(
            f"Canonical Trait Mapping Table {table_path} refused:\n  - "
            + "\n  - ".join(problems[:20])
        )
    return CanonicalTraitMappingTable(
        resource_id=resource_id,
        version=version,
        location=location,
        sha256=hashlib.sha256(data).hexdigest(),
        entries=entries,
    )


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
    terms = [term.strip() for term in value.split(",")]
    curies: list[str] = []
    for term in terms:
        curie = _curie(term)
        if curie is None:
            raise TraitOntologyMappingError(
                f"{analysis_id}: source trait_ontology_id term {term!r} in {value!r} is "
                "not a CURIE, an EFO or ORDO URI, or an OBO Library PURL"
            )
        curies.append(curie)
    return TRAIT_ONTOLOGY_ID_SEPARATOR.join(curies)


def map_trait_ontology(
    source: SourceTrait, table: CanonicalTraitMappingTable | None
) -> TraitOntologyMapping:
    """One Analysis's mapping: the source term, else the table, else unmapped."""
    rejected = ""
    note = ""
    if source.ontology_id:
        if is_gene_shaped_trait_ontology_id(source.ontology_id):
            rejected = source.ontology_id
            note = (
                f"source term {source.ontology_id!r} rejected: a gene or protein "
                "identifier is not a Trait Ontology Mapping (#141)"
            )
        elif is_authority_name_trait_ontology_label(source.ontology_label):
            rejected = source.ontology_id
            note = (
                f"source term {source.ontology_id!r} rejected: its label "
                f"{source.ontology_label!r} names an identifier authority, not a Trait (#141)"
            )
        else:
            return TraitOntologyMapping(
                trait_ontology_id=normalise_trait_ontology_id(
                    source.ontology_id, analysis_id=source.analysis_id
                ),
                trait_ontology_label=source.ontology_label,
                method=SOURCE_PROVIDED,
            )

    hit = None
    if table is not None and source.label.strip():
        hit = table.entries.get(_normalise_label(source.label))
    if hit is not None:
        return TraitOntologyMapping(
            trait_ontology_id=hit[0],
            trait_ontology_label=hit[1],
            method=CANONICAL_TABLE_LOOKUP,
            note=note,
            rejected_source_term=rejected,
        )
    if not source.label.strip():
        reason = "no Source Trait Label to look up"
    elif table is None:
        reason = "no Canonical Trait Mapping Table"
    else:
        reason = f"Source Trait Label {source.label!r} is not in the Canonical Trait Mapping Table"
    return TraitOntologyMapping(
        trait_ontology_id="",
        trait_ontology_label="",
        method=UNMAPPED,
        note=f"{note}; {reason}" if note else f"no acceptable source term; {reason}",
        rejected_source_term=rejected,
    )


def map_trait_ontologies(
    sources: Iterable[SourceTrait], table: CanonicalTraitMappingTable | None
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
    "CANONICAL_TABLE_LOOKUP",
    "CANONICAL_TRAIT_MAPPING_RESOURCE",
    "SOURCE_PROVIDED",
    "TRAIT_ONTOLOGY_MAPPING_METHODS",
    "UNMAPPED",
    "CanonicalTraitMappingTable",
    "SourceTrait",
    "TraitOntologyMapping",
    "TraitOntologyMappingError",
    "TraitOntologyMappingRun",
    "load_canonical_trait_mapping_table",
    "map_trait_ontologies",
    "map_trait_ontology",
    "normalise_trait_ontology_id",
]
