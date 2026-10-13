#!/usr/bin/env python3
"""Emit unbuilt, acceptance-pending MVP ancestry and META candidates (#221).

This is Phase B tooling: it turns the metadata-only MVP partition into candidate
Release Bundles for the single-ancestry Stores and the cross-ancestry META
Store. It is deliberately conservative.

* It emits ``status: candidate`` bundles with no ``accepted_at`` and a
  ``validation.yaml`` ``acceptance`` block whose human-acceptance field stays
  ``pending``. Nothing here accepts, registers, builds, completes or publishes a
  Store, and no existing bundle is overwritten.
* The ancestry partition is validated against the accepted immutable counts and
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
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
for _extra in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if _extra not in sys.path:
        sys.path.insert(0, _extra)

# The partition module lives beside this script in a hyphenated directory that is
# not importable as a package; load it by path.
_spec = importlib.util.spec_from_file_location(
    "dbgap_mvp_partition", Path(__file__).with_name("partition.py")
)
assert _spec and _spec.loader
partition_module = importlib.util.module_from_spec(_spec)
sys.modules["dbgap_mvp_partition"] = partition_module
_spec.loader.exec_module(partition_module)

from ogstores import bundle as bundle_module  # noqa: E402

DEFAULT_CONFIG = "resources/generators/dbgap-mvp/config-candidate.yaml"

#: The columns a resolution table must carry for a bundle to pass the registry
#: and shared-core Analysis contract.
REQUIRED_RESOLUTION_COLUMNS: tuple[str, ...] = (
    "analysis_id",
    "assigned_ancestry",
    "ancestry_assignment_method",
    "stored_effect_scale",
    "sample_size_kind",
    "sample_size_scope",
    "sample_size",
    "original_effect_scale",
    "original_sd_method",
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
) -> dict:
    release: dict = {
        "store_id": store_id,
        "label": label,
        "access_posture": config.get("access_posture", "public"),
        "status": "candidate",
        "derived_from": derived_from,
        "created_at": created_at,
        "source_snapshot_id": config["source_snapshot_id"],
        "generator": {"commands": list(config.get("generator_commands") or [])},
        "description": (
            f"MVP PMID {config.get('publication_pmid')} {completion_state} candidate "
            f"({label}). Unbuilt, acceptance-pending; no Store exists."
        ),
        "notes": notes,
    }
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
        if not ld_panel:
            raise CandidateError(
                f"completed Store {store_id} has no resolved ld-panel path; refusing "
                "to emit a completion bundle from an unqualified reference"
            )
        build["complete"] = {
            "command": "complete-dense",
            "options": {"ld-panel": ld_panel, "ancestry": ancestry},
        }
    else:
        build["build"] = {
            "command": "build-dense-vcf",
            "options": {
                "source-reader-capability": config.get("source_reader_capability"),
                "source-assembly": config.get("source_assembly"),
            },
        }
    return build


def render_validation_yaml(*, store_id: str, partition_total: int) -> dict:
    """A candidate Validation Record with human acceptance explicitly pending."""
    return {
        "status": "not_run",
        "validator": {"name": "resources/generators/dbgap-mvp/generate_candidate.py"},
        "acceptance": {
            "status": "pending",
            "human_acceptance": "pending",
            "membership_accepted": False,
            "note": (
                "Phase B candidate only. Membership is not accepted; no Store was "
                "built or validated. Do not relabel this evidence as verified."
            ),
            "partition_total": partition_total,
        },
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


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def emit_bundle(
    out_dir: Path,
    store_id: str,
    release: Mapping,
    build: Mapping,
    validation: Mapping,
    analyses_text: str | None,
) -> Path:
    bundle_dir = out_dir / store_id
    if bundle_dir.exists():
        raise CandidateError(f"refusing to overwrite existing bundle: {bundle_dir}")
    _atomic_write(bundle_dir / "release.yaml", yaml.safe_dump(dict(release), sort_keys=False))
    _atomic_write(bundle_dir / "build.yaml", yaml.safe_dump(dict(build), sort_keys=False))
    _atomic_write(bundle_dir / "validation.yaml", yaml.safe_dump(dict(validation), sort_keys=False))
    if analyses_text is not None:
        _atomic_write(bundle_dir / "analyses.tsv", analyses_text)
    return bundle_dir


def check_bundle(bundle_dir: Path, registry_root: Path) -> list[str]:
    """Run the registry's executable bundle contract and return its errors."""
    bundle = bundle_module.load(bundle_dir.name, registry_root=registry_root)
    return list(bundle_module.check(bundle, registry_root=registry_root))


# ---------------------------------------------------------------------------
# plan / emit
# ---------------------------------------------------------------------------


def build_partition(config: Mapping, mapping_path: Path | str):
    rows = partition_module.read_mapping(mapping_path)
    partition = partition_module.partition_mapping(rows)
    # Production config omits `partition_expected` and therefore validates
    # against the accepted immutable counts. A test fixture may supply its own
    # expected counts, but it cannot silently relax the real ones.
    expected = config.get("partition_expected") or partition_module.EXPECTED_COUNTS
    partition_module.validate_partition(partition, expected=expected)
    return rows, partition


