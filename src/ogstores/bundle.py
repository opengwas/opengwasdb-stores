"""Load and validate Release Bundles without inspecting Store artifacts.

``check()`` owns registry-side structure only: bundle identity and provenance,
declared bundle files, checksum syntax, Release Status, lineage, the shared
``analyses.tsv`` contract, and a mapped bundle's Column Map. It accumulates
errors and never raises, so CI and a Manifest Generator can report every defect
from one pass. It does not resolve source files, construct artifact paths, or
open a Store.

See docs/spec/store-release-workflow.md and ADRs 0017, 0022, 0023, and 0028.
"""

from __future__ import annotations

import csv
import functools
import hashlib
import re
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

import yaml

from ogstores.manifest import EXCLUDE_COLUMN, classify_exclusion
from ogstores.paths import is_valid_store_id

# `opengwasdb.model` is imported inside the functions that read analyses.tsv,
# not here. Every process that imports `ogstores` imports this module: the
# release entry point, each Snakemake parse of the release workflow, the
# parent-death supervisor. Importing opengwasdb's model costs each of them
# about 1.5s, and the release path never reads an analyses table.
if TYPE_CHECKING:
    from opengwasdb.model import analyses as opengwasdb_analyses

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

VALID_STATUSES: frozenset[str] = frozenset({
    "candidate",
    "accepted",
    "built",
    "validated",
    "superseded",
    "withdrawn",
})

LEGAL_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "candidate": frozenset({"candidate", "accepted", "withdrawn", "superseded"}),
    "accepted": frozenset({"accepted", "built", "withdrawn", "superseded"}),
    "built": frozenset({"built", "validated", "superseded", "withdrawn"}),
    "validated": frozenset({"validated", "superseded", "withdrawn"}),
    "superseded": frozenset({"superseded", "withdrawn"}),
    "withdrawn": frozenset({"withdrawn"}),
}

# The Release Statuses the release workflow may build (#247). `accepted` is the
# human-frozen input the build rests on; `built` and `validated` keep a re-run,
# or a `--config force=1` replacement, working. A `candidate` has not been
# accepted, and `superseded` and `withdrawn` are retired. This set is the one
# statement of the rule: callers ask the functions below.
BUILDABLE_STATUSES: frozenset[str] = frozenset({"accepted", "built", "validated"})

# Release-level identity and provenance required by the current bundle contract.
# The identity file trims to identity, lineage, status, creation time, source
# snapshot identity, prose, and the generation command log (issue #136).
# Coverage, cadence, Source Collection, source defaults, the sidecar pointer
# map, and the standalone build_environment block were removed because they are
# derived elsewhere or read by no code.
RELEASE_REQUIRED_KEYS: tuple[str, ...] = (
    "store_id",
    "label",
    "status",
    "source_snapshot_id",
    "created_at",
    "description",
    "generator",
)

BUILD_REQUIRED_KEYS: tuple[str, ...] = (
    "store_id",
    "layout",
    "completion_state",
    "post",
)

# The Build Recipe blocks that produce a Store, `build` for an observed-only
# release and `complete` for a Reference-Completed one. Each is also the name of
# the execution Step it plans (`ogstores.run`).
STORE_PRODUCING_STEPS: frozenset[str] = frozenset({"build", "complete"})

# Optional named Indexed Variant Subset declarations (stores #209, epic #214).
# A Build Recipe may declare one or more; `plan()` turns each into a
# `build-indexed-subset` Step. The declaration names a maintained variant-list
# Reference Resource by `resource_id` and records the resolved `variant_list`
# path and `reference_assembly` it uses, so the immutable bundle is
# self-contained and `plan()` needs no resource I/O. Ragged Store Releases have
# no Dense Component to index, so they reject any declaration rather than
# silently dropping it (CONTRIBUTING's fail-loud rule; epic #214 non-goal).
INDEXED_SUBSETS_KEY: str = "indexed_subsets"
INDEXED_SUBSET_STEP_PREFIX: str = "indexed-subset-"
# The subset-name grammar is opengwasdb's own (`_NAME_RE`,
# `opengwas/layouts/dense/indexed_subsets.py`), so a name the planner accepts is
# one the consumer accepts. The registry is additionally stricter in rejecting
# `..`, because the name becomes part of a record filename.
INDEXED_SUBSET_NAME_RE: re.Pattern[str] = re.compile(
    r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z"
)
INDEXED_SUBSET_REQUIRED_KEYS: tuple[str, ...] = (
    "name",
    "resource_id",
    "variant_list",
    "reference_assembly",
)
INDEXED_SUBSET_OPTIONAL_KEYS: tuple[str, ...] = ("checksum", "options")
# Dense and Hybrid releases have a self-contained Dense Component; Ragged does
# not (ADR 0061 in the sibling `opengwasdb` repository).
INDEXED_SUBSET_LAYOUTS: frozenset[str] = frozenset({"dense", "hybrid"})
# An `options` key that re-specifies an input the runtime verifies would let the
# executed CLI consume a different list or assembly than the one hashed and
# checked -- a silent integrity failure. Keys are normalised exactly as
# `render_options` renders them (`str(key).strip().lstrip("-")`).
INDEXED_SUBSET_RESERVED_OPTION_KEYS: frozenset[str] = frozenset(
    {"variant-list", "reference-assembly"}
)


def normalize_option_key(key: object) -> str:
    """The flag name `render_options` derives from an `options` key (ADR 0023)."""
    return str(key).strip().lstrip("-")


def reserved_options(options: Mapping[Any, Any], reserved: Iterable[str]) -> list[str]:
    """The `options` keys, as written, that render as one of the `reserved` flags.

    A reserved flag is one the registry composes from a verified input of its
    own; passed through `options`, it would reach the CLI unverified.
    """
    flags = frozenset(reserved)
    return sorted(str(key) for key in options if normalize_option_key(key) in flags)


def unrenderable_options(options: Mapping[Any, Any]) -> list[str]:
    """The `options` keys, as written, whose value cannot render as CLI tokens.

    `plan.render_options` (ADR 0023) turns a bool into a bare flag, a list into
    one flag occurrence per item and anything else into `str(value)`. A mapping
    would arrive as its own repr, and `null` or an empty list as nothing at all
    -- an option the declaration states and the command never sees. Both are the
    silently-dropped-instruction class the registry refuses, so a value must be
    a string, number or bool, or a non-empty list of those.
    """

    def renders(value: object) -> bool:
        scalars = (str, int, float, bool)
        if isinstance(value, scalars):
            return True
        return (
            isinstance(value, (list, tuple))
            and bool(value)
            and all(isinstance(item, scalars) for item in value)
        )

    return sorted(str(key) for key, value in options.items() if not renders(value))


def is_valid_indexed_subset_name(name: object) -> bool:
    """Return True if `name` is a filesystem- and consumer-safe subset name."""
    return (
        isinstance(name, str)
        and ".." not in name
        and INDEXED_SUBSET_NAME_RE.fullmatch(name) is not None
    )


def indexed_subset_step_name(name: str) -> str:
    """The execution Step name carrying one declared subset's identity."""
    return f"{INDEXED_SUBSET_STEP_PREFIX}{name}"


# Legacy Trial Store Releases where required Analysis metadata is unpopulated
# in upstream BESD sources and cannot be resolved without external study metadata
# (issue #134). Blank required values are tolerated strictly for these two named
# releases; any new release (including any future ragged-BESD or completed release)
# must provide valid required values or fail check().
LEGACY_BLANK_ANALYSIS_RELEASES: frozenset[str] = frozenset({"OGS-00001", "OGS-00002"})

# The controlled vocabulary for ``assigned_ancestry``: the seven super-population
# codes the ancestry-mixture Reference Resource assigns (see
# resources/reference-resources/ukb-ancestry-mixture-hg38/resource.yaml and
# docs/release-metadata-schema.md). Free-text Source Ancestry Labels such as
# "European" are provenance, not Assigned Ancestry, and are rejected here so the
# two vocabularies cannot silently read as the same fact (issue #133).
SUPERPOPULATIONS: tuple[str, ...] = (
    "AFR",
    "AMR",
    "EAS",
    "EUR",
    "MID",
    "NAF",
    "SAS",
)

# ``trait_ontology_id`` is a controlled-vocabulary term for a Trait, never a
# gene or protein identifier. Issue #130 wrote an authority-qualified Ensembl
# gene id (``ENSEMBL:ENSG...``) into it and named the authority in
# ``trait_ontology_label``, silently asserting that a gene *is* the trait
# (issue #141). ``RETIRED_ANALYSIS_COLUMNS`` cannot catch this: the column is
# legitimate and only its vocabulary was wrong. This rejects the identifier
# authorities a gene or protein target would carry -- Ensembl (bare or
# authority-qualified), HGNC, Entrez/NCBI Gene, and UniProt -- so the class
# cannot recur. A gene symbol in ``analysis_label`` and coordinates in
# ``trait_chr``/``trait_bp``/the target sidecar remain the gene's real home.
_GENE_SHAPED_TRAIT_ONTOLOGY_ID: re.Pattern[str] = re.compile(
    r"\A(?:ENSEMBL:|ENS[A-Z]*[GT]\d+(?:\.\d+)?|HGNC:|ENTREZ:|ENTREZGENE:|"
    r"NCBIGENE:|UNIPROT:)",
    re.IGNORECASE,
)

