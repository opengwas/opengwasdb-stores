#!/usr/bin/env python3
"""Real Analysis-metadata resolution for the Store Release workflow (issue #99).

The resolve phase used to be a passthrough: it froze ``source_file`` to an
absolute path and emitted ``work/analyses.resolved.tsv`` unchanged in every
interpretation-bearing column, while the *generator* wrote ancestry and
effect-scale values back into the committed ``analyses.tsv``. That made the
"fixed input" mutable, so a rebuild was not reproducible from the registry
alone.

This module is the real computation, done registry-side, over the fixed input
and only ever writing a *new* resolved table:

* **Ancestry.** For every Analysis with usable source allele frequencies, its
  A1-oriented AF profile is fitted against the declared ``ancestry_mixture``
  Reference Resource with :func:`opengwasdb.ancestry.assign_ancestry`, producing
  the Assigned Ancestry, the Ancestry Assignment Method, and the per-super-
  population ancestry proportions. An Analysis with no usable source AF keeps
  its declared ``source_trusted_no_af`` trust (issue #11's settled policy); an
  attempted fit that fails a gate becomes ``unassigned`` rather than silently
  keeping a prior label.
* **Effect scale / phenotype SD.** For every quantitative Analysis, the implied
  phenotype SD is computed from the source's own standard errors and
  A1-oriented allele frequencies with
  :func:`opengwasdb.build.phenotype_sd.estimate_phenotype_sd`. A
  ``declared_standardised`` Analysis is not rescaled: its implied SD is compared
  against 1 within ``sd_tolerance`` and recorded (``passed`` / ``warning`` /
  ``failed``). An Analysis whose phenotype SD could not be established upstream
  (``original_sd_method: unavailable``) is given the estimate and the
  ``estimated_from_source_maf`` method, which is what then reaches the Store.
* **Reporting.** Every Analysis whose Analytical Metadata was derived rather
  than declared is named in the resolution report, with the fields that were
  derived.

A failed effect-scale check is *evidence*, not a workflow failure: the
completion-run caller records it in ``validation.yaml`` and lands the release as
``built`` (issue #99 decision 2). A family that wants it blocking sets
``effect_scale_validation.block_on_failure: yes``, which this module surfaces as
``ResolutionResult.blocked``.

The one Reader pass per source file yields both the ancestry AF map and the
SD-estimation SE/AF arrays, so resolution reads each source once.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

#: Registry columns a resolution cannot proceed without. These are the same
#: columns the shared builder manifest requires (`release_manifest.py`), so a
#: release that resolves is a release that can be translated for the builder.
REQUIRED_COLUMNS: tuple[str, ...] = ("analysis_id", "stored_effect_scale")

#: Prefix identifying the data-discovered ancestry-proportion columns.
ANCESTRY_PROP_PREFIX = "ancestry_prop_"

#: Free-text source ancestry labels that are precise enough to compare against
#: an AF-based super-population call. Ambiguous labels (Multiple/Mixed,
#: NR/Unknown, Other) are deliberately absent so no mismatch is fabricated.
SOURCE_LABEL_TO_SUPERPOP: dict[str, str] = {
    "African": "AFR",
    "East Asian": "EAS",
    "European": "EUR",
    "South Asian": "SAS",
    "South East Asian": "EAS",
    "Greater Middle Eastern": "MID",
    "Hispanic or Latin American": "AMR",
    "Native American": "AMR",
}

_TRUE_STRINGS = {"true", "yes", "1", "on"}

#: Non-quantitative effect scales: no phenotype SD is defined for them.
_NON_QUANTITATIVE_SCALES = {"log_or", "log_hazard"}

#: The method tier this module computes (ADR-0029). Reference-AF fallback and
#: the beta-distribution tier are separate, later work; source AF is what the
#: FinnGen and ukb-b sources carry.
ESTIMATED_FROM_SOURCE_MAF = "estimated_from_source_maf"

_DEFAULT_SD_EFFECT_SCALE = "sd"


class ResolutionError(Exception):
    """A release whose metadata resolution cannot proceed."""


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_STRINGS
    return bool(value)


def _as_float(value: object, key: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ResolutionError(f"{key}: {value!r} is not a number") from exc


def _as_int(value: object, key: str) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise ResolutionError(f"{key}: {value!r} is not an integer") from exc


def _mapping(container: Mapping, key: str) -> dict:
    value = container.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ResolutionError(f"{key}: must be a mapping")
    return value


def _data_path(repo_root: Path, value: object, *, key: str) -> Path:
    """Resolve a Reference Resource path declaration to a filesystem path.

    Registry-relative paths are resolved against the repository root, so a
    small tracked resource (a fixture panel, the QC panel) is found regardless
    of the working directory; an absolute external path is used as declared.
    """
    if not isinstance(value, str) or not value.strip():
        raise ResolutionError(f"{key}: must be a non-empty path")
    path = Path(value)
    return path if path.is_absolute() else repo_root / path


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AncestrySettings:
    """The release's `ancestry_assignment` block, parsed."""

    reference_resource_id: str
    maf_floor: float
    tau: float
    delta: float
    n_min: int
    residual_max: float


