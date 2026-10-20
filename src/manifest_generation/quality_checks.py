"""Quality Checks: release-policy tests of what Analysis Resolution measured (#285).

ADR 0036: *OpenGWASDB measures and Manifest Generation decides.* A Quality
Check tests one measurement of one Analysis against a threshold, or against the
values it accepts when the measurement is a status. The measurement comes from
the Analysis's Resolution Record, except where OpenGWASDB does not measure it
yet: today only effect-placeholder rows, read from a reviewed listing. Its
consequence is fixed where the check is declared here, never by the code that
applies it:

- an **exclusion** makes the Analysis an ``exclude_from_build`` audit row with
  one of the check's controlled reasons (ADR 0025);
- a **warning** keeps the Analysis and records the finding.

A threshold is a shared default declared with its check. A release config may
override it under ``quality_checks:``; the effective thresholds are recorded in
the resolution receipt and in ``validation.yaml``. Every check is evaluated for
every Analysis, passing or not, and each result goes into the
``sidecars/quality_checks.tsv`` contract file (``ogstores.bundle``).

Adding a check is a declaration plus :func:`register`; a warning, or an
exclusion at an existing stage, needs no edit to the release policy. A
:class:`Stage` is only a precedence slot: the release policy
(``candidate_workflow._decide``) settles an Analysis in a fixed order and asks
each slot in turn for its first failing exclusion. The stage decides which
reason wins when several apply and what the audit row carries. A new stage is
a new slot, so it needs an edit to ``_decide``.

A measurement an exclusion cannot use is never a silent pass: an ``invalid``
result refuses the run where that check would decide membership, and an
``unmeasured`` one refuses to include the Analysis. On an Analysis an earlier
rule already excluded, both are only recorded.

Not every exclusion is a Quality Check. A missing or failed Resolution Record
has nothing to measure, missing sample-size or case-control counts are
candidate-table metadata, and the Store Composition Rule is a release-level
admission rule (ADR 0033). These stay release policy
(``candidate_workflow.POLICY_EXCLUSION_CATEGORIES``).
"""

from __future__ import annotations

import math
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Callable, Iterator, Mapping, Sequence

from ogstores.bundle import (
    QUALITY_CHECK_LEADING_COLUMNS,
    QUALITY_CHECK_MEASURED_SUFFIX,
    QUALITY_CHECK_RESULTS,
    SUPERPOPULATIONS,
)


class Kind(StrEnum):
    """The consequence a check declares."""

    EXCLUSION = "exclusion"
    WARNING = "warning"


class Stage(StrEnum):
    """The precedence slot where a failed exclusion takes effect, in policy order.

    - ``RESOLVED``: the Analysis has a successful Resolution Record; its Assigned
      Ancestry is not read yet, so the audit row carries none.
    - ``ANCESTRY_READ``: its Assigned Ancestry is read but not yet assessed; the
      row carries the value read, unassessed.
    - ``SELECTION``: its Assigned Ancestry is tested against the Selection; the
      row carries it as ``af_assigned`` when it is a super-population code.
    - ``TARGET_ANCESTRY``: it has the Selection's Assigned Ancestry (and, when
      case-control, its counts); the row carries it as ``af_assigned``.
    """

    RESOLVED = "resolved"
    ANCESTRY_READ = "ancestry_read"
    SELECTION = "selection"
    TARGET_ANCESTRY = "target_ancestry"


class Result(StrEnum):
    """One check's result for one Analysis; the contract's vocabulary."""

    PASSED = "passed"
    FAILED = "failed"
    #: The check does not apply (a phenotype SD on a log-OR scale). Decided
    #: before anything is measured, so it wins over ``UNMEASURED``.
    NOT_APPLICABLE = "not_applicable"
    #: There is no measurement to test.
    UNMEASURED = "unmeasured"
    #: The measurement breaks its contract (an unknown EAF-orientation outcome,
    #: a non-numeric SD); ``Finding.detail`` says how.
    INVALID = "invalid"
    #: An exclusion check's measurement fails its test, but a policy the release
    #: declared keeps the Analysis and records the finding. Only a check
    #: declared ``warns`` gives it; today ``assigned_ancestry``, for an Analysis
    #: whose source has no allele frequencies when the release trusts the
    #: source's super-population (#287).
    WARNING = "warning"


