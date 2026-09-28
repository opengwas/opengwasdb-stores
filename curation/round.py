#!/usr/bin/env python3
"""Resumable, stage-per-command Canonical Trait Mapping curation round.

The monolithic round runner (:mod:`curation.curation_round`) works on a small
fixture but is fragile over the real ukb-b queue (2,502 labels): a single
failure or interruption loses all completed work, and a rerun cannot tell a
stale shortlist from a fresh one. This module replaces it with idempotent
stages, each of which reads the pins recorded by ``round-init`` and refuses to
run when an artifact contradicts them.

The round directory (default ``.cache/curation/rounds/<round-id>/``) holds::

    round.yaml              every pin: ontology release/index, embedding stores
                            and their build ids, chooser, thresholds, inputs
    mapping-before.tsv      the mapping table as it stood at round-init
    queue.tsv               gap-scan's work queue (already-mapped labels removed)
    shortlists.tsv          candidates for the queue
    choices/<2hex>/<sha>.yaml        one chooser result per trait label
    choices/<2hex>/<sha>.error.yaml  a failed request, replaced on a later success
    proposals.tsv           the reduced, lane-A proposals table
    cost-ledger.tsv         per-label tokens/cost/model_version
    reconciliation.tsv      bucket counts and totals (buckets sum to the queue)
    review-queue.tsv        sub-threshold proposals for a curator
    no-suitable-term.tsv    confident abstentions, unmapped by design
    coverage-report.<ext>   per-family before/after coverage

Stages
------
``round-init``
    Pin the run. Writes ``round.yaml`` and snapshots the mapping table.
``gap-scan``
    Exclude labels already in the mapping table and write ``queue.tsv``.
``candidates``
    Shortlist the queue against lane B's precomputed vectors.
``choose``
    Map: one atomically-written result file per trait label, concurrent and
    cost-capped, skipping work already done for the same request fingerprint.
``reduce``
    Fold the result files into ``proposals.tsv`` and reconcile every queued
    label into exactly one bucket.
``promote``
    Write confident proposals to the Canonical Trait Mapping Table; abstentions
    and uncertain proposals never reach it.
``coverage``
    Report the after state from the whole post-promotion table.

``curation-round`` runs the stages in order and stops after ``reduce`` when the
round is incomplete.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import math
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from curation import candidates as candidates_mod
from curation import choice as choice_mod
from curation import coverage
from curation import gap_scan
from curation import promotion
from curation.chooser import NONE_SUITABLE, Candidate, ChoiceResult, Chooser
from curation.embedding import (
    DEFAULT_EMBEDDING_BATCH_SIZE,
    DEFAULT_EMBEDDING_CHUNK_SIZE,
    DEFAULT_EMBEDDING_MAX_RETRIES,
    Embedder,
    EmbeddingChannel,
    EmbeddingError,
    EmbeddingStore,
    EmbeddingStoreError,
    PINNED_EMBEDDING_MODEL_ID,
    build_trait_embedding_store,
    embedder_for_model,
    read_embedding_store_meta,
    resolve_retriever,
)
from curation.jev_chooser import DEFAULT_JEV_CONTEXT, DEFAULT_JEV_MODEL
from curation.ontology import IndexFormatError, OntologyIndex, load_index

REPO_ROOT: Path = Path(__file__).resolve().parents[1]

#: Default root under which round directories live. Gitignored, like every
#: other intermediate curation artifact.
DEFAULT_ROUNDS_ROOT: Path = REPO_ROOT / ".cache" / "curation" / "rounds"

ROUND_YAML_FILENAME: str = "round.yaml"
MAPPING_BEFORE_FILENAME: str = "mapping-before.tsv"
QUEUE_FILENAME: str = "queue.tsv"
SHORTLISTS_FILENAME: str = "shortlists.tsv"
TRAIT_EMBEDDINGS_DIRNAME: str = "trait-embeddings"
CHOICES_DIRNAME: str = "choices"
PROPOSALS_FILENAME: str = "proposals.tsv"
COST_LEDGER_FILENAME: str = "cost-ledger.tsv"
RECONCILIATION_FILENAME: str = "reconciliation.tsv"
REVIEW_QUEUE_FILENAME: str = "review-queue.tsv"
NO_SUITABLE_FILENAME: str = "no-suitable-term.tsv"

# Buckets every queued label falls into exactly once.
BUCKET_NO_CANDIDATE: str = "no_candidate"
BUCKET_PENDING: str = "pending"
BUCKET_ERROR: str = "error"
BUCKET_NONE_SUITABLE: str = "none_suitable"
BUCKET_PROPOSED: str = "proposed"
BUCKETS: tuple[str, ...] = (
    BUCKET_NO_CANDIDATE,
    BUCKET_PENDING,
    BUCKET_ERROR,
    BUCKET_NONE_SUITABLE,
    BUCKET_PROPOSED,
)

COST_LEDGER_COLUMNS: tuple[str, ...] = (
    "trait_label",
    "input_tokens",
    "cost_usd",
    "model_version",
)
RECONCILIATION_COLUMNS: tuple[str, ...] = ("metric", "value")

#: The fingerprint recipe version, so a change to what a request covers can
#: invalidate every stored result deliberately.
FINGERPRINT_VERSION: str = "choice-v1"

DEFAULT_WORKERS: int = 8

_TSV_UNSAFE_RE = re.compile(r"[\t\r\n]+")
_REPORT_EXTENSIONS: dict[str, str] = {"text": "txt", "markdown": "md", "tsv": "tsv"}

_API_KEY_ENV_VARS: tuple[str, ...] = (
    "OPENGWASDB_JEV_API_KEY",
    "TYPESAFE_API_KEY",
    "OPENGWASDB_EMBEDDING_API_KEY",
    "OPENGWASDB_EMBEDDING_API_KEY",
)


class RoundError(ValueError):
    """Base error for a curation round that cannot be served."""


class RoundConfigError(RoundError):
    """Raised when ``round.yaml`` is missing or malformed."""


class RoundPinError(RoundError):
    """Raised when an artifact contradicts a pin recorded by ``round-init``."""


class RoundStateError(RoundError):
    """Raised when a stage cannot run in the round's current state."""


# ---------------------------------------------------------------------------
# round.yaml pins
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmbeddingPin:
    """A pinned embedding store: where it is and what exact build it is."""

    directory: Path
    model_id: str
    ontology_release: str
    build_id: str
    dimension: int
    count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "directory": str(self.directory),
            "model_id": self.model_id,
            "ontology_release": self.ontology_release,
            "build_id": self.build_id,
            "dimension": self.dimension,
            "count": self.count,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, key: str) -> "EmbeddingPin":
        if not isinstance(data, Mapping):
            raise RoundConfigError(f"{key} must be a mapping")
        directory = data.get("directory")
        model_id = data.get("model_id")
        release = data.get("ontology_release")
        build_id = data.get("build_id")
        for field_name, value in (
            ("directory", directory),
            ("model_id", model_id),
            ("ontology_release", release),
            ("build_id", build_id),
        ):
            if not isinstance(value, str) or not value:
                raise RoundConfigError(f"{key}.{field_name} is missing")
        return cls(
            directory=_resolve_path(directory),
            model_id=model_id,
            ontology_release=release,
            build_id=build_id,
            dimension=int(data.get("dimension") or 0),
            count=int(data.get("count") or 0),
        )


@dataclass(frozen=True)
class ChooserPin:
    """The chooser identity a round uses, so a result can be attributed."""

    chooser_id: str
    version: str
    model: str
    context: str
    fixture: Path | None = None

    def resolved_version(self) -> str:
        """The version a result is attributed to.

        A Jev chooser's version *is* its model id, so an empty pinned version
        falls back to the pinned model. This is the single source ``choose``
        and ``reduce`` both use, so their fingerprints cannot drift apart.
        """
        if self.version:
            return self.version
        if self.chooser_id == "jev":
            return self.model or DEFAULT_JEV_MODEL
        return self.version

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.chooser_id,
            "version": self.version,
            "model": self.model,
            "context": self.context,
            "fixture": str(self.fixture) if self.fixture else None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ChooserPin":
        if not isinstance(data, Mapping):
            raise RoundConfigError("chooser must be a mapping")
        chooser_id = data.get("id")
        if not isinstance(chooser_id, str) or not chooser_id:
            raise RoundConfigError("chooser.id is missing")
        fixture = data.get("fixture")
        return cls(
            chooser_id=chooser_id,
            version=str(data.get("version", "")),
            model=str(data.get("model", "")),
            context=str(data.get("context", "")),
            fixture=_resolve_path(fixture) if isinstance(fixture, str) and fixture else None,
        )