def _reference_ld_panel(config: Mapping, ancestry: str) -> str | None:
    reference = (config["stores"][ancestry] or {}).get("completion_reference") or {}
    if reference.get("status") != "qualified":
        return None
    return reference.get("ld_panel") or None


def run_plan(config: Mapping, mapping_path: Path | str, out_dir: Path) -> dict:
    _, partition = build_partition(config, mapping_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(
        out_dir / "partition.tsv", partition_module.render_partition_tsv(partition)
    )
    plan = {
        "publication_pmid": config.get("publication_pmid"),
        "source_snapshot_id": config.get("source_snapshot_id"),
        "total": sum(len(partition[ancestry]) for ancestry in partition_module.ANCESTRY_ORDER),
        "legacy_european_bucket": partition_module.legacy_european_count(partition),
        "stores": {},
    }
    for ancestry, store in config["stores"].items():
        plan["stores"][ancestry] = {
            "candidate_store_key": store.get("candidate_store_key"),
            "observed_store_id": store.get("observed_store_id"),
            "completed_store_id": store.get("completed_store_id"),
            "members": len(partition[ancestry]),
            "observed_only": bool(store.get("observed_only")),
            "completion_reference_status": (
                (store.get("completion_reference") or {}).get("status")
            ),
        }
    _atomic_write(out_dir / "candidate-plan.yaml", yaml.safe_dump(plan, sort_keys=False))
    return plan


def run_emit(
    config: Mapping,
    mapping_path: Path | str,
    resolution_path: Path | str,
    out_dir: Path,
    created_at: str,
    check: bool = True,
) -> dict:
    _, partition = build_partition(config, mapping_path)
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
        for ancestry in partition_module.ANCESTRY_ORDER
        for analysis_id in partition[ancestry]
    }
    extra = set(resolution_by_id) - intended
    if extra:
        raise CandidateError(
            f"resolution table has {len(extra)} row(s) outside the intended membership"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    emitted: dict[str, str] = {}
    checked: list[str] = []
    for ancestry, store in config["stores"].items():
        members = partition[ancestry]
        member_rows = []
        for analysis_id in members:
            if analysis_id not in resolution_by_id:
                raise CandidateError(
                    f"no resolution row for intended {ancestry} analysis {analysis_id!r}"
                )
            member_rows.append(resolution_by_id[analysis_id])
        analyses_text = render_analyses_tsv(member_rows, columns)

        observed_id = store["observed_store_id"]
        observed_only = bool(store.get("observed_only"))
        notes = (
            f"Ancestry bucket {ancestry}; {len(members)} admitted Analyses. "
            f"candidate_store_key {store.get('candidate_store_key')}. "
            + (
                "Cross-ancestry META is observed-only by policy: no derived_from "
                "and no completion step. "
                if observed_only
                else ""
            )
            + "Unbuilt, acceptance-pending candidate; the human acceptance step is pending."
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
        ld_panel = _reference_ld_panel(config, ancestry)
        if ld_panel is None:
            # Reference not qualified: record a plan, do not emit a bundle.
            _atomic_write(
                out_dir / f"{completed_id}.completion-plan.yaml",
                yaml.safe_dump(
                    {
                        "store_id": completed_id,
                        "derived_from": observed_id,
                        "completion_state": "reference_completed",
                        "candidate_store_key": store.get("candidate_store_key"),
                        "reference": store.get("completion_reference"),
                        "status": "hold",
                        "note": (
                            "No qualified completion reference; the completed child is "
                            "a recorded plan only and must not be executed or registered."
                        ),
                    },
                    sort_keys=False,
                ),
            )
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
                    f"Lineage-linked completed candidate for {observed_id} "
                    f"({ancestry}); reference {(store.get('completion_reference') or {}).get('resource_id')}. "
                    "Unbuilt, acceptance-pending; unqualified references never reach here."
                ),
            ),
            render_build_yaml(
                store_id=completed_id,
                config=config,
                ancestry=store.get("assigned_ancestry"),
                completion=True,
                ld_panel=ld_panel,
            ),
            render_validation_yaml(store_id=completed_id, partition_total=len(intended)),
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
    )
    _atomic_write(out_dir / "partition.tsv", partition_module.render_partition_tsv(partition))
    return {"emitted": emitted, "checked": checked}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "emit"))
    parser.add_argument("--config", type=Path, default=REPO_ROOT / DEFAULT_CONFIG)
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--resolution", type=Path, default=None)
    parser.add_argument(
        "--created-at",
        default=None,
        help="ISO timestamp for emitted release.yaml files (defaults to now)",
    )
    parser.add_argument("--no-check", action="store_true", help="skip bundle.check()")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    created_at = args.created_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    if args.mode == "plan":
        plan = run_plan(config, args.mapping, args.out_dir)
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
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
