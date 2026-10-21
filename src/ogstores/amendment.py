"""The one amendment an accepted Release Bundle allows (ADR 0037, #305).

An accepted Release Bundle is immutable, with one exception: an Observed-Only
bundle may gain a `complete` block and switch to `completion_state:
reference_completed`, so that a Store built and published before it declared
completion can be completed later under its id. `refuse_changed_bundles`
holds such a bundle, one that completes its own Store
(`Bundle.completes_own_store`), to the Store it published, before anything is
written, and names what changed.

The baseline is the published release's records, never its Store (ADR 0023),
and only what made that Store:

- once the build phase has published (`records/register.json`), the steps
  that write its Store, `variant-reference` and `build`, must be planned as
  their records planned them (`planned_argv`, which `register` held the
  executed argv to), and the manifest the bundle's `analyses.tsv` makes now
  must be the one the Store was built from (`work/analyses.tsv`), excluding
  the same Analyses;
- once the completion phase has published (`records/completion-register.json`),
  the same for its `complete`.

Post steps (`top-hits`, `rho`, `indexed-subset-*`, `overview`, `validate`) are
re-runnable in-place additions (ADR 0034, opengwasdb ADR 0061), so a new
declaration, or a planner that has moved on since, is not a change to the
Store. Observed-only and two-id releases are not checked here, as before #305,
and `release.yaml`'s prose and provenance are left to review for every bundle.
Gaining a `complete` block passes: the build phase plans what the bundle
planned as `observed_only` (`plan._plan_completing_own_store`).

A record names the artifact root it was written under (`record_path`), and the
bundle is planned under that root for the comparison, so a release copied to
another root still matches. `--config force=1` keeps its meaning: a forced run
may rebuild from a changed bundle, so a forced release is not checked.
"""

from __future__ import annotations

import csv
import json
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ogstores import bundle as bundle_module
from ogstores import manifest, paths, run
from ogstores.bundle import Bundle
from ogstores.plan import BUILD_PHASE, REGISTER_STEPS, Step, plan

# The steps whose argv made a published Store; every other step is a
# re-runnable in-place addition to it.
STORE_WRITING_STEPS: frozenset[str] = run.STORE_PRODUCING_STEPS | {run.VARIANT_REFERENCE_STEP}


class AmendmentError(RuntimeError):
    """A bundle that completes its own Store no longer plans the Store it published."""


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def _record_root(record: dict[str, Any], store_id: str, default: Path) -> Path:
    """The artifact root a record was written under, from its own `record_path`."""
    record_path = Path(str(record.get("record_path") or ""))
    if record_path.parent.name == "records" and record_path.parent.parent.name == store_id:
        return record_path.parents[2]
    return default


def _phase_changes(b: Bundle, phase: str, register: dict[str, Any], root: Path) -> list[str]:
    """How the bundle's plan of one published phase's Store-writing steps differs from their records."""
    store_id = b.store_id
    plans: dict[Path, dict[str, Step]] = {}

    def planned_at(record_root: Path) -> dict[str, Step]:
        if record_root not in plans:
            plans[record_root] = {step.name: step for step in plan(b, artifact_root=record_root) if step.phase == phase}
        return plans[record_root]

    phase_plan = planned_at(_record_root(register, store_id, root))
    if not phase_plan:
        return [f"{store_id}: its {phase} phase is published, but the bundle no longer plans one"]
    planned = [name for name in phase_plan if name in STORE_WRITING_STEPS]
    registered = [Path(str(p)).stem for p in register.get("inputs") or []]
    registered = [name for name in registered if name in STORE_WRITING_STEPS]
    changes = [
        f"{store_id}: {phase} phase step {name!r} is planned, but its published release has no such step"
        for name in planned
        if name not in registered
    ]
    changes += [
        f"{store_id}: {phase} phase step {name!r} was published, but the bundle no longer plans it"
        for name in registered
        if name not in planned
    ]
    for name in (n for n in registered if n in planned):
        record = _read_json(paths.record_path(store_id, name, root=root))
        if record is None or not isinstance(record.get("planned_argv"), list):
            changes.append(f"{store_id}: {phase} phase step {name!r} has no record of the argv it planned")
            continue
        step = planned_at(_record_root(record, store_id, root)).get(name)
        if step is not None and step.argv != record["planned_argv"]:
            changes.append(
                f"{store_id}: {phase} phase step {name!r}: {run.argv_change(record['planned_argv'], step.argv)}"
            )
    return changes