@dataclass(frozen=True)
class RoundConfig:
    """The pinned description of one curation round, read from ``round.yaml``."""

    round_id: str
    created_at: str
    round_dir: Path
    ontology_release: str
    index_path: Path
    ontology_embeddings: EmbeddingPin | None
    trait_embeddings: EmbeddingPin | None
    chooser: ChooserPin
    shortlist_size: int
    confidence_threshold: float
    margin_threshold: float
    threshold_evidence: str
    manifests: tuple[Path, ...]
    queue_tsv: Path | None
    resource_dir: Path

    @property
    def queue_path(self) -> Path:
        return self.round_dir / QUEUE_FILENAME

    @property
    def shortlists_path(self) -> Path:
        return self.round_dir / SHORTLISTS_FILENAME

    @property
    def choices_dir(self) -> Path:
        return self.round_dir / CHOICES_DIRNAME

    @property
    def proposals_path(self) -> Path:
        return self.round_dir / PROPOSALS_FILENAME

    @property
    def cost_ledger_path(self) -> Path:
        return self.round_dir / COST_LEDGER_FILENAME

    @property
    def reconciliation_path(self) -> Path:
        return self.round_dir / RECONCILIATION_FILENAME

    @property
    def review_queue_path(self) -> Path:
        return self.round_dir / REVIEW_QUEUE_FILENAME

    @property
    def no_suitable_path(self) -> Path:
        return self.round_dir / NO_SUITABLE_FILENAME

    @property
    def mapping_before_path(self) -> Path:
        return self.round_dir / MAPPING_BEFORE_FILENAME

    @property
    def mapping_path(self) -> Path:
        return self.resource_dir / promotion.MAPPING_FILENAME

    def to_dict(self) -> dict[str, object]:
        return {
            "round_id": self.round_id,
            "created_at": self.created_at,
            "ontology": {
                "release": self.ontology_release,
                "index_path": str(self.index_path),
                "embeddings": (
                    self.ontology_embeddings.to_dict()
                    if self.ontology_embeddings
                    else None
                ),
            },
            "trait_embeddings": (
                self.trait_embeddings.to_dict() if self.trait_embeddings else None
            ),
            "chooser": self.chooser.to_dict(),
            "shortlist_size": self.shortlist_size,
            "thresholds": {
                "confidence": self.confidence_threshold,
                "margin": self.margin_threshold,
                "evidence": self.threshold_evidence,
            },
            "inputs": {
                "manifests": [str(path) for path in self.manifests],
                "queue_tsv": str(self.queue_tsv) if self.queue_tsv else None,
            },
            "resource_dir": str(self.resource_dir),
        }

    def core_signature(self) -> dict[str, object]:
        """Every pin except the timestamp and the round's own directory."""
        data = self.to_dict()
        data.pop("created_at", None)
        data.pop("round_id", None)
        return data


def _resolve_path(value: str | Path) -> Path:
    """Resolve a recorded path against the repository root when relative."""
    path = Path(value)
    if path.is_absolute():
        return path
    return (REPO_ROOT / path).resolve()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_round_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def round_dir_for(round_id: str, rounds_root: Path | str = DEFAULT_ROUNDS_ROOT) -> Path:
    """The default round directory for an id."""
    return Path(rounds_root) / round_id


def _yaml_dump(data: Any) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def _atomic_write_text(text: str, dest_path: Path) -> None:
    """Write text via a temp file + fsync + rename, cleaning up on failure."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = dest_path.with_name(
        f".{dest_path.name}.tmp.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}"
    )
    try:
        with open(temp_path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, dest_path)
    except BaseException:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def _atomic_write_yaml(data: Any, dest_path: Path) -> None:
    _atomic_write_text(_yaml_dump(data), dest_path)


def read_round_config(round_dir: Path | str) -> RoundConfig:
    """Read and validate a round's ``round.yaml``."""
    directory = Path(round_dir)
    config_path = directory / ROUND_YAML_FILENAME
    if not config_path.is_file():
        raise RoundConfigError(
            f"round config does not exist: {config_path} (run round-init first)"
        )
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RoundConfigError(f"{config_path} is not valid YAML: {exc}") from exc
    if not isinstance(data, Mapping):
        raise RoundConfigError(f"{config_path} is not a mapping")

    ontology = data.get("ontology")
    if not isinstance(ontology, Mapping):
        raise RoundConfigError(f"{config_path} has no ontology mapping")
    release = ontology.get("release")
    index_path = ontology.get("index_path")
    if not isinstance(release, str) or not release:
        raise RoundConfigError(f"{config_path} has no ontology.release")
    if not isinstance(index_path, str) or not index_path:
        raise RoundConfigError(f"{config_path} has no ontology.index_path")

    embeddings_data = ontology.get("embeddings")
    ontology_embeddings = (
        EmbeddingPin.from_dict(embeddings_data, key="ontology.embeddings")
        if embeddings_data
        else None
    )
    trait_data = data.get("trait_embeddings")
    trait_embeddings = (
        EmbeddingPin.from_dict(trait_data, key="trait_embeddings")
        if trait_data
        else None
    )

    chooser = ChooserPin.from_dict(data.get("chooser") or {})

    thresholds = data.get("thresholds")
    if not isinstance(thresholds, Mapping):
        raise RoundConfigError(f"{config_path} has no thresholds mapping")

    inputs = data.get("inputs")
    if not isinstance(inputs, Mapping):
        raise RoundConfigError(f"{config_path} has no inputs mapping")
    manifests = tuple(
        _resolve_path(str(path)) for path in (inputs.get("manifests") or [])
    )
    queue_value = inputs.get("queue_tsv")
    queue_tsv = _resolve_path(queue_value) if queue_value else None

    resource_dir = data.get("resource_dir")
    if not isinstance(resource_dir, str) or not resource_dir:
        raise RoundConfigError(f"{config_path} has no resource_dir")

    return RoundConfig(
        round_id=str(data.get("round_id") or directory.name),
        created_at=str(data.get("created_at", "")),
        round_dir=directory,
        ontology_release=release,
        index_path=_resolve_path(index_path),
        ontology_embeddings=ontology_embeddings,
        trait_embeddings=trait_embeddings,
        chooser=chooser,
        shortlist_size=int(data.get("shortlist_size") or 0),
        confidence_threshold=float(thresholds.get("confidence") or 0.0),
        margin_threshold=float(thresholds.get("margin") or 0.0),
        threshold_evidence=str(thresholds.get("evidence", "")),
        manifests=manifests,
        queue_tsv=queue_tsv,
        resource_dir=_resolve_path(resource_dir),
    )


def _embedding_pin(directory: Path) -> EmbeddingPin:
    meta = read_embedding_store_meta(directory)
    return EmbeddingPin(
        directory=directory,
        model_id=meta.model_id,
        ontology_release=meta.ontology_release,
        build_id=meta.build_id,
        dimension=meta.dimension,
        count=meta.count,
    )


def init_round(
    round_dir: Path | str,
    *,
    index_path: Path | str,
    ontology_embeddings: Path | str | None = None,
    trait_embeddings: Path | str | None = None,
    embedding_model: str | None = None,
    chooser_id: str = "stub",
    chooser_version: str = "1",
    chooser_model: str = "",
    chooser_context: str = DEFAULT_JEV_CONTEXT,
    chooser_fixture: Path | str | None = None,
    manifests: Sequence[Path | str] = (),
    queue_tsv: Path | str | None = None,
    resource_dir: Path | str = promotion.DEFAULT_RESOURCE_DIR,
    shortlist_size: int = candidates_mod.DEFAULT_SHORTLIST_SIZE,
    confidence_threshold: float = promotion.DEFAULT_CONFIDENCE_THRESHOLD,
    margin_threshold: float = promotion.DEFAULT_MARGIN_THRESHOLD,
    threshold_evidence: str = "",
    round_id: str | None = None,
    created_at: str | None = None,
    force: bool = False,
) -> RoundConfig:
    """Pin a curation round and write ``round.yaml``.

    The pins are: the ontology release and index path, the optional
    ontology/trait embedding stores with their model and content-addressed
    build id, the chooser identity and context, the shortlist size, the
    confidence and margin thresholds with their free-text evidence, and the
    manifests or explicit queue TSV the round consumes. The current mapping
    table is snapshotted to ``mapping-before.tsv`` so coverage can compute the
    rows this round adds.

    Re-running with the same pins is a no-op; re-running with different pins
    raises unless ``force`` is set.
    """
    directory = Path(round_dir)
    resolved_index = _resolve_path(index_path)

    if shortlist_size < 1:
        raise RoundConfigError(f"shortlist_size must be at least 1, got {shortlist_size}")
    if not 0.0 <= confidence_threshold <= 1.0:
        raise RoundConfigError(
            f"confidence_threshold must be between 0 and 1, got {confidence_threshold}"
        )
    if margin_threshold < 0.0:
        raise RoundConfigError(
            f"margin_threshold must be non-negative, got {margin_threshold}"
        )
    resolved_manifests = tuple(_resolve_path(path) for path in manifests)
    resolved_queue = _resolve_path(queue_tsv) if queue_tsv else None
    if bool(resolved_manifests) == bool(resolved_queue):
        raise RoundConfigError(
            "supply exactly one of manifests or queue_tsv (a harvested "
            "validation set is an explicit queue)"
        )

    index = load_index(resolved_index)
    if chooser_id == "jev" and not chooser_model:
        chooser_model = DEFAULT_JEV_MODEL
    if chooser_id == "jev" and chooser_version in ("", "1"):
        # Jev's version is its model id; the stub default of "1" would make
        # choose and reduce compute different fingerprints.
        chooser_version = chooser_model
    ontology_pin = _embedding_pin(_resolve_path(ontology_embeddings)) if ontology_embeddings else None
    trait_pin = _embedding_pin(_resolve_path(trait_embeddings)) if trait_embeddings else None
    _validate_embedding_pins(index.ontology_release, ontology_pin, trait_pin)
    if embedding_model and ontology_pin and ontology_pin.model_id != embedding_model:
        raise RoundPinError(
            f"ontology embedding store model {ontology_pin.model_id!r} does not "
            f"match --embedding-model {embedding_model!r}"
        )

    config = RoundConfig(
        round_id=round_id or directory.name or _default_round_id(),
        created_at=created_at or _utc_now(),
        round_dir=directory,
        ontology_release=index.ontology_release,
        index_path=resolved_index,
        ontology_embeddings=ontology_pin,
        trait_embeddings=trait_pin,
        chooser=ChooserPin(
            chooser_id=chooser_id,
            version=chooser_version,
            model=chooser_model,
            context=chooser_context,
            fixture=_resolve_path(chooser_fixture) if chooser_fixture else None,
        ),
        shortlist_size=shortlist_size,
        confidence_threshold=confidence_threshold,
        margin_threshold=margin_threshold,
        threshold_evidence=threshold_evidence,
        manifests=resolved_manifests,
        queue_tsv=resolved_queue,
        resource_dir=_resolve_path(resource_dir),
    )

    config_path = directory / ROUND_YAML_FILENAME
    if config_path.is_file() and not force:
        existing = read_round_config(directory)
        if existing.core_signature() != config.core_signature():
            raise RoundStateError(
                f"{config_path} already exists with different pins; pass "
                "--force to re-initialise the round"
            )
        return existing

    directory.mkdir(parents=True, exist_ok=True)
    _snapshot_mapping_before(config, force=force)
    _atomic_write_yaml(config.to_dict(), config_path)
    return config


