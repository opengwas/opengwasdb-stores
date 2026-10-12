"""Reconcile Store Release membership, lifecycle and published evidence (#217).

A readiness view answers one question per registered release: does the registry
say what was actually built, and is each distinct kind of evidence present?
It deliberately does **not** run a build or a Store validation. Membership is
derived from the committed ``analyses.tsv`` (the registry's own record), and
the published Store is consulted only through the supported OpenGWASDB
reporting interface, ``opengwasdb info --format json`` -- never by opening the
Store's arrays, tables or ``manifest.json`` directly (ADR 0023).

Two rules keep the view honest:

* **Missing and unknown are not clean passes.** Every evidence channel is a
  :class:`Channel` with an explicit :class:`EvidenceState`; ``missing`` and
  ``unknown`` are distinct from ``pass`` and are surfaced as gaps.
* **Excluded manifest rows are not built membership.** The index summary
  separates intended/admitted/excluded counts, and a release whose published
  artifact predates its current accepted membership is reported with the
  explicit ID-set difference, not as if the new membership were already built.

Historical host-side facts that cannot be re-derived from git (which host
records exist, what the published artifact holds, errata) live in a committed
ledger, :file:`docs/readiness-evidence.yaml`. The tool renders both a
machine-readable view and a Markdown view. See
``docs/spec/store-release-workflow.md`` and ADRs 0017, 0022, 0023, 0025 and 0060.
"""

from __future__ import annotations

import csv
import io
import json
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from opengwasdb.model import analyses as opengwasdb_analyses

from ogstores import bundle as bundle_module
from ogstores import paths
from ogstores.bundle import Bundle
from ogstores.manifest import EXCLUDE_COLUMN, classify_exclusion

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

DEFAULT_LEDGER: Path = REPO_ROOT / "docs" / "readiness-evidence.yaml"


class EvidenceState(StrEnum):
    """The truth-value of one evidence channel.

    ``MISSING`` and ``UNKNOWN`` are deliberately not ``PASS``: a channel no
    evidence addresses is a gap, and a read that could not be completed is not
    a clean bill of health (CONTRIBUTING: absence is not zero).
    """

    PASS = "pass"
    PASS_WITH_WARNINGS = "pass_with_warnings"
    FAIL = "fail"
    MISSING = "missing"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"
    HISTORICAL = "historical"


@dataclass(frozen=True)
class Channel:
    """One evidence dimension for one release, with its citation."""

    state: EvidenceState
    detail: str = ""
    citation: str = ""

    @property
    def is_gap(self) -> bool:
        return self.state in (EvidenceState.FAIL, EvidenceState.MISSING, EvidenceState.UNKNOWN)


@dataclass(frozen=True)
class Membership:
    """Intended, admitted and excluded membership of one Release Bundle."""

    intended: tuple[str, ...]
    admitted: tuple[str, ...]
    excluded: tuple[str, ...]

    @property
    def n_intended(self) -> int:
        return len(self.intended)

    @property
    def n_admitted(self) -> int:
        return len(self.admitted)

    @property
    def n_excluded(self) -> int:
        return len(self.excluded)


@dataclass(frozen=True)
class MembershipDelta:
    """Explicit ID-set difference between current admitted and a baseline."""

    baseline_ref: str
    baseline_admitted: int
    added: tuple[str, ...]
    removed: tuple[str, ...]
    note: str = ""


@dataclass(frozen=True)
class BuiltEvidence:
    """What the published artifact is recorded to hold, and against which ref."""

    state: EvidenceState
    analyses: int | None
    baseline_ref: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class ReleaseReadiness:
    """The reconciled readiness view for one Store Release."""

    store_id: str
    label: str
    status: str
    layout: str
    completion_state: str
    derived_from: str | None
    membership: Membership
    built: BuiltEvidence
    delta: MembershipDelta | None
    channels: Mapping[str, Channel]
    warnings: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    errata: tuple[str, ...] = ()

    @property
    def gaps(self) -> tuple[str, ...]:
        return tuple(name for name, channel in self.channels.items() if channel.is_gap)


# --------------------------------------------------------------------------
# Ledger loading
# --------------------------------------------------------------------------


