#!/usr/bin/env python3
"""Release-plan loader for the executable Store Release `build.yaml` (issue #95).

Reads one Store Release directory's `build.yaml` and validates it into a
`ReleasePlan` before anything expensive runs:

    python3 resources/lib/release_plan.py families/finngen-r13/releases/r13-pilot-20

Two shapes are accepted. The executable (CLI) schema evolves the pre-#95 bundle
in place rather than introducing a second filename (ADR 0022), and #97 migrated
the seven Trial Store Releases onto it:

* **CLI schema** - `build.command` names an ``opengwasdb`` CLI subcommand
  (``opengwasdb --help``), `build.arguments` is an *opaque* flag mapping passed
  through unchanged, `source.root`/`source.analyses` fix the input, and the
  optional `rho` / `reference_completion` branches say whether those in-place
  and child releases are built.
* **legacy schema** - the pre-#95 bundle with `builder.entrypoint`, accepted so
  the loader never rejects a release #97 did not migrate. Legacy plans carry an
  explicit warning and are not executable by command name until migrated.

Validation refuses, naming the offending key:

* a `build.command` that is not an ``opengwasdb`` CLI subcommand;
* rho enabled on a layout with no rho implementation (rho is Dense-only);
* a Reference Resource referenced by `ancestry_assignment` or
  `effect_scale_validation` that is not declared in `reference_resources`;
* a source file named in `analyses.tsv` that is missing, or whose declared
  checksum does not match;
* a `source.root`/`source.analyses` that does not exist.

`source.root` and `source.analyses` are resolved relative to the release
directory unless absolute, and every source path in `analyses.tsv` is resolved
relative to `source.root` unless absolute (so the existing absolute-path
manifests keep working). Rows with `exclude_from_build` set are not checked;
they are not read by the build.

YAML is read with `resources.lib.release_yaml.read_release_yaml`, the same
block-style subset reader every generator uses for these bundles, so the loader
and the build agree on what a file means. It understands block mappings and
sequences, quoted/bare scalars, `~`/`null`, `yes`/`no`, and `[]`, but not inline
flow mappings or multi-line block scalars.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from resources.lib.release_yaml import read_release_yaml, read_tsv  # noqa: E402

# Store layouts are named `<layout>-<completion state>`; only Dense has a rho
# implementation (there is no Hybrid or Ragged rho).
_DENSE_LAYOUT = "dense"

_TRUE_STRINGS = {"true", "yes", "1", "on"}

# Columns that name an analysis's source file, in preference order. Real
# bundles emit `source_file`; the workflow prototype's fixture used `file_name`.
_SOURCE_FILE_COLUMNS = ("source_file", "file_name")


class PlanError(Exception):
    """A `build.yaml` that cannot be loaded, naming the offending key."""

    def __init__(self, key: str, message: str) -> None:
        self.key = key
        super().__init__(f"{key}: {message}")


@dataclass(frozen=True)
class ReleasePlan:
    """A validated Store Release build plan."""

    release_dir: Path
    schema: str  # "cli" or "legacy"
    store_family_id: str | None
    family_release_id: str | None
    store_layout: str | None
    source_root: Path | None
    analyses_path: Path | None
    build_command: str | None
    build_arguments: dict[str, object] = field(default_factory=dict)
    rho_enabled: bool = False
    reference_completion_enabled: bool = False
    completed_release_id: str | None = None
    completion_command: str | None = None
    builder_entrypoint: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReleaseCheck:
    """The outcome of loading and checking one release directory."""

    plan: ReleasePlan | None
    ok: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]


@lru_cache(maxsize=1)
def known_commands() -> frozenset[str]:
    """The set of ``opengwasdb`` CLI subcommand names, read from its Typer app."""
    try:
        from opengwasdb.cli.main import app
    except ImportError as exc:  # pragma: no cover - environment without opengwasdb
        raise PlanError("build.command", f"cannot enumerate opengwasdb CLI subcommands: {exc}") from exc
    names: set[str] = set()
    for command in app.registered_commands:
        name = command.name
        if not name and command.callback is not None:
            name = command.callback.__name__.replace("_", "-")
        if name:
            names.add(name)
    return frozenset(names)


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_STRINGS
    return bool(value)


def _mapping(data: dict, key: str) -> dict:
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PlanError(key, "must be a mapping")
    return value


def _resolve(release_dir: Path, value: object, *, key: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise PlanError(key, "must be a non-empty path")
    path = Path(value)
    return path if path.is_absolute() else release_dir / path


def _declared_resource_ids(data: dict) -> set[str]:
    resources = data.get("reference_resources")
    if resources is None:
        return set()
    if not isinstance(resources, list):
        raise PlanError("reference_resources", "must be a list of Reference Resource declarations")
    declared: set[str] = set()
    for index, resource in enumerate(resources):
        if not isinstance(resource, dict) or not resource.get("resource_id"):
            raise PlanError(f"reference_resources[{index}].resource_id", "missing resource_id")
        declared.add(str(resource["resource_id"]))
    return declared


def _referenced_resource_ids(data: dict) -> list[tuple[str, str]]:
    """Return `(key, resource_id)` for every Reference Resource pointer in the plan."""
    references: list[tuple[str, str]] = []
    ancestry = _mapping(data, "ancestry_assignment")
    if ancestry.get("reference_resource_id"):
        references.append(("ancestry_assignment.reference_resource_id", str(ancestry["reference_resource_id"])))
    for section, field_name in (("ancestry_assignment", "reference_resources"), ("effect_scale_validation", "reference_resources")):
        entries = _mapping(data, section).get(field_name)
        if entries is None:
            continue
        if not isinstance(entries, list):
            raise PlanError(f"{section}.{field_name}", "must be a list")
        for index, entry in enumerate(entries):
            if isinstance(entry, dict) and entry.get("resource_id"):
                references.append((f"{section}.{field_name}[{index}].resource_id", str(entry["resource_id"])))
    return references


def _check_references(data: dict, declared: set[str]) -> None:
    for key, resource_id in _referenced_resource_ids(data):
        if resource_id not in declared:
            raise PlanError(key, f"Reference Resource {resource_id!r} is not declared in reference_resources")


def _layout(data: dict, command: str | None) -> str | None:
    layout = data.get("store_layout")
    if layout:
        return str(layout)
    if command:
        for token in command.split("-"):
            if token in {"dense", "hybrid", "ragged"}:
                return token
    return None


def load_plan(release_dir: Path) -> ReleasePlan:
    """Read and validate `<release_dir>/build.yaml`, or raise `PlanError`."""
    build_yaml = release_dir / "build.yaml"
    if not build_yaml.is_file():
        raise PlanError("build.yaml", f"{build_yaml} not found")
    data = read_release_yaml(build_yaml)

    build = _mapping(data, "build")
    builder = _mapping(data, "builder")
    command = build.get("command")
    entrypoint = builder.get("entrypoint")

    if command:
        schema = "cli"
        warnings: tuple[str, ...] = ()
    elif entrypoint:
        schema = "legacy"
        warnings = (
            "legacy build.yaml uses builder.entrypoint, not build.command; "
            "not executable by CLI name until migrated (issue #97)",
        )
    else:
        raise PlanError(
            "build.command",
            "build.yaml declares neither build.command (CLI schema) nor builder.entrypoint (legacy schema)",
        )

    declared = _declared_resource_ids(data)
    _check_references(data, declared)

    source = _mapping(data, "source")
    rho = _mapping(data, "rho")
    rho_enabled = _as_bool(rho.get("enabled", False))
    reference_completion = _mapping(data, "reference_completion")
    completion_enabled = _as_bool(reference_completion.get("enabled", False))
    completion_command = reference_completion.get("command")
    completed_release_id = reference_completion.get("family_release_id")

    arguments = build.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise PlanError("build.arguments", "must be a mapping of CLI flags (opaque passthrough)")

    if schema == "cli" and str(command) not in known_commands():
        raise PlanError("build.command", f"unknown opengwasdb CLI subcommand {command!r}")

    resolved_layout = _layout(data, str(command) if command else None)
    if rho_enabled and (resolved_layout is None or resolved_layout.split("-")[0] != _DENSE_LAYOUT):
        raise PlanError(
            "rho.enabled",
            f"rho is Dense-only; store layout {resolved_layout!r} has no rho implementation",
        )

    if schema == "cli":
        if completion_enabled:
            if not completed_release_id:
                raise PlanError("reference_completion.family_release_id", "required when reference_completion is enabled")
            if not completion_command:
                raise PlanError("reference_completion.command", "required when reference_completion is enabled")
            if str(completion_command) not in known_commands():
                raise PlanError(
                    "reference_completion.command",
                    f"unknown opengwasdb CLI subcommand {completion_command!r}",
                )
        source_root = _resolve(release_dir, source.get("root"), key="source.root")
        analyses_path = _resolve(release_dir, source.get("analyses"), key="source.analyses")
    else:
        artifacts = _mapping(data, "artifacts")
        if artifacts.get("source_dir"):
            source_root = _resolve(release_dir, artifacts["source_dir"], key="artifacts.source_dir")
        else:
            candidate = release_dir / "source"
            source_root = candidate if candidate.is_dir() else None
        analyses_path = release_dir / "analyses.tsv"

    return ReleasePlan(
        release_dir=release_dir,
        schema=schema,
        store_family_id=str(data["store_family_id"]) if data.get("store_family_id") else None,
        family_release_id=str(data["family_release_id"]) if data.get("family_release_id") else None,
        store_layout=resolved_layout,
        source_root=source_root,
        analyses_path=analyses_path,
        build_command=str(command) if command else None,
        build_arguments=arguments,
        rho_enabled=rho_enabled,
        reference_completion_enabled=completion_enabled,
        completed_release_id=str(completed_release_id) if completed_release_id else None,
        completion_command=str(completion_command) if completion_command else None,
        builder_entrypoint=str(entrypoint) if entrypoint else None,
        warnings=warnings,
    )


def _hash_file(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_file_column(rows: list[dict[str, str]]) -> str | None:
    columns = rows[0].keys() if rows else []
    for candidate in _SOURCE_FILE_COLUMNS:
        if candidate in columns:
            return candidate
    return None


def _check_sources(
    plan: ReleasePlan, errors: list[str], warnings: list[str], *, verify_checksums: bool
) -> None:
    root = plan.source_root
    root_present = root is not None and root.is_dir()
    if not root_present:
        if plan.schema == "cli":
            errors.append(f"source.root: {root} does not exist")
            return
        # Legacy bundles acquired their inputs outside the repository (ADR
        # 0015), so an absent source directory is unverifiable, not a failure.
        warnings.append(f"source.root {root} not present; skipped source-file checks for legacy release")

    analyses = plan.analyses_path
    if analyses is None or not analyses.is_file():
        errors.append(f"source.analyses: {analyses} not found")
        return

    rows = read_tsv(analyses)
    if not rows:
        warnings.append(f"{analyses} names no analyses")
        return
    file_column = _source_file_column(rows)
    if file_column is None:
        warnings.append(f"{analyses} names no source files; skipped source-file checks")
        return
    if not root_present:
        # Legacy with a named file column but no local source directory: the
        # manifest is still readable, but its files cannot be checked here.
        return

    assert root is not None
    for index, row in enumerate(rows):
        if _as_bool(row.get("exclude_from_build")):
            continue
        analysis_id = row.get("analysis_id") or f"row {index + 2}"
        value = (row.get(file_column) or "").strip()
        if not value:
            errors.append(f"source.analyses: {analysis_id} names no source file")
            continue
        source_file = Path(value)
        if not source_file.is_absolute():
            source_file = root / source_file
        if not source_file.is_file():
            errors.append(f"source.analyses: {analysis_id} source file not found: {source_file}")
            continue
        checksum = (row.get("checksum") or "").strip()
        if not (verify_checksums and checksum):
            continue
        algorithm = (row.get("checksum_algorithm") or "sha256").strip().lower() or "sha256"
        try:
            actual = _hash_file(source_file, algorithm)
        except ValueError:
            errors.append(f"source.analyses: {analysis_id} unsupported checksum_algorithm {algorithm!r}")
            continue
        if actual != checksum.lower():
            errors.append(
                f"source.analyses: {analysis_id} checksum mismatch for {source_file} "
                f"({algorithm} expected {checksum.lower()}, got {actual})"
            )


def _release_dir(location: Path) -> Path:
    if location.is_file():
        return location.parent
    return location


def check_release(location: Path, *, verify_checksums: bool = True) -> ReleaseCheck:
    """Load and check a release directory (or a `build.yaml` path)."""
    release_dir = _release_dir(Path(location))
    try:
        plan = load_plan(release_dir)
    except PlanError as exc:
        return ReleaseCheck(plan=None, ok=False, errors=(str(exc),), warnings=())

    errors: list[str] = []
    warnings: list[str] = list(plan.warnings)
    _check_sources(plan, errors, warnings, verify_checksums=verify_checksums)
    return ReleaseCheck(plan=plan, ok=not errors, errors=tuple(errors), warnings=tuple(warnings))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report whether a Store Release build.yaml is buildable.")
    parser.add_argument(
        "release",
        help="Store Release directory containing build.yaml (a direct build.yaml path is also accepted)",
    )
    parser.add_argument(
        "--no-checksums",
        action="store_true",
        help="skip source-file checksum verification (structural checks only)",
    )
    args = parser.parse_args(argv)

    result = check_release(Path(args.release), verify_checksums=not args.no_checksums)
    if result.plan is not None:
        identity = result.plan.family_release_id or result.plan.release_dir.name
        print(f"{'PASS' if result.ok else 'FAIL'} {identity} ({result.plan.schema} schema)")
        if result.plan.build_command:
            print(f"  build.command: {result.plan.build_command}")
    else:
        print(f"FAIL {args.release}")
    for warning in result.warnings:
        print(f"  warning: {warning}")
    for error in result.errors:
        print(f"  error: {error}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