@dataclass(frozen=True)
class EffectScaleSettings:
    """The release's `effect_scale_validation` block, parsed."""

    min_overlap_variants: int
    maf_min: float
    maf_max: float
    sd_tolerance: float
    warning_multiplier: float
    dispersion_max: float
    block_on_failure: bool


def ancestry_settings(config: Mapping) -> AncestrySettings | None:
    """The enabled `ancestry_assignment` block, or None when not opted in."""
    block = _mapping(config, "ancestry_assignment")
    if not block or not _as_bool(block.get("enabled", False)):
        return None
    resource_id = block.get("reference_resource_id")
    if not isinstance(resource_id, str) or not resource_id.strip():
        raise ResolutionError("ancestry_assignment.reference_resource_id: required when enabled")
    gates = _mapping(block, "gates")
    return AncestrySettings(
        reference_resource_id=resource_id,
        maf_floor=_as_float(block.get("maf_floor", 0.01), "ancestry_assignment.maf_floor"),
        tau=_as_float(gates.get("tau", 0.50), "ancestry_assignment.gates.tau"),
        delta=_as_float(gates.get("delta", 0.20), "ancestry_assignment.gates.delta"),
        n_min=_as_int(gates.get("n_min", 5000), "ancestry_assignment.gates.n_min"),
        residual_max=_as_float(
            gates.get("residual_max", 0.06), "ancestry_assignment.gates.residual_max"
        ),
    )


def effect_scale_settings(config: Mapping) -> EffectScaleSettings | None:
    """The enabled `effect_scale_validation` block, or None when not opted in."""
    block = _mapping(config, "effect_scale_validation")
    if not block or not _as_bool(block.get("enabled", False)):
        return None
    thresholds = _mapping(block, "thresholds")
    return EffectScaleSettings(
        min_overlap_variants=_as_int(
            block.get("min_overlap_variants", thresholds.get("min_overlap_variants", 20)),
            "effect_scale_validation.min_overlap_variants",
        ),
        maf_min=_as_float(block.get("maf_min", thresholds.get("maf_min", 0.01)), "effect_scale_validation.maf_min"),
        maf_max=_as_float(block.get("maf_max", thresholds.get("maf_max", 0.5)), "effect_scale_validation.maf_max"),
        sd_tolerance=_as_float(
            block.get("sd_tolerance", thresholds.get("sd_tolerance", 0.15)),
            "effect_scale_validation.sd_tolerance",
        ),
        warning_multiplier=_as_float(
            block.get("warning_multiplier", thresholds.get("warning_multiplier", 2.0)),
            "effect_scale_validation.warning_multiplier",
        ),
        dispersion_max=_as_float(
            block.get("dispersion_max", thresholds.get("dispersion_max", 0.5)),
            "effect_scale_validation.dispersion_max",
        ),
        block_on_failure=_as_bool(block.get("block_on_failure", False)),
    )


def reference_resource(config: Mapping, resource_id: str) -> dict:
    """The declared Reference Resource with `resource_id`, or raise."""
    resources = config.get("reference_resources") or []
    if not isinstance(resources, list):
        raise ResolutionError("reference_resources: must be a list")
    for entry in resources:
        if isinstance(entry, dict) and entry.get("resource_id") == resource_id:
            return entry
    raise ResolutionError(
        f"reference_resources: no declaration with resource_id={resource_id!r}"
    )


# ---------------------------------------------------------------------------
# One Reader pass per source file
# ---------------------------------------------------------------------------