def load_ledger(path: Path | str | None = None) -> dict[str, Any]:
    """Load the committed historical-evidence ledger (never the artifact root)."""
    ledger_path = Path(path) if path else DEFAULT_LEDGER
    with open(ledger_path, "r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle) or {}
    if not isinstance(document, dict):
        raise ValueError(f"{ledger_path} must be a YAML mapping")
    releases = document.get("releases") or {}
    if not isinstance(releases, dict):
        raise ValueError(f"{ledger_path}: 'releases' must be a mapping")
    return document


def release_ledger(ledger: Mapping[str, Any], store_id: str) -> dict[str, Any]:
    entry = (ledger.get("releases") or {}).get(store_id) or {}
    if not isinstance(entry, dict):
        raise ValueError(f"ledger entry for {store_id} must be a mapping")
    return entry


# --------------------------------------------------------------------------
# Membership
# --------------------------------------------------------------------------


def membership_of(bundle_obj: Bundle) -> Membership:
    """Return intended/admitted/excluded membership from ``analyses.tsv`` only."""
    table = opengwasdb_analyses.read_analyses(bundle_obj.analyses_path)
    admitted_rows, excluded_rows = bundle_module.partition_membership(table)
    intended = tuple((row.get("analysis_id") or "").strip() for row in table.rows)
    admitted = tuple((row.get("analysis_id") or "").strip() for row in admitted_rows)
    excluded = tuple((row.get("analysis_id") or "").strip() for row in excluded_rows)
    return Membership(intended=intended, admitted=admitted, excluded=excluded)


def membership_delta(
    current: Membership,
    baseline_admitted: Sequence[str],
    baseline_ref: str,
    note: str = "",
) -> MembershipDelta:
    """Compute the explicit added/removed ID-set difference against a baseline."""
    current_set = set(current.admitted)
    baseline_set = set(baseline_admitted)
    return MembershipDelta(
        baseline_ref=baseline_ref,
        baseline_admitted=len(baseline_admitted),
        added=tuple(sorted(current_set - baseline_set)),
        removed=tuple(sorted(baseline_set - current_set)),
        note=note,
    )


def git_admitted_ids(store_id: str, ref: str, repo_root: Path | str | None = None) -> tuple[str, ...]:
    """Read a historical bundle's admitted IDs from git, never an artifact.

    The membership baseline for a release whose published Store predates a
    later composition change is a *committed previous bundle*, so the diff is
    reproducible from the registry alone.
    """
    root = Path(repo_root) if repo_root else REPO_ROOT
    rel = f"stores/{store_id}/analyses.tsv"
    result = subprocess.run(
        ["git", "-C", str(root), "show", f"{ref}:{rel}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(
            f"cannot read {rel} at {ref}: {result.stderr.strip() or 'git show failed'}"
        )
    reader = csv.DictReader(io.StringIO(result.stdout), delimiter="\t")
    admitted: list[str] = []
    for row_index, row in enumerate(reader):
        analysis_id = (row.get("analysis_id") or "").strip()
        if classify_exclusion(row.get(EXCLUDE_COLUMN), row_index, analysis_id):
            continue
        admitted.append(analysis_id)
    return tuple(admitted)


# --------------------------------------------------------------------------
# Evidence channels
# --------------------------------------------------------------------------


def _status_channel(bundle_obj: Bundle) -> Channel:
    status = bundle_obj.status or ""
    if status in bundle_module.VALID_STATUSES:
        return Channel(EvidenceState.PASS, f"declared status {status!r}")
    return Channel(
        EvidenceState.FAIL,
        f"declared status {status!r} is not in the Release Status vocabulary",
    )


def _acquisition_channel(bundle_obj: Bundle) -> Channel:
    snapshot = bundle_obj.release.get("source_snapshot_id")
    if snapshot:
        return Channel(EvidenceState.PASS, f"source snapshot {snapshot!r} recorded")
    return Channel(
        EvidenceState.MISSING,
        "no source_snapshot_id recorded; source acquisition is not evidenced",
    )


def _committed_validation(bundle_obj: Bundle) -> Channel:
    """The Store validation channel, from the committed Validation Record only."""
    validation = bundle_obj.validation
    if not validation:
        return Channel(EvidenceState.MISSING, "no committed Validation Record")
    status = str(validation.get("status") or "")
    validator = validation.get("validator") or {}
    name = str(validator.get("name") or "") if isinstance(validator, Mapping) else ""
    runtime = (validation.get("build_environment") or {})
    runtime_rev = ""
    if isinstance(runtime, Mapping):
        runtime_rev = str(runtime.get("opengwasdb_commit") or "")
    if name == "opengwasdb validate":
        state = {
            "passed": EvidenceState.PASS,
            "passed_with_warnings": EvidenceState.PASS_WITH_WARNINGS,
            "failed": EvidenceState.FAIL,
        }.get(status, EvidenceState.FAIL)
        return Channel(state, f"committed Store validation {status!r} by {name}", runtime_rev)
    # A candidate-generation record is real evidence, but it is not Store
    # validation (issue #217): the Store may not exist at all yet.
    return Channel(
        EvidenceState.MISSING,
        f"committed record {status!r} is candidate-generation evidence "
        f"by {name or 'an unknown tool'}, not a Store validation",
        runtime_rev,
    )


def _store_validation_channel(entry: Mapping[str, Any], committed: Channel) -> Channel:
    """Store validation evidence: committed verdict first, host history otherwise.

    A candidate-generation record is not a Store validation, so it falls
    through to the host-side history (or a gap) instead of passing.
    """
    host = entry.get("host_validation")
    if committed.state in (
        EvidenceState.PASS,
        EvidenceState.PASS_WITH_WARNINGS,
        EvidenceState.FAIL,
    ):
        detail = committed.detail
        if host:
            detail += f"; host-side history, not this record: {host}"
        return Channel(committed.state, detail, committed.citation)
    if host:
        return Channel(EvidenceState.HISTORICAL, f"host-side validation: {host}")
    detail = committed.detail or "no committed or host-side Store validation recorded"
    return Channel(EvidenceState.UNKNOWN, detail)


def _registration_channel(entry: Mapping[str, Any]) -> Channel:
    detail = entry.get("host_registration")
    if not detail:
        return Channel(
            EvidenceState.UNKNOWN,
            "no host-side registration provenance recorded",
        )
    return Channel(EvidenceState.HISTORICAL, f"host-side registration: {detail}")


def _completion_channel(bundle_obj: Bundle) -> Channel:
    completion = bundle_obj.completion_state or ""
    if completion != "reference_completed":
        return Channel(EvidenceState.NOT_APPLICABLE, f"completion_state={completion!r}")
    complete = bundle_obj.build.get("complete") or {}
    options = complete.get("options") if isinstance(complete, Mapping) else None
    ancestry = options.get("ancestry") if isinstance(options, Mapping) else None
    if not bundle_obj.derived_from:
        return Channel(
            EvidenceState.FAIL,
            "reference_completed release has no derived_from parent",
        )
    if not ancestry:
        return Channel(
            EvidenceState.MISSING,
            "reference completion declares no ancestry; reference appropriateness unproven",
        )
    return Channel(
        EvidenceState.PASS,
        f"reference completion from {bundle_obj.derived_from!r} "
        f"against ancestry {ancestry!r}",
    )


def _observed_build_channel(
    bundle_obj: Bundle,
    built: BuiltEvidence,
) -> Channel:
    if (bundle_obj.completion_state or "") == "reference_completed":
        return Channel(
            EvidenceState.NOT_APPLICABLE,
            "Reference-Completed release takes its observed parent's build",
        )
    if built.state is EvidenceState.MISSING:
        return Channel(EvidenceState.MISSING, built.detail or "no observed build recorded")
    return Channel(built.state, built.detail, built.baseline_ref or "")


def _required_index_channel(
    bundle_obj: Bundle,
    info: Mapping[str, Any] | None,
    entry: Mapping[str, Any],
) -> Channel:
    """The required format-0.2.0 variant-side index (core ADR 0060).

    ``by_variant`` is required for Ragged components and Hybrid Overflow. The
    supported ``opengwasdb info`` command does not expose it, so presence is
    only ever ``unknown`` from this seam; it is never inferred from a clean
    manifest read here.
    """
    layout = bundle_obj.layout or ""
    if layout not in ("ragged", "hybrid"):
        return Channel(
            EvidenceState.NOT_APPLICABLE,
            f"layout {layout!r} has no required variant-side index",
        )
    fmt = str((info or {}).get("format_version") or "") if info else ""
    if not fmt:
        fmt = str(entry.get("published_format") or "")
    if fmt.startswith("0.2"):
        return Channel(
            EvidenceState.UNKNOWN,
            "format 0.2.0 requires by_variant (ADR 0060); presence not exposed "
            "by the supported info command and not verified by a validate run (#218)",
        )
    shown = fmt or "not read"
    return Channel(
        EvidenceState.MISSING,
        f"layout {layout!r} requires the format-0.2.0 by_variant index "
        f"(ADR 0060); published format is {shown}; pending runtime adoption (#218)",
    )


def _hm3_channel(bundle_obj: Bundle, entry: Mapping[str, Any]) -> Channel:
    declared = bundle_obj.build.get("indexed_subsets")
    if not declared:
        return Channel(EvidenceState.NOT_APPLICABLE, "no HM3 Indexed Variant Subset declared")
    present = entry.get("hm3_present")
    if present is True:
        return Channel(EvidenceState.PASS, "HM3 Indexed Variant Subset present (optional)")
    if present is False:
        return Channel(
            EvidenceState.MISSING,
            "HM3 Indexed Variant Subset declared but absent from the published "
            "artifact; optional, so this is not invalidity (ADR 0061)",
        )
    return Channel(
        EvidenceState.UNKNOWN,
        "HM3 Indexed Variant Subset declared; presence in the published artifact "
        "not recorded",
    )


def _format_runtime_channel(
    bundle_obj: Bundle,
    info: Mapping[str, Any] | None,
    entry: Mapping[str, Any],
) -> Channel:
    if info is None:
        runtime = entry.get("host_runtime") or entry.get("published_format")
        if runtime:
            return Channel(EvidenceState.HISTORICAL, f"published runtime/format: {runtime}")
        return Channel(
            EvidenceState.UNKNOWN,
            "no fresh `opengwasdb info` read and no recorded runtime/format",
        )
    fmt = str(info.get("format_version") or "")
    layout = str(info.get("primary_layout") or "")
    completion = str(info.get("completion_state") or "")
    mismatches: list[str] = []
    if layout and layout != (bundle_obj.layout or ""):
        mismatches.append(f"layout {layout!r} != bundle {bundle_obj.layout!r}")
    if completion and completion != (bundle_obj.completion_state or ""):
        mismatches.append(
            f"completion {completion!r} != bundle {bundle_obj.completion_state!r}"
        )
    detail = (
        f"opengwasdb info: format {fmt or 'unknown'}, layout {layout or 'unknown'}, "
        f"completion {completion or 'unknown'}"
    )
    if mismatches:
        return Channel(EvidenceState.FAIL, detail + "; " + "; ".join(mismatches))
    return Channel(EvidenceState.PASS, detail)


def _warning_text(warning: Any) -> str:
    if isinstance(warning, Mapping):
        return "; ".join(f"{key}: {value}" for key, value in warning.items())
    return str(warning)


def _warnings_channel(
    bundle_obj: Bundle,
    entry: Mapping[str, Any],
) -> Channel:
    warnings: list[str] = []
    validation = bundle_obj.validation or {}
    for warning in validation.get("warnings") or []:
        warnings.append(_warning_text(warning))
    for warning in entry.get("warnings") or []:
        warnings.append(_warning_text(warning))
    if not warnings:
        return Channel(EvidenceState.PASS, "no warnings recorded")
    shown = warnings[:3]
    detail = f"{len(warnings)} warning(s): " + "; ".join(shown)
    if len(warnings) > len(shown):
        detail += f"; … ({len(warnings) - len(shown)} more)"
    return Channel(EvidenceState.PASS_WITH_WARNINGS, detail)


def _lineage_channel(
    bundle_obj: Bundle,
    bundles_by_id: Mapping[str, Bundle],
    incomplete_parents: set[str],
    entry: Mapping[str, Any],
) -> Channel:
    parent_id = bundle_obj.derived_from
    if not parent_id:
        return Channel(EvidenceState.NOT_APPLICABLE, "no derived_from lineage")
    if parent_id not in bundles_by_id:
        return Channel(
            EvidenceState.FAIL,
            f"derived_from {parent_id!r} is not a registered Release Bundle",
        )
    if parent_id in incomplete_parents:
        return Channel(
            EvidenceState.FAIL,
            f"derived from {parent_id!r}, whose membership is recorded incomplete; "
            "the derived artifact inherits that incompleteness",
        )
    note = entry.get("lineage_note")
    detail = f"derived from {parent_id!r}"
    if note:
        detail += f"; {note}"
    return Channel(EvidenceState.PASS, detail)


# --------------------------------------------------------------------------
# Supported OpenGWASDB reporting interface
# --------------------------------------------------------------------------


def store_info(
    store_path: Path | str,
    runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    """Read manifest-level facts through ``opengwasdb info --format json``.

    This is the one sanctioned way the registry looks at a published Store
    (ADR 0023): the core command owns opening the Store, and this function
    only parses its JSON. It reads no Store file or directory itself.
    """
    argv = ["opengwasdb", "info", str(store_path), "--format", "json"]
    run = runner or _default_runner
    result = run(argv)
    if result.returncode != 0:
        raise ValueError(
            f"`{' '.join(argv)}` failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"`{' '.join(argv)}` did not print JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"`{' '.join(argv)}` printed {type(payload).__name__}, not an object")
    return payload


def _default_runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------


def reconcile_release(
    bundle_obj: Bundle,
    ledger: Mapping[str, Any],
    bundles_by_id: Mapping[str, Bundle],
    incomplete_parents: set[str],
    *,
    info: Mapping[str, Any] | None = None,
    baseline_reader: Callable[[str, str], tuple[str, ...]] | None = None,
) -> ReleaseReadiness:
    """Reconcile one release into an explicit readiness record."""
    entry = release_ledger(ledger, bundle_obj.store_id)
    membership = membership_of(bundle_obj)

    delta: MembershipDelta | None = None
    baseline_ref = entry.get("built_baseline_ref")
    baseline_error = ""
    if baseline_ref:
        reader = baseline_reader or (lambda sid, ref: git_admitted_ids(sid, ref))
        try:
            baseline_admitted = reader(bundle_obj.store_id, str(baseline_ref))
        except (ValueError, OSError) as exc:
            # A shallow checkout may not carry the baseline commit. Do not crash
            # the view and do not invent the baseline: record the gap and fall
            # back to comparing the published count with current membership.
            baseline_error = f"baseline ref {baseline_ref!r} not readable: {exc}"
        else:
            delta = membership_delta(
                membership,
                baseline_admitted,
                str(baseline_ref),
                note=str(entry.get("baseline_note") or ""),
            )

    built_analyses = entry.get("built_analyses")
    if built_analyses is None:
        built = BuiltEvidence(
            EvidenceState.MISSING,
            None,
            None,
            "no built artifact recorded",
        )
    else:
        target = delta.baseline_admitted if delta is not None else len(membership.admitted)
        state = EvidenceState.PASS if int(built_analyses) == target else EvidenceState.FAIL
        detail = (
            f"published artifact holds {built_analyses} Analyses"
            + (f" (baseline {baseline_ref})" if baseline_ref else "")
        )
        if state is EvidenceState.FAIL:
            detail += (
                f"; current admitted membership is {len(membership.admitted)}"
            )
        built = BuiltEvidence(state, int(built_analyses), baseline_ref, detail)

    committed = _committed_validation(bundle_obj)
    channels: dict[str, Channel] = {
        "release_status": _status_channel(bundle_obj),
        "source_acquisition": _acquisition_channel(bundle_obj),
        "observed_build": _observed_build_channel(bundle_obj, built),
        "reference_completion": _completion_channel(bundle_obj),
        "required_variant_index": _required_index_channel(bundle_obj, info, entry),
        "optional_hm3": _hm3_channel(bundle_obj, entry),
        "store_validation": _store_validation_channel(entry, committed),
        "registration": _registration_channel(entry),
        "format_runtime": _format_runtime_channel(bundle_obj, info, entry),
        "lineage": _lineage_channel(bundle_obj, bundles_by_id, incomplete_parents, entry),
        "warnings": _warnings_channel(bundle_obj, entry),
    }
    # The committed Validation Record channel is named separately so a
    # candidate-generation record cannot hide behind the host history.
    channels["committed_validation_record"] = committed

    if entry.get("incomplete_membership") and built.analyses is not None:
        built = BuiltEvidence(
            EvidenceState.FAIL,
            built.analyses,
            built.baseline_ref,
            (built.detail or "published artifact recorded")
            + "; membership recorded incomplete pending restored inputs",
        )
        channels["observed_build"] = _observed_build_channel(bundle_obj, built)

    warnings = tuple(str(w) for w in (entry.get("warnings") or []))
    blockers = [str(b) for b in (entry.get("blockers") or [])]
    if baseline_error:
        blockers.append(baseline_error)
    for name, channel in channels.items():
        if channel.state is EvidenceState.FAIL:
            blockers.append(f"{name}: {channel.detail}")
    errata = tuple(str(e) for e in (entry.get("errata") or []))

    return ReleaseReadiness(
        store_id=bundle_obj.store_id,
        label=bundle_obj.label or "",
        status=bundle_obj.status or "",
        layout=bundle_obj.layout or "",
        completion_state=bundle_obj.completion_state or "",
        derived_from=bundle_obj.derived_from,
        membership=membership,
        built=built,
        delta=delta,
        channels=channels,
        warnings=warnings,
        blockers=tuple(blockers),
        errata=errata,
    )


def discover_bundles(registry_root: Path | str | None = None) -> list[Bundle]:
    root = Path(registry_root) if registry_root else REPO_ROOT / "stores"
    store_ids = sorted(
        d.name
        for d in root.iterdir()
        if d.is_dir() and paths.is_valid_store_id(d.name) and (d / "release.yaml").is_file()
    )
    return [bundle_module.load(sid, registry_root=root) for sid in store_ids]


def reconcile_registry(
    registry_root: Path | str | None = None,
    ledger: Mapping[str, Any] | None = None,
    *,
    bundle_objects: Sequence[Bundle] | None = None,
    info_by_store: Mapping[str, Mapping[str, Any]] | None = None,
    baseline_reader: Callable[[str, str], tuple[str, ...]] | None = None,
    incomplete_parents: Iterable[str] = (),
) -> list[ReleaseReadiness]:
    """Reconcile every registered release into a readiness record list."""
    loaded = list(bundle_objects) if bundle_objects is not None else discover_bundles(registry_root)
    loaded.sort(key=lambda b: b.store_id)
    ledger_doc = ledger if ledger is not None else load_ledger()
    by_id = {b.store_id: b for b in loaded}
    incomplete = set(incomplete_parents)
    # A parent is incomplete when the ledger marks it so; this is the mechanism
    # that keeps OGS-00010 flagged as derived from the incomplete OGS-00009.
    for entry_id, entry in (ledger_doc.get("releases") or {}).items():
        if isinstance(entry, Mapping) and entry.get("incomplete_membership"):
            incomplete.add(entry_id)
    info_map = info_by_store or {}
    return [
        reconcile_release(
            b,
            ledger_doc,
            by_id,
            incomplete,
            info=info_map.get(b.store_id),
            baseline_reader=baseline_reader,
        )
        for b in loaded
    ]


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def readiness_document(
    releases: Sequence[ReleaseReadiness],
    ledger: Mapping[str, Any],
) -> dict[str, Any]:
    """Render the readiness records as a JSON/YAML-serialisable document."""
    return {
        "recorded_at": ledger.get("recorded_at"),
        "artifact_root": ledger.get("artifact_root"),
        "releases": [
            {
                "store_id": r.store_id,
                "label": r.label,
                "status": r.status,
                "layout": r.layout,
                "completion_state": r.completion_state,
                "derived_from": r.derived_from,
                "membership": {
                    "intended": r.membership.n_intended,
                    "admitted": r.membership.n_admitted,
                    "excluded": r.membership.n_excluded,
                },
                "built": {
                    "state": r.built.state.value,
                    "analyses": r.built.analyses,
                    "baseline_ref": r.built.baseline_ref,
                    "detail": r.built.detail,
                },
                "delta": None
                if r.delta is None
                else {
                    "baseline_ref": r.delta.baseline_ref,
                    "baseline_admitted": r.delta.baseline_admitted,
                    "added": list(r.delta.added),
                    "removed": list(r.delta.removed),
                    "note": r.delta.note,
                },
                "channels": {
                    name: {
                        "state": channel.state.value,
                        "detail": channel.detail,
                        "citation": channel.citation,
                    }
                    for name, channel in r.channels.items()
                },
                "warnings": list(r.warnings),
                "blockers": list(r.blockers),
                "errata": list(r.errata),
            }
            for r in releases
        ],
    }


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_readiness_markdown(
    releases: Sequence[ReleaseReadiness],
    ledger: Mapping[str, Any],
) -> str:
    """Render a human-readable readiness view."""
    lines: list[str] = [
        "# Store Release readiness reconciliation",
        "",
        "Generated by `python3 resources/scripts/release_readiness.py` from the",
        "committed Release Bundles and the historical-evidence ledger. Do not edit",
        "by hand; regenerate.",
        "",
        f"Recorded evidence date: {ledger.get('recorded_at') or 'unknown'}.",
        f"Artifact root: `{ledger.get('artifact_root') or 'unset'}`.",
        "",
        "Membership is derived from committed `analyses.tsv`; published-artifact",
        "facts are read only through `opengwasdb info --format json` or recorded as",
        "historical host evidence. `missing` and `unknown` are not passes.",
        "",
        "## Membership and published artifact",
        "",
        "| Store | Status | Layout | Intended | Admitted | Excluded | Built | Baseline / delta |",
        "|:---|:---|:---|---:|---:|---:|---:|:---|",
    ]
    for r in releases:
        m = r.membership
        if r.delta is None:
            diff = "-"
        else:
            diff = (
                f"`{r.delta.baseline_ref}` ({r.delta.baseline_admitted} admitted): "
                f"+{len(r.delta.added)} / -{len(r.delta.removed)}"
            )
        built = "-" if r.built.analyses is None else str(r.built.analyses)
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{r.store_id}`",
                    r.status or "-",
                    r.layout or "-",
                    str(m.n_intended),
                    str(m.n_admitted),
                    str(m.n_excluded),
                    built,
                    diff,
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## Evidence channels",
            "",
            "| Store | Channel | State | Detail |",
            "|:---|:---|:---|:---|",
        ]
    )
    for r in releases:
        for name, channel in r.channels.items():
            lines.append(
                f"| `{r.store_id}` | {name} | {channel.state.value} | {_cell(channel.detail)} |"
            )

    lines.extend(["", "## Membership differences", ""])
    any_delta = False
    for r in releases:
        if r.delta is None:
            continue
        any_delta = True
        lines.append(
            f"### `{r.store_id}` vs `{r.delta.baseline_ref}` "
            f"(historical baseline {r.delta.baseline_admitted} admitted)"
        )
        lines.append("")
        if r.delta.note:
            lines.append(r.delta.note)
            lines.append("")
        lines.append(f"- Added ({len(r.delta.added)}): {', '.join(r.delta.added) or 'none'}")
        lines.append(f"- Removed ({len(r.delta.removed)}): {', '.join(r.delta.removed) or 'none'}")
        lines.append("")
    if not any_delta:
        lines.append("No release declares a historical membership baseline.")
        lines.append("")

    lines.extend(["## Warnings, errata and blockers", ""])
    for r in releases:
        if not (r.warnings or r.errata or r.blockers):
            continue
        lines.append(f"### `{r.store_id}`")
        lines.append("")
        for warning in r.warnings:
            lines.append(f"- Warning: {warning}")
        for erratum in r.errata:
            lines.append(f"- Erratum: {erratum}")
        for blocker in r.blockers:
            lines.append(f"- Blocker: {blocker}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def dump_readiness_yaml(
    releases: Sequence[ReleaseReadiness],
    ledger: Mapping[str, Any],
) -> str:
    return yaml.safe_dump(
        readiness_document(releases, ledger),
        sort_keys=False,
        allow_unicode=True,
    )


__all__ = [
    "Channel",
    "BuiltEvidence",
    "EvidenceState",
    "Membership",
    "MembershipDelta",
    "ReleaseReadiness",
    "discover_bundles",
    "dump_readiness_yaml",
    "git_admitted_ids",
    "load_ledger",
    "membership_delta",
    "membership_of",
    "readiness_document",
    "reconcile_registry",
    "reconcile_release",
    "release_ledger",
    "render_readiness_markdown",
    "store_info",
]
