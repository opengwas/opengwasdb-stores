#!/usr/bin/env python3
"""Emit unbuilt, acceptance-pending MVP ancestry and META candidates (#221).

This is Phase B tooling: it turns the metadata-only MVP partition into candidate
Release Bundles for the single-ancestry Stores and the cross-ancestry META
Store. It is deliberately conservative.

* It emits the observed Store with ``status: accepted`` and an ``accepted_at``
  so the dev release workflow may build it (#247), and its ``validation.yaml``
  ``acceptance`` block keeps ``human_acceptance`` ``pending``. That acceptance
  is workflow-buildability, not scientific release acceptance or publication.
  A completed child stays ``status: candidate`` while its reference is merely
  pending/blocked, so the workflow refuses it. Nothing here registers, builds,
  completes or publishes a Store, and no existing bundle is overwritten.
* The ancestry partition is validated against the intended bound counts and
  must be disjoint; a missing or drifted membership fails loudly.
* META is emitted observed-only: no ``derived_from`` and no completion block,
  regardless of the fact that an EUR panel exists.
* A lineage-linked completed child is emitted only when its ``completion_reference``
  is ``qualified``. A ``hold`` reference (AMR today) produces a recorded plan,
  not a buildable bundle.
* Without a resolution table it runs in ``plan`` mode: it writes the validated
  partition and a per-store plan only, refusing to invent Analytical Metadata.

Usage:

    python3 resources/generators/dbgap-mvp/generate_candidate.py plan \\
        --mapping /data/opengwasdb/work/epic232/220/conversion-retry-.../mapping.tsv \\
        --out-dir /data/opengwasdb/work/epic232/221

    python3 resources/generators/dbgap-mvp/generate_candidate.py emit \\
        --mapping .../mapping.tsv --resolution /path/to/resolution.tsv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]

# The partition module lives beside this script in a hyphenated directory that is
# not importable as a package; load it by path.
_spec = importlib.util.spec_from_file_location(
    "dbgap_mvp_partition", Path(__file__).with_name("partition.py")
)
assert _spec and _spec.loader
partition_module = importlib.util.module_from_spec(_spec)
sys.modules["dbgap_mvp_partition"] = partition_module
_spec.loader.exec_module(partition_module)

# Shared containment-checked, exclusive-temp writer (parent BAF pattern).
_safe_spec = importlib.util.spec_from_file_location(
    "ogstores_safe_write", REPO_ROOT / "resources" / "generators" / "lib" / "safe_write.py"
)
assert _safe_spec and _safe_spec.loader
safe_write = importlib.util.module_from_spec(_safe_spec)
sys.modules["ogstores_safe_write"] = safe_write
_safe_spec.loader.exec_module(safe_write)

# The parent's exact metadata content-hash algorithm.
_content_spec = importlib.util.spec_from_file_location(
    "ogstores_metadata_content_sha",
    REPO_ROOT / "resources" / "generators" / "lib" / "metadata_content_sha.py",
)
assert _content_spec and _content_spec.loader
metadata_content = importlib.util.module_from_spec(_content_spec)
sys.modules["ogstores_metadata_content_sha"] = metadata_content
_content_spec.loader.exec_module(metadata_content)

from ogstores import bundle as bundle_module  # noqa: E402
from resources.generators.lib import generation_rules  # noqa: E402
from opengwasdb.model.analyses import SampleSizeKind  # noqa: E402

DEFAULT_CONFIG = "resources/generators/dbgap-mvp/config-candidate.yaml"

#: The columns a resolution table must carry for a bundle to pass the registry
#: and shared-core Analysis contract.
REQUIRED_RESOLUTION_COLUMNS: tuple[str, ...] = (
    "analysis_id",
    "assigned_ancestry",
    "ancestry_assignment_method",
    "source_ancestry_label",
    "stored_effect_scale",
    "sample_size_kind",
    "sample_size_scope",
    "sample_size",
    "original_effect_scale",
    "original_sd_method",
    # Dataset-source binding: the real source file path, genome build and
    # source label/ID must be present on every row. The emitter never invents a
    # default source build or sample-size/SD value.
    "source_file",
    "source_genome_build",
    "source_label",
    "source_analysis_id",
)


class CandidateError(ValueError):
    """The inputs cannot produce a schema-valid candidate Release Bundle."""


# ---------------------------------------------------------------------------
# small I/O helpers
# ---------------------------------------------------------------------------


def read_tsv(path: Path | str) -> list[dict[str, str]]:
    tsv_path = Path(path)
    if not tsv_path.is_file():
        raise CandidateError(f"TSV not found: {tsv_path}")
    with tsv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        fieldnames = tuple(reader.fieldnames or ())
        rows = [{key: (value or "").strip() for key, value in row.items()} for row in reader]
    if not fieldnames:
        raise CandidateError(f"TSV has no header: {tsv_path}")
    if not rows:
        raise CandidateError(f"TSV has no data rows: {tsv_path}")
    return rows


def sha256_file(path: Path | str) -> str:
    """Hash a regular metadata file (never an association file)."""
    import hashlib

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


DEFAULT_REGISTRY_ROOTS: tuple[Path, ...] = (
    REPO_ROOT / "stores",
    Path("/home/gh13047/repo/opengwasdb-stores/stores"),
)

META_ANCESTRY_ALIASES: frozenset[str] = frozenset(
    {"", "META", "MULTI-ANCESTRY", "MULTIANCESTRY", "MIXED"}
)

#: Sample-size semantics come from the public core enum, never invented here.
INTEGER_SAMPLE_SIZE_KINDS: frozenset[str] = frozenset(
    {SampleSizeKind.TOTAL.value, SampleSizeKind.CASE_CONTROL.value}
)
NON_INTEGER_SAMPLE_SIZE_KINDS: frozenset[str] = frozenset(
    {SampleSizeKind.EFFECTIVE.value, SampleSizeKind.VARIANT_LEVEL.value}
)
KNOWN_SAMPLE_SIZE_KINDS: frozenset[str] = frozenset(kind.value for kind in SampleSizeKind)


def _normalised_ancestry(value: str | None) -> str:
    return (value or "").strip().upper().replace("_", "-").replace(" ", "-")


def assert_not_registry_store_root(
    out_dir: Path | str, registry_roots: Sequence[Path | str] = ()
) -> None:
    """Refuse to emit into a registry ``stores/`` tree from any checkout.

    A candidate passes ``bundle.check()``, and the Phase A Snakefile targets
    every valid bundle under ``stores/``; writing there would make an unbuilt,
    acceptance-pending candidate buildable. Rejects this checkout's root, the
    canonical/primary root, any caller-supplied ``--registry-root``, any path
    component literally named ``stores``, and any symlinked ancestor.
    """
    path = Path(out_dir)
    roots = [Path(root).resolve() for root in (*DEFAULT_REGISTRY_ROOTS, *registry_roots)]
    resolved = path.resolve()
    for root in roots:
        if resolved == root or root in resolved.parents:
            raise CandidateError(
                f"refusing to emit under the registry Store root {root}: {path}"
            )
    if "stores" in resolved.parts:
        raise CandidateError(
            f"refusing to emit under a path component named 'stores': {path}"
        )
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise CandidateError(
                f"refusing to emit under a symlinked output path: {ancestor}"
            )


def _finite_positive(value: object, where: str) -> None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise CandidateError(f"{where} is not numeric: {value!r}") from exc
    if not math.isfinite(number) or number <= 0:
        raise CandidateError(f"{where} must be finite and positive: {value!r}")


def _positive_integer(value: object, where: str) -> None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise CandidateError(f"{where} is not numeric: {value!r}") from exc
    if not math.isfinite(number) or number <= 0 or not number.is_integer():
        raise CandidateError(f"{where} must be a positive integer: {value!r}")


def validate_resolution_row(
    row: Mapping[str, str],
    ancestry: str,
    assigned_ancestry: str | None,
    manifest_row: Mapping[str, str] | None = None,
) -> None:
    """Per-row gates before any emit.

    Non-empty required values, exact source-ID binding, the manifest's own
    data-file path and genome assembly, the named sidecar's genome assembly,
    finite positive sample size/SD, and an unambiguous ancestry.
    """
    analysis_id = row.get("analysis_id", "")
    for column in REQUIRED_RESOLUTION_COLUMNS:
        # assigned_ancestry is handled below: META is canonically empty and an
        # explicit unassigned record keeps an empty value rather than being
        # forced to the bucket label. Every other required value must be present.
        if column == "assigned_ancestry":
            continue
        if not (row.get(column) or "").strip():
            raise CandidateError(
                f"resolution row {analysis_id!r} has a blank required column {column!r}"
            )
    declared = (row.get("assigned_ancestry") or "").strip()
    if ancestry == "META":
        # META must be unambiguously empty or a META/multi-ancestry alias; a
        # single-ancestry label such as EUR or AFR on a META row is a
        # cross-ancestry leak, not a normal value.
        if _normalised_ancestry(declared) not in META_ANCESTRY_ALIASES:
            raise CandidateError(
                f"resolution row {analysis_id!r} assigned_ancestry {declared!r} is "
                "not an unambiguous empty/META value"
            )
    elif declared and declared != (assigned_ancestry or ""):
        # An explicit unassigned record (empty value) is preserved rather than
        # forced to the bucket label; a non-empty mismatch still fails.
        raise CandidateError(
            f"resolution row {analysis_id!r} assigned_ancestry {declared!r} does "
            f"not match the {ancestry} bucket"
        )
    label = (row.get("source_ancestry_label") or "").strip()
    # The source label is the authority mapping bucket for this membership, so it
    # must match exactly (META included). The public core accepting a mismatch is
    # not a semantic pass.
    if label != ancestry:
        raise CandidateError(
            f"resolution row {analysis_id!r} source_ancestry_label {label!r} does "
            f"not match the {ancestry} authority mapping bucket"
        )
    method = (row.get("ancestry_assignment_method") or "").strip()
    # The canonical pair must be consistent: the effective published ancestry is
    # empty for META (a META alias is not a published single-population), else the
    # declared value. An empty published ancestry must say `unassigned`; a
    # published MVP AF ancestry must say `af_assigned`. The public enum tolerating
    # any other pairing is not a semantic pass.
    published = "" if ancestry == "META" else declared
    if not published:
        if method != "unassigned":
            raise CandidateError(
                f"resolution row {analysis_id!r} publishes an empty ancestry but "
                f"ancestry_assignment_method {method!r}; expected 'unassigned'"
            )
    elif method != "af_assigned":
        raise CandidateError(
            f"resolution row {analysis_id!r} publishes ancestry {published!r} but "
            f"ancestry_assignment_method {method!r}; expected 'af_assigned'"
        )
    if (row.get("source_analysis_id") or "").strip() != analysis_id:
        raise CandidateError(
            f"resolution row {analysis_id!r} source_analysis_id "
            f"{(row.get('source_analysis_id') or '')!r} != analysis_id"
        )
    kind = (row.get("sample_size_kind") or "").strip()
    approved_n = ""
    if manifest_row is not None:
        data_file = (manifest_row.get("data_file") or "").strip()
        if not data_file:
            raise CandidateError(f"manifest row {analysis_id!r} has no data_file")
        if (row.get("source_file") or "").strip() != data_file:
            raise CandidateError(
                f"resolution row {analysis_id!r} source_file "
                f"{(row.get('source_file') or '')!r} != manifest data_file {data_file!r}"
            )
        manifest_assembly = (manifest_row.get("genome_assembly") or "").strip()
        if not manifest_assembly:
            raise CandidateError(f"manifest row {analysis_id!r} has no genome_assembly")
        if (row.get("source_genome_build") or "").strip() != manifest_assembly:
            raise CandidateError(
                f"resolution row {analysis_id!r} source_genome_build "
                f"{(row.get('source_genome_build') or '')!r} != manifest genome_assembly "
                f"{manifest_assembly!r}"
            )
        yaml_file = (manifest_row.get("yaml_file") or "").strip()
        if not yaml_file:
            raise CandidateError(
                f"manifest row {analysis_id!r} has no metadata sidecar (yaml_file)"
            )
        sidecar_path = Path(yaml_file)
        if not sidecar_path.is_file():
            raise CandidateError(f"sidecar not found for {analysis_id!r}: {sidecar_path}")
        sidecar = yaml.safe_load(sidecar_path.read_text(encoding="utf-8"))
        if not isinstance(sidecar, Mapping):
            raise CandidateError(f"sidecar {sidecar_path} is not a YAML mapping")
        sidecar_gwas = str(sidecar.get("gwas_id") or "").strip()
        if sidecar_gwas != analysis_id:
            raise CandidateError(
                f"sidecar {sidecar_path} gwas_id {sidecar_gwas!r} != {analysis_id!r}"
            )
        sidecar_file = str(sidecar.get("data_file_name") or "").strip()
        if sidecar_file != Path(data_file).name:
            raise CandidateError(
                f"sidecar {sidecar_path} data_file_name {sidecar_file!r} != "
                f"{Path(data_file).name!r}"
            )
        sidecar_assembly = str(sidecar.get("genome_assembly") or "").strip()
        if not sidecar_assembly:
            raise CandidateError(f"sidecar {sidecar_path} has no genome_assembly")
        if sidecar_assembly != manifest_assembly or (
            row.get("source_genome_build") or ""
        ).strip() != sidecar_assembly:
            raise CandidateError(
                f"resolution row {analysis_id!r} genome assembly disagreement: source "
                f"{(row.get('source_genome_build') or '')!r}, manifest "
                f"{manifest_assembly!r}, sidecar {sidecar_assembly!r}"
            )
        approved_n = (manifest_row.get("sample_size") or "").strip()
    # sample_size_kind semantics follow the public core contract: a raw total or
    # case/control count is an integer (and matches the approved N); an
    # effective/variant-level size may be a non-integer and the effective size
    # must document its derivation.
    if kind in INTEGER_SAMPLE_SIZE_KINDS:
        _positive_integer(row.get("sample_size"), f"resolution row {analysis_id!r} sample_size")
        if approved_n:
            try:
                matches = int(float(row["sample_size"])) == int(float(approved_n))
            except (TypeError, ValueError) as exc:
                raise CandidateError(
                    f"resolution row {analysis_id!r} has a non-numeric sample size"
                ) from exc
            if not matches:
                raise CandidateError(
                    f"resolution row {analysis_id!r} sample_size {row['sample_size']!r} "
                    f"!= approved N {approved_n!r}"
                )
    elif kind in NON_INTEGER_SAMPLE_SIZE_KINDS:
        _finite_positive(row.get("sample_size"), f"resolution row {analysis_id!r} sample_size")
        if kind == "effective" and not (row.get("sample_size_method") or "").strip():
            raise CandidateError(
                f"resolution row {analysis_id!r} effective sample size requires a "
                "documented sample_size_method"
            )
    else:
        raise CandidateError(
            f"resolution row {analysis_id!r} has unsupported sample_size_kind "
            f"{kind!r}; expected one of {sorted(KNOWN_SAMPLE_SIZE_KINDS)}"
        )
    for column in ("original_sd", "phenotype_sd"):
        if column in row and (row.get(column) or "").strip():
            _finite_positive(row[column], f"resolution row {analysis_id!r} {column}")


def read_planned_manifests(config: Mapping) -> dict[str, int]:
    """Count the planned acquisition-manifest rows from the metadata root.

    Reads the four regular store manifests only (membership/file-path metadata);
    it never opens an association file. Returns ``{filename: rows}``.
    """
    metadata = config.get("metadata") or {}
    root = metadata.get("root")
    if not root:
        raise CandidateError("config has no metadata.root to read planned manifests from")
    manifest_dir = Path(root) / "manifests"
    counts: dict[str, int] = {}
    for store in config["stores"].values():
        filename = store.get("planned_manifest")
        if not filename or filename in counts:
            continue
        path = manifest_dir / filename
        if not path.is_file():
            raise CandidateError(f"planned manifest not found: {path}")
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle, delimiter="\t")
            next(reader, None)
            counts[filename] = sum(1 for _ in reader)
    return counts


def validate_planned_manifests(
    config: Mapping, partition: Mapping[str, Sequence[str]], counts: Mapping[str, int]
) -> None:
    """Cross-check manifest membership against the mapping partition.

    EUR and META share the combined legacy European manifest; the individual
    buckets each have their own. A manifest whose row count disagrees with the
    intended membership fails loudly.
    """
    shared: dict[str, list[str]] = {}
    for ancestry, store in config["stores"].items():
        filename = store.get("planned_manifest")
        if filename:
            shared.setdefault(filename, []).append(ancestry)
    for filename, buckets in shared.items():
        if filename not in counts:
            continue
        expected = sum(len(partition[bucket]) for bucket in buckets)
        if counts[filename] != expected:
            raise CandidateError(
                f"planned manifest {filename} has {counts[filename]} rows; "
                f"expected {expected} for {buckets}"
            )


def _metadata_root(config: Mapping) -> Path:
    root = (config.get("metadata") or {}).get("root")
    if not root:
        raise CandidateError("config has no metadata.root")
    return Path(root)


def _resolve_metadata_path(config: Mapping, value: str | None) -> Path | None:
    """Resolve a named metadata input to an existing regular file, if possible.

    Relative names are tried against this checkout and the primary checkout (the
    gitignored candidate table lives only in the primary). Returns None rather
    than guessing when the file is absent.
    """
    if not value:
        return None
    candidate = Path(value)
    bases = [Path("/")]
    if not candidate.is_absolute():
        bases = [REPO_ROOT, Path("/home/gh13047/repo/opengwasdb-stores")]
    for base in bases:
        resolved = candidate if candidate.is_absolute() else base / candidate
        if resolved.is_file():
            return resolved
    return None


def _require_metadata_file(
    config: Mapping, value: str | None, what: str
) -> Path:
    path = _resolve_metadata_path(config, value)
    if path is None:
        raise CandidateError(f"configured {what} file not found: {value!r}")
    return path


def metadata_fingerprint(
    config: Mapping, mapping_path: Path | str | None = None
) -> dict:
    """Immutable per-file fingerprints of the regular metadata inputs.

    An independent, reproducible binding over the receipt, the manifests, the
    candidate table and the mapping table (regular metadata only, never an
    association file), so an emit can verify the metadata it reads is the set it
    planned against rather than hashing whatever is on disk after the fact.
    """
    metadata = config.get("metadata") or {}
    root = _metadata_root(config)
    files: dict[str, dict[str, object]] = {}

    def add(path: Path | str) -> None:
        candidate = Path(path)
        if not candidate.is_file():
            raise CandidateError(f"metadata file missing: {candidate}")
        files[str(candidate)] = {
            "sha256": sha256_file(candidate),
            "size": candidate.stat().st_size,
        }

    add(root / "parent-run-summary.json")
    for filename in read_manifest_rows(config):
        add(root / "manifests" / filename)
    add(_require_metadata_file(config, metadata.get("candidates_table"), "candidates_table"))
    if mapping_path is not None:
        add(mapping_path)
    digest = hashlib.sha256()
    for key in sorted(files):
        digest.update(key.encode("utf-8"))
        digest.update(str(files[key]["sha256"]).encode("utf-8"))
    return {"files": files, "aggregate_sha256": digest.hexdigest()}


def source_snapshot_summary(config: Mapping) -> dict | None:
    """Record the immutable source-metadata fingerprint snapshot, if present."""
    snapshot_path = _metadata_root(config) / "input-metadata-before.json"
    if not snapshot_path.is_file():
        return None
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if not isinstance(snapshot, dict):
        raise CandidateError("input-metadata-before.json is not a mapping")
    return {
        "snapshot": str(snapshot_path),
        "snapshot_sha256": sha256_file(snapshot_path),
        "total_files": len(snapshot),
    }


def verify_source_fingerprints(config: Mapping) -> dict:
    """Re-hash every source metadata file against the immutable before-snapshot.

    The refresh records ``input-metadata-before.json`` (path -> sha256/size)
    over the regular source metadata. Verifying all entries binds an emit to
    that frozen snapshot rather than to a mutated current tree carrying a
    copied receipt label.
    """
    summary = source_snapshot_summary(config)
    if summary is None:
        raise CandidateError(
            "source metadata fingerprint snapshot input-metadata-before.json not found"
        )
    snapshot = json.loads(Path(summary["snapshot"]).read_text(encoding="utf-8"))
    if not snapshot:
        raise CandidateError("input-metadata-before.json has no entries")
    for path_str, record in snapshot.items():
        path = Path(path_str)
        if not path.is_file():
            raise CandidateError(f"source metadata file missing: {path}")
        digest = (record or {}).get("sha256")
        if not digest:
            raise CandidateError(f"source metadata snapshot entry has no sha256: {path}")
        if sha256_file(path) != digest:
            raise CandidateError(f"source metadata fingerprint mismatch: {path}")
        size = (record or {}).get("size")
        if size is not None and path.stat().st_size != int(size):
            raise CandidateError(f"source metadata size mismatch: {path}")
    return {**summary, "verified_files": len(snapshot)}


def metadata_hashes(config: Mapping, mapping_path: Path | str | None = None) -> dict:
    """Actual hashes of the regular metadata inputs (never an association file)."""
    metadata = config.get("metadata") or {}
    root = _metadata_root(config)
    receipt_path = root / "parent-run-summary.json"
    candidates_path = _require_metadata_file(
        config, metadata.get("candidates_table"), "candidates_table"
    )
    manifests = read_manifest_rows(config)
    computed_content, content_entries = metadata_content.metadata_content_sha256(root)
    result: dict = {
        "metadata_receipt": str(receipt_path),
        "metadata_receipt_sha256": sha256_file(receipt_path),
        "candidates_table": metadata.get("candidates_table"),
        "candidates_table_path": str(candidates_path),
        "candidates_sha256": sha256_file(candidates_path),
        "planned_manifests": {
            filename: sha256_file(root / "manifests" / filename) for filename in manifests
        },
        "metadata_fingerprint": metadata_fingerprint(config, mapping_path),
        "source_snapshot": source_snapshot_summary(config),
        "computed_content_sha256": computed_content,
        "computed_content_entries": content_entries,
    }
    if mapping_path is not None:
        result["mapping_table"] = str(mapping_path)
        result["mapping_sha256"] = sha256_file(mapping_path)
    return result


def verify_metadata_snapshot(config: Mapping) -> dict:
    """Verify the configured snapshot against the actual metadata receipt.

    Every required receipt field must be present with its exact type, the
    declared content sha must be valid, and the parent's own content-hash
    algorithm must reproduce it from the frozen metadata tree. A missing or
    vacuous field is a failure, not a skip.
    """
    metadata = config.get("metadata") or {}
    root = _metadata_root(config)
    receipt_path = root / "parent-run-summary.json"
    if not receipt_path.is_file():
        raise CandidateError(f"metadata receipt not found: {receipt_path}")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    for key in ("code_sha", "output_metadata_content_sha256", "membership"):
        if metadata.get(key) in (None, ""):
            raise CandidateError(f"config metadata.{key} is required")
        if str(metadata[key]) != str(receipt.get(key)):
            raise CandidateError(
                f"config metadata.{key}={metadata[key]!r} != receipt {receipt.get(key)!r}"
            )
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(metadata["code_sha"])):
        raise CandidateError("metadata.code_sha is not a valid hex sha")
    declared = str(metadata["output_metadata_content_sha256"])
    if not re.fullmatch(r"[0-9a-f]{64}", declared):
        raise CandidateError(
            "metadata.output_metadata_content_sha256 is not a valid 64-hex sha"
        )
    # Strict booleans/ints: no int()/bool scalar coercion.
    if receipt.get("promotion") is not False:
        raise CandidateError("metadata receipt must record promotion as the boolean false")
    if receipt.get("store_operations") is not False:
        raise CandidateError(
            "metadata receipt must record store_operations as the boolean false"
        )
    association_opens = receipt.get("association_opens")
    if isinstance(association_opens, bool) or association_opens != 0:
        raise CandidateError(
            "metadata receipt must record association_opens as the integer 0"
        )
    if receipt.get("metadata_idempotent") is not True:
        raise CandidateError(
            "metadata receipt must record metadata_idempotent as the boolean true"
        )
    if receipt.get("source_metadata_unchanged") is not True:
        raise CandidateError(
            "metadata receipt must record source_metadata_unchanged as the boolean true"
        )
    exit_codes = receipt.get("refresh_exit_codes")
    if (
        not isinstance(exit_codes, list)
        or not exit_codes
        or any(
            isinstance(code, bool) or not isinstance(code, int) or code != 0
            for code in exit_codes
        )
    ):
        raise CandidateError(
            "metadata receipt must record all-zero integer refresh_exit_codes"
        )
    if str(receipt.get("output_metadata_content_sha256")) != declared:
        raise CandidateError(
            "metadata receipt content sha != config metadata content sha"
        )
    # Reproduce the parent's exact content hash over the frozen metadata tree.
    actual_sha, entries = metadata_content.metadata_content_sha256(root)
    if actual_sha != declared:
        raise CandidateError(
            f"recomputed metadata content sha {actual_sha} != declared {declared} "
            f"over {entries} entries"
        )
    return receipt


def read_manifest_rows(config: Mapping) -> dict[str, dict[str, dict[str, str]]]:
    """Read the four store manifests and index each by analysis_id (metadata)."""
    manifest_dir = _metadata_root(config) / "manifests"
    files: dict[str, dict[str, dict[str, str]]] = {}
    for store in config["stores"].values():
        filename = store.get("planned_manifest")
        if not filename or filename in files:
            continue
        path = manifest_dir / filename
        if not path.is_file():
            raise CandidateError(f"planned manifest not found: {path}")
        indexed: dict[str, dict[str, str]] = {}
        for row in read_tsv(path):
            analysis_id = (row.get("analysis_id") or "").strip()
            if not analysis_id:
                raise CandidateError(f"manifest {filename} has a blank analysis_id")
            if analysis_id in indexed:
                raise CandidateError(
                    f"manifest {filename} has duplicate analysis_id {analysis_id!r}"
                )
            indexed[analysis_id] = row
        files[filename] = indexed
    return files


def validate_manifest_membership(
    config: Mapping,
    partition: Mapping[str, Sequence[str]],
    manifests: Mapping[str, Mapping[str, Mapping[str, str]]],
) -> None:
    """Exact manifest ID sets, disjoint, and matching the partition.

    A same-count set with a swapped or missing ID must fail; counts alone are
    not membership proof. Each row also needs an ``ok`` status, a 64-hex sha256
    and a non-empty data file and genome assembly.
    """
    expected: dict[str, set[str]] = {}
    for ancestry, store in config["stores"].items():
        filename = store.get("planned_manifest")
        if filename:
            expected.setdefault(filename, set()).update(partition[ancestry])
    union: set[str] = set()
    for filename, rows in manifests.items():
        ids = set(rows)
        if filename in expected and ids != expected[filename]:
            missing = expected[filename] - ids
            extra = ids - expected[filename]
            raise CandidateError(
                f"manifest {filename} ID set does not match the partition "
                f"(missing {len(missing)}, extra {len(extra)})"
            )
        overlap = union & ids
        if overlap:
            raise CandidateError(
                f"manifests overlap on {len(overlap)} analysis_id(s)"
            )
        union |= ids
        for analysis_id, row in rows.items():
            if (row.get("status") or "").strip() != "ok":
                raise CandidateError(
                    f"manifest row {analysis_id!r} status is not 'ok'"
                )
            digest = (row.get("sha256") or "").strip().lower()
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise CandidateError(
                    f"manifest row {analysis_id!r} has an invalid sha256 digest"
                )
            if not (row.get("data_file") or "").strip():
                raise CandidateError(f"manifest row {analysis_id!r} has no data_file")
            if not (row.get("genome_assembly") or "").strip():
                raise CandidateError(
                    f"manifest row {analysis_id!r} has no genome_assembly"
                )
    all_intended = {
        analysis_id for ancestry in partition for analysis_id in partition[ancestry]
    }
    if union != all_intended:
        raise CandidateError(
            f"manifest union has {len(union)} IDs; partition has {len(all_intended)}"
        )


def load_config(path: Path | str) -> dict:
    config_path = Path(path)
    if not config_path.is_file():
        raise CandidateError(f"candidate config not found: {config_path}")
    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise CandidateError(f"candidate config {config_path} is not a YAML mapping")
    if not isinstance(document.get("stores"), dict):
        raise CandidateError(f"candidate config {config_path} has no 'stores' mapping")
    return document


def _index_unique(rows: Sequence[Mapping[str, str]], what: str) -> dict[str, dict[str, str]]:
    indexed: dict[str, dict[str, str]] = {}
    for row in rows:
        analysis_id = (row.get("analysis_id") or "").strip()
        if not analysis_id:
            raise CandidateError(f"{what} has a blank analysis_id")
        if analysis_id in indexed:
            raise CandidateError(f"{what} has duplicate analysis_id {analysis_id!r}")
        indexed[analysis_id] = dict(row)
    return indexed


# ---------------------------------------------------------------------------
# bundle rendering
# ---------------------------------------------------------------------------


def render_release_yaml(
    *,
    store_id: str,
    label: str,
    config: Mapping,
    created_at: str,
    derived_from: str | None,
    completion_state: str,
    notes: str,
    source_snapshot: Mapping | None = None,
    status: str = "candidate",
    accepted_at: str | None = None,
) -> dict:
    release: dict = {
        "store_id": store_id,
        "label": label,
        "access_posture": config.get("access_posture", "public"),
        "status": status,
        "derived_from": derived_from,
        "created_at": created_at,
        "source_snapshot_id": config["source_snapshot_id"],
        "generator": {"commands": list(config.get("generator_commands") or [])},
        "description": (
            f"MVP PMID {config.get('publication_pmid')} {completion_state} bundle "
            f"({label}). Unbuilt; workflow-buildable for dev, scientific acceptance "
            "pending; no Store exists."
            if status == "accepted"
            else (
                f"MVP PMID {config.get('publication_pmid')} {completion_state} "
                f"candidate ({label}). Unbuilt, acceptance-pending; no Store exists."
            )
        ),
        "notes": notes,
    }
    if accepted_at is not None:
        release["accepted_at"] = accepted_at
    if source_snapshot:
        release["source_snapshot"] = dict(source_snapshot)
    return release


def render_build_yaml(
    *,
    store_id: str,
    config: Mapping,
    ancestry: str | None,
    completion: bool,
    ld_panel: str | None,
) -> dict:
    build: dict = {
        "store_id": store_id,
        "layout": config.get("layout", "dense"),
        "completion_state": "reference_completed" if completion else "observed_only",
        "post": {"top_hits": True, "overview": True},
    }
    if completion:
        options: dict = {}
        if ancestry:
            options["ancestry"] = ancestry
        if ld_panel:
            options["ld-panel"] = ld_panel
        build["complete"] = {"command": "complete-dense", "options": options}
    else:
        build["build"] = {
            "command": "build-dense-vcf",
            "options": {
                "source-reader-capability": config.get("source_reader_capability"),
                "source-assembly": config.get("source_assembly"),
            },
        }
    return build


def render_validation_yaml(
    *,
    store_id: str,
    partition_total: int,
    completion_reference: Mapping | None = None,
) -> dict:
    """A candidate Validation Record with human acceptance explicitly pending."""
    acceptance: dict = {
        "status": "pending",
        "human_acceptance": "pending",
        "membership_accepted": False,
        "note": (
            "Phase B candidate only. Membership is not accepted; no Store was "
            "built or validated. Do not relabel this evidence as verified."
        ),
        "partition_total": partition_total,
    }
    if completion_reference is not None:
        acceptance["completion_reference"] = {
            "resource_id": completion_reference.get("resource_id"),
            "declared_status": completion_reference.get("declared_status"),
            "qualification": completion_reference.get("qualification"),
            "activation": completion_reference.get("activation"),
            "blocked": completion_reference.get("activation") != "active",
        }
    return {
        "status": "not_run",
        "validator": {"name": "resources/generators/dbgap-mvp/generate_candidate.py"},
        "acceptance": acceptance,
        "warnings": [
            "Candidate: membership, Analytical Metadata and the Source Inventory are "
            "not yet frozen or accepted.",
            "No Store build or validation has run.",
        ],
        "errors": [],
    }


def render_analyses_tsv(rows: Sequence[Mapping[str, str]], columns: Sequence[str]) -> str:
    import io

    fieldnames = ["analysis_index"] + [column for column in columns if column != "analysis_index"]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    for index, row in enumerate(rows):
        record = {column: row.get(column, "") for column in fieldnames}
        record["analysis_index"] = str(index)
        writer.writerow(record)
    return output.getvalue()
def _atomic_write(
    path: Path, text: str, root: Path, *, overwrite: bool = False
) -> None:
    """Write through the shared containment-checked, exclusive-temp helper."""
    try:
        safe_write.atomic_write_text(path, text, root, overwrite=overwrite)
    except safe_write.SafeWriteError as exc:
        raise CandidateError(str(exc)) from exc


def emit_bundle(
    out_dir: Path,
    store_id: str,
    release: Mapping,
    build: Mapping,
    validation: Mapping,
    analyses_text: str | None,
) -> Path:
    bundle_dir = out_dir / store_id
    if bundle_dir.is_symlink():
        raise CandidateError(f"refusing to write through a symlinked bundle path: {bundle_dir}")
    try:
        bundle_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise CandidateError(f"refusing to overwrite existing bundle: {bundle_dir}") from exc
    _atomic_write(bundle_dir / "release.yaml", yaml.safe_dump(dict(release), sort_keys=False), out_dir)
    _atomic_write(bundle_dir / "build.yaml", yaml.safe_dump(dict(build), sort_keys=False), out_dir)
    _atomic_write(bundle_dir / "validation.yaml", yaml.safe_dump(dict(validation), sort_keys=False), out_dir)
    if analyses_text is not None:
        _atomic_write(bundle_dir / "analyses.tsv", analyses_text, out_dir)
    return bundle_dir


def check_bundle(bundle_dir: Path, registry_root: Path) -> list[str]:
    """Run the registry's bundle contract and Manifest Generation's rules (#250)."""
    bundle = bundle_module.load(bundle_dir.name, registry_root=registry_root)
    return list(generation_rules.check_with_contract(bundle, registry_root=registry_root))


# ---------------------------------------------------------------------------
# plan / emit
# ---------------------------------------------------------------------------


def build_partition(config: Mapping, mapping_path: Path | str):
    rows = partition_module.read_mapping(mapping_path)
    partition = partition_module.partition_mapping(rows)
    # Production config omits `partition_expected` and therefore validates
    # against the intended bound counts. A test fixture may supply its own
    # expected counts, but it cannot silently relax the real ones.
    expected = config.get("partition_expected") or partition_module.EXPECTED_COUNTS
    partition_module.validate_partition(partition, expected=expected)
    return rows, partition


def _reference_ld_panel(config: Mapping, ancestry: str) -> str | None:
    reference = (config["stores"][ancestry] or {}).get("completion_reference") or {}
    if reference.get("status") != "qualified":
        return None
    return reference.get("ld_panel") or None


def run_plan(
    config: Mapping,
    mapping_path: Path | str,
    out_dir: Path,
    manifest_counts: Mapping[str, int] | None = None,
    registry_roots: Sequence[Path | str] = (),
) -> dict:
    _, partition = build_partition(config, mapping_path)
    manifest_counts = manifest_counts or {}
    if manifest_counts:
        validate_planned_manifests(config, partition, manifest_counts)
    verify_metadata_snapshot(config)
    manifests = read_manifest_rows(config)
    validate_manifest_membership(config, partition, manifests)
    assert_not_registry_store_root(out_dir, registry_roots)
    out_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(
        out_dir / "partition.tsv",
        partition_module.render_partition_tsv(partition),
        out_dir,
        overwrite=True,
    )
    plan = {
        "publication_pmid": config.get("publication_pmid"),
        "source_snapshot_id": config.get("source_snapshot_id"),
        "metadata": config.get("metadata"),
        "source_hashes": metadata_hashes(config, mapping_path),
        "planned_manifest_counts": dict(manifest_counts),
        "total": sum(len(partition[ancestry]) for ancestry in partition_module.ANCESTRY_ORDER),
        "legacy_european_bucket": partition_module.legacy_european_count(partition),
        "stores": {},
    }
    for ancestry, store in config["stores"].items():
        planned = store.get("planned_manifest")
        plan["stores"][ancestry] = {
            "candidate_store_key": store.get("candidate_store_key"),
            "observed_store_id": store.get("observed_store_id"),
            "completed_store_id": store.get("completed_store_id"),
            "members": len(partition[ancestry]),
            "observed_only": bool(store.get("observed_only")),
            "completion_reference_status": (
                (store.get("completion_reference") or {}).get("qualification")
            ),
            "planned_manifest": planned,
            "planned_manifest_members": manifest_counts.get(planned) if planned else None,
            "resolution": "pending",
        }
    _atomic_write(
        out_dir / "candidate-plan.yaml",
        yaml.safe_dump(plan, sort_keys=False),
        out_dir,
        overwrite=True,
    )
    return plan


def _suggested_ld_panel(config: Mapping, ancestry: str) -> str | None:
    reference = (config["stores"][ancestry] or {}).get("completion_reference") or {}
    return reference.get("suggested_ld_panel") or None


def render_candidate_definitions(
    config: Mapping,
    manifest_counts: Mapping[str, int] | None = None,
    source_hashes: Mapping | None = None,
) -> dict:
    """The reviewed intended-membership/recipe definitions, tracked (not built)."""
    manifest_counts = manifest_counts or {}
    definitions: dict = {
        "status": "provisional-draft",
        "owner_ordering_approved": bool(config.get("owner_ordering_approved")),
        "publication_pmid": config.get("publication_pmid"),
        "source_snapshot_id": config.get("source_snapshot_id"),
        "intended_bound_total": config.get("intended_bound_total"),
        "metadata": config.get("metadata"),
        "source_hashes": dict(source_hashes or {}),
        "finngen": config.get("finngen"),
        "stores": {},
    }
    for ancestry, store in config["stores"].items():
        observed_id = store["observed_store_id"]
        reference = store.get("completion_reference") or {}
        planned = store.get("planned_manifest")
        observed = {
            "store_id": observed_id,
            "role": "observed",
            "label": store.get("label"),
            "status": "candidate-definition",
            "acceptance": "pending",
            "candidate_store_key": store.get("candidate_store_key"),
            "intended_members": store.get("intended_members"),
            "assigned_ancestry": store.get("assigned_ancestry"),
            "observed_only": bool(store.get("observed_only")),
            "planned_manifest": planned,
            "planned_manifest_members": manifest_counts.get(planned) if planned else None,
            "resolution": "pending",
            "actual_completion": "not_claimed",
            "recipe": {"command": "build-dense-vcf"},
            "in_stores": False,
        }
        completed_id = store.get("completed_store_id")
        completed = None
        if completed_id:
            completed = {
                "store_id": completed_id,
                "role": "completed-definition",
                "label": f"{store.get('label')}-completed",
                "derived_from": observed_id,
                "status": "pending",
                "acceptance": "pending",
                "human_acceptance": "pending",
                "blocked": reference.get("activation") != "active",
                "resolution": "pending",
                "actual_completion": "not_claimed",
                "recipe": {
                    "command": "complete-dense",
                    "ancestry": store.get("assigned_ancestry"),
                },
                "reference": {
                    "resource_id": reference.get("resource_id"),
                    "declared_status": reference.get("declared_status"),
                    "qualification": reference.get("qualification"),
                    "activation": reference.get("activation"),
                    "suggested_ld_panel": reference.get("suggested_ld_panel"),
                },
                "in_stores": False,
            }
        definitions["stores"][ancestry] = {"observed": observed, "completed": completed}
    return definitions


def write_candidate_definitions(
    config: Mapping,
    out_dir: Path,
    manifest_counts: Mapping[str, int] | None = None,
    mapping_path: Path | str | None = None,
    registry_roots: Sequence[Path | str] = (),
) -> Path:
    assert_not_registry_store_root(out_dir, registry_roots)
    source_hashes = metadata_hashes(config, mapping_path)
    path = out_dir / "candidates.yaml"
    _atomic_write(
        path,
        yaml.safe_dump(
            render_candidate_definitions(config, manifest_counts, source_hashes),
            sort_keys=False,
        ),
        out_dir,
        overwrite=True,
    )
    return path


def run_emit(
    config: Mapping,
    mapping_path: Path | str,
    resolution_path: Path | str,
    out_dir: Path,
    created_at: str,
    check: bool = True,
    manifest_counts: Mapping[str, int] | None = None,
    registry_roots: Sequence[Path | str] = (),
    expected_metadata_fingerprint: str | None = None,
    only_buckets: Sequence[str] | None = None,
) -> dict:
    _, partition = build_partition(config, mapping_path)
    stores = dict(config["stores"])
    if only_buckets is not None:
        unknown = sorted(set(only_buckets) - set(stores))
        if unknown:
            raise CandidateError(
                f"unknown bucket(s) {unknown}; configured stores are {sorted(stores)}"
            )
        wanted = set(only_buckets)
        stores = {bucket: store for bucket, store in stores.items() if bucket in wanted}
    resolution_rows = read_tsv(resolution_path)
    resolution_by_id = _index_unique(resolution_rows, "resolution table")
    columns = [column for column in resolution_rows[0].keys() if column != "analysis_index"]
    missing = [column for column in REQUIRED_RESOLUTION_COLUMNS if column not in columns]
    if missing:
        raise CandidateError(
            f"resolution table is missing required column(s): {', '.join(missing)}"
        )

    # Fail closed: every intended member needs a resolution row, and no extra row
    # may sneak into a bundle.
    intended = {
        analysis_id
        for ancestry in stores
        for analysis_id in partition[ancestry]
    }
    extra = set(resolution_by_id) - intended
    if extra:
        raise CandidateError(
            f"resolution table has {len(extra)} row(s) outside the intended membership"
        )

    verify_metadata_snapshot(config)
    manifests = read_manifest_rows(config)
    validate_manifest_membership(config, partition, manifests)
    manifest_index = {
        analysis_id: row for rows in manifests.values() for analysis_id, row in rows.items()
    }

    # Validate every intended row -- including its source-label binding -- before
    # any output directory exists, so an invalid resolution leaves nothing behind.
    member_rows_by_ancestry: dict[str, list[dict[str, str]]] = {}
    for ancestry, store in stores.items():
        member_rows = []
        for analysis_id in partition[ancestry]:
            if analysis_id not in resolution_by_id:
                raise CandidateError(
                    f"no resolution row for intended {ancestry} analysis {analysis_id!r}"
                )
            row = resolution_by_id[analysis_id]
            validate_resolution_row(
                row, ancestry, store.get("assigned_ancestry"), manifest_index.get(analysis_id)
            )
            if ancestry == "META":
                # Emit the empty super-population for cross-ancestry rows: the
                # core contract rejects a non-super-population value, and a
                # single-ancestry label would misdescribe META.
                row = dict(row)
                row["assigned_ancestry"] = ""
            member_rows.append(row)
        member_rows_by_ancestry[ancestry] = member_rows

    assert_not_registry_store_root(out_dir, registry_roots)
    out_dir.mkdir(parents=True, exist_ok=True)
    emitted: dict[str, str] = {}
    checked: list[str] = []
    manifest_counts = manifest_counts or {}
    metadata = config.get("metadata") or {}
    metadata_root = _metadata_root(config)
    resolution_sha = sha256_file(resolution_path)
    mapping_sha = sha256_file(mapping_path)
    receipt_path = metadata_root / "parent-run-summary.json"
    receipt_sha = sha256_file(receipt_path)
    manifest_sha = {
        filename: sha256_file(metadata_root / "manifests" / filename)
        for filename in manifests
    }
    candidates_path = _require_metadata_file(
        config, metadata.get("candidates_table"), "candidates_table"
    )
    candidates_sha = sha256_file(candidates_path)
    fingerprint = metadata_fingerprint(config, mapping_path)
    source_proof = verify_source_fingerprints(config)
    if expected_metadata_fingerprint and (
        expected_metadata_fingerprint != fingerprint["aggregate_sha256"]
    ):
        raise CandidateError(
            "metadata fingerprint does not match the planned value: "
            f"{fingerprint['aggregate_sha256']} != {expected_metadata_fingerprint}"
        )
    for ancestry, store in stores.items():
        member_rows = member_rows_by_ancestry[ancestry]
        analyses_text = render_analyses_tsv(member_rows, columns)

        planned = store.get("planned_manifest")
        source_snapshot = {
            "metadata_root": metadata.get("root"),
            "code_sha": metadata.get("code_sha"),
            "output_metadata_content_sha256": metadata.get("output_metadata_content_sha256"),
            "membership": metadata.get("membership"),
            "association_opens": metadata.get("association_opens"),
            "acceptance": metadata.get("acceptance"),
            "candidates_table": metadata.get("candidates_table"),
            "candidates_table_path": str(candidates_path),
            "candidates_sha256": candidates_sha,
            "mapping_table": str(mapping_path),
            "configured_mapping_table": metadata.get("mapping"),
            "mapping_sha256": mapping_sha,
            "metadata_receipt": str(receipt_path),
            "metadata_receipt_sha256": receipt_sha,
            "metadata_fingerprint_sha256": fingerprint["aggregate_sha256"],
            "metadata_content_sha256_status": "recomputed_matches_receipt",
            "source_metadata_snapshot": source_proof["snapshot"],
            "source_metadata_snapshot_sha256": source_proof["snapshot_sha256"],
            "source_metadata_verified_files": source_proof["verified_files"],
            "resolution_table": str(resolution_path),
            "resolution_sha256": resolution_sha,
            "planned_manifest": planned,
            "planned_manifest_members": manifest_counts.get(planned) if planned else None,
            "planned_manifest_sha256": manifest_sha.get(planned) if planned else None,
            "source_metadata_only": True,
        }

        observed_id = store["observed_store_id"]
        observed_only = bool(store.get("observed_only"))
        notes = (
            f"Ancestry bucket {ancestry}; {len(member_rows)} intended Analyses. "
            f"candidate_store_key {store.get('candidate_store_key')}. "
            + (
                "Cross-ancestry META is observed-only by policy: no derived_from "
                "and no completion step. "
                if observed_only
                else ""
            )
            + "Unbuilt candidate; the human scientific-acceptance step is pending."
            + (
                " Release Status is 'accepted' ONLY so the dev release workflow may "
                "build this bundle (#247); it is not scientific release acceptance, "
                "publication approval or mirror promotion, and no Store exists yet. "
                "The pending human acceptance is recorded in "
                "validation.yaml:acceptance.human_acceptance."
            )
        )
        emit_bundle(
            out_dir,
            observed_id,
            render_release_yaml(
                store_id=observed_id,
                label=store["label"],
                config=config,
                created_at=created_at,
                derived_from=None,
                completion_state="observed-only",
                notes=notes,
                source_snapshot=source_snapshot,
                status="accepted",
                accepted_at=created_at,
            ),
            render_build_yaml(
                store_id=observed_id,
                config=config,
                ancestry=store.get("assigned_ancestry"),
                completion=False,
                ld_panel=None,
            ),
            render_validation_yaml(store_id=observed_id, partition_total=len(intended)),
            analyses_text,
        )
        emitted[ancestry] = observed_id

        completed_id = store.get("completed_store_id")
        if observed_only or not completed_id:
            continue
        reference = store.get("completion_reference") or {}
        suggested = _suggested_ld_panel(config, ancestry)
        # Always record the pending, blocked completion DEFINITION so the intended
        # lineage and recipe are reviewable. Qualification is never inferred from
        # a declared resource or a config label.
        _atomic_write(
            out_dir / f"{completed_id}.completion-plan.yaml",
            yaml.safe_dump(
                {
                    "store_id": completed_id,
                    "derived_from": observed_id,
                    "completion_state": "reference_completed",
                    "candidate_store_key": store.get("candidate_store_key"),
                    "reference": reference,
                    "status": "pending",
                    "blocked": reference.get("activation") != "active",
                    "human_acceptance": "pending",
                    "source_snapshot": source_snapshot,
                    "note": (
                        "Completion definition only. The reference is not qualified or "
                        "activated and the completed Store is not accepted, built or "
                        "registered. Do not execute or promote."
                    ),
                },
                sort_keys=False,
            ),
            out_dir,
        )
        if suggested is None:
            # No suggested panel: the definition above is the honest artifact; do
            # not fabricate a buildable recipe with a missing path.
            continue
        emit_bundle(
            out_dir,
            completed_id,
            render_release_yaml(
                store_id=completed_id,
                label=f"{store['label']}-completed",
                config=config,
                created_at=created_at,
                derived_from=observed_id,
                completion_state="reference-completed",
                notes=(
                    f"Lineage-linked completed definition for {observed_id} ({ancestry}); "
                    f"declared reference {reference.get('resource_id')}, qualification "
                    f"{reference.get('qualification')!r}, activation {reference.get('activation')!r}. "
                    "Unbuilt, acceptance-pending; not qualified or approved for execution."
                ),
                source_snapshot=source_snapshot,
            ),
            render_build_yaml(
                store_id=completed_id,
                config=config,
                ancestry=store.get("assigned_ancestry"),
                completion=True,
                ld_panel=suggested,
            ),
            render_validation_yaml(
                store_id=completed_id,
                partition_total=len(intended),
                completion_reference=reference,
            ),
            analyses_text,
        )
        emitted[f"{ancestry}-completed"] = completed_id

    if check:
        for store_id in emitted.values():
            errors = check_bundle(out_dir / store_id, out_dir)
            if errors:
                raise CandidateError(
                    f"emitted bundle {store_id} fails bundle.check(): "
                    + "; ".join(errors)
                )
            checked.append(store_id)

    # #247: the release workflow builds only `BUILDABLE_STATUSES`. Every observed
    # Store is emitted `accepted` so `partition_buildable` keeps it; the completion
    # children stay `candidate` while their reference is pending/blocked, so the
    # workflow must refuse them rather than schedule them silently.
    observed_bundles = [
        bundle_module.load(emitted[ancestry], registry_root=out_dir)
        for ancestry in stores
        if ancestry in emitted
    ]
    bundle_module.refuse_unbuildable(observed_bundles)
    for key, store_id in emitted.items():
        if key.endswith("-completed") and bundle_module.is_buildable(
            bundle_module.load(store_id, registry_root=out_dir)
        ):
            raise CandidateError(
                f"completed definition {store_id} must not be buildable while its "
                "reference is pending/blocked"
            )

    _atomic_write(
        out_dir / "candidate-plan.yaml",
        yaml.safe_dump(
            {
                "publication_pmid": config.get("publication_pmid"),
                "source_snapshot_id": config.get("source_snapshot_id"),
                "emitted": emitted,
                "checked": checked,
                "acceptance": "pending",
            },
            sort_keys=False,
        ),
        out_dir,
        overwrite=True,
    )
    _atomic_write(
        out_dir / "partition.tsv",
        partition_module.render_partition_tsv(partition),
        out_dir,
        overwrite=True,
    )
    return {"emitted": emitted, "checked": checked}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "emit", "definitions"))
    parser.add_argument("--config", type=Path, default=REPO_ROOT / DEFAULT_CONFIG)
    parser.add_argument("--mapping", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--resolution", type=Path, default=None)
    parser.add_argument(
        "--created-at",
        default=None,
        help="ISO timestamp for emitted release.yaml files (defaults to now)",
    )
    parser.add_argument("--no-check", action="store_true", help="skip bundle.check()")
    parser.add_argument(
        "--expected-metadata-fingerprint",
        default=None,
        help="refuse emit unless the metadata fingerprint matches this value",
    )
    parser.add_argument(
        "--buckets",
        nargs="+",
        default=None,
        help="emit only the named ancestry buckets (default: every configured store)",
    )
    parser.add_argument(
        "--registry-root",
        action="append",
        default=[],
        type=Path,
        help="additional registry stores/ root to refuse as an output (repeatable)",
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)
    created_at = args.created_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    registry_roots = tuple(args.registry_root or ())
    manifest_counts: dict[str, int] = {}
    metadata = config.get("metadata") or {}
    if metadata.get("root") and Path(metadata["root"]).is_dir():
        manifest_counts = read_planned_manifests(config)

    mapping_for_hash = args.mapping or _resolve_metadata_path(config, metadata.get("mapping"))
    if args.mode == "definitions":
        path = write_candidate_definitions(
            config,
            args.out_dir,
            manifest_counts,
            mapping_path=mapping_for_hash,
            registry_roots=registry_roots,
        )
        print(json.dumps({"definitions": str(path)}, indent=2))
        return 0

    if args.mapping is None:
        raise CandidateError(f"{args.mode} mode requires --mapping")
    if args.mode == "plan":
        plan = run_plan(
            config, args.mapping, args.out_dir, manifest_counts, registry_roots
        )
        print(json.dumps(plan, indent=2))
        return 0

    if args.resolution is None:
        raise CandidateError("emit mode requires --resolution; use plan for metadata-only")
    result = run_emit(
        config,
        args.mapping,
        args.resolution,
        args.out_dir,
        created_at=created_at,
        check=not args.no_check,
        manifest_counts=manifest_counts,
        registry_roots=registry_roots,
        expected_metadata_fingerprint=args.expected_metadata_fingerprint,
        only_buckets=args.buckets,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
