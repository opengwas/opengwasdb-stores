"""Load and validate Release Bundles without inspecting Store artifacts.

``check()`` owns registry-side structure only: bundle identity and provenance,
declared bundle files, checksum syntax, Release Status, lineage, and the shared
``analyses.tsv`` contract. It accumulates errors and never raises, so CI and a
Manifest Generator can report every defect from one pass. It does not resolve
source files, construct artifact paths, or open a Store.

See docs/spec/store-release-workflow.md and ADRs 0017, 0022, 0023, and 0028.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml
from opengwasdb.model import analyses as opengwasdb_analyses

from ogstores.paths import is_valid_store_id

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

# Release-level identity and provenance required by the current bundle contract.
# ``build_environment`` remains optional because it is historical run evidence,
# not provenance needed to identify or regenerate the accepted input.
RELEASE_REQUIRED_KEYS: tuple[str, ...] = (
    "store_id",
    "label",
    "status",
    "source_collection_id",
    "source_snapshot_id",
    "association_coverage",
    "release_kind",
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

PHASE_B_REQUIRED_COLUMNS: tuple[str, ...] = (
    "assigned_ancestry",
    "ancestry_assignment_method",
    "stored_effect_scale",
    "original_sd_method",
)

# Store-level descriptive metadata that is derived from the membership table.
# Names intentionally match their source columns: in particular, ``context``
# is not renamed to ``assay`` because the Analysis contract has no assay field.
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
        return self.release.get("status")

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
    def validation_path(self) -> Path | None:
        path = self.root / "validation.yaml"
        return path if path.is_file() else None


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


def summarise(bundle: Bundle) -> dict[str, str | int]:
    """Derive a Store Release summary solely from its Analysis membership.

    The function reads ``analyses.tsv`` and no Store or Release Artifact. It
    preserves absence as ``NA`` and raw constant values verbatim; it never
    substitutes metadata from ``release.yaml`` or a Build Recipe.
    """
    table = opengwasdb_analyses.read_analyses(bundle.analyses_path)
    summary: dict[str, str | int] = {"n_analyses": len(table.rows)}
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
        elif _is_missing(generator, "name"):
            errors.append("release.yaml generator is missing required key: 'name'")
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

    sidecars = release.get("sidecars")
    if sidecars is not None and not isinstance(sidecars, Mapping):
        errors.append("release.yaml sidecars must be a mapping")
    elif isinstance(sidecars, Mapping):
        for name, relative in sidecars.items():
            if not isinstance(relative, str) or not relative.strip():
                errors.append(f"declared sidecar {name!r} must be a relative path")
                continue
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts:
                errors.append(
                    f"declared sidecar {name!r} must stay inside the bundle: "
                    f"{relative!r}"
                )
                continue
            try:
                sidecar_path = Path(bundle.root) / path
                if sidecar_path.is_symlink():
                    errors.append(
                        f"declared sidecar file {name!r} must not be a symbolic link: "
                        f"{sidecar_path}"
                    )
                elif not sidecar_path.is_file():
                    errors.append(
                        f"declared sidecar file {name!r} does not exist: "
                        f"{sidecar_path}"
                    )
            except OSError as exc:
                errors.append(f"declared sidecar file {name!r} could not be inspected: {exc}")

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
    if completion_state in {"observed_only", "reference_completed"}:
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
    build = bundle.build if isinstance(bundle.build, Mapping) else {}
    parent_id = release.get("derived_from")
    completion_state = build.get("completion_state")
    errors: list[str] = []

    if completion_state == "reference_completed" and not parent_id:
        return [
            "reference_completed release requires non-empty derived_from "
            "pointing to a parent Store Release"
        ]
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
    return errors


def _check_analyses(bundle: Bundle) -> list[str]:
    errors: list[str] = []
    try:
        analyses_path = Path(bundle.analyses_path)
        if analyses_path.is_symlink():
            return [f"analyses file must not be a symbolic link: {analyses_path}"]
        if not analyses_path.is_file():
            return [f"analyses file does not exist: {bundle.analyses_path}"]
        table = opengwasdb_analyses.read_analyses(bundle.analyses_path)
    except Exception as exc:
        return [f"failed to read analyses.tsv: {type(exc).__name__}: {exc}"]

    for column in PHASE_B_REQUIRED_COLUMNS:
        if column not in table.fieldnames:
            errors.append(
                f"analyses.tsv is missing Phase B required column: {column!r}"
            )

    for column in opengwasdb_analyses.RETIRED_ANALYSIS_COLUMNS:
        if column in table.fieldnames:
            errors.append(
                f"Store Release {bundle.store_id} analyses.tsv contains retired "
                f"Analysis column {column!r}"
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

    release = bundle.release if isinstance(bundle.release, Mapping) else {}
    build = bundle.build if isinstance(bundle.build, Mapping) else {}
    completion_state = build.get("completion_state")
    block_name = "complete" if completion_state == "reference_completed" else "build"
    block = build.get(block_name)
    command = block.get("command") if isinstance(block, Mapping) else None
    allow_blank_overlay_values = (
        release.get("status") == "candidate"
        or command in {"build-ragged-besd", "complete-ragged"}
    )
    for error in analysis_errors:
        if allow_blank_overlay_values and "has no value for required column" in error:
            continue
        errors.append(error)

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
    return errors


def check(
    bundle: Bundle,
    previous_status: str | Bundle | None = None,
    registry_root: Path | str | None = None,
) -> list[str]:
    """Return every registry-side error found in ``bundle``; never raise.

    The check reads files contained in the Release Bundle and other registered
    bundles needed to resolve lineage. It never resolves source paths, uses an
    artifact-root helper, or opens/inspects a built Store.
    """
    errors: list[str] = []
    stages: tuple[tuple[str, Callable[[], list[str]]], ...] = (
        ("bundle identity", lambda: _check_identity(bundle)),
        ("release.yaml", lambda: _check_release(bundle)),
        ("build.yaml", lambda: _check_build(bundle)),
        ("Release Status transition", lambda: _check_previous_status(bundle, previous_status)),
        ("Release Lineage", lambda: _check_lineage(bundle, registry_root)),
        ("validation.yaml", lambda: _check_validation(bundle)),
        ("analyses.tsv", lambda: _check_analyses(bundle)),
    )
    for label, stage in stages:
        try:
            errors.extend(stage())
        except Exception as exc:  # The public contract is diagnostic, never exceptional.
            errors.append(f"{label} could not be checked: {type(exc).__name__}: {exc}")
    return errors


__all__ = [
    "BUILD_REQUIRED_KEYS",
    "Bundle",
    "LEGAL_STATUS_TRANSITIONS",
    "PHASE_B_REQUIRED_COLUMNS",
    "RELEASE_REQUIRED_KEYS",
    "VALID_STATUSES",
    "check",
    "is_legal_status_transition",
    "load",
    "validate_status_transition",
]