if tuple(Result) != QUALITY_CHECK_RESULTS:  # pragma: no cover - import-time guard
    raise ImportError("quality_checks.Result disagrees with ogstores.bundle.QUALITY_CHECK_RESULTS")

#: The pinned upstream EAF-orientation vocabulary (opengwasdb
#: ``EafOrientationOutcome``). ``failed`` means the A1-oriented source
#: frequencies correlate negatively with the reference; ``unverified`` means no
#: direction could be read. A value outside it is a contract violation.
EAF_ORIENTATION_OUTCOMES: tuple[str, ...] = ("passed", "failed", "unverified")

#: Effect scales with no phenotype SD to estimate.
NON_QUANTITATIVE_EFFECT_SCALES: frozenset[str] = frozenset({"log_or", "log_hazard"})


class QualityCheckError(ValueError):
    """A check was declared, registered, configured or applied inconsistently."""


def record_section(record: Mapping[str, Any] | None, key: str) -> Mapping[str, Any]:
    """``record[key]`` when it is a mapping, else an empty one (absence, not an error)."""
    value = (record or {}).get(key)
    return value if isinstance(value, Mapping) else {}


def diagnostic_count(value: Any) -> str:
    """Render a resolver diagnostic count for evidence text, or ``?``."""
    return str(value) if type(value) is int else "?"


@dataclass(frozen=True)
class Subject:
    """What a Quality Check may read for one Analysis.

    ``selection_ancestry`` is the super-population the release's Selection
    expects. ``effect_placeholder_reason`` is the reviewed listing's entry for
    this Analysis, or ``None`` when it is not listed.
    ``source_superpopulation`` is the Source Metadata Table's, and
    ``trust_source_ancestry_without_af`` the release's opt-in to take it as
    Assigned Ancestry when the record shows the source has no allele
    frequencies (#287).
    """

    analysis_id: str
    record: Mapping[str, Any] | None
    stored_effect_scale: str
    selection_ancestry: str
    effect_placeholder_reason: str | None = None
    source_superpopulation: str = ""
    trust_source_ancestry_without_af: bool = False

    @property
    def resolved(self) -> bool:
        """True when a Resolution Record exists and reports success."""
        return self.record is not None and self.record.get("status") == "success"


@dataclass(frozen=True)
class Finding:
    """What one check found for one Analysis.

    ``measured`` is the measurement as the sidecar records it (empty when there
    is none). ``reason`` is the controlled exclusion reason a failed exclusion
    writes; ``detail`` is its evidence, or why a result is ``invalid``.
    :func:`evaluate` sets ``check``.
    """

    result: Result
    measured: str = ""
    reason: str = ""
    detail: str = ""
    check: QualityCheck | None = field(default=None, compare=False)

    @property
    def name(self) -> str:
        assert self.check is not None, "a Finding is bound to its check by evaluate()"
        return self.check.name


@dataclass(frozen=True)
class QualityCheck:
    """One declared Quality Check.

    ``evaluate(subject, threshold)`` returns a :class:`Finding`; ``threshold`` is
    the effective value (the default, or the release's override), or ``None``
    for a check with none. ``reasons`` are the controlled exclusion reasons it
    may write, each declared by one check only. ``measures`` says what it reads.
    """

    name: str
    kind: Kind
    stage: Stage
    measures: str
    evaluate: Callable[[Subject, Any], Finding]
    reasons: tuple[str, ...] = ()
    category: str = ""
    default_threshold: Any = None
    parse_threshold: Callable[[Any], Any] | None = None
    #: An exclusion that may give a ``warning`` result: a declared release
    #: policy keeps the Analysis though its measurement fails the test.
    warns: bool = False

    def __post_init__(self) -> None:
        if not self.name or not self.name.isidentifier():
            raise QualityCheckError(f"Quality Check name {self.name!r} must be an identifier")
        if not isinstance(self.kind, Kind):
            raise QualityCheckError(f"Quality Check {self.name!r} kind {self.kind!r} is not a Kind")
        if not isinstance(self.stage, Stage):
            raise QualityCheckError(
                f"Quality Check {self.name!r} stage {self.stage!r} is not a Stage"
            )
        if self.kind is Kind.EXCLUSION and (not self.reasons or not self.category):
            raise QualityCheckError(
                f"exclusion Quality Check {self.name!r} must declare its reasons and category"
            )
        if self.kind is Kind.WARNING and self.reasons:
            raise QualityCheckError(
                f"warning Quality Check {self.name!r} excludes nothing, so it has no reasons"
            )
        if self.warns and self.kind is not Kind.EXCLUSION:
            raise QualityCheckError(
                f"warning Quality Check {self.name!r} warns already; only an exclusion "
                "declares that a release policy may keep a failing Analysis"
            )
        if self.default_threshold is not None and self.parse_threshold is None:
            raise QualityCheckError(
                f"Quality Check {self.name!r} has a default threshold but no parser for overrides"
            )

    @property
    def has_threshold(self) -> bool:
        return self.default_threshold is not None