# Authority names that describe the identifier's vocabulary rather than the
# Trait it identifies. #130 stored ``Ensembl`` here; a trait label belongs in
# the field instead (issue #141).
_TRAIT_ONTOLOGY_AUTHORITY_LABELS: frozenset[str] = frozenset(
    {"ensembl", "hgnc", "entrez", "entrezgene", "entrez gene", "ncbigene", "uniprot"}
)

# A ``trait_ontology_id`` holds one CURIE, or several joined by exactly ", " in
# the source's order when the source maps the Trait to more than one term
# (GWAS Catalog's ``MAPPED_TRAIT_URI``, #286). Each term is a CURIE --
# ``EFO:0004839``, ``OBA:VT0001253``, ``Orphanet:101960`` -- never a URI.
TRAIT_ONTOLOGY_ID_SEPARATOR: str = ", "
_TRAIT_ONTOLOGY_CURIE: re.Pattern[str] = re.compile(
    r"[A-Za-z][A-Za-z0-9._-]*:[A-Za-z0-9][A-Za-z0-9._-]*"
)

# Accepted Store Releases emitted before #286 with ``trait_ontology_id`` values
# that are not CURIEs: OGS-00011 carries GWAS Catalog's raw EFO/ORDO URIs and
# half-converted lists. An accepted bundle is never rewritten, so its values
# are tolerated by name and disclosed on check()'s second channel (#142); every
# other bundle is held to the CURIE form.
LEGACY_NON_CURIE_TRAIT_ONTOLOGY_RELEASES: frozenset[str] = frozenset({"OGS-00011"})


def trait_ontology_id_terms(value: str) -> list[str]:
    """The terms of a ``trait_ontology_id`` value, in order; empty for a blank value.

    The one split rule (#286): split on every comma and strip each part, so a
    malformed list (``A,B``, padding, an empty term) still yields its terms for
    the guards. Whether the value is in canonical form is a separate question:
    :func:`is_canonical_trait_ontology_id`.
    """
    return [part.strip() for part in value.split(",")] if value else []


def is_trait_ontology_curie(term: str) -> bool:
    """Whether one ``trait_ontology_id`` term is a CURIE."""
    return _TRAIT_ONTOLOGY_CURIE.fullmatch(term) is not None


def is_canonical_trait_ontology_id(value: str) -> bool:
    """Whether ``value`` is blank, one CURIE, or CURIEs joined by exactly ``", "``."""
    terms = trait_ontology_id_terms(value)
    return all(map(is_trait_ontology_curie, terms)) and (
        TRAIT_ONTOLOGY_ID_SEPARATOR.join(terms) == value
    )


def is_gene_shaped_trait_ontology_id(value: str) -> bool:
    """Whether any term of ``value`` is a gene or protein identifier (#141).

    Every term is checked, so a gene identifier cannot hide behind an ontology
    term in a list (#286).
    """
    return any(_GENE_SHAPED_TRAIT_ONTOLOGY_ID.match(term) for term in trait_ontology_id_terms(value))


def is_authority_name_trait_ontology_label(label: str) -> bool:
    """Whether ``label`` names an identifier authority rather than a Trait (#141).

    The label is checked whole, as #141 defines it, and never split: a term's
    own label may contain a comma (``osteoarthritis, hip``).
    """
    return label.strip().lower() in _TRAIT_ONTOLOGY_AUTHORITY_LABELS

# Store-level descriptive metadata that is derived from the membership table.
# Names intentionally match their source columns: in particular, ``context``
# is not renamed to ``assay`` because the Analysis contract has no assay field.
#: ``sidecars/quality_checks.tsv`` (#285, ADR 0036): every Analysis's Quality
#: Check results. Optional: a bundle emitted before #285 has none. Present, it
#: is validated by :func:`_check_quality_checks`. Store Release Build never
#: reads it; membership is already decided in ``analyses.tsv``.
QUALITY_CHECKS_SIDECAR: str = "sidecars/quality_checks.tsv"
#: Its leading columns; each check then adds ``<check>`` and ``<check>_measured``.
QUALITY_CHECK_LEADING_COLUMNS: tuple[str, ...] = (
    "analysis_id",
    "exclude_from_build",
    "exclusion_reason",
)
QUALITY_CHECK_MEASURED_SUFFIX: str = "_measured"
#: The controlled ``<check>`` result vocabulary.
QUALITY_CHECK_RESULTS: tuple[str, ...] = (
    "passed",
    "failed",
    "not_applicable",
    "unmeasured",
    "invalid",
    # The measurement fails the test, but a declared release policy keeps the
    # Analysis and records the finding (#287: Assigned Ancestry trusted from the
    # source for an Analysis with no allele frequencies).
    "warning",
)
#: The exclusions sidecar, whose ``reason`` the Quality Check sidecar must agree with.
EXCLUSIONS_SIDECAR: str = "sidecars/exclusions.tsv"
#: How many row-level errors one sidecar reports before summarising the rest.
_MAX_SIDECAR_ROW_ERRORS: int = 10

#: The Source Reader Capability that reads a tabular collection through a Column
#: Map (#292, ADR 0036; opengwasdb ADR 0063). A bundle with an Analysis read
#: through it binds its map in the Build Recipe and carries it; see
#: :func:`_check_column_map`.
COLUMN_MAP_CAPABILITY: str = "opengwasdb.column-map"
#: The Build Recipe key binding the map; see :class:`ColumnMapBinding`.
COLUMN_MAP_KEY: str = "column_map"
COLUMN_MAP_BINDING_KEYS: tuple[str, ...] = ("name", "sha256", "file")
#: Where a bundle carries the map it binds: ``column_maps/<file>``.
COLUMN_MAPS_DIR: str = "column_maps"
COLUMN_MAP_SUFFIX: str = ".toml"
#: Lower-case, as ``hashlib`` writes it, so the binding compares byte for byte
#: with the Resolution Record's fingerprint.
_SHA256_RE: re.Pattern[str] = re.compile(r"\A[0-9a-f]{64}\Z")
#: The flag Store Release Build will compose from the binding; ``options`` may not pass it.
_COLUMN_MAP_OPTIONS: frozenset[str] = frozenset({"column-map"})
_READER_OPTION: str = "source-reader-capability"

SUMMARY_COLUMNS: tuple[str, ...] = (
    "first_author",
    "publication_pmid",
    "tissue",
    "context",
    "assigned_ancestry",
    "sample_size",
    "source_url",
)

_IDENTIFIER_SUMMARY_COLUMNS: tuple[str, ...] = (
    "first_author",
    "publication_pmid",
    "tissue",
    "context",
    "assigned_ancestry",
)

_HEX_PATTERNS: dict[int, re.Pattern[str]] = {
    length: re.compile(rf"\A[0-9a-fA-F]{{{length}}}\Z")
    for length in (32, 40, 64)
}


def _is_valid_hex(value: object, length: int) -> bool:
    pattern = _HEX_PATTERNS.get(length) or re.compile(
        rf"\A[0-9a-fA-F]{{{length}}}\Z"
    )
    return isinstance(value, str) and pattern.fullmatch(value.strip()) is not None


def is_legal_status_transition(from_status: str, to_status: str) -> bool:
    """Return whether a Release Status transition is in the lifecycle graph."""
    return to_status in LEGAL_STATUS_TRANSITIONS.get(from_status, ())


def validate_status_transition(from_status: str, to_status: str) -> list[str]:
    """Return all vocabulary/transition errors for one status change."""
    errors: list[str] = []
    if from_status not in VALID_STATUSES:
        errors.append(
            f"invalid current status {from_status!r}; expected one of "
            f"{sorted(VALID_STATUSES)}"
        )
    if to_status not in VALID_STATUSES:
        errors.append(
            f"invalid target status {to_status!r}; expected one of "
            f"{sorted(VALID_STATUSES)}"
        )
    if not errors and not is_legal_status_transition(from_status, to_status):
        errors.append(
            f"illegal status transition from {from_status!r} to {to_status!r}; "
            f"allowed transitions from {from_status!r}: "
            f"{sorted(LEGAL_STATUS_TRANSITIONS[from_status])}"
        )
    return errors


@dataclass(frozen=True)
class Bundle:
    store_id: str
    root: Path
    release: dict[str, Any]
    build: dict[str, Any]
    analyses_path: Path
    validation: dict[str, Any] | None = None

    @property
    def status(self) -> str | None:
        # A release.yaml that is not a mapping has no status; check() reports it.
        return self.release.get("status") if isinstance(self.release, Mapping) else None

    @property
    def label(self) -> str | None:
        return self.release.get("label")

    @property
    def layout(self) -> str | None:
        return self.build.get("layout")

    @property
    def completion_state(self) -> str | None:
        return self.build.get("completion_state")

    @property
    def derived_from(self) -> str | None:
        return self.release.get("derived_from")

    @property
    def completes_own_store(self) -> bool:
        """Whether this release completes its own published Store under its id (ADR 0037).

        A `reference_completed` release with no `derived_from` is published
        twice: its build phase publishes the Observed-Only Store, and its
        completion phase replaces it. With `derived_from` it is a two-id child
        of that parent (ADR 0007), as before.
        """
        return self.completion_state == "reference_completed" and not self.derived_from

    @property
    def validation_path(self) -> Path | None:
        path = self.root / "validation.yaml"
        return path if path.is_file() else None