@dataclass
class SourceMetrics:
    """The per-Analysis source evidence one Reader pass yields.

    ``af_by_alid`` is the non-palindromic ``{canonical_alid: A1 frequency}`` map
    the ancestry mixture fit consumes; ``se``/``af`` are the aligned arrays the
    phenotype-SD estimator consumes. A row without a usable frequency is
    excluded from both rather than defaulted.
    """

    af_by_alid: dict[str, float] = field(default_factory=dict)
    se: list[float] = field(default_factory=list)
    af: list[float] = field(default_factory=list)

    @property
    def has_usable_af(self) -> bool:
        return bool(self.af_by_alid)


def read_source_metrics(path: Path, capability: str, stored_effect_scale: str) -> SourceMetrics:
    """One pass over one Analysis's source file: AF map plus SE/AF arrays.

    Uses the same :class:`~opengwasdb.readers.interface.SourceReader` seam the
    builders use, resolved from the row's ``source_reader_capability``, so the
    AF/SE extraction cannot drift from what the build reads. Each association is
    canonicalised to its A1-oriented ALID and frequency; the frequency is the
    stored (A1) allele's, exactly as the reader documents.
    """
    from opengwasdb.model.enums import StoredEffectScale
    from opengwasdb.readers import is_palindromic, resolve_reader
    from opengwasdb.variants.normalise import VariantNormalisationError, orient_to_canonical

    try:
        scale = StoredEffectScale(stored_effect_scale)
    except ValueError as exc:
        raise ResolutionError(f"stored_effect_scale {stored_effect_scale!r} is not a controlled value") from exc

    metrics = SourceMetrics()
    reader = resolve_reader(capability, str(path), scale)
    for association in reader.stream_associations():
        af = association.eaf
        if af is None or not 0.0 < af < 1.0:
            continue
        try:
            orientation = orient_to_canonical(
                association.chromosome, association.position, association.ref, association.alt
            )
        except VariantNormalisationError:
            continue
        if not is_palindromic(association.ref, association.alt):
            metrics.af_by_alid[orientation.variant.alid] = af
        if association.se > 0:
            metrics.se.append(association.se)
            metrics.af.append(af)
    return metrics


# ---------------------------------------------------------------------------
# Ancestry assignment
# ---------------------------------------------------------------------------


@dataclass
class AncestryOutcome:
    assigned_ancestry: str
    method: str
    status: str  # af_assigned | unassigned | source_trusted_no_af
    proportions: dict[str, float] = field(default_factory=dict)
    gate_reason: str = ""
    dominant_superpop: str = ""
    af_overlap: int = 0
    attempt: bool = False


def assign_analysis_ancestry(
    row: Mapping[str, str],
    metrics: SourceMetrics,
    reference,
    settings: AncestrySettings,
) -> AncestryOutcome:
    """Assign one Analysis's ancestry, or preserve declared trust when it cannot.

    Mirrors the release-bundle ancestry stage (issues #23/#25): only an Analysis
    with usable source AF attempts a fit; a failure leaves it ``unassigned``
    rather than keeping a prior label that would imply validation never ran; no
    usable source AF preserves the declared ``source_trusted_no_af``.
    """
    from opengwasdb.ancestry import Gates, assign_ancestry

    if not metrics.has_usable_af:
        return AncestryOutcome(
            assigned_ancestry=row.get("assigned_ancestry", ""),
            method=row.get("ancestry_assignment_method", "") or "source_trusted_no_af",
            status="source_trusted_no_af",
        )
    result = assign_ancestry(
        metrics.af_by_alid,
        reference,
        Gates(
            tau=settings.tau,
            delta=settings.delta,
            n_min=settings.n_min,
            residual_max=settings.residual_max,
        ),
    )
    if result.gate_reason == "ok":
        return AncestryOutcome(
            assigned_ancestry=str(result.assigned_ancestry or ""),
            method="af_assigned",
            status="af_assigned",
            proportions=dict(result.superpop_composition),
            gate_reason=result.gate_reason,
            dominant_superpop=str(result.dominant_superpop or ""),
            af_overlap=result.af_overlap,
            attempt=True,
        )
    return AncestryOutcome(
        assigned_ancestry="",
        method="unassigned",
        status="unassigned",
        proportions=dict(result.superpop_composition),
        gate_reason=result.gate_reason,
        dominant_superpop=str(result.dominant_superpop or ""),
        af_overlap=result.af_overlap,
        attempt=True,
    )


# ---------------------------------------------------------------------------
# Effect scale / phenotype SD
# ---------------------------------------------------------------------------


