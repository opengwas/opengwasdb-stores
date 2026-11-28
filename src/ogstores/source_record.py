"""Write an increment's `source-record.json`: the owner-attested facts SOURCE did not record (ADR 0045 §6).

A Store built before the engine recorded its rsID policy, its rows' effective
assembly, and the digests of its Variant Reference, reference panel and LD
panel cannot pin an Increment to itself. `splice-release --source-record`
takes those facts instead (opengwasdb ADR 0109 §3, §9), and the registry
writes them here, from the published release's own records, into the open
Increment Bundle, where the owner's acceptance digest binds them.

    python3 -m ogstores.source_record OGS-00011 --increment 1 \\
        --attested-by "<owner>" --variant-reference-sha256 <hex> --ld-panel-digest <digest> \\
        [--reference-panel-sha256 <hex>] [--registry-root stores] [--artifact-root <root>]

What it reads, and what it refuses:

- `rsid_conflict_policy` is `records/build.json`'s `--on-rsid-conflict`, which
  must equal the Validation Record's `observed.rsid_conflicts.policy`;
- `source_assembly` is the effective assembly of every admitted row of the
  published `work/analyses.tsv`: the row's `source_assembly` where the column
  is present, otherwise the build's `--source-assembly`. One assembly for
  every row is required, and it must agree with each row's
  `source_genome_build`, which must be present and not blank. A default flag
  alone is never a per-row record;
- `reference_panel_sha256` is an explicit null when the build took no
  `--reference-panel`; a build that took one needs the owner's attested digest;
- `indexed_subsets` are, for each subset `build.yaml` declares, its
  `completion-indexed-subset-<name>.json`'s (or, for a Store that is not
  completed, `indexed-subset-<name>.json`'s) `--variant-list` and
  `--overflow-only`; a declared subset without its record refuses;
- `variant_reference_sha256` and `ld_panel_digest` are the owner's attested
  values, given on the command line. A path, an mtime or a digest taken now
  does not show what the build read, so this never computes them; it records
  the recorded paths beside them in `evidence`;
- `attested_by` is required.

It writes only into a candidate increment: an accepted one's digest would
break. It opens no Store (ADR 0023).
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ogstores import bundle as bundle_module
from ogstores import increment, paths, register, run

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
#: `source_genome_build` spellings, and the assembly each names.
GENOME_BUILD_ASSEMBLIES: dict[str, str] = {
    "grch38": "hg38", "hg38": "hg38", "grch37": "hg19", "hg19": "hg19",
}
COMPLETION_SUBSET_PREFIX: str = "completion-indexed-subset-"
BUILD_SUBSET_PREFIX: str = "indexed-subset-"


class SourceRecordError(RuntimeError):
    """The attestation cannot be written; nothing was."""


def _flag(argv: Sequence[str], flag: str) -> str | None:
    """The value of `flag` in an argv, in either spelling; None when absent."""
    for index, token in enumerate(argv):
        if token == flag and index + 1 < len(argv):
            return str(argv[index + 1])
        if token.startswith(f"{flag}="):
            return token.split("=", 1)[1]
    return None


def _record(store_id: str, step: str, root: Path) -> dict[str, Any]:
    path = paths.record_path(store_id, step, root=root)
    try:
        record = run.read_record(path)
    except ValueError as exc:
        raise SourceRecordError(str(exc)) from exc
    if record is None or not isinstance(record.get("argv"), list):
        raise SourceRecordError(f"{store_id}: no step record with an argv at {path}")
    return record


def _effective_assembly(store_id: str, root: Path, default: str | None) -> str:
    path = paths.build_manifest_path(store_id, root=root)
    if not path.is_file():
        raise SourceRecordError(f"{store_id}: no published build manifest at {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t", quoting=csv.QUOTE_NONE))
    assemblies: dict[str, list[str]] = {}
    for row in rows:
        effective = (row.get("source_assembly") or "").strip() or default
        if not effective:
            raise SourceRecordError(
                f"{store_id}: row {row.get('analysis_id')!r} has no source_assembly and the build gave no "
                "--source-assembly, so its effective assembly is unknown"
            )
        declared = (row.get("source_genome_build") or "").strip()
        if not declared:
            raise SourceRecordError(
                f"{store_id}: row {row.get('analysis_id')!r} has a blank source_genome_build, so its effective "
                f"assembly {effective!r} cannot be confirmed"
            )
        named = GENOME_BUILD_ASSEMBLIES.get(declared.lower())
        if named != effective.lower():
            raise SourceRecordError(
                f"{store_id}: row {row.get('analysis_id')!r} has effective assembly {effective!r} but "
                f"source_genome_build {declared!r}"
            )
        assemblies.setdefault(effective, []).append(str(row.get("analysis_id")))
    if len(assemblies) != 1:
        shown = {k: v[:3] for k, v in assemblies.items()}
        raise SourceRecordError(f"{store_id}: the published rows' effective assemblies differ: {shown}")
    return next(iter(assemblies))


def _indexed_subsets(b: bundle_module.Bundle, root: Path) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    prefix = COMPLETION_SUBSET_PREFIX if b.completes_own_store else BUILD_SUBSET_PREFIX
    declared = b.build.get(bundle_module.INDEXED_SUBSETS_KEY) or []
    names = [d.get("name") for d in declared if isinstance(d, dict)]
    subsets: dict[str, dict[str, str]] = {}
    evidence: dict[str, str] = {}
    for name in names:
        step = f"{prefix}{name}"
        path = paths.record_path(b.store_id, step, root=root)
        record = _record(b.store_id, step, root)
        variant_list = _flag(record["argv"], "--variant-list")
        policy = _flag(record["argv"], "--overflow-only")
        if not variant_list or not policy:
            raise SourceRecordError(f"{b.store_id}: {path.name} names no --variant-list or --overflow-only")
        subsets[name] = {"variant_list_path": variant_list, "overflow_only": policy}
        evidence[name] = f"records/{path.name} argv --variant-list, --overflow-only"
    return subsets, evidence


def build_source_record(
    store_id: str,
    number: int,
    *,
    registry_root: Path | str,
    artifact_root: Path | str,
    attested_by: str | None,
    variant_reference_sha256: str | None,
    ld_panel_digest: str | None,
    reference_panel_sha256: str | None = None,
) -> dict[str, Any]:
    """The attestation for increment `number`, from the records and the owner's values; raises to refuse."""
    root = Path(artifact_root)
    if not attested_by or not attested_by.strip():
        raise SourceRecordError("--attested-by is required: an unattested fact is not a pin")
    if not variant_reference_sha256 or not _SHA256_RE.fullmatch(variant_reference_sha256):
        raise SourceRecordError(
            "--variant-reference-sha256 must be the owner's attested sha256 (64 lowercase hex) of the "
            "Variant Reference the build read"
        )
    if not ld_panel_digest or not ld_panel_digest.strip():
        raise SourceRecordError(
            "--ld-panel-digest is required: the owner attests `opengwasdb ld-panel-digest PATH --ancestry …` "
            "for the panel completion read (opengwasdb#522)"
        )
    b = bundle_module.load(store_id, registry_root=registry_root)
    inc = increment.get(b, number)
    if inc.status != "candidate":
        raise SourceRecordError(
            f"{store_id}: increment {increment.directory_name(number)} is {inc.status!r}; the attestation is "
            "written only into a candidate, before the owner's acceptance digest binds it"
        )

    build = _record(store_id, "build", root)
    argv = build["argv"]
    policy = _flag(argv, "--on-rsid-conflict")
    observed = ((b.validation or {}).get("observed") or {}) if isinstance(b.validation, dict) else {}
    recorded_policy = (observed.get("rsid_conflicts") or {}).get("policy") if isinstance(observed, dict) else None
    if not policy or not recorded_policy:
        raise SourceRecordError(
            f"{store_id}: the rsID policy is not recorded twice: build argv --on-rsid-conflict {policy!r}, "
            f"validation.yaml observed.rsid_conflicts.policy {recorded_policy!r}"
        )
    if policy != recorded_policy:
        raise SourceRecordError(
            f"{store_id}: the rsID policy disagrees: build argv {policy!r}, validation.yaml {recorded_policy!r}"
        )
    default_assembly = _flag(argv, "--source-assembly")
    assembly = _effective_assembly(store_id, root, default_assembly)

    panel = _flag(argv, "--reference-panel")
    if panel is not None and (not reference_panel_sha256 or not _SHA256_RE.fullmatch(reference_panel_sha256)):
        raise SourceRecordError(
            f"{store_id}: the build took --reference-panel {panel}; give the owner's attested "
            "--reference-panel-sha256"
        )
    if panel is None and reference_panel_sha256:
        raise SourceRecordError(f"{store_id}: the build took no --reference-panel, so its digest is null")

    evidence: dict[str, Any] = {
        "rsid_conflict_policy": "records/build.json argv --on-rsid-conflict; validation.yaml "
                                "observed.rsid_conflicts.policy",
        "source_assembly": "effective assembly of every admitted row of work/analyses.tsv (its source_assembly, "
                           f"else build argv --source-assembly {default_assembly!r}), checked against "
                           "source_genome_build",
        "variant_reference_sha256": "owner attestation, for build argv --variant-reference "
                                    f"{_flag(argv, '--variant-reference')!r}",
        "reference_panel_sha256": (
            "build argv has no --reference-panel" if panel is None
            else f"owner attestation, for build argv --reference-panel {panel!r}"
        ),
    }
    if b.completes_own_store:
        complete = _record(store_id, "complete", root)
        evidence["ld_panel_digest"] = (
            f"owner attestation, for complete argv --ld-panel {_flag(complete['argv'], '--ld-panel')!r} "
            f"--ancestry {_flag(complete['argv'], '--ancestry')!r}"
        )
    else:
        evidence["ld_panel_digest"] = "owner attestation"
    subsets, subset_evidence = _indexed_subsets(b, root)
    evidence["indexed_subsets"] = subset_evidence
    return {
        "rsid_conflict_policy": policy,
        "source_assembly": assembly,
        "variant_reference_sha256": variant_reference_sha256,
        "reference_panel_sha256": reference_panel_sha256 if panel is not None else None,
        "ld_panel_digest": ld_panel_digest,
        "indexed_subsets": subsets,
        "attested_by": attested_by,
        "evidence": evidence,
    }