class NotBuildableError(ValueError):
    """The release workflow was asked to build a bundle its Release Status does not allow (#247)."""


def is_completion_pending(bundle: Bundle, published: str | None) -> bool:
    """Whether `published` is the Observed-Only Store this release's completion has not replaced yet.

    One predicate for every reader of the published completion state: the
    master list marks such a row "completion pending" and readiness reports it
    as the declared order of business (issue #307). Only a release that
    completes its own Store publishes an Observed-Only Store under the id it
    will complete; a two-id child publishes only its completed Store, so for it
    the same pair is a contradiction and not a pending completion (ADR 0007,
    ADR 0037).
    """
    return bundle.completes_own_store and published == "observed_only"


def is_buildable(bundle: Bundle) -> bool:
    """Return whether the release workflow may build this bundle (`BUILDABLE_STATUSES`)."""
    return bundle.status in BUILDABLE_STATUSES


def build_refusal(bundle: Bundle) -> str | None:
    """Why the release workflow must not build this bundle, or None when it may."""
    if is_buildable(bundle):
        return None
    status = bundle.status
    if status not in VALID_STATUSES:
        return (
            f"{bundle.store_id} has no valid Release Status ({status!r}); "
            "`pixi run bundle-check` names the problem."
        )
    if "accepted" in LEGAL_STATUS_TRANSITIONS[status]:
        return (
            f"{bundle.store_id} has Release Status {status!r}. To accept it, review it, "
            f"then set `status: accepted` and `accepted_at` in {bundle.root / 'release.yaml'}, "
            "check it with `pixi run bundle-check`, and commit the change."
        )
    return (
        f"{bundle.store_id} has Release Status {status!r}, which never returns to 'accepted'; "
        "a replacement is a new Store Release."
    )


def partition_buildable(bundles: Iterable[Bundle]) -> tuple[list[Bundle], list[str]]:
    """Split bundles into those the release workflow may build and a refusal for each other one."""
    buildable: list[Bundle] = []
    refusals: list[str] = []
    for b in bundles:
        refusal = build_refusal(b)
        if refusal is None:
            buildable.append(b)
        else:
            refusals.append(refusal)
    return buildable, refusals


def refuse_unbuildable(bundles: Iterable[Bundle]) -> None:
    """Raise `NotBuildableError` naming every bundle the release workflow must not build."""
    _, refusals = partition_buildable(bundles)
    if refusals:
        listed = "\n".join(f"  {refusal}" for refusal in refusals)
        raise NotBuildableError(
            "Refusing to run: only a bundle with Release Status "
            f"{', '.join(sorted(BUILDABLE_STATUSES))} is built (#247):\n{listed}\n"
            "Nothing was written."
        )


@dataclass(frozen=True)
class ToleratedGap:
    """Errors ``check()`` tolerated rather than reporting on its primary channel.

    ``count`` is how many individual error strings were suppressed; ``detail``
    names what they were; ``citation`` is the issue (or status) that justifies
    tolerating them. One record per release, aggregated rather than per-error,
    so the disclosure is a count a reviewer can quote -- "70 blank required
    Analysis values" -- not a 70-line echo of the exemption (issue #142).
    """

    store_id: str
    count: int
    detail: str
    citation: str


class CheckResult(list[str]):
    """``check()``'s two return channels: errors plus tolerated suppressions.

    A ``list`` subclass so the primary channel stays a plain errors list and no
    existing caller that compares ``== []``, iterates, or tests membership
    breaks. ``.tolerated`` is the second channel -- a tuple of
    :class:`ToleratedGap` naming what ``check()`` deliberately did not report,
    so a caller can surface the gap instead of a clean ``[]`` silently erasing
    it (issue #142).
    """

    def __init__(
        self,
        errors: Iterable[str] = (),
        tolerated: Iterable[ToleratedGap] = (),
    ) -> None:
        super().__init__(errors)
        self.tolerated = tuple(tolerated)

    def __repr__(self) -> str:
        return (
            f"CheckResult(errors={list(self)!r}, tolerated={self.tolerated!r})"
        )


def _column_values(
    table: opengwasdb_analyses.AnalysesTable, column: str
) -> list[str] | None:
    """Return complete raw values, or ``None`` when the column is sparse."""
    if column not in table.fieldnames or not table.rows:
        return None
    values = [row.get(column, "") for row in table.rows]
    if any(not isinstance(value, str) or not value.strip() for value in values):
        return None
    return values


def _collapse_identifier(
    table: opengwasdb_analyses.AnalysesTable, column: str
) -> str:
    values = _column_values(table, column)
    if values is None:
        return "NA"
    distinct = tuple(dict.fromkeys(values))
    if len(distinct) == 1:
        return distinct[0]
    return f"mixed ({len(distinct)})"


def _collapse_quantity(
    table: opengwasdb_analyses.AnalysesTable, column: str
) -> str:
    values = _column_values(table, column)
    if values is None:
        return "NA"
    distinct = tuple(dict.fromkeys(values))
    if len(distinct) == 1:
        return distinct[0]

    parsed: list[tuple[Decimal, str]] = []
    for raw in distinct:
        try:
            number = Decimal(raw)
        except InvalidOperation as exc:
            raise ValueError(
                f"cannot summarise non-numeric {column} value {raw!r}"
            ) from exc
        if not number.is_finite():
            raise ValueError(
                f"cannot summarise non-finite {column} value {raw!r}"
            )
        parsed.append((number, raw))

    minimum = min(parsed, key=lambda item: item[0])[1]
    maximum = max(parsed, key=lambda item: item[0])[1]
    return f"{minimum}-{maximum}"


def _collapse_url(table: opengwasdb_analyses.AnalysesTable, column: str) -> str:
    values = _column_values(table, column)
    if values is None:
        return "NA"
    distinct = tuple(dict.fromkeys(values))
    if len(distinct) == 1:
        return distinct[0]

    parsed = [urlsplit(value) for value in distinct]
    for raw, parts in zip(distinct, parsed, strict=True):
        if not parts.scheme or not parts.netloc:
            raise ValueError(f"cannot summarise invalid {column} URL {raw!r}")

    authorities = {(parts.scheme.lower(), parts.netloc.lower()) for parts in parsed}
    if len(authorities) != 1:
        return f"mixed ({len(distinct)})"

    first = parsed[0]
    path_parts = [parts.path.split("/") for parts in parsed]
    common: list[str] = []
    for components in zip(*path_parts, strict=False):
        if len(set(components)) != 1:
            break
        common.append(components[0])

    common_path = "/".join(common)
    # A common path component without a trailing slash may be a whole file
    # (for example URLs differing only by query). Otherwise publish a directory
    # prefix, never a partial filename.
    if any(parts.path != common_path for parts in parsed):
        common_path = common_path.rstrip("/") + "/"
    return urlunsplit((first.scheme, first.netloc, common_path or "/", "", ""))


def partition_membership(
    table: opengwasdb_analyses.AnalysesTable,
) -> tuple[list[Mapping[str, str]], list[Mapping[str, str]]]:
    """Split ``analyses.tsv`` rows into (admitted, excluded) using one strict parse.

    ``exclude_from_build`` is the registry-only admission column (ADR 0025).
    This is the same classifier the build-manifest materialiser uses, so a
    review view cannot count a row as admitted that the builder would refuse.
    """
    admitted: list[Mapping[str, str]] = []
    excluded: list[Mapping[str, str]] = []
    for row_index, row in enumerate(table.rows):
        analysis_id = (row.get("analysis_id") or "").strip()
        if classify_exclusion(row.get(EXCLUDE_COLUMN), row_index, analysis_id):
            excluded.append(row)
        else:
            admitted.append(row)
    return admitted, excluded