@dataclass
class EffectScaleOutcome:
    status: str  # passed | warning | failed | skipped
    skip_reason: str = ""
    original_sd: str = ""
    original_sd_method: str = ""
    implied_sd_median: float = float("nan")
    dispersion: float = float("nan")
    n_retained: int = 0
    notes: str = ""


def assess_effect_scale(
    row: Mapping[str, str],
    metrics: SourceMetrics,
    settings: EffectScaleSettings,
) -> EffectScaleOutcome:
    """Compute or verify one Analysis's effect scale and phenotype SD.

    Quantitative, ``declared_standardised`` Analyses are verified (implied SD
    against 1) and never rescaled. Quantitative Analyses whose phenotype SD
    could not be established upstream are given the source-MAF estimate, which
    is what then reaches the built Store. Binary Analyses are explicitly
    ``skipped`` (no phenotype SD).
    """
    from opengwasdb.build.phenotype_sd import estimate_phenotype_sd
    from opengwasdb.model.enums import OriginalSdMethod

    stored_scale = row.get("stored_effect_scale", "")
    declared_method = row.get("original_sd_method", "")
    if stored_scale in _NON_QUANTITATIVE_SCALES:
        return EffectScaleOutcome(
            status="skipped",
            skip_reason="non_quantitative_effect_scale",
            original_sd=row.get("original_sd", ""),
            original_sd_method=declared_method,
            notes="binary/log-hazard analyses have no phenotype SD to estimate",
        )
    if not metrics.af or not metrics.se:
        return EffectScaleOutcome(
            status="skipped",
            skip_reason="no_usable_source_af",
            original_sd=row.get("original_sd", ""),
            original_sd_method=declared_method,
            notes="no source variant carried both a usable allele frequency and a standard error",
        )

    import numpy as np

    # MAF bounds are the release's; a variant outside them is not usable evidence
    # for the implied-SD summary (ADR-0029 thresholds, issue #16).
    se = np.asarray(metrics.se, dtype=float)
    af = np.asarray(metrics.af, dtype=float)
    maf = np.minimum(af, 1.0 - af)
    within_bounds = (maf >= settings.maf_min) & (maf <= settings.maf_max)
    se, af = se[within_bounds], af[within_bounds]

    sample_size = _as_float(row.get("sample_size", ""), f"{row.get('analysis_id')}:sample_size")
    estimate = estimate_phenotype_sd(
        OriginalSdMethod.ESTIMATED_FROM_SOURCE_MAF,
        sample_size,
        se=se,
        af=af,
    )
    median_sd = float(estimate.sd)
    dispersion = float(estimate.dispersion)
    n_retained = int(se.size)

    if n_retained < settings.min_overlap_variants:
        return EffectScaleOutcome(
            status="skipped", skip_reason="low_overlap",
            original_sd=row.get("original_sd", ""), original_sd_method=declared_method,
            implied_sd_median=median_sd, dispersion=dispersion, n_retained=n_retained,
            notes=f"only {n_retained} usable variants, fewer than {settings.min_overlap_variants}",
        )
    if not (median_sd == median_sd) or median_sd in (float("inf"), float("-inf")):
        return EffectScaleOutcome(
            status="failed", skip_reason="no_implied_sd",
            original_sd=row.get("original_sd", ""), original_sd_method=declared_method,
            n_retained=n_retained, notes="no finite implied phenotype SD could be computed",
        )
    if not (dispersion == dispersion) or dispersion > settings.dispersion_max:
        status, reason = "warning", "unstable_implied_sd"
    elif declared_method == "declared_standardised":
        delta = abs(median_sd - 1.0)
        if delta <= settings.sd_tolerance:
            status, reason = "passed", ""
        elif delta <= settings.sd_tolerance * settings.warning_multiplier:
            status, reason = "warning", "scale_inconsistent"
        else:
            status, reason = "failed", "scale_inconsistent"
    else:
        status, reason = "passed", ""

    original_sd = row.get("original_sd", "")
    original_sd_method = declared_method
    if status in ("passed", "warning") and declared_method != "declared_standardised" and not original_sd:
        original_sd = f"{median_sd:.6g}"
        original_sd_method = ESTIMATED_FROM_SOURCE_MAF

    notes = (
        f"{reason} (median implied SD={median_sd:.3f}, dispersion={dispersion:.3f}, n={n_retained})"
        if reason
        else f"median implied SD={median_sd:.3f}, dispersion={dispersion:.3f}, n={n_retained}"
    )
    return EffectScaleOutcome(
        status=status, original_sd=original_sd, original_sd_method=original_sd_method,
        implied_sd_median=median_sd, dispersion=dispersion, n_retained=n_retained, notes=notes,
    )