# ---------------------------------------------------------------------------
# The built-in checks: each was an exclusion before #285, unchanged
# ---------------------------------------------------------------------------


def _minimum_rows(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise QualityCheckError(
            "must be a whole number of rows of at least 1 (an Analysis with no "
            "build-eligible row has no association to build)"
        )
    return value


def _build_eligible_rows(subject: Subject, minimum: int) -> Finding:
    """Rows with a finite effect and a positive standard error (#176)."""
    if not subject.resolved:
        return Finding(Result.UNMEASURED)
    diagnostics = record_section(subject.record, "diagnostics")
    if "build_eligible_rows" not in diagnostics or diagnostics["build_eligible_rows"] is None:
        return Finding(Result.UNMEASURED)
    value = diagnostics["build_eligible_rows"]
    if type(value) is not int or value < 0:
        return Finding(
            Result.INVALID,
            measured=repr(value),
            detail=(
                f"{subject.analysis_id}: resolver diagnostics.build_eligible_rows {value!r} "
                "is not a non-negative integer count"
            ),
        )
    if value >= minimum:
        return Finding(Result.PASSED, measured=str(value))
    counts = (
        f"rows_read={diagnostic_count(diagnostics.get('rows_read'))}, "
        f"canonical_rows_retained={diagnostic_count(diagnostics.get('canonical_rows_retained'))}"
    )
    if value == 0:
        detail = (
            "resolver found no row with a finite effect and positive standard "
            f"error ({counts})"
        )
    else:
        detail = (
            f"resolver found {value} row(s) with a finite effect and positive standard "
            f"error, fewer than the release minimum of {minimum} ({counts})"
        )
    return Finding(Result.FAILED, str(value), "no_build_eligible_rows", detail)


def _effect_placeholder_rows(subject: Subject, _threshold: None) -> Finding:
    """A reviewed listing of Analyses with ``±2.2e-308`` placeholder effects.

    Core does not count these rows yet, so the measurement (the row counts) is
    a reviewer's, recorded as the listing's reason (ADR 0036, clarified by
    #285). When core counts or refuses them, this check reads the Resolution
    Record instead.
    """
    reason = subject.effect_placeholder_reason
    if reason is None:
        return Finding(Result.PASSED, measured="not_listed")
    return Finding(Result.FAILED, measured=reason, reason="effect_placeholder_rows", detail=reason)


def _eaf_orientation(subject: Subject, _threshold: None) -> Finding:
    """The resolver's EAF-orientation outcome and the gate that names it.

    ``failed``, or the assignment gate ``eaf_orientation``, fails. ``passed``
    and ``unverified`` (no direction readable, so the ordinary ancestry policy
    decides) pass. No outcome at all is unmeasured.
    """
    if not subject.resolved:
        return Finding(Result.UNMEASURED)
    ancestry = record_section(subject.record, "ancestry")
    outcome = str(ancestry.get("eaf_orientation") or "").strip().lower()
    gate_reason = str(ancestry.get("gate_reason") or "").strip()
    measured = (
        f"eaf_orientation={outcome or 'unrecorded'} "
        f"gate_reason={gate_reason or 'none'} "
        f"r={ancestry.get('eaf_orientation_r')!r}"
    )
    if outcome and outcome not in EAF_ORIENTATION_OUTCOMES:
        return Finding(
            Result.INVALID,
            measured,
            detail=(
                f"{subject.analysis_id}: resolver ancestry carries unknown eaf_orientation "
                f"{outcome!r}; expected one of "
                f"{', '.join(EAF_ORIENTATION_OUTCOMES)} (opengwasdb EafOrientationOutcome)"
            ),
        )
    if outcome == "failed" or gate_reason == "eaf_orientation":
        return Finding(Result.FAILED, measured, "orientation_failure", measured)
    if outcome:
        return Finding(Result.PASSED, measured)
    return Finding(Result.UNMEASURED, measured)


#: The resolver ``maf_state`` values under which it reads the frequency column
#: and counts every row with no usable frequency (``maf_rows_missing``).
#: ``unavailable`` -- no MAF request -- counts nothing, so it proves nothing.
MAF_STATES_COUNTING_MISSING: frozenset[str] = frozenset({"disabled", "filtered"})


def no_allele_frequency(record: Mapping[str, Any] | None) -> int | None:
    """The row count when the record shows a source with no allele frequency, else ``None``.

    Under a MAF request (``maf_state`` ``disabled`` or ``filtered``; a
    ``defaults.maf_threshold`` of 0 filters nothing), the resolver counts the
    rows whose frequency is missing, non-finite, or 0 or 1
    (``maf_rows_missing``) among the rows it observed
    (``canonical_rows_observed``). Every observed row without one, and no
    ancestry site read, is a source with no allele frequencies, such as Sun
    2018's. A source whose frequencies the fit could not read -- palindromic
    sites only, unusable standard errors at the reference sites -- also has no
    ancestry site, but its frequencies are counted present, so it is a measured
    failure, never an absence (#287 review). Without a MAF request the record
    says nothing about frequencies, and this returns ``None``.
    """
    diagnostics = record_section(record, "diagnostics")
    state = diagnostics.get("maf_state")
    observed = diagnostics.get("canonical_rows_observed")
    missing = diagnostics.get("maf_rows_missing")
    sites = diagnostics.get("ancestry_sites")
    if (
        state in MAF_STATES_COUNTING_MISSING
        and type(observed) is int
        and observed > 0
        and type(missing) is int
        and missing == observed
        and type(sites) is int
        and sites == 0
    ):
        return observed
    return None


def _assigned_ancestry(subject: Subject, _threshold: None) -> Finding:
    """The record's Assigned Ancestry against the Selection's super-population.

    ADR 0036: Assigned Ancestry never moves an Analysis to another Store
    Release; a disagreement with the Selection is a Quality Check. No usable
    assignment is ``ancestry_unassigned``; a different super-population is
    ``ancestry_not_eur`` (the reason keeps its historical spelling whatever the
    Selection's ancestry).

    #287's decision: when the record shows ancestry unmeasurable because the
    source has no allele frequencies (:func:`no_allele_frequency`: every row
    counted without one, under a MAF request) and the
    release opts in (``release_policy.trust_source_ancestry_without_af``), the
    Source Metadata Table's ``source_superpopulation`` stands in, recorded as a
    ``warning`` when it is the Selection's. A measured ancestry -- assigned, or
    measured and gated -- never takes the source label.
    """
    if not subject.resolved:
        return Finding(Result.UNMEASURED)
    ancestry = record_section(subject.record, "ancestry")
    assigned = str(ancestry.get("assigned_ancestry") or "").strip()
    gate_reason = str(ancestry.get("gate_reason") or "").strip()
    measured = f"assigned_ancestry={assigned or 'none'} gate_reason={gate_reason or 'none'}"
    if not assigned:
        observed = no_allele_frequency(subject.record)
        if observed is None:
            return Finding(
                Result.FAILED, measured, "ancestry_unassigned", f"gate_reason={gate_reason or 'none'}"
            )
        source = subject.source_superpopulation
        measured += (
            f" ancestry_sites=0 maf_rows_missing={observed} of {observed} "
            f"source_superpopulation={source or 'none'}"
        )
        unmeasurable = (
            f"no allele frequency on any of the {observed} rows read, so ancestry is "
            "unmeasurable"
        )
        if not subject.trust_source_ancestry_without_af:
            return Finding(
                Result.FAILED, measured, "ancestry_unassigned",
                f"{unmeasurable}; release_policy.trust_source_ancestry_without_af is off",
            )
        if source not in SUPERPOPULATIONS:
            return Finding(
                Result.FAILED, measured, "ancestry_unassigned",
                f"{unmeasurable}, and the Source Metadata Table gives no source_superpopulation "
                "to trust",
            )
        if source != subject.selection_ancestry:
            return Finding(
                Result.FAILED, measured, "ancestry_not_eur",
                f"{unmeasurable}; source_superpopulation={source}, trusted without allele "
                f"frequencies (release target {subject.selection_ancestry})",
            )
        return Finding(
            Result.WARNING, measured,
            detail=f"{unmeasurable}; Assigned Ancestry {source} from source_superpopulation "
            "(source_trusted_no_af)",
        )
    if assigned not in SUPERPOPULATIONS:
        return Finding(
            Result.FAILED,
            measured,
            "ancestry_unassigned",
            f"assigned_ancestry {assigned!r} is not a super-population code",
        )
    if assigned != subject.selection_ancestry:
        return Finding(
            Result.FAILED,
            measured,
            "ancestry_not_eur",
            f"assigned_ancestry={assigned} (release target {subject.selection_ancestry})",
        )
    return Finding(Result.PASSED, measured)


#: OpenGWASDB's phenotype-SD reason -> the controlled exclusion reason.
_SD_REASONS: Mapping[str, str] = {
    "no_reference_resource_for_ancestry": "sd_no_reference_resource_for_ancestry",
    "no_qualifying_evidence": "sd_no_qualifying_evidence",
    "no_usable_sample_size": "sd_no_usable_sample_size",
}


def _phenotype_sd(subject: Subject, _threshold: None) -> Finding:
    """OpenGWASDB's phenotype-SD estimate for a quantitative Analysis (#152).

    It passes only as a positive finite estimate: the builder refuses any other
    ``original_sd``. A failure keeps OpenGWASDB's own reason.
    """
    if subject.stored_effect_scale in NON_QUANTITATIVE_EFFECT_SCALES:
        return Finding(Result.NOT_APPLICABLE)
    if not subject.resolved:
        return Finding(Result.UNMEASURED)
    pheno = record_section(subject.record, "phenotype_sd")
    status = str(pheno.get("status") or "").strip()
    reason = str(pheno.get("reason") or "").strip()
    estimate = pheno.get("estimate")
    measured = f"status={status or 'missing'} reason={reason or 'none'}"
    if status == "estimated" and isinstance(estimate, Mapping) and estimate.get("sd") is not None:
        raw = estimate["sd"]
        try:
            sd_value = float(raw)
        except (TypeError, ValueError):
            return Finding(
                Result.INVALID,
                f"{measured} sd={raw!r}",
                detail=f"{subject.analysis_id}: phenotype_sd estimate sd {raw!r} is not a number",
            )
        measured += f" sd={sd_value!r}"
        if not math.isfinite(sd_value) or sd_value <= 0:
            return Finding(
                Result.FAILED,
                measured,
                "sd_no_qualifying_evidence",
                f"estimated phenotype SD {sd_value!r} is not a positive finite number",
            )
        return Finding(Result.PASSED, measured)
    if status == "skipped" and not reason:
        return Finding(
            Result.FAILED,
            measured,
            "sd_no_reference_resource_for_ancestry",
            "quantitative SD estimation was skipped",
        )
    if status in {"skipped", "unavailable"}:
        return Finding(
            Result.FAILED,
            measured,
            _SD_REASONS.get(reason, "sd_failed"),
            reason or "no phenotype SD could be estimated",
        )
    return Finding(
        Result.FAILED,
        measured,
        "sd_failed",
        f"phenotype_sd status={status or 'missing'!r} reason={reason or 'none'!r}",
    )


#: The built-in checks, in the order the release policy applies them.
BUILT_IN_CHECKS: tuple[QualityCheck, ...] = (
    QualityCheck(
        name="build_eligible_rows",
        kind=Kind.EXCLUSION,
        stage=Stage.RESOLVED,
        measures=(
            "diagnostics.build_eligible_rows: rows with a finite effect and a positive "
            "standard error, after any INFO and MAF floor"
        ),
        evaluate=_build_eligible_rows,
        reasons=("no_build_eligible_rows",),
        category="effect_scale",
        default_threshold=1,
        parse_threshold=_minimum_rows,
    ),
    QualityCheck(
        name="effect_placeholder_rows",
        kind=Kind.EXCLUSION,
        stage=Stage.RESOLVED,
        measures=(
            "rows whose effect is the +/-2.2e-308 placeholder, from the reviewed "
            "release_policy.effect_placeholder_exclusions listing until core counts them"
        ),
        evaluate=_effect_placeholder_rows,
        reasons=("effect_placeholder_rows",),
        category="effect_scale",
    ),
    QualityCheck(
        name="eaf_orientation",
        kind=Kind.EXCLUSION,
        stage=Stage.ANCESTRY_READ,
        measures="ancestry.eaf_orientation and ancestry.gate_reason",
        evaluate=_eaf_orientation,
        reasons=("orientation_failure",),
        category="orientation",
    ),
    QualityCheck(
        name="assigned_ancestry",
        kind=Kind.EXCLUSION,
        stage=Stage.SELECTION,
        measures=(
            "ancestry.assigned_ancestry against the Selection's super-population; for a "
            "source with no allele frequency, its source_superpopulation when the release "
            "trusts it"
        ),
        evaluate=_assigned_ancestry,
        reasons=("ancestry_unassigned", "ancestry_not_eur"),
        category="ancestry",
        warns=True,
    ),
    QualityCheck(
        name="phenotype_sd",
        kind=Kind.EXCLUSION,
        stage=Stage.TARGET_ANCESTRY,
        measures="phenotype_sd.status, phenotype_sd.reason and phenotype_sd.estimate.sd",
        evaluate=_phenotype_sd,
        reasons=(
            "sd_no_reference_resource_for_ancestry",
            "sd_no_qualifying_evidence",
            "sd_no_usable_sample_size",
            "sd_failed",
        ),
        category="effect_scale",
    ),
)


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, QualityCheck] = {}


