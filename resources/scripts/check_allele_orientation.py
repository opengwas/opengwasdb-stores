#!/usr/bin/env python3
"""Allele-orientation check for OGS-00011 sources that failed the EAF orientation gate.

Why this exists
---------------
Ancestry assignment fits each source's ``effect_allele_frequency`` against the
Ancestry Reference Panel. When that correlation is negative
(``gate_reason == "eaf_orientation"``, or ``eaf_orientation == "failed"`` with a
different gate named first), the source's frequency column contradicts the
reference. Two upstream mistakes can produce that:

1. **The frequency column is the other allele's frequency** (a mislabelled
   column, or a ``1 - EAF`` write-out) while the effect allele, the other allele
   and ``beta`` remain self-consistent. The reported effect direction is then
   *correct* and only the frequency needs inverting.
2. **The effect and other alleles are swapped.** A label-based harmonisation of
   ``beta`` to the canonical allele is then sign-flipped relative to the truth;
   swapping the allele pair fixes the effect *and* the derived frequency.

Those two are distinguishable without trusting the source's own labels: compare
the source's canonical-allele-harmonised effect against an *independent* analysis
of the same trait, harmonised the same way.

- positively correlated  -> effects agree on allele direction, so only the
  frequency column is wrong  -> ``flip_frequency``
- negatively correlated  -> the effect direction is inverted -> ``swap_alleles``
- near zero / too few overlapping variants -> ``inconclusive``

What it does
------------
For every OGS-00011 study whose ``eaf_orientation`` is ``failed``:

1. read the study's harmonised GWAS-SSF source and take its top ``--top-n``
   variants by significance (``|z| = |beta / se|``, which is monotone with the
   p-value the file reports);
2. find independent analyses of the same trait among the *included* analyses of
   OGS-00011 (matched by trait ontology id, then by normalised trait label) or
   among the UK Biobank analyses of OGS-00010 (matched by normalised label);
3. harmonise both sides' effects to the canonical allele (``chr:pos:a1:a2``,
   ``a1 < a2``) and correlate;
4. write one YAML per study plus a ``summary.tsv``.

Every candidate comparison is evaluated and recorded; the recommendation is the
majority verdict over the comparisons that resolved a direction, so a single
idiosyncratic reference study cannot decide the outcome alone.

Outputs land in ``stores/OGS-00011/sidecars/allele-check/``.

Run from the repository root::

    pixi run python resources/scripts/check_allele_orientation.py --workers 16

This is a review aid: it writes evidence and a recommended upstream fix. It never
edits a source file and never changes release membership.
"""

from __future__ import annotations

import os

# Cap BLAS/OpenMP threads before numpy (and its transitive importers) are loaded,
# so a worker pool of N processes does not spawn N * cores of threads.
for _var in (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "POLARS_MAX_THREADS",
):
    os.environ.setdefault(_var, "1")

import argparse  # noqa: E402
import collections  # noqa: E402
import csv  # noqa: E402
import heapq  # noqa: E402
import math  # noqa: E402
import re  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from concurrent.futures import ProcessPoolExecutor, as_completed  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Iterable, Mapping, Sequence  # noqa: E402

import numpy as np  # noqa: E402
import yaml  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
STORE_ID = "OGS-00011"
INVENTORY_SIDECARS = REPO_ROOT / "stores" / STORE_ID / "sidecars"
ANCESTRY_TSV = INVENTORY_SIDECARS / "ancestry.tsv"
SOURCE_READINESS_TSV = INVENTORY_SIDECARS / "source_readiness.tsv"
ANALYSES_TSV = REPO_ROOT / "stores" / STORE_ID / "analyses.tsv"
COMPARISON_ANALYSES_TSV = REPO_ROOT / "stores" / "OGS-00010" / "analyses.tsv"
COMPARISON_STORE = Path("/data/opengwasdb/stores/OGS-00010/store.opengwasdb")
DEFAULT_OUT_DIR = INVENTORY_SIDECARS / "allele-check"

#: The per-study top-variant count both sides are compared over.
DEFAULT_TOP_N = 100
#: How many comparison analyses are evaluated per target, best first.
DEFAULT_MAX_COMPARISONS = 3
#: Below this many overlapping variants a correlation is not worth reading; the
#: comparison is recorded but cannot carry a verdict.
MIN_OVERLAP = 20
#: The verdict thresholds, as agreed with the operator.
R_FLIP = 0.2
R_SWAP = -0.2

GATE_COLUMN = "gate_reason"
ORIENTATION_COLUMN = "eaf_orientation"