def _snapshot_mapping_before(config: RoundConfig, *, force: bool = False) -> None:
    """Record the mapping table as it stood at round-init, once."""
    snapshot = config.mapping_before_path
    if snapshot.exists() and not force:
        return
    source = config.mapping_path
    if source.is_file():
        _atomic_write_text(source.read_text(encoding="utf-8"), snapshot)
    else:
        _atomic_write_text(
            "\t".join(promotion.MAPPING_COLUMNS) + "\n", snapshot
        )


def _validate_embedding_pins(
    ontology_release: str,
    ontology_pin: EmbeddingPin | None,
    trait_pin: EmbeddingPin | None,
) -> None:
    """Refuse a store built for another release, or a mismatched pair."""
    for name, pin in (("ontology", ontology_pin), ("trait", trait_pin)):
        if pin is None:
            continue
        if pin.ontology_release != ontology_release:
            raise RoundPinError(
                f"{name} embedding store was built for ontology release "
                f"{pin.ontology_release!r}, not {ontology_release!r}"
            )
    if ontology_pin and trait_pin:
        if ontology_pin.model_id != trait_pin.model_id:
            raise RoundPinError(
                f"trait embedding store model {trait_pin.model_id!r} does not "
                f"match ontology store model {ontology_pin.model_id!r}"
            )
        if ontology_pin.dimension != trait_pin.dimension:
            raise RoundPinError(
                f"trait embedding store dimension {trait_pin.dimension} does not "
                f"match ontology store dimension {ontology_pin.dimension}"
            )


def _validate_index_pin(config: RoundConfig) -> OntologyIndex:
    """Load the pinned index and refuse a different release."""
    index = load_index(config.index_path)
    if index.ontology_release != config.ontology_release:
        raise RoundPinError(
            f"index {config.index_path} carries release "
            f"{index.ontology_release!r}, but round.yaml pins "
            f"{config.ontology_release!r}"
        )
    return index


def _validate_embedding_artifacts(config: RoundConfig) -> None:
    """Re-read the pinned stores' meta and refuse a changed model or build."""
    for name, pin in (
        ("ontology", config.ontology_embeddings),
        ("trait", config.trait_embeddings),
    ):
        if pin is None:
            continue
        try:
            meta = read_embedding_store_meta(pin.directory)
        except EmbeddingStoreError as exc:
            raise RoundPinError(f"{name} embedding store is unavailable: {exc}") from exc
        if (
            meta.model_id != pin.model_id
            or meta.ontology_release != pin.ontology_release
            or meta.build_id != pin.build_id
        ):
            raise RoundPinError(
                f"{name} embedding store {pin.directory} no longer matches the "
                "pinned model/release/build_id; rebuild it or re-run round-init"
            )


# ---------------------------------------------------------------------------
# gap-scan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GapScanOutcome:
    queue_path: Path
    entries: tuple[gap_scan.QueueEntry, ...]
    already_mapped_excluded: int


def _mapping_labels_from_table(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    return coverage.read_mapping_labels(path)


def _queue_entries_from_tsv(path: Path) -> list[gap_scan.QueueEntry]:
    """Read an explicit queue TSV, preserving counts/families when present."""
    if not path.is_file():
        raise RoundStateError(f"queue TSV does not exist: {path}")
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f, delimiter="\t")
        header = next(reader, None)
        columns = list(header) if header is not None else []
        if "trait_label" not in columns:
            raise RoundStateError(f"{path} has no trait_label column")
        count_column = "occurrence_count" if "occurrence_count" in columns else None
        family_column = "store_families" if "store_families" in columns else None

        counts: dict[str, int] = {}
        families: dict[str, set[str]] = {}
        for row_index, fields in enumerate(reader):
            if len(fields) != len(columns):
                raise RoundStateError(
                    f"{path} data row {row_index} has {len(fields)} fields; "
                    f"header has {len(columns)}"
                )
            row = dict(zip(columns, fields))
            label = gap_scan.normalize_trait_label(row.get("trait_label"))
            if not label:
                continue
            occurrence = 1
            if count_column:
                raw = (row.get(count_column) or "").strip()
                if raw:
                    try:
                        occurrence = int(raw)
                    except ValueError as exc:
                        raise RoundStateError(
                            f"{path} data row {row_index} has a non-integer "
                            f"{count_column}: {raw!r}"
                        ) from exc
            counts[label] = counts.get(label, 0) + occurrence
            if family_column:
                for family in (row.get(family_column) or "").split(","):
                    family = family.strip()
                    if family:
                        families.setdefault(label, set()).add(family)

    entries = [
        gap_scan.QueueEntry(
            trait_label=label,
            occurrence_count=counts[label],
            store_families=tuple(sorted(families.get(label, set()))),
        )
        for label in counts
    ]
    entries.sort(key=lambda entry: (-entry.occurrence_count, entry.trait_label))
    return entries


def run_gap_scan(round_dir: Path | str, *, mapping_path: Path | str | None = None) -> GapScanOutcome:
    """Build the round's work queue, excluding already-mapped labels."""
    config = read_round_config(round_dir)

    if config.queue_tsv is not None:
        candidates_entries = _queue_entries_from_tsv(config.queue_tsv)
    else:
        candidates_entries = gap_scan.scan_manifests(config.manifests)

    live_table = Path(mapping_path) if mapping_path is not None else config.mapping_path
    already_mapped = _mapping_labels_from_table(live_table)

    entries: list[gap_scan.QueueEntry] = []
    excluded = 0
    for entry in candidates_entries:
        if gap_scan.normalize_trait_label(entry.trait_label) in already_mapped:
            excluded += 1
            continue
        entries.append(entry)

    _atomic_write_text(gap_scan.format_queue_tsv(entries), config.queue_path)
    return GapScanOutcome(
        queue_path=config.queue_path,
        entries=tuple(entries),
        already_mapped_excluded=excluded,
    )


# ---------------------------------------------------------------------------
# candidates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidatesOutcome:
    shortlists_path: Path
    labels: int
    candidate_rows: int
    no_candidate_labels: int
    embedding_used: bool


def _resolve_round_embedding(config: RoundConfig) -> EmbeddingChannel | None:
    """Build the pinned semantic channel, or ``None`` for lexical-only.

    The round is offline by construction: it uses the two precomputed stores
    and never an endpoint. Whatever *is* pinned is validated first -- even when
    only one of the pair is present -- and a store that contradicts its pin
    (changed model/release/build id, or a corrupt vector payload) raises
    :class:`RoundPinError`. A wrong or unreadable pinned artifact must never
    silently degrade the round to lexical-only.
    """
    _validate_embedding_artifacts(config)
    if config.ontology_embeddings is None or config.trait_embeddings is None:
        if config.ontology_embeddings is not None or config.trait_embeddings is not None:
            print(
                "candidates: warning: both --ontology-embeddings and "
                "--trait-embeddings are required for the offline semantic "
                "channel; continuing lexical-only",
                file=sys.stderr,
            )
        return None
    try:
        retriever = resolve_retriever(
            config.ontology_embeddings.directory,
            config.ontology_release,
            model_id=config.ontology_embeddings.model_id,
            trait_embeddings=config.trait_embeddings.directory,
        )
    except EmbeddingError as exc:
        # Everything about this path is pinned, so any failure is a pin
        # contradiction (corrupt vectors, or a model/release/dimension
        # disagreement) rather than something to degrade past.
        raise RoundPinError(
            f"pinned embedding stores are unusable: {exc}"
        ) from exc
    return EmbeddingChannel(retriever)


def run_candidates(
    round_dir: Path | str,
    *,
    embedding: EmbeddingChannel | None = None,
) -> CandidatesOutcome:
    """Generate shortlists for the round's queue from its pinned stores."""
    config = read_round_config(round_dir)
    index = _validate_index_pin(config)
    queue_rows = candidates_mod.read_work_queue(config.queue_path)
    labels = [row.get("trait_label", "") for row in queue_rows]

    channel = embedding if embedding is not None else _resolve_round_embedding(config)
    rows = candidates_mod.generate_shortlists(
        labels, index, config.shortlist_size, channel
    )
    _atomic_write_text(
        candidates_mod.format_shortlist_tsv(rows), config.shortlists_path
    )
    shortlisted = {
        gap_scan.normalize_trait_label(row.trait_label) for row in rows
    }
    queue_labels = {gap_scan.normalize_trait_label(label) for label in labels if label}
    return CandidatesOutcome(
        shortlists_path=config.shortlists_path,
        labels=len(queue_labels),
        candidate_rows=len(rows),
        no_candidate_labels=len(queue_labels - shortlisted),
        embedding_used=channel is not None,
    )


