#!/usr/bin/env python3
"""Pinned ontology release and the rebuildable retrieval index (issue #164).

Candidate generation resolves a queued Trait label against one *named* ontology
release. This module owns two things:

1. The pin itself. :data:`PINNED_ONTOLOGY_RELEASE` is the single string every
   shortlist row carries, so a later proposal can state which ontology release
   it was resolved against. It is pinned by name and bumped deliberately, never
   floated to whatever is current.

2. The retrieval index. The index is *derived* from the pinned release's source
   document (an OBO file) and is a rebuildable pipeline artifact held outside
   the tracked tree -- it is not a Reference Resource, because Reference
   Resources are build resources (ADR-0011) and nothing in a Build Recipe
   consumes this index. :func:`build_index_from_obo` rebuilds it and
   :func:`write_index` / :func:`load_index` persist and reload it.

Index format
------------
The index is a small JSON document, versioned by ``index_format_version`` so a
stale artifact fails loudly rather than being read with the wrong shape::

    {
      "index_format_version": 3,
      "ontology_release": "efo/v3.94.0",
      "excluded_non_curie_count": 6583,
      "terms": [
        {
          "ontology_id": "EFO:0004340",
          "label": "body mass index",
          "definition": "A measurement of ...",
          "parent_id": "EFO:0004338",
          "parent_label": "body weights and measures",
          "synonyms": ["BMI", "Quetelet index"],
          "alt_ids": [],
          "xrefs": ["ICD10:E11"],
          "is_obsolete": false,
          "replaced_by": ""
        }
      ]
    }

JSON rather than a TSV is deliberate: a term's synonyms are a nested list and
its definition is free text that may contain tabs, which a flat table cannot
carry without an escaping convention. The artifact is derived and untracked, so
it never needs to be hand-diffed.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, replace
from functools import cached_property
from pathlib import Path
from typing import Iterator, Mapping, Sequence

# ---------------------------------------------------------------------------
# The pin
# ---------------------------------------------------------------------------

# The ontology release candidate generation resolves against. Recorded on
# every shortlist row. Bump only when the pinned source document is replaced;
# a shortlist built against one release must never be reported as another.
PINNED_ONTOLOGY_RELEASE: str = "efo/v3.94.0"

# The index schema version. A reader refuses an artifact whose version it does
# not know, so a rebuild is forced rather than a stale shape being misread.
# Bumped to 2 when canonical id normalisation was added: a version-1 index
# carried the OBO's native ``efo:EFO_...`` ids and IRI ids and must be rebuilt.
# Bumped to 3 when ``xrefs`` storage was added (issue #185): a version-2 index
# has no xrefs, so its ``icd10_lookup`` would silently be wrong; every artifact
# must be rebuilt.
INDEX_FORMAT_VERSION: int = 3

# Rebuildable index artifacts live outside the tracked tree. See `.gitignore`.
DEFAULT_INDEX_DIR: Path = Path(".cache") / "curation"


class OntologyError(ValueError):
    """Base error for an ontology source or index that cannot be read."""


class IndexFormatError(OntologyError):
    """Raised when an index artifact is not a known, well-formed index."""


class ObOFormatError(OntologyError):
    """Raised when an OBO source document cannot be parsed."""


# ---------------------------------------------------------------------------
# Canonical ontology identifiers
# ---------------------------------------------------------------------------
#
# An OBO release does not spell every term id the way the rest of the repository
# does. EFO v3.94.0's native terms use a lower-case, underscore form
# (``efo:EFO_0003939``), some imported terms are already CURIEs
# (``MONDO:0000001``), and many more carry a full IRI. Every Release Manifest
# and every shortlist row uses the canonical CURIE form, so the index must
# normalise before it stores or looks anything up.

# ``<lower>:<PREFIX>_<local>`` -> ``<PREFIX>:<local>`` (e.g. efo:EFO_0000270).
_LOWER_PREFIX_RE = re.compile(
    r"^([a-z][a-z0-9]*):([A-Za-z][A-Za-z0-9]*)_([^:]+)$"
)
# OBO PURL, in either the ``X_N`` or the ``X:N`` spelling.
_OBO_PURL_RE = re.compile(
    r"^https?://purl\.obolibrary\.org/obo/([A-Za-z][A-Za-z0-9]*)[_:]([^:]+)$"
)
# EBI's EFO-repository IRI form.
_EBI_EFO_RE = re.compile(r"^https?://www\.ebi\.ac\.uk/efo/EFO[_:]([^:]+)$")
# ORDO's Orphanet IRI form.
_ORPHA_IRI_RE = re.compile(
    r"^https?://www\.orpha\.net/ORDO/Orphanet[_:]([^:]+)$"
)
# A canonical CURIE: a non-empty alphabetic prefix, a colon, and a local id.
_CURIE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*:[^\s:]+$")


def normalise_ontology_id(ontology_id: str | None) -> str:
    """Return the canonical CURIE form of an ontology identifier.

    Normalises the OBO release's spelling variants to the form used by Release
    Manifests and shortlist rows:

    * ``efo:EFO_0000270`` -> ``EFO:0000270``;
    * ``http://purl.obolibrary.org/obo/MONDO_0004979`` -> ``MONDO:0004979``;
    * ``http://www.ebi.ac.uk/efo/EFO_0000270`` -> ``EFO:0000270``;
    * ``http://www.orpha.net/ORDO/Orphanet_58`` -> ``Orphanet:58``.

    An id that is already a CURIE is returned unchanged, as is one that no rule
    recognises (a gene or dbpedia IRI). Idempotent, so it is safe to apply at
    both index-build time and every comparison against a source-provided id.
    """
    value = (ontology_id or "").strip()
    if not value:
        return ""
    match = _OBO_PURL_RE.match(value)
    if match:
        return f"{match.group(1)}:{match.group(2)}"
    match = _EBI_EFO_RE.match(value)
    if match:
        return f"EFO:{match.group(1)}"
    match = _ORPHA_IRI_RE.match(value)
    if match:
        return f"Orphanet:{match.group(1)}"
    match = _LOWER_PREFIX_RE.match(value)
    if match:
        return f"{match.group(2)}:{match.group(3)}"
    return value


def is_curie(ontology_id: str | None) -> bool:
    """Whether ``ontology_id`` is a canonical ``PREFIX:local`` CURIE."""
    value = (ontology_id or "").strip()
    # An ``http:``/``https:`` IRI trivially matches the prefix:local shape, so
    # reject any URL before the CURIE regex can accept it.
    if "://" in value:
        return False
    return bool(_CURIE_RE.match(value))


# ---------------------------------------------------------------------------
# Terms and index
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OntologyTerm:
    """One ontology term as candidate generation needs to see it.

    ``parent_id``/``parent_label`` are the term's primary ``is_a`` parent. The
    parent is carried because it is what distinguishes a measurement term from
    a disease term of the same name (issue #161, user story 11).
    """

    ontology_id: str
    label: str
    definition: str = ""
    parent_id: str = ""
    parent_label: str = ""
    synonyms: tuple[str, ...] = ()
    alt_ids: tuple[str, ...] = ()
    xrefs: tuple[str, ...] = ()
    is_obsolete: bool = False
    replaced_by: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "ontology_id": self.ontology_id,
            "label": self.label,
            "definition": self.definition,
            "parent_id": self.parent_id,
            "parent_label": self.parent_label,
            "synonyms": list(self.synonyms),
            "alt_ids": list(self.alt_ids),
            "xrefs": list(self.xrefs),
            "is_obsolete": self.is_obsolete,
            "replaced_by": self.replaced_by,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "OntologyTerm":
        return cls(
            ontology_id=str(data.get("ontology_id", "")),
            label=str(data.get("label", "")),
            definition=str(data.get("definition", "")),
            parent_id=str(data.get("parent_id", "")),
            parent_label=str(data.get("parent_label", "")),
            synonyms=tuple(str(s) for s in data.get("synonyms", []) or []),
            alt_ids=tuple(str(s) for s in data.get("alt_ids", []) or []),
            xrefs=tuple(str(s) for s in data.get("xrefs", []) or []),
            is_obsolete=bool(data.get("is_obsolete", False)),
            replaced_by=str(data.get("replaced_by", "")),
        )


# ---------------------------------------------------------------------------
# Lexical primitives
# ---------------------------------------------------------------------------
# These are the *retrieval* normalisation/tokenisation rules the lexical
# channels share. They live with the index because the prebuilt lookups below
# are built from them; ``curation.candidates`` re-exports them for callers.

_PUNCTUATION_RE = re.compile(r"[\W_]+", re.UNICODE)


def normalise_label(label: str | None) -> str:
    """Trim, lowercase, strip punctuation, and collapse whitespace.

    Deliberately close to, but wider than, the canonical table's
    ``trimws(tolower(x))`` lookup key: this is a *retrieval* normalisation, so
    ``"Body-mass index (BMI)"`` and ``"body mass index bmi"`` collapse to the
    same string and the normalised channel can find terms the exact channel
    cannot.
    """
    text = (label or "").strip().lower()
    text = _PUNCTUATION_RE.sub(" ", text)
    return " ".join(text.split())


def tokenize(label: str | None) -> frozenset[str]:
    """Tokenize a label after normalisation."""
    normalised = normalise_label(label)
    return frozenset(normalised.split()) if normalised else frozenset()


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    """Jaccard similarity, 0.0 for two empty token sets."""
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def acronym(label: str | None) -> str:
    """First letter of each token of a multi-word label, lowercased.

    A single-token label has no distinct acronym and yields ``""`` -- returning
    the token itself would make the synonym channel match on ordinary shared
    words.
    """
    tokens = normalise_label(label).split()
    if len(tokens) < 2:
        return ""
    return "".join(token[0] for token in tokens)


@dataclass(frozen=True)
class LexicalLookups:
    """Prebuilt lookup structures for the lexical retrieval channels.

    A channel that scans every term for every query is quadratic in the corpus
    (94k EFO terms x 2.5k ukb-b labels). These maps are built once per loaded
    :class:`OntologyIndex` and turn each channel into a handful of dictionary
    probes plus, for token overlap, one pass over the postings of the query's
    tokens. The channels' outputs -- ids, order, and ranks -- are identical to
    the brute-force scans they replace; the equivalence is asserted in
    ``tests/curation/test_candidates.py``.
    """

    exact: Mapping[str, tuple[str, ...]]
    normalised: Mapping[str, tuple[str, ...]]
    token_postings: Mapping[str, tuple[str, ...]]
    term_token_counts: Mapping[str, int]
    raw_synonyms: Mapping[str, tuple[str, ...]]
    normalised_synonyms: Mapping[str, tuple[str, ...]]
    acronyms: Mapping[str, tuple[str, ...]]

    @classmethod
    def build(cls, index: "OntologyIndex") -> "LexicalLookups":
        exact: dict[str, list[str]] = defaultdict(list)
        normalised: dict[str, list[str]] = defaultdict(list)
        token_postings: dict[str, list[str]] = defaultdict(list)
        term_token_counts: dict[str, int] = {}
        raw_synonyms: dict[str, list[str]] = defaultdict(list)
        normalised_synonyms: dict[str, list[str]] = defaultdict(list)
        acronyms: dict[str, list[str]] = defaultdict(list)

        for term in index:
            ontology_id = term.ontology_id
            exact[term.label].append(ontology_id)
            normalised[normalise_label(term.label)].append(ontology_id)

            tokens = tokenize(term.label)
            for synonym in term.synonyms:
                tokens |= tokenize(synonym)
            term_token_counts[ontology_id] = len(tokens)
            for token in tokens:
                token_postings[token].append(ontology_id)

            for synonym in term.synonyms:
                raw_synonyms[synonym].append(ontology_id)
                normalised_synonyms[normalise_label(synonym)].append(ontology_id)

            generated = {acronym(term.label)}
            generated.update(acronym(synonym) for synonym in term.synonyms)
            for candidate in generated:
                if candidate:
                    acronyms[candidate].append(ontology_id)

        def frozen(mapping: Mapping[str, list[str]]) -> dict[str, tuple[str, ...]]:
            return {key: tuple(sorted(values)) for key, values in mapping.items()}

        return cls(
            exact=frozen(exact),
            normalised=frozen(normalised),
            token_postings=frozen(token_postings),
            term_token_counts=term_token_counts,
            raw_synonyms=frozen(raw_synonyms),
            normalised_synonyms=frozen(normalised_synonyms),
            acronyms=frozen(acronyms),
        )

    def exact_ids(self, label: str) -> list[str]:
        """Ids whose label is byte-for-byte ``label``, sorted ascending."""
        return list(self.exact.get(label, ()))

    def normalised_ids(self, key: str) -> list[str]:
        """Ids whose label normalises to ``key``, sorted ascending."""
        if not key:
            return []
        return list(self.normalised.get(key, ()))

    def synonym_ids(self, raw: str, normalised: str) -> list[str]:
        """Ids matched by a declared synonym, a normalised synonym, or an acronym."""
        matched: set[str] = set()
        if raw:
            matched.update(self.raw_synonyms.get(raw, ()))
        if normalised:
            matched.update(self.normalised_synonyms.get(normalised, ()))
            matched.update(self.acronyms.get(normalised, ()))
        return sorted(matched)

    def token_overlap_ids(
        self, query_tokens: frozenset[str], limit: int
    ) -> list[str]:
        """Ids ranked by Jaccard token overlap, ties broken by id."""
        if not query_tokens:
            return []
        intersections: dict[str, int] = {}
        for token in query_tokens:
            for ontology_id in self.token_postings.get(token, ()):
                intersections[ontology_id] = intersections.get(ontology_id, 0) + 1
        if not intersections:
            return []
        query_size = len(query_tokens)
        scored: list[tuple[float, str]] = []
        for ontology_id, intersection in intersections.items():
            term_size = self.term_token_counts.get(ontology_id, 0)
            union = query_size + term_size - intersection
            if union <= 0:
                continue
            score = intersection / union
            if score > 0.0:
                scored.append((score, ontology_id))
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        return [ontology_id for _, ontology_id in scored[:limit]]


@dataclass(frozen=True)
class OntologyIndex:
    """The retrieval index: one pinned release's terms, in source order."""

    ontology_release: str
    terms: tuple[OntologyTerm, ...]
    #: How many source terms were dropped because their id could not be
    #: normalised to a CURIE (gene/dbpedia IRIs). Reported by the builder so a
    #: surprising corpus is visible rather than silently smaller.
    excluded_non_curie_count: int = 0

    def __len__(self) -> int:
        return len(self.terms)

    def __iter__(self) -> Iterator[OntologyTerm]:
        return iter(self.terms)

    @cached_property
    def _by_id(self) -> dict[str, OntologyTerm]:
        return {term.ontology_id: term for term in self.terms}

    def by_id(self) -> dict[str, OntologyTerm]:
        """A mapping of ontology id to term, built once per index."""
        return self._by_id

    @cached_property
    def lexical_lookups(self) -> LexicalLookups:
        """The lexical channels' prebuilt lookup structures, built once."""
        return LexicalLookups.build(self)

    @cached_property
    def icd10_lookup(self) -> dict[str, tuple[str, ...]]:
        """Normalised ICD-10 xref code to the live terms carrying it.

        Keys are the xref code uppercased and stripped of its decimal point
        (``ICD10:C34.9`` -> ``C349``), so a ukb-b label code normalised the
        same way probes the map directly. The provenances (``ICD10``,
        ``ICD10CM``, ``ICD10WHO``) are merged per code, obsolete terms are
        skipped (an obsolete term is never a retrievable candidate), and ids
        are sorted so the order is deterministic.
        """
        by_code: dict[str, set[str]] = {}
        for term in self.terms:
            if term.is_obsolete:
                continue
            for xref in term.xrefs:
                code = icd10_code_from_xref(xref)
                if code:
                    by_code.setdefault(code, set()).add(term.ontology_id)
        return {code: tuple(sorted(ids)) for code, ids in by_code.items()}

    @cached_property
    def _alt_id_owner(self) -> dict[str, str]:
        """Map every declared alternate id to the canonical term owning it."""
        owners: dict[str, str] = {}
        for term in self.terms:
            for alt_id in term.alt_ids:
                owners.setdefault(alt_id, term.ontology_id)
        return owners

    def equivalent_ids(self, ontology_id: str | None) -> tuple[str, ...]:
        """Canonical ids equivalent to a source id, excluding the id itself.

        Two relationships make ids equivalent:

        * a term's ``alt_ids`` are aliases of the term, so an id that is an
          ``alt_id`` of a term is treated as that term and vice versa;
        * an obsolete term's ``replaced_by`` target is its successor.

        The closure is followed transitively and de-duplicated, so a chain of
        replacements (or an alias of a replaced term) resolves fully. Unknown
        ids, and obsolete terms with no replacement, yield an empty tuple.
        """
        source = normalise_ontology_id(ontology_id)
        if not source:
            return ()
        accepted: list[str] = []
        seen: set[str] = {source}
        frontier: list[str] = [source]
        while frontier:
            current = frontier.pop(0)
            candidates: list[str] = []
            term = self._by_id.get(current)
            if term is not None:
                candidates.extend(term.alt_ids)
                if term.replaced_by:
                    candidates.append(term.replaced_by)
            owner_id = self._alt_id_owner.get(current)
            if owner_id is not None:
                candidates.append(owner_id)
            for candidate in candidates:
                if candidate and candidate not in seen:
                    seen.add(candidate)
                    accepted.append(candidate)
                    frontier.append(candidate)
        return tuple(accepted)

    def to_dict(self) -> dict[str, object]:
        return {
            "index_format_version": INDEX_FORMAT_VERSION,
            "ontology_release": self.ontology_release,
            "excluded_non_curie_count": self.excluded_non_curie_count,
            "terms": [term.to_dict() for term in self.terms],
        }


# ---------------------------------------------------------------------------
# ICD-10 xrefs
# ---------------------------------------------------------------------------
# EFO imports ICD-10 codes as term xrefs in three provenances. The lookup key
# is the code itself, uppercased and with the decimal point removed, so both a
# ``ICD10:Q51`` and a ``ICD10CM:C34.9`` xref land in one map an ICD-10 code
# can probe directly.

_ICD10_XREF_PREFIXES: tuple[str, ...] = ("ICD10:", "ICD10CM:", "ICD10WHO:")


def normalise_icd10_code(code: str | None) -> str:
    """Return an ICD-10 code's lookup key: uppercase, no decimal point.

    An annotation suffix (the ``{source=...}`` block EFO appends to imported
    xrefs) and trailing whitespace are dropped first, so ``"c34.9 "`` and
    ``"C34.9 {source=\"MONDO:equivalentTo\"}"`` both key as ``"C349"``.
    """
    value = (code or "").strip()
    value = value.split("{", 1)[0].strip()
    value = value.split()[0] if value else value
    return value.upper().replace(".", "")


def icd10_code_from_xref(xref: str | None) -> str:
    """The normalised ICD-10 code key a term xref carries, or ``""``.

    Only xrefs in the ``ICD10``/``ICD10CM``/``ICD10WHO`` provenances are read;
    a MONDO or MESH xref is not an ICD-10 code and yields nothing.
    """
    value = (xref or "").strip()
    for prefix in _ICD10_XREF_PREFIXES:
        if value.startswith(prefix):
            return normalise_icd10_code(value[len(prefix):])
    return ""


# ---------------------------------------------------------------------------
# Building the index from an OBO source document
# ---------------------------------------------------------------------------

# `def: "text" [xref]` / `synonym: "text" SCOPE [xref]`. The quoted string may
# contain escaped quotes; definitions may span a single line by OBO convention.
_QUOTED_PREFIX_RE = re.compile(r'^"(?P<value>(?:[^"\\]|\\.)*)"')


def _extract_quoted(value: str) -> str:
    """Return the first double-quoted string in an OBO value, unescaped.

    Falls back to the whole value when it is not quoted, so a malformed source
    line yields something inspectable rather than being silently dropped.
    """
    match = _QUOTED_PREFIX_RE.match(value)
    if not match:
        return value.strip()
    return (
        match.group("value")
        .replace('\\"', '"')
        .replace("\\\\", "\\")
    )


def _iter_obo_stanzas(text: str) -> Iterator[tuple[str, list[str]]]:
    """Yield ``(stanza_type, body_lines)`` for every ``[Type]`` block in an OBO file."""
    stanza_type: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if stanza_type is not None:
                yield stanza_type, body
            stanza_type = stripped[1:-1].strip()
            body = []
        elif not stripped:
            # Blank lines are ignored; a new ``[Header]`` ends the stanza.
            continue
        elif stanza_type is not None:
            body.append(line)
    if stanza_type is not None:
        yield stanza_type, body


def _parse_obo_term(body: Sequence[str]) -> OntologyTerm | None:
    """Parse one ``[Term]`` stanza body, or ``None`` when it has no id."""
    fields: dict[str, list[str]] = defaultdict(list)
    for line in body:
        tag, separator, value = line.partition(":")
        if not separator:
            continue
        fields[tag.strip()].append(value.strip())

    if "id" not in fields or not fields["id"][0]:
        return None

    parent_id, parent_label = "", ""
    for is_a in fields.get("is_a", []):
        identifier, _, label = is_a.partition("!")
        parent_id = identifier.strip()
        parent_label = label.strip()
        break

    definition = _extract_quoted(fields["def"][0]) if fields.get("def") else ""
    synonyms = tuple(_extract_quoted(s) for s in fields.get("synonym", []))
    alt_ids = tuple(
        normalise_ontology_id(value) for value in fields.get("alt_id", [])
    )
    # Xrefs are kept as the raw OBO values (e.g. ``ICD10:E11``,
    # ``ICD10CM:E30.1 {source=...}``); the ICD-10 lookup indexes them, so a
    # malformed or unrecognised xref simply contributes nothing there.
    xrefs = tuple(fields.get("xref", []))

    return OntologyTerm(
        ontology_id=normalise_ontology_id(fields["id"][0]),
        label=fields.get("name", [""])[0],
        definition=definition,
        parent_id=normalise_ontology_id(parent_id),
        parent_label=parent_label,
        synonyms=synonyms,
        alt_ids=alt_ids,
        xrefs=xrefs,
        is_obsolete=any(v.lower() == "true" for v in fields.get("is_obsolete", [])),
        replaced_by=normalise_ontology_id(fields.get("replaced_by", [""])[0]),
    )


def parse_obo(text: str) -> list[OntologyTerm]:
    """Parse the ``[Term]`` stanzas of an OBO document into terms.

    A parent label is taken from the ``is_a ... ! label`` comment when present,
    and otherwise resolved from the parent term's own label in the same
    document. A parent id that names no term in the document keeps an empty
    label rather than a guessed one.
    """
    terms: list[OntologyTerm] = []
    for stanza_type, body in _iter_obo_stanzas(text):
        if stanza_type != "Term":
            continue
        term = _parse_obo_term(body)
        if term is not None:
            terms.append(term)

    by_id = {term.ontology_id: term for term in terms}
    resolved: list[OntologyTerm] = []
    for term in terms:
        if term.parent_id and not term.parent_label:
            parent = by_id.get(term.parent_id)
            if parent is not None:
                term = replace(term, parent_label=parent.label)
        resolved.append(term)
    return resolved


def _read_source_text(path: Path) -> str:
    """Read an OBO source document, transparently decompressing ``.gz``."""
    if not path.is_file():
        raise OntologyError(f"ontology source does not exist: {path}")
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return f.read()
    return path.read_text(encoding="utf-8")


def build_index_from_obo(
    obo_path: Path | str,
    ontology_release: str = PINNED_ONTOLOGY_RELEASE,
) -> OntologyIndex:
    """Rebuild the retrieval index from a pinned release's OBO document.

    Term, parent, ``replaced_by`` and alternate ids are all normalised to the
    canonical CURIE form. A term whose id is still not a CURIE after that (a
    gene or dbpedia IRI) cannot be retrieved or matched, so it is excluded and
    counted on the index.
    """
    source = Path(obo_path)
    parsed = parse_obo(_read_source_text(source))
    if not parsed:
        raise ObOFormatError(f"{source} contains no [Term] stanzas")
    terms: list[OntologyTerm] = []
    excluded = 0
    for term in parsed:
        if not is_curie(term.ontology_id):
            excluded += 1
            continue
        terms.append(term)
    if not terms:
        raise ObOFormatError(
            f"{source} contains no [Term] stanzas with a canonical ontology id"
        )
    if excluded:
        print(
            f"ontology-index: excluded {excluded} term(s) whose id is not a "
            "canonical CURIE (gene/dbpedia-style IRIs)",
            file=sys.stderr,
        )
    return OntologyIndex(
        ontology_release=ontology_release,
        terms=tuple(terms),
        excluded_non_curie_count=excluded,
    )


# ---------------------------------------------------------------------------
# Persisting and loading the index
# ---------------------------------------------------------------------------


def default_index_path(ontology_release: str = PINNED_ONTOLOGY_RELEASE) -> Path:
    """The default untracked artifact path for a pinned release's index."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", ontology_release).strip("-")
    return DEFAULT_INDEX_DIR / f"{slug}.index.json"


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


def write_index(index: OntologyIndex, path: Path | str) -> None:
    """Serialize an index to ``path`` atomically."""
    text = json.dumps(index.to_dict(), ensure_ascii=False, indent=2) + "\n"
    _write_text_atomically(text, Path(path))


def index_from_dict(data: object) -> OntologyIndex:
    """Validate and decode a serialized index document."""
    if not isinstance(data, dict):
        raise IndexFormatError("index artifact is not a JSON object")
    version = data.get("index_format_version")
    if version != INDEX_FORMAT_VERSION:
        raise IndexFormatError(
            f"index format version {version!r} is not the supported "
            f"{INDEX_FORMAT_VERSION}; rebuild the index"
        )
    release = data.get("ontology_release")
    if not isinstance(release, str) or not release:
        raise IndexFormatError("index artifact carries no ontology_release")
    raw_terms = data.get("terms")
    if not isinstance(raw_terms, list):
        raise IndexFormatError("index artifact carries no terms list")
    terms = tuple(
        OntologyTerm.from_dict(term)
        for term in raw_terms
        if isinstance(term, dict)
    )
    excluded = data.get("excluded_non_curie_count")
    excluded_count = int(excluded) if isinstance(excluded, int) and not isinstance(excluded, bool) else 0
    return OntologyIndex(
        ontology_release=release,
        terms=terms,
        excluded_non_curie_count=excluded_count,
    )


def load_index(path: Path | str) -> OntologyIndex:
    """Load a previously built index artifact, failing loudly on a bad shape."""
    index_path = Path(path)
    if not index_path.is_file():
        raise IndexFormatError(f"ontology index does not exist: {index_path}")
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise IndexFormatError(f"{index_path} is not valid JSON: {exc}") from exc
    return index_from_dict(data)


# ---------------------------------------------------------------------------
# CLI: rebuild the index artifact
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ontology-index",
        description=(
            "Rebuild the untracked retrieval index from a pinned ontology "
            "release's OBO document."
        ),
    )
    parser.add_argument(
        "--obo",
        required=True,
        metavar="PATH",
        help="OBO source document for the pinned release (may be .gz)",
    )
    parser.add_argument(
        "--release",
        default=PINNED_ONTOLOGY_RELEASE,
        metavar="RELEASE",
        help=f"pinned ontology release string (default: {PINNED_ONTOLOGY_RELEASE})",
    )
    parser.add_argument(
        "--output",
        default=None,
        metavar="PATH",
        help="index artifact path (default: the release's .cache/curation path)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        index = build_index_from_obo(args.obo, args.release)
    except OntologyError as exc:
        print(f"ontology-index: error: {exc}", file=sys.stderr)
        return 1

    output = Path(args.output) if args.output else default_index_path(args.release)
    write_index(index, output)
    print(
        f"ontology-index: wrote {len(index)} terms for {index.ontology_release} "
        f"to {output}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