def write_source_record(store_id: str, number: int, *, registry_root: Path | str, **kwargs: Any) -> Path:
    """Write the attestation into increment `number`, atomically, and return its path."""
    record = build_source_record(store_id, number, registry_root=registry_root, **kwargs)
    b = bundle_module.load(store_id, registry_root=registry_root)
    target = increment.get(b, number).root / increment.SOURCE_RECORD_FILE
    register.write_text_atomically(json.dumps(record, indent=2, sort_keys=True) + "\n", target)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("store_id")
    parser.add_argument("--increment", type=int, required=True, help="the open increment's number")
    parser.add_argument("--attested-by", help="the owner who attests these facts")
    parser.add_argument("--variant-reference-sha256", help="the attested sha256 of the Variant Reference read")
    parser.add_argument("--ld-panel-digest", help="the attested digest of the LD panel completion read")
    parser.add_argument("--reference-panel-sha256", help="the attested sha256, when the build took one")
    parser.add_argument("--registry-root", default=str(bundle_module.REPO_ROOT / "stores"))
    parser.add_argument("--artifact-root", help="default: the deployment's Artifact Root")
    args = parser.parse_args(argv)
    try:
        written = write_source_record(
            args.store_id, args.increment,
            registry_root=args.registry_root,
            artifact_root=args.artifact_root or paths.artifact_root(),
            attested_by=args.attested_by,
            variant_reference_sha256=args.variant_reference_sha256,
            ld_panel_digest=args.ld_panel_digest,
            reference_panel_sha256=args.reference_panel_sha256,
        )
    except (SourceRecordError, increment.IncrementError) as exc:
        print(f"source-record: {exc}; nothing was written", file=sys.stderr)
        return 1
    print(f"source-record: wrote {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