# ---------------------------------------------------------------------------
# embed-traits (round step)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmbedTraitsOutcome:
    store_dir: Path
    model_id: str
    build_id: str
    count: int
    config: RoundConfig


def _record_trait_embedding_pin(round_dir: Path | str, pin: EmbeddingPin) -> None:
    """Record the trait store pin in ``round.yaml`` without a full re-init.

    The runbook cannot pin ``--trait-embeddings`` at ``round-init`` because the
    store is built from the round's own queue. This updates only the
    ``trait_embeddings`` key, so ``mapping-before.tsv`` is never re-snapshotted
    and no ``--force`` is needed.
    """
    config_path = Path(round_dir) / ROUND_YAML_FILENAME
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:  # pragma: no cover - read_round_config checks
        raise RoundConfigError(f"{config_path} is not valid YAML: {exc}") from exc
    if not isinstance(data, Mapping):
        raise RoundConfigError(f"{config_path} is not a mapping")
    updated = dict(data)
    updated["trait_embeddings"] = pin.to_dict()
    _atomic_write_yaml(updated, config_path)


def _read_trait_labels_from_queue(path: Path) -> list[str]:
    """The ``trait_label`` column of the round's gap-scan queue."""
    rows = candidates_mod.read_work_queue(path)
    return [str(row.get("trait_label", "")) for row in rows]


def run_embed_traits(
    round_dir: Path | str,
    *,
    embedder: Embedder | None = None,
    endpoint: str | None = None,
    api_key: str | None = None,
    served_model: str | None = None,
    output: Path | str | None = None,
    chunk_size: int = DEFAULT_EMBEDDING_CHUNK_SIZE,
    batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE,
    max_retries: int = DEFAULT_EMBEDDING_MAX_RETRIES,
) -> EmbedTraitsOutcome:
    """Embed the round's queue with the pinned ontology store's model.

    This is the missing link in the runbook: ``round-init`` cannot pin a trait
    store that only the round's own queue can produce. The step writes the
    store under the round directory, records its pin in ``round.yaml``, and
    refuses to build anything that disagrees with the pinned ontology store.
    """
    config = read_round_config(round_dir)
    if config.ontology_embeddings is None:
        raise RoundPinError(
            "round.yaml pins no ontology embedding store; run round-init with "
            "--ontology-embeddings before embedding traits"
        )
    if not config.queue_path.is_file():
        raise RoundStateError(
            f"the round's queue does not exist: {config.queue_path} "
            "(run round-gap-scan first)"
        )

    ontology_pin = config.ontology_embeddings
    labels = _read_trait_labels_from_queue(config.queue_path)
    store_dir = (
        Path(output)
        if output is not None
        else config.round_dir / TRAIT_EMBEDDINGS_DIRNAME
    )
    if embedder is None:
        embedder = embedder_for_model(
            ontology_pin.model_id,
            endpoint=endpoint,
            api_key=api_key,
            batch_size=batch_size,
            max_retries=max_retries,
            served_model=served_model,
        )
    if embedder.model_id != ontology_pin.model_id:
        raise RoundPinError(
            f"trait embedder model {embedder.model_id!r} does not match the "
            f"pinned ontology store model {ontology_pin.model_id!r}"
        )

    store = build_trait_embedding_store(
        labels,
        embedder,
        ontology_release=config.ontology_release,
        chunks_dir=store_dir / "chunks",
        chunk_size=chunk_size,
        on_chunk=_progress_hook("round-embed-traits"),
    )
    if store.model_id != ontology_pin.model_id:
        raise RoundPinError(
            f"trait embedding store model {store.model_id!r} does not match the "
            f"pinned ontology store model {ontology_pin.model_id!r}"
        )
    if store.dimension != ontology_pin.dimension:
        raise RoundPinError(
            f"trait embedding dimension {store.dimension} does not match the "
            f"pinned ontology store dimension {ontology_pin.dimension}"
        )
    store.save(store_dir)
    pin = _embedding_pin(store_dir)
    _record_trait_embedding_pin(round_dir, pin)
    return EmbedTraitsOutcome(
        store_dir=store_dir,
        model_id=store.model_id,
        build_id=store.build_id,
        count=store.count,
        config=read_round_config(round_dir),
    )


def _progress_hook(label: str):
    def report(done: int, total: int) -> None:
        print(f"{label}: chunk {done}/{total}", file=sys.stderr)

    return report


# ---------------------------------------------------------------------------
# choose (map)
# ---------------------------------------------------------------------------


def choice_fingerprint(
    chooser_id: str,
    chooser_version: str,
    model: str,
    context: str,
    trait_label: str,
    candidates: Sequence[Candidate],
) -> str:
    """A deterministic digest of everything a chooser request depends on.

    Covers the chooser identity, its model and context, the trait label, and the
    ordered shortlist with the candidate evidence a chooser sees. A changed
    shortlist, model, or context yields a different fingerprint, so a stored
    result from another request is never mistaken for this one's.
    """
    digest = hashlib.sha256()
    for part in (FINGERPRINT_VERSION, chooser_id, chooser_version, model, context):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    digest.update(trait_label.encode("utf-8"))
    digest.update(b"\x00")
    for candidate in candidates:
        for part in (
            candidate.ontology_id,
            candidate.ontology_label,
            candidate.definition,
            candidate.parent_id,
            candidate.parent_label,
            "true" if candidate.is_obsolete else "false",
            candidate.ontology_release,
        ):
            digest.update(part.encode("utf-8"))
            digest.update(b"\x00")
    return digest.hexdigest()


def choice_file_paths(
    round_dir: Path | str, normalised_label: str
) -> tuple[Path, Path]:
    """The per-label result and error paths, sharded by the label's hash."""
    digest = hashlib.sha256(normalised_label.encode("utf-8")).hexdigest()
    folder = Path(round_dir) / CHOICES_DIRNAME / digest[:2]
    return folder / f"{digest}.yaml", folder / f"{digest}.error.yaml"


def _redact_api_keys(message: str, extra_keys: Sequence[str] = ()) -> str:
    """Remove any configured API key from a persisted error message."""
    keys = [os.environ.get(variable) for variable in _API_KEY_ENV_VARS]
    keys.extend(extra_keys)
    for value in keys:
        if value:
            message = message.replace(value, "***")
    return message


def _sum_round_recorded_cost(config: RoundConfig) -> float:
    """The cost already recorded by this round's finished result files.

    Seeding the spend cap from the result files makes ``--max-cost-usd``
    cumulative across an interrupt/resume rather than per invocation, so
    repeated resumes cannot spend without bound.
    """
    choices_dir = config.choices_dir
    if not choices_dir.is_dir():
        return 0.0
    total = 0.0
    for path in choices_dir.rglob("*.yaml"):
        if path.name.endswith(".error.yaml"):
            continue
        data = _read_result(path)
        if data is None:
            continue
        cost = _optional_float(data.get("cost_usd"))
        if cost is not None:
            total += cost
    return total


def _estimate_request_cost(
    chooser: Chooser, trait_label: str, candidates: Sequence[Candidate]
) -> float:
    """A conservative per-request cost estimate, or 0.0 when unavailable."""
    estimator = getattr(chooser, "estimate_cost_usd", None)
    if not callable(estimator):
        return 0.0
    try:
        estimate = estimator(trait_label, list(candidates))
    except Exception:  # noqa: BLE001 - an estimate must never block a run
        return 0.0
    if estimate is None:
        return 0.0
    try:
        value = float(estimate)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and value > 0.0 else 0.0