def membership_ids(bundle: Bundle) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return ``(admitted analysis_ids, excluded analysis_ids)`` from ``analyses.tsv``.

    Reads only the bundle's membership table, never a Store or Release
    Artifact (ADR 0023): the registry owns intended and admitted membership;
    what was actually built is a separate evidence channel.
    """
    from opengwasdb.model import analyses as opengwasdb_analyses
    table = opengwasdb_analyses.read_analyses(bundle.analyses_path)
    admitted, excluded = partition_membership(table)
    admitted_ids = tuple((row.get("analysis_id") or "").strip() for row in admitted)
    excluded_ids = tuple((row.get("analysis_id") or "").strip() for row in excluded)
    return admitted_ids, excluded_ids


def summarise(bundle: Bundle) -> dict[str, str | int]:
    """Derive a Store Release summary solely from its Analysis membership.

    The function reads ``analyses.tsv`` and no Store or Release Artifact. It
    preserves absence as ``NA`` and raw constant values verbatim; it never
    substitutes metadata from ``release.yaml`` or a Build Recipe.

    Membership counts are separated rather than collapsed: ``n_manifest`` is
    every manifest row, ``n_admitted`` the rows that pass the
    ``exclude_from_build`` admission column, and ``n_excluded`` the rest.
    ``n_analyses`` is the admitted count -- the Store's membership -- so a
    derived view never presents an excluded manifest row as a built Analysis
    (issue #217). ``n_manifest`` is a *selection* count, not the intended Source
    Inventory size, and is named separately from ``n_admitted`` for that reason.
    """
    from opengwasdb.model import analyses as opengwasdb_analyses
    table = opengwasdb_analyses.read_analyses(bundle.analyses_path)
    admitted, excluded = partition_membership(table)
    summary: dict[str, str | int] = {
        "n_manifest": len(table.rows),
        "n_admitted": len(admitted),
        "n_excluded": len(excluded),
        "n_analyses": len(admitted),
    }
    for column in _IDENTIFIER_SUMMARY_COLUMNS:
        summary[column] = _collapse_identifier(table, column)
    summary["sample_size"] = _collapse_quantity(table, "sample_size")
    summary["source_url"] = _collapse_url(table, "source_url")
    return summary


def _read_yaml(path: Path) -> dict[str, Any] | None:
    """Read a mapping document, recording all read/shape failures as data."""
    try:
        if path.is_symlink():
            return {"__read_error__": "bundle documents must not be symbolic links"}
        if not path.is_file():
            return None
        with path.open("r", encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        return {"__yaml_error__": str(exc)}
    except (OSError, UnicodeError) as exc:
        return {"__read_error__": f"{type(exc).__name__}: {exc}"}

    if document is None:
        return {}
    if not isinstance(document, dict):
        return {
            "__document_error__":
                f"expected a YAML mapping, got {type(document).__name__}"
        }
    return document


def load(store_id: str, registry_root: Path | str | None = None) -> Bundle:
    """Load registry files for one Release Bundle; never open its Store."""
    if registry_root is not None:
        candidate = Path(registry_root)
        root = candidate if candidate.name == store_id else candidate / store_id
    else:
        candidates = (
            Path("stores") / store_id,
            REPO_ROOT / "stores" / store_id,
            Path.cwd() / store_id,
            Path.cwd(),
        )
        root = next(
            (
                candidate
                for candidate in candidates
                if candidate.is_dir()
                and (candidate / "release.yaml").is_file()
            ),
            Path("stores") / store_id,
        )

    return Bundle(
        store_id=store_id,
        root=root,
        release=_read_yaml(root / "release.yaml") or {},
        build=_read_yaml(root / "build.yaml") or {},
        analyses_path=root / "analyses.tsv",
        validation=_read_yaml(root / "validation.yaml"),
    )


def _document_errors(
    document: object, filename: str
) -> tuple[Mapping[str, Any] | None, list[str]]:
    if not isinstance(document, Mapping):
        return None, [
            f"{filename} must contain a mapping, got {type(document).__name__}"
        ]
    if not document:
        return document, [f"{filename} is missing or empty"]
    if "__yaml_error__" in document:
        return None, [f"{filename} is malformed YAML: {document['__yaml_error__']}"]
    if "__read_error__" in document:
        return None, [f"{filename} could not be read: {document['__read_error__']}"]
    if "__document_error__" in document:
        return None, [f"{filename} {document['__document_error__']}"]
    return document, []


def _is_missing(document: Mapping[str, Any], key: str) -> bool:
    value = document.get(key)
    return value is None or value == ""


def _check_identity(bundle: Bundle) -> list[str]:
    errors: list[str] = []
    if not is_valid_store_id(bundle.store_id):
        errors.append(
            f"malformed store_id {bundle.store_id!r}: must match pattern "
            "'OGS-\\d{5}' (ADR 0022)"
        )

    root = Path(bundle.root)
    try:
        if root.is_symlink():
            errors.append(f"store directory {root} must not be a symbolic link")
        elif not root.is_dir():
            errors.append(f"store directory {root} does not exist")
        elif root.name != bundle.store_id:
            errors.append(
                f"store_id {bundle.store_id!r} does not match directory name "
                f"{root.name!r}"
            )
    except OSError as exc:
        errors.append(f"store directory {root} could not be inspected: {exc}")
    return errors


def _check_release(bundle: Bundle) -> list[str]:
    release, errors = _document_errors(bundle.release, "release.yaml")
    if release is None:
        return errors

    if release.get("store_id") != bundle.store_id:
        errors.append(
            f"release.yaml store_id {release.get('store_id')!r} does not match "
            f"bundle store_id {bundle.store_id!r}"
        )
    for key in RELEASE_REQUIRED_KEYS:
        if _is_missing(release, key):
            errors.append(f"release.yaml is missing required key: {key!r}")

    status = release.get("status")
    if status is not None and status not in VALID_STATUSES:
        errors.append(
            f"release.yaml has invalid status {status!r}; expected one of "
            f"{sorted(VALID_STATUSES)}"
        )

    generator = release.get("generator")
    if generator is not None:
        if not isinstance(generator, Mapping):
            errors.append("release.yaml generator must be a mapping")
        else:
            commands = generator.get("commands")
            if commands is None:
                errors.append(
                    "release.yaml generator is missing required key: 'commands'"
                )
            elif not isinstance(commands, list) or not commands or not all(
                isinstance(command, str) and command.strip() for command in commands
            ):
                errors.append(
                    "release.yaml generator commands must be a non-empty list "
                    "of non-empty strings"
                )
        version = generator.get("version") if isinstance(generator, Mapping) else None
        if isinstance(version, str) and version.startswith("sha256:"):
            checksum = version.removeprefix("sha256:")
            if not _is_valid_hex(checksum, 64):
                errors.append(
                    "release.yaml generator version has invalid sha256 checksum: "
                    f"{checksum!r}"
                )

    source_snapshot = release.get("source_snapshot")
    if source_snapshot is not None and not isinstance(source_snapshot, Mapping):
        errors.append("release.yaml source_snapshot must be a mapping")
    elif isinstance(source_snapshot, Mapping):
        checksum = source_snapshot.get("manifest_sha256")
        if checksum is not None and not _is_valid_hex(checksum, 64):
            errors.append(
                "release.yaml source_snapshot manifest_sha256 is invalid hex "
                f"sha256: {checksum!r}"
            )

    return errors


def _check_indexed_subsets(build: Mapping[str, Any]) -> list[str]:
    """Validate optional `indexed_subsets` declarations without any resource I/O.

    Shape and vocabulary only: names, required string fields, checksum syntax,
    uniqueness, and the Ragged refusal. Whether the declared `resource_id`
    resolves to the maintained resource, and whether the list's bytes match the
    declared checksum, are runtime facts the release entry point checks before a
    build (`ogstores.resources`); `check()` never opens a resource or a Store.
    """
    errors: list[str] = []
    if INDEXED_SUBSETS_KEY not in build:
        return errors
    raw = build.get(INDEXED_SUBSETS_KEY)
    if raw is None:
        return [
            f"build.yaml {INDEXED_SUBSETS_KEY} must be a list of declarations, "
            "not null; omit the key to declare none"
        ]
    if not isinstance(raw, list):
        return [
            f"build.yaml {INDEXED_SUBSETS_KEY} must be a list of declarations, "
            f"not {type(raw).__name__}"
        ]
    if not raw:
        return errors

    layout = build.get("layout")
    if layout not in INDEXED_SUBSET_LAYOUTS:
        errors.append(
            f"build.yaml {INDEXED_SUBSETS_KEY} may be declared only for "
            f"{sorted(INDEXED_SUBSET_LAYOUTS)} layout, not {layout!r}"
        )

    allowed = set(INDEXED_SUBSET_REQUIRED_KEYS) | set(INDEXED_SUBSET_OPTIONAL_KEYS)
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        where = f"build.yaml {INDEXED_SUBSETS_KEY}[{index}]"
        if not isinstance(entry, Mapping):
            errors.append(f"{where} must be a mapping")
            continue

        unknown = sorted(set(entry) - allowed)
        if unknown:
            errors.append(
                f"{where} has unknown key(s) {unknown}; allowed keys are "
                f"{sorted(allowed)}"
            )
        for key in INDEXED_SUBSET_REQUIRED_KEYS:
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{where} requires a non-empty string {key!r}")

        name = entry.get("name")
        if isinstance(name, str) and name.strip():
            if not is_valid_indexed_subset_name(name):
                errors.append(
                    f"{where} name {name!r} must match "
                    f"{INDEXED_SUBSET_NAME_RE.pattern} and contain no '..'"
                )
            elif name in seen:
                errors.append(f"{where} duplicates subset name {name!r}")
            else:
                seen.add(name)

        checksum = entry.get("checksum")
        if checksum is not None and not _is_valid_hex(checksum, 64):
            errors.append(
                f"{where} checksum must be a 64-character hex sha256, "
                f"got {checksum!r}"
            )

        options = entry.get("options")
        if options is not None:
            if not isinstance(options, Mapping):
                errors.append(f"{where} options must be a mapping")
            else:
                reserved = reserved_options(options, INDEXED_SUBSET_RESERVED_OPTION_KEYS)
                if reserved:
                    errors.append(
                        f"{where} options must not set {reserved}; variant_list and "
                        "reference_assembly are the declaration's own verified inputs"
                    )
                elif "overwrite" in options:
                    errors.append(
                        f"{where} options must not set 'overwrite'; a Release Bundle "
                        "declares a named subset to build, not to replace in place"
                    )
                else:
                    unrenderable = unrenderable_options(options)
                    if unrenderable:
                        errors.append(
                            f"{where} options must give a value for {unrenderable} "
                            "that renders as CLI tokens: a string, number or bool, or "
                            "a non-empty list of them. A mapping would be passed as "
                            "its own repr, and null or an empty list as nothing"
                        )

    return errors


def _check_build(bundle: Bundle) -> list[str]:
    build, errors = _document_errors(bundle.build, "build.yaml")
    if build is None:
        return errors

    if build.get("store_id") != bundle.store_id:
        errors.append(
            f"build.yaml store_id {build.get('store_id')!r} does not match "
            f"bundle store_id {bundle.store_id!r}"
        )
    for key in BUILD_REQUIRED_KEYS:
        if _is_missing(build, key):
            errors.append(f"build.yaml is missing required key: {key!r}")

    layout = build.get("layout")
    if layout is not None and layout not in {"dense", "ragged", "hybrid"}:
        errors.append(
            f"build.yaml has invalid layout {layout!r}; expected 'dense', "
            "'ragged', or 'hybrid'"
        )

    completion_state = build.get("completion_state")
    if completion_state is not None and completion_state not in {
        "observed_only",
        "reference_completed",
    }:
        errors.append(
            f"build.yaml has invalid completion_state {completion_state!r}; "
            "expected 'observed_only' or 'reference_completed'"
        )

    block_name = "complete" if completion_state == "reference_completed" else "build"
    block = build.get(block_name)
    release = bundle.release if isinstance(bundle.release, Mapping) else {}
    if completion_state == "reference_completed" and not release.get("derived_from"):
        # One id, published twice (ADR 0037): the build phase publishes the
        # Observed-Only Store that the completion phase then completes.
        for name in ("build", "complete"):
            phase_block = build.get(name)
            if not isinstance(phase_block, Mapping):
                errors.append(
                    f"build.yaml for a reference_completed release with no derived_from "
                    f"must declare a {name!r} mapping: it builds and publishes its own "
                    "Observed-Only Store, then completes it (ADR 0037)"
                )
            elif _is_missing(phase_block, "command"):
                errors.append(f"build.yaml {name} block is missing required key: 'command'")
    elif completion_state in {"observed_only", "reference_completed"}:
        if not isinstance(block, Mapping):
            errors.append(
                f"build.yaml for {completion_state} release must declare "
                f"a {block_name!r} mapping"
            )
        elif _is_missing(block, "command"):
            errors.append(f"build.yaml {block_name} block is missing required key: 'command'")

    post = build.get("post")
    if post is not None and not isinstance(post, Mapping):
        errors.append("build.yaml post must be a mapping")
    if "artifacts" in build:
        errors.append(
            "build.yaml must not declare 'artifacts'; the artifact root is "
            "deployment configuration resolved by paths.artifact_root() (issue #126)"
        )

    errors.extend(_check_indexed_subsets(build))

    # 908797f made the BESD prefix a frozen bundle provenance fact. Validate
    # the value, but deliberately do not stat the external BESD artifacts.
    command = block.get("command") if isinstance(block, Mapping) else None
    if command == "build-ragged-besd":
        release = bundle.release if isinstance(bundle.release, Mapping) else {}
        snapshot = release.get("source_snapshot")
        prefix = snapshot.get("besd_prefix") if isinstance(snapshot, Mapping) else None
        if not isinstance(prefix, str) or not prefix.strip():
            errors.append(
                "release.yaml requires a non-empty string "
                "'source_snapshot.besd_prefix' for build-ragged-besd"
            )

    return errors


def _registry_directory(bundle: Bundle, registry_root: Path | str | None) -> Path:
    if registry_root is None:
        return Path(bundle.root).parent
    root = Path(registry_root)
    return root.parent if root.name == bundle.store_id else root


def _check_lineage(
    bundle: Bundle, registry_root: Path | str | None
) -> list[str]:
    release = bundle.release if isinstance(bundle.release, Mapping) else {}
    parent_id = release.get("derived_from")
    errors: list[str] = []

    # A reference_completed release without derived_from completes its own
    # Store (ADR 0037); `_check_build` requires both of its phases' blocks.
    if parent_id in (None, ""):
        return errors
    if not is_valid_store_id(parent_id):
        return [
            f"derived_from {parent_id!r} is not a valid store_id "
            "(must match 'OGS-\\d{5}')"
        ]
    if parent_id == bundle.store_id:
        return [f"derived_from cannot reference itself: {parent_id!r}"]

    parent_root = _registry_directory(bundle, registry_root) / parent_id
    try:
        registered = (
            not parent_root.is_symlink()
            and parent_root.is_dir()
            and not (parent_root / "release.yaml").is_symlink()
            and (parent_root / "release.yaml").is_file()
        )
    except OSError as exc:
        return [f"derived_from {parent_id!r} could not be resolved: {exc}"]
    if not registered:
        return [
            f"unresolvable derived_from {parent_id!r}: registered parent "
            "Release Bundle not found"
        ]

    parent_release = _read_yaml(parent_root / "release.yaml")
    if not isinstance(parent_release, Mapping) or parent_release.get("store_id") != parent_id:
        errors.append(
            f"unresolvable derived_from {parent_id!r}: parent release.yaml "
            "does not declare the registered store_id"
        )
    return errors


def _check_previous_status(
    bundle: Bundle, previous_status: str | Bundle | None
) -> list[str]:
    if previous_status is None:
        return []
    if isinstance(previous_status, Bundle):
        previous_release = (
            previous_status.release
            if isinstance(previous_status.release, Mapping)
            else {}
        )
        before = previous_release.get("status")
    else:
        before = previous_status
    release = bundle.release if isinstance(bundle.release, Mapping) else {}
    after = release.get("status")
    if not isinstance(before, str) or not isinstance(after, str):
        return ["status transition requires string current and target statuses"]
    return validate_status_transition(before, after)


# The Validation Record verdicts that give Release Status `validated` (#195,
# decided 7 Oct 2026): warnings are non-blocking, so they stay in the record.
VALIDATED_RECORD_STATUSES: frozenset[str] = frozenset({"passed", "passed_with_warnings"})


def _check_validation(bundle: Bundle) -> list[str]:
    release = bundle.release if isinstance(bundle.release, Mapping) else {}
    status = release.get("status")
    if status not in {"built", "validated"}:
        return []
    validation, errors = _document_errors(bundle.validation, "validation.yaml")
    if validation is None:
        return errors
    validation_status = validation.get("status")
    allowed = {"not_run", "passed", "passed_with_warnings", "failed"}
    if validation_status not in allowed:
        errors.append(
            f"validation.yaml has invalid status {validation_status!r}; "
            f"expected one of {sorted(allowed)}"
        )
    elif status == "validated" and validation_status not in VALIDATED_RECORD_STATUSES:
        # opengwasdb validate passing, with or without warnings, is what makes
        # a release validated; a failed or never-run validate never does (#195).
        errors.append(
            f"release status 'validated' needs a validation.yaml status of "
            f"{' or '.join(sorted(VALIDATED_RECORD_STATUSES))}; got {validation_status!r}"
        )
    return errors


def _check_analyses(
    bundle: Bundle, analyses: Callable[[], opengwasdb_analyses.AnalysesTable]
) -> tuple[list[str], list[ToleratedGap]]:
    from opengwasdb.model import analyses as opengwasdb_analyses
    errors: list[str] = []
    tolerated: list[ToleratedGap] = []
    try:
        analyses_path = Path(bundle.analyses_path)
        if analyses_path.is_symlink():
            return [f"analyses file must not be a symbolic link: {analyses_path}"], []
        if not analyses_path.is_file():
            return [f"analyses file does not exist: {bundle.analyses_path}"], []
        table = analyses()
    except Exception as exc:
        return [f"failed to read analyses.tsv: {type(exc).__name__}: {exc}"], []

    for column in opengwasdb_analyses.RETIRED_ANALYSIS_COLUMNS:
        if column in table.fieldnames:
            errors.append(
                f"Store Release {bundle.store_id} analyses.tsv contains retired "
                f"Analysis column {column!r}"
            )

    # A Trait Ontology Mapping is an ontology term plus its trait label. It is
    # never a gene identifier with an authority name standing in for the label:
    # that asserts a gene is the Trait (issue #141). Empty is the correct
    # answer where no acceptable term exists. Each term of a list is checked,
    # and each term must be a CURIE (#286).
    if "trait_ontology_id" in table.fieldnames:
        tolerate_non_curie = bundle.store_id in LEGACY_NON_CURIE_TRAIT_ONTOLOGY_RELEASES
        tolerated_non_curie = 0
        for row in table.rows:
            analysis_id = row.get("analysis_id") or "<unknown analysis_id>"
            ontology_id = row.get("trait_ontology_id") or ""
            if ontology_id and is_gene_shaped_trait_ontology_id(ontology_id):
                errors.append(
                    f"analysis {analysis_id!r} has gene-shaped trait_ontology_id "
                    f"{ontology_id!r}; trait_ontology_id must be an ontology term "
                    "(or empty), never a gene or protein identifier"
                )
            elif not is_canonical_trait_ontology_id(ontology_id):
                if tolerate_non_curie:
                    tolerated_non_curie += 1
                else:
                    errors.append(
                        f"analysis {analysis_id!r} has trait_ontology_id {ontology_id!r}, "
                        "which is not a CURIE or a list of CURIEs joined by "
                        f"{TRAIT_ONTOLOGY_ID_SEPARATOR!r}"
                    )
            if is_authority_name_trait_ontology_label(row.get("trait_ontology_label") or ""):
                errors.append(
                    f"analysis {analysis_id!r} has trait_ontology_label "
                    f"{row.get('trait_ontology_label')!r}, which names an identifier "
                    "authority rather than a Trait; use the trait label or leave it "
                    "empty"
                )
        if tolerated_non_curie:
            tolerated.append(
                ToleratedGap(
                    store_id=bundle.store_id,
                    count=tolerated_non_curie,
                    detail="non-CURIE trait_ontology_id values",
                    citation="#286",
                )
            )

    # Assigned Ancestry is the registry-normalised super-population code, never a
    # free-text Source Ancestry Label. Empty is the one valid way to record an
    # Analysis that is unassigned; "European" is not a synonym for "EUR".
    if "assigned_ancestry" in table.fieldnames:
        for row in table.rows:
            value = (row.get("assigned_ancestry") or "").strip()
            if value and value not in SUPERPOPULATIONS:
                analysis_id = row.get("analysis_id") or "<unknown analysis_id>"
                errors.append(
                    f"analysis {analysis_id!r} has assigned_ancestry {value!r}; "
                    f"expected one of {list(SUPERPOPULATIONS)} or empty for "
                    "unassigned"
                )

    # Case/control counts are required only for case-control semantics, and
    # absence is distinct from zero. Storing 0 for a non-case-control Analysis
    # fabricates an event count that the evidence does not support.
    if "sample_size_kind" in table.fieldnames:
        for row in table.rows:
            kind = (row.get("sample_size_kind") or "").strip()
            if not kind or kind == "case_control":
                continue
            analysis_id = row.get("analysis_id") or "<unknown analysis_id>"
            for column in ("n_cases", "n_controls"):
                if column not in table.fieldnames:
                    continue
                value = (row.get(column) or "").strip()
                if value:
                    errors.append(
                        f"analysis {analysis_id!r} has {column}={value!r} but "
                        f"sample_size_kind={kind!r} is not 'case_control'; "
                        "case and control counts must be absent when not applicable"
                    )

    active_table = table
    if "exclude_from_build" in table.fieldnames:
        active_table = opengwasdb_analyses.AnalysesTable(
            fieldnames=table.fieldnames,
            rows=tuple(
                row for row in table.rows
                if row.get("exclude_from_build") != "true"
            ),
        )

    try:
        analysis_errors = opengwasdb_analyses.validate_analyses(active_table)
    except Exception as exc:
        errors.append(f"failed to validate analyses.tsv: {type(exc).__name__}: {exc}")
        analysis_errors = []

    # Required values must be present in a bundle Store Release Build builds
    # (#247). One it will not build -- a candidate still being resolved, or a
    # retired release -- may leave them blank; what a candidate may leave blank
    # is Manifest Generation's policy, not this contract's (#250). Legacy trial
    # releases OGS-00001 and OGS-00002 are tolerated pending external study
    # metadata and upstream schema resolution (issue #134), strictly scoped to
    # those two so that any new release (including any future ragged-BESD
    # build) fails loudly if required Analysis values are blank.
    allow_blank_overlay_values = (
        not is_buildable(bundle)
        or bundle.store_id in LEGACY_BLANK_ANALYSIS_RELEASES
    )
    suppressed_blank_values = 0
    for error in analysis_errors:
        if allow_blank_overlay_values and "has no value for required column" in error:
            suppressed_blank_values += 1
            continue
        errors.append(error)
    # Surface the exemption on check()'s second channel instead of silently
    # discarding it: a clean error list must not erase 70 interpretation-bearing
    # values (issue #142). The suppressed strings are all one form, so the
    # disclosure is the count, not a 70-line echo.
    if suppressed_blank_values:
        tolerated.append(
            ToleratedGap(
                store_id=bundle.store_id,
                count=suppressed_blank_values,
                detail="blank required Analysis values",
                citation=(
                    "#134"
                    if bundle.store_id in LEGACY_BLANK_ANALYSIS_RELEASES
                    else "not buildable"
                ),
            )
        )

    if {"checksum", "checksum_algorithm"}.issubset(table.fieldnames):
        lengths = {"md5": 32, "sha1": 40, "sha256": 64}
        for row in table.rows:
            checksum = (row.get("checksum") or "").strip()
            algorithm = (row.get("checksum_algorithm") or "").strip()
            analysis_id = row.get("analysis_id") or "<unknown analysis_id>"
            if not checksum and not algorithm:
                continue
            if algorithm not in lengths:
                errors.append(
                    f"analysis {analysis_id!r} has unsupported "
                    f"checksum_algorithm {algorithm!r}"
                )
            elif not _is_valid_hex(checksum, lengths[algorithm]):
                errors.append(
                    f"analysis {analysis_id!r} has invalid {algorithm} "
                    f"checksum {checksum!r}"
                )
    return errors, tolerated


def _entry_kind(path: Path) -> str:
    """``absent``, ``file`` or ``directory``; ``other`` for a symbolic link or anything else.

    A bundle's contract files are real files in the bundle, never links out of it.
    """
    if path.is_symlink():
        return "other"
    if not path.exists():
        return "absent"
    if path.is_file():
        return "file"
    return "directory" if path.is_dir() else "other"


def _analyses_reader(bundle: Bundle) -> Callable[[], opengwasdb_analyses.AnalysesTable]:
    """``analyses.tsv``, parsed at most once by one ``check()``; a failed parse raises again."""

    @functools.cache
    def read() -> opengwasdb_analyses.AnalysesTable:
        from opengwasdb.model import analyses as opengwasdb_analyses

        return opengwasdb_analyses.read_analyses(bundle.analyses_path)

    return read


def _read_sidecar_rows(path: Path) -> tuple[list[str], list[list[str]]]:
    """Header and rows of a strict TSV sidecar, or raise ``ValueError``."""
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE))
    if not rows:
        raise ValueError("is empty; it needs at least a header")
    header, body = rows[0], rows[1:]
    for number, row in enumerate(body, 2):
        if len(row) != len(header):
            raise ValueError(f"line {number} has {len(row)} fields; the header has {len(header)}")
    return header, body


def _quality_check_column_errors(header: Sequence[str]) -> list[str]:
    leading = len(QUALITY_CHECK_LEADING_COLUMNS)
    expected = (
        f"columns must be {', '.join(QUALITY_CHECK_LEADING_COLUMNS)}, then one "
        f"<check>, <check>{QUALITY_CHECK_MEASURED_SUFFIX} pair per Quality Check"
    )
    pairs = header[leading:]
    if tuple(header[:leading]) != QUALITY_CHECK_LEADING_COLUMNS or not pairs or len(pairs) % 2:
        return [f"{QUALITY_CHECKS_SIDECAR}: {expected}; got {list(header)}"]
    names = pairs[0::2]
    for name, measured in zip(names, pairs[1::2]):
        if not name.isidentifier() or measured != f"{name}{QUALITY_CHECK_MEASURED_SUFFIX}":
            return [f"{QUALITY_CHECKS_SIDECAR}: {expected}; {name!r} is followed by {measured!r}"]
    if len(set(names)) != len(names):
        return [f"{QUALITY_CHECKS_SIDECAR}: columns name a Quality Check twice: {names}"]
    return []


def _exclusion_reasons(bundle: Bundle) -> dict[str, str] | None:
    """``analysis_id -> reason`` from ``sidecars/exclusions.tsv``, or ``None`` if absent."""
    path = bundle.root / EXCLUSIONS_SIDECAR
    if not path.is_file() or path.is_symlink():
        return None
    header, body = _read_sidecar_rows(path)
    if "analysis_id" not in header or "reason" not in header:
        raise ValueError(f"{EXCLUSIONS_SIDECAR} needs analysis_id and reason columns")
    id_at, reason_at = header.index("analysis_id"), header.index("reason")
    return {row[id_at]: row[reason_at] for row in body}


def _check_quality_checks(
    bundle: Bundle, analyses: Callable[[], opengwasdb_analyses.AnalysesTable]
) -> list[str]:
    """Validate ``sidecars/quality_checks.tsv`` when the bundle carries one (#285).

    It must name every ``analyses.tsv`` Analysis once, in the same order; use
    the controlled result vocabulary; and agree with ``analyses.tsv`` on which
    Analyses are excluded, and with ``sidecars/exclusions.tsv`` (when present)
    on why. Absent is valid.
    """
    path = bundle.root / QUALITY_CHECKS_SIDECAR
    kind = _entry_kind(path)
    if kind == "absent":
        return []
    if kind != "file":
        return [f"{QUALITY_CHECKS_SIDECAR} must be a regular file"]
    try:
        header, body = _read_sidecar_rows(path)
    except (OSError, UnicodeDecodeError, ValueError, csv.Error) as exc:
        return [f"{QUALITY_CHECKS_SIDECAR} {exc}"]
    errors = _quality_check_column_errors(header)
    if errors:
        return errors

    table = analyses()
    expected: list[tuple[str, bool]] = []
    for index, row in enumerate(table.rows):
        analysis_id = (row.get("analysis_id") or "").strip()
        expected.append(
            (analysis_id, classify_exclusion(row.get(EXCLUDE_COLUMN), index, analysis_id))
        )
    found = [row[0] for row in body]
    wanted = [analysis_id for analysis_id, _ in expected]
    if found != wanted:
        missing = [aid for aid in wanted if aid not in set(found)]
        extra = [aid for aid in found if aid not in set(wanted)]
        detail = []
        if missing:
            detail.append(f"missing {missing[:5]}")
        if extra:
            detail.append(f"not in analyses.tsv {extra[:5]}")
        if not missing and not extra:
            detail.append("rows are not in analyses.tsv order")
        return [
            f"{QUALITY_CHECKS_SIDECAR} must have one row per analyses.tsv Analysis, "
            f"in its order: {'; '.join(detail)}"
        ]
    try:
        reasons = _exclusion_reasons(bundle)
    except (OSError, UnicodeDecodeError, ValueError, csv.Error) as exc:
        return [f"{EXCLUSIONS_SIDECAR} {exc}"]

    leading = len(QUALITY_CHECK_LEADING_COLUMNS)
    checks = list(zip(header[leading::2], range(leading, len(header), 2)))
    row_errors: list[str] = []
    for row, (analysis_id, excluded) in zip(body, expected):
        flag, reason = row[1], row[2]
        if flag not in ("", "true"):
            row_errors.append(f"{analysis_id}: exclude_from_build {flag!r} is not 'true' or empty")
        elif (flag == "true") != excluded:
            row_errors.append(
                f"{analysis_id}: exclude_from_build {flag!r} disagrees with analyses.tsv"
            )
        if excluded and not reason:
            row_errors.append(f"{analysis_id}: excluded with an empty exclusion_reason")
        if not excluded and reason:
            row_errors.append(f"{analysis_id}: included but has exclusion_reason {reason!r}")
        if excluded and reasons is not None and reasons.get(analysis_id) != reason:
            row_errors.append(
                f"{analysis_id}: exclusion_reason {reason!r} disagrees with "
                f"{EXCLUSIONS_SIDECAR} ({reasons.get(analysis_id)!r})"
            )
        for name, at in checks:
            if row[at] not in QUALITY_CHECK_RESULTS:
                row_errors.append(
                    f"{analysis_id}: {name} result {row[at]!r} is not one of "
                    f"{', '.join(QUALITY_CHECK_RESULTS)}"
                )
    shown = [f"{QUALITY_CHECKS_SIDECAR}: {error}" for error in row_errors[:_MAX_SIDECAR_ROW_ERRORS]]
    if len(row_errors) > _MAX_SIDECAR_ROW_ERRORS:
        shown.append(
            f"{QUALITY_CHECKS_SIDECAR}: {len(row_errors) - _MAX_SIDECAR_ROW_ERRORS} more row error(s)"
        )
    return shown


@dataclass(frozen=True)
class ColumnMapBinding:
    """The Build Recipe's ``column_map``: which map a mapped bundle reads through.

    ``name`` and ``sha256`` are the fingerprint OpenGWASDB records in a mapped
    Resolution Record (``resolution_config.column_map``) and Store manifest
    (``provenance.column_map``): the map's own ``name``, which may be any
    non-empty string, and the sha256 of its file's bytes. ``file`` is where the
    bundle carries it, under ``column_maps/``; a built-in map has none.
    """

    name: str
    sha256: str
    file: str | None = None

    @property
    def path(self) -> str | None:
        """The carried map, relative to the bundle root; ``None`` for a built-in."""
        return column_map_path(self.file) if self.file is not None else None

    def fingerprint(self) -> dict[str, str]:
        """The ``{name, sha256}`` a Resolution Record made with this map carries."""
        return {"name": self.name, "sha256": self.sha256}


def column_map_path(file: str) -> str:
    """Where a bundle carries a Column Map ``file``, relative to its root."""
    return f"{COLUMN_MAPS_DIR}/{file}"


def is_valid_column_map_file(file: object) -> bool:
    """A carried map's file name: the Indexed Variant Subset grammar, ending ``.toml``."""
    return (
        isinstance(file, str)
        and file.endswith(COLUMN_MAP_SUFFIX)
        and is_valid_indexed_subset_name(file)
    )


def _recipe_options(build: Mapping[str, Any]) -> Iterable[tuple[str, Mapping[Any, Any]]]:
    """``(block name, options)`` for each Store-producing Build Recipe block."""
    for block_name in sorted(STORE_PRODUCING_STEPS):
        block = build.get(block_name)
        options = block.get("options") if isinstance(block, Mapping) else None
        if isinstance(options, Mapping):
            yield block_name, options


def _column_map_option_errors(build: Mapping[str, Any]) -> list[str]:
    """``--column-map`` comes from the checksummed binding, never through ``options``."""
    errors: list[str] = []
    for block_name, options in _recipe_options(build):
        reserved = reserved_options(options, _COLUMN_MAP_OPTIONS)
        if reserved:
            errors.append(
                f"build.yaml {block_name}.options must not set {reserved}; the Column Map "
                f"is the Build Recipe's checksummed {COLUMN_MAP_KEY!r} binding"
            )
    return errors


def _column_map_binding(build: Mapping[str, Any]) -> tuple[ColumnMapBinding | None, list[str]]:
    """The Build Recipe's binding, or ``None`` when it binds none or binds one malformed."""
    if COLUMN_MAP_KEY not in build:
        return None, []
    raw = build[COLUMN_MAP_KEY]
    where = f"build.yaml {COLUMN_MAP_KEY}"
    shape = "a Column Map binding {name, sha256, file}, with no file for a built-in map"
    if raw is None:
        return None, [f"{where} must be {shape}, not null; omit the key to bind none"]
    if not isinstance(raw, Mapping):
        return None, [f"{where} must be {shape}, not {type(raw).__name__}"]
    errors: list[str] = []
    unknown = sorted(str(key) for key in raw if key not in COLUMN_MAP_BINDING_KEYS)
    if unknown:
        errors.append(f"{where} has unknown key(s) {unknown}; allowed are {list(COLUMN_MAP_BINDING_KEYS)}")
    name, sha256, file = raw.get("name"), raw.get("sha256"), raw.get("file")
    if not isinstance(name, str) or not name.strip():
        errors.append(f"{where} name must be a non-empty string, the map's own name; got {name!r}")
    if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
        errors.append(
            f"{where} sha256 must be the map file's sha256 as 64 lower-case hex "
            f"characters; got {sha256!r}"
        )
    if file is not None and not is_valid_column_map_file(file):
        errors.append(
            f"{where} file {file!r} must be a file name in {COLUMN_MAPS_DIR}/ matching "
            f"{INDEXED_SUBSET_NAME_RE.pattern}, with no '..', ending {COLUMN_MAP_SUFFIX}"
        )
    if file is None and isinstance(name, str):
        builtins = _builtin_column_maps()
        packaged = builtins.get(name)
        if packaged is None:
            errors.append(
                f"{where} {name!r} names no built-in map and carries no file; a carried "
                f"map needs file, and the built-ins are {sorted(builtins)}"
            )
        elif isinstance(sha256, str) and _SHA256_RE.fullmatch(sha256) and sha256 != packaged:
            errors.append(
                f"{where} sha256 {sha256} is not built-in Column Map {name!r}'s: the "
                f"pinned opengwasdb ships it with sha256 {packaged}"
            )
    if errors:
        return None, errors
    return ColumnMapBinding(name=name, sha256=sha256, file=file), []


@functools.cache
def _builtin_column_maps() -> dict[str, str]:
    """OpenGWASDB's built-in Column Maps by name, each with the sha256 of the file it ships.

    Imported when a binding names a built-in, not with this module: importing
    the package takes over a second, and every Snakemake job imports this one.
    """
    from opengwasdb.readers.column_map import BUILTIN_COLUMN_MAPS, load_column_map

    return {name: load_column_map(name).sha256 for name in BUILTIN_COLUMN_MAPS}


def _column_map_reference_errors(
    build: Mapping[str, Any],
    rows: Sequence[Mapping[str, str]],
    binding_declared: bool,
) -> list[str]:
    """A bundle binds a map exactly when one of its Analyses reads through it.

    A row reads through its own ``source_reader_capability``, or, left blank,
    through the Build Recipe's ``source-reader-capability``.
    """
    inherited = {
        str(value).strip()
        for _, options in _recipe_options(build)
        for key, value in options.items()
        if normalize_option_key(key) == _READER_OPTION
    }
    own = [(row.get("source_reader_capability") or "").strip() for row in rows]
    named = sum(capability == COLUMN_MAP_CAPABILITY for capability in own)
    inheriting = sum(not capability for capability in own) if COLUMN_MAP_CAPABILITY in inherited else 0
    if named + inheriting and not binding_declared:
        return [
            f"Column Map missing: {named + inheriting} analyses.tsv row(s) read through "
            f"{COLUMN_MAP_CAPABILITY} ({named} named, {inheriting} from the Build Recipe), "
            f"but build.yaml binds no {COLUMN_MAP_KEY!r}"
        ]
    if binding_declared and not named + inheriting:
        return [
            f"Column Map bound in build.yaml, but no Analysis reads through it: no "
            f"analyses.tsv row names {COLUMN_MAP_CAPABILITY} or inherits it from the "
            "Build Recipe"
        ]
    return []


def _column_map_file_errors(path: Path, relative: str, binding: ColumnMapBinding) -> list[str]:
    """Checksum and shallow shape of a carried map; OpenGWASDB validates the rest."""
    data = path.read_bytes()
    errors: list[str] = []
    digest = hashlib.sha256(data).hexdigest()
    if digest != binding.sha256:
        errors.append(
            f"Column Map {relative} has sha256 {digest}; build.yaml "
            f"{COLUMN_MAP_KEY}.sha256 records {binding.sha256}"
        )
    try:
        document = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return [*errors, f"Column Map {relative} is not UTF-8 TOML: {exc}"]
    for key in ("column_map_version", "name"):
        if key not in document:
            errors.append(f"Column Map {relative} is missing {key!r}")
    if "name" in document and document["name"] != binding.name:
        errors.append(
            f"Column Map {relative} names itself {document['name']!r}; build.yaml "
            f"binds {binding.name!r}"
        )
    return errors


def _bound_column_map_file(build: Mapping[str, Any]) -> tuple[bool, str | None]:
    """``(known, path)``: the carried map the Build Recipe binds, if it can be told.

    Read from the binding's ``file`` alone, so a defect in its other fields
    does not hide one in ``column_maps/``. A binding that is not a mapping, or
    whose ``file`` is malformed, binds no file anyone can name: ``known`` is
    then false. No binding, or a built-in one, binds no file.
    """
    raw = build.get(COLUMN_MAP_KEY)
    if COLUMN_MAP_KEY in build and not isinstance(raw, Mapping):
        return raw is None, None
    file = raw.get("file") if isinstance(raw, Mapping) else None
    if file is None:
        return True, None
    return (True, column_map_path(file)) if is_valid_column_map_file(file) else (False, None)


def _carried_column_map_errors(
    bundle: Bundle, build: Mapping[str, Any], binding: ColumnMapBinding | None
) -> list[str]:
    """``column_maps/`` holds the bound map, checksummed, and nothing else."""
    directory = Path(bundle.root) / COLUMN_MAPS_DIR
    kind = _entry_kind(directory)
    if kind not in ("absent", "directory"):
        return [f"Column Map directory {COLUMN_MAPS_DIR}/ must be a directory, not a symbolic link or file"]
    known, expected = _bound_column_map_file(build)
    errors: list[str] = []
    if kind == "directory" and known:
        bound = expected or (f"built-in {binding.name!r}" if binding is not None else "no carried map")
        for entry in sorted(directory.iterdir()):
            relative = column_map_path(entry.name)
            if relative != expected:
                errors.append(f"Column Map {relative} is carried but not bound: build.yaml binds {bound}")
    if expected is None:
        return errors
    path = Path(bundle.root) / expected
    match _entry_kind(path):
        case "absent":
            errors.append(f"Column Map: build.yaml binds {expected}, but the bundle does not carry it")
        case "file" if binding is not None:
            errors.extend(_column_map_file_errors(path, expected, binding))
        case "file":
            pass  # The binding's own errors say why its checksum cannot be compared.
        case _:
            errors.append(f"Column Map {expected} must be a regular file")
    return errors


def _check_column_map(
    bundle: Bundle, analyses: Callable[[], opengwasdb_analyses.AnalysesTable]
) -> tuple[list[str], list[ToleratedGap]]:
    """A mapped bundle binds its Column Map and carries it, checksummed (#292, ADR 0036).

    The map is checked shallowly: readable TOML with ``column_map_version`` and
    a ``name`` equal to the binding's. Its full validation is OpenGWASDB's, at
    resolution and build. A built-in's sha256 is checked against the map file
    the pinned package ships.
    """
    build = bundle.build if isinstance(bundle.build, Mapping) else {}
    binding_declared = COLUMN_MAP_KEY in build
    binding, errors = _column_map_binding(build)
    errors += _column_map_option_errors(build)
    rows = None
    if _entry_kind(Path(bundle.analyses_path)) == "file":
        try:
            rows = analyses().rows
        except Exception:  # The analyses.tsv stage reports why it cannot be read.
            pass
    if rows is not None:
        errors += _column_map_reference_errors(build, rows, binding_declared)
    errors += _carried_column_map_errors(bundle, build, binding)
    return errors, []


def check(
    bundle: Bundle,
    previous_status: str | Bundle | None = None,
    registry_root: Path | str | None = None,
) -> CheckResult:
    """Return every registry-side error found in ``bundle``; never raise.

    The primary return channel is the errors list itself (empty means valid),
    kept a plain ``list`` by making :class:`CheckResult` a ``list`` subclass, so
    every existing caller comparing ``== []``, iterating, or testing membership
    is unchanged. The second channel is ``CheckResult.tolerated``: the
    suppressions the #134 legacy exemption (and a bundle that is not buildable)
    deliberately tolerate, each with its count and citation, so a caller can
    surface the gap instead of a clean ``[]`` silently erasing it (issue #142).

    The check reads files contained in the Release Bundle and other registered
    bundles needed to resolve lineage. It never resolves source paths, uses an
    artifact-root helper, or opens/inspects a built Store.
    """
    errors: list[str] = []
    tolerated: list[ToleratedGap] = []
    analyses = _analyses_reader(bundle)
    stages: tuple[
        tuple[str, Callable[[], tuple[list[str], list[ToleratedGap]]]], ...
    ] = (
        ("bundle identity", lambda: (_check_identity(bundle), [])),
        ("release.yaml", lambda: (_check_release(bundle), [])),
        ("build.yaml", lambda: (_check_build(bundle), [])),
        ("Release Status transition", lambda: (_check_previous_status(bundle, previous_status), [])),
        ("Release Lineage", lambda: (_check_lineage(bundle, registry_root), [])),
        ("validation.yaml", lambda: (_check_validation(bundle), [])),
        ("analyses.tsv", lambda: _check_analyses(bundle, analyses)),
        (QUALITY_CHECKS_SIDECAR, lambda: (_check_quality_checks(bundle, analyses), [])),
        ("Column Map", lambda: _check_column_map(bundle, analyses)),
    )
    for label, stage in stages:
        try:
            stage_errors, stage_tolerated = stage()
        except Exception as exc:  # The public contract is diagnostic, never exceptional.
            errors.append(f"{label} could not be checked: {type(exc).__name__}: {exc}")
            continue
        errors.extend(stage_errors)
        tolerated.extend(stage_tolerated)
    return CheckResult(errors, tolerated)


__all__ = [
    "BUILDABLE_STATUSES",
    "BUILD_REQUIRED_KEYS",
    "COLUMN_MAPS_DIR",
    "COLUMN_MAP_BINDING_KEYS",
    "COLUMN_MAP_CAPABILITY",
    "COLUMN_MAP_KEY",
    "COLUMN_MAP_SUFFIX",
    "Bundle",
    "CheckResult",
    "ColumnMapBinding",
    "LEGACY_BLANK_ANALYSIS_RELEASES",
    "LEGACY_NON_CURIE_TRAIT_ONTOLOGY_RELEASES",
    "LEGAL_STATUS_TRANSITIONS",
    "QUALITY_CHECKS_SIDECAR",
    "QUALITY_CHECK_LEADING_COLUMNS",
    "QUALITY_CHECK_MEASURED_SUFFIX",
    "QUALITY_CHECK_RESULTS",
    "NotBuildableError",
    "RELEASE_REQUIRED_KEYS",
    "STORE_PRODUCING_STEPS",
    "SUPERPOPULATIONS",
    "TRAIT_ONTOLOGY_ID_SEPARATOR",
    "ToleratedGap",
    "VALIDATED_RECORD_STATUSES",
    "VALID_STATUSES",
    "build_refusal",
    "check",
    "column_map_path",
    "is_authority_name_trait_ontology_label",
    "is_buildable",
    "is_canonical_trait_ontology_id",
    "is_completion_pending",
    "is_gene_shaped_trait_ontology_id",
    "is_legal_status_transition",
    "is_trait_ontology_curie",
    "is_valid_column_map_file",
    "load",
    "membership_ids",
    "partition_buildable",
    "partition_membership",
    "refuse_unbuildable",
    "reserved_options",
    "summarise",
    "trait_ontology_id_terms",
    "unrenderable_options",
    "validate_status_transition",
]