def checks() -> tuple[QualityCheck, ...]:
    """Every registered check, in registration order (the order they apply)."""
    return tuple(_REGISTRY.values())


def register(check: QualityCheck) -> None:
    """Add a check; its name and every reason must be new."""
    if check.name in _REGISTRY:
        raise QualityCheckError(f"Quality Check {check.name!r} is already registered")
    claimed = {reason: other.name for other in _REGISTRY.values() for reason in other.reasons}
    for reason in check.reasons:
        if reason in claimed:
            raise QualityCheckError(
                f"exclusion reason {reason!r} is already declared by Quality Check "
                f"{claimed[reason]!r}"
            )
    _REGISTRY[check.name] = check


def unregister(name: str) -> None:
    if name not in _REGISTRY:
        raise QualityCheckError(f"Quality Check {name!r} is not registered")
    del _REGISTRY[name]


@contextmanager
def registered(*added: QualityCheck) -> Iterator[None]:
    """Register checks for the duration of a block (tests, one-off runs)."""
    done: list[str] = []
    try:
        for check in added:
            register(check)
            done.append(check.name)
        yield
    finally:
        for name in reversed(done):
            unregister(name)


for _check in BUILT_IN_CHECKS:
    register(_check)
del _check


def exclusion_categories() -> dict[str, str]:
    """Exclusion reason -> its ``exclusions.tsv`` category, for every registered check."""
    return {reason: check.category for check in checks() for reason in check.reasons}


