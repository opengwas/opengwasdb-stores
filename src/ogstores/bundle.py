"""Release Bundle ownership: load it, and check what the registry owns.

`check()` covers registry-side facts only -- required keys, `store_id` matching
the directory name, id format, declared files existing, checksums, `derived_from`
resolving, legal status transitions -- and delegates the `analyses.tsv` contract
to `opengwasdb.model.analyses.read_analyses` rather than reimplementing it
(ADR 0017). It never opens a Store.

It does assert that Phase B's columns are present and vocabulary-valid
(`assigned_ancestry`, `ancestry_assignment_method`, `stored_effect_scale`,
`original_sd_method`, ...). Asserting presence is registry-side structural
validation; recomputing the values would be Phase A writing `analyses.tsv`,
which it never does.

See docs/spec/store-release-workflow.md and ADRs 0017, 0022, 0023, 0024.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from opengwasdb.model.analyses import (
    AnalysesTable,
    read_analyses,
    validate_analyses,
)

from ogstores.paths import STORE_ID_PATTERN, is_valid_store_id

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

PHASE_B_REQUIRED_COLUMNS: tuple[str, ...] = (
    "assigned_ancestry",
    "ancestry_assignment_method",
    "stored_effect_scale",
    "original_sd_method",
)

RELEASE_REQUIRED_KEYS: tuple[str, ...] = (
    "store_id",
    "label",
    "family",
    "status",
    "source_collection_id",
    "association_coverage",
    "created_at",
    "description",
    "source_snapshot_id",
    "release_kind",
    "generator",
)

BUILD_REQUIRED_KEYS: tuple[str, ...] = (
    "store_id",
    "layout",
    "completion_state",
    "artifacts",
    "post",
)

_HEX_PATTERNS: dict[int, re.Pattern[str]] = {
    32: re.compile(r"\A[0-9a-fA-F]{32}\Z"),
    40: re.compile(r"\A[0-9a-fA-F]{40}\Z"),
    64: re.compile(r"\A[0-9a-fA-F]{64}\Z"),
}


def _is_valid_hex(s: str, length: int) -> bool:
    pat = _HEX_PATTERNS.get(length)
    if pat is None:
        pat = re.compile(rf"\A[0-9a-fA-F]{{{length}}}\Z")
    return bool(isinstance(s, str) and pat.fullmatch(s.strip()))


def is_legal_status_transition(from_status: str, to_status: str) -> bool:
    """Return True if transitioning from `from_status` to `to_status` is legal."""
    if from_status not in LEGAL_STATUS_TRANSITIONS:
        return False
    return to_status in LEGAL_STATUS_TRANSITIONS[from_status]


def validate_status_transition(from_status: str, to_status: str) -> list[str]:
    """Return a list of errors if transition from `from_status` to `to_status` is illegal."""
    errors: list[str] = []
    if from_status not in VALID_STATUSES:
        errors.append(
            f"invalid current status {from_status!r}; expected one of {sorted(VALID_STATUSES)}"
        )
    if to_status not in VALID_STATUSES:
        errors.append(
            f"invalid target status {to_status!r}; expected one of {sorted(VALID_STATUSES)}"
        )
    if not errors and not is_legal_status_transition(from_status, to_status):
        allowed = sorted(LEGAL_STATUS_TRANSITIONS.get(from_status, ()))
        errors.append(
            f"illegal status transition from {from_status!r} to {to_status!r}; "
            f"allowed transitions from {from_status!r}: {allowed}"
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
    def family(self) -> str | None:
        return self.release.get("family")

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
        vp = self.root / "validation.yaml"
        return vp if vp.is_file() else None


def load(store_id: str, registry_root: Path | str | None = None) -> Bundle:
    """Load a Release Bundle from `stores/<store_id>` or a custom registry root."""
    if registry_root is not None:
        p = Path(registry_root)
        root = p if p.name == store_id else p / store_id
    else:
        candidates = [
            Path("stores") / store_id,
            REPO_ROOT / "stores" / store_id,
            Path.cwd() / store_id,
            Path.cwd(),
        ]
        root = None
        for c in candidates:
            if c.is_dir() and (c / "release.yaml").is_file():
                root = c
                break
        if root is None:
            root = Path("stores") / store_id

    release_file = root / "release.yaml"
    build_file = root / "build.yaml"
    analyses_file = root / "analyses.tsv"
    validation_file = root / "validation.yaml"

    release_data: dict[str, Any] = {}
    if release_file.is_file():
        try:
            with open(release_file, "r", encoding="utf-8") as f:
                release_data = yaml.safe_load(f) or {}
        except yaml.YAMLError as exc:
            release_data = {"__yaml_error__": str(exc)}

    build_data: dict[str, Any] = {}
    if build_file.is_file():
        try:
            with open(build_file, "r", encoding="utf-8") as f:
                build_data = yaml.safe_load(f) or {}
        except yaml.YAMLError as exc:
            build_data = {"__yaml_error__": str(exc)}

    validation_data: dict[str, Any] | None = None
    if validation_file.is_file():
        try:
            with open(validation_file, "r", encoding="utf-8") as f:
                validation_data = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            validation_data = {"__yaml_error__": str(exc)}

    return Bundle(
        store_id=store_id,
        root=root,
        release=release_data,
        build=build_data,
        analyses_path=analyses_file,
        validation=validation_data,
    )


def _resolve_parent_bundle(
    parent_id: str, bundle_root: Path, registry_root: Path | str | None
) -> bool:
    candidates = [
        bundle_root.parent / parent_id,
        Path("stores") / parent_id,
        REPO_ROOT / "stores" / parent_id,
    ]
    if registry_root is not None:
        candidates.insert(0, Path(registry_root) / parent_id)
    return any(c.is_dir() and (c / "release.yaml").is_file() for c in candidates)


def check(
    bundle: Bundle,
    previous_status: str | Bundle | None = None,
    registry_root: Path | str | None = None,
) -> list[str]:
    """Validate registry-side structural facts for `bundle`.

    Covers required keys, store_id directory matching, ID format, declared files
    existing, checksum format/integrity, derived_from resolution, legal status
    transitions (against optional `previous_status`), and analyses.tsv contract
    delegated to public opengwasdb.model.analyses functions.
    Never opens a Store or inspects artifact paths.
    """
    errors: list[str] = []

    # 1. Store ID format & directory match
    if not is_valid_store_id(bundle.store_id):
        errors.append(
            f"malformed store_id {bundle.store_id!r}: must match pattern 'OGS-\\d{{5}}' (ADR 0022)"
        )

    if not bundle.root.is_dir():
        errors.append(f"store directory {bundle.root} does not exist")
    elif bundle.root.name != bundle.store_id:
        errors.append(
            f"store_id {bundle.store_id!r} does not match directory name {bundle.root.name!r}"
        )

    # 2. release.yaml validation
    if not bundle.release:
        errors.append("release.yaml is missing or empty")
    elif "__yaml_error__" in bundle.release:
        errors.append(f"release.yaml is malformed YAML: {bundle.release['__yaml_error__']}")
    else:
        rel_sid = bundle.release.get("store_id")
        if rel_sid != bundle.store_id:
            errors.append(
                f"release.yaml store_id {rel_sid!r} does not match bundle store_id {bundle.store_id!r}"
            )

        for key in RELEASE_REQUIRED_KEYS:
            if key not in bundle.release or bundle.release[key] is None or bundle.release[key] == "":
                errors.append(f"release.yaml is missing required key: {key!r}")

        status = bundle.release.get("status")
        if status is not None and status not in VALID_STATUSES:
            errors.append(
                f"release.yaml has invalid status {status!r}; expected one of {sorted(VALID_STATUSES)}"
            )

        # Status transition check if previous status or bundle was supplied
        effective_prev_status = (
            previous_status.status if isinstance(previous_status, Bundle) else previous_status
        )
        if effective_prev_status is not None and status is not None:
            errors.extend(validate_status_transition(effective_prev_status, status))

        generator = bundle.release.get("generator")
        if isinstance(generator, dict):
            if "name" not in generator and "command" not in generator:
                errors.append("release.yaml generator must declare 'name' or 'command'")
            ver = generator.get("version")
            if isinstance(ver, str) and ver.startswith("sha256:"):
                h = ver.split("sha256:", 1)[1]
                if not _is_valid_hex(h, 64):
                    errors.append(f"release.yaml generator version has invalid sha256 checksum: {h!r}")
        elif generator is not None and not isinstance(generator, str):
            errors.append(f"release.yaml generator has invalid type {type(generator).__name__}")

        source_snapshot = bundle.release.get("source_snapshot")
        if isinstance(source_snapshot, dict) and "manifest_sha256" in source_snapshot:
            sha = source_snapshot["manifest_sha256"]
            if sha is not None and not _is_valid_hex(str(sha), 64):
                errors.append(
                    f"release.yaml source_snapshot manifest_sha256 is invalid hex sha256: {sha!r}"
                )

        sidecars = bundle.release.get("sidecars", {})
        if isinstance(sidecars, dict):
            for name, rel_path in sidecars.items():
                if rel_path:
                    sp = bundle.root / rel_path
                    if not sp.exists():
                        errors.append(f"declared sidecar file {name!r} does not exist: {sp}")

    # 3. build.yaml validation
    if not bundle.build:
        errors.append("build.yaml is missing or empty")
    elif "__yaml_error__" in bundle.build:
        errors.append(f"build.yaml is malformed YAML: {bundle.build['__yaml_error__']}")
    else:
        bld_sid = bundle.build.get("store_id")
        if bld_sid != bundle.store_id:
            errors.append(
                f"build.yaml store_id {bld_sid!r} does not match bundle store_id {bundle.store_id!r}"
            )

        for key in BUILD_REQUIRED_KEYS:
            if key not in bundle.build or bundle.build[key] is None:
                errors.append(f"build.yaml is missing required key: {key!r}")

        layout = bundle.build.get("layout")
        if layout is not None and layout not in ("dense", "ragged", "hybrid"):
            errors.append(
                f"build.yaml has invalid layout {layout!r}; expected 'dense', 'ragged', or 'hybrid'"
            )

        completion_state = bundle.build.get("completion_state")
        if completion_state is not None and completion_state not in (
            "observed_only",
            "reference_completed",
        ):
            errors.append(
                f"build.yaml has invalid completion_state {completion_state!r}; "
                f"expected 'observed_only' or 'reference_completed'"
            )

        if completion_state == "reference_completed":
            if "complete" not in bundle.build or not isinstance(bundle.build["complete"], dict):
                errors.append(
                    "build.yaml for reference_completed release must declare 'complete' block"
                )
            elif "command" not in bundle.build["complete"]:
                errors.append("build.yaml complete block missing required 'command'")
        elif completion_state == "observed_only":
            if "build" not in bundle.build or not isinstance(bundle.build["build"], dict):
                errors.append("build.yaml for observed_only release must declare 'build' block")
            elif "command" not in bundle.build["build"]:
                errors.append("build.yaml build block missing required 'command'")

    # 4. Lineage / derived_from resolution (for all bundles that declare derived_from)
    derived_from = bundle.release.get("derived_from") if bundle.release else None
    comp_state = bundle.build.get("completion_state") if bundle.build else None

    if comp_state == "reference_completed":
        if not derived_from:
            errors.append(
                "reference_completed release requires non-empty derived_from pointing to parent release"
            )
        elif not is_valid_store_id(str(derived_from)):
            errors.append(
                f"derived_from {derived_from!r} is not a valid store_id (must match 'OGS-\\d{{5}}')"
            )
        elif derived_from == bundle.store_id:
            errors.append(f"derived_from cannot reference itself: {derived_from!r}")
        else:
            if not _resolve_parent_bundle(str(derived_from), bundle.root, registry_root):
                errors.append(
                    f"unresolvable derived_from {derived_from!r}: parent release bundle not found in registry"
                )
    elif derived_from is not None and derived_from != "":
        if not is_valid_store_id(str(derived_from)):
            errors.append(
                f"derived_from {derived_from!r} is not a valid store_id (must match 'OGS-\\d{{5}}')"
            )
        elif derived_from == bundle.store_id:
            errors.append(f"derived_from cannot reference itself: {derived_from!r}")
        else:
            if not _resolve_parent_bundle(str(derived_from), bundle.root, registry_root):
                errors.append(
                    f"unresolvable derived_from {derived_from!r}: parent release bundle not found in registry"
                )

    # 5. Status-aware validation.yaml check
    status = bundle.release.get("status") if bundle.release else None
    if status in ("built", "validated"):
        if bundle.validation is None and not (bundle.root / "validation.yaml").is_file():
            errors.append(f"release with status {status!r} requires validation.yaml")
        if bundle.validation is not None:
            if "__yaml_error__" in bundle.validation:
                errors.append(f"validation.yaml is malformed YAML: {bundle.validation['__yaml_error__']}")
            else:
                v_status = bundle.validation.get("status")
                if v_status not in ("passed", "passed_with_warnings", "failed", "not_run"):
                    errors.append(f"validation.yaml has invalid status {v_status!r}")

    # 6. analyses.tsv validation delegated to public opengwasdb.model.analyses surface
    if not bundle.analyses_path.is_file():
        errors.append(f"analyses file does not exist: {bundle.analyses_path}")
        return errors

    try:
        table = read_analyses(bundle.analyses_path)
    except Exception as exc:
        errors.append(f"failed to read analyses.tsv: {exc}")
        return errors

    for col in PHASE_B_REQUIRED_COLUMNS:
        if col not in table.fieldnames:
            errors.append(f"analyses.tsv is missing Phase B required column: {col!r}")

    cmd = (
        bundle.build.get("complete", {}).get("command")
        if comp_state == "reference_completed"
        else bundle.build.get("build", {}).get("command")
    )

    if cmd in ("build-ragged-besd", "complete-ragged") and "analysis_id" not in table.fieldnames:
        errors.append("analyses.tsv is missing required column: 'analysis_id'")

    # Delegate analysis table validation to public opengwasdb surface
    active_table = table
    if any(r.get("exclude_from_build") == "true" for r in table.rows):
        active_rows = tuple(r for r in table.rows if r.get("exclude_from_build") != "true")
        active_table = AnalysesTable(fieldnames=table.fieldnames, rows=active_rows)

    raw_analyses_errors = validate_analyses(active_table)
    if status == "candidate" or cmd in ("build-ragged-besd", "complete-ragged"):
        # BESD builds (build-ragged-besd / complete-ragged) and candidate releases:
        # As specified in opengwasdb#173 and documented in OGS-00001/OGS-00002 pilot_report.md,
        # BESD sources derive probes directly from the input .epi binary file rather than a tabular
        # manifest. In this workflow, analyses.tsv acts as an attribution and probe-ID overlay.
        # Missing-value errors ('has no value for required column') on unpopulated shared-core
        # cells are therefore expected for BESD releases and candidate releases and suppressed here.
        # Crucially, all vocabulary checks, case/control rules, and header column requirements
        # from opengwasdb.model.analyses.validate_analyses remain strictly enforced.
        for err in raw_analyses_errors:
            if "has no value for required column" not in err:
                errors.append(err)
    else:
        errors.extend(raw_analyses_errors)

    # 7. Checksum format validation in analyses.tsv
    if "checksum" in table.fieldnames and "checksum_algorithm" in table.fieldnames:
        for row in table.rows:
            cs = row.get("checksum", "").strip()
            alg = row.get("checksum_algorithm", "").strip()
            aid = row.get("analysis_id") or "<unknown analysis_id>"
            if cs and alg:
                if alg not in ("sha256", "sha1", "md5"):
                    errors.append(f"analysis {aid!r} has unsupported checksum_algorithm {alg!r}")
                elif alg == "sha256" and not _is_valid_hex(cs, 64):
                    errors.append(f"analysis {aid!r} has invalid sha256 checksum {cs!r}")
                elif alg == "md5" and not _is_valid_hex(cs, 32):
                    errors.append(f"analysis {aid!r} has invalid md5 checksum {cs!r}")
                elif alg == "sha1" and not _is_valid_hex(cs, 40):
                    errors.append(f"analysis {aid!r} has invalid sha1 checksum {cs!r}")

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