# ---------------------------------------------------------------------------
# Release-level resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolutionResult:
    """One release's resolved table, report, checks, and escalation state."""

    fieldnames: list[str]
    rows: list[dict[str, str]]
    report_columns: list[str]
    report_rows: list[dict[str, str]]
    checks: dict[str, str]
    warnings: list[str]
    derived_analysis_ids: list[str]
    blocked: bool  # escalation was requested AND a failure occurred


def _row_fieldnames(fieldnames: Sequence[str], proportions: Sequence[str]) -> list[str]:
    columns = list(fieldnames)
    for population in proportions:
        column = f"{ANCESTRY_PROP_PREFIX}{population}"
        if column not in columns:
            columns.append(column)
    return columns


def _checks(
    ancestry: AncestrySettings | None,
    effect_scale: EffectScaleSettings | None,
    ancestry_outcomes: Sequence[AncestryOutcome],
    effect_outcomes: Sequence[EffectScaleOutcome],
    mismatch: bool,
) -> dict[str, str]:
    if ancestry is None or not any(o.attempt for o in ancestry_outcomes):
        ancestry_check = "not_run"
    elif any(o.status == "unassigned" for o in ancestry_outcomes) or mismatch:
        ancestry_check = "passed_with_warnings"
    else:
        ancestry_check = "passed"

    attempted = [o for o in effect_outcomes if o.status in ("passed", "warning", "failed")]
    if effect_scale is None or not attempted:
        effect_check = "not_run"
    elif any(o.status == "failed" for o in attempted):
        effect_check = "failed"
    elif any(o.status == "warning" for o in attempted):
        effect_check = "passed_with_warnings"
    else:
        effect_check = "passed"
    return {
        "ancestry": ancestry_check,
        "effect_scale": effect_check,
        "sd_estimation": effect_check,
    }