# ---------------------------------------------------------------------------
# Thresholds and release overrides
# ---------------------------------------------------------------------------


def parse_overrides(value: Any, where: str = "quality_checks") -> dict[str, Any]:
    """Validate a release config's ``quality_checks:`` block.

    It maps a registered check's name to its threshold for this release. An
    absent block is no override. An unknown name, a check with no threshold,
    or a value its parser refuses is an error, never ignored.
    """
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise QualityCheckError(
            f"{where} must be a mapping of Quality Check name to threshold"
        )
    parsed: dict[str, Any] = {}
    for name, raw in value.items():
        check = _REGISTRY.get(name)
        if check is None:
            raise QualityCheckError(
                f"{where}.{name}: no such Quality Check; registered: "
                + ", ".join(_REGISTRY)
            )
        if not check.has_threshold or check.parse_threshold is None:
            raise QualityCheckError(
                f"{where}.{name}: Quality Check {name!r} has no threshold to override; "
                "it tests a status"
            )
        try:
            parsed[name] = check.parse_threshold(raw)
        except (QualityCheckError, TypeError, ValueError) as exc:
            raise QualityCheckError(f"{where}.{name}: {raw!r} {exc}") from exc
    return parsed


def effective_thresholds(overrides: Mapping[str, Any]) -> dict[str, Any]:
    """Each registered threshold check's value for this release."""
    return {
        check.name: overrides.get(check.name, check.default_threshold)
        for check in checks()
        if check.has_threshold
    }


