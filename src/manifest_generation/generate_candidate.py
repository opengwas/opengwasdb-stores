#!/usr/bin/env python3
"""The Manifest Generation entry point: one release config -> one candidate Release Bundle.

One command runs the whole shared pipeline (ADR 0036, #287) for any release
config (``docs/spec/release-config.md``), and never invokes Store Release Build
or builds a Store:

    pixi run generate-candidate OGS-00011 \\
      --config resources/generators/gwas-catalog-eur-hybrid/release-eur-hybrid-full.yaml \\
      --cores 64

Stages, in order (``--stage``, default ``all``):

    freeze      freeze the Source Inventory from the Source Collection's connector
                files for the Selection (#283), or prove the frozen one is this
                release's; a frozen inventory is never overwritten
    preflight   prove the frozen inventory before any association row is read (#151)
    prepare     derive the canonical resolver manifest from the frozen exact paths
    resolve     invoke `opengwasdb resolve-analyses` (owns the pool and checkpoints)
                into the Source Collection's Resolution Area, reusing every record
                whose fingerprint still matches; keep the records, write the receipt
    verify      account every record the receipt names; fail on missing/stale/edited
    render      Trait Ontology Mapping (#286), then the release policy -- Quality
                Checks (#285), the Assigned Ancestry check and the Store
                Composition Rule -- then render the bundle into the Work Root
    emit        check the rendered bundle and emit it (manifest_generation.emit)

``all`` runs every stage. A later stage run on its own repeats the in-memory
steps it needs (``prepare``, ``verify``, ``render``) deterministically, and
never ``freeze``, ``preflight`` or ``resolve``.

Where the files go (#284):

    <inventory.directory>/<snapshot-id>.*   the frozen Source Inventory and its connectors
    <work-root>/<store-id>/                 this Store Release's Work Root
      freeze/<snapshot-id>.json             what the freeze stage found or wrote
      preflight/<snapshot-id>.json
      resolver/analyses.tsv                 this Selection's resolver manifest
      resolver/resolve.log
      resolver/index.json                   the resolver's index for this run
      resolver/resolution_receipt.json      names the records the bundle is decided from
      bundle/                               the rendered candidate, before emit
    <resolution-root>/<source-collection-id>/   the Resolution Area, shared by every Selection
      .lock                                 held by the resolving run and its resolver
      checkpoint/<settings-digest>/         the resolver's records directory, per settings
      records/<analysis>/<digest>/<sha256>.json   kept Resolution Records, write-once

**Resume.** Re-running a stopped run repeats nothing costly: freeze finds the
frozen inventory, and resolution reuses each record whose fingerprint still
matches; there is no `--resume` to pass. The resolution root is deployment
configuration: `--resolution-root`, else OPENGWASDB_RESOLUTION_ROOT, else
ogstores.yaml's `resolution_root`, else /data/opengwasdb/work/resolution.

The registry owns membership, method tiers, thresholds and exclusions;
``opengwasdb`` owns the statistics.

Usage:
    pixi run generate-candidate OGS-00011 --config <release config> --cores 64
    pixi run generate-candidate OGS-00011 --config <release config> --stage render
    python3 -m manifest_generation.generate_candidate OGS-00011 --config <release config> ...
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from manifest_generation import REPO_ROOT
from manifest_generation.candidate_workflow import (
    DEFAULT_RESOLVER_BIN,
    RECEIPT_FILENAME,
    CandidateConfiguration,
    CandidateError,
    CandidateTables,
    account_records,
    acquisition_evidence,
    apply_release_policy,
    build_candidate_tables,
    build_resolution_receipt,
    derive_resolver_manifest,
    load_candidate_configuration,
    map_candidate_trait_ontologies,
    now_utc,
    read_analysis_metadata,
    read_resolution_receipt,
    render_build_yaml,
    render_column_maps,
    render_release_yaml,
    render_resolver_manifest,
    render_validation_yaml,
    resolver_argv,
    resolver_settings,
    resolver_settings_digest,
    run_resolver,
    sha256_file,
    sha256_text,
    validate_candidate_analyses,
    verify_records,
    write_resolution_receipt,
)
from manifest_generation.emit import CandidateBundle, EmitError, emit, render_yaml
from manifest_generation.inventory import freeze_release
from manifest_generation.resolution_area import (
    DEFAULT_RESOLUTION_ROOT,
    RESOLUTION_ROOT_ENV_VAR,
    ResolutionArea,
    ResolutionAreaError,
    resolution_root,
)
from manifest_generation.safe_write import atomic_write_bytes
from manifest_generation.trait_ontology_mapping import (
    TraitOntologyMappingError,
    load_canonical_trait_mapping_table,
)
from manifest_generation.source_inventory import (
    InventoryError,
    PreflightConfigError,
    preflight,
    read_inventory,
    render_preflight_summary,
)
from ogstores.paths import require_valid_store_id

#: The module this entry point runs as, recorded in the bundle's command log.
#: `__spec__.name`, not `__name__`, which is "__main__" under `python3 -m`.
MODULE = __spec__.name
#: The stages, in pipeline order; ``all`` runs every one.
STAGES = ("freeze", "preflight", "prepare", "resolve", "verify", "render", "emit")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("store_id", help="Candidate Store Release id, e.g. OGS-00011")
    parser.add_argument("--config", required=True, help="the release config")
    parser.add_argument("--cores", type=int, help="worker count (default: config runtime.cores)")
    # Refused, not ignored: an operator who passes it expects it to mean something.
    parser.add_argument("--resume", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--stage", choices=STAGES + ("all",), default="all")
    # Verification-only (docs/spec/store-release-workflow.md): run another
    # resolver executable, such as the one a past run's records were resolved
    # with (the OGS-00011 regeneration, #287). The receipt records it, and emit
    # refuses such a receipt into the repository's own registry.
    parser.add_argument("--resolver", default=DEFAULT_RESOLVER_BIN, help=argparse.SUPPRESS)
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument("--registry-root", help="registry stores/ directory (default: <repo>/stores)")
    parser.add_argument("--work-root", help="run manifest/logs/receipt root (default: config output.work_root)")
    parser.add_argument(
        "--resolution-root",
        help="root of the per-Source-Collection Resolution Areas (default: "
        f"{RESOLUTION_ROOT_ENV_VAR}, else ogstores.yaml resolution_root, else "
        f"{DEFAULT_RESOLUTION_ROOT})",
    )
    parser.add_argument("--created-at", help=argparse.SUPPRESS)
    parser.add_argument("--frozen-at", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.resolver != DEFAULT_RESOLVER_BIN:
        # Resolved once, here, against the caller's working directory: the
        # resolve stage runs it from the repository root, and the receipt
        # records it, so both must be this one absolute path.
        found = shutil.which(args.resolver) if os.sep not in args.resolver else args.resolver
        if found is None or not Path(found).is_file():
            parser.error(f"--resolver {args.resolver!r} is not an executable file")
        args.resolver = str(Path(found).resolve())
    if args.resume:
        parser.error(
            "--resume is no longer an option: reuse is automatic (#284). Every run reuses "
            "each Resolution Record whose fingerprint still matches; drop the flag"
        )
    return args


class Pipeline:
    """Shared state for one candidate run across the stages."""

    def __init__(
        self, args: argparse.Namespace, repo_root: Path, operator_command: str
    ) -> None:
        self.args = args
        self.repo_root = repo_root
        self.store_id = require_valid_store_id(args.store_id)
        self.config: CandidateConfiguration = load_candidate_configuration(
            self._config_path(), repo_root
        )
        self.registry_root = (
            Path(args.registry_root) if args.registry_root else repo_root / "stores"
        )
        self.work_root = Path(args.work_root) if args.work_root else self.config.base.work_root
        self.run_root = self.work_root / self.store_id
        resolver_root = self.run_root / "resolver"
        self.resolver_manifest_path = resolver_root / "analyses.tsv"
        self.resolver_log_path = resolver_root / "resolve.log"
        self.index_path = resolver_root / "index.json"
        self.receipt_path = resolver_root / RECEIPT_FILENAME
        self.rendered_path = self.run_root / "bundle"
        # Records are kept per Source Collection, not per Store Release (#284).
        self.resolution_area = ResolutionArea.for_collection(
            resolution_root(args.resolution_root, repo_root=repo_root),
            self.config.base.source_collection_id,
        )
        snapshot_id = self.config.base.inventory_snapshot_id
        self.freeze_report_path = self.run_root / "freeze" / f"{snapshot_id}.json"
        self.preflight_report_path = self.run_root / "preflight" / f"{snapshot_id}.json"
        self.cores = args.cores if args.cores is not None else self.config.base.cores
        # The executed command log starts with the operator invocation; the
        # resolver argv is appended when it actually runs.
        self.commands: list[str] = [operator_command]

    def _config_path(self) -> Path:
        path = Path(self.args.config)
        return path if path.is_absolute() else self.repo_root / path

    @property
    def inventory_path(self) -> Path:
        return self.config.base.inventory_path

    @property
    def provenance_path(self) -> Path:
        return self.config.base.inventory_provenance_path


def stage_freeze(pipeline: Pipeline) -> dict:
    outcome = freeze_release(pipeline.config.base, frozen_at=pipeline.args.frozen_at)
    report = {"snapshot_id": pipeline.config.base.inventory_snapshot_id, **outcome.as_dict()}
    pipeline.freeze_report_path.parent.mkdir(parents=True, exist_ok=True)
    pipeline.freeze_report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    verb = "Froze" if outcome.frozen else "Found the frozen"
    print(
        f"{verb} Source Inventory {pipeline.config.base.inventory_snapshot_id} "
        f"({outcome.provenance_path}) for Selection {pipeline.config.base.selection.as_dict()}"
    )
    return report


def stage_preflight(pipeline: Pipeline) -> dict:
    inventory_path = pipeline.inventory_path
    if not inventory_path.is_file():
        raise InventoryError(
            f"frozen source inventory not found: {inventory_path}; run the freeze stage first"
        )
    rows = read_inventory(inventory_path)
    result = preflight(
        inventory_path=inventory_path,
        provenance_path=pipeline.provenance_path,
        rows=rows,
        config=replace(pipeline.config.base, work_root=pipeline.work_root),
        repo_root=pipeline.repo_root,
        cores=pipeline.cores,
    )
    report_path = pipeline.preflight_report_path
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(result.report, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    print(render_preflight_summary(result.report))
    print(f"Report: {report_path}")
    if not result.ok:
        raise CandidateError(
            "preflight failed: "
            + "; ".join(result.failures)
            + f". See {report_path}"
        )
    return result.report


def stage_prepare(pipeline: Pipeline) -> tuple[list, dict]:
    rows = read_inventory(pipeline.inventory_path)
    ready_ids = [row.analysis_id for row in rows if row.ready]
    metadata = read_analysis_metadata(pipeline.provenance_path, ready_ids)
    manifest = derive_resolver_manifest(rows, pipeline.config, metadata)
    pipeline.resolver_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    pipeline.resolver_manifest_path.write_text(
        render_resolver_manifest(manifest), encoding="utf-8"
    )
    print(
        f"Resolver manifest: {pipeline.resolver_manifest_path} "
        f"({len(manifest)} ready Analyses)"
    )
    return manifest, metadata


def stage_resolve(pipeline: Pipeline, manifest: list) -> None:
    area = pipeline.resolution_area
    # Each set of resolver settings has its own checkpoint, so a Selection
    # resolved under other settings never evicts this one's records (#284).
    settings_digest = resolver_settings_digest(resolver_settings(pipeline.config))
    checkpoint = area.checkpoint_dir(settings_digest)
    argv = resolver_argv(
        resolver_bin=pipeline.args.resolver,
        manifest_path=pipeline.resolver_manifest_path,
        records_dir=checkpoint,
        config=pipeline.config,
        cores=pipeline.cores,
    )
    pipeline.commands.append(" ".join(argv))
    # One run at a time per Source Collection. The resolver holds the lock too,
    # so a killed run cannot let a second one interleave with it.
    with area.exclusive(f"{MODULE} {pipeline.store_id}") as lock_fd:
        print("Running: " + " ".join(argv))
        run = run_resolver(
            argv, cwd=pipeline.repo_root, log_path=pipeline.resolver_log_path, pass_fds=(lock_fd,)
        )
        if run.returncode != 0:
            raise CandidateError(
                f"resolver exited {run.returncode}; see {run.log_path}"
            )
        print(f"Resolver log: {run.log_path}")

        # A successful exit is not enough: account the records, keep them, then
        # bind the receipt to them and the contract they were resolved under. A
        # later standalone verify/emit is only trustworthy against this receipt.
        records, failures = account_records(manifest, checkpoint)
        if failures:
            raise CandidateError(
                f"resolver exited 0 but {len(failures)} record(s) are not accountable; refusing "
                "to bind a receipt:\n  - " + "\n  - ".join(failures)
            )
        index_bytes = (checkpoint / "index.json").read_bytes()
        index = json.loads(index_bytes)
        kept = {row.analysis_id: area.keep(checkpoint, row.analysis_id) for row in manifest}
        atomic_write_bytes(
            pipeline.index_path, index_bytes, pipeline.index_path.parent, overwrite=True
        )
        receipt = build_resolution_receipt(
            manifest_rows=manifest,
            config=pipeline.config,
            manifest_path=pipeline.resolver_manifest_path,
            area=area,
            kept=kept,
            argv=argv,
            settings_digest=settings_digest,
            index=index,
            override=resolver_override(pipeline.args.resolver),
        )
        write_resolution_receipt(pipeline.receipt_path, receipt)
    print(
        f"Resolution receipt: {pipeline.receipt_path} ({len(records)} records kept in "
        f"{area.root})"
    )


def resolver_override(resolver: str) -> dict[str, str] | None:
    """The receipt's record of a ``--resolver`` override: the executable's path and sha256."""
    if resolver == DEFAULT_RESOLVER_BIN:
        return None
    path = Path(resolver)
    if not path.is_absolute() or not path.is_file():
        raise CandidateError(
            f"--resolver {resolver!r} is not an absolute path to an executable file; "
            "parse_args resolves it"
        )
    return {"path": str(path), "sha256": sha256_file(path)}


def refuse_overridden_resolver(receipt: dict, *, registry_root: Path, repo_root: Path) -> None:
    """Refuse a receipt another resolver made from the repository's own registry.

    ``--resolver`` exists for verification runs into a temporary registry; a
    candidate in ``stores/`` is decided by the pinned resolver (ADR 0032). A
    receipt into ``stores/`` must therefore say so: ``resolver.override`` null.
    One without the key, written before #287 or stripped of it, is refused too.
    """
    if Path(registry_root).resolve() != (Path(repo_root) / "stores").resolve():
        return  # A temporary registry: verification may use any resolver.
    resolver = receipt.get("resolver")
    if not isinstance(resolver, dict) or "override" not in resolver:
        # Written before the marker existed, or stripped of it: nothing shows
        # the pinned resolver made it.
        raise CandidateError(
            "the resolution receipt does not record whether --resolver overrode the pinned "
            "opengwasdb (resolver.override), so it may not emit into the repository's "
            f"registry {registry_root}; re-run the resolve stage, which reuses every "
            "matching record and rewrites the receipt"
        )
    override = resolver["override"]
    if override is not None:
        raise CandidateError(
            f"the resolution receipt was made by --resolver {override.get('path')!r}, not the "
            "pinned opengwasdb: it is verification-only and may not emit into the "
            f"repository's registry {registry_root}; emit into a temporary --registry-root"
        )


def stage_verify(pipeline: Pipeline, manifest: list) -> tuple[list, dict]:
    records, failures = verify_records(
        manifest,
        config=pipeline.config,
        manifest_path=pipeline.resolver_manifest_path,
        receipt_path=pipeline.receipt_path,
        index_path=pipeline.index_path,
    )
    if failures:
        raise CandidateError(
            f"{len(failures)} resolver accounting failure(s); refusing to finalise:\n  - "
            + "\n  - ".join(failures)
        )
    index = json.loads(pipeline.index_path.read_text(encoding="utf-8"))
    return records, index


def stage_render(
    pipeline: Pipeline,
    manifest: list,
    metadata: dict,
    records: list,
    index: dict,
) -> tuple[CandidateBundle, CandidateTables]:
    """Trait Ontology Mapping, the release policy and rendering; the bundle lands in the Work Root."""
    # Trait Ontology Mapping (#286, ADR 0036 step 5): after verification, from
    # the frozen Source Metadata Table's source terms and the Canonical Trait
    # Mapping Table the release config names.
    try:
        table = load_canonical_trait_mapping_table(pipeline.config.base.canonical_trait_mapping_table)
    except TraitOntologyMappingError as exc:
        raise CandidateError(str(exc)) from exc
    metadata, trait_ontology_run = map_candidate_trait_ontologies(metadata, table)
    print(
        "Trait Ontology Mapping: "
        + ", ".join(
            f"{method}={count}"
            for method, count in trait_ontology_run.provenance()["method_counts"].items()
        )
    )

    # Release policy (ADR 0036 step 6): Quality Checks, the Assigned Ancestry
    # check and the Store Composition Rule.
    rows = read_inventory(pipeline.inventory_path)
    outcomes = apply_release_policy(rows, pipeline.config, metadata, {
        record["analysis_id"]: record for record in records
    }, manifest_rows=manifest)
    tables = build_candidate_tables(
        inventory_rows=rows,
        outcomes=outcomes,
        config=pipeline.config,
        index_summary=index,
        # A region-filtered release carries its filtering's plan and counts (#300),
        # and only from files filtered to its own region policy (#282 review).
        acquisition_evidence=acquisition_evidence(
            pipeline.config.base, [row.analysis_id for row in rows if row.ready]
        ),
    )
    print(
        f"Release policy: {tables.included_rows} included, {tables.excluded_rows} excluded"
        + (
            " (" + ", ".join(f"{k}={v}" for k, v in tables.exclusion_counts.items()) + ")"
            if tables.exclusion_counts
            else ""
        )
    )
    if tables.reference_overlap_errors:
        raise CandidateError("bundle reference-overlap validation failed:\n  - "
                             + "\n  - ".join(tables.reference_overlap_errors))
    schema_errors = validate_candidate_analyses(tables.analyses_tsv)
    if schema_errors:
        raise CandidateError(
            "analyses.tsv failed the pinned OpenGWASDB Analysis schema:\n  - "
            + "\n  - ".join(schema_errors)
        )

    inventory_sha256 = sha256_file(pipeline.inventory_path)
    created_at = pipeline.args.created_at or now_utc()
    generator_version = "sha256:" + sha256_text(Path(__file__).read_bytes())
    commands = list(dict.fromkeys(pipeline.commands))
    try:
        receipt = read_resolution_receipt(pipeline.receipt_path)
    except CandidateError:
        receipt = None
    if isinstance(receipt, dict):
        resolver_block = receipt.get("resolver")
        recorded_argv = resolver_block.get("argv") if isinstance(resolver_block, dict) else None
        if isinstance(recorded_argv, list) and recorded_argv:
            commands = list(dict.fromkeys([*commands, " ".join(str(t) for t in recorded_argv)]))
    receipt_sha256 = (
        sha256_file(pipeline.receipt_path) if pipeline.receipt_path.is_file() else None
    )

    candidate = CandidateBundle(
        store_id=pipeline.store_id,
        release=render_release_yaml(
            store_id=pipeline.store_id,
            config=pipeline.config,
            tables=tables,
            created_at=created_at,
            inventory_sha256=inventory_sha256,
            preflight_report=pipeline.preflight_report_path,
            index_summary=index,
            generator_version=generator_version,
            resolver_receipt_path=pipeline.receipt_path,
            resolver_receipt_sha256=receipt_sha256,
        ),
        build=render_build_yaml(pipeline.store_id, pipeline.config),
        column_maps=render_column_maps(pipeline.config),
        analyses_tsv=tables.analyses_tsv,
        commands=commands,
        validation=render_validation_yaml(
            tables=tables,
            index_summary=index,
            validated_at=created_at,
            validator_name=MODULE,
            trait_ontology_mapping=trait_ontology_run.provenance(),
        ),
        sidecars={
            "source_readiness.tsv": tables.source_readiness_tsv,
            "ancestry.tsv": tables.ancestry_tsv,
            "sd_estimation.tsv": tables.sd_estimation_tsv,
            "exclusions.tsv": tables.exclusions_tsv,
            "reference_overlap.tsv": tables.reference_overlap_tsv,
            "quality_checks.tsv": tables.quality_checks_tsv,
        }
        # Only a release with targeted Analyses carries the target sidecar (#300).
        | ({"analysis_targets.tsv": tables.analysis_targets_tsv} if tables.analysis_targets_tsv else {})
        | dict(tables.acquisition_evidence),
    )
    write_rendered(candidate, pipeline.rendered_path)
    print(f"Rendered: {pipeline.rendered_path}")
    return candidate, tables


def write_rendered(candidate: CandidateBundle, directory: Path) -> None:
    """Write the rendered bundle into the Work Root for review, replacing an earlier render.

    The files are the emitter's (``emit.render_yaml``), with the command log in
    ``release.yaml``; nothing here is a Release Bundle until ``emit`` checks it
    and moves it into the registry.
    """
    staging = directory.with_name(directory.name + ".partial")
    shutil.rmtree(staging, ignore_errors=True)
    (staging / "sidecars").mkdir(parents=True)
    release = dict(candidate.release)
    release["generator"] = {**dict(release.get("generator") or {}), "commands": list(candidate.commands)}
    (staging / "release.yaml").write_text(render_yaml(release), encoding="utf-8")
    (staging / "build.yaml").write_text(render_yaml(candidate.build), encoding="utf-8")
    (staging / "analyses.tsv").write_text(candidate.analyses_tsv, encoding="utf-8")
    if candidate.validation is not None:
        (staging / "validation.yaml").write_text(render_yaml(candidate.validation), encoding="utf-8")
    for name, text in candidate.sidecars.items():
        (staging / "sidecars" / name).write_text(text, encoding="utf-8")
    if candidate.column_maps:
        (staging / "column_maps").mkdir()
        for name, data in candidate.column_maps.items():
            (staging / "column_maps" / name).write_bytes(data)
    shutil.rmtree(directory, ignore_errors=True)
    staging.rename(directory)


def stage_emit(pipeline: Pipeline, candidate: CandidateBundle, tables: CandidateTables) -> Path:
    refuse_overridden_resolver(
        read_resolution_receipt(pipeline.receipt_path),
        registry_root=pipeline.registry_root,
        repo_root=pipeline.repo_root,
    )
    emitted = emit(candidate, pipeline.registry_root)
    print(
        f"Candidate {pipeline.store_id} emitted to {emitted} "
        f"({tables.included_rows} included, {tables.excluded_rows} excluded)"
    )
    if tables.exclusion_counts:
        print(
            "  exclusions: "
            + ", ".join(f"{k}={v}" for k, v in tables.exclusion_counts.items())
        )
    if tables.warnings:
        print("  review:")
        for warning in tables.warnings:
            print(f"    - {warning}")
    return emitted


def run(pipeline: Pipeline) -> Path | None:
    """Run ``--stage`` (or every stage), repeating only the in-memory steps a later stage needs."""
    stage = pipeline.args.stage
    every = stage == "all"
    if every or stage == "freeze":
        stage_freeze(pipeline)
    if every or stage == "preflight":
        stage_preflight(pipeline)
    if stage in ("freeze", "preflight"):
        return None
    manifest, metadata = stage_prepare(pipeline)
    if every or stage == "resolve":
        stage_resolve(pipeline, manifest)
    if stage in ("prepare", "resolve"):
        return None
    records, index = stage_verify(pipeline, manifest)
    if stage == "verify":
        return None
    candidate, tables = stage_render(pipeline, manifest, metadata, records, index)
    if stage == "render":
        return None
    return stage_emit(pipeline, candidate, tables)


def operator_command(argv: Sequence[str]) -> str:
    """Render the invocation this process is carrying out, for the command log."""
    return " ".join(["python3", "-m", MODULE, *argv])


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        pipeline = Pipeline(
            args, Path(args.repo_root).resolve(), operator_command(raw_argv)
        )
    except (PreflightConfigError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    try:
        run(pipeline)
    except (
        CandidateError, EmitError, InventoryError, PreflightConfigError, ResolutionAreaError
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
