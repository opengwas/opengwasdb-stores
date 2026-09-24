#!/usr/bin/env python3
"""Validate a chooser against the held-out Trait Ontology Mapping set (issue #168).

Retrieval and choice are separate skills and must be measured separately.
:mod:`curation.recall` scores *retrieval* -- whether the correct ontology term
ever reached the shortlist. This module scores *choice*: given that the correct
term is in the shortlist, does the chooser pick it? Reporting a chooser's
accuracy over the whole validation set would silently fold retrieval failures
into the chooser's score, so every pair whose correct term was not retrieved is
excluded here and counted as a retrieval miss instead.

What it does
------------
- Reads the harvested validation set (:mod:`curation.harvest`) and the candidate
  shortlists (:mod:`curation.candidates`).
- Restricts scoring to pairs whose ground-truth ontology id is in the shortlist
  -- choice accuracy *conditional on retrieval*.
- Runs the chooser once per distinct trait label and compares its selection to
  the ground truth.
- Bins each prediction's reported probability against whether it was correct,
  producing a reliability/calibration curve.
- Reports accuracy per stratum (analyte measurement vs disease, plus ``other``)
  and in aggregate.
- States the evidence caveats plainly, including that the validation set is
  almost entirely analyte measurements and whether the disease stratum is large
  enough to mean anything.
- Recommends a promotion confidence threshold and a runner-up margin threshold
  from the observed curve.
- Records cost per label and total spend when the chooser reports it (live
  runs).

Explicit invocation
-------------------
This is a validation harness, not a CI test. Nothing here runs during
``pixi run test-*``. A live run against a hosted Jev endpoint additionally
requires ``--live``; without it the command refuses to open a socket. Offline
runs replay a fixture chooser instead.

CLI
---
::

    python3 -m curation.validate_chooser \\
        --validation <validation.tsv> --shortlists <shortlist.tsv> \\
        --chooser stub --fixture <fixture.json>

    # live (explicit): never runs during standard CI
    python3 -m curation.validate_chooser \\
        --validation <validation.tsv> --shortlists <shortlist.tsv> \\
        --chooser jev --jev-endpoint https://... --live
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from curation.choice import (
    ChoiceError,
    Candidate,
    build_chooser,
    build_proposal,
    read_shortlists,
)
from curation.chooser import Chooser
from curation.gap_scan import normalize_trait_label
from curation.harvest import (
    STRATUM_ANALYTE_MEASUREMENT,
    STRATUM_DISEASE,
    STRATUM_ORDER,
)
from curation.recall import ValidationPair, read_validation

# The number of equal-width probability bins in the reliability curve.
DEFAULT_RELIABILITY_BINS: int = 10

# A promotion threshold is only recommended when the retained slice reaches
# this observed accuracy.
DEFAULT_TARGET_ACCURACY: float = 0.95

# A stratum smaller than this is reported but explicitly not treated as
# evidence: a handful of disease labels cannot establish disease accuracy.
DEFAULT_MIN_STRATUM_SAMPLE: int = 30


class ValidationError(ValueError):
    """Base error for a validation run that cannot proceed."""


class ValidationInputError(ValidationError):
    """Raised when the validation set or shortlist input is unusable."""


# ---------------------------------------------------------------------------
# Records and report structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChoiceRecord:
    """One conditional choice: the correct term was retrieved and scored."""

    trait_label: str
    stratum: str
    correct_ontology_id: str
    correct_ontology_label: str
    selected_ontology_id: str
    selected_ontology_label: str
    selected_probability: float
    runner_up_margin: float
    correct: bool
    chooser_id: str
    chooser_version: str


@dataclass(frozen=True)
class ReliabilityBin:
    """One bin of the calibration curve: predicted probability vs accuracy."""

    lower: float
    upper: float
    count: int
    correct: int
    mean_probability: float

    @property
    def observed_accuracy(self) -> float:
        """Observed accuracy for this bin (0.0 for an empty bin)."""
        return self.correct / self.count if self.count else 0.0


@dataclass(frozen=True)
class ThresholdRecommendation:
    """Promotion thresholds inferred from the reliability evidence."""

    confidence_threshold: float | None
    margin_threshold: float | None
    confidence_evidence: str
    margin_evidence: str


@dataclass(frozen=True)
class StratumReport:
    """Choice accuracy and calibration for one validation stratum."""

    stratum: str
    evaluated: int
    correct: int
    accuracy: float
    bins: tuple[ReliabilityBin, ...]
    too_small: bool


@dataclass(frozen=True)
class CostReport:
    """Per-label and total spend recorded from a cost-reporting chooser."""

    tracked: bool
    labels: int
    total_usd: float
    per_label: tuple[tuple[str, float], ...]

    @property
    def cost_per_label(self) -> float:
        """Mean spend per decided label (0.0 when nothing was tracked)."""
        return self.total_usd / self.labels if self.labels else 0.0


@dataclass(frozen=True)
class ValidationReport:
    """The full chooser validation: accuracy, calibration, caveats, and cost."""

    chooser_id: str
    chooser_version: str
    validation_size: int
    eligible: int
    evaluated: int
    skipped_obsolete: int
    skipped_no_shortlist: int
    skipped_not_retrieved: int
    skipped_no_proposal: int
    strata: tuple[StratumReport, ...]
    aggregate: StratumReport
    reliability: tuple[ReliabilityBin, ...]
    recommendation: ThresholdRecommendation
    cost: CostReport
    caveats: tuple[str, ...]

    def stratum(self, name: str) -> StratumReport | None:
        for report in self.strata:
            if report.stratum == name:
                return report
        return None

    @property
    def accuracy(self) -> float:
        return self.aggregate.accuracy


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _shortlist_lookup(
    shortlists: Mapping[str, Sequence[Candidate]],
) -> dict[str, tuple[str, list[Candidate]]]:
    """Index shortlists by the gap-scan-normalised trait label.

    The harvest's labels and the work queue's labels can differ in case and
    surrounding whitespace; keying on the same ``trimws(tolower(x))``
    normalisation the canonical-table lookup uses makes them meet.
    """
    lookup: dict[str, tuple[str, list[Candidate]]] = {}
    for label, candidates in shortlists.items():
        key = normalize_trait_label(label)
        if not key:
            continue
        lookup.setdefault(key, (label, list(candidates)))
    return lookup


def _select_records(
    pairs: Sequence[ValidationPair],
    lookup: Mapping[str, tuple[str, list[Candidate]]],
    chooser: Chooser,
) -> tuple[list[ChoiceRecord], dict[str, int]]:
    """Run the chooser once per retrievable label and build choice records."""
    counts = {
        "obsolete": 0,
        "no_shortlist": 0,
        "not_retrieved": 0,
        "no_proposal": 0,
    }

    by_label: "OrderedDict[str, list[ValidationPair]]" = OrderedDict()
    for pair in pairs:
        if pair.is_obsolete:
            counts["obsolete"] += 1
            continue
        key = normalize_trait_label(pair.trait_label)
        by_label.setdefault(key, []).append(pair)

    records: list[ChoiceRecord] = []
    for key, label_pairs in by_label.items():
        entry = lookup.get(key)
        if entry is None:
            counts["no_shortlist"] += len(label_pairs)
            continue
        raw_label, candidates = entry
        candidate_ids = {candidate.ontology_id for candidate in candidates}
        eligible = [pair for pair in label_pairs if pair.ontology_id in candidate_ids]
        counts["not_retrieved"] += len(label_pairs) - len(eligible)
        if not eligible:
            continue

        result = chooser.choose(raw_label, list(candidates))
        if result is None:
            # An empty shortlist yields no proposal; here the shortlist is
            # non-empty, so a None is the chooser explicitly declining.
            counts["no_proposal"] += len(eligible)
            continue

        proposal = build_proposal(raw_label, candidates, result)
        for pair in eligible:
            records.append(
                ChoiceRecord(
                    trait_label=raw_label,
                    stratum=pair.stratum,
                    correct_ontology_id=pair.ontology_id,
                    correct_ontology_label=pair.ontology_label,
                    selected_ontology_id=proposal.selected_ontology_id,
                    selected_ontology_label=proposal.selected_ontology_label,
                    selected_probability=proposal.confidence,
                    runner_up_margin=proposal.runner_up_margin,
                    correct=proposal.selected_ontology_id == pair.ontology_id,
                    chooser_id=proposal.chooser_id,
                    chooser_version=proposal.chooser_version,
                )
            )
    return records, counts


def reliability_bins(
    records: Sequence[ChoiceRecord],
    bins: int = DEFAULT_RELIABILITY_BINS,
) -> tuple[ReliabilityBin, ...]:
    """Bin reported probabilities against observed correctness.

    Each record contributes its *selected* probability and whether that
    selection was right, so the curve answers "when the chooser said 0.9, how
    often was it correct?". All bins are returned, including empty ones, so the
    curve's shape is visible.
    """
    if bins < 1:
        raise ValidationError(f"bins must be at least 1, got {bins}")
    width = 1.0 / bins
    counts = [0] * bins
    correct = [0] * bins
    probability_sums = [0.0] * bins

    for record in records:
        probability = min(max(record.selected_probability, 0.0), 1.0)
        index = min(int(probability * bins), bins - 1)
        counts[index] += 1
        correct[index] += 1 if record.correct else 0
        probability_sums[index] += probability

    result: list[ReliabilityBin] = []
    for index in range(bins):
        result.append(
            ReliabilityBin(
                lower=index * width,
                upper=(index + 1) * width,
                count=counts[index],
                correct=correct[index],
                mean_probability=(
                    probability_sums[index] / counts[index] if counts[index] else 0.0
                ),
            )
        )
    return tuple(result)


def _accuracy(records: Sequence[ChoiceRecord]) -> float:
    if not records:
        return 0.0
    return sum(1 for record in records if record.correct) / len(records)


def _stratum_report(
    name: str,
    records: Sequence[ChoiceRecord],
    bins: int,
    min_sample: int,
) -> StratumReport:
    total = len(records)
    correct = sum(1 for record in records if record.correct)
    return StratumReport(
        stratum=name,
        evaluated=total,
        correct=correct,
        accuracy=correct / total if total else 0.0,
        bins=reliability_bins(records, bins),
        too_small=total < min_sample,
    )


def _recommend_threshold(
    records: Sequence[ChoiceRecord],
    value_of: Callable[[ChoiceRecord], float],
    *,
    target_accuracy: float,
    min_sample: int,
    label: str,
) -> tuple[float | None, str]:
    """Pick the smallest threshold meeting target accuracy on enough evidence."""
    if not records:
        return None, f"no evaluated choices, so no {label} threshold is supported"

    candidates = sorted({value_of(record) for record in records})
    best_any: tuple[float, int, float] | None = None
    for threshold in candidates:
        kept = [record for record in records if value_of(record) >= threshold]
        accuracy = _accuracy(kept)
        if best_any is None or (accuracy, len(kept)) > (best_any[2], best_any[1]):
            best_any = (threshold, len(kept), accuracy)
        if len(kept) < min_sample:
            continue
        if accuracy >= target_accuracy:
            return threshold, (
                f"{label} >= {threshold:.3f} keeps {len(kept)}/{len(records)} "
                f"choices at {accuracy:.1%} accuracy"
            )

    if best_any is not None:
        threshold, kept, accuracy = best_any
        return threshold, (
            f"no {label} threshold reached {target_accuracy:.0%} accuracy on at "
            f"least {min_sample} choices; the best observed is {label} >= "
            f"{threshold:.3f} keeping {kept}/{len(records)} at {accuracy:.1%}. "
            "Treat this threshold as provisional."
        )
    return None, f"no {label} threshold could be estimated"


def _caveats(
    strata: Mapping[str, StratumReport],
    *,
    min_sample: int,
) -> tuple[str, ...]:
    """The honest limits of this validation set, stated plainly."""
    lines: list[str] = []
    total = sum(report.evaluated for report in strata.values())
    analyte = strata.get(STRATUM_ANALYTE_MEASUREMENT)
    disease = strata.get(STRATUM_DISEASE)

    if total == 0:
        lines.append(
            "No choice could be scored: no validation pair had its correct "
            "term in a shortlist. This is a retrieval result, not a choice result."
        )
        return tuple(lines)

    if analyte is not None and analyte.evaluated:
        lines.append(
            f"The validation set is almost entirely analyte measurements: "
            f"{analyte.evaluated}/{total} scored choices "
            f"({analyte.evaluated / total:.0%}) are {STRATUM_ANALYTE_MEASUREMENT}. "
            "Its accuracy does not establish disease-term accuracy."
        )
    else:
        lines.append(
            "The validation set contains no scored analyte-measurement choices; "
            "treat the aggregate with caution."
        )

    if disease is None or disease.evaluated == 0:
        lines.append(
            "The disease stratum is unmeasured: no disease-term pair had its "
            "correct term retrieved, so disease accuracy is unknown."
        )
    elif disease.too_small:
        lines.append(
            f"The disease stratum is too small to be evidence: only "
            f"{disease.evaluated} scored choice(s) (minimum "
            f"{min_sample} for a stable estimate). Do not generalize its "
            "accuracy."
        )
    else:
        lines.append(
            f"The disease stratum has {disease.evaluated} scored choices at "
            f"{disease.accuracy:.1%}; it is large enough to report, but still "
            "small relative to the analyte stratum."
        )

    lines.append(
        "Choice accuracy is conditional on retrieval: pairs whose correct term "
        "was not in the shortlist are excluded here and counted separately."
    )
    lines.append(
        "Promotion thresholds below are recommendations from this held-out set "
        "only and do not replace a live validation run."
    )
    return tuple(lines)


def _collect_cost(
    chooser: Chooser,
    records: Sequence[ChoiceRecord],
) -> CostReport:
    """Collect per-label spend a cost-reporting chooser exposed during the run."""
    raw = getattr(chooser, "cost_records", None)
    if not raw:
        return CostReport(tracked=False, labels=0, total_usd=0.0, per_label=())

    per_label: list[tuple[str, float]] = []
    for record in raw:
        try:
            label = str(getattr(record, "trait_label"))
            cost = float(getattr(record, "cost_usd"))
        except (AttributeError, TypeError, ValueError):
            continue
        if not math.isfinite(cost) or cost < 0.0:
            continue
        per_label.append((label, cost))

    # Only count costs incurred for the labels this report evaluated.
    evaluated = {record.trait_label for record in records}
    relevant = [
        (label, cost) for label, cost in per_label if not evaluated or label in evaluated
    ]
    total = math.fsum(cost for _, cost in relevant)
    return CostReport(
        tracked=True,
        labels=len(relevant),
        total_usd=total,
        per_label=tuple(relevant),
    )


def _eligible_count(
    pairs: Sequence[ValidationPair],
    lookup: Mapping[str, tuple[str, list[Candidate]]],
) -> int:
    """Count non-obsolete pairs whose correct term is in their shortlist."""
    total = 0
    for pair in pairs:
        if pair.is_obsolete:
            continue
        entry = lookup.get(normalize_trait_label(pair.trait_label))
        if entry is None:
            continue
        if pair.ontology_id in {candidate.ontology_id for candidate in entry[1]}:
            total += 1
    return total


def evaluate_chooser(
    pairs: Sequence[ValidationPair],
    shortlists: Mapping[str, Sequence[Candidate]],
    chooser: Chooser,
    *,
    bins: int = DEFAULT_RELIABILITY_BINS,
    target_accuracy: float = DEFAULT_TARGET_ACCURACY,
    min_stratum_sample: int = DEFAULT_MIN_STRATUM_SAMPLE,
) -> ValidationReport:
    """Score ``chooser`` on ``pairs`` whose correct term is in ``shortlists``.

    ``chooser`` is called once per distinct trait label. The returned report
    separates retrieval failures from choice failures, breaks accuracy down by
    stratum, and carries a probability reliability curve.
    """
    if not 0.0 < target_accuracy <= 1.0:
        raise ValidationError(
            f"target_accuracy must be in (0, 1], got {target_accuracy}"
        )
    if min_stratum_sample < 1:
        raise ValidationError(
            f"min_stratum_sample must be at least 1, got {min_stratum_sample}"
        )

    lookup = _shortlist_lookup(shortlists)
    records, counts = _select_records(pairs, lookup, chooser)

    strata_records: dict[str, list[ChoiceRecord]] = {}
    for record in records:
        strata_records.setdefault(record.stratum, []).append(record)

    ordered_strata = [
        name
        for name in STRATUM_ORDER
        if name in strata_records
    ] + sorted(name for name in strata_records if name not in STRATUM_ORDER)

    strata = tuple(
        _stratum_report(name, strata_records[name], bins, min_stratum_sample)
        for name in ordered_strata
    )
    strata_map = {report.stratum: report for report in strata}
    aggregate = _stratum_report("aggregate", records, bins, min_stratum_sample)

    confidence_threshold, confidence_evidence = _recommend_threshold(
        records,
        lambda record: record.selected_probability,
        target_accuracy=target_accuracy,
        min_sample=min_stratum_sample,
        label="confidence",
    )
    margin_threshold, margin_evidence = _recommend_threshold(
        records,
        lambda record: record.runner_up_margin,
        target_accuracy=target_accuracy,
        min_sample=min_stratum_sample,
        label="runner-up margin",
    )

    chooser_id = records[0].chooser_id if records else getattr(chooser, "chooser_id", "")
    chooser_version = (
        records[0].chooser_version
        if records
        else getattr(chooser, "chooser_version", "")
    )

    return ValidationReport(
        chooser_id=chooser_id,
        chooser_version=chooser_version,
        validation_size=len(pairs),
        eligible=_eligible_count(pairs, lookup),
        evaluated=len(records),
        skipped_obsolete=counts["obsolete"],
        skipped_no_shortlist=counts["no_shortlist"],
        skipped_not_retrieved=counts["not_retrieved"],
        skipped_no_proposal=counts["no_proposal"],
        strata=strata,
        aggregate=aggregate,
        reliability=reliability_bins(records, bins),
        recommendation=ThresholdRecommendation(
            confidence_threshold=confidence_threshold,
            margin_threshold=margin_threshold,
            confidence_evidence=confidence_evidence,
            margin_evidence=margin_evidence,
        ),
        cost=_collect_cost(chooser, records),
        caveats=_caveats(strata_map, min_sample=min_stratum_sample),
    )


def run_validation(
    *,
    validation_path: Path | str,
    shortlists_path: Path | str,
    chooser: Chooser,
    bins: int = DEFAULT_RELIABILITY_BINS,
    target_accuracy: float = DEFAULT_TARGET_ACCURACY,
    min_stratum_sample: int = DEFAULT_MIN_STRATUM_SAMPLE,
) -> ValidationReport:
    """Read the validation set and shortlists, then score ``chooser``."""
    validation_file = Path(validation_path)
    if not validation_file.is_file():
        raise ValidationInputError(
            f"validation set does not exist: {validation_file}"
        )
    shortlists_file = Path(shortlists_path)
    if not shortlists_file.is_file():
        raise ValidationInputError(f"shortlist does not exist: {shortlists_file}")

    pairs = read_validation(validation_file)
    shortlists = read_shortlists(shortlists_file)
    return evaluate_chooser(
        pairs,
        shortlists,
        chooser,
        bins=bins,
        target_accuracy=target_accuracy,
        min_stratum_sample=min_stratum_sample,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _format_percent(value: float) -> str:
    return f"{value:.1%}"


def render_report(report: ValidationReport) -> str:
    """Render the validation report as human-readable text."""
    lines: list[str] = []
    lines.append("Chooser validation report")
    lines.append("=========================")
    lines.append("")
    lines.append(
        f"chooser: {report.chooser_id or '(unknown)'} "
        f"version {report.chooser_version or '(unknown)'}"
    )
    lines.append(
        f"validation pairs: {report.validation_size}; "
        f"correct term retrieved: {report.eligible}; "
        f"scored (choice conditional on retrieval): {report.evaluated}"
    )
    lines.append(
        f"excluded: {report.skipped_obsolete} obsolete, "
        f"{report.skipped_no_shortlist} with no shortlist, "
        f"{report.skipped_not_retrieved} not retrieved, "
        f"{report.skipped_no_proposal} with no proposal"
    )
    lines.append("")

    lines.append("Accuracy by stratum")
    lines.append("-------------------")
    header = f"{'stratum':<22}{'scored':>8}{'correct':>9}{'accuracy':>10}"
    lines.append(header)
    for stratum in report.strata:
        note = "  (too small to be evidence)" if stratum.too_small else ""
        lines.append(
            f"{stratum.stratum:<22}{stratum.evaluated:>8}{stratum.correct:>9}"
            f"{_format_percent(stratum.accuracy):>10}{note}"
        )
    lines.append(
        f"{'aggregate':<22}{report.aggregate.evaluated:>8}"
        f"{report.aggregate.correct:>9}"
        f"{_format_percent(report.aggregate.accuracy):>10}"
    )
    lines.append("")

    lines.append("Reliability curve (reported probability vs observed accuracy)")
    lines.append("-------------------------------------------------------------")
    lines.append(f"{'bin':<16}{'n':>6}{'mean p':>9}{'observed':>10}{'gap':>9}")
    for bin_ in report.reliability:
        if bin_.count == 0:
            lines.append(f"{_format_bin(bin_):<16}{0:>6}{'-':>9}{'-':>10}{'-':>9}")
            continue
        gap = bin_.observed_accuracy - bin_.mean_probability
        lines.append(
            f"{_format_bin(bin_):<16}{bin_.count:>6}"
            f"{bin_.mean_probability:>9.3f}"
            f"{_format_percent(bin_.observed_accuracy):>10}"
            f"{gap:>+9.3f}"
        )
    lines.append("")

    lines.append("Recommendation")
    lines.append("--------------")
    if report.recommendation.confidence_threshold is None:
        lines.append(
            f"confidence threshold: not established -- "
            f"{report.recommendation.confidence_evidence}"
        )
    else:
        lines.append(
            f"confidence threshold: {report.recommendation.confidence_threshold:.3f} "
            f"({report.recommendation.confidence_evidence})"
        )
    if report.recommendation.margin_threshold is None:
        lines.append(
            f"runner-up margin threshold: not established -- "
            f"{report.recommendation.margin_evidence}"
        )
    else:
        lines.append(
            f"runner-up margin threshold: "
            f"{report.recommendation.margin_threshold:.3f} "
            f"({report.recommendation.margin_evidence})"
        )
    lines.append("")

    lines.append("Cost")
    lines.append("----")
    if report.cost.tracked:
        lines.append(
            f"tracked {report.cost.labels} label(s); total "
            f"${report.cost.total_usd:.4f}; mean "
            f"${report.cost.cost_per_label:.4f}/label"
        )
        for label, cost in report.cost.per_label:
            lines.append(f"  {label}: ${cost:.4f}")
    else:
        lines.append("not tracked (offline chooser reported no cost)")
    lines.append("")

    lines.append("Caveats")
    lines.append("-------")
    for caveat in report.caveats:
        lines.append(f"- {caveat}")
    lines.append("")
    return "\n".join(lines)


def _format_bin(bin_: ReliabilityBin) -> str:
    if bin_.lower == 0.0:
        return f"[0.00,{bin_.upper:.2f})"
    return f"[{bin_.lower:.2f},{bin_.upper:.2f})"


def report_to_json(report: ValidationReport) -> str:
    """Render the report as a machine-readable JSON document."""
    payload = {
        "chooser_id": report.chooser_id,
        "chooser_version": report.chooser_version,
        "validation_size": report.validation_size,
        "eligible": report.eligible,
        "evaluated": report.evaluated,
        "skipped": {
            "obsolete": report.skipped_obsolete,
            "no_shortlist": report.skipped_no_shortlist,
            "not_retrieved": report.skipped_not_retrieved,
            "no_proposal": report.skipped_no_proposal,
        },
        "strata": [
            {
                "stratum": stratum.stratum,
                "evaluated": stratum.evaluated,
                "correct": stratum.correct,
                "accuracy": stratum.accuracy,
                "too_small": stratum.too_small,
            }
            for stratum in report.strata
        ],
        "aggregate": {
            "evaluated": report.aggregate.evaluated,
            "correct": report.aggregate.correct,
            "accuracy": report.aggregate.accuracy,
        },
        "reliability": [
            {
                "lower": bin_.lower,
                "upper": bin_.upper,
                "count": bin_.count,
                "correct": bin_.correct,
                "mean_probability": bin_.mean_probability,
                "observed_accuracy": bin_.observed_accuracy,
            }
            for bin_ in report.reliability
        ],
        "recommendation": {
            "confidence_threshold": report.recommendation.confidence_threshold,
            "margin_threshold": report.recommendation.margin_threshold,
            "confidence_evidence": report.recommendation.confidence_evidence,
            "margin_evidence": report.recommendation.margin_evidence,
        },
        "cost": {
            "tracked": report.cost.tracked,
            "labels": report.cost.labels,
            "total_usd": report.cost.total_usd,
            "cost_per_label": report.cost.cost_per_label,
        },
        "caveats": list(report.caveats),
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="validate-chooser",
        description=(
            "Score a chooser on choice accuracy conditional on retrieval and "
            "produce a probability reliability curve. This is an explicit "
            "validation harness, never part of the standard CI test run."
        ),
    )
    parser.add_argument(
        "--validation",
        required=True,
        metavar="TSV",
        help="harvested validation set TSV",
    )
    parser.add_argument(
        "--shortlists",
        required=True,
        metavar="TSV",
        help="candidate shortlist TSV from curation.candidates",
    )
    parser.add_argument(
        "--chooser",
        default="stub",
        metavar="NAME",
        help="chooser to validate: stub or jev (default: stub)",
    )
    parser.add_argument(
        "--fixture",
        default=None,
        metavar="PATH",
        help="recorded choices for the stub chooser (JSON or TSV)",
    )
    parser.add_argument(
        "--jev-fixture",
        default=None,
        metavar="PATH",
        help="recorded Jev decisions for an offline Jev run (JSON or TSV)",
    )
    parser.add_argument(
        "--jev-endpoint",
        default=os.environ.get("OPENGWASDB_JEV_ENDPOINT"),
        metavar="URL",
        help="hosted Jev decision endpoint (requires --live)",
    )
    parser.add_argument(
        "--jev-model",
        default=os.environ.get("OPENGWASDB_JEV_MODEL", "jev-typesafe-v1"),
        metavar="MODEL",
        help="Jev model id to request",
    )
    parser.add_argument(
        "--jev-api-key",
        default=os.environ.get("OPENGWASDB_JEV_API_KEY"),
        metavar="KEY",
        help="bearer token for the hosted Jev endpoint",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "explicitly allow a live network run; required when a hosted Jev "
            "endpoint is used and never set by the standard test suite"
        ),
    )
    parser.add_argument(
        "--bins",
        type=int,
        default=DEFAULT_RELIABILITY_BINS,
        metavar="N",
        help=f"reliability-curve bins (default: {DEFAULT_RELIABILITY_BINS})",
    )
    parser.add_argument(
        "--target-accuracy",
        type=float,
        default=DEFAULT_TARGET_ACCURACY,
        metavar="FLOAT",
        help=(
            "accuracy a recommended threshold must reach "
            f"(default: {DEFAULT_TARGET_ACCURACY})"
        ),
    )
    parser.add_argument(
        "--min-stratum-sample",
        type=int,
        default=DEFAULT_MIN_STRATUM_SAMPLE,
        metavar="N",
        help=(
            "scored choices a stratum needs to count as evidence "
            f"(default: {DEFAULT_MIN_STRATUM_SAMPLE})"
        ),
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="report format (default: text)",
    )
    parser.add_argument(
        "--output",
        default=None,
        metavar="PATH",
        help="write the report here instead of stdout",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    live_hosted = (
        args.chooser == "jev"
        and args.jev_endpoint
        and not args.jev_fixture
    )
    if live_hosted and not args.live:
        print(
            "validate-chooser: error: a live Jev run requires --live; this "
            "harness never opens a socket unless invoked explicitly",
            file=sys.stderr,
        )
        return 1

    if not 0.0 < args.target_accuracy <= 1.0:
        print(
            f"validate-chooser: error: --target-accuracy must be in (0, 1], "
            f"got {args.target_accuracy}",
            file=sys.stderr,
        )
        return 1
    if args.min_stratum_sample < 1:
        print(
            f"validate-chooser: error: --min-stratum-sample must be at least 1, "
            f"got {args.min_stratum_sample}",
            file=sys.stderr,
        )
        return 1

    try:
        chooser = build_chooser(
            args.chooser,
            args.fixture,
            jev_endpoint=args.jev_endpoint,
            jev_model=args.jev_model,
            jev_api_key=args.jev_api_key,
            jev_fixture=args.jev_fixture,
        )
        report = run_validation(
            validation_path=args.validation,
            shortlists_path=args.shortlists,
            chooser=chooser,
            bins=args.bins,
            target_accuracy=args.target_accuracy,
            min_stratum_sample=args.min_stratum_sample,
        )
    except (ValidationError, ChoiceError) as exc:
        print(f"validate-chooser: error: {exc}", file=sys.stderr)
        return 1

    text = (
        report_to_json(report) if args.format == "json" else render_report(report)
    )
    if args.output:
        _write_text_atomically(text, Path(args.output))
    else:
        sys.stdout.write(text)
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