def receipt_contract(overrides: Mapping[str, Any]) -> dict[str, Any]:
    """What the resolution receipt binds about thresholds.

    Always the effective thresholds, defaults included, and the release's
    overrides: changing a shared default, or adding, changing or removing an
    override, makes a receipt stale.
    """
    return {
        "thresholds": effective_thresholds(overrides),
        "overrides": dict(overrides),
    }


# ---------------------------------------------------------------------------
# Applying checks
# ---------------------------------------------------------------------------


def evaluate(subject: Subject, overrides: Mapping[str, Any]) -> tuple[Finding, ...]:
    """Every registered check's finding for one Analysis, in registry order.

    A check reports a measurement it cannot use as ``invalid`` rather than
    raising, so recording it never aborts a run; :func:`first_exclusion` and
    :func:`require_measured` decide when it must.
    """
    thresholds = effective_thresholds(overrides)
    findings = []
    for check in checks():
        finding = check.evaluate(subject, thresholds.get(check.name))
        if not isinstance(finding.result, Result):
            raise QualityCheckError(
                f"Quality Check {check.name!r} returned result {finding.result!r} for "
                f"{subject.analysis_id}; expected a Result"
            )
        if finding.result is Result.WARNING and not check.warns:
            raise QualityCheckError(
                f"Quality Check {check.name!r} gave {subject.analysis_id} a warning result, "
                "but it is not declared to warn"
            )
        if check.kind is Kind.EXCLUSION and finding.result is Result.FAILED and (
            finding.reason not in check.reasons
        ):
            raise QualityCheckError(
                f"Quality Check {check.name!r} failed {subject.analysis_id} with reason "
                f"{finding.reason!r}, which it does not declare"
            )
        if any(character in finding.measured for character in "\t\r\n"):
            raise QualityCheckError(
                f"Quality Check {check.name!r} measured {finding.measured!r} for "
                f"{subject.analysis_id}, which cannot be one TSV field"
            )
        findings.append(replace(finding, check=check))
    return tuple(findings)