def _read_result(path: Path) -> Mapping[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None
    return data if isinstance(data, Mapping) else None


def _chooser_identity(chooser: Chooser | None, config: RoundConfig) -> tuple[str, str, str, str]:
    """The (id, version, model, context) a result is attributed to.

    Both ``choose`` (with a live chooser) and ``reduce`` (with only the pin)
    build their request fingerprints through here, so the two always agree.
    """
    if chooser is None:
        pin = config.chooser
        return pin.chooser_id, pin.resolved_version(), pin.model, pin.context
    chooser_id = str(getattr(chooser, "chooser_id", config.chooser.chooser_id))
    chooser_version = str(getattr(chooser, "chooser_version", ""))
    if not chooser_version:
        chooser_version = config.chooser.resolved_version()
    return (
        chooser_id,
        chooser_version,
        config.chooser.model,
        config.chooser.context,
    )


def _chooser_cost_for_label(chooser: Chooser | None, trait_label: str) -> float | None:
    """A cost recorded outside the ChoiceResult, when the chooser keeps one.

    The Jev chooser reports cost on the result. A chooser that tracks costs in
    a ``cost_records`` list (as the round's cost tests do) is supported too, so
    the ledger reflects what was actually spent.
    """
    records = getattr(chooser, "cost_records", None)
    if not records:
        return None
    for record in reversed(records):
        if getattr(record, "trait_label", None) == trait_label:
            try:
                return float(getattr(record, "cost_usd"))
            except (AttributeError, TypeError, ValueError):
                return None
    return None


@dataclass(frozen=True)
class ChooseLabelOutcome:
    trait_label: str
    status: str  # "chosen", "skipped", "error"
    cost_usd: float | None = None
    error_class: str = ""
    message: str = ""


def _choose_label(
    trait_label: str,
    candidates: Sequence[Candidate],
    chooser: Chooser,
    config: RoundConfig,
    extra_api_keys: Sequence[str] = (),
) -> ChooseLabelOutcome:
    normalised = gap_scan.normalize_trait_label(trait_label)
    result_path, error_path = choice_file_paths(config.round_dir, normalised)
    chooser_id, chooser_version, model, context = _chooser_identity(chooser, config)
    fingerprint = choice_fingerprint(
        chooser_id, chooser_version, model, context, trait_label, candidates
    )

    existing = _read_result(result_path)
    if existing is not None and existing.get("fingerprint") == fingerprint:
        return ChooseLabelOutcome(trait_label=trait_label, status="skipped")

    attempts = 0
    if error_path.is_file():
        previous = _read_result(error_path) or {}
        try:
            attempts = int(previous.get("attempts") or 0)
        except (TypeError, ValueError):
            attempts = 0

    try:
        result = chooser.choose(trait_label, list(candidates))
        if result is None:
            raise RoundStateError(
                f"chooser returned no result for a non-empty shortlist for "
                f"{trait_label!r}"
            )
    except BaseException as exc:  # noqa: BLE001 - persist then continue
        if isinstance(exc, KeyboardInterrupt):
            raise
        message = _redact_api_keys(str(exc), extra_api_keys)
        error_data: dict[str, Any] = {
            "trait_label": trait_label,
            "normalised_label": normalised,
            "fingerprint": fingerprint,
            "error_class": type(exc).__name__,
            "message": message,
            "attempts": attempts + 1,
            "at": _utc_now(),
        }
        raw_response = getattr(exc, "raw_response", None)
        if raw_response is not None:
            # A failure after a paid response still records the (key-free)
            # answer so a curator can diagnose it from the error file alone.
            error_data["raw_response"] = (
                dict(raw_response)
                if isinstance(raw_response, Mapping)
                else raw_response
            )
        _atomic_write_yaml(error_data, error_path)
        return ChooseLabelOutcome(
            trait_label=trait_label,
            status="error",
            error_class=type(exc).__name__,
            message=message,
        )

    cost = result.cost_usd
    if cost is None:
        cost = _chooser_cost_for_label(chooser, trait_label)
    offered = [candidate.ontology_id for candidate in candidates]
    _atomic_write_yaml(
        {
            "trait_label": trait_label,
            "normalised_label": normalised,
            "fingerprint": fingerprint,
            "offered_candidate_ids": offered,
            "selected_ontology_id": result.selected_ontology_id,
            "probabilities": dict(result.probabilities),
            "model_confidence": result.model_confidence,
            "model_version": result.model_version,
            "input_tokens": result.input_tokens,
            "cost_usd": cost,
            "raw_response": (
                dict(result.raw_response) if result.raw_response is not None else None
            ),
            "chooser_id": result.chooser_id or chooser_id,
            "chooser_version": result.chooser_version or chooser_version,
            "completed_at": _utc_now(),
        },
        result_path,
    )
    if error_path.is_file():
        try:
            error_path.unlink()
        except OSError:
            pass
    return ChooseLabelOutcome(trait_label=trait_label, status="chosen", cost_usd=cost)


@dataclass(frozen=True)
class ChooseOutcome:
    processed: int
    chosen: int
    skipped: int
    failed: int
    total_cost_usd: float
    cost_tracked: bool
    limit_reached: bool
    cost_cap_reached: bool


def _validate_shortlist_release(
    grouped: Mapping[str, Sequence[Candidate]], config: RoundConfig
) -> None:
    """Refuse a shortlist generated against a different ontology release."""
    for label, candidates in grouped.items():
        for candidate in candidates:
            release = candidate.ontology_release
            if release and release != config.ontology_release:
                raise RoundPinError(
                    f"shortlist for {label!r} was resolved against ontology "
                    f"release {release!r}, but round.yaml pins "
                    f"{config.ontology_release!r}"
                )


def _result_is_current(
    config: RoundConfig,
    trait_label: str,
    candidates: Sequence[Candidate],
    chooser: Chooser,
) -> bool:
    """True when a stored result already answers this exact request."""
    chooser_id, chooser_version, model, context = _chooser_identity(chooser, config)
    fingerprint = choice_fingerprint(
        chooser_id, chooser_version, model, context, trait_label, candidates
    )
    normalised = gap_scan.normalize_trait_label(trait_label)
    result_path, _ = choice_file_paths(config.round_dir, normalised)
    data = _read_result(result_path)
    return data is not None and data.get("fingerprint") == fingerprint


def run_choose(
    round_dir: Path | str,
    *,
    chooser: Chooser | None = None,
    workers: int = DEFAULT_WORKERS,
    limit: int | None = None,
    max_cost_usd: float | None = None,
    extra_api_keys: Sequence[str] = (),
) -> ChooseOutcome:
    """Choose for every shortlisted label, resumable per trait label.

    A label whose result file already carries the current fingerprint is
    skipped. Each result is written atomically, and a failure writes an error
    file that a later successful run removes. ``--limit`` caps the labels a
    pilot issues; ``--max-cost-usd`` stops new requests once the cumulative
    recorded cost reaches the cap. An interrupt leaves only complete files.
    """
    config = read_round_config(round_dir)
    if workers < 1:
        raise RoundStateError(f"workers must be at least 1, got {workers}")
    if limit is not None and limit < 0:
        raise RoundStateError(f"limit must be non-negative, got {limit}")
    if max_cost_usd is not None and max_cost_usd < 0:
        raise RoundStateError(
            f"max_cost_usd must be non-negative, got {max_cost_usd}"
        )

    grouped = choice_mod.read_shortlists(config.shortlists_path)
    _validate_shortlist_release(grouped, config)
    if chooser is None:
        chooser = _build_round_chooser(config)

    labels = list(grouped.keys())
    # The cap is cumulative for the round: seed it from the result files a
    # previous invocation (or a resumed run) already paid for.
    total_cost = _sum_round_recorded_cost(config)
    reserved_cost = 0.0
    cost_lock = threading.Lock()
    chosen = skipped = failed = processed = 0
    limit_reached = False
    cost_cap_reached = False

    pending = iter(labels)
    futures: dict[concurrent.futures.Future, str] = {}
    future_reserved: dict[concurrent.futures.Future, float] = {}
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    try:
        while True:
            while len(futures) < workers:
                try:
                    trait_label = next(pending)
                except StopIteration:
                    break
                candidates = grouped[trait_label]
                if not candidates:
                    continue
                if _result_is_current(config, trait_label, candidates, chooser):
                    # A finished label costs nothing and does not consume the
                    # pilot budget; count it as skipped and move on.
                    skipped += 1
                    continue
                if limit is not None and processed >= limit:
                    limit_reached = True
                    break
                if max_cost_usd is not None:
                    with cost_lock:
                        if total_cost + reserved_cost >= max_cost_usd:
                            cost_cap_reached = True
                            break
                future = executor.submit(
                    _choose_label,
                    trait_label,
                    list(candidates),
                    chooser,
                    config,
                    tuple(extra_api_keys),
                )
                futures[future] = trait_label
                if max_cost_usd is not None:
                    # Reserve an estimate for the in-flight request so a
                    # concurrent batch cannot collectively overshoot the cap.
                    with cost_lock:
                        reserve = _estimate_request_cost(
                            chooser, trait_label, candidates
                        )
                        reserved_cost += reserve
                    future_reserved[future] = reserve
                processed += 1

            if not futures:
                break

            done, _ = concurrent.futures.wait(
                futures, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for future in done:
                futures.pop(future, None)
                reserved = future_reserved.pop(future, 0.0)
                outcome = future.result()
                if outcome.status == "skipped":
                    skipped += 1
                elif outcome.status == "chosen":
                    chosen += 1
                else:
                    failed += 1
                with cost_lock:
                    reserved_cost -= reserved
                    if outcome.cost_usd is not None:
                        total_cost += outcome.cost_usd
    except KeyboardInterrupt:
        executor.shutdown(wait=True, cancel_futures=True)
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    return ChooseOutcome(
        processed=processed,
        chosen=chosen,
        skipped=skipped,
        failed=failed,
        total_cost_usd=total_cost,
        cost_tracked=total_cost > 0.0,
        limit_reached=limit_reached,
        cost_cap_reached=cost_cap_reached,
    )


def _build_round_chooser(config: RoundConfig) -> Chooser:
    """Build the chooser recorded in ``round.yaml``.

    A stub chooser replays its pinned fixture. A Jev chooser prefers a pinned
    fixture (hermetic); otherwise it uses the hosted endpoint from the
    environment. The API key is never pinned.
    """
    pin = config.chooser
    fixture = str(pin.fixture) if pin.fixture else None
    if pin.chooser_id == "stub":
        if fixture is None:
            raise RoundStateError(
                "round.yaml pins the stub chooser but no fixture path"
            )
        from curation.stub_chooser import StubChooser

        return StubChooser.from_path(
            fixture,
            chooser_id=pin.chooser_id,
            chooser_version=pin.version,
        )
    if pin.chooser_id == "jev":
        return choice_mod.build_chooser(
            "jev",
            None,
            jev_endpoint=os.environ.get("OPENGWASDB_JEV_ENDPOINT"),
            jev_model=pin.model or DEFAULT_JEV_MODEL,
            jev_api_key=os.environ.get("OPENGWASDB_JEV_API_KEY"),
            jev_fixture=fixture,
            jev_context=pin.context or DEFAULT_JEV_CONTEXT,
        )
    raise RoundStateError(f"unknown pinned chooser: {pin.chooser_id!r}")


# ---------------------------------------------------------------------------
# reduce
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Reconciliation:
    """The bucket partition of the round's work queue."""

    queue_total: int
    no_candidate: int
    pending: int
    error: int
    none_suitable: int
    proposed: int
    stale: int
    total_input_tokens: int
    total_cost_usd: float

    def as_rows(self) -> list[tuple[str, str]]:
        return [
            ("queue_total", str(self.queue_total)),
            (BUCKET_NO_CANDIDATE, str(self.no_candidate)),
            (BUCKET_PENDING, str(self.pending)),
            (BUCKET_ERROR, str(self.error)),
            (BUCKET_NONE_SUITABLE, str(self.none_suitable)),
            (BUCKET_PROPOSED, str(self.proposed)),
            ("stale", str(self.stale)),
            ("total_input_tokens", str(self.total_input_tokens)),
            ("total_cost_usd", f"{self.total_cost_usd:.6f}"),
        ]


@dataclass(frozen=True)
class ReduceOutcome:
    proposals_path: Path
    cost_ledger_path: Path
    reconciliation_path: Path
    reconciliation: Reconciliation
    complete: bool


def _reconstruct_result(data: Mapping[str, Any]) -> ChoiceResult:
    probabilities = data.get("probabilities") or {}
    return ChoiceResult(
        selected_ontology_id=str(data.get("selected_ontology_id") or ""),
        probabilities={str(key): float(value) for key, value in probabilities.items()},
        chooser_id=str(data.get("chooser_id") or ""),
        chooser_version=str(data.get("chooser_version") or ""),
        model_version=str(data.get("model_version") or ""),
        model_confidence=_optional_float(data.get("model_confidence")),
        input_tokens=_optional_int(data.get("input_tokens")),
        cost_usd=_optional_float(data.get("cost_usd")),
        raw_response=data.get("raw_response"),
        request_fingerprint=str(data.get("fingerprint") or ""),
    )


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _read_reconciliation(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f, delimiter="\t")
        header = next(reader, None)
        if not header or list(header) != list(RECONCILIATION_COLUMNS):
            return values
        for fields in reader:
            if len(fields) == 2:
                values[fields[0]] = fields[1]
    return values


def _reconcile_round(
    config: RoundConfig,
) -> tuple[Reconciliation, list[choice_mod.Proposal], list[list[str]]]:
    """Classify every queued label and build the proposals and cost ledger.

    This is the single classification used by both ``reduce`` and coverage's
    staleness check, so the two can never disagree about the buckets. Every
    queued label lands in exactly one bucket.
    """
    queue_rows = candidates_mod.read_work_queue(config.queue_path)
    queue_entries: list[tuple[str, str]] = []
    for row in queue_rows:
        label = (row.get("trait_label") or "").strip()
        normalised = gap_scan.normalize_trait_label(label)
        if normalised:
            queue_entries.append((label, normalised))

    grouped = choice_mod.read_shortlists(config.shortlists_path)
    _validate_shortlist_release(grouped, config)
    shortlists = {
        gap_scan.normalize_trait_label(label): list(candidates)
        for label, candidates in grouped.items()
    }

    counts = {bucket: 0 for bucket in BUCKETS}
    stale = 0
    proposals: list[choice_mod.Proposal] = []
    ledger_rows: list[list[str]] = []
    total_tokens = 0
    total_cost = 0.0

    for label, normalised in queue_entries:
        candidates = shortlists.get(normalised) or []
        if not candidates:
            counts[BUCKET_NO_CANDIDATE] += 1
            ledger_rows.append([_tsv(label), "", "", ""])
            continue

        result_path, error_path = choice_file_paths(config.round_dir, normalised)
        data = _read_result(result_path)
        chooser_id, chooser_version, model, context = _chooser_identity(None, config)
        fingerprint = choice_fingerprint(
            chooser_id, chooser_version, model, context, label, candidates
        )
        if data is None:
            if error_path.is_file():
                counts[BUCKET_ERROR] += 1
                ledger_rows.append([_tsv(label), "", "", ""])
            else:
                counts[BUCKET_PENDING] += 1
                ledger_rows.append([_tsv(label), "", "", ""])
            continue
        if str(data.get("fingerprint") or "") != fingerprint:
            stale += 1
            counts[BUCKET_PENDING] += 1
            ledger_rows.append([_tsv(label), "", "", ""])
            continue

        result = _reconstruct_result(data)
        proposal = choice_mod.build_proposal(label, candidates, result)
        proposals.append(proposal)
        if proposal.selected_ontology_id == NONE_SUITABLE:
            counts[BUCKET_NONE_SUITABLE] += 1
        else:
            counts[BUCKET_PROPOSED] += 1

        input_tokens = _optional_int(data.get("input_tokens"))
        cost = _optional_float(data.get("cost_usd"))
        if input_tokens is not None:
            total_tokens += input_tokens
        if cost is not None:
            total_cost += cost
        ledger_rows.append(
            [
                _tsv(label),
                "" if input_tokens is None else str(input_tokens),
                "" if cost is None else f"{cost:.6f}",
                _tsv(str(data.get("model_version") or "")),
            ]
        )

    reconciliation = Reconciliation(
        queue_total=len(queue_entries),
        no_candidate=counts[BUCKET_NO_CANDIDATE],
        pending=counts[BUCKET_PENDING],
        error=counts[BUCKET_ERROR],
        none_suitable=counts[BUCKET_NONE_SUITABLE],
        proposed=counts[BUCKET_PROPOSED],
        stale=stale,
        total_input_tokens=total_tokens,
        total_cost_usd=total_cost,
    )
    if sum(
        (
            reconciliation.no_candidate,
            reconciliation.pending,
            reconciliation.error,
            reconciliation.none_suitable,
            reconciliation.proposed,
        )
    ) != reconciliation.queue_total:
        raise RoundStateError(
            "internal error: reconciliation buckets do not sum to the queue"
        )

    return reconciliation, proposals, ledger_rows


def run_reduce(
    round_dir: Path | str, *, allow_incomplete: bool = False
) -> ReduceOutcome:
    """Fold the result files into proposals, a cost ledger, and a reconciliation.

    Every queued label is classified into exactly one bucket: ``no_candidate``
    (nothing retrieved), ``pending`` (no result, or a result for a stale
    shortlist), ``error`` (the request failed), ``none_suitable`` (a confident
    abstention), or ``proposed``. ``complete`` is false when any label is
    pending or errored; the CLI turns that into a non-zero exit unless
    ``--allow-incomplete`` was given. ``allow_incomplete`` is accepted for
    callers that want to proceed regardless and is otherwise informational.
    """
    config = read_round_config(round_dir)
    reconciliation, proposals, ledger_rows = _reconcile_round(config)

    _atomic_write_text(choice_mod.format_proposals_tsv(proposals), config.proposals_path)
    ledger_lines = ["\t".join(COST_LEDGER_COLUMNS)]
    ledger_lines.extend("\t".join(row) for row in ledger_rows)
    _atomic_write_text("\n".join(ledger_lines) + "\n", config.cost_ledger_path)
    recon_lines = ["\t".join(RECONCILIATION_COLUMNS)]
    recon_lines.extend(
        f"{metric}\t{value}" for metric, value in reconciliation.as_rows()
    )
    _atomic_write_text("\n".join(recon_lines) + "\n", config.reconciliation_path)

    complete = reconciliation.pending == 0 and reconciliation.error == 0
    return ReduceOutcome(
        proposals_path=config.proposals_path,
        cost_ledger_path=config.cost_ledger_path,
        reconciliation_path=config.reconciliation_path,
        reconciliation=reconciliation,
        complete=complete,
    )


def _tsv(value: str) -> str:
    return _TSV_UNSAFE_RE.sub(" ", value)


# ---------------------------------------------------------------------------
# promote
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromoteOutcome:
    promotion: promotion.PromotionOutcome
    review_queue_path: Path
    no_suitable_path: Path


def run_promote(
    round_dir: Path | str,
    *,
    reviewed_queue: Path | str | None = None,
    rejections: Path | str | None = None,
    as_of: str | None = None,
) -> PromoteOutcome:
    """Apply the round's proposals to the mapping table.

    Confident proposals are appended; ``none_suitable`` abstentions are never
    written to the table. A confident abstention is recorded in
    ``no-suitable-term.tsv``; an uncertain one goes to the review queue, where a
    curator may amend in a real term. A reviewed copy of the queue is passed
    back with ``reviewed_queue`` on a later run.
    """
    config = read_round_config(round_dir)
    outcome = promotion.run_promotion(
        proposals_path=config.proposals_path,
        review_queue_path=config.review_queue_path,
        resource_dir=config.resource_dir,
        shortlists_path=config.shortlists_path,
        rejections_path=rejections,
        reviewed_queue_path=reviewed_queue,
        no_suitable_path=config.no_suitable_path,
        confidence_threshold=config.confidence_threshold,
        margin_threshold=config.margin_threshold,
        as_of=as_of,
    )
    return PromoteOutcome(
        promotion=outcome,
        review_queue_path=config.review_queue_path,
        no_suitable_path=config.no_suitable_path,
    )


# ---------------------------------------------------------------------------
# coverage
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoverageOutcome:
    report: coverage.CoverageReport
    report_text: str
    report_path: Path | None


def _verify_reconciliation(config: RoundConfig) -> dict[str, str]:
    """Require a current ``reconciliation.tsv`` before coverage reports it.

    Coverage must never present bucket counts that do not describe the current
    queue/shortlists/results, so a missing or stale reconciliation fails loudly
    rather than silently reporting zeros.
    """
    if not config.reconciliation_path.is_file():
        raise RoundStateError(
            f"reconciliation.tsv does not exist: {config.reconciliation_path} "
            "(run round-reduce first)"
        )
    stored = _read_reconciliation(config.reconciliation_path)
    current, _, _ = _reconcile_round(config)
    expected = dict(current.as_rows())
    mismatched = {
        key: (stored.get(key), value)
        for key, value in expected.items()
        if stored.get(key) != value
    }
    if mismatched:
        detail = ", ".join(
            f"{key}: recorded {recorded!r} vs current {value!r}"
            for key, (recorded, value) in sorted(mismatched.items())
        )
        raise RoundStateError(
            f"reconciliation.tsv is stale relative to the current "
            f"shortlists/results; re-run round-reduce ({detail})"
        )
    return stored


def run_coverage(
    round_dir: Path | str,
    *,
    manifests: Sequence[Path | str] | None = None,
    report_format: str = coverage.DEFAULT_FORMAT,
    report_path: Path | str | None = None,
) -> CoverageOutcome:
    """Compute the round's coverage from the whole post-promotion table.

    The after state is read from every row of ``mapping.tsv``, so a row that
    existed before this round still resolves the Analyses it covers. The bucket
    counts come from ``reconciliation.tsv`` and the cost from
    ``cost-ledger.tsv``.
    """
    config = read_round_config(round_dir)
    manifest_paths = tuple(manifests) if manifests is not None else config.manifests
    family_stats = (
        coverage.scan_coverage_manifests(manifest_paths) if manifest_paths else {}
    )

    mapping_after = config.mapping_path
    promoted_labels = coverage.promoted_labels_from_mapping(
        mapping_after,
        config.mapping_before_path if config.mapping_before_path.is_file() else None,
    )
    mapped_after_labels = coverage.read_mapping_labels(mapping_after)
    reconciliation = _verify_reconciliation(config)

    def _count(bucket: str) -> int:
        raw = reconciliation.get(bucket, "0")
        try:
            return int(raw)
        except ValueError:
            return 0

    cost_usd, cost_tracked = (
        coverage.read_cost_report(config.cost_ledger_path)
        if config.cost_ledger_path.is_file()
        else (0.0, False)
    )
    report = coverage.compute_coverage(
        family_stats,
        promoted_labels=promoted_labels,
        review_queue_size=coverage.read_review_queue_size(config.review_queue_path),
        no_candidate_count=_count(BUCKET_NO_CANDIDATE),
        none_suitable_count=_count(BUCKET_NONE_SUITABLE),
        pending_count=_count(BUCKET_PENDING),
        error_count=_count(BUCKET_ERROR),
        cost_usd=cost_usd,
        cost_tracked=cost_tracked,
        mapped_after_labels=mapped_after_labels,
    )
    report_text = coverage.render_report(report, report_format)

    resolved_path: Path | None = None
    if report_path is not None:
        resolved_path = Path(report_path)
    else:
        extension = _REPORT_EXTENSIONS.get(report_format, "txt")
        resolved_path = config.round_dir / f"coverage-report.{extension}"
    _atomic_write_text(report_text, resolved_path)
    return CoverageOutcome(report=report, report_text=report_text, report_path=resolved_path)


# ---------------------------------------------------------------------------
# The thin convenience runner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoundOutcome:
    config: RoundConfig
    gap_scan: GapScanOutcome
    candidates: CandidatesOutcome
    choose: ChooseOutcome
    reduce: ReduceOutcome
    promote: PromoteOutcome | None
    coverage: CoverageOutcome | None


def run_round(
    round_dir: Path | str,
    *,
    chooser: Chooser | None = None,
    embedding: EmbeddingChannel | None = None,
    workers: int = DEFAULT_WORKERS,
    limit: int | None = None,
    max_cost_usd: float | None = None,
    reviewed_queue: Path | str | None = None,
    rejections: Path | str | None = None,
    as_of: str | None = None,
    report_format: str = coverage.DEFAULT_FORMAT,
    report_path: Path | str | None = None,
    manifests: Sequence[Path | str] | None = None,
) -> RoundOutcome:
    """Run every stage in order, stopping after ``reduce`` when incomplete.

    Each stage is idempotent, so a rerun resumes rather than repeating work.
    Promotion and coverage run only when ``reduce`` reconciled the whole queue.
    """
    config = read_round_config(round_dir)
    gap = run_gap_scan(config.round_dir)
    candidates = run_candidates(config.round_dir, embedding=embedding)
    choose = run_choose(
        config.round_dir,
        chooser=chooser,
        workers=workers,
        limit=limit,
        max_cost_usd=max_cost_usd,
    )
    reduced = run_reduce(config.round_dir, allow_incomplete=True)
    if not reduced.complete:
        return RoundOutcome(
            config=config,
            gap_scan=gap,
            candidates=candidates,
            choose=choose,
            reduce=reduced,
            promote=None,
            coverage=None,
        )

    promoted = run_promote(
        config.round_dir,
        reviewed_queue=reviewed_queue,
        rejections=rejections,
        as_of=as_of,
    )
    covered = run_coverage(
        config.round_dir,
        manifests=manifests,
        report_format=report_format,
        report_path=report_path,
    )
    return RoundOutcome(
        config=config,
        gap_scan=gap,
        candidates=candidates,
        choose=choose,
        reduce=reduced,
        promote=promoted,
        coverage=covered,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _add_round_location(parser: argparse.ArgumentParser, *, required: bool = True) -> None:
    parser.add_argument(
        "--round-dir",
        default=None,
        metavar="DIR",
        required=required,
        help="the round directory holding round.yaml and the stage artifacts",
    )


def _resolve_cli_round_dir(args: argparse.Namespace) -> Path:
    if args.round_dir:
        return Path(args.round_dir)
    round_id = getattr(args, "round_id", None)
    if not round_id:
        raise RoundStateError("supply --round-dir or --round-id")
    return round_dir_for(round_id, getattr(args, "rounds_root", DEFAULT_ROUNDS_ROOT))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="curation-round",
        description=(
            "Resumable per-trait Canonical Trait Mapping curation round: "
            "round-init, gap-scan, candidates, choose, reduce, promote, coverage."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser(
        "round-init", help="pin the round and write round.yaml"
    )
    init_parser.add_argument("--round-dir", default=None, metavar="DIR")
    init_parser.add_argument("--round-id", default=None, metavar="ID")
    init_parser.add_argument(
        "--rounds-root", default=str(DEFAULT_ROUNDS_ROOT), metavar="DIR"
    )
    init_parser.add_argument("--index", required=True, metavar="JSON")
    init_parser.add_argument("--ontology-embeddings", default=None, metavar="DIR")
    init_parser.add_argument("--trait-embeddings", default=None, metavar="DIR")
    init_parser.add_argument(
        "--embedding-model", default=PINNED_EMBEDDING_MODEL_ID, metavar="MODEL"
    )
    init_parser.add_argument("--chooser", default="stub", metavar="NAME")
    init_parser.add_argument("--chooser-version", default=None, metavar="VERSION")
    init_parser.add_argument("--chooser-model", default=None, metavar="MODEL")
    init_parser.add_argument("--chooser-context", default=None, metavar="TEXT")
    init_parser.add_argument("--fixture", default=None, metavar="PATH")
    init_parser.add_argument("--manifests", nargs="+", default=None, metavar="MANIFEST")
    init_parser.add_argument("--queue-tsv", default=None, metavar="TSV")
    init_parser.add_argument(
        "--resource-dir", default=str(promotion.DEFAULT_RESOURCE_DIR), metavar="DIR"
    )
    init_parser.add_argument(
        "--shortlist-size", type=int, default=candidates_mod.DEFAULT_SHORTLIST_SIZE
    )
    init_parser.add_argument(
        "--confidence-threshold", type=float, default=promotion.DEFAULT_CONFIDENCE_THRESHOLD
    )
    init_parser.add_argument(
        "--margin-threshold", type=float, default=promotion.DEFAULT_MARGIN_THRESHOLD
    )
    init_parser.add_argument("--threshold-evidence", default="", metavar="TEXT")
    init_parser.add_argument("--force", action="store_true")
    init_parser.set_defaults(handler=_main_init)

    gap_parser = subparsers.add_parser("gap-scan", help="write the work queue")
    _add_round_location(gap_parser)
    gap_parser.set_defaults(handler=_main_gap_scan)

    candidates_parser = subparsers.add_parser(
        "candidates", help="generate shortlists for the queue"
    )
    _add_round_location(candidates_parser)
    candidates_parser.set_defaults(handler=_main_candidates)

    embed_traits_parser = subparsers.add_parser(
        "embed-traits",
        help="embed the round's queue with the pinned ontology store's model",
    )
    _add_round_location(embed_traits_parser)
    embed_traits_parser.add_argument("--output", default=None, metavar="DIR")
    embed_traits_parser.add_argument(
        "--endpoint",
        default=os.environ.get("OPENGWASDB_EMBEDDING_ENDPOINT"),
        metavar="URL",
    )
    embed_traits_parser.add_argument(
        "--api-key",
        default=os.environ.get("OPENGWASDB_EMBEDDING_API_KEY"),
        metavar="KEY",
    )
    embed_traits_parser.add_argument(
        "--served-model",
        default=os.environ.get("OPENGWASDB_EMBEDDING_SERVED_MODEL"),
        metavar="NAME",
    )
    embed_traits_parser.add_argument("--batch-size", type=int, default=128)
    embed_traits_parser.add_argument("--chunk-size", type=int, default=1000)
    embed_traits_parser.add_argument("--max-retries", type=int, default=5)
    embed_traits_parser.set_defaults(handler=_main_embed_traits)

    choose_parser = subparsers.add_parser(
        "choose", help="map each shortlisted label to a choice result"
    )
    _add_round_location(choose_parser)
    choose_parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    choose_parser.add_argument("--limit", type=int, default=None, metavar="N")
    choose_parser.add_argument("--max-cost-usd", type=float, default=None, metavar="USD")
    choose_parser.add_argument("--fixture", default=None, metavar="PATH")
    choose_parser.add_argument(
        "--jev-endpoint", default=os.environ.get("OPENGWASDB_JEV_ENDPOINT"), metavar="URL"
    )
    choose_parser.add_argument(
        "--jev-api-key", default=os.environ.get("OPENGWASDB_JEV_API_KEY"), metavar="KEY"
    )
    choose_parser.set_defaults(handler=_main_choose)

    reduce_parser = subparsers.add_parser(
        "reduce", help="aggregate the results into proposals and a ledger"
    )
    _add_round_location(reduce_parser)
    reduce_parser.add_argument("--allow-incomplete", action="store_true")
    reduce_parser.set_defaults(handler=_main_reduce)

    promote_parser = subparsers.add_parser(
        "promote", help="write confident proposals to the mapping table"
    )
    _add_round_location(promote_parser)
    promote_parser.add_argument("--reviewed-queue", default=None, metavar="TSV")
    promote_parser.add_argument("--rejections", default=None, metavar="TSV")
    promote_parser.add_argument("--as-of", default=None, metavar="YYYY-MM-DD")
    promote_parser.set_defaults(handler=_main_promote)

    coverage_parser = subparsers.add_parser(
        "coverage", help="report the round's before/after coverage"
    )
    _add_round_location(coverage_parser)
    coverage_parser.add_argument("--manifests", nargs="+", default=None, metavar="MANIFEST")
    coverage_parser.add_argument(
        "--format", choices=sorted(coverage.RENDERERS_MAP), default=coverage.DEFAULT_FORMAT
    )
    coverage_parser.add_argument("--report", default=None, metavar="PATH")
    coverage_parser.add_argument("--output", default=None, metavar="PATH")
    coverage_parser.set_defaults(handler=_main_coverage)

    return parser


def _main_init(args: argparse.Namespace) -> int:
    chooser_model = args.chooser_model or ""
    if args.chooser == "jev" and not chooser_model:
        chooser_model = DEFAULT_JEV_MODEL
    chooser_version = args.chooser_version
    if chooser_version is None:
        # Jev's version is its model id; pinning the model pins the version.
        chooser_version = "1" if args.chooser == "stub" else chooser_model
    try:
        round_dir = _resolve_cli_round_dir(args)
        config = init_round(
            round_dir,
            index_path=args.index,
            ontology_embeddings=args.ontology_embeddings,
            trait_embeddings=args.trait_embeddings,
            embedding_model=args.embedding_model,
            chooser_id=args.chooser,
            chooser_version=chooser_version,
            chooser_model=chooser_model,
            chooser_context=args.chooser_context or DEFAULT_JEV_CONTEXT,
            chooser_fixture=args.fixture,
            manifests=args.manifests or (),
            queue_tsv=args.queue_tsv,
            resource_dir=args.resource_dir,
            shortlist_size=args.shortlist_size,
            confidence_threshold=args.confidence_threshold,
            margin_threshold=args.margin_threshold,
            threshold_evidence=args.threshold_evidence,
            round_id=args.round_id,
            force=args.force,
        )
    except (RoundError, IndexFormatError, EmbeddingStoreError) as exc:
        print(f"round-init: error: {exc}", file=sys.stderr)
        return 1
    print(f"round-init: pinned round at {config.round_dir}", file=sys.stderr)
    return 0


def _main_gap_scan(args: argparse.Namespace) -> int:
    try:
        outcome = run_gap_scan(_resolve_cli_round_dir(args))
    except (RoundError, gap_scan.GapScanError, IndexFormatError) as exc:
        print(f"gap-scan: error: {exc}", file=sys.stderr)
        return 1
    print(
        f"gap-scan: {len(outcome.entries)} label(s) queued, "
        f"{outcome.already_mapped_excluded} already mapped",
        file=sys.stderr,
    )
    return 0


def _main_candidates(args: argparse.Namespace) -> int:
    try:
        outcome = run_candidates(_resolve_cli_round_dir(args))
    except (RoundError, IndexFormatError, EmbeddingError) as exc:
        print(f"candidates: error: {exc}", file=sys.stderr)
        return 1
    print(
        f"candidates: {outcome.candidate_rows} candidate row(s) for "
        f"{outcome.labels} label(s); {outcome.no_candidate_labels} with none",
        file=sys.stderr,
    )
    return 0


def _main_embed_traits(args: argparse.Namespace) -> int:
    try:
        outcome = run_embed_traits(
            _resolve_cli_round_dir(args),
            endpoint=args.endpoint,
            api_key=args.api_key,
            served_model=args.served_model,
            output=args.output,
            batch_size=args.batch_size,
            chunk_size=args.chunk_size,
            max_retries=args.max_retries,
        )
    except (RoundError, EmbeddingError, IndexFormatError) as exc:
        print(f"embed-traits: error: {exc}", file=sys.stderr)
        return 1
    print(
        f"embed-traits: wrote {outcome.count} vectors (model "
        f"{outcome.model_id}) to {outcome.store_dir}",
        file=sys.stderr,
    )
    return 0


def _main_choose(args: argparse.Namespace) -> int:
    round_dir = _resolve_cli_round_dir(args)
    chooser = None
    extra_api_keys = (args.jev_api_key,) if args.jev_api_key else ()
    try:
        config = read_round_config(round_dir)
        if config.chooser.chooser_id == "jev":
            # Honour an explicit --jev-endpoint/--jev-api-key even without a
            # fixture; otherwise the flags would silently do nothing.
            chooser = choice_mod.build_chooser(
                "jev",
                args.fixture,
                jev_endpoint=args.jev_endpoint,
                jev_model=config.chooser.model or DEFAULT_JEV_MODEL,
                jev_api_key=args.jev_api_key,
                jev_fixture=args.fixture,
                jev_context=config.chooser.context or DEFAULT_JEV_CONTEXT,
            )
        elif args.fixture:
            chooser = choice_mod.build_chooser(
                config.chooser.chooser_id, args.fixture
            )
    except (RoundError, choice_mod.ChoiceError) as exc:
        print(f"choose: error: {exc}", file=sys.stderr)
        return 1
    try:
        outcome = run_choose(
            round_dir,
            chooser=chooser,
            workers=args.workers,
            limit=args.limit,
            max_cost_usd=args.max_cost_usd,
            extra_api_keys=extra_api_keys,
        )
    except KeyboardInterrupt:
        print("choose: interrupted; completed result files are intact", file=sys.stderr)
        return 130
    except (RoundError, choice_mod.ChoiceError) as exc:
        print(f"choose: error: {exc}", file=sys.stderr)
        return 1
    print(
        f"choose: {outcome.chosen} chosen, {outcome.skipped} skipped, "
        f"{outcome.failed} failed; cost ${outcome.total_cost_usd:.4f}",
        file=sys.stderr,
    )
    return 0


def _main_reduce(args: argparse.Namespace) -> int:
    try:
        outcome = run_reduce(
            _resolve_cli_round_dir(args), allow_incomplete=args.allow_incomplete
        )
    except (RoundError, choice_mod.ChoiceError) as exc:
        print(f"reduce: error: {exc}", file=sys.stderr)
        return 1
    recon = outcome.reconciliation
    print(
        f"reduce: {recon.proposed} proposed, {recon.none_suitable} no-suitable, "
        f"{recon.no_candidate} no-candidate, {recon.pending} pending, "
        f"{recon.error} errored; total cost ${recon.total_cost_usd:.4f}",
        file=sys.stderr,
    )
    if not outcome.complete and not args.allow_incomplete:
        print(
            "reduce: incomplete (pending or errored labels); "
            "re-run choose or pass --allow-incomplete",
            file=sys.stderr,
        )
        return 1
    return 0


def _main_promote(args: argparse.Namespace) -> int:
    try:
        outcome = run_promote(
            _resolve_cli_round_dir(args),
            reviewed_queue=args.reviewed_queue,
            rejections=args.rejections,
            as_of=args.as_of,
        )
    except (RoundError, promotion.PromotionError, choice_mod.ChoiceError) as exc:
        print(f"promote: error: {exc}", file=sys.stderr)
        return 1
    plan = outcome.promotion.plan
    print(
        f"promote: {len(plan.promoted)} promoted, {len(plan.queued)} queued, "
        f"{len(plan.no_suitable)} no-suitable, {len(plan.suppressed)} suppressed; "
        f"resource version {outcome.promotion.version}",
        file=sys.stderr,
    )
    return 0


def _main_coverage(args: argparse.Namespace) -> int:
    output = args.output or args.report
    try:
        outcome = run_coverage(
            _resolve_cli_round_dir(args),
            manifests=args.manifests,
            report_format=args.format,
            report_path=output,
        )
    except (RoundError, coverage.CoverageError, gap_scan.GapScanError) as exc:
        print(f"coverage: error: {exc}", file=sys.stderr)
        return 1
    if output is None:
        sys.stdout.write(outcome.report_text)
        sys.stdout.flush()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