def _manifest_rows(path: Path) -> tuple[list[str], dict[str, dict[str, str]]]:
    with open(path, encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        rows = {row[manifest.ANALYSIS_ID_COLUMN]: row for row in reader}
        return list(reader.fieldnames or []), rows


def _excluded(sidecar: Path) -> set[str]:
    document = _read_json(sidecar) or {}
    return {str(entry.get("analysis_id")) for entry in document.get("dropped") or []}


def _membership_changes(b: Bundle, root: Path) -> list[str]:
    """How the build manifest the bundle makes now differs from the one its Store was built from."""
    store_id = b.store_id
    published = paths.build_manifest_path(store_id, root=root)
    published_sidecar = paths.build_manifest_sidecar_path(store_id, root=root)
    if not published.is_file():
        return [f"{store_id}: membership cannot be compared: no derived build manifest at {published}"]
    with tempfile.TemporaryDirectory() as scratch:
        current, current_sidecar = Path(scratch) / "analyses.tsv", Path(scratch) / "exclusions.json"
        try:
            manifest.materialise_build_manifest(b.analyses_path, current, current_sidecar)
        except manifest.ManifestError as exc:
            return [f"{store_id}: membership: {b.analyses_path} no longer makes a build manifest: {exc}"]
        if current.read_bytes() == published.read_bytes() and _excluded(current_sidecar) == _excluded(published_sidecar):
            return []
        current_columns, current_rows = _manifest_rows(current)
        published_columns, published_rows = _manifest_rows(published)
        excluded_now, excluded_then = _excluded(current_sidecar), _excluded(published_sidecar)

    details = [f"analysis {a!r} added" for a in current_rows if a not in published_rows]
    details += [f"analysis {a!r} removed" for a in published_rows if a not in current_rows]
    for analysis_id, row in current_rows.items():
        before = published_rows.get(analysis_id)
        if before is not None and before != row:
            columns = [c for c in dict.fromkeys([*published_columns, *current_columns]) if before.get(c) != row.get(c)]
            details.append(f"analysis {analysis_id!r} changed ({', '.join(columns)})")
    details += [f"analysis {a!r} newly excluded" for a in sorted(excluded_now - excluded_then)]
    details += [f"analysis {a!r} no longer excluded" for a in sorted(excluded_then - excluded_now)]
    if published_columns != current_columns and not details:
        details.append(f"columns {published_columns} became {current_columns}")
    if not details:
        details.append("the build manifest's rows were reordered")
    return [f"{store_id}: membership: {'; '.join(details)}"]


def bundle_changes(b: Bundle, artifact_root: Path | str) -> list[str]:
    """How a bundle that completes its own Store no longer plans the Store its release published.

    Empty for any other bundle, for a release that has not published, and for
    one whose bundle has only gained a `complete` block or changed its post
    steps.
    """
    if not b.completes_own_store:
        return []
    root = Path(artifact_root)
    changes: list[str] = []
    for phase, register_step in REGISTER_STEPS.items():
        register = _read_json(paths.record_path(b.store_id, register_step, root=root))
        if register is None:
            continue
        changes += _phase_changes(b, phase, register, root)
        if phase == BUILD_PHASE:
            changes += _membership_changes(b, root)
    return changes


def refuse_changed_bundles(
    store_ids: Iterable[str],
    *,
    registry_root: Path | str,
    artifact_root: Path | str,
    forced: Iterable[str] = frozenset(),
    held: Iterable[str] = frozenset(),
    parents: bool = True,
) -> frozenset[str]:
    """Refuse a run of any buildable bundle that no longer plans the Store it published (#305).

    `store_ids` are the releases the run may touch. With `parents`, the
    parents a run naming a two-id child schedules are checked with them. A
    forced release is let through, to be rebuilt from its bundle as it now
    stands.

    A release in `held`, whose lock another live run holds, is not checked
    now, because its records are that run's to rewrite, and it is returned.
    The caller must check each returned release once it holds its lock, before
    any job runs, or refuse the run: a release is never let through unchecked.
    """
    forced_ids = frozenset(forced)
    held_ids = frozenset(held)
    deferred: set[str] = set()
    pending = sorted(set(store_ids))
    seen: set[str] = set()
    changes: list[str] = []
    while pending:
        store_id = pending.pop(0)
        if store_id in seen or not paths.is_valid_store_id(store_id):
            continue
        seen.add(store_id)
        loaded = bundle_module.load(store_id, registry_root=registry_root)
        if loaded.derived_from and parents:
            pending.append(loaded.derived_from)
        if store_id in forced_ids or not bundle_module.is_buildable(loaded) or not loaded.completes_own_store:
            continue
        if store_id in held_ids:
            deferred.add(store_id)
            continue
        changes += bundle_changes(loaded, artifact_root)
    if changes:
        listed = "\n".join(f"  {change}" for change in changes)
        raise AmendmentError(
            "Refusing to run: these bundles, which complete their own Store, no longer plan the Store "
            "their release published. ADR 0037 allows one amendment, an Observed-Only bundle gaining a "
            f"`complete` block; another build, completion or membership is a new Store Release (ADR 0004):\n{listed}\n"
            "Nothing was written. `--config force=1` rebuilds a named release from its bundle as it "
            "now stands (#195)."
        )
    return frozenset(deferred)


__all__ = [
    "AmendmentError",
    "bundle_changes",
    "refuse_changed_bundles",
]
