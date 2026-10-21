#!/usr/bin/env python3
"""UK Biobank Showcase resolution for ukb-b trait labels (issue #185).

The choice stage of the curation pipeline hands Jev a raw ukb-b ``trait_label``
and asks it to map the label to an EFO term. The label is the whole queue row,
so a coded field's label -- ``diagnoses - main icd10: c20 malignant neoplasm of
rectum`` -- mixes a field title, a code, and the code's description. This
module parses that structure and resolves the title against the UK Biobank
Showcase schema (the same data fields ukb-b was harvested from), so the next
pipeline pass can retrieve on exactly the text that denotes the phenotype
(``retrieval_text``) and, for question fields, can give the chooser the
question the field actually asks (``trait_context``).

The Showcase schema is an *untracked input*: this module reads ``schema-1.tsv``
(fields), ``schema-3.tsv`` (category titles), and ``schema-13.tsv`` (the
category tree) from a directory. It never bakes their contents in, so the
repository stays free of the ~4 MiB real schema.

Covered label families
----------------------
``diagnoses - main icd10`` / ``diagnoses - secondary icd10``
    ICD-10 coded diagnoses; the label is ``<code> <description>``.
``operative procedures - main opcs`` / ``operative procedures - secondary opcs``
    OPCS-4 coded procedures; same ``<code> <description>`` shape, and the
    title alias ``opcs`` -> ``opcs4`` resolves against the Showcase title.
``treatment/medication code``, ``non-cancer illness code, self-reported``,
``cancer code, self-reported``, ``operation code``
    Coded interview fields; the label value *is* the code description.
``type of cancer: icd10``
    ICD-10 coded cancer registry field; one title with a colon in it.
``<title>: <value>`` (general)
    Any other ukb-b label; split on the first colon.

Coded versus not
----------------
A field is *coded* when the label's value is one entry of the field's encoding
(the code, or the code's description) and the notes are boilerplate. A
question field is *not* coded: the label is the question plus the participant's
answer, and the field's notes *are* the question text. Classification uses the
Showcase ``value_type`` (UK Biobank types 21/22 are the categorical coded
types, in contrast to the 11/31 continuous types) and the family name for a
label a schema field did not resolve.

For a coded field ``retrieval_text`` is the value text with the code dropped
(``c20 malignant neoplasm of rectum`` -> ``malignant neoplasm of rectum``) and
``trait_context`` is deliberately empty: the notes of a coded field are
boilerplate describing the *field*, not the trait, so carrying them would only
dilute the chooser's evidence. For a non-coded field ``retrieval_text`` is the
label itself and ``trait_context`` carries the question text, category path,
and units. Whether the context should reach Jev at all is evaluated in the
next pipeline pass; this module only makes the primitives importable.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, replace
from html import unescape
from pathlib import Path
from typing import Iterable, Mapping, Sequence

# ---------------------------------------------------------------------------
# Parsed labels
# ---------------------------------------------------------------------------

# Constants for the known ukb-b label families (issue #185).
FAMILY_DIAGNOSES_MAIN_ICD10: str = "diagnoses_main_icd10"
FAMILY_DIAGNOSES_SECONDARY_ICD10: str = "diagnoses_secondary_icd10"
FAMILY_PROCEDURES_MAIN_OPCS: str = "operative_procedures_main_opcs"
FAMILY_PROCEDURES_SECONDARY_OPCS: str = "operative_procedures_secondary_opcs"
FAMILY_TREATMENT_MEDICATION: str = "treatment_medication_code"
FAMILY_NON_CANCER_ILLNESS: str = "non_cancer_illness_code_self_reported"
FAMILY_CANCER_CODE_SELF_REPORTED: str = "cancer_code_self_reported"
FAMILY_OPERATION_CODE: str = "operation_code"
FAMILY_TYPE_OF_CANCER: str = "type_of_cancer"
# Any label that is not one of the known families (or a bare free-text label).
FAMILY_GENERAL: str = "general"

# The families whose label value starts with a code token and a description.
# (title prefix on the label, family, Showcase schema title)
_KNOWN_FAMILIES: tuple[tuple[str, str, str], ...] = (
    (
        "diagnoses - main icd10:",
        FAMILY_DIAGNOSES_MAIN_ICD10,
        "Diagnoses - main ICD10",
    ),
    (
        "diagnoses - secondary icd10:",
        FAMILY_DIAGNOSES_SECONDARY_ICD10,
        "Diagnoses - secondary ICD10",
    ),
    (
        "operative procedures - main opcs:",
        FAMILY_PROCEDURES_MAIN_OPCS,
        "Operative procedures - main OPCS4",
    ),
    (
        "operative procedures - secondary opcs:",
        FAMILY_PROCEDURES_SECONDARY_OPCS,
        "Operative procedures - secondary OPCS4",
    ),
    (
        "treatment/medication code:",
        FAMILY_TREATMENT_MEDICATION,
        "Treatment/medication code",
    ),
    (
        "non-cancer illness code, self-reported:",
        FAMILY_NON_CANCER_ILLNESS,
        "Non-cancer illness code, self-reported",
    ),
    (
        "cancer code, self-reported:",
        FAMILY_CANCER_CODE_SELF_REPORTED,
        "Cancer code, self-reported",
    ),
    ("operation code:", FAMILY_OPERATION_CODE, "Operation code"),
    (
        "type of cancer: icd10:",
        FAMILY_TYPE_OF_CANCER,
        "Type of cancer: ICD10",
    ),
)

# The code families; a label in one of these is coded even when the Showcase
# field itself did not resolve.
_CODED_FAMILIES: frozenset[str] = frozenset(
    family for _, family, _ in _KNOWN_FAMILIES
)

# ICD-10 / OPCS-4 code shapes: a letter, one or two digits, and an optional
# dotted decimal (``c20``, ``b34.9``, ``c18.7``, ``a55.9``).
_CODE_RE = re.compile(r"^[A-Za-z]\d{1,2}(\.\d{1,3})?$")

# The OPCS title alias: ukb-b labels say ``opcs`` where the Showcase title says
# ``OPCS4``.
_OPCS_WORD_RE = re.compile(r"\bopcs\b", re.IGNORECASE)
# ukb-b appends ``(recoded)`` to a field title to tell the recoded instance
# apart; the base question title is the lookup key.
_RECODED_RE = re.compile(r"\(recoded\)", re.IGNORECASE)


@dataclass(frozen=True)
class UkbLabel:
    """One parsed ukb-b trait label.

    ``title`` is the Showcase lookup key for the field's title (lowercased,
    ``opcs`` aliased to ``opcs4``, ``(recoded)`` stripped). ``code`` is the
    leading code token a coded family's value carries (``""`` when the family
    has no code); ``code_system`` is ``icd10``, ``opcs4``, or ``""``. ``value``
    is the text after the title and, for a code family, after the leading code
    token.
    """

    raw: str
    family: str
    title: str
    code: str
    code_system: str
    value: str


def _title_lookup_key(title: str) -> str:
    """The Showcase title lookup key for a label or schema title.

    Matching is on the lowercased, whitespace-normalised title with the
    ``opcs`` -> ``opcs4`` alias and ``(recoded)`` removed, so the same key is
    produced on both sides of the lookup.
    """
    text = _RECODED_RE.sub(" ", title)
    text = _OPCS_WORD_RE.sub("opcs4", text)
    return " ".join(text.lower().split())


def _split_code(text: str) -> tuple[str, str]:
    """Split ``'<code> <description>'`` into ``(code, description)``.

    The first whitespace-delimited token is treated as the code only when it
    has the ICD-10/OPCS-4 shape; otherwise the text is not a code prefix.
    """
    parts = text.split(None, 1)
    if not parts or not _CODE_RE.match(parts[0]):
        return "", text
    head = parts[0]
    rest = parts[1].strip() if len(parts) > 1 else ""
    return head, rest


def parse_trait_label(label: str | None) -> UkbLabel:
    """Parse a ukb-b trait label into its title, code, and value.

    Known families are recognised by their exact title prefix; anything else
    falls back to the general ``title: value`` split (or a bare value when the
    label has no colon).
    """
    raw = (label or "").strip()
    if not raw:
        return UkbLabel("", FAMILY_GENERAL, "", "", "", "")
    lowered = raw.lower()
    for prefix, family, schema_title in _KNOWN_FAMILIES:
        if not lowered.startswith(prefix):
            continue
        rest = raw[len(prefix):].strip()
        key = _title_lookup_key(schema_title)
        if family in (
            FAMILY_DIAGNOSES_MAIN_ICD10,
            FAMILY_DIAGNOSES_SECONDARY_ICD10,
            FAMILY_TYPE_OF_CANCER,
        ):
            code, value = _split_code(rest)
            if code:
                return UkbLabel(raw, family, key, code, "icd10", value)
            return UkbLabel(raw, family, key, "", "", rest)
        if family in (
            FAMILY_PROCEDURES_MAIN_OPCS,
            FAMILY_PROCEDURES_SECONDARY_OPCS,
        ):
            code, value = _split_code(rest)
            if code:
                return UkbLabel(raw, family, key, code, "opcs4", value)
            return UkbLabel(raw, family, key, "", "", rest)
        return UkbLabel(raw, family, key, "", "", rest)

    title, separator, value = raw.partition(":")
    if not separator:
        return UkbLabel(raw, FAMILY_GENERAL, "", "", "", raw)
    return UkbLabel(
        raw, FAMILY_GENERAL, _title_lookup_key(title), "", "", value.strip()
    )


def icd10_code_from_label(label: str | None) -> str:
    """The leading ICD-10 code of a label, or ``""`` when it carries none.

    Only the ICD-10-bearing ukb-b families are scanned, never arbitrary tokens
    that merely look like codes (``b12`` must not fire the retrieval channel).
    The code is returned as written; normalise with
    :func:`~curation.ontology.normalise_icd10_code` for a lookup key.
    """
    parsed = parse_trait_label(label)
    if parsed.code_system == "icd10":
        return parsed.code
    return ""


# ---------------------------------------------------------------------------
# The Showcase schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShowcaseField:
    """One Showcase data field as this resolver needs it."""

    field_id: str
    title: str
    value_type: str
    encoding_id: str
    units: str
    main_category: str
    notes: str
    category_path: tuple[str, ...]


def _read_schema_text(path: Path) -> str:
    """Read a schema TSV, tolerating the Showcase download's encoding.

    The real schema-3 carries Windows-1252 punctuation (em dashes) inside
    category descriptions; UTF-8 is tried first and cp1252 is the fallback so
    the untracked input is read as-is rather than rejected.
    """
    if not path.is_file():
        raise ValueError(f"Showcase schema TSV does not exist: {path}")
    data = path.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252")


def _schema_columns(path: Path, names: Sequence[str]) -> set[str]:
    """Return the header columns of a schema TSV, requiring ``names``."""
    first_line = _read_schema_text(path).split("\n", 1)[0]
    columns = set(first_line.split("\t"))
    for name in names:
        if name not in columns:
            raise ValueError(f"{path} has no {name!r} column; unexpected schema TSV")
    return columns


def _read_schema_fields(path: Path) -> list[ShowcaseField]:
    """Read schema-1.tsv (the Showcase data fields) into field objects."""
    _schema_columns(
        path, ("field_id", "title", "value_type", "main_category", "encoding_id")
    )
    fields: list[ShowcaseField] = []
    for row in csv.DictReader(io.StringIO(_read_schema_text(path)), delimiter="\t"):
        fields.append(
            ShowcaseField(
                field_id=(row.get("field_id") or "").strip(),
                title=(row.get("title") or "").strip(),
                value_type=(row.get("value_type") or "").strip(),
                encoding_id=(row.get("encoding_id") or "").strip(),
                units=(row.get("units") or "").strip(),
                main_category=(row.get("main_category") or "").strip(),
                notes=(row.get("notes") or "").strip(),
                category_path=(),
            )
        )
    return fields


def _read_category_titles(path: Path) -> dict[str, str]:
    """Read schema-3.tsv (category id -> title)."""
    _schema_columns(path, ("category_id",))
    categories: dict[str, str] = {}
    for row in csv.DictReader(io.StringIO(_read_schema_text(path)), delimiter="\t"):
        category_id = (row.get("category_id") or "").strip()
        if category_id:
            categories[category_id] = (row.get("title") or "").strip()
    return categories


def _read_category_tree(path: Path) -> dict[str, str]:
    """Read schema-13.tsv (category tree) into {child_id: parent_id}.

    A child is listed under each parent; the first parent wins so the walk is
    deterministic.
    """
    _schema_columns(path, ("parent_id", "child_id"))
    parent_of: dict[str, str] = {}
    for row in csv.DictReader(io.StringIO(_read_schema_text(path)), delimiter="\t"):
        parent = (row.get("parent_id") or "").strip()
        child = (row.get("child_id") or "").strip()
        if parent and child:
            parent_of.setdefault(child, parent)
    return parent_of


def _category_path(
    main_category: str,
    categories: Mapping[str, str],
    parent_of: Mapping[str, str],
) -> tuple[str, ...]:
    """The root-first chain of category titles containing ``main_category``.

    A category that is absent from schema-3 or whose chain leaves the tree
    contributes the categories it does resolve, rather than failing the field.
    """
    if not main_category or main_category not in categories:
        return ()
    chain: list[str] = []
    seen: set[str] = set()
    current = main_category
    while current and current not in seen and len(seen) < 64:
        seen.add(current)
        chain.append(current)
        current = parent_of.get(current, "")
    resolved: list[str] = []
    for category_id in reversed(chain):
        title = categories.get(category_id, "")
        if title:
            resolved.append(title)
    return tuple(resolved)


# ---------------------------------------------------------------------------
# The resolver and its result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UkbField:
    """A trait label resolved against the Showcase schema.

    ``field_id``/``schema_title``/``encoding_id``/``value_type``/``units`` and
    ``category_path`` are empty when no Showcase field matches the label's
    title. ``retrieval_text`` is the text the next pipeline pass should
    retrieve and choose on; ``trait_context`` carries the field's question
    text, category path, and units for a non-coded field (see the module
    docstring for the coded-field decision).
    """

    label: UkbLabel
    field_id: str
    schema_title: str
    encoding_id: str
    value_type: str
    units: str
    category_path: tuple[str, ...]
    is_coded: bool
    retrieval_text: str
    trait_context: str


# UK Biobank's categorical (coded) value types. The code-bearing fields use 22
# (categorical single) or 21 (categorical multiple); the continuous types the
# pipeline must treat as questions are 11 and 31.
_CODED_VALUE_TYPES: frozenset[str] = frozenset({"21", "22"})


def _classify_coded(parsed: UkbLabel, field: ShowcaseField | None) -> bool:
    """Whether a parsed label denotes a coded field's value.

    A known code family is always coded. A general label is coded when its
    field is one of the categorical value types; a label a field did not
    resolve to is treated as a question (the label is kept whole).
    """
    if parsed.family in _CODED_FAMILIES:
        return True
    if field is not None:
        return field.value_type in _CODED_VALUE_TYPES
    return False


# Showcase notes reference sibling fields as ``~F1234~``; the marker is
# provenance about another field, not part of the question's prose.
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_FIELD_REFERENCE_RE = re.compile(r"~F\d+~")


def _plain_text(value: str | None) -> str:
    """Strip light HTML and Showcase field references from a notes cell.

    The real schema's question notes carry ``<p>``/``<i>``/``<ul><li>`` tags,
    ``&nbsp;`` and friends, and ``~F1234~`` references to sibling fields. The
    chooser context must be clean prose, so tags and field references are
    dropped (a field reference is not part of the question), the common HTML
    entities are unescaped, and whitespace is collapsed.
    """
    text = unescape(value or "")
    text = _HTML_TAG_RE.sub(" ", text)
    text = _FIELD_REFERENCE_RE.sub(" ", text)
    return " ".join(text.split())


def _compose_trait_context(field: ShowcaseField | None) -> str:
    """The question text plus field metadata for a non-coded field.

    Empty when no field resolved. The ``[field ...; category: ...; units: ...]``
    tail makes the provenance of the context explicit without the boilerplate
    of a coded field's notes.
    """
    question = _plain_text(field.notes) if field is not None else ""
    details: list[str] = []
    if field is not None:
        if field.field_id and field.title:
            details.append(f"field {field.field_id} '{field.title}'")
        if field.category_path:
            details.append("category: " + " / ".join(field.category_path))
        if field.units:
            details.append(f"units: {field.units}")
    if not details:
        return question
    tail = "; ".join(details)
    return f"{question} [{tail}]" if question else f"[{tail}]"


class ShowcaseResolver:
    """Resolves ukb-b trait labels against a Showcase schema directory.

    Build the resolver from a directory holding ``schema-1.tsv``,
    ``schema-3.tsv``, and ``schema-13.tsv`` (see :meth:`from_directory`), then
    map each label to its :class:`UkbField`.
    """

    def __init__(self, fields: Iterable[ShowcaseField]) -> None:
        # Title key -> field; the first field wins so a title collision (a
        # base title vs a ``(recoded)`` title) resolves deterministically.
        self._by_title_key: dict[str, ShowcaseField] = {}
        for field in fields:
            key = _title_lookup_key(field.title)
            if key and key not in self._by_title_key:
                self._by_title_key[key] = field

    @classmethod
    def from_directory(cls, directory: Path | str) -> "ShowcaseResolver":
        """Build a resolver from a directory of Showcase schema TSVs."""
        root = Path(directory)
        categories = _read_category_titles(root / "schema-3.tsv")
        parent_of = _read_category_tree(root / "schema-13.tsv")
        fields = [
            replace(field, category_path=_category_path(
                field.main_category, categories, parent_of
            ))
            for field in _read_schema_fields(root / "schema-1.tsv")
        ]
        return cls(fields)

    def __len__(self) -> int:
        return len(self._by_title_key)

    def resolve(self, trait_label: str | None) -> UkbField | None:
        """Resolve a trait label to its Showcase field and context.

        ``None`` is returned only for a blank label; a label whose title no
        field matches still yields a :class:`UkbField` with the label itself as
        ``retrieval_text`` and no field context, so a downstream pass never
        has to special-case an unresolved title. A label with no colon is
        matched as a bare title when its lowercased form equals a schema
        title (continuous measurement labels carry no ``: value``).
        """
        parsed = parse_trait_label(trait_label)
        if not parsed.raw:
            return None
        field = self._by_title_key.get(parsed.title) if parsed.title else None
        if field is None and not parsed.title:
            # A bare label (``waist circumference``, no colon) is a title-only
            # match: ukb-b renders some measurement fields without a value.
            field = self._by_title_key.get(_title_lookup_key(parsed.raw))
        is_coded = _classify_coded(parsed, field)
        retrieval_text = parsed.value if is_coded and parsed.value else parsed.raw
        trait_context = "" if is_coded else _compose_trait_context(field)
        if field is None:
            return UkbField(
                label=parsed,
                field_id="",
                schema_title="",
                encoding_id="",
                value_type="",
                units="",
                category_path=(),
                is_coded=is_coded,
                retrieval_text=retrieval_text,
                trait_context=trait_context,
            )
        return UkbField(
            label=parsed,
            field_id=field.field_id,
            schema_title=field.title,
            encoding_id=field.encoding_id,
            value_type=field.value_type,
            units=field.units,
            category_path=field.category_path,
            is_coded=is_coded,
            retrieval_text=retrieval_text,
            trait_context=trait_context,
        )