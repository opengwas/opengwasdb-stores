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
      "index_format_version": 1,
      "ontology_release": "efo/v3.78.0",
      "terms": [
        {
          "ontology_id": "EFO:0004340",
          "label": "body mass index",
          "definition": "A measurement of ...",
          "parent_id": "EFO:0004338",
          "parent_label": "body weights and measures",
          "synonyms": ["BMI", "Quetelet index"],
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
from pathlib import Path
from typing import Iterator, Sequence

# ---------------------------------------------------------------------------
# The pin
# ---------------------------------------------------------------------------

# The ontology release candidate generation resolves against. Recorded on
# every shortlist row. Bump only when the pinned source document is replaced;
# a shortlist built against one release must never be reported as another.
PINNED_ONTOLOGY_RELEASE: str = "efo/v3.78.0"

# The index schema version. A reader refuses an artifact whose version it does
# not know, so a rebuild is forced rather than a stale shape being misread.
INDEX_FORMAT_VERSION: int = 1

# Rebuildable index artifacts live outside the tracked tree. See `.gitignore`.
DEFAULT_INDEX_DIR: Path = Path(".cache") / "curation"


class OntologyError(ValueError):
    """Base error for an ontology source or index that cannot be read."""


class IndexFormatError(OntologyError):
    """Raised when an index artifact is not a known, well-formed index."""


class ObOFormatError(OntologyError):
    """Raised when an OBO source document cannot be parsed."""


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
            is_obsolete=bool(data.get("is_obsolete", False)),
            replaced_by=str(data.get("replaced_by", "")),
        )


@dataclass(frozen=True)
class OntologyIndex:
    """The retrieval index: one pinned release's terms, in source order."""

    ontology_release: str
    terms: tuple[OntologyTerm, ...]

    def __len__(self) -> int:
        return len(self.terms)

    def __iter__(self) -> Iterator[OntologyTerm]:
        return iter(self.terms)

    def by_id(self) -> dict[str, OntologyTerm]:
        return {term.ontology_id: term for term in self.terms}

    def to_dict(self) -> dict[str, object]:
        return {
            "index_format_version": INDEX_FORMAT_VERSION,
            "ontology_release": self.ontology_release,
            "terms": [term.to_dict() for term in self.terms],
        }


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

    return OntologyTerm(
        ontology_id=fields["id"][0],
        label=fields.get("name", [""])[0],
        definition=definition,
        parent_id=parent_id,
        parent_label=parent_label,
        synonyms=synonyms,
        is_obsolete=any(v.lower() == "true" for v in fields.get("is_obsolete", [])),
        replaced_by=fields.get("replaced_by", [""])[0],
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
    """Rebuild the retrieval index from a pinned release's OBO document."""
    source = Path(obo_path)
    terms = parse_obo(_read_source_text(source))
    if not terms:
        raise ObOFormatError(f"{source} contains no [Term] stanzas")
    return OntologyIndex(ontology_release=ontology_release, terms=tuple(terms))


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
    return OntologyIndex(ontology_release=release, terms=terms)


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