def resolve_analyses(
    rows: Sequence[Mapping[str, str]],
    *,
    fieldnames: Sequence[str],
    source_root: Path,
    config: Mapping,
    repo_root: Path,
) -> ResolutionResult:
    """Resolve every buildable Analysis's ancestry and effect scale.

    Reads each Analysis's source file once, writes no input, and returns the
    resolved rows (the committed columns plus the data-discovered
    ``ancestry_prop_*`` columns), the resolution report, the release-level
    checks, and whether a failure should block the workflow.
    """
    ancestry = ancestry_settings(config)
    effect_scale = effect_scale_settings(config)

    reference = None
    if ancestry is not None:
        from opengwasdb.ancestry import load_reference

        declaration = reference_resource(config, ancestry.reference_resource_id)
        location = _data_path(repo_root, declaration.get("location"), key="reference_resources.location")
        fine_group_map = _data_path(
            repo_root, declaration.get("fine_group_map"), key="reference_resources.fine_group_map"
        )
        if not location.is_file():
            raise ResolutionError(f"ancestry reference not found: {location}")
        if not fine_group_map.is_file():
            raise ResolutionError(f"ancestry fine-group map not found: {fine_group_map}")
        reference = load_reference(str(location), str(fine_group_map), maf_floor=ancestry.maf_floor)

    proportions: list[str] = list(getattr(reference, "superpops", []) or [])
    resolved_fieldnames = _row_fieldnames(fieldnames, proportions)
    resolved_rows: list[dict[str, str]] = []
    report_rows: list[dict[str, str]] = []
    ancestry_outcomes: list[AncestryOutcome] = []
    effect_outcomes: list[EffectScaleOutcome] = []
    warnings: list[str] = []
    derived_ids: list[str] = []
    mismatch_seen = False

    for row in rows:
        analysis_id = row.get("analysis_id", "")
        missing = [column for column in REQUIRED_COLUMNS if not row.get(column)]
        if missing:
            raise ResolutionError(f"{analysis_id or 'row'}: missing required column(s): {', '.join(missing)}")

        value = (row.get("source_file") or row.get("file_name") or "").strip()
        if not value:
            raise ResolutionError(f"{analysis_id}: names no source file")
        source = Path(value)
        absolute = source if source.is_absolute() else source_root / source

        capability = row.get("source_reader_capability") or "opengwasdb.gwas-vcf"
        stored_scale = row.get("stored_effect_scale") or _DEFAULT_SD_EFFECT_SCALE
        metrics = read_source_metrics(absolute, capability, stored_scale)

        derived_fields: list[str] = []
        resolved = {**row, "source_file": str(absolute)}

        if ancestry is not None:
            outcome = assign_analysis_ancestry(row, metrics, reference, ancestry)
            ancestry_outcomes.append(outcome)
            resolved["assigned_ancestry"] = outcome.assigned_ancestry
            resolved["ancestry_assignment_method"] = outcome.method
            for population in proportions:
                resolved[f"{ANCESTRY_PROP_PREFIX}{population}"] = (
                    f"{outcome.proportions.get(population, 0.0):.6g}" if outcome.attempt else ""
                )
            if outcome.attempt:
                derived_fields.extend(
                    ["assigned_ancestry", "ancestry_assignment_method"]
                    + [f"{ANCESTRY_PROP_PREFIX}{population}" for population in proportions]
                )
            if outcome.status == "unassigned":
                warnings.append(f"{analysis_id}: ancestry gate failed ({outcome.gate_reason})")
            elif outcome.status == "af_assigned":
                expected = SOURCE_LABEL_TO_SUPERPOP.get(row.get("source_ancestry_label", ""))
                if expected is not None and expected != outcome.dominant_superpop:
                    mismatch_seen = True
                    warnings.append(
                        f"{analysis_id}: source_ancestry_label="
                        f"{row.get('source_ancestry_label')!r} disagrees with AF-based "
                        f"dominant_superpop={outcome.dominant_superpop!r}"
                    )

        effect = None
        if effect_scale is not None:
            effect = assess_effect_scale(row, metrics, effect_scale)
            effect_outcomes.append(effect)
            resolved["original_sd"] = effect.original_sd
            resolved["original_sd_method"] = effect.original_sd_method
            if effect.original_sd and effect.original_sd != row.get("original_sd", ""):
                derived_fields.extend(["original_sd", "original_sd_method"])
            if effect.status == "failed":
                warnings.append(f"{analysis_id}: empirical effect-scale status=failed ({effect.notes})")
            elif effect.status == "warning":
                warnings.append(f"{analysis_id}: empirical effect-scale status=warning ({effect.notes})")
            elif effect.skip_reason == "low_overlap":
                warnings.append(f"{analysis_id}: skipped effect-scale, low source-AF overlap")

        resolved_rows.append(resolved)
        if derived_fields:
            derived_ids.append(analysis_id)
        report_rows.append({
            "analysis_id": analysis_id,
            "source_file": str(absolute),
            "derived_fields": ",".join(derived_fields),
            "assigned_ancestry": resolved.get("assigned_ancestry", ""),
            "ancestry_assignment_method": resolved.get("ancestry_assignment_method", ""),
            "ancestry_status": ancestry_outcomes[-1].status if ancestry is not None else "",
            "effect_scale_status": effect.status if effect is not None else "",
            "phenotype_sd": resolved.get("original_sd", ""),
            "sd_method": resolved.get("original_sd_method", ""),
            "resolution_status": "derived" if derived_fields else "declared",
            "notes": effect.notes if effect is not None else "",
            **{
                f"{ANCESTRY_PROP_PREFIX}{population}": resolved.get(
                    f"{ANCESTRY_PROP_PREFIX}{population}", ""
                )
                for population in proportions
            },
        })

    checks = _checks(ancestry, effect_scale, ancestry_outcomes, effect_outcomes, mismatch_seen)
    blocked = bool(effect_scale is not None and effect_scale.block_on_failure and checks["effect_scale"] == "failed")
    report_columns = [
        "analysis_id", "source_file", "derived_fields", "assigned_ancestry",
        "ancestry_assignment_method", "ancestry_status", "effect_scale_status",
        "phenotype_sd", "sd_method", "resolution_status",
        *[f"{ANCESTRY_PROP_PREFIX}{population}" for population in proportions],
        "notes",
    ]
    return ResolutionResult(
        fieldnames=resolved_fieldnames,
        rows=resolved_rows,
        report_columns=report_columns,
        report_rows=report_rows,
        checks=checks,
        warnings=warnings,
        derived_analysis_ids=derived_ids,
        blocked=blocked,
    )