def _exclusions(findings: Sequence[Finding]) -> Iterator[Finding]:
    return (f for f in findings if f.check is not None and f.check.kind is Kind.EXCLUSION)


def first_exclusion(findings: Sequence[Finding], stage: Stage) -> Finding | None:
    """The first failed exclusion at ``stage``, in registry order.

    An ``invalid`` exclusion at this stage would decide membership on a broken
    measurement, so it raises :class:`QualityCheckError` instead.
    """
    for finding in _exclusions(findings):
        if finding.check.stage is not stage:
            continue
        if finding.result is Result.INVALID:
            raise QualityCheckError(finding.detail)
        if finding.result is Result.FAILED:
            return finding
    return None


def require_measured(findings: Sequence[Finding], analysis_id: str) -> None:
    """Refuse to include an Analysis an exclusion could not test.

    ``unmeasured`` (or ``invalid``) on an exclusion is no evidence that the
    Analysis passes, so including it would be a silent pass.
    """
    unproven = [
        f"{finding.name} is {finding.result.value}"
        + (f" ({finding.detail})" if finding.detail else "")
        for finding in _exclusions(findings)
        if finding.result in (Result.UNMEASURED, Result.INVALID)
    ]
    if unproven:
        raise QualityCheckError(
            f"{analysis_id}: refusing to include an Analysis an exclusion Quality Check "
            "could not test: " + "; ".join(unproven)
        )


# ---------------------------------------------------------------------------
# Recording results
# ---------------------------------------------------------------------------


def declared_checks(findings: Sequence[Finding]) -> tuple[QualityCheck, ...]:
    return tuple(finding.check for finding in findings if finding.check is not None)


def sidecar_columns(declared: Sequence[QualityCheck]) -> tuple[str, ...]:
    columns = list(QUALITY_CHECK_LEADING_COLUMNS)
    for check in declared:
        columns.extend((check.name, f"{check.name}{QUALITY_CHECK_MEASURED_SUFFIX}"))
    return tuple(columns)