#: UK Biobank field-label prefixes collapsed before a label is compared, so a
#: field description is matched on the phenotype phrase it actually names.
_UKB_LABEL_PREFIXES: tuple[re.Pattern[str], ...] = (
    re.compile(r"^diagnoses - (?:main|secondary) icd10: [a-z0-9.]+ "),
    re.compile(r"^non-cancer illness code, self-reported: "),
    re.compile(r"^cancer code, self-reported: "),
    re.compile(r"^treatment/medication code: "),
    re.compile(r"^operative procedures - (?:main|secondary) opcs: [a-z0-9.]+ "),
    re.compile(r"^operation code: "),
    re.compile(r"^illnesses of (?:mother|father|siblings): "),
    re.compile(r"^self-reported: "),
    re.compile(r"^medication for [^:]+: "),
)


# ---------------------------------------------------------------------------
# Trait label / ontology normalisation
# ---------------------------------------------------------------------------


def normalise_label(value: str) -> str:
    """A trait label reduced to comparable words.

    Parenthetical qualifiers are dropped (they distinguish models and data
    fields, not phenotypes), punctuation is removed and case is folded.
    """
    text = (value or "").lower()
    text = re.sub(r"\(.*?\)", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


_LABEL_STOPWORDS = frozenset(
    {
        "ukb",
        "data",
        "field",
        "level",
        "levels",
        "and",
        "or",
        "of",
        "the",
        "in",
        "a",
        "code",
        "main",
        "secondary",
        "diagnoses",
        "icd10",
        "opcs",
        "treatment",
        "medication",
        "non",
        "cancer",
        "illness",
        "self",
        "reported",
        "operative",
        "procedures",
        "operation",
        "pct",
        "where",
        "patients",
        "gp",
        "was",
        "registered",
    }
)


def label_tokens(value: str) -> frozenset[str]:
    return frozenset(
        token
        for token in normalise_label(value).split()
        if token not in _LABEL_STOPWORDS and len(token) > 2
    )


def strip_ukb_label_prefix(value: str) -> str:
    """A UK Biobank field label reduced to the phenotype phrase it names."""
    low = (value or "").lower()
    for pattern in _UKB_LABEL_PREFIXES:
        match = pattern.match(low)
        if match:
            return value[match.end() :].strip()
    return value


def normalise_ontology_id(value: str) -> str:
    """One ontology CURIE or URI reduced to ``PREFIX:LOCAL``, or ``""``.

    ``http://www.ebi.ac.uk/efo/EFO_0004337``, ``EFO_0004337`` and
    ``efo:EFO_0004337`` all normalise to ``EFO:0004337``; ``MONDO:0005301`` and
    ``http://purl.obolibrary.org/obo/MONDO_0005301`` both to ``MONDO:0005301``.
    """
    text = (value or "").strip()
    if not text:
        return ""
    text = text.rsplit("/", 1)[-1].strip()
    if not text:
        return ""
    if ":" in text:
        prefix, _, local = text.partition(":")
        return f"{prefix.upper()}:{local}" if prefix and local else ""
    if "_" in text:
        prefix, _, local = text.partition("_")
        if prefix.isalpha() and local:
            return f"{prefix.upper()}:{local}"
    return ""


def ontology_ids(value: str) -> frozenset[str]:
    return frozenset(
        normalised
        for normalised in (normalise_ontology_id(part) for part in (value or "").split(","))
        if normalised
    )


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TargetStudy:
    """One OGS-00011 analysis whose source failed the EAF orientation gate."""

    analysis_id: str
    trait: str
    trait_ontology_id: str
    publication_pmid: str
    source_file: Path
    gate_reason: str
    eaf_orientation: str
    eaf_orientation_r: float | None


@dataclass(frozen=True)
class ComparisonStudy:
    """One candidate comparison analysis, from OGS-00011 or OGS-00010."""

    source_bundle: str
    analysis_id: str
    trait: str
    publication_pmid: str
    ontology_ids: frozenset[str]
    label_tokens: frozenset[str]
    normalised_label: str


def _read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def _float_or_none(value: str) -> float | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


#: The readiness sidecar carries no ontology id, so a target's is read from the
#: membership table once and reused. Filled by :func:`_ontology_for`.
_ONTOLOGY_BY_ANALYSIS: dict[str, str] | None = None


def _ontology_for(analysis_id: str) -> str:
    global _ONTOLOGY_BY_ANALYSIS
    if _ONTOLOGY_BY_ANALYSIS is None:
        _ONTOLOGY_BY_ANALYSIS = {
            row["analysis_id"]: (row.get("trait_ontology_id") or "").strip()
            for row in _read_tsv(ANALYSES_TSV)
        }
    return _ONTOLOGY_BY_ANALYSIS.get(analysis_id, "")


def load_targets(ancestry_tsv: Path, readiness_tsv: Path) -> list[TargetStudy]:
    """Every study whose source failed the EAF orientation gate, sorted by id.

    The set is the union of the studies the gate itself named
    (``gate_reason == "eaf_orientation"``) and those whose recorded orientation
    outcome is ``failed`` under an earlier gate: 162 of the 4,783 resolved
    Analyses. Both are in scope because both record a source whose frequency
    column contradicts the reference.
    """
    readiness = {row["analysis_id"]: row for row in _read_tsv(readiness_tsv)}
    targets: list[TargetStudy] = []
    for row in _read_tsv(ancestry_tsv):
        analysis_id = row["analysis_id"]
        orientation = (row.get(ORIENTATION_COLUMN) or "").strip().lower()
        gate_reason = (row.get(GATE_COLUMN) or "").strip()
        if orientation != "failed" and gate_reason != "eaf_orientation":
            continue
        source = readiness.get(analysis_id)
        if source is None or not source.get("data_file"):
            raise SystemExit(f"{analysis_id}: no source_readiness row with a data_file")
        targets.append(
            TargetStudy(
                analysis_id=analysis_id,
                trait=(source.get("trait") or "").strip(),
                trait_ontology_id=_ontology_for(analysis_id),
                publication_pmid=(source.get("publication_pmid") or "").strip(),
                source_file=Path(source["data_file"]),
                gate_reason=gate_reason,
                eaf_orientation=orientation,
                eaf_orientation_r=_float_or_none(row.get("eaf_orientation_r", "")),
            )
        )
    return sorted(targets, key=lambda study: study.analysis_id)


def load_comparison_pools() -> tuple[list[ComparisonStudy], list[ComparisonStudy]]:
    """``(OGS-00011 included analyses, OGS-00010 UK Biobank analyses)``.

    OGS-00011's pool is the same batch's *included* analyses: they passed the
    orientation gate, so they are the honest internal reference. OGS-00010's pool
    is a different collection entirely, which makes it the stronger independent
    check wherever a trait can be matched at all.
    """
    catalogue: list[ComparisonStudy] = []
    for row in _read_tsv(ANALYSES_TSV):
        if (row.get("exclude_from_build") or "").strip().lower() == "true":
            continue
        label = (row.get("source_label") or "").strip() or (row.get("analysis_label") or "").strip()
        catalogue.append(
            ComparisonStudy(
                source_bundle="OGS-00011",
                analysis_id=row["analysis_id"],
                trait=label,
                publication_pmid=(row.get("publication_pmid") or "").strip(),
                ontology_ids=ontology_ids(row.get("trait_ontology_id", "")),
                label_tokens=label_tokens(label),
                normalised_label=normalise_label(label),
            )
        )
    ukb: list[ComparisonStudy] = []
    for row in _read_tsv(COMPARISON_ANALYSES_TSV):
        label = (row.get("source_label") or "").strip() or (row.get("analysis_label") or "").strip()
        stripped = strip_ukb_label_prefix(label)
        ukb.append(
            ComparisonStudy(
                source_bundle="OGS-00010",
                analysis_id=row["analysis_id"],
                trait=label,
                publication_pmid="",
                ontology_ids=frozenset(),
                label_tokens=label_tokens(stripped),
                normalised_label=normalise_label(stripped),
            )
        )
    return catalogue, ukb


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Match:
    """One comparison analysis chosen for a target, with why it was chosen."""

    study: ComparisonStudy
    match_type: str
    similarity: float
    independent: bool


def _token_similarity(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _is_independent(study: ComparisonStudy, target: TargetStudy) -> bool:
    """Whether a comparison cannot share the target's pipeline mistake.

    A different source collection is independent by construction. Within
    OGS-00011 a different publication is; the same publication may share the
    processing that inverted the frequency, so it is not.
    """
    if study.source_bundle != "OGS-00011":
        return True
    if not study.publication_pmid or not target.publication_pmid:
        return False
    return study.publication_pmid != target.publication_pmid


class Matcher:
    """Choose comparison analyses for a target, preferring independent evidence.

    Order of preference:

    1. an OGS-00011 *included* analysis sharing a trait ontology id;
    2. an OGS-00011 included analysis with the same normalised trait label;
    3. an OGS-00010 analysis whose stripped label matches the target trait.

    Within a tier, an independent match is preferred (a different publication,
    or a different collection), then a closer trait label, then the lowest
    analysis id, so the choice is deterministic.
    """

    def __init__(self, catalogue: Sequence[ComparisonStudy], ukb: Sequence[ComparisonStudy]) -> None:
        self._by_ontology: dict[str, list[ComparisonStudy]] = {}
        self._by_label: dict[str, list[ComparisonStudy]] = {}
        for study in catalogue:
            for ontology_id in study.ontology_ids:
                self._by_ontology.setdefault(ontology_id, []).append(study)
            self._by_label.setdefault(study.normalised_label, []).append(study)
        self._ukb = list(ukb)

    def matches(self, target: TargetStudy, limit: int) -> list[Match]:
        for candidates, match_type in (
            (self._ontology_candidates(target), "ontology"),
            (self._by_label.get(normalise_label(target.trait), []), "label"),
            (self._ukb_label_candidates(target), "ukb_label"),
        ):
            chosen = self._choose(target, candidates, match_type, limit)
            if chosen:
                return chosen
        return []

    def _ontology_candidates(self, target: TargetStudy) -> list[ComparisonStudy]:
        found: list[ComparisonStudy] = []
        seen: set[str] = set()
        for ontology_id in ontology_ids(target.trait_ontology_id):
            for study in self._by_ontology.get(ontology_id, []):
                if study.analysis_id in seen or study.analysis_id == target.analysis_id:
                    continue
                seen.add(study.analysis_id)
                found.append(study)
        return found

    def _ukb_label_candidates(self, target: TargetStudy) -> list[ComparisonStudy]:
        """UK Biobank candidates: the stripped label must *be* the target trait.

        Deliberately strict. UK Biobank labels are long field descriptions, so a
        loose token overlap happily matches unrelated fields (``Ever taken
        cannabis`` to ``Ever taken oral contraceptive pill``). Only an exact
        normalised label, or a UK Biobank label carrying every target token, is
        trusted.
        """
        wanted = label_tokens(target.trait)
        wanted_label = normalise_label(target.trait)
        if not wanted:
            return []
        found: list[ComparisonStudy] = []
        for study in self._ukb:
            if not study.label_tokens:
                continue
            if study.normalised_label == wanted_label or wanted <= study.label_tokens:
                found.append(study)
        return found

    def _choose(
        self, target: TargetStudy, candidates: Sequence[ComparisonStudy], match_type: str, limit: int
    ) -> list[Match]:
        if not candidates or limit <= 0:
            return []
        wanted = label_tokens(target.trait)
        wanted_label = normalise_label(target.trait)

        def similarity(study: ComparisonStudy) -> float:
            if study.normalised_label == wanted_label:
                return 1.0
            return _token_similarity(wanted, study.label_tokens)

        def rank(study: ComparisonStudy) -> tuple[int, float, str]:
            # Independent first, then the closest label, then a stable id.
            return (0 if _is_independent(study, target) else 1, -similarity(study), study.analysis_id)

        chosen: list[Match] = []
        seen: set[tuple[str, str]] = set()
        for study in sorted(candidates, key=rank):
            key = (study.source_bundle, study.analysis_id)
            if key in seen:
                continue
            seen.add(key)
            chosen.append(
                Match(
                    study=study,
                    match_type=match_type,
                    similarity=round(similarity(study), 4),
                    independent=_is_independent(study, target),
                )
            )
            if len(chosen) >= limit:
                break
        return chosen


# ---------------------------------------------------------------------------
# Source scans (run in worker processes)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TopVariants:
    """A source's most significant variants, effects harmonised to the canonical allele."""

    analysis_id: str
    effects: Mapping[str, float]
    n_rows: int
    ranked_by: str
    error: str | None = None


def _open_reader(path: Path):
    from opengwasdb.readers.gwas_ssf import GwasSsfReader

    return GwasSsfReader(str(path))


def _harmonised(beta: np.ndarray, flipped: np.ndarray) -> np.ndarray:
    return np.where(flipped, -beta, beta)


def scan_top_variants(analysis_id: str, path: str, top_n: int) -> TopVariants:
    """The ``top_n`` most significant variants of one source.

    Significance is ``|z| = |beta / se|``, which is monotone with the p-value a
    harmonised file reports, so this is the file's top variants by p-value
    without a second pass over the file purely to read the ``p_value`` column.
    Effects are harmonised to the canonical allele: ``beta`` when the source's
    effect allele is the canonical ``a1``, ``-beta`` otherwise.
    """
    try:
        reader = _open_reader(Path(path))
        heap: list[tuple[float, str, float]] = []
        n_rows = 0
        n_beta = 0
        for chunk in reader.stream_metric_chunks():
            n_rows += int(len(chunk))
            beta = chunk.beta
            se = chunk.se
            n_beta += int(np.isfinite(beta).sum())
            usable = np.isfinite(beta) & np.isfinite(se) & (se > 0)
            if not usable.any():
                continue
            score = np.zeros(beta.shape, dtype="float64")
            score[usable] = np.abs(beta[usable] / se[usable])
            score[~usable] = -1.0
            harmonised = _harmonised(beta, chunk.flipped)
            take = min(top_n, score.size)
            kth = score.size - take
            for index in np.argpartition(score, kth)[kth:].tolist():
                if score[index] <= 0.0:
                    continue
                heapq.heappush(heap, (float(score[index]), str(chunk.alid[index]), float(harmonised[index])))
                if len(heap) > top_n:
                    heapq.heappop(heap)
        if heap:
            return TopVariants(analysis_id, {alid: value for _, alid, value in heap}, n_rows, "abs_z")
        if n_beta == 0:
            return TopVariants(analysis_id, {}, n_rows, "abs_z", error="source carries no usable effect")
        # No standard error anywhere (a real shape in this pool): rank by |beta|.
        return _top_by_beta(analysis_id, path, top_n, n_rows)
    except Exception as exc:  # noqa: BLE001 - one bad source must not stop the batch
        return TopVariants(analysis_id, {}, 0, "abs_z", error=f"{type(exc).__name__}: {exc}")


def _top_by_beta(analysis_id: str, path: str, top_n: int, n_rows: int) -> TopVariants:
    reader = _open_reader(Path(path))
    heap: list[tuple[float, str, float]] = []
    for chunk in reader.stream_metric_chunks():
        beta = chunk.beta
        finite = np.isfinite(beta)
        if not finite.any():
            continue
        score = np.where(finite, np.abs(beta), -1.0)
        harmonised = _harmonised(np.where(finite, beta, 0.0), chunk.flipped)
        take = min(top_n, score.size)
        kth = score.size - take
        for index in np.argpartition(score, kth)[kth:].tolist():
            if score[index] <= 0.0:
                continue
            heapq.heappush(heap, (float(score[index]), str(chunk.alid[index]), float(harmonised[index])))
            if len(heap) > top_n:
                heapq.heappop(heap)
    return TopVariants(analysis_id, {alid: value for _, alid, value in heap}, n_rows, "abs_beta")


def scan_matched_effects(path: str, alids: Sequence[str]) -> dict[str, float]:
    """One matched OGS-00011 source's canonical-allele-harmonised effect per alid."""
    wanted = np.asarray(sorted(set(alids)), dtype=object)
    reader = _open_reader(Path(path))
    found: dict[str, float] = {}
    for chunk in reader.stream_metric_chunks():
        mask = np.isin(chunk.alid, wanted)
        if not mask.any():
            continue
        harmonised = _harmonised(chunk.beta, chunk.flipped)
        for index in np.flatnonzero(mask).tolist():
            value = harmonised[index]
            if np.isfinite(value):
                found[str(chunk.alid[index])] = float(value)
    return found


# ---------------------------------------------------------------------------
# Statistics and verdicts
# ---------------------------------------------------------------------------


def correlate(pairs: Sequence[tuple[float, float]]) -> tuple[float | None, float | None]:
    """``(pearson_r, spearman_r)`` over paired effects, or ``None`` where undefined."""
    if len(pairs) < 3:
        return None, None
    x = np.asarray([pair[0] for pair in pairs], dtype="float64")
    y = np.asarray([pair[1] for pair in pairs], dtype="float64")
    if not (np.isfinite(x).all() and np.isfinite(y).all()):
        return None, None
    if float(np.std(x)) == 0.0 or float(np.std(y)) == 0.0:
        return None, None
    pearson = float(np.corrcoef(x, y)[0, 1])
    x_rank = _rank(x)
    y_rank = _rank(y)
    spearman = (
        float(np.corrcoef(x_rank, y_rank)[0, 1])
        if float(np.std(x_rank)) and float(np.std(y_rank))
        else None
    )
    return pearson, spearman


def _rank(values: np.ndarray) -> np.ndarray:
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(values.shape, dtype="float64")
    ranks[order] = np.arange(values.size, dtype="float64")
    return ranks


def verdict_for(pearson: float | None, n_overlap: int) -> str:
    """One comparison's verdict from its canonical-allele effect correlation."""
    if n_overlap < MIN_OVERLAP or pearson is None:
        return "inconclusive"
    if pearson >= R_FLIP:
        return "flip_frequency"
    if pearson <= R_SWAP:
        return "swap_alleles"
    return "inconclusive"


def aggregate(verdicts: Iterable[str]) -> tuple[str, dict[str, int]]:
    """The recommendation from every comparison: a majority of the resolved ones.

    Comparisons that resolved no direction (too few variants, a flat effect
    vector) are not votes. A tie between ``flip_frequency`` and
    ``swap_alleles`` is ``inconclusive`` rather than a coin flip.
    """
    tally: dict[str, int] = {}
    for value in verdicts:
        tally[value] = tally.get(value, 0) + 1
    definitive = {key: tally.get(key, 0) for key in ("flip_frequency", "swap_alleles")}
    top = sorted(definitive.items(), key=lambda item: (-item[1], item[0]))
    if top[0][1] == 0:
        return "inconclusive", tally
    if top[0][1] == top[1][1]:
        return "inconclusive", tally
    return top[0][0], tally


# ---------------------------------------------------------------------------
# Per-target result
# ---------------------------------------------------------------------------


@dataclass
class ComparisonResult:
    """One evaluated comparison: which study, and what its correlation said."""

    match: Match
    source_file: Path | None
    check: dict[str, Any]

    def as_document(self) -> dict[str, Any]:
        study = self.match.study
        document: dict[str, Any] = {
            "source_bundle": study.source_bundle,
            "analysis_id": study.analysis_id,
            "trait": study.trait,
            "publication_pmid": study.publication_pmid,
            "match_type": self.match.match_type,
            "trait_similarity": self.match.similarity,
            "independent": self.match.independent,
        }
        if self.source_file is not None:
            document["source_file"] = str(self.source_file)
        document.update(self.check)
        return document


@dataclass
class StudyResult:
    target: TargetStudy
    comparisons: list[ComparisonResult]
    recommendation: str
    verdict_tally: Mapping[str, int]
    notes: str
    measured_at: str
    error: str | None = None

    @property
    def primary(self) -> ComparisonResult | None:
        return self.comparisons[0] if self.comparisons else None

    def as_document(self) -> dict[str, Any]:
        primary = self.primary
        return {
            "analysis_id": self.target.analysis_id,
            "trait": self.target.trait,
            "trait_ontology_id": self.target.trait_ontology_id,
            "publication_pmid": self.target.publication_pmid,
            "source_file": str(self.target.source_file),
            "orientation_gate": {
                "gate_reason": self.target.gate_reason,
                "eaf_orientation": self.target.eaf_orientation,
                "eaf_orientation_r": self.target.eaf_orientation_r,
            },
            "matched_study": None if primary is None else primary.as_document(),
            "supporting_comparisons": [comparison.as_document() for comparison in self.comparisons[1:]],
            "check": {
                "n_comparisons": len(self.comparisons),
                "verdict_tally": dict(sorted(self.verdict_tally.items())),
                **({} if primary is None else primary.check),
            },
            "recommendation": self.recommendation,
            "notes": self.notes,
            "measured_at": self.measured_at,
            **({} if self.error is None else {"error": self.error}),
        }


# ---------------------------------------------------------------------------
# OGS-00010 lookups
# ---------------------------------------------------------------------------


def _store_effects(identifiers: Sequence[str], analysis_id: str) -> dict[str, float]:
    """Canonical-allele effects from an OGS-00010 analysis, as ``z * se``.

    The store's ``z`` is already aligned to the canonical allele and ``se`` is
    positive, so ``z * se`` is the aligned effect on the source's own scale. A
    single batched lookup is used when every identifier resolved; otherwise each
    identifier is queried alone, so an unresolved one cannot shift the alignment.
    """
    from opengwasdb.query import query_store

    if not identifiers:
        return {}
    query = query_store(str(COMPARISON_STORE))
    result = query.lookup(list(identifiers), [analysis_id])
    z = result["z"]
    se = result["se"]
    if len(z) == len(identifiers):
        return {
            identifier: float(zv * sev)
            for identifier, zv, sev in zip(identifiers, z.tolist(), se.tolist(), strict=True)
            if math.isfinite(zv) and math.isfinite(sev)
        }
    found: dict[str, float] = {}
    for identifier in identifiers:
        single = query.lookup([identifier], [analysis_id])
        if len(single["z"]) == 0:
            continue
        zv = float(single["z"][0])
        sev = float(single["se"][0])
        if math.isfinite(zv) and math.isfinite(sev):
            found[identifier] = zv * sev
    return found


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _format_r(value: float | None) -> str:
    return "" if value is None else f"{value:.6f}"


def _build_notes(
    target: TargetStudy,
    comparisons: Sequence[ComparisonResult],
    recommendation: str,
    tally: Mapping[str, int],
) -> str:
    parts: list[str] = []
    definitive = [c for c in comparisons if c.check.get("verdict") in {"flip_frequency", "swap_alleles"}]
    if not comparisons:
        parts.append("no independent analysis of this trait was found in OGS-00011's included analyses or in OGS-00010")
        return "; ".join(parts)

    if any(not comparison.match.independent for comparison in comparisons):
        parts.append(
            "at least one comparison is from the same publication as the source, so a shared pipeline "
            "mistake would not be detected there; treat that comparison as weaker evidence"
        )
    if len(comparisons) > 1:
        parts.append(
            f"{len(comparisons)} comparison analyses evaluated; verdicts {dict(sorted(tally.items()))}"
        )
    if not definitive:
        overlaps = ", ".join(str(comparison.check.get("n_variants_overlapping", 0)) for comparison in comparisons)
        parts.append(
            f"no comparison resolved a direction (overlapping variants: {overlaps}; "
            f"{MIN_OVERLAP} needed)"
        )
        return "; ".join(parts)

    if recommendation == "flip_frequency":
        parts.append(
            "canonical-allele effects agree with the comparison analysis/analyses "
            f"({_r_summary(definitive)}), so the source's allele labels and beta are self-consistent and "
            "only its effect_allele_frequency column is inverted"
        )
    elif recommendation == "swap_alleles":
        parts.append(
            "canonical-allele effects are inverted against the comparison analysis/analyses "
            f"({_r_summary(definitive)}), so the source's effect and other alleles are swapped relative "
            "to the reported betas"
        )
    else:
        parts.append(
            "comparisons disagree on the direction, so no single fix is justified from this evidence"
        )
    return "; ".join(parts)


def _r_summary(comparisons: Sequence[ComparisonResult]) -> str:
    return ", ".join(
        f"{comparison.match.study.source_bundle}:{comparison.match.study.analysis_id} "
        f"r={comparison.check.get('pearson_r')} over {comparison.check.get('n_variants_overlapping')}"
        for comparison in comparisons
    )


def run(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    measured_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    targets = load_targets(ANCESTRY_TSV, SOURCE_READINESS_TSV)
    if args.limit:
        targets = targets[: args.limit]
    catalogue, ukb = load_comparison_pools()
    matcher = Matcher(catalogue, ukb)
    readiness = {row["analysis_id"]: row for row in _read_tsv(SOURCE_READINESS_TSV)}

    print(
        f"targets: {len(targets)} | OGS-00011 comparison pool: {len(catalogue)} | "
        f"OGS-00010 comparison pool: {len(ukb)}",
        flush=True,
    )

    planned: list[tuple[TargetStudy, list[Match], list[Path | None]]] = []
    match_counts: dict[str, int] = {}
    bytes_by_file: dict[str, int] = {}
    for target in targets:
        chosen = matcher.matches(target, args.max_comparisons)
        files: list[Path | None] = []
        for match in chosen:
            if match.study.source_bundle == "OGS-00011":
                row = readiness.get(match.study.analysis_id)
                if row is None or not row.get("data_file"):
                    raise SystemExit(f"matched OGS-00011 analysis {match.study.analysis_id} has no data_file")
                path = Path(row["data_file"])
                files.append(path)
                bytes_by_file[str(path)] = int(row.get("data_bytes") or 0)
            else:
                files.append(None)
        planned.append((target, chosen, files))
        key = "no_matching_trait" if not chosen else f"{chosen[0].study.source_bundle}:{chosen[0].match_type}"
        match_counts[key] = match_counts.get(key, 0) + 1

    print("match plan:", ", ".join(f"{k}={v}" for k, v in sorted(match_counts.items())), flush=True)
    print(
        f"OGS-00011 comparison sources to scan: {len(bytes_by_file)} "
        f"({sum(bytes_by_file.values()) / 1e9:.1f} GB)",
        flush=True,
    )

    if args.plan_only:
        return 0

    # -- phase 1: top variants of every target ---------------------------------
    print("scanning target sources...", flush=True)
    top_by_target: dict[str, TopVariants] = {}
    started = time.time()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(scan_top_variants, target.analysis_id, str(target.source_file), args.top_n): target
            for target in targets
        }
        for done, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            top_by_target[result.analysis_id] = result
            if done % 20 == 0 or done == len(futures):
                print(f"  {done}/{len(futures)} targets ({time.time() - started:.0f}s)", flush=True)

    # -- phase 2: the matched effects, one scan per distinct OGS-00011 source ---
    wanted_by_file: dict[str, set[str]] = {}
    for target, _, files in planned:
        top = top_by_target.get(target.analysis_id)
        if top is None or not top.effects:
            continue
        for path in files:
            if path is not None:
                wanted_by_file.setdefault(str(path), set()).update(top.effects)

    print(f"scanning {len(wanted_by_file)} distinct OGS-00011 comparison sources...", flush=True)
    matched_effects: dict[str, dict[str, float]] = {}
    if wanted_by_file:
        started = time.time()
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(scan_matched_effects, path, sorted(alids)): path
                for path, alids in wanted_by_file.items()
            }
            for done, future in enumerate(as_completed(futures), start=1):
                matched_effects[futures[future]] = future.result()
                if done % 10 == 0 or done == len(futures):
                    print(f"  {done}/{len(futures)} comparison sources ({time.time() - started:.0f}s)", flush=True)

    # -- phase 3: correlate and write ------------------------------------------
    store_cache: dict[str, dict[str, float]] = {}
    results: list[StudyResult] = []
    for target, matches, files in planned:
        top = top_by_target.get(target.analysis_id)
        if top is None or top.error:
            reason = "target source could not be read" if top is None else f"target source could not be read: {top.error}"
            results.append(
                StudyResult(target, [], "inconclusive", {}, reason, measured_at, error=top.error if top else None)
            )
            continue
        if not matches:
            results.append(
                StudyResult(
                    target,
                    [],
                    "no_matching_trait",
                    {},
                    "no independent analysis of this trait was found in OGS-00011's included analyses or in "
                    "OGS-00010; the orientation failure stays an exclusion",
                    measured_at,
                )
            )
            continue

        comparisons: list[ComparisonResult] = []
        for match, source_file in zip(matches, files, strict=True):
            identifiers = sorted(top.effects)
            if match.study.source_bundle == "OGS-00011":
                other = matched_effects.get(str(source_file), {})
                effect_measure = "beta_vs_beta"
            else:
                cached = store_cache.setdefault(match.study.analysis_id, {})
                missing = [identifier for identifier in identifiers if identifier not in cached]
                if missing:
                    cached.update(_store_effects(missing, match.study.analysis_id))
                other = cached
                effect_measure = "beta_vs_store_z_times_se"
            pairs = [
                (top.effects[alid], other[alid])
                for alid in identifiers
                if alid in other and math.isfinite(other[alid]) and math.isfinite(top.effects[alid])
            ]
            pearson, spearman = correlate(pairs)
            check = {
                "n_variants_checked": len(top.effects),
                "n_variants_overlapping": len(pairs),
                "effect_measure": effect_measure,
                "pearson_r": None if pearson is None else round(pearson, 6),
                "spearman_r": None if spearman is None else round(spearman, 6),
                "verdict": verdict_for(pearson, len(pairs)),
            }
            comparisons.append(ComparisonResult(match, source_file, check))

        recommendation, tally = aggregate(comparison.check["verdict"] for comparison in comparisons)
        results.append(
            StudyResult(
                target,
                comparisons,
                recommendation,
                tally,
                _build_notes(target, comparisons, recommendation, tally),
                measured_at,
            )
        )

    # -- write -----------------------------------------------------------------
    for result in results:
        document = result.as_document()
        (out_dir / f"{result.target.analysis_id}.yaml").write_text(
            yaml.safe_dump(document, sort_keys=False, default_flow_style=False, width=110),
            encoding="utf-8",
        )

    summary_path = out_dir / "summary.tsv"
    with summary_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, delimiter="\t")
        writer.writerow(
            [
                "analysis_id",
                "trait",
                "publication_pmid",
                "gate_reason",
                "eaf_orientation_r",
                "n_comparisons",
                "matched_source_bundle",
                "matched_analysis_id",
                "matched_trait",
                "match_type",
                "independent",
                "n_variants_checked",
                "n_variants_overlapping",
                "pearson_r",
                "spearman_r",
                "recommendation",
            ]
        )
        for result in results:
            primary = result.primary
            match = None if primary is None else primary.match
            check = {} if primary is None else primary.check
            writer.writerow(
                [
                    result.target.analysis_id,
                    result.target.trait,
                    result.target.publication_pmid,
                    result.target.gate_reason,
                    _format_r(result.target.eaf_orientation_r),
                    len(result.comparisons),
                    "" if match is None else match.study.source_bundle,
                    "" if match is None else match.study.analysis_id,
                    "" if match is None else match.study.trait,
                    "" if match is None else match.match_type,
                    "" if match is None else ("true" if match.independent else "false"),
                    check.get("n_variants_checked", ""),
                    check.get("n_variants_overlapping", ""),
                    _format_r(check.get("pearson_r")),
                    _format_r(check.get("spearman_r")),
                    result.recommendation,
                ]
            )

    tally: dict[str, int] = collections.Counter(result.recommendation for result in results)
    print("recommendations:", ", ".join(f"{k}={v}" for k, v in sorted(tally.items())), flush=True)
    print(f"wrote {len(results)} YAML files and {summary_path}", flush=True)
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--workers", type=int, default=min(16, os.cpu_count() or 4), help="parallel source scans")
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N, help="variants compared per study")
    parser.add_argument(
        "--max-comparisons",
        type=int,
        default=DEFAULT_MAX_COMPARISONS,
        help="comparison analyses evaluated per target, best first",
    )
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="where the YAML files are written")
    parser.add_argument("--limit", type=int, help="process only the first N targets (for a smoke run)")
    parser.add_argument("--plan-only", action="store_true", help="report the match plan and exit")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.workers < 1:
        raise SystemExit("--workers must be >= 1")
    if args.top_n < 1:
        raise SystemExit("--top-n must be >= 1")
    if args.max_comparisons < 1:
        raise SystemExit("--max-comparisons must be >= 1")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
