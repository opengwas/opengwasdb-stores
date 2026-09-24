#!/usr/bin/env python3
"""Run a full Canonical Trait Mapping Table curation round (issue #170).

This module wires the four curation stages (issues #163-#169) into one
end-to-end round over a Release Manifest and emits the coverage report that
says what the round achieved. It is the "first ukb-b curation round" driver:
the default Manifest is the committed ukb-b Dense observed VCF release, whose
~2,500 free-text field labels are the whole reason the curation pipeline
exists.

The pipeline
------------
::

    analyses.tsv (ukb-b by default)
      -> curation.gap_scan     (unmapped Trait work queue)
      -> curation.candidates   (multi-channel ontology shortlist)
      -> curation.choice       (proposal; stub or Jev chooser)
      -> curation.promotion    (gate: confidence >= 0.85 and margin >= 0.20)
      -> curation.coverage     (before/after unmapped rate per Store Family)

Only the promotion stage writes the Canonical Trait Mapping Table
(``resources/reference-resources/canonical-trait-mapping-efo/mapping.tsv``) and
bumps its ``resource.yaml`` version; every other stage writes only the
intermediate artifacts under the round's work directory. A proposal that clears
both thresholds is promoted; everything else is routed to the review queue.
A Trait label for which no channel retrieved any candidate is left unmapped by
design -- the pipeline never forces an approximate term onto it -- and the
coverage report counts it.

Strict boundaries
-----------------
A round writes to exactly two kinds of place: the round work directory
(work queue, shortlists, proposals, review queue, coverage report) and the
Reference Resource directory promotion owns. It **never** modifies a Release
Manifest, an accepted Release Bundle, or a built Store Release. ``--dry-run``
copies the Reference Resource into the work directory first, so a rehearsal
cannot touch the real table either.

CLI
---
::

    python3 -m curation.curation_round \\
        --index <ontology-index.json> --chooser stub --fixture <fixture.json> \\
        [--manifests <analyses.tsv> ...] \\
        [--resource-dir <dir>] [--work-dir <dir>] \\
        [--shortlist-size 10] [--confidence-threshold 0.85] \\
        [--margin-threshold 0.20] [--as-of YYYY-MM-DD] \\
        [--format text|markdown|tsv] [--report <path>] [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from curation import candidates as candidates_mod
from curation import choice as choice_mod
from curation import coverage
from curation import gap_scan
from curation import promotion
from curation.chooser import ChoiceError, Chooser
from curation.embedding import (
    DEFAULT_EMBEDDING_MIN_SCORE,
    DEFAULT_EMBEDDING_TOP_K,
    PINNED_EMBEDDING_MODEL_ID,
    EmbeddingChannel,
)
from curation.jev_chooser import DEFAULT_JEV_MODEL
from curation.ontology import IndexFormatError, load_index

REPO_ROOT: Path = Path(__file__).resolve().parents[1]

#: The committed ukb-b Dense observed VCF Release Manifest: the corpus this
#: pipeline was built to curate.
DEFAULT_UKB_B_MANIFEST: Path = (
    REPO_ROOT
    / "families"
    / "ukb-b"
    / "releases"
    / "dense-observed-vcf-c128"
    / "analyses.tsv"
)

DEFAULT_RESOURCE_DIR: Path = promotion.DEFAULT_RESOURCE_DIR

#: Intermediate artifacts live outside the tracked tree, under the gitignored
#: ``.cache/``, so a round never scatters files across the repository.
DEFAULT_WORK_DIR: Path = REPO_ROOT / ".cache" / "curation" / "round"

DEFAULT_SHORTLIST_SIZE: int = candidates_mod.DEFAULT_SHORTLIST_SIZE
DEFAULT_CONFIDENCE_THRESHOLD: float = promotion.DEFAULT_CONFIDENCE_THRESHOLD
DEFAULT_MARGIN_THRESHOLD: float = promotion.DEFAULT_MARGIN_THRESHOLD

_REPORT_EXTENSIONS: dict[str, str] = {
    "text": "txt",
    "markdown": "md",
    "tsv": "tsv",
}


class CurationRoundError(ValueError):
    """Base error for a round that cannot be run."""


@dataclass(frozen=True)
class CurationRoundResult:
    """Everything a completed round produced, for inspection and reporting."""

    work_queue_path: Path
    shortlists_path: Path
    proposals_path: Path
    review_queue_path: Path
    report_path: Path | None
    coverage: coverage.CoverageReport
    promotion: promotion.PromotionOutcome
    report_text: str


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


def run_curation_round(
    *,
    manifests: Sequence[Path | str] = (DEFAULT_UKB_B_MANIFEST,),
    index_path: Path | str,
    chooser: Chooser,
    resource_dir: Path | str = DEFAULT_RESOURCE_DIR,
    work_dir: Path | str = DEFAULT_WORK_DIR,
    shortlist_size: int = DEFAULT_SHORTLIST_SIZE,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    margin_threshold: float = DEFAULT_MARGIN_THRESHOLD,
    as_of: str | None = None,
    rejections_path: Path | str | None = None,
    reviewed_queue_path: Path | str | None = None,
    embedding: EmbeddingChannel | None = None,
    report_format: str = coverage.DEFAULT_FORMAT,
    report_path: Path | str | None = None,
) -> CurationRoundResult:
    """Run gap scan -> candidates -> choice -> promotion -> coverage.

    Intermediate artifacts are written under ``work_dir``. The Canonical Trait
    Mapping Table and its ``resource.yaml`` are written by promotion inside
    ``resource_dir``; pass a copy to rehearse without touching the real table
    (the CLI's ``--dry-run`` does this). No Release Manifest, bundle, or store
    is ever written.
    """
    if shortlist_size < 1:
        raise CurationRoundError(
            f"shortlist_size must be at least 1, got {shortlist_size}"
        )

    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    work_queue_path = work / "work-queue.tsv"
    shortlists_path = work / "shortlists.tsv"
    proposals_path = work / "proposals.tsv"
    review_queue_path = work / "review-queue.tsv"

    # 1. Gap scan: the unmapped Trait work queue.
    entries = gap_scan.scan_manifests(manifests)
    _write_text_atomically(gap_scan.format_queue_tsv(entries), work_queue_path)
    labels = [entry.trait_label for entry in entries]

    # 2. Candidate generation: a lexical (optionally semantic) shortlist.
    index = load_index(index_path)
    candidate_rows = candidates_mod.generate_shortlists(
        labels, index, shortlist_size, embedding
    )
    _write_text_atomically(
        candidates_mod.format_shortlist_tsv(candidate_rows), shortlists_path
    )

    # 3. Choice: one proposal per label with a non-empty shortlist. A label
    #    with no candidate contributes no row and is never forced to a term.
    grouped = choice_mod.read_shortlists(shortlists_path)
    proposals = choice_mod.build_proposals(grouped, chooser)
    _write_text_atomically(
        choice_mod.format_proposals_tsv(proposals), proposals_path
    )

    # 4. Promotion: gate on confidence and margin, promote the confident rows,
    #    queue the rest, and bump the Reference Resource version.
    outcome = promotion.run_promotion(
        proposals_path=proposals_path,
        review_queue_path=review_queue_path,
        resource_dir=resource_dir,
        shortlists_path=shortlists_path,
        rejections_path=rejections_path,
        reviewed_queue_path=reviewed_queue_path,
        confidence_threshold=confidence_threshold,
        margin_threshold=margin_threshold,
        as_of=as_of,
    )

    # 5. Coverage: before/after unmapped rate per Store Family, in Analyses.
    family_stats = coverage.scan_coverage_manifests(manifests)
    promoted_labels = {row.trait_label for row in outcome.plan.promoted}
    no_candidate_count = coverage.count_no_candidate_labels(labels, grouped.keys())
    cost_usd, cost_tracked = coverage.chooser_cost(chooser)
    report = coverage.compute_coverage(
        family_stats,
        promoted_labels=promoted_labels,
        review_queue_size=len(outcome.plan.queued),
        no_candidate_count=no_candidate_count,
        cost_usd=cost_usd,
        cost_tracked=cost_tracked,
    )
    report_text = coverage.render_report(report, report_format)

    resolved_report_path: Path | None = None
    if report_path is not None:
        resolved_report_path = Path(report_path)
        _write_text_atomically(report_text, resolved_report_path)

    return CurationRoundResult(
        work_queue_path=work_queue_path,
        shortlists_path=shortlists_path,
        proposals_path=proposals_path,
        review_queue_path=review_queue_path,
        report_path=resolved_report_path,
        coverage=report,
        promotion=outcome,
        report_text=report_text,
    )


def _stage_resource_copy(resource_dir: Path, work_dir: Path) -> Path:
    """Copy a Reference Resource into the work directory for a dry run."""
    destination = work_dir / "dry-run-resource"
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(resource_dir, destination)
    return destination


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="curation-round",
        description=(
            "Run a full Canonical Trait Mapping Table curation round over a "
            "Release Manifest and emit the coverage report."
        ),
    )
    parser.add_argument(
        "--manifests",
        nargs="+",
        default=[str(DEFAULT_UKB_B_MANIFEST)],
        metavar="MANIFEST",
        help=(
            "analyses.tsv or bundle directory to curate "
            f"(default: {DEFAULT_UKB_B_MANIFEST})"
        ),
    )
    parser.add_argument(
        "--index",
        required=True,
        metavar="JSON",
        help="retrieval index built from the pinned ontology release",
    )
    parser.add_argument(
        "--chooser",
        default="stub",
        metavar="NAME",
        help="chooser to run: stub (default) or jev",
    )
    parser.add_argument(
        "--fixture",
        default=None,
        metavar="PATH",
        help="recorded choices for the stub chooser (JSON or TSV)",
    )
    parser.add_argument(
        "--jev-endpoint",
        default=os.environ.get("OPENGWASDB_JEV_ENDPOINT"),
        metavar="URL",
        help="hosted Jev decision endpoint (enables --chooser jev)",
    )
    parser.add_argument(
        "--jev-model",
        default=os.environ.get("OPENGWASDB_JEV_MODEL", DEFAULT_JEV_MODEL),
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
        "--jev-fixture",
        default=None,
        metavar="PATH",
        help="recorded Jev decisions for hermetic offline runs (JSON or TSV)",
    )
    parser.add_argument(
        "--resource-dir",
        default=str(DEFAULT_RESOURCE_DIR),
        metavar="DIR",
        help=(
            "Reference Resource directory promotion writes "
            f"(default: {DEFAULT_RESOURCE_DIR})"
        ),
    )
    parser.add_argument(
        "--work-dir",
        default=str(DEFAULT_WORK_DIR),
        metavar="DIR",
        help=f"directory for intermediate round artifacts (default: {DEFAULT_WORK_DIR})",
    )
    parser.add_argument(
        "--shortlist-size",
        type=int,
        default=DEFAULT_SHORTLIST_SIZE,
        metavar="N",
        help=f"maximum candidates per label (default: {DEFAULT_SHORTLIST_SIZE})",
    )
    parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=DEFAULT_CONFIDENCE_THRESHOLD,
        metavar="FLOAT",
        help=(
            "minimum confidence to auto-accept "
            f"(default: {DEFAULT_CONFIDENCE_THRESHOLD})"
        ),
    )
    parser.add_argument(
        "--margin-threshold",
        type=float,
        default=DEFAULT_MARGIN_THRESHOLD,
        metavar="FLOAT",
        help=(
            "minimum runner-up margin to auto-accept "
            f"(default: {DEFAULT_MARGIN_THRESHOLD})"
        ),
    )
    parser.add_argument(
        "--as-of",
        default=None,
        metavar="YYYY-MM-DD",
        help="ISO date to stamp on promoted rows (default: today)",
    )
    parser.add_argument(
        "--rejections",
        default=None,
        metavar="TSV",
        help="rejection registry of pairs never to re-propose",
    )
    parser.add_argument(
        "--reviewed-queue",
        default=None,
        metavar="TSV",
        help="reviewed queue whose curator decisions to apply",
    )
    parser.add_argument(
        "--enable-embedding",
        action="store_true",
        help="add the semantic embedding channel to candidate generation",
    )
    parser.add_argument(
        "--embedding-index",
        default=None,
        metavar="JSON",
        help="semantic embedding index artifact; supplying it also enables the channel",
    )
    parser.add_argument(
        "--embedding-model",
        default=PINNED_EMBEDDING_MODEL_ID,
        metavar="MODEL",
        help="default embedding model/artifact to resolve",
    )
    parser.add_argument(
        "--embedding-endpoint",
        default=os.environ.get("OPENGWASDB_EMBEDDING_ENDPOINT"),
        metavar="URL",
        help="hosted OpenAI-compatible /embeddings endpoint",
    )
    parser.add_argument(
        "--embedding-api-key",
        default=os.environ.get("OPENGWASDB_EMBEDDING_API_KEY"),
        metavar="KEY",
        help="bearer token for the hosted embedding endpoint",
    )
    parser.add_argument(
        "--embedding-top-k",
        type=int,
        default=DEFAULT_EMBEDDING_TOP_K,
        metavar="N",
        help=f"neighbours the semantic channel returns (default: {DEFAULT_EMBEDDING_TOP_K})",
    )
    parser.add_argument(
        "--embedding-min-score",
        type=float,
        default=DEFAULT_EMBEDDING_MIN_SCORE,
        metavar="SCORE",
        help=(
            "minimum cosine similarity a semantic neighbour must exceed "
            f"(default: {DEFAULT_EMBEDDING_MIN_SCORE})"
        ),
    )
    parser.add_argument(
        "--format",
        choices=sorted(coverage.RENDERERS_MAP),
        default=coverage.DEFAULT_FORMAT,
        help=f"coverage report format (default: {coverage.DEFAULT_FORMAT})",
    )
    parser.add_argument(
        "--report",
        default=None,
        metavar="PATH",
        help="write the coverage report here (default: <work-dir>/coverage-report.<ext>)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "copy the Reference Resource into the work directory and promote "
            "there, so the real Canonical Trait Mapping Table is untouched"
        ),
    )
    return parser


def _resolve_embedding(
    args: argparse.Namespace, ontology_release: str
) -> EmbeddingChannel | None:
    """Resolve the semantic channel from parsed args, or ``None``.

    Reuses :func:`curation.candidates.resolve_embedding`, which degrades to
    lexical-only with a warning when the channel is unavailable.
    """
    return candidates_mod.resolve_embedding(args, ontology_release)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.shortlist_size < 1:
        print(
            f"curation-round: error: --shortlist-size must be at least 1, "
            f"got {args.shortlist_size}",
            file=sys.stderr,
        )
        return 1
    if not 0.0 <= args.confidence_threshold <= 1.0:
        print(
            f"curation-round: error: --confidence-threshold must be between "
            f"0 and 1, got {args.confidence_threshold}",
            file=sys.stderr,
        )
        return 1
    if args.margin_threshold < 0.0:
        print(
            f"curation-round: error: --margin-threshold must be non-negative, "
            f"got {args.margin_threshold}",
            file=sys.stderr,
        )
        return 1

    work_dir = Path(args.work_dir)
    resource_dir = Path(args.resource_dir)
    if args.dry_run:
        try:
            resource_dir = _stage_resource_copy(resource_dir, work_dir)
        except OSError as exc:
            print(f"curation-round: error: dry-run staging failed: {exc}", file=sys.stderr)
            return 1

    report_path = args.report
    if report_path is None:
        extension = _REPORT_EXTENSIONS.get(args.format, "txt")
        report_path = work_dir / f"coverage-report.{extension}"

    try:
        index = load_index(args.index)
        embedding = _resolve_embedding(args, index.ontology_release)
        chooser = choice_mod.build_chooser(
            args.chooser,
            args.fixture,
            jev_endpoint=args.jev_endpoint,
            jev_model=args.jev_model,
            jev_api_key=args.jev_api_key,
            jev_fixture=args.jev_fixture,
        )
        result = run_curation_round(
            manifests=args.manifests,
            index_path=args.index,
            chooser=chooser,
            resource_dir=resource_dir,
            work_dir=work_dir,
            shortlist_size=args.shortlist_size,
            confidence_threshold=args.confidence_threshold,
            margin_threshold=args.margin_threshold,
            as_of=args.as_of,
            rejections_path=args.rejections,
            reviewed_queue_path=args.reviewed_queue,
            embedding=embedding,
            report_format=args.format,
            report_path=report_path,
        )
    except (CurationRoundError, coverage.CoverageError, gap_scan.GapScanError,
            promotion.PromotionError, ChoiceError, IndexFormatError) as exc:
        print(f"curation-round: error: {exc}", file=sys.stderr)
        return 1

    print(result.report_text, end="")
    plan = result.promotion.plan
    print(
        f"curation-round: {len(plan.promoted)} promoted, "
        f"{len(plan.queued)} queued for review, "
        f"{result.coverage.no_candidate_count} left unmapped with no candidate; "
        f"resource version {result.promotion.version}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