def sidecar_row(
    analysis_id: str,
    included: bool,
    exclusion_reason: str,
    findings: Sequence[Finding],
) -> dict[str, str]:
    """One Analysis's ``sidecars/quality_checks.tsv`` row."""
    row = {
        "analysis_id": analysis_id,
        "exclude_from_build": "" if included else "true",
        "exclusion_reason": "" if included else exclusion_reason,
    }
    for finding in findings:
        row[finding.name] = finding.result.value
        row[f"{finding.name}{QUALITY_CHECK_MEASURED_SUFFIX}"] = finding.measured
    return row


@dataclass(frozen=True)
class Decided:
    """One Analysis as the release policy left it, for :func:`summarise`."""

    included: bool
    exclusion_reason: str
    findings: Sequence[Finding]


@dataclass(frozen=True)
class CheckSummary:
    """One check's ``validation.yaml`` ``quality_checks`` entry.

    ``consequence`` counts the Analyses it excluded (an exclusion) or the
    included Analyses it warned about (a warning). ``warned`` counts, for an
    exclusion that ``warns``, the included Analyses with a ``warning`` result;
    ``None`` for any other check.
    """

    name: str
    kind: Kind
    stage: Stage
    measures: str
    threshold: Any
    default_threshold: Any
    overridden: bool
    results: Mapping[Result, int]
    consequence: int
    warned: int | None = None

    def as_dict(self) -> dict[str, Any]:
        document = {
            "kind": self.kind.value,
            "stage": self.stage.value,
            "measures": self.measures,
            "threshold": self.threshold,
            "default_threshold": self.default_threshold,
            "overridden": self.overridden,
            "results": {result.value: count for result, count in self.results.items()},
            ("excluded" if self.kind is Kind.EXCLUSION else "warned"): self.consequence,
        }
        if self.warned is not None:
            document["warned"] = self.warned
        return document


def summarise(
    decided: Sequence[Decided], overrides: Mapping[str, Any]
) -> dict[str, CheckSummary]:
    """Each applied check's summary, keyed by check name."""
    thresholds = effective_thresholds(overrides)
    declared = declared_checks(decided[0].findings) if decided else checks()
    counts = {check.name: {result: 0 for result in Result} for check in declared}
    consequence = {check.name: 0 for check in declared}
    warned = {check.name: 0 for check in declared}
    for analysis in decided:
        for finding in analysis.findings:
            counts[finding.name][finding.result] += 1
            if finding.check.kind is Kind.EXCLUSION:
                consequence[finding.name] += (
                    not analysis.included and analysis.exclusion_reason in finding.check.reasons
                )
                warned[finding.name] += analysis.included and finding.result is Result.WARNING
            else:
                consequence[finding.name] += (
                    analysis.included and finding.result is Result.FAILED
                )
    return {
        check.name: CheckSummary(
            name=check.name,
            kind=check.kind,
            stage=check.stage,
            measures=check.measures,
            threshold=thresholds.get(check.name),
            default_threshold=check.default_threshold,
            overridden=check.name in overrides,
            results=counts[check.name],
            consequence=consequence[check.name],
            warned=warned[check.name] if check.warns else None,
        )
        for check in declared
    }


def warning_lines(summaries: Mapping[str, CheckSummary]) -> list[str]:
    """One review warning per warning check that found something."""
    lines = []
    for summary in summaries.values():
        if summary.kind is Kind.WARNING and summary.consequence:
            lines.append(
                f"{summary.consequence} included Analysis/Analyses failed the "
                f"{summary.name} Quality Check "
                + (f"(threshold {summary.threshold}) " if summary.threshold is not None else "")
                + "and were kept with the finding recorded; see sidecars/quality_checks.tsv"
            )
    return lines


__all__ = [
    "BUILT_IN_CHECKS",
    "EAF_ORIENTATION_OUTCOMES",
    "NON_QUANTITATIVE_EFFECT_SCALES",
    "CheckSummary",
    "Decided",
    "Finding",
    "Kind",
    "QualityCheck",
    "QualityCheckError",
    "Result",
    "Stage",
    "Subject",
    "checks",
    "declared_checks",
    "diagnostic_count",
    "effective_thresholds",
    "evaluate",
    "exclusion_categories",
    "first_exclusion",
    "MAF_STATES_COUNTING_MISSING",
    "no_allele_frequency",
    "parse_overrides",
    "receipt_contract",
    "record_section",
    "register",
    "registered",
    "require_measured",
    "sidecar_columns",
    "sidecar_row",
    "summarise",
    "unregister",
    "warning_lines",
]
