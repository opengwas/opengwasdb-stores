#!/usr/bin/env python3
"""Hermetic tests for the Phase B candidate workflow (issue #153).

These are fixture-scale and network-free: a temporary mirror, candidate
metadata table, frozen Source Inventory and config are built in a temp
directory, and a fake `opengwasdb resolve-analyses` (``fixtures/fake_resolver.py``)
supplies the resolver contract -- atomic per-Analysis records, a deterministic
``index.json``, fingerprint inputs and ``--resume`` semantics -- with outcomes
the test chooses. The real resolver's statistics, reference loading and worker
pool are exercised upstream, not here.

Covered contracts:

1. mixed study designs and every controlled exclusion outcome (#152 policy);
2. duplicate-content accessions surfaced, not collapsed;
3. malformed/controlled-failure sources isolated to the affected Analysis;
4. resolver accounting: missing, stale, duplicate and extra records all fail
   finalisation before any bundle is replaced;
5. resume reuses unchanged successful records and reproduces the same bytes;
6. an interrupted resolver leaves a prior candidate untouched and no partial one;
7. 1 worker and many workers produce byte-identical bundle tables/sidecars;
8. the emitted analyses.tsv passes the pinned OpenGWASDB schema and the whole
   bundle passes bundle.check();
9. the operator entry point binds the frozen inventory, the #152 policy and the
   executed command log into release.yaml, and never writes anything but a
   candidate;
10. Resolution Records are kept once per Source Collection and reused by every
    Selection (#284): a Trial Store Release and a full release resolve each
    shared Analysis once; only a changed file, reference or setting
    re-resolves; each receipt names exactly its records and verification
    catches a missing, stale or edited one; a second run over a locked
    collection area is refused.

Run from the repository root:
    python3 tests/phase-b-candidate/test_phase_b_candidate.py
"""

from __future__ import annotations

import contextlib
import csv
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterator, Sequence
from unittest import mock

import yaml

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

import manifest_generation.candidate_workflow as candidate_workflow  # noqa: E402
import manifest_generation.freeze as freeze_module  # noqa: E402
import manifest_generation.qc_panel_concordance as qc_panel_concordance  # noqa: E402
from ogstores import bundle as bundle_module  # noqa: E402
from ogstores.plan import plan  # noqa: E402
from manifest_generation.candidate_workflow import (  # noqa: E402
    RECEIPT_FILENAME,
    RESOLVER_MANIFEST_COLUMNS,
    USABLE_MAF_STATES,
    AnalysisMetadata,
    CandidateConfiguration,
    CandidateError,
    ResolverRow,
    account_records,
    analysis_maf_threshold,
    apply_release_policy,
    build_candidate_tables,
    compute_resolution_contract,
    controlled_exclusion_categories,
    derive_resolver_manifest,
    info_score_emission,
    load_candidate_configuration,
    maf_threshold_emission,
    map_candidate_trait_ontologies,
    read_analysis_metadata,
    trait_sources,
    read_genotyping_technologies,
    read_effect_placeholder_exclusions,
    read_maf_filter_exempt_analyses,
    read_resolution_receipt,
    render_release_yaml,
    render_validation_yaml,
    render_resolver_manifest,
    resolver_argv,
    resolver_settings,
    validate_candidate_analyses,
    verify_records,
)
from manifest_generation.freeze import write_frozen_inventory  # noqa: E402
from manifest_generation.inventory import freeze_release  # noqa: E402
from manifest_generation.freeze import freeze as freeze_connectors  # noqa: E402
from manifest_generation.selection import parse_selection  # noqa: E402
from manifest_generation.trait_ontology_mapping import (  # noqa: E402
    SourceTrait,
    TraitOntologyMapping,
    TraitOntologyMappingMethod,
    load_canonical_trait_mapping_table,
)
from manifest_generation import generate_candidate  # noqa: E402
from manifest_generation.resolution_area import (  # noqa: E402
    DEFAULT_RESOLUTION_ROOT,
    RESOLUTION_ROOT_ENV_VAR,
    KeptRecord,
    ResolutionArea,
    ResolutionAreaError,
    ResolutionAreaLocked,
    resolution_root,
)
from manifest_generation.release_config import RELEASE_CONFIG_VERSION  # noqa: E402
from manifest_generation.source_inventory import (  # noqa: E402
    PreflightConfigError,
    SourceInventoryRow,
    read_inventory,
)

CLI = ("-m", "manifest_generation.generate_candidate")
FAKE_RESOLVER = Path(__file__).resolve().parent / "fixtures/fake_resolver.py"
SNAKEMAKE = shutil.which("snakemake")
STORE_KEY = "hybrid__European"
SNAPSHOT_ID = "fixture-snapshot"
STORE_ID = "OGS-99001"

# (analysis_id, study_design, sample_size, readiness, content, n_cases, n_controls)
READY_ANALYSES = [
    ("GCST90000001", "quantitative", "5000", "ok", "A", "", ""),
    ("GCST90000002", "quantitative", "6000", "ok", "A", "", ""),
    ("GCST90000003", "quantitative", "7000", "ok", "B", "", ""),
    ("GCST90000004", "case-control", "8000", "ok", "C", "4000", "4000"),
    ("GCST90000005", "case-control", "9000", "ok", "D", "4500", "4500"),
    ("GCST90000006", "quantitative", "3000", "ok", "E", "", ""),
    ("GCST90000007", "quantitative", "4000", "ok", "F", "", ""),
    ("GCST90000008", "quantitative", "2000", "ok", "G", "", ""),
    ("GCST90000009", "case-control", "1000", "ok", "H", "", ""),
]
NON_READY = ("GCST90000010", "quantitative", "1000", "header_rejected", "", "", "")

# outcome per analysis_id, fed to the fake resolver
OUTCOMES = {
    # included quantitative, but high dispersion -> warning
    "GCST90000001": {"sd": 1.2, "sd_dispersion": 0.6},
    "GCST90000002": {"sd": 0.8, "sd_dispersion": 0.05},
    "GCST90000003": {"sd_status": "unavailable", "sd_reason": "no_qualifying_evidence"},
    "GCST90000004": {},
    "GCST90000005": {"assigned_ancestry": "EAS"},
    "GCST90000006": {"assigned_ancestry": None, "gate_reason": "overlap"},
    "GCST90000007": {
        "assigned_ancestry": None,
        "gate_reason": "eaf_orientation",
        "eaf_orientation": "failed",
        "eaf_orientation_r": -0.98,
    },
    "GCST90000008": {"status": "controlled_failure", "error": "simulated parse failure"},
    "GCST90000009": {},  # counts blank in the candidate table -> excluded
}

EXPECTED_INCLUDED = {"GCST90000001", "GCST90000002", "GCST90000004"}
EXPECTED_EXCLUDED = {
    "GCST90000003": "sd_no_qualifying_evidence",
    "GCST90000005": "ancestry_not_eur",
    "GCST90000006": "ancestry_unassigned",
    "GCST90000007": "orientation_failure",
    "GCST90000008": "resolution_failed",
    "GCST90000009": "missing_case_control_counts",
}

#: The fixture Selection, in #283's grammar.
SELECTION = {"where": {"store_key": STORE_KEY}}
FROZEN_AT = "2026-09-21T00:00:00Z"
SOURCE_METADATA_HEADER = (
    "source_analysis_id", "source_trait_label", "source_trait_ontology_id",
    "source_trait_ontology_label", "source_ancestry_label", "source_superpopulation",
    "study_design", "sample_size", "n_cases", "n_controls", "publication_pmid",
    "first_author", "store_key",
)
ACQUISITION_MANIFEST_HEADER = (
    "source_analysis_id", "status", "location", "bytes", "sha256", "source_url",
    "sidecar_location", "sidecar_url", "sidecar_bytes", "error",
)

META_TEMPLATE = "gwas_id: {analysis_id}\ngenome_assembly: GRCh38\nis_harmonised: true\ncoordinate_system: 1-based\n"

# A fixture Canonical Trait Mapping Table (#286): the candidate tests never read
# the tracked one, so curating it cannot change them.
TRAIT_TABLE_RESOURCE_ID = "canonical-trait-mapping-efo"
# The Ancestry Reference Panel names the tracked declaration, whose genome_build
# the resolver is passed (opengwasdb#277), and stands in a fixture panel for its
# location, as the trait table above does.
ANCESTRY_RESOURCE_ID = "ukb-ancestry-mixture-hg38"
TRAIT_TABLE_TEXT = (
    "trait_label\ttrait_ontology_id\ttrait_ontology_label\n"
    "weight\tEFO:0004338\tbody weight\n"
)
#: sha256 of TRAIT_TABLE_TEXT.
TRAIT_TABLE_SHA256 = "92a1eabaf92b3821219cc8e52c83245c3e93efaa29cf4ebbf7b05cc5724d14de"
#: The LD panel a fixture completion completes against: the tracked EUR panel
#: declaration (#312), whose ancestry is the fixture's Selection super-population.
COMPLETION_PANEL_RESOURCE_ID = "ukb-hg38-eur-ld"
#: A trait_ontology_mapping block for tests that render validation.yaml for other reasons.
TRAIT_ONTOLOGY_PROVENANCE = {
    "canonical_trait_mapping_table": None,
    "method_counts": {"source_provided": 0, "canonical_table_lookup": 0, "unmapped": 0},
    "source_id_without_label": 0,
    "rejected_source_terms": [],
}


@dataclass
class Fixture:
    root: Path
    config_path: Path
    inventory_path: Path
    provenance_path: Path
    #: The Source Collection's connector files (#283), which freeze reads.
    source_metadata_table_path: Path
    acquisition_manifest_path: Path
    outcomes_path: Path
    registry_root: Path
    work_root: Path
    trait_table_dir: Path
    resolution_root: Path


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FixtureBuilder:
    """Build one self-consistent fixture tree in a temporary directory."""

    def __init__(self, root: Path, bodies=None) -> None:
        #: Optional ``analysis_id -> bytes`` for the mirror's source files; the
        #: default is a placeholder body only the fake resolver can read.
        self.bodies = bodies
        self.root = root
        self.mirror = root / "mirror"
        self.mirror.mkdir(parents=True, exist_ok=True)
        self.reference = root / "reference"
        self.reference.mkdir(parents=True, exist_ok=True)
        self.acq = root / "acq"
        self.acq.mkdir(parents=True, exist_ok=True)
        self.inventory_dir = root / "inventory"
        self.inventory_dir.mkdir(parents=True, exist_ok=True)

    def build(
        self,
        *,
        freeze: bool = True,
        completion: dict | None = None,
        layout: str = "hybrid",
    ) -> Fixture:
        """Write the mirror, both connector files and a release config.

        ``freeze`` freezes the Source Inventory up front, as unit tests that
        read it before any stage runs need; without it, the entry point's
        ``freeze`` stage writes it. ``completion`` declares a reference-completion
        recipe beside a ``dense`` release's ``build`` block (#340, ADR 0037).
        """
        (self.reference / "ref_freqs.tsv.gz").write_bytes(b"fixture reference\n")
        (self.reference / "ld").mkdir(parents=True, exist_ok=True)
        (self.reference / "ancestry_groups.tsv").write_text(
            "fine_group\tsuper_population\nEUR_A\tEUR\nEAS_A\tEAS\n", encoding="utf-8"
        )

        all_rows = READY_ANALYSES + [NON_READY]
        manifest_rows: list[dict[str, str]] = []
        for analysis_id, _design, _sample_size, readiness, content, _n_cases, _n_controls in all_rows:
            row = {"source_analysis_id": analysis_id, "status": readiness}
            if readiness in {"ok", "already_present"}:
                body = (
                    self.bodies(analysis_id)
                    if self.bodies is not None
                    else (f"source-body-{content}\n" * (1 + ord(content[0]) % 3)).encode()
                )
                data_path = self.mirror / f"{analysis_id}.h.tsv.gz"
                data_path.write_bytes(body)
                meta_path = self.mirror / f"{analysis_id}.h.tsv.gz-meta.yaml"
                meta_path.write_text(
                    META_TEMPLATE.format(analysis_id=analysis_id), encoding="utf-8"
                )
                row.update(
                    location=str(data_path),
                    bytes=str(len(body)),
                    sha256=_sha256_bytes(body),
                    source_url=f"https://example.invalid/{analysis_id}.h.tsv.gz",
                    sidecar_location=str(meta_path),
                    sidecar_url=f"https://example.invalid/{analysis_id}.h.tsv.gz-meta.yaml",
                    sidecar_bytes=str(meta_path.stat().st_size),
                )
            else:
                row["error"] = "missing required GWAS-SSF columns: beta"
            manifest_rows.append(row)
        acquisition_manifest_path = self.acq / "acquisition-manifest.tsv"
        _write_tsv(acquisition_manifest_path, list(ACQUISITION_MANIFEST_HEADER), manifest_rows)

        table_rows = []
        for analysis_id, design, sample_size, _readiness, _content, n_cases, n_controls in all_rows:
            table_rows.append(
                {
                    "source_analysis_id": analysis_id,
                    "source_trait_label": f"Trait {analysis_id}",
                    "source_trait_ontology_id": "http://purl.obolibrary.org/obo/MONDO_0005148",
                    "source_trait_ontology_label": f"mapped {analysis_id}",
                    "source_ancestry_label": "European",
                    "source_superpopulation": "EUR",
                    "study_design": design,
                    "sample_size": sample_size,
                    "n_cases": n_cases,
                    "n_controls": n_controls,
                    "publication_pmid": "12345678",
                    "first_author": "Author A",
                    "store_key": STORE_KEY,
                }
            )
        source_metadata_table_path = self.root / "source-metadata.tsv"
        _write_tsv(source_metadata_table_path, list(SOURCE_METADATA_HEADER), table_rows)

        inventory_path = self.inventory_dir / f"{SNAPSHOT_ID}.tsv"
        provenance_path = self.inventory_dir / f"{SNAPSHOT_ID}.meta.yaml"
        if freeze:
            frozen = freeze_connectors(
                snapshot_id=SNAPSHOT_ID,
                source_collection_id="gwas-catalog-ssf",
                metadata_table=source_metadata_table_path,
                acquisition_manifest=acquisition_manifest_path,
                selection=parse_selection(SELECTION),
                frozen_at=FROZEN_AT,
            )
            write_frozen_inventory(frozen, inventory_path, provenance_path)

        work_root = self.root / "work"
        registry_root = self.root / "stores"
        registry_root.mkdir(parents=True, exist_ok=True)

        trait_table_dir = self.root / "resources" / "reference-resources" / TRAIT_TABLE_RESOURCE_ID
        trait_table_dir.mkdir(parents=True)
        (trait_table_dir / "resource.yaml").write_text(
            f"resource_id: {TRAIT_TABLE_RESOURCE_ID}\nkind: trait_ontology_mapping\nversion: 1\n"
            f"location: resources/reference-resources/{TRAIT_TABLE_RESOURCE_ID}/mapping.tsv\n"
            "location_kind: tracked_file\n",
            encoding="utf-8",
        )
        (trait_table_dir / "mapping.tsv").write_text(TRAIT_TABLE_TEXT, encoding="utf-8")

        config = {
            "release_config_version": RELEASE_CONFIG_VERSION,
            "label": "fixture-candidate",
            "access_posture": "public",
            "description": "fixture candidate",
            "notes": "fixture notes",
            "source_collection": {
                "source_collection_id": "gwas-catalog-ssf",
                "connectors": {
                    "source_metadata_table": str(source_metadata_table_path),
                    "acquisition_manifest": str(acquisition_manifest_path),
                },
                "inventory": {"snapshot_id": SNAPSHOT_ID, "directory": str(self.inventory_dir)},
            },
            "selection": SELECTION,
            "build": {
                "layout": layout,
                "completion_state": "observed_only",
                "command": "build-hybrid" if layout == "hybrid" else "build-dense-vcf",
                "options": {
                    "source-reader-capability": "opengwasdb.gwas-ssf",
                    "source-assembly": "hg38",
                },
                # A Dense release plans the Top-Hit Index and the completion
                # inline, so its post steps are the Dense golden's (§`tests/plan`).
                "post": {"top_hits": layout == "dense", "overview": True},
            },
            "defaults": {
                "source_genome_build": "GRCh38",
                "license": "fixture license",
                "sample_size_scope": "analysis_level",
                "by_study_design": {
                    "quantitative": {
                        "stored_effect_scale": "sd",
                        "original_effect_scale": "sd",
                        "original_sd_method": "estimated_from_source_maf",
                        "sample_size_kind": "total",
                    },
                    "case-control": {
                        "stored_effect_scale": "log_or",
                        "original_effect_scale": "log_or",
                        "original_sd_method": "binary_trait",
                        "sample_size_kind": "case_control",
                    },
                },
            },
            "release_policy": {"selection_superpopulation": "EUR"},
            "reference_resources": [
                {
                    "resource_id": ANCESTRY_RESOURCE_ID,
                    "kind": "ancestry_mixture",
                    "ancestry": "multi",
                    "super_populations": ["AFR", "AMR", "EAS", "EUR", "MID", "NAF", "SAS"],
                    "genome_build": "GRCh38",
                    "variant_id_convention": "chr:pos:A1:A2",
                    "location": str(self.reference / "ref_freqs.tsv.gz"),
                    "location_kind": "external_file",
                    "version": "fixture-v1",
                    "fine_group_map": str(self.reference / "ancestry_groups.tsv"),
                },
                {
                    "resource_id": TRAIT_TABLE_RESOURCE_ID,
                    "kind": "trait_ontology_mapping",
                    "location": str(trait_table_dir),
                    "version": "1",
                },
            ],
            "ancestry_assignment": {
                "reference_resource_id": ANCESTRY_RESOURCE_ID,
                "maf_floor": 0.01,
                "extraction_panel": None,
                "gates": {"tau": 0.50, "delta": 0.20, "n_min": 5000, "residual_max": 0.06},
            },
            "effect_scale_validation": {
                "maf_min": 0.01,
                "maf_max": 0.5,
                "dispersion_max": 0.5,
                "reference_resources": [],
            },
            "trait_ontology_mapping": {"reference_resource_id": TRAIT_TABLE_RESOURCE_ID},
            "runtime": {"cores": 4, "min_free_gb": 0},
            "output": {"work_root": str(work_root)},
        }
        if completion is not None:
            config["build"]["completion_state"] = "reference_completed"
            config["completion"] = completion
            config["reference_resources"].append(
                {
                    # The tracked panel declaration supplies the ancestry, which
                    # complete-dense names both the panel directory and the
                    # Analyses' filter with; this release declares it present
                    # under the fixture's own reference root.
                    "resource_id": COMPLETION_PANEL_RESOURCE_ID,
                    "kind": "ld_panel",
                    "location": str(self.reference / "ld"),
                    "location_kind": "external_directory",
                }
            )
        config_path = self.root / "config.yaml"
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

        outcomes_path = self.root / "outcomes.json"
        outcomes_path.write_text(json.dumps(OUTCOMES), encoding="utf-8")
        return Fixture(
            root=self.root,
            config_path=config_path,
            inventory_path=inventory_path,
            provenance_path=provenance_path,
            source_metadata_table_path=source_metadata_table_path,
            acquisition_manifest_path=acquisition_manifest_path,
            outcomes_path=outcomes_path,
            registry_root=registry_root,
            work_root=work_root,
            trait_table_dir=trait_table_dir,
            resolution_root=self.root / "resolution",
        )


def _refreeze(fixture: "Fixture", *, snapshot_id: str = SNAPSHOT_ID) -> None:
    """Freeze the fixture's connectors again after a test edited them.

    A frozen inventory is never overwritten in place by the pipeline; a test
    that changes the Source Collection removes the old freeze first.
    """
    for path in fixture.inventory_path.parent.glob(f"{snapshot_id}.*"):
        path.unlink()
    frozen = freeze_connectors(
        snapshot_id=snapshot_id,
        source_collection_id="gwas-catalog-ssf",
        metadata_table=fixture.source_metadata_table_path,
        acquisition_manifest=fixture.acquisition_manifest_path,
        selection=parse_selection(SELECTION),
        frozen_at=FROZEN_AT,
    )
    write_frozen_inventory(
        frozen,
        fixture.inventory_path.parent / f"{snapshot_id}.tsv",
        fixture.inventory_path.parent / f"{snapshot_id}.meta.yaml",
    )


def _rewrite_source_metadata(fixture: "Fixture", edits: dict[str, dict[str, str]]) -> None:
    """Edit some Analyses' cells in the Source Metadata Table and freeze it again."""
    path = fixture.source_metadata_table_path
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        columns = list(reader.fieldnames or [])
        rows = list(reader)
    for row in rows:
        row.update(edits.get(row["source_analysis_id"], {}))
    _write_tsv(path, columns, rows)
    _refreeze(fixture)


def _write_tsv(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, delimiter="\t")
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in columns})


def _fingerprint_digest(fingerprints: dict) -> str:
    """The resolver's canonical digest, so a test can forge a self-consistent record."""
    clean = {key: value for key, value in fingerprints.items() if key != "fingerprint_digest"}
    payload = json.dumps(clean, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _receipt_of(fixture: Fixture) -> dict:
    return json.loads((fixture.work_root / STORE_ID / "resolver" / RECEIPT_FILENAME).read_text())


def _checkpoint(fixture: Fixture) -> Path:
    """The resolver records directory the candidate's run resolved into (#284)."""
    receipt = _receipt_of(fixture)
    return (
        Path(receipt["resolution_area"]) / "checkpoint" / receipt["resolver"]["settings_digest"]
    )


def _kept_record(fixture: Fixture, analysis_id: str) -> Path:
    """The kept Resolution Record the candidate's receipt names for one Analysis."""
    receipt = _receipt_of(fixture)
    return Path(receipt["resolution_area"]) / receipt["records"][analysis_id]["record"]


def _forge_kept_record(fixture: Fixture, analysis_id: str, edit) -> None:
    """Edit a kept record *and* re-bind the receipt to its new checksum.

    A plain edit is caught by the record checksum first; forging the checksum
    too shows the record-level checks still hold behind it.
    """
    receipt_path = fixture.work_root / STORE_ID / "resolver" / RECEIPT_FILENAME
    receipt = json.loads(receipt_path.read_text())
    entry = receipt["records"][analysis_id]
    area = Path(receipt["resolution_area"])
    record = json.loads((area / entry["record"]).read_text())
    edit(record)
    data = json.dumps(record, indent=2, sort_keys=True).encode("utf-8")
    entry["record_sha256"] = hashlib.sha256(data).hexdigest()
    entry["record"] = (
        f"records/{analysis_id}/{entry['fingerprint_digest']}/{entry['record_sha256']}.json"
    )
    (area / entry["record"]).write_bytes(data)
    receipt_path.write_text(json.dumps(receipt, indent=2))


def _two_resolver_rows() -> list[ResolverRow]:
    """Two minimal resolver rows for unit-level accounting tests."""
    return [
        ResolverRow("GCST1", "/tmp/a.h.tsv.gz", "opengwasdb.gwas-ssf", "sd",
                    "estimated_from_source_maf", "100", "a" * 64, "sha256", "10"),
        ResolverRow("GCST2", "/tmp/b.h.tsv.gz", "opengwasdb.gwas-ssf", "sd",
                    "estimated_from_source_maf", "200", "b" * 64, "sha256", "20"),
    ]


def _cli_invocation(
    fixture: Fixture,
    *args: str,
    cores: int = 1,
    outcomes: Path | None = None,
    fail_after: int | None = None,
    registry_root: Path | None = None,
    work_root: Path | None = None,
    resolution_root: Path | None = None,
    store_id: str = STORE_ID,
    config_path: Path | None = None,
    resolved_log: Path | None = None,
    resolver: str | Path = FAKE_RESOLVER,
    extra_env: dict[str, str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """The entry point's argv and environment for one test run."""
    command = [
        sys.executable,
        *CLI,
        store_id,
        "--config",
        str(config_path or fixture.config_path),
        "--cores",
        str(cores),
        "--resolver",
        str(resolver),
        "--repo-root",
        str(REPO_ROOT),
        "--registry-root",
        str(registry_root or fixture.registry_root),
        "--work-root",
        str(work_root or fixture.work_root),
        # Never the deployment default on /data: every test resolves into its
        # own temporary resolution root.
        "--resolution-root",
        str(resolution_root or fixture.resolution_root),
        *args,
    ]
    # The fake resolver is exec'd through its shebang, so put this interpreter's
    # directory first on PATH: it then runs under the same environment (and the
    # same installed opengwasdb) as the registry process.
    interpreter_dir = str(Path(sys.executable).resolve().parent)
    env = {
        **os.environ,
        "PATH": interpreter_dir + os.pathsep + os.environ.get("PATH", ""),
        "FAKE_RESOLVER_OUTCOMES": str(outcomes or fixture.outcomes_path),
        **(extra_env or {}),
    }
    if fail_after is not None:
        env["FAKE_RESOLVER_FAIL_AFTER"] = str(fail_after)
    if resolved_log is not None:
        env["FAKE_RESOLVER_LOG"] = str(resolved_log)
    return command, env


@contextlib.contextmanager
def _one_invocation() -> Iterator[None]:
    """Clear the state the entry point memoises on its modules, before and after a run.

    :func:`_run_cli` drives the entry point in this process, so a cache one
    invocation fills outlives it, where a subprocess would start empty every
    time. The content digest memo and the provenance parse each refuse to
    answer for bytes a file no longer holds (``ContentDigestMemoTests``, #290
    review), but a run is still held to the state a fresh process starts from.
    """

    def clear() -> None:
        candidate_workflow._SHA256_BY_FILE_IDENTITY.clear()
        freeze_module._parsed_provenance.cache_clear()
        # Per-worker concordance state: a single-worker run fills it in this
        # process. Every run sets it before use, and dropping it after the run
        # releases the reference the run loaded with it.
        qc_panel_concordance._WORKER_REF = None
        qc_panel_concordance._WORKER_PANEL = None
        qc_panel_concordance._WORKER_GATES = None

    clear()
    try:
        yield
    finally:
        clear()


def _run_cli(fixture: Fixture, *args: str, **kwargs) -> subprocess.CompletedProcess:
    """Drive the operator entry point in this process, then report as a subprocess would.

    The suite's subject is what the entry point does, not that ``python -m`` can
    start it: reaching ``generate_candidate.main`` in this interpreter runs the
    same argparse, the same stages and the same files, starting from the state a
    fresh process would have (:func:`_one_invocation`). A fresh interpreter for
    every run costs about 1.4 s here (``load_candidate_configuration`` imports
    ``opengwasdb``), which made this suite the repository's slowest (#282).

    Tests that assert on the process itself -- an uncaught exception's
    traceback, a killed run's inherited lock -- use :func:`_run_cli_subprocess`.
    """
    command, env = _cli_invocation(fixture, *args, **kwargs)
    stdout, stderr = io.StringIO(), io.StringIO()
    previous_cwd = os.getcwd()
    # The subprocess got exactly this environment; give the in-process run the
    # same one, so the fake resolver's own variables reach it either way.
    with mock.patch.dict(os.environ, env, clear=True):
        os.chdir(REPO_ROOT)
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                with _one_invocation():
                    try:
                        code = generate_candidate.main(command[3:])
                    except SystemExit as exc:  # argparse refusing a flag, as `python -m` would
                        # Python's own exit semantics: an integer is the code, None
                        # is a clean exit, and anything else is a message on stderr
                        # with exit code 1.
                        code = exc.code
                        if code is not None and not isinstance(code, int):
                            print(code, file=stderr)
                            code = 1
        finally:
            os.chdir(previous_cwd)
    # A main that returns None exited cleanly, as it does under `python -m`.
    return subprocess.CompletedProcess(
        command, 0 if code is None else code, stdout.getvalue(), stderr.getvalue()
    )


def _run_cli_subprocess(
    fixture: Fixture, *args: str, **kwargs
) -> subprocess.CompletedProcess:
    """Run the entry point as ``python3 -m manifest_generation.generate_candidate``."""
    command, env = _cli_invocation(fixture, *args, **kwargs)
    return subprocess.run(
        command, cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, check=False
    )


def _read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def _candidate_tables_bytes(registry_root: Path) -> dict[str, bytes]:
    root = registry_root / STORE_ID
    return {
        "analyses.tsv": (root / "analyses.tsv").read_bytes(),
        "source_readiness.tsv": (root / "sidecars/source_readiness.tsv").read_bytes(),
        "ancestry.tsv": (root / "sidecars/ancestry.tsv").read_bytes(),
        "sd_estimation.tsv": (root / "sidecars/sd_estimation.tsv").read_bytes(),
        "exclusions.tsv": (root / "sidecars/exclusions.tsv").read_bytes(),
    }


def _synthetic_inventory_row(
    *, study_design: str = "quantitative", sample_size: str = "5000", readiness: str = "ok"
) -> SourceInventoryRow:
    return SourceInventoryRow(
        analysis_id="GCST90000001",
        publication_pmid="12345678",
        trait="Trait",
        study_design=study_design,
        sample_size=sample_size,
        readiness_status=readiness,
        data_url="https://example.invalid/x.h.tsv.gz",
        yaml_url="https://example.invalid/x.h.tsv.gz-meta.yaml",
        data_file="/mirror/GCST90000001.h.tsv.gz",
        yaml_file="/mirror/GCST90000001.h.tsv.gz-meta.yaml",
        data_bytes="100",
        yaml_bytes="10",
        sha256="a" * 64,
        error="",
    )


def _synthetic_analysis_metadata(
    *, sample_size: str = "5000", n_cases: str = "", n_controls: str = ""
) -> AnalysisMetadata:
    return AnalysisMetadata(
        analysis_id="GCST90000001",
        source_label="Trait",
        source_trait_ontology_id="",
        source_trait_ontology_label="",
        publication_pmid="12345678",
        first_author="Author",
        n_cases=n_cases,
        n_controls=n_controls,
        sample_size=sample_size,
        trait_ontology=TraitOntologyMapping("", "", TraitOntologyMappingMethod.UNMAPPED),
    )


def _synthetic_resolver_record(
    *,
    gate_reason: str = "ok",
    eaf_orientation: str = "passed",
    eaf_orientation_r: float = 0.99,
    assigned_ancestry: str | None = "EUR",
    status: str = "success",
    phenotype_sd_status: str = "estimated",
    phenotype_sd_reason: str | None = None,
) -> dict:
    """A resolver record in the pinned real shape, including the EafOrientationOutcome."""
    estimate = (
        {"sd": 1.0, "dispersion": 0.05, "method": "estimated_from_source_maf", "notes": ""}
        if phenotype_sd_status == "estimated"
        else None
    )
    return {
        "record_schema_version": 1,
        "analysis_id": "GCST90000001",
        "status": status,
        "fingerprints": {},
        "diagnostics": {
            "source_file": "/mirror/GCST90000001.h.tsv.gz",
            "rows_read": 100,
            "ancestry_sites": 100,
            # Measured by the build_eligible_rows Quality Check (#285): an
            # included Analysis must carry it.
            "build_eligible_rows": 100,
        },
        "ancestry": {
            "assigned_ancestry": assigned_ancestry,
            "dominant_superpop": assigned_ancestry,
            "dominant_proportion": 0.9 if assigned_ancestry else 0.2,
            "runner_up_margin": 0.7 if assigned_ancestry else 0.1,
            "af_overlap": 100000,
            "residual": 0.01,
            "gate_reason": gate_reason,
            "eaf_orientation": eaf_orientation,
            "eaf_orientation_r": eaf_orientation_r,
            "superpop_composition": {assigned_ancestry: 0.9} if assigned_ancestry else {},
            "fine_composition": {},
        },
        "phenotype_sd": {
            "status": phenotype_sd_status,
            "reason": phenotype_sd_reason,
            "estimate": estimate,
            "reference_id": "",
            "n_evidence_considered": 100,
            "n_estimate_inputs": 90 if estimate else 0,
            "evidence_sampled": False,
        },
        "error": None if status == "success" else "controlled failure",
        "warnings": [],
        "metrics": {"elapsed_seconds": 0.0, "peak_memory_bytes": 0},
    }


class CandidateWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="phase-b-candidate-")
        self.fixture = FixtureBuilder(Path(self._tmp.name)).build()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- happy path and policy -------------------------------------------------

    def test_full_pipeline_mixed_designs_and_controlled_outcomes(self) -> None:
        # Through `python3 -m`: the whole operator entry point, in a new process.
        result = _run_cli_subprocess(self.fixture)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

        bundle_dir = self.fixture.registry_root / STORE_ID
        self.assertTrue((bundle_dir / "release.yaml").is_file())
        self.assertTrue((bundle_dir / "build.yaml").is_file())
        self.assertTrue((bundle_dir / "validation.yaml").is_file())
        self.assertTrue((bundle_dir / "analyses.tsv").is_file())
        for sidecar in ("source_readiness", "ancestry", "sd_estimation", "exclusions", "reference_overlap"):
            self.assertTrue((bundle_dir / "sidecars" / f"{sidecar}.tsv").is_file())

        analyses = _read_tsv(bundle_dir / "analyses.tsv")
        included = {row["analysis_id"] for row in analyses if row["exclude_from_build"] != "true"}
        excluded = {
            row["analysis_id"]: row for row in analyses if row["exclude_from_build"] == "true"
        }
        self.assertEqual(included, EXPECTED_INCLUDED)
        self.assertEqual(set(excluded), set(EXPECTED_EXCLUDED))
        # The pinned GWAS-SSF reader has no validated imputation INFO/R²
        # projection; MAF and lookalike header spellings are not substitutes.
        self.assertEqual({row["info_score_threshold"] for row in analyses}, {"NaN"})

        # Case-control rows carry log_or/binary_trait and counts; quantitative
        # rows carry the computable SD tier.
        by_id = {row["analysis_id"]: row for row in analyses}
        self.assertEqual(by_id["GCST90000004"]["stored_effect_scale"], "log_or")
        self.assertEqual(by_id["GCST90000004"]["original_sd_method"], "binary_trait")
        self.assertEqual(by_id["GCST90000004"]["n_cases"], "4000")
        self.assertEqual(by_id["GCST90000004"]["n_controls"], "4000")
        self.assertEqual(by_id["GCST90000001"]["stored_effect_scale"], "sd")
        self.assertEqual(by_id["GCST90000001"]["original_sd_method"], "estimated_from_source_maf")
        self.assertEqual(by_id["GCST90000001"]["assigned_ancestry"], "EUR")
        self.assertNotEqual(by_id["GCST90000001"]["original_sd"], "")

        # Every excluded row carries its controlled reason in its audit row.
        for analysis_id, reason in EXPECTED_EXCLUDED.items():
            self.assertIn(reason, excluded[analysis_id]["inclusion_reason"])
            self.assertIn(reason, controlled_exclusion_categories())

        exclusions = _read_tsv(bundle_dir / "sidecars/exclusions.tsv")
        reasons = {row["analysis_id"]: row["reason"] for row in exclusions}
        self.assertEqual(reasons, EXPECTED_EXCLUDED)

        # Every selected Analysis (included or excluded) has ancestry + SD rows.
        selected_ids = EXPECTED_INCLUDED | set(EXPECTED_EXCLUDED)
        ancestry = _read_tsv(bundle_dir / "sidecars/ancestry.tsv")
        sd_rows = _read_tsv(bundle_dir / "sidecars/sd_estimation.tsv")
        self.assertEqual({row["analysis_id"] for row in ancestry}, selected_ids)
        self.assertEqual({row["analysis_id"] for row in sd_rows}, selected_ids)
        overlap_rows = _read_tsv(bundle_dir / "sidecars/reference_overlap.tsv")
        self.assertEqual({row["analysis_id"] for row in overlap_rows}, selected_ids)
        self.assertEqual(
            {row["rows_scanned"] for row in overlap_rows if row["exclude_from_build"] != "true"},
            {"2000"},
        )
        sd_by_id = {row["analysis_id"]: row for row in sd_rows}
        self.assertEqual(sd_by_id["GCST90000004"]["status"], "skipped")
        self.assertEqual(
            sd_by_id["GCST90000004"]["skip_reason"], "non_quantitative_effect_scale"
        )
        self.assertEqual(sd_by_id["GCST90000003"]["status"], "failed")
        self.assertEqual(sd_by_id["GCST90000001"]["status"], "warning")

        # The 6,035-row-style inventory evidence stays distinct from membership.
        readiness = _read_tsv(bundle_dir / "sidecars/source_readiness.tsv")
        self.assertEqual(len(readiness), len(READY_ANALYSES) + 1)
        membership = {row["analysis_id"]: row["candidate_membership"] for row in readiness}
        self.assertEqual(membership["GCST90000010"], "not_ready")
        self.assertEqual(membership["GCST90000001"], "included")
        self.assertEqual(membership["GCST90000003"], "excluded")

        # Duplicate content is surfaced, not collapsed: both are included.
        duplicate_rows = {
            row["analysis_id"]: row["duplicate_content_group"]
            for row in readiness
            if row["duplicate_content_group"]
        }
        self.assertEqual(set(duplicate_rows), {"GCST90000001", "GCST90000002"})

        # release.yaml is a candidate that binds the frozen snapshot and the run.
        release = yaml.safe_load((bundle_dir / "release.yaml").read_text(encoding="utf-8"))
        self.assertEqual(release["status"], "candidate")
        self.assertEqual(release["source_snapshot_id"], SNAPSHOT_ID)
        self.assertEqual(release["source_snapshot"]["inventory_tsv_sha256"],
                         _sha256_bytes(self.fixture.inventory_path.read_bytes()))
        commands = " ".join(release["generator"]["commands"])
        self.assertIn("resolve-analyses", commands)
        self.assertIn("--n-workers 1", commands)
        self.assertIn("python3 -m manifest_generation.generate_candidate", commands)
        receipt_path = self.fixture.work_root / STORE_ID / "resolver" / RECEIPT_FILENAME
        self.assertEqual(
            release["source_snapshot"]["resolver_receipt_sha256"],
            hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        )

        build = yaml.safe_load((bundle_dir / "build.yaml").read_text(encoding="utf-8"))
        self.assertEqual(build["layout"], "hybrid")
        self.assertEqual(build["completion_state"], "observed_only")
        self.assertEqual(build["build"]["command"], "build-hybrid")
        self.assertNotIn("artifacts", build)
        # Dense/Hybrid releases declare the maintained hm3 Indexed Variant Subset
        # by default (stores #210), sourced from the hapmap3-hg38 resource.
        self.assertEqual(
            build["indexed_subsets"],
            [{
                "name": "hm3",
                "resource_id": "hapmap3-hg38",
                "variant_list": (
                    "/data/opengwasdb/reference/hapmap3-hg38/hm3.grch38.alid.txt"
                ),
                "reference_assembly": "GRCh38",
                "checksum": (
                    "4a7c82aa625a006ff437dcaa2769b00e056413d54c72e98f7378327c69513398"
                ),
            }],
        )

        validation = yaml.safe_load((bundle_dir / "validation.yaml").read_text(encoding="utf-8"))
        self.assertIn(validation["status"], {"passed", "passed_with_warnings"})
        self.assertEqual(validation["checks"]["schema"], "passed")
        self.assertEqual(validation["checks"]["reference_overlap"], "passed")
        self.assertEqual(validation["reference_overlap"]["median_rate"], 1.0)

        # The complete bundle passes the executable contract, and the active
        # (non-excluded) analyses pass the pinned OpenGWASDB schema.
        loaded = bundle_module.load(STORE_ID, registry_root=self.fixture.registry_root)
        self.assertEqual(list(bundle_module.check(loaded, registry_root=self.fixture.registry_root)), [])
        self.assertEqual(validate_candidate_analyses((bundle_dir / "analyses.tsv").read_text()), [])
        # The observed-only Hybrid recipe is plannable exactly as ADR 0023 requires.
        steps = plan(loaded)
        self.assertEqual(steps[0].name, "build")
        self.assertIn("build-hybrid", steps[0].argv)
        step_names = [step.name for step in steps]
        self.assertIn("indexed-subset-hm3", step_names)
        self.assertLess(
            step_names.index("indexed-subset-hm3"),
            step_names.index("validate"),
        )

    def test_info_threshold_configuration_and_no_declaration_emission(self) -> None:
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        self.assertEqual(config.info_score_threshold, "0.6")
        for value in ("0", "1", "0.6"):
            document = yaml.safe_load(self.fixture.config_path.read_text())
            document["defaults"]["info_score_threshold"] = value
            self.fixture.config_path.write_text(yaml.safe_dump(document))
            loaded = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
            self.assertEqual(loaded.info_score_threshold, value)
        # With no declaration configured every Analysis emits literal NaN, even
        # for an explicit zero request: the requested floor alone is not evidence.
        document = yaml.safe_load(self.fixture.config_path.read_text())
        document["defaults"]["info_score_threshold"] = 0
        self.fixture.config_path.write_text(yaml.safe_dump(document))
        result = _run_cli(self.fixture)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        emitted = _read_tsv(self.fixture.registry_root / STORE_ID / "analyses.tsv")
        self.assertEqual({row["info_score_threshold"] for row in emitted}, {"NaN"})
        self.assertEqual({row["imputation_score_column"] for row in emitted}, {""})

        for invalid in ("NaN", "inf", "-0.1", "1.1", "", True):
            document = yaml.safe_load(self.fixture.config_path.read_text())
            document["defaults"]["info_score_threshold"] = invalid
            self.fixture.config_path.write_text(yaml.safe_dump(document))
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError, "info_score_threshold"
            ):
                load_candidate_configuration(self.fixture.config_path, REPO_ROOT)

    def _declare_scores(self, rows: list[dict[str, str]]) -> Path:
        path = self.fixture.root / "score-declarations.tsv"
        _write_tsv(path, list(candidate_workflow.SCORE_DECLARATION_COLUMNS), rows)
        document = yaml.safe_load(self.fixture.config_path.read_text())
        document["release_policy"]["imputation_score_declarations"] = str(path)
        self.fixture.config_path.write_text(yaml.safe_dump(document))
        return path

    def _set_outcome(self, analysis_id: str, **values: object) -> None:
        outcomes = json.loads(self.fixture.outcomes_path.read_text())
        outcomes.setdefault(analysis_id, {}).update(values)
        self.fixture.outcomes_path.write_text(json.dumps(outcomes))

    def _set_threshold(self, value: object) -> None:
        document = yaml.safe_load(self.fixture.config_path.read_text())
        document["defaults"]["info_score_threshold"] = value
        self.fixture.config_path.write_text(yaml.safe_dump(document))

    def _set_maf_threshold(self, value: object) -> None:
        document = yaml.safe_load(self.fixture.config_path.read_text())
        if value is None:
            document["defaults"].pop("maf_threshold", None)
        else:
            document["defaults"]["maf_threshold"] = value
        self.fixture.config_path.write_text(yaml.safe_dump(document))

    def _set_maf_exempt(self, technologies: object) -> None:
        document = yaml.safe_load(self.fixture.config_path.read_text())
        if technologies is None:
            document["release_policy"].pop("maf_filter_exempt_genotyping_technologies", None)
        else:
            document["release_policy"]["maf_filter_exempt_genotyping_technologies"] = technologies
        self.fixture.config_path.write_text(yaml.safe_dump(document))

    def _set_maf_exempt_analyses(self, path: object) -> None:
        document = yaml.safe_load(self.fixture.config_path.read_text())
        if path is None:
            document["release_policy"].pop("maf_filter_exempt_analyses", None)
        else:
            document["release_policy"]["maf_filter_exempt_analyses"] = path
        self.fixture.config_path.write_text(yaml.safe_dump(document))

    def _set_store_composition(self, value: object) -> None:
        document = yaml.safe_load(self.fixture.config_path.read_text())
        if value is None:
            document["release_policy"].pop("store_composition", None)
        else:
            document["release_policy"]["store_composition"] = value
        self.fixture.config_path.write_text(yaml.safe_dump(document))

    def _set_indexed_subsets(self, value: object, *, present: bool = True) -> None:
        document = yaml.safe_load(self.fixture.config_path.read_text())
        if not present:
            document.pop("indexed_subsets", None)
        else:
            document["indexed_subsets"] = value
        self.fixture.config_path.write_text(yaml.safe_dump(document))

    def _set_indexed_subsets_opt_out(self, value: object) -> None:
        document = yaml.safe_load(self.fixture.config_path.read_text())
        if value is None:
            document.pop("indexed_subsets_opt_out", None)
        else:
            document["indexed_subsets_opt_out"] = value
        self.fixture.config_path.write_text(yaml.safe_dump(document))

    def _write_exemptions(
        self, rows: str, header: str = "analysis_id\treason\n"
    ) -> Path:
        path = self.fixture.root / "maf-filter-exemptions.tsv"
        path.write_text(header + rows, encoding="utf-8")
        return path

    def _write_metadata_yaml(self, name: str, technologies: object) -> Path:
        path = self.fixture.root / name
        document: dict[str, object] = {
            "genome_assembly": "GRCh38",
            "is_harmonised": True,
        }
        if technologies is not None:
            document["genotyping_technology"] = technologies
        path.write_text(yaml.safe_dump(document), encoding="utf-8")
        return path

    def test_declared_usable_analysis_emits_threshold_and_triple(self) -> None:
        analysis_id = "GCST90000001"
        self._declare_scores([{
            "analysis_id": analysis_id,
            "imputation_score_column": "imputation_INFO",
            "imputation_score_kind": "imputation_info",
            "imputation_score_provenance": "provider specification: INFO is imputation quality",
        }])
        self._set_threshold(0.8)
        self._set_outcome(analysis_id, info_score_state="filtered", info_rows_usable=1500)
        result = _run_cli(self.fixture)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        by_id = {
            row["analysis_id"]: row
            for row in _read_tsv(self.fixture.registry_root / STORE_ID / "analyses.tsv")
        }
        row = by_id[analysis_id]
        self.assertEqual(row["info_score_threshold"], "0.8")
        self.assertEqual(row["imputation_score_column"], "imputation_INFO")
        self.assertEqual(row["imputation_score_kind"], "imputation_info")
        self.assertEqual(
            row["imputation_score_provenance"],
            "provider specification: INFO is imputation quality",
        )
        # An undeclared Analysis gets literal NaN and empty triple cells.
        undeclared = by_id["GCST90000002"]
        self.assertEqual(undeclared["info_score_threshold"], "NaN")
        self.assertEqual(undeclared["imputation_score_column"], "")
        self.assertEqual(undeclared["imputation_score_kind"], "")
        self.assertEqual(undeclared["imputation_score_provenance"], "")

    def test_zero_threshold_with_usable_scores_emits_zero(self) -> None:
        analysis_id = "GCST90000001"
        self._declare_scores([{
            "analysis_id": analysis_id,
            "imputation_score_column": "imputation_INFO",
            "imputation_score_kind": "imputation_info",
            "imputation_score_provenance": "provider specification: INFO is imputation quality",
        }])
        self._set_threshold(0)
        self._set_outcome(analysis_id, info_score_state="disabled", info_rows_usable=1500)
        result = _run_cli(self.fixture)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        by_id = {
            row["analysis_id"]: row
            for row in _read_tsv(self.fixture.registry_root / STORE_ID / "analyses.tsv")
        }
        # Zero is a real requested floor, emitted as "0", not treated as absence.
        self.assertEqual(by_id[analysis_id]["info_score_threshold"], "0")
        self.assertEqual(by_id[analysis_id]["imputation_score_column"], "imputation_INFO")

    # -- MAF threshold (issue #176) -------------------------------------------

    def test_maf_threshold_configuration_parsing(self) -> None:
        def loaded():
            return load_candidate_configuration(self.fixture.config_path, REPO_ROOT)

        self._set_maf_threshold(None)
        self.assertIsNone(loaded().maf_threshold)
        for value, expected in ((0, "0"), ("0", "0"), (0.005, "0.005"), ("0.5", "0.5")):
            self._set_maf_threshold(value)
            self.assertEqual(loaded().maf_threshold, expected)
        for invalid in (True, "", "NaN", "inf", "-0.01", "0.5001", "abc"):
            self._set_maf_threshold(invalid)
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError, "maf_threshold"
            ):
                loaded()
        self._set_maf_threshold(0.005)
        self._set_maf_exempt(None)
        self.assertEqual(loaded().maf_filter_exempt_genotyping_technologies, ())
        self._set_maf_exempt([" Whole genome sequencing "])
        self.assertEqual(
            loaded().maf_filter_exempt_genotyping_technologies, ("Whole genome sequencing",)
        )
        for invalid in ("Whole genome sequencing", [""], [1], ["ok", ""]):
            self._set_maf_exempt(invalid)
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError, "maf_filter_exempt_genotyping_technologies"
            ):
                loaded()

    def test_maf_threshold_is_per_analysis_from_metadata_yaml(self) -> None:
        # Omitted defaults.maf_threshold means no MAF filter at all.
        base = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        self.assertIsNone(base.maf_threshold)
        self.assertEqual(base.maf_filter_exempt_genotyping_technologies, ())

        self._set_maf_threshold(0.005)
        self._set_maf_exempt(["Whole genome sequencing", "Exome-wide sequencing"])
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        self.assertEqual(config.maf_threshold, "0.005")
        self.assertEqual(
            config.maf_filter_exempt_genotyping_technologies,
            ("Whole genome sequencing", "Exome-wide sequencing"),
        )
        missing = _synthetic_inventory_row()
        absent_yaml = self.fixture.root / "absent.yaml"
        self.assertEqual(
            read_genotyping_technologies(str(absent_yaml)), []
        )
        # Missing technology metadata is never an exemption.
        self.assertEqual(
            analysis_maf_threshold(replace(missing, yaml_file=str(absent_yaml)), config),
            "0.005",
        )
        exempt_path = self._write_metadata_yaml("wgs.yaml", ["Whole genome sequencing"])
        self.assertEqual(
            read_genotyping_technologies(str(exempt_path)), ["Whole genome sequencing"]
        )
        exempt = replace(missing, yaml_file=str(exempt_path))
        # Every technology in the exempt list -> NaN (no MAF filter).
        self.assertEqual(analysis_maf_threshold(exempt, config), "NaN")
        # An empty technology list is not "all in the list".
        empty_path = self._write_metadata_yaml("empty.yaml", [])
        self.assertEqual(
            analysis_maf_threshold(replace(missing, yaml_file=str(empty_path)), config),
            "0.005",
        )
        # One technology outside the list -> not exempt.
        mixed_path = self._write_metadata_yaml(
            "mixed.yaml", ["Whole genome sequencing", "Genome-wide genotyping array"]
        )
        self.assertEqual(
            analysis_maf_threshold(replace(missing, yaml_file=str(mixed_path)), config),
            "0.005",
        )
        # A no-MAF-filter release ignores exemptions entirely.
        self._set_maf_threshold(None)
        no_filter = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        self.assertEqual(analysis_maf_threshold(exempt, no_filter), "NaN")

    def test_maf_threshold_emission_is_per_analysis_evidence(self) -> None:
        declared = ResolverRow(
            "GCST1", "/tmp/a.h.tsv.gz", "opengwasdb.gwas-ssf", "sd",
            "estimated_from_source_maf", "100", "a" * 64, "sha256", "10",
            maf_threshold="0.005",
        )
        matching = {
            "diagnostics": {"maf_state": "filtered"},
            "fingerprints": {"resolution_config": {"maf_threshold": 0.005}},
        }
        self.assertEqual(maf_threshold_emission(declared, matching), "0.005")
        self.assertEqual(maf_threshold_emission(None, matching), "NaN")
        self.assertEqual(
            maf_threshold_emission(replace(declared, maf_threshold="NaN"), matching),
            "NaN",
        )
        # A record that did not apply MAF (unavailable), or one without the
        # #176 diagnostics, has no evidence: NaN.
        unavailable = {
            "diagnostics": {"maf_state": "unavailable"},
            "fingerprints": {"resolution_config": {"maf_threshold": 0.005}},
        }
        self.assertEqual(maf_threshold_emission(declared, unavailable), "NaN")
        self.assertEqual(maf_threshold_emission(declared, {"diagnostics": {}}), "NaN")
        # A fingerprint that no longer binds the value emits NaN.
        stale = {
            "diagnostics": {"maf_state": "filtered"},
            "fingerprints": {"resolution_config": {"maf_threshold": 0.01}},
        }
        self.assertEqual(maf_threshold_emission(declared, stale), "NaN")
        # Zero is a real requested floor, emitted as "0", not treated as absence.
        zero = replace(declared, maf_threshold="0")
        disabled = {
            "diagnostics": {"maf_state": "disabled"},
            "fingerprints": {"resolution_config": {"maf_threshold": 0}},
        }
        self.assertEqual(maf_threshold_emission(zero, disabled), "0")
        self.assertEqual(USABLE_MAF_STATES, frozenset({"disabled", "filtered"}))

    def test_maf_threshold_manifest_receipt_and_candidate_emission(self) -> None:
        self._set_maf_threshold(0.005)
        result = _run_cli(self.fixture)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        run_root = self.fixture.work_root / STORE_ID / "resolver"
        self.assertIn("maf_threshold", RESOLVER_MANIFEST_COLUMNS)
        manifest = _read_tsv(run_root / "analyses.tsv")
        self.assertEqual({row["maf_threshold"] for row in manifest}, {"0.005"})
        record = json.loads(_kept_record(self.fixture, "GCST90000001").read_text())
        self.assertEqual(
            record["fingerprints"]["resolution_config"]["maf_threshold"], 0.005
        )
        self.assertEqual(record["diagnostics"]["maf_state"], "filtered")
        emitted = _read_tsv(self.fixture.registry_root / STORE_ID / "analyses.tsv")
        self.assertEqual({row["maf_threshold"] for row in emitted}, {"0.005"})
        receipt = read_resolution_receipt(run_root / RECEIPT_FILENAME)
        self.assertEqual(receipt["contract"]["maf_threshold"], "0.005")
        self.assertEqual(
            receipt["contract"]["maf_filter_exempt_genotyping_technologies"], []
        )

    def test_maf_threshold_requires_resolver_fingerprint(self) -> None:
        self._set_maf_threshold(0.005)
        result = _run_cli(self.fixture)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        def _drop(record: dict) -> None:
            del record["fingerprints"]["resolution_config"]['maf_threshold']
            record["fingerprints"]["fingerprint_digest"] = _fingerprint_digest(
                record["fingerprints"]
            )

        record_path = _checkpoint(self.fixture) / "GCST90000001.json"
        record = json.loads(record_path.read_text())
        _drop(record)
        record_path.write_text(json.dumps(record))
        _forge_kept_record(self.fixture, "GCST90000001", _drop)
        inventory = read_inventory(self.fixture.inventory_path)
        metadata = read_analysis_metadata(self.fixture.provenance_path,
            (row.analysis_id for row in inventory if row.ready),
        )
        manifest = derive_resolver_manifest(
            inventory,
            load_candidate_configuration(self.fixture.config_path, REPO_ROOT),
            metadata,
        )
        _, failures = account_records(manifest, _checkpoint(self.fixture))
        self.assertTrue(
            any("resolution_config.maf_threshold" in error for error in failures), failures
        )
        stale = _run_cli(self.fixture, "--stage", "emit")
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("resolution_config.maf_threshold", stale.stderr + stale.stdout)

    def test_changed_maf_threshold_makes_the_receipt_stale(self) -> None:
        self._set_maf_threshold(0.005)
        self._resolved_candidate()
        self._set_maf_threshold(0.01)
        self._assert_emit_rejects_and_preserves("resolution contract changed")

    def test_maf_filter_exemption_file_parsing(self) -> None:
        # An absent path is no exemptions, never an error.
        self.assertEqual(read_maf_filter_exempt_analyses(None), {})
        good = self._write_exemptions(
            "GCST90428462\tplaceholder frequency one\n"
            "GCST90428463\tplaceholder frequency two\n"
        )
        self.assertEqual(
            read_maf_filter_exempt_analyses(good),
            {
                "GCST90428462": "placeholder frequency one",
                "GCST90428463": "placeholder frequency two",
            },
        )
        duplicate = self._write_exemptions("GCST1\tone\nGCST1\ttwo\n")
        with self.assertRaises(CandidateError) as caught:
            read_maf_filter_exempt_analyses(duplicate)
        self.assertIn(f"{duplicate}:3: duplicate analysis_id", str(caught.exception))
        blank_id = self._write_exemptions("\treason\n")
        with self.assertRaises(CandidateError) as caught:
            read_maf_filter_exempt_analyses(blank_id)
        self.assertIn(f"{blank_id}:2: analysis_id must be non-empty", str(caught.exception))
        blank_reason = self._write_exemptions("GCST1\t \n")
        with self.assertRaises(CandidateError) as caught:
            read_maf_filter_exempt_analyses(blank_reason)
        self.assertIn(f"{blank_reason}:2: reason must be non-empty", str(caught.exception))
        bad_header = self._write_exemptions(
            "GCST1\treason\n", header="analysis_id\trationale\n"
        )
        with self.assertRaises(CandidateError) as caught:
            read_maf_filter_exempt_analyses(bad_header)
        self.assertIn(f"{bad_header}: expected exact TSV headers", str(caught.exception))
        missing = self.fixture.root / "absent-exemptions.tsv"
        with self.assertRaises(CandidateError) as caught:
            read_maf_filter_exempt_analyses(missing)
        self.assertIn(f"not found: {missing}", str(caught.exception))

    def test_committed_maf_filter_exemptions_resolve_against_the_repo_root(self) -> None:
        # `release_policy.maf_filter_exempt_analyses` is a repo-root-relative path and the
        # committed table records the operator's reason for each exemption.
        self._set_maf_exempt_analyses(
            "resources/generators/gwas-catalog-eur-hybrid/maf-filter-exemptions.tsv"
        )
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        self.assertEqual(
            config.maf_filter_exempt_analyses_path,
            REPO_ROOT / "resources/generators/gwas-catalog-eur-hybrid/maf-filter-exemptions.tsv",
        )
        self.assertEqual(
            set(config.maf_filter_exempt_analyses), {"GCST90428462", "GCST90428463"}
        )
        for analysis_id in ("GCST90428462", "GCST90428463"):
            reason = config.maf_filter_exempt_analyses[analysis_id]
            self.assertIn("effect_allele_frequency is 0.0 on every row", reason)
            self.assertIn("minor_allele_freq_lower_limit 0.01", reason)

    def test_listed_analysis_emits_nan_maf_threshold_in_manifest_and_receipt(self) -> None:
        self._set_maf_threshold(0.005)
        exempt = self._write_exemptions("GCST90000001\tplaceholder frequency (fixture)\n")
        self._set_maf_exempt_analyses(str(exempt))
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        rows = read_inventory(self.fixture.inventory_path)
        metadata = read_analysis_metadata(self.fixture.provenance_path, (row.analysis_id for row in rows if row.ready)
        )
        manifest = derive_resolver_manifest(rows, config, metadata)
        by_id = {row.analysis_id: row for row in manifest}
        # The exempt Analysis carries the NaN the manifest already understands;
        # every other Analysis keeps the configured floor.
        self.assertEqual(by_id["GCST90000001"].maf_threshold, "NaN")
        self.assertEqual(by_id["GCST90000002"].maf_threshold, "0.005")
        result = _run_cli(self.fixture)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        emitted = {
            row["analysis_id"]: row
            for row in _read_tsv(self.fixture.registry_root / STORE_ID / "analyses.tsv")
        }
        # No resolver evidence for an exempt Analysis -> literal NaN, as for an
        # exempt genotyping technology.
        self.assertEqual(emitted["GCST90000001"]["maf_threshold"], "NaN")
        self.assertEqual(emitted["GCST90000002"]["maf_threshold"], "0.005")
        receipt = read_resolution_receipt(
            self.fixture.work_root / STORE_ID / "resolver" / RECEIPT_FILENAME
        )
        self.assertEqual(receipt["contract"]["maf_filter_exempt_analyses"], str(exempt))
        self.assertEqual(
            receipt["contract"]["maf_filter_exempt_analyses_reasons"],
            {"GCST90000001": "placeholder frequency (fixture)"},
        )

    def test_changed_maf_exemption_makes_the_receipt_stale(self) -> None:
        self._set_maf_threshold(0.005)
        exempt = self._write_exemptions("GCST90000001\tfirst reason\n")
        self._set_maf_exempt_analyses(str(exempt))
        self._resolved_candidate()
        self._write_exemptions("GCST90000001\tsecond reason\n")
        self._assert_emit_rejects_and_preserves("resolution contract changed")

    def test_declared_analysis_with_no_usable_scores_is_included_with_nan_info(self) -> None:
        # Core no longer treats zero usable scores as a controlled failure, and
        # the registry no longer refuses the Analysis (#176): every row is
        # retained and the emitted INFO cells are literal NaN / empty.
        analysis_id = "GCST90000001"
        self._declare_scores([{
            "analysis_id": analysis_id,
            "imputation_score_column": "imputation_INFO",
            "imputation_score_kind": "imputation_info",
            "imputation_score_provenance": "provider specification: INFO is imputation quality",
        }])
        self._set_outcome(analysis_id, info_rows_usable=0)
        result = _run_cli(self.fixture)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        exclusions = {
            row["analysis_id"]: row
            for row in _read_tsv(self.fixture.registry_root / STORE_ID / "sidecars/exclusions.tsv")
        }
        self.assertNotIn(analysis_id, exclusions)
        by_id = {
            row["analysis_id"]: row
            for row in _read_tsv(self.fixture.registry_root / STORE_ID / "analyses.tsv")
        }
        self.assertEqual(by_id[analysis_id]["exclude_from_build"], "")
        self.assertEqual(by_id[analysis_id]["info_score_threshold"], "NaN")
        self.assertEqual(by_id[analysis_id]["imputation_score_column"], "")
        self.assertEqual(by_id[analysis_id]["imputation_score_kind"], "")
        self.assertEqual(by_id[analysis_id]["imputation_score_provenance"], "")

    def test_info_score_emission_is_per_analysis_evidence(self) -> None:
        declared = ResolverRow(
            "GCST1", "/tmp/a.h.tsv.gz", "opengwasdb.gwas-ssf", "sd",
            "estimated_from_source_maf", "100", "a" * 64, "sha256", "10",
            info_score_threshold="0.6", imputation_score_column="imputation_INFO",
            imputation_score_kind="imputation_info",
            imputation_score_provenance="provider: INFO quality",
        )
        matching_fingerprint = {
            "resolution_config": {
                "info_score_threshold": 0.6,
                "imputation_score_column": "imputation_INFO",
                "imputation_score_kind": "imputation_info",
                "imputation_score_provenance": "provider: INFO quality",
            }
        }
        usable = {
            "diagnostics": {"info_score_state": "filtered", "info_rows_usable": 5},
            "fingerprints": matching_fingerprint,
        }
        self.assertEqual(
            info_score_emission(declared, usable),
            ("0.6", "imputation_INFO", "imputation_info", "provider: INFO quality"),
        )
        # Undeclared: NaN whatever the record says.
        undeclared = replace(
            declared, info_score_threshold="NaN", imputation_score_column="",
            imputation_score_kind="", imputation_score_provenance="",
        )
        self.assertEqual(info_score_emission(undeclared, usable), ("NaN", "", "", ""))
        # Legacy record without the #175 diagnostics: NaN, never read as a pass.
        self.assertEqual(
            info_score_emission(declared, {"diagnostics": {"rows_read": 100}}),
            ("NaN", "", "", ""),
        )
        # Declared but no usable score: NaN.
        self.assertEqual(
            info_score_emission(
                declared,
                {"diagnostics": {"info_score_state": "filtered", "info_rows_usable": 0},
                 "fingerprints": matching_fingerprint},
            ),
            ("NaN", "", "", ""),
        )
        # Declared and usable, but the fingerprint no longer binds it: NaN.
        stale = {
            "diagnostics": {"info_score_state": "filtered", "info_rows_usable": 5},
            "fingerprints": {"resolution_config": {"info_score_threshold": 0.6}},
        }
        self.assertEqual(info_score_emission(declared, stale), ("NaN", "", "", ""))

    def test_declared_score_manifest_receipt_and_candidate_handoff(self) -> None:
        analysis_id = "GCST90000004"
        path = self._declare_scores([{
            "analysis_id": analysis_id,
            "imputation_score_column": "imputation_INFO",
            "imputation_score_kind": "imputation_info",
            "imputation_score_provenance": "provider specification: INFO is imputation quality",
        }])
        document = yaml.safe_load(self.fixture.config_path.read_text())
        document["defaults"]["info_score_threshold"] = 0
        self.fixture.config_path.write_text(yaml.safe_dump(document))
        result = _run_cli(self.fixture)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        run_root = self.fixture.work_root / STORE_ID / "resolver"
        manifest = _read_tsv(run_root / "analyses.tsv")
        self.assertEqual(tuple(manifest[0]), RESOLVER_MANIFEST_COLUMNS)
        by_id = {row["analysis_id"]: row for row in manifest}
        declared = by_id[analysis_id]
        self.assertEqual(declared["info_score_threshold"], "0")
        self.assertEqual(declared["imputation_score_column"], "imputation_INFO")
        self.assertEqual(declared["imputation_score_kind"], "imputation_info")
        self.assertEqual(declared["imputation_score_provenance"],
                         "provider specification: INFO is imputation quality")
        self.assertEqual(by_id["GCST90000001"]["info_score_threshold"], "NaN")
        self.assertEqual(by_id["GCST90000001"]["imputation_score_column"], "")
        record = json.loads(_kept_record(self.fixture, analysis_id).read_text())
        self.assertEqual(record["fingerprints"]["resolution_config"]["imputation_score_column"],
                         "imputation_INFO")
        receipt = read_resolution_receipt(run_root / RECEIPT_FILENAME)
        self.assertEqual(receipt["contract"]["imputation_score_declarations_sha256"],
                         hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(receipt["manifest_sha256"],
                         hashlib.sha256((run_root / "analyses.tsv").read_bytes()).hexdigest())
        final = _read_tsv(self.fixture.registry_root / STORE_ID / "analyses.tsv")
        by_id = {row["analysis_id"]: row for row in final}
        # An approved declaration with usable resolver evidence emits the exact
        # requested floor and triple; the undeclared Analysis stays NaN.
        self.assertEqual(by_id[analysis_id]["info_score_threshold"], "0")
        self.assertEqual(by_id[analysis_id]["imputation_score_column"], "imputation_INFO")
        self.assertEqual(by_id[analysis_id]["imputation_score_kind"], "imputation_info")
        self.assertEqual(
            by_id[analysis_id]["imputation_score_provenance"],
            "provider specification: INFO is imputation quality",
        )
        self.assertEqual(by_id["GCST90000001"]["info_score_threshold"], "NaN")
        self.assertEqual(by_id["GCST90000001"]["imputation_score_column"], "")
        self.assertEqual(_run_cli(self.fixture, "--stage", "verify").returncode, 0)
        path.write_text(path.read_text().replace("imputation_INFO", "imputation_R2"))
        stale = _run_cli(self.fixture, "--stage", "verify")
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("resolution contract changed", stale.stderr + stale.stdout)

    def test_declared_score_requires_resolver_fingerprint(self) -> None:
        analysis_id = "GCST90000004"
        self._declare_scores([{
            "analysis_id": analysis_id, "imputation_score_column": "imputation_r2",
            "imputation_score_kind": "imputation_r2",
            "imputation_score_provenance": "provider dictionary: imputation r2",
        }])
        result = _run_cli(self.fixture)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        # Both the checkpoint record a fresh run accounts and the kept record a
        # later emit reads lose the key.
        def _drop(record: dict) -> None:
            del record["fingerprints"]["resolution_config"]['imputation_score_kind']
            record["fingerprints"]["fingerprint_digest"] = _fingerprint_digest(
                record["fingerprints"]
            )

        record_path = _checkpoint(self.fixture) / f"{analysis_id}.json"
        record = json.loads(record_path.read_text())
        _drop(record)
        record_path.write_text(json.dumps(record))
        _forge_kept_record(self.fixture, analysis_id, _drop)
        inventory = read_inventory(self.fixture.inventory_path)
        metadata = read_analysis_metadata(self.fixture.provenance_path,
                                           (row.analysis_id for row in inventory if row.ready))
        manifest = derive_resolver_manifest(inventory, load_candidate_configuration(
            self.fixture.config_path, REPO_ROOT), metadata)
        _, failures = account_records(manifest, _checkpoint(self.fixture))
        self.assertTrue(any("resolution_config.imputation_score_kind" in error
                            for error in failures), failures)
        stale = _run_cli(self.fixture, "--stage", "emit")
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("resolution_config.imputation_score_kind", stale.stderr + stale.stdout)

    def test_score_declaration_validation_and_legacy_manifest(self) -> None:
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        rows = read_inventory(self.fixture.inventory_path)
        metadata = read_analysis_metadata(self.fixture.provenance_path,
                                           (row.analysis_id for row in rows if row.ready))
        legacy = derive_resolver_manifest(rows, config, metadata)
        self.assertEqual({row.info_score_threshold for row in legacy}, {"NaN"})
        self.assertEqual({row.imputation_score_kind for row in legacy}, {""})
        valid = {"analysis_id": "GCST90000001", "imputation_score_column": "r2",
                 "imputation_score_kind": "imputation_r2",
                 "imputation_score_provenance": "provider data dictionary: imputation R2"}
        for broken, message in (
            ([{**valid, "analysis_id": "unknown"}], "unknown analysis_id"),
            ([valid, valid], "duplicate analysis_id"),
            ([{**valid, "imputation_score_column": " r2"}], "exact non-empty"),
            ([{**valid, "imputation_score_kind": "MAF"}], "invalid imputation_score_kind"),
            ([{**valid, "imputation_score_provenance": ""}], "independent provider evidence"),
        ):
            with self.subTest(broken=broken):
                self._declare_scores(broken)
                configured = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
                with self.assertRaisesRegex(CandidateError, message):
                    derive_resolver_manifest(rows, configured, metadata)
        path = self._declare_scores([valid])
        path.write_text(path.read_text().replace("imputation_score_kind", "score_kind"))
        with self.assertRaisesRegex(CandidateError, "exact TSV headers"):
            derive_resolver_manifest(rows, load_candidate_configuration(
                self.fixture.config_path, REPO_ROOT), metadata)
        path.unlink()
        with self.assertRaisesRegex(CandidateError, "table not found"):
            derive_resolver_manifest(rows, load_candidate_configuration(
                self.fixture.config_path, REPO_ROOT), metadata)

    def _trial_selection(self, analysis_ids: list[str]) -> tuple:
        """A second Selection of the fixture's collection, frozen; (config, rows, metadata)."""
        document = yaml.safe_load(self.fixture.config_path.read_text())
        document["selection"] = {"analyses": analysis_ids}
        document["source_collection"]["inventory"]["snapshot_id"] = "fixture-trial"
        self.fixture.config_path.write_text(yaml.safe_dump(document))
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        freeze_release(config.base, frozen_at=FROZEN_AT)
        rows = read_inventory(config.base.inventory_path)
        metadata = read_analysis_metadata(
            config.base.inventory_provenance_path, [row.analysis_id for row in rows if row.ready]
        )
        return config, rows, metadata

    def test_score_declarations_are_the_collections_and_apply_to_the_selection(self) -> None:
        """A shared declarations table names Analyses other Selections take (#287 review)."""
        declaration = {
            "imputation_score_column": "r2", "imputation_score_kind": "imputation_r2",
            "imputation_score_provenance": "provider data dictionary: imputation R2",
        }
        self._declare_scores([
            {"analysis_id": "GCST90000001", **declaration},
            # In the collection, outside this Selection: another release's.
            {"analysis_id": "GCST90000002", **declaration},
        ])
        config, rows, metadata = self._trial_selection(["GCST90000001", "GCST90000004"])
        manifest = {row.analysis_id: row for row in derive_resolver_manifest(rows, config, metadata)}
        self.assertEqual(set(manifest), {"GCST90000001", "GCST90000004"})
        self.assertEqual(manifest["GCST90000001"].imputation_score_column, "r2")
        self.assertEqual(manifest["GCST90000004"].imputation_score_column, "")

        # An Analysis the collection does not intend is still refused.
        self._declare_scores([{"analysis_id": "GCST99999999", **declaration}])
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        with self.assertRaisesRegex(CandidateError, "unknown analysis_id 'GCST99999999'"):
            derive_resolver_manifest(rows, config, metadata)

        # Without the collection's Source Metadata Table, a declaration outside
        # the Selection cannot be checked, so it is refused rather than ignored.
        self._declare_scores([{"analysis_id": "GCST90000002", **declaration}])
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        self.fixture.source_metadata_table_path.rename(self.fixture.root / "moved.tsv")
        with self.assertRaisesRegex(CandidateError, "collection's Source Metadata Table"):
            derive_resolver_manifest(rows, config, metadata)

    def test_worker_count_byte_equivalence(self) -> None:
        single_root = self.fixture.root / "stores-1"
        multi_root = self.fixture.root / "stores-4"
        single_root.mkdir()
        multi_root.mkdir()
        one = _run_cli(
            self.fixture, cores=1, registry_root=single_root, work_root=self.fixture.root / "work-1",
            resolution_root=self.fixture.root / "resolution-1",
        )
        self.assertEqual(one.returncode, 0, one.stderr)
        four = _run_cli(
            self.fixture, cores=4, registry_root=multi_root, work_root=self.fixture.root / "work-4",
            resolution_root=self.fixture.root / "resolution-4",
        )
        self.assertEqual(four.returncode, 0, four.stderr)
        self.assertEqual(_candidate_tables_bytes(single_root), _candidate_tables_bytes(multi_root))

    def test_resume_reuses_records_and_reproduces_bytes(self) -> None:
        first = _run_cli(self.fixture)
        self.assertEqual(first.returncode, 0, first.stderr)
        before = _candidate_tables_bytes(self.fixture.registry_root)

        second = _run_cli(self.fixture)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("--resume", second.stdout)
        after = _candidate_tables_bytes(self.fixture.registry_root)
        self.assertEqual(before, after)

        index = json.loads(
            (self.fixture.work_root / STORE_ID / "resolver/index.json").read_text()
        )
        self.assertGreater(index["n_resumed"], 0)

    def test_kill_then_resume_matches_uninterrupted_run(self) -> None:
        ok_root = self.fixture.root / "stores-ok"
        ok_root.mkdir()
        ok = _run_cli(
            self.fixture, registry_root=ok_root, work_root=self.fixture.root / "work-ok",
            resolution_root=self.fixture.root / "resolution-ok",
        )
        self.assertEqual(ok.returncode, 0, ok.stderr)
        expected = _candidate_tables_bytes(ok_root)

        resume_root = self.fixture.root / "stores-resume"
        resume_root.mkdir()
        resume_work = self.fixture.root / "work-resume"
        killed = _run_cli(
            self.fixture,
            registry_root=resume_root,
            work_root=resume_work,
            resolution_root=self.fixture.root / "resolution-resume",
            fail_after=3,
        )
        self.assertNotEqual(killed.returncode, 0)
        self.assertFalse((resume_root / STORE_ID).exists())

        resumed = _run_cli(
            self.fixture,
            registry_root=resume_root,
            work_root=resume_work,
            resolution_root=self.fixture.root / "resolution-resume",
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(_candidate_tables_bytes(resume_root), expected)
        index = json.loads(
            (resume_work / STORE_ID / "resolver/index.json").read_text()
        )
        self.assertEqual(index["n_resumed"], 3)

    def test_re_emitting_the_candidate_is_byte_identical_in_every_file(self) -> None:
        """`--stage emit` over its own candidate rewrites every file byte for byte (#248)."""
        self.assertEqual(_run_cli(self.fixture).returncode, 0)
        bundle_dir = self.fixture.registry_root / STORE_ID

        def every_file() -> dict[str, bytes]:
            return {str(p.relative_to(bundle_dir)): p.read_bytes() for p in sorted(bundle_dir.rglob("*")) if p.is_file()}

        pinned = ("--stage", "emit", "--created-at", "2026-10-14T00:00:00Z")
        first = _run_cli(self.fixture, *pinned)
        self.assertEqual(first.returncode, 0, first.stderr)
        emitted = every_file()
        second = _run_cli(self.fixture, *pinned)
        self.assertEqual(second.returncode, 0, second.stderr)

        self.assertIn("release.yaml", emitted)
        self.assertIn("summary.yaml", emitted)
        self.assertEqual(every_file(), emitted)

    def test_interrupted_run_preserves_prior_candidate(self) -> None:
        first = _run_cli(self.fixture)
        self.assertEqual(first.returncode, 0, first.stderr)
        before = _candidate_tables_bytes(self.fixture.registry_root)
        release_before = (self.fixture.registry_root / STORE_ID / "release.yaml").read_bytes()

        # A changed gate forces every Analysis to be re-resolved, so the
        # interruption lands mid-run rather than among reused records.
        document = yaml.safe_load(self.fixture.config_path.read_text(encoding="utf-8"))
        document["ancestry_assignment"]["gates"]["tau"] = 0.6
        self.fixture.config_path.write_text(yaml.safe_dump(document, sort_keys=False))
        interrupted = _run_cli(self.fixture, fail_after=2)
        self.assertNotEqual(interrupted.returncode, 0)
        self.assertIn("resolver exited", interrupted.stderr)

        self.assertEqual(_candidate_tables_bytes(self.fixture.registry_root), before)
        self.assertEqual(
            (self.fixture.registry_root / STORE_ID / "release.yaml").read_bytes(), release_before
        )

    def test_failed_finalisation_does_not_create_a_candidate(self) -> None:
        result = _run_cli(self.fixture, fail_after=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.fixture.registry_root / STORE_ID).exists())

    # -- accounting ------------------------------------------------------------

    # -- Trait Ontology Mapping (#286) -------------------------------------------

    def _rewrite_candidate_traits(self, traits: dict[str, tuple[str, str, str]]) -> None:
        """Set (source_trait_label, source_trait_ontology_id, source_trait_ontology_label)
        for some Analyses in the Source Metadata Table, and freeze it again."""
        _rewrite_source_metadata(
            self.fixture,
            {
                analysis_id: {
                    "source_trait_label": label,
                    "source_trait_ontology_id": uri,
                    "source_trait_ontology_label": term_label,
                }
                for analysis_id, (label, uri, term_label) in traits.items()
            },
        )

    def test_emitted_trait_columns_come_from_the_trait_ontology_mapping_step(self) -> None:
        self._rewrite_candidate_traits(
            {
                "GCST90000001": (
                    "Trait GCST90000001",
                    "http://www.ebi.ac.uk/efo/EFO_0004340, http://purl.obolibrary.org/obo/MONDO_0005148",
                    "body mass index, type 2 diabetes mellitus",
                ),
                "GCST90000002": ("Weight", "", ""),
                "GCST90000003": ("Trait GCST90000003", "", ""),
                "GCST90000004": (
                    "Trait GCST90000004",
                    "http://www.orpha.net/ORDO/Orphanet_101960",
                    "genetic chronic primary adrenal insufficiency",
                ),
            }
        )
        result = _run_cli(self.fixture)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        bundle_dir = self.fixture.registry_root / STORE_ID
        by_id = {row["analysis_id"]: row for row in _read_tsv(bundle_dir / "analyses.tsv")}

        def trait(analysis_id: str) -> tuple[str, str, str]:
            row = by_id[analysis_id]
            return (
                row["trait_ontology_id"],
                row["trait_ontology_label"],
                row["trait_ontology_mapping_method"],
            )

        self.assertEqual(
            trait("GCST90000001"),
            (
                "EFO:0004340, MONDO:0005148",
                "body mass index, type 2 diabetes mellitus",
                "source_provided",
            ),
        )
        self.assertEqual(trait("GCST90000002"), ("EFO:0004338", "body weight", "canonical_table_lookup"))
        self.assertEqual(trait("GCST90000003"), ("", "", "unmapped"))
        self.assertEqual(
            trait("GCST90000004"),
            ("Orphanet:101960", "genetic chronic primary adrenal insufficiency", "source_provided"),
        )
        self.assertEqual(
            trait("GCST90000005"), ("MONDO:0005148", "mapped GCST90000005", "source_provided")
        )

        validation = yaml.safe_load((bundle_dir / "validation.yaml").read_text(encoding="utf-8"))
        self.assertEqual(
            validation["trait_ontology_mapping"],
            {
                "canonical_trait_mapping_table": {
                    "resource_id": TRAIT_TABLE_RESOURCE_ID,
                    "version": 1,
                    "location": f"resources/reference-resources/{TRAIT_TABLE_RESOURCE_ID}/mapping.tsv",
                    "sha256": TRAIT_TABLE_SHA256,
                    "rows": 1,
                },
                "method_counts": {
                    "source_provided": len(READY_ANALYSES) - 2,
                    "canonical_table_lookup": 1,
                    "unmapped": 1,
                },
                "source_id_without_label": 0,
                "rejected_source_terms": [],
            },
        )

    def test_trait_ontology_mapping_runs_after_verification(self) -> None:
        """An unknown source form does not block resolution; it fails the emit, by name."""
        self._rewrite_candidate_traits(
            {"GCST90000002": ("Trait", "https://example.org/trait/7", "a trait")}
        )
        resolved = _run_cli(self.fixture, "--stage", "resolve")
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        emitted = self._run_emit_only()
        self.assertNotEqual(emitted.returncode, 0)
        self.assertIn("GCST90000002", emitted.stderr)
        self.assertIn("'https://example.org/trait/7'", emitted.stderr)
        self.assertFalse((self.fixture.registry_root / STORE_ID).exists())

    def test_a_row_without_a_trait_ontology_mapping_is_refused(self) -> None:
        row = _synthetic_inventory_row()
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        unmapped = replace(_synthetic_analysis_metadata(), trait_ontology=None)
        outcomes = apply_release_policy(
            [row], config, {row.analysis_id: unmapped}, {row.analysis_id: _synthetic_resolver_record()},
        )
        with self.assertRaisesRegex(CandidateError, "Trait Ontology Mapping"):
            build_candidate_tables(
                inventory_rows=[row], outcomes=outcomes, config=config, index_summary={}
            )

    def _candidate_metadata(self) -> dict:
        rows = read_inventory(self.fixture.inventory_path)
        return read_analysis_metadata(
            self.fixture.provenance_path, [row.analysis_id for row in rows if row.ready]
        )

    def test_the_source_term_comes_from_the_frozen_source_metadata_table(self) -> None:
        """#286/#287: the step reads the Source Metadata Table frozen with the inventory."""
        self._rewrite_candidate_traits(
            {
                "GCST90000001": ("Weight", "http://www.ebi.ac.uk/efo/EFO_0004340", "body mass index"),
                "GCST90000002": ("weight", "", ""),
                "GCST90000003": ("Trait GCST90000003", "", ""),
            }
        )
        metadata = self._candidate_metadata()
        sources = trait_sources(metadata)
        self.assertEqual(
            {source.analysis_id: source for source in sources}["GCST90000001"],
            SourceTrait("GCST90000001", "Weight", "http://www.ebi.ac.uk/efo/EFO_0004340", "body mass index"),
        )
        mapped, _ = map_candidate_trait_ontologies(
            metadata, load_canonical_trait_mapping_table(self.fixture.trait_table_dir)
        )
        self.assertEqual(
            {key: (m.trait_ontology.trait_ontology_id, m.trait_ontology.method) for key, m in mapped.items()
             if key in ("GCST90000001", "GCST90000002", "GCST90000003", "GCST90000004")},
            {
                "GCST90000001": ("EFO:0004340", "source_provided"),
                "GCST90000002": ("EFO:0004338", "canonical_table_lookup"),
                "GCST90000003": ("", "unmapped"),
                "GCST90000004": ("MONDO:0005148", "source_provided"),
            },
        )

    def test_every_metadata_field_comes_from_the_frozen_source_metadata_table(self) -> None:
        metadata = self._candidate_metadata()
        self.assertEqual(
            metadata["GCST90000004"],
            AnalysisMetadata(
                analysis_id="GCST90000004",
                source_label="Trait GCST90000004",
                source_trait_ontology_id="http://purl.obolibrary.org/obo/MONDO_0005148",
                source_trait_ontology_label="mapped GCST90000004",
                publication_pmid="12345678",
                first_author="Author A",
                n_cases="4000",
                n_controls="4000",
                sample_size="8000",
                source_ancestry_label="European",
                source_superpopulation="EUR",
            ),
        )

    def test_an_inventory_frozen_without_its_connectors_is_refused(self) -> None:
        """The candidate-table freeze recorded no frozen connectors: nothing to read (#287)."""
        provenance = yaml.safe_load(self.fixture.provenance_path.read_text())
        del provenance["source_metadata_table"]
        legacy = self.fixture.root / "legacy.meta.yaml"
        legacy.write_text(yaml.safe_dump(provenance), encoding="utf-8")
        with self.assertRaisesRegex(CandidateError, "frozen Source Metadata Table"):
            read_analysis_metadata(legacy, ["GCST90000001"])

    def test_an_analysis_the_frozen_table_does_not_intend_is_refused(self) -> None:
        with self.assertRaisesRegex(CandidateError, "GCST99999999"):
            read_analysis_metadata(self.fixture.provenance_path, ["GCST90000001", "GCST99999999"])

    def test_an_edited_frozen_table_is_refused(self) -> None:
        table = self.fixture.inventory_path.with_name(f"{SNAPSHOT_ID}.source-metadata.tsv")
        table.write_text(table.read_text().replace("Author A", "Author B"), encoding="utf-8")
        with self.assertRaisesRegex(CandidateError, "edited after the freeze"):
            self._candidate_metadata()

    def _run_emit_only(self) -> subprocess.CompletedProcess:
        return _run_cli(self.fixture, "--stage", "emit")

    def test_missing_record_fails_before_replacement(self) -> None:
        self.assertEqual(_run_cli(self.fixture).returncode, 0)
        before = _candidate_tables_bytes(self.fixture.registry_root)
        _kept_record(self.fixture, "GCST90000002").unlink()
        result = self._run_emit_only()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing", result.stderr)
        self.assertEqual(_candidate_tables_bytes(self.fixture.registry_root), before)

    def test_another_selections_record_in_the_checkpoint_is_not_a_failure(self) -> None:
        # The checkpoint is shared by every Selection of the collection (#284);
        # a receipt naming a record outside its manifest is what fails
        # (SharedResolutionRecordsTests).
        self.assertEqual(_run_cli(self.fixture).returncode, 0)
        (_checkpoint(self.fixture) / "GCST99999999.json").write_text("{}", encoding="utf-8")
        result = self._run_emit_only()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_record_schema_version_mismatch_fails_before_replacement(self) -> None:
        self.assertEqual(_run_cli(self.fixture).returncode, 0)
        _forge_kept_record(
            self.fixture, "GCST90000001", lambda r: r.__setitem__("record_schema_version", 2)
        )
        result = self._run_emit_only()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("record_schema_version", result.stderr)

    def test_index_schema_version_mismatch_fails_before_replacement(self) -> None:
        self.assertEqual(_run_cli(self.fixture).returncode, 0)
        index_path = self.fixture.work_root / STORE_ID / "resolver/index.json"
        index = json.loads(index_path.read_text())
        index["record_schema_version"] = 2
        index_path.write_text(json.dumps(index), encoding="utf-8")
        result = self._run_emit_only()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("record_schema_version", result.stderr)

    # -- stale-contract (resolution receipt) -----------------------------------

    def _resolved_candidate(self) -> None:
        """Produce a successful candidate whose records and receipt are on disk."""
        result = _run_cli(self.fixture)
        self.assertEqual(result.returncode, 0, result.stderr)

    def _assert_emit_rejects_and_preserves(self, expected_message: str) -> None:
        before = _candidate_tables_bytes(self.fixture.registry_root)
        result = self._run_emit_only()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(expected_message, result.stderr)
        self.assertEqual(_candidate_tables_bytes(self.fixture.registry_root), before)

    def test_changed_gate_makes_the_receipt_stale(self) -> None:
        self._resolved_candidate()
        document = yaml.safe_load(self.fixture.config_path.read_text(encoding="utf-8"))
        document["ancestry_assignment"]["gates"]["tau"] = 0.9
        self.fixture.config_path.write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
        )
        self._assert_emit_rejects_and_preserves("resolution contract changed")

    def test_changed_ancestry_reference_makes_the_receipt_stale(self) -> None:
        self._resolved_candidate()
        reference = self.fixture.root / "reference/ref_freqs.tsv.gz"
        reference.write_bytes(reference.read_bytes() + b"# changed content\n")
        self._assert_emit_rejects_and_preserves("ancestry_reference_sha256")

    def test_changed_ancestry_groups_makes_the_receipt_stale(self) -> None:
        self._resolved_candidate()
        groups = self.fixture.root / "reference/ancestry_groups.tsv"
        groups.write_text(
            groups.read_text(encoding="utf-8") + "AFR_A\tAFR\n", encoding="utf-8"
        )
        self._assert_emit_rejects_and_preserves("ancestry_groups_sha256")

    def test_missing_receipt_fails_before_replacement(self) -> None:
        self._resolved_candidate()
        receipt = self.fixture.work_root / STORE_ID / "resolver" / RECEIPT_FILENAME
        receipt.unlink()
        self._assert_emit_rejects_and_preserves("resolution receipt is missing")

    def test_receipt_binds_every_record_digest(self) -> None:
        self._resolved_candidate()
        receipt = json.loads(
            (self.fixture.work_root / STORE_ID / "resolver" / RECEIPT_FILENAME).read_text()
        )
        self.assertEqual(list(receipt["records"]), receipt["analysis_ids"])
        for analysis_id in receipt["analysis_ids"]:
            record_path = _kept_record(self.fixture, analysis_id)
            record = json.loads(record_path.read_text())
            entry = receipt["records"][analysis_id]
            self.assertEqual(
                entry["fingerprint_digest"], record["fingerprints"]["fingerprint_digest"]
            )
            self.assertEqual(
                entry["record_sha256"], hashlib.sha256(record_path.read_bytes()).hexdigest()
            )
        self.assertIn("resolve-analyses", " ".join(receipt["resolver"]["argv"]))

    def test_tampered_fingerprint_with_valid_self_digest_is_rejected(self) -> None:
        # The exact hole review found: mutate a gate/evidence fingerprint input
        # and recompute a *valid* self-digest. The record is internally
        # consistent, so only the receipt can reject it.
        self._resolved_candidate()
        record_path = _kept_record(self.fixture, "GCST90000001")
        record = json.loads(record_path.read_text())
        record["fingerprints"]["resolution_config"]["gates"]["tau"] = 0.99
        record["fingerprints"]["resolution_config"]["evidence_sample"] = 123
        record["fingerprints"]["opengwasdb_git_hash"] = "a-different-revision"
        record["fingerprints"]["fingerprint_digest"] = _fingerprint_digest(
            record["fingerprints"]
        )
        record_path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
        self._assert_emit_rejects_and_preserves("bound")

    def test_changed_tool_identity_makes_the_receipt_stale(self) -> None:
        self._resolved_candidate()
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        manifest = derive_resolver_manifest(
            read_inventory(self.fixture.inventory_path),
            config,
            read_analysis_metadata(self.fixture.provenance_path,
                [row.analysis_id for row in read_inventory(self.fixture.inventory_path) if row.ready],
            ),
        )
        run_root = self.fixture.work_root / STORE_ID / "resolver"
        with mock.patch.object(
            candidate_workflow,
            "_opengwasdb_tool_identity",
            return_value=("0.3.0", "0" * 64),
        ):
            _, failures = verify_records(
                manifest,
                config=config,
                manifest_path=run_root / "analyses.tsv",
                receipt_path=run_root / RECEIPT_FILENAME,
                index_path=run_root / "index.json",
            )
        self.assertTrue(
            any("opengwasdb_resolver_sha256" in failure for failure in failures), failures
        )

    def test_stale_record_fails_before_replacement(self) -> None:
        self.assertEqual(_run_cli(self.fixture).returncode, 0)
        _forge_kept_record(
            self.fixture,
            "GCST90000001",
            lambda r: r["fingerprints"].__setitem__("source_recorded_sha256", "0" * 64),
        )
        result = self._run_emit_only()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fingerprint", result.stderr)

    def test_account_records_flags_missing_duplicate_and_extra(self) -> None:
        rows = _two_resolver_rows()
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp)
            base_index = {
                "record_schema_version": 1,
                "n_total": 2,
                "analyses": [
                    {"analysis_id": "GCST1", "status": "success"},
                    {"analysis_id": "GCST2", "status": "success"},
                ],
            }
            # index present but a record missing
            (records / "index.json").write_text(json.dumps(base_index))
            _, failures = account_records(rows, records)
            self.assertTrue(any("missing" in failure for failure in failures))

            # duplicate id in the index
            duplicate_index = dict(base_index)
            duplicate_index["analyses"] = [
                {"analysis_id": "GCST1", "status": "success"},
                {"analysis_id": "GCST1", "status": "success"},
            ]
            (records / "index.json").write_text(json.dumps(duplicate_index))
            _, failures = account_records(rows, records)
            self.assertTrue(any("duplicate analysis_id" in failure for failure in failures))

            # A record the manifest does not name is another Selection's: the
            # checkpoint is shared by the collection (#284).
            (records / "index.json").write_text(json.dumps(base_index))
            (records / "GCST99999999.json").write_text("{}")
            _, failures = account_records(rows, records)
            self.assertFalse(any("GCST99999999" in failure for failure in failures), failures)

    def test_account_records_rejects_incompatible_schema_versions(self) -> None:
        rows = _two_resolver_rows()[:1]
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp)
            (records / "index.json").write_text(
                json.dumps(
                    {
                        "record_schema_version": 2,
                        "n_total": 1,
                        "analyses": [{"analysis_id": "GCST1", "status": "success"}],
                    }
                )
            )
            _, failures = account_records(rows, records)
            self.assertTrue(any("record_schema_version" in failure for failure in failures))

            (records / "index.json").write_text(
                json.dumps(
                    {
                        "record_schema_version": 1,
                        "n_total": 1,
                        "analyses": [{"analysis_id": "GCST1", "status": "success"}],
                    }
                )
            )
            (records / "GCST1.json").write_text(
                json.dumps(
                    {
                        "record_schema_version": 2,
                        "analysis_id": "GCST1",
                        "status": "success",
                    }
                )
            )
            _, failures = account_records(rows, records)
            self.assertTrue(any("record_schema_version" in failure for failure in failures))

    def test_read_resolution_receipt_rejects_incompatible_schema_versions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / RECEIPT_FILENAME
            path.write_text(json.dumps({"receipt_schema_version": 3}))
            with self.assertRaises(CandidateError):
                read_resolution_receipt(path)
            path.write_text(
                json.dumps({"receipt_schema_version": 2, "record_schema_version": 9})
            )
            with self.assertRaises(CandidateError):
                read_resolution_receipt(path)
            with self.assertRaises(CandidateError):
                read_resolution_receipt(Path(tmp) / "missing.json")

    # -- units -----------------------------------------------------------------

    def test_resolver_argv_composes_declared_facts(self) -> None:
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        argv = resolver_argv(
            resolver_bin="opengwasdb",
            manifest_path=Path("/tmp/analyses.tsv"),
            records_dir=Path("/tmp/records"),
            config=config,
            cores=64,
        )
        self.assertEqual(argv[:2], ["opengwasdb", "resolve-analyses"])
        for flag in (
            "--ancestry-reference",
            "--ancestry-groups",
            "--default-source-reader-capability",
            "--maf-floor",
            "--tau",
            "--delta",
            "--n-min",
            "--residual-max",
            "--n-workers",
            "--resume",
        ):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--n-workers") + 1], "64")
        self.assertNotIn("--af-reference", argv)  # source-AF-only policy (#152)

    def _with_ancestry_declaration(self, text: str) -> tuple[CandidateConfiguration, Path]:
        """The fixture config, its Ancestry Reference Panel declared by ``text``."""
        declaration = self.fixture.root / "ancestry-declaration" / "resource.yaml"
        declaration.parent.mkdir()
        declaration.write_text(text, encoding="utf-8")
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        return replace(config, ancestry_reference_declaration=declaration), declaration

    def test_the_resolver_is_passed_the_ancestry_panels_declared_build(self) -> None:
        """opengwasdb#277: the panel's build is its resource.yaml's, never a guess."""
        config, _ = self._with_ancestry_declaration(
            f"resource_id: {ANCESTRY_RESOURCE_ID}\nkind: ancestry_mixture\ngenome_build: GRCh38\n"
        )
        argv = resolver_argv(
            resolver_bin="opengwasdb",
            manifest_path=Path("/tmp/analyses.tsv"),
            records_dir=Path("/tmp/records"),
            config=config,
            cores=1,
        )
        self.assertEqual(argv[argv.index("--reference-build") + 1], "GRCh38")
        self.assertNotIn("--source-build", argv)

    def test_each_manifest_row_carries_its_analysis_declared_build_else_the_default(self) -> None:
        """opengwasdb#277: each Analysis's build is the one `render` emits, never `--source-build`."""
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        rows = read_inventory(self.fixture.inventory_path)
        metadata = read_analysis_metadata(
            self.fixture.provenance_path, [row.analysis_id for row in rows if row.ready]
        )
        declared, *blank = sorted(metadata)
        metadata[declared] = replace(metadata[declared], source_genome_build="GRCh37")

        manifest = derive_resolver_manifest(rows, config, metadata)

        builds = {row.analysis_id: row.source_genome_build for row in manifest}
        self.assertEqual(builds, {declared: "GRCh37", **{analysis_id: "GRCh38" for analysis_id in blank}})
        header = render_resolver_manifest(manifest).splitlines()[0].split("\t")
        self.assertIn("source_genome_build", header)

    def test_a_panel_declaring_no_build_is_refused_naming_its_declaration(self) -> None:
        config, declaration = self._with_ancestry_declaration(
            f"resource_id: {ANCESTRY_RESOURCE_ID}\nkind: ancestry_mixture\n"
        )
        with self.assertRaisesRegex(CandidateError, f"{declaration}.*genome_build"):
            resolver_settings(config)

    def test_resolver_manifest_uses_exact_inventory_paths(self) -> None:
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        rows = read_inventory(self.fixture.inventory_path)
        metadata = read_analysis_metadata(self.fixture.provenance_path, [row.analysis_id for row in rows if row.ready]
        )
        manifest = derive_resolver_manifest(rows, config, metadata)
        self.assertEqual(len(manifest), len(READY_ANALYSES))
        rendered = render_resolver_manifest(manifest)
        header = rendered.splitlines()[0].split("\t")
        self.assertEqual(tuple(header), RESOLVER_MANIFEST_COLUMNS)
        inventory_by_id = {row.analysis_id: row for row in rows}
        for entry in manifest:
            self.assertEqual(entry.source_file, inventory_by_id[entry.analysis_id].data_file)
            self.assertEqual(entry.checksum, inventory_by_id[entry.analysis_id].sha256)

    def test_apply_release_policy_is_total_and_controlled(self) -> None:
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        rows = read_inventory(self.fixture.inventory_path)
        ready_ids = [row.analysis_id for row in rows if row.ready]
        metadata = read_analysis_metadata(self.fixture.provenance_path, ready_ids)
        manifest = derive_resolver_manifest(rows, config, metadata)
        # Synthesise records straight from the manifest, using the fake's shape.
        run = _run_cli(self.fixture, "--stage", "resolve")
        self.assertEqual(run.returncode, 0, run.stderr)
        records, failures = verify_records(
            manifest,
            config=config,
            manifest_path=self.fixture.work_root / STORE_ID / "resolver/analyses.tsv",
            receipt_path=self.fixture.work_root / STORE_ID / "resolver" / RECEIPT_FILENAME,
            index_path=self.fixture.work_root / STORE_ID / "resolver/index.json",
        )
        self.assertEqual(failures, [])
        index = json.loads(
            (self.fixture.work_root / STORE_ID / "resolver/index.json").read_text()
        )
        metadata, _ = map_candidate_trait_ontologies(
            metadata, load_canonical_trait_mapping_table(self.fixture.trait_table_dir)
        )
        outcomes = apply_release_policy(
            rows, config, metadata, {record["analysis_id"]: record for record in records}
        )
        self.assertEqual(
            {outcome.analysis_id for outcome in outcomes if outcome.included},
            EXPECTED_INCLUDED,
        )
        self.assertTrue(
            all(
                outcome.exclusion_reason in controlled_exclusion_categories()
                for outcome in outcomes
                if not outcome.included
            )
        )
        tables = build_candidate_tables(
            inventory_rows=rows, outcomes=outcomes, config=config, index_summary=index
        )
        self.assertEqual(tables.included_rows, len(EXPECTED_INCLUDED))
        self.assertEqual(dict(tables.exclusion_counts), {
            "ancestry_not_eur": 1,
            "ancestry_unassigned": 1,
            "missing_case_control_counts": 1,
            "orientation_failure": 1,
            "resolution_failed": 1,
            "sd_no_qualifying_evidence": 1,
        })
        self.assertEqual(tables.ancestry_check, "passed_with_warnings")
        self.assertEqual(tables.sd_check, "passed_with_warnings")

    def _declare_variant_reference(self) -> None:
        """Declare a Variant Reference that exists, as the resolver needs it to."""
        axis = self.fixture.root / "reference" / "axis.gz"
        axis.write_bytes(b"fixture variant reference\n")
        config = yaml.safe_load(self.fixture.config_path.read_text())
        config["build"]["options"]["variant-reference"] = str(axis)
        self.fixture.config_path.write_text(yaml.safe_dump(config))

    def test_a_missing_resolver_settings_file_is_refused_before_resolving(self) -> None:
        config = yaml.safe_load(self.fixture.config_path.read_text())
        config["build"]["options"]["variant-reference"] = str(self.fixture.root / "absent.gz")
        self.fixture.config_path.write_text(yaml.safe_dump(config))
        run = _run_cli(self.fixture)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("--variant-reference", run.stderr)
        self.assertNotIn("Traceback", run.stderr)

    def test_high_projected_overflow_blocks_candidate_publication(self) -> None:
        self._declare_variant_reference()
        outcomes = json.loads(self.fixture.outcomes_path.read_text())
        outcomes["GCST90000001"]["variant_reference_rows_matched"] = 0
        self.fixture.outcomes_path.write_text(json.dumps(outcomes))
        run = _run_cli(self.fixture)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("GCST90000001", run.stderr + run.stdout)
        self.assertIn("off-reference share", run.stderr + run.stdout)
        self.assertFalse((self.fixture.registry_root / STORE_ID).exists())

    def test_zero_build_eligible_rows_excludes_and_publication_succeeds(self) -> None:
        # OGS-00011's blocker: 476 successful records with zero build-eligible
        # rows refused the candidate through the overlap gate. They now carry
        # one audited exclusion reason and the overlap gate never sees them.
        self._declare_variant_reference()
        self._set_outcome("GCST90000001", build_eligible_rows=0)
        self._set_outcome("GCST90000001", build_eligible_rows_on_variant_reference=0)
        self._set_outcome("GCST90000001", build_eligible_rows_off_variant_reference=0)
        run = _run_cli(self.fixture)
        self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
        bundle = self.fixture.registry_root / STORE_ID
        analyses = {row["analysis_id"]: row for row in _read_tsv(bundle / "analyses.tsv")}
        self.assertEqual(analyses["GCST90000001"]["exclude_from_build"], "true")
        self.assertIn(
            "excluded: no_build_eligible_rows:",
            analyses["GCST90000001"]["inclusion_reason"],
        )
        exclusions = {
            row["analysis_id"]: row for row in _read_tsv(bundle / "sidecars/exclusions.tsv")
        }
        row = exclusions["GCST90000001"]
        self.assertEqual(row["reason"], "no_build_eligible_rows")
        self.assertEqual(row["category"], "effect_scale")
        self.assertEqual(row["resolver_status"], "success")
        self.assertEqual(row["exclude_from_build"], "true")
        self.assertIn("finite effect and positive standard error", row["detail"])
        self.assertIn("canonical_rows_retained=2000", row["detail"])
        overlap = {
            row["analysis_id"]: row
            for row in _read_tsv(bundle / "sidecars/reference_overlap.tsv")
        }
        self.assertEqual(overlap["GCST90000001"]["build_eligible_rows"], "0")
        self.assertEqual(overlap["GCST90000001"]["exclude_from_build"], "true")
        validation = yaml.safe_load((bundle / "validation.yaml").read_text())
        self.assertEqual(validation["checks"]["reference_overlap"], "passed")
        self.assertEqual(validation["errors"], [])

    def test_bundle_overlap_sidecar_and_gate_name_worst_contributor(self) -> None:
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        config = replace(config, build_options={**config.build_options, "variant-reference": "/axis.gz"})
        first = _synthetic_inventory_row()
        second = replace(first, analysis_id="GCST90000002", data_file="/mirror/second.gz")
        records = []
        for row, matches in ((first, 100), (second, 0)):
            record = _synthetic_resolver_record()
            record["analysis_id"] = row.analysis_id
            record["diagnostics"].update(
                ancestry_rows_read=100, ancestry_reference_rows_matched=90,
                variant_reference_rows_matched=matches,
                build_eligible_rows=100,
                build_eligible_rows_on_variant_reference=matches,
                build_eligible_rows_off_variant_reference=100 - matches,
            )
            records.append(record)
        outcomes = apply_release_policy(
            [first, second], config,
            {row.analysis_id: _synthetic_analysis_metadata() for row in (first, second)},
            {record["analysis_id"]: record for record in records},
        )
        tables = build_candidate_tables(
            inventory_rows=[first, second], outcomes=outcomes, config=config, index_summary={}
        )
        overlap = list(csv.DictReader(io.StringIO(tables.reference_overlap_tsv), delimiter="\t"))
        self.assertEqual([row["variant_reference_rate"] for row in overlap], ["1", "0"])
        self.assertEqual([row["rows_scanned"] for row in overlap], ["100", "100"])
        validation = render_validation_yaml(
            tables=tables, index_summary={}, validated_at="now", validator_name="test",
            trait_ontology_mapping=TRAIT_ONTOLOGY_PROVENANCE,
        )
        self.assertEqual(validation["checks"]["reference_overlap"], "failed")
        self.assertEqual(validation["reference_overlap"]["projected_off_reference_share"], 0.5)
        self.assertEqual(validation["reference_overlap"]["low_overlap_analyses"], ["GCST90000002"])
        self.assertIn("GCST90000002", " ".join(validation["errors"]))

    def test_overlap_gate_prefers_post_info_build_eligible_counts(self) -> None:
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        config = replace(config, build_options={**config.build_options, "variant-reference": "/axis.gz"})
        first = _synthetic_inventory_row()
        second = replace(first, analysis_id="GCST90000002", data_file="/mirror/second.gz")
        records = []
        # The pre-INFO legacy counts say every row matched, but the post-INFO
        # eligible rows do not: 40/50 for the first and 0/50 for the second.
        for row, eligible, on, legacy_matched in ((first, 50, 40, 100), (second, 50, 0, 100)):
            record = _synthetic_resolver_record()
            record["analysis_id"] = row.analysis_id
            record["diagnostics"].update(
                ancestry_rows_read=100, ancestry_reference_rows_matched=90,
                variant_reference_rows_matched=legacy_matched,
                build_eligible_rows=eligible,
                build_eligible_rows_on_variant_reference=on,
                build_eligible_rows_off_variant_reference=eligible - on,
                info_score_state="filtered", info_rows_usable=eligible,
            )
            records.append(record)
        outcomes = apply_release_policy(
            [first, second], config,
            {row.analysis_id: _synthetic_analysis_metadata() for row in (first, second)},
            {record["analysis_id"]: record for record in records},
        )
        tables = build_candidate_tables(
            inventory_rows=[first, second], outcomes=outcomes, config=config, index_summary={}
        )
        overlap = list(csv.DictReader(io.StringIO(tables.reference_overlap_tsv), delimiter="\t"))
        # The legacy columns are left describing the pre-INFO rows, unchanged.
        self.assertEqual([row["variant_reference_rate"] for row in overlap], ["1", "1"])
        self.assertEqual([row["rows_scanned"] for row in overlap], ["100", "100"])
        self.assertEqual(
            [row["build_eligible_rows"] for row in overlap], ["50", "50"]
        )
        self.assertEqual(
            [row["build_eligible_rows_on_variant_reference"] for row in overlap], ["40", "0"]
        )
        self.assertEqual([row["build_eligible_rate"] for row in overlap], ["0.8", "0"])
        validation = render_validation_yaml(
            tables=tables, index_summary={}, validated_at="now", validator_name="test",
            trait_ontology_mapping=TRAIT_ONTOLOGY_PROVENANCE,
        )
        summary = validation["reference_overlap"]
        self.assertEqual(summary["projected_off_reference_basis"], "build_eligible_rows")
        self.assertEqual(summary["projected_off_reference_share"], 0.6)
        self.assertEqual(summary["low_overlap_analyses"], ["GCST90000002"])
        self.assertEqual(validation["checks"]["reference_overlap"], "failed")

    def test_a_record_without_post_info_diagnostics_is_refused_not_included(self) -> None:
        # Before #285 such a record was included and the overlap gate fell back
        # to the legacy pre-INFO counts. The build_eligible_rows Quality Check
        # cannot test it, and an untested exclusion is never a silent pass.
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        config = replace(config, build_options={**config.build_options, "variant-reference": "/axis.gz"})
        row = _synthetic_inventory_row()
        record = _synthetic_resolver_record()
        record["analysis_id"] = row.analysis_id
        record["diagnostics"].pop("build_eligible_rows")
        record["diagnostics"].update(
            rows_read=100, ancestry_rows_read=100, ancestry_reference_rows_matched=100,
            variant_reference_rows_matched=80,
        )
        with self.assertRaises(CandidateError) as raised:
            apply_release_policy(
                [row], config, {row.analysis_id: _synthetic_analysis_metadata()},
                {row.analysis_id: record},
            )
        self.assertIn(row.analysis_id, str(raised.exception))
        self.assertIn("build_eligible_rows is unmeasured", str(raised.exception))

    def test_listed_effect_placeholder_analysis_is_excluded_with_its_reason(self) -> None:
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        config = replace(
            config,
            effect_placeholder_exclusions={"GCST90000002": "5 of 100 rows are placeholders"},
        )
        first = _synthetic_inventory_row()
        second = replace(first, analysis_id="GCST90000002", data_file="/mirror/second.gz")
        records = {}
        for row in (first, second):
            record = _synthetic_resolver_record()
            record["analysis_id"] = row.analysis_id
            records[row.analysis_id] = record
        outcomes = apply_release_policy(
            [first, second], config,
            {row.analysis_id: _synthetic_analysis_metadata() for row in (first, second)},
            records,
        )
        by_id = {outcome.analysis_id: outcome for outcome in outcomes}
        self.assertTrue(by_id["GCST90000001"].included)
        self.assertFalse(by_id["GCST90000002"].included)
        self.assertEqual(by_id["GCST90000002"].exclusion_reason, "effect_placeholder_rows")
        self.assertEqual(
            by_id["GCST90000002"].exclusion_detail, "5 of 100 rows are placeholders"
        )
        tables = build_candidate_tables(
            inventory_rows=[first, second], outcomes=outcomes, config=config, index_summary={}
        )
        self.assertEqual(dict(tables.exclusion_counts), {"effect_placeholder_rows": 1})
        (excluded,) = csv.DictReader(io.StringIO(tables.exclusions_tsv), delimiter="\t")
        self.assertEqual(excluded["category"], "effect_scale")
        self.assertEqual(excluded["reason"], "effect_placeholder_rows")

    def test_non_positive_or_non_finite_sd_estimate_is_no_evidence(self) -> None:
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        row = _synthetic_inventory_row()
        for sd in (0.0, -1.0, float("nan"), float("inf")):
            record = _synthetic_resolver_record()
            record["phenotype_sd"]["estimate"]["sd"] = sd
            (outcome,) = apply_release_policy(
                [row], config, {row.analysis_id: _synthetic_analysis_metadata()},
                {row.analysis_id: record},
            )
            self.assertFalse(outcome.included, sd)
            self.assertEqual(outcome.exclusion_reason, "sd_no_qualifying_evidence")
            self.assertIn("not a positive finite number", outcome.exclusion_detail)

    def test_committed_effect_placeholder_exclusions_are_readable(self) -> None:
        path = REPO_ROOT / "resources/generators/gwas-catalog-eur-hybrid/effect-placeholder-exclusions.tsv"
        found = read_effect_placeholder_exclusions(path)
        self.assertEqual(len(found), 27)
        for analysis_id in ("GCST90454200", "GCST90454201", "GCST90565871", "GCST90565872"):
            self.assertIn("standard_error 0", found[analysis_id])
        self.assertEqual(read_effect_placeholder_exclusions(None), {})
        duplicate = self.fixture.root / "placeholder-duplicate.tsv"
        duplicate.write_text("analysis_id\treason\nA\tx\nA\ty\n", encoding="utf-8")
        with self.assertRaisesRegex(CandidateError, "duplicate analysis_id"):
            read_effect_placeholder_exclusions(duplicate)
        document = yaml.safe_load(self.fixture.config_path.read_text())
        document["release_policy"]["effect_placeholder_exclusions"] = (
            "resources/generators/gwas-catalog-eur-hybrid/effect-placeholder-exclusions.tsv"
        )
        self.fixture.config_path.write_text(yaml.safe_dump(document))
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        self.assertEqual(config.effect_placeholder_exclusions, found)

    # -- Indexed Variant Subset defaults (stores #210, epic #214) -------------

    def test_indexed_subsets_default_opt_out_and_malformed_errors(self) -> None:
        # Default: a config that says nothing emits the maintained hm3 entry.
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        self.assertEqual(len(config.indexed_subsets), 1)
        self.assertEqual(config.indexed_subsets[0]["name"], "hm3")
        self.assertEqual(config.indexed_subsets[0]["resource_id"], "hapmap3-hg38")
        self.assertIsNone(config.indexed_subsets_opt_out)
        build = candidate_workflow.render_build_yaml(STORE_ID, config)
        self.assertEqual(build["indexed_subsets"][0]["name"], "hm3")

        # A reviewed opt-out is an explicit empty list plus a written reason;
        # the build still declares the key, so the omission is reviewable.
        self._set_indexed_subsets([])
        self._set_indexed_subsets_opt_out("pilot predates the maintained HapMap3 list")
        opt_out = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        self.assertEqual(tuple(opt_out.indexed_subsets), ())
        self.assertEqual(
            opt_out.indexed_subsets_opt_out,
            "pilot predates the maintained HapMap3 list",
        )
        self.assertEqual(
            candidate_workflow.render_build_yaml(STORE_ID, opt_out)[
                "indexed_subsets"
            ],
            [],
        )

        # Empty without a reason is a generator error, not a silent omission.
        self._set_indexed_subsets_opt_out(None)
        with self.assertRaisesRegex(ValueError, "record why"):
            load_candidate_configuration(self.fixture.config_path, REPO_ROOT)

        # A null value is malformed, matching plan()/bundle.check().
        self._set_indexed_subsets(None)
        with self.assertRaisesRegex(ValueError, "not null"):
            load_candidate_configuration(self.fixture.config_path, REPO_ROOT)

        # A declaration missing a required field, or with a bad checksum, errors.
        self._set_indexed_subsets([{"name": "hm3"}])
        with self.assertRaisesRegex(ValueError, "requires a non-empty string"):
            load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        self._set_indexed_subsets([{
            "name": "hm3",
            "resource_id": "hapmap3-hg38",
            "variant_list": "/list.txt",
            "reference_assembly": "GRCh38",
            "checksum": "not-a-sha256",
        }])
        with self.assertRaisesRegex(ValueError, "64-character hex sha256"):
            load_candidate_configuration(self.fixture.config_path, REPO_ROOT)

        # A Ragged release may never declare one.
        self._set_indexed_subsets([{
            "name": "hm3",
            "resource_id": "hapmap3-hg38",
            "variant_list": "/list.txt",
            "reference_assembly": "GRCh38",
        }])
        document = yaml.safe_load(self.fixture.config_path.read_text())
        document["build"]["layout"] = "ragged"
        self.fixture.config_path.write_text(yaml.safe_dump(document))
        with self.assertRaisesRegex(ValueError, "Ragged"):
            load_candidate_configuration(self.fixture.config_path, REPO_ROOT)

    def test_indexed_subsets_reviewed_opt_out_is_recorded_in_release_notes(self) -> None:
        self._set_indexed_subsets([])
        self._set_indexed_subsets_opt_out("pilot predates the maintained HapMap3 list")
        result = _run_cli(self.fixture)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        bundle = self.fixture.registry_root / STORE_ID
        build = yaml.safe_load((bundle / "build.yaml").read_text())
        self.assertEqual(build["indexed_subsets"], [])
        release = yaml.safe_load((bundle / "release.yaml").read_text())
        self.assertIn("Indexed Variant Subsets (#210)", release["notes"])
        self.assertIn("intentionally omitted", release["notes"])
        self.assertIn("pilot predates the maintained HapMap3 list", release["notes"])

    # -- Store composition / MAF-filtered admission (issue #203) --------------

    def _maf_filtered_fixture(self):
        """A synthetic pair: one MAF-filtered Analysis, one with no applied floor."""
        filtered = _synthetic_inventory_row()
        unfiltered = replace(
            filtered, analysis_id="GCST90000002", data_file="/mirror/second.gz"
        )
        filtered_record = _synthetic_resolver_record()
        filtered_record["analysis_id"] = filtered.analysis_id
        filtered_record["diagnostics"].update(
            maf_state="filtered",
            build_eligible_rows=100,
            build_eligible_rows_off_variant_reference=40,
        )
        filtered_record["fingerprints"] = {
            "resolution_config": {"maf_threshold": 0.005}
        }
        unfiltered_record = _synthetic_resolver_record()
        unfiltered_record["analysis_id"] = unfiltered.analysis_id
        unfiltered_record["diagnostics"].update(
            maf_state="unavailable",
            build_eligible_rows=200,
            build_eligible_rows_off_variant_reference=50,
        )
        declarations = [
            ResolverRow(
                filtered.analysis_id, "/mirror/GCST90000001.h.tsv.gz",
                "opengwasdb.gwas-ssf", "sd", "estimated_from_source_maf", "5000",
                "a" * 64, "sha256", "100", maf_threshold="0.005",
            ),
            ResolverRow(
                unfiltered.analysis_id, "/mirror/second.gz", "opengwasdb.gwas-ssf",
                "sd", "estimated_from_source_maf", "5000", "b" * 64, "sha256",
                "100", maf_threshold="NaN",
            ),
        ]
        return filtered, unfiltered, [filtered_record, unfiltered_record], declarations

    def _store_composition_outcomes(self, config: object, records: list[dict]):
        filtered = _synthetic_inventory_row()
        unfiltered = replace(
            filtered, analysis_id="GCST90000002", data_file="/mirror/second.gz"
        )
        return apply_release_policy(
            [filtered, unfiltered],
            config,
            {
                row.analysis_id: _synthetic_analysis_metadata()
                for row in (filtered, unfiltered)
            },
            {record["analysis_id"]: record for record in records},
            self._maf_filtered_fixture()[3],
        )

    def test_store_composition_excludes_unfiltered_and_keeps_maf_filtered(self) -> None:
        config = replace(
            load_candidate_configuration(self.fixture.config_path, REPO_ROOT),
            require_maf_filtered=True,
            maf_threshold="0.005",
        )
        filtered, unfiltered, records, declarations = self._maf_filtered_fixture()
        outcomes = apply_release_policy(
            [filtered, unfiltered],
            config,
            {
                row.analysis_id: _synthetic_analysis_metadata()
                for row in (filtered, unfiltered)
            },
            {record["analysis_id"]: record for record in records},
            declarations,
        )
        by_id = {outcome.analysis_id: outcome for outcome in outcomes}
        self.assertTrue(by_id["GCST90000001"].included)
        self.assertEqual(by_id["GCST90000001"].maf_threshold, "0.005")
        routed = by_id["GCST90000002"]
        self.assertFalse(routed.included)
        self.assertEqual(routed.exclusion_reason, "not_maf_filtered")
        # The last gate keeps the other decided fields: the reason is only Store
        # composition, not an ancestry or effect-scale failure.
        self.assertEqual(routed.assigned_ancestry, "EUR")
        self.assertEqual(routed.original_sd, "1")
        self.assertEqual(
            routed.exclusion_detail,
            "no MAF floor applied (requested maf_threshold=NaN, resolver "
            "maf_state=unavailable, genotyping_technology=unrecorded); 50 of 200 "
            "build-eligible rows are off the variant reference (#203)",
        )
        tables = build_candidate_tables(
            inventory_rows=[filtered, unfiltered],
            outcomes=outcomes,
            config=config,
            index_summary={},
        )
        self.assertEqual(dict(tables.exclusion_counts), {"not_maf_filtered": 1})
        (excluded,) = csv.DictReader(io.StringIO(tables.exclusions_tsv), delimiter="\t")
        self.assertEqual(excluded["category"], "store_composition")
        self.assertEqual(excluded["reason"], "not_maf_filtered")

    def test_store_composition_keeps_a_stronger_existing_exclusion(self) -> None:
        config = replace(
            load_candidate_configuration(self.fixture.config_path, REPO_ROOT),
            require_maf_filtered=True,
            maf_threshold="0.005",
        )
        filtered, unfiltered, records, declarations = self._maf_filtered_fixture()
        # The unfiltered Analysis already fails the target-ancestry gate.
        records[1]["ancestry"]["assigned_ancestry"] = "EAS"
        outcomes = apply_release_policy(
            [filtered, unfiltered],
            config,
            {
                row.analysis_id: _synthetic_analysis_metadata()
                for row in (filtered, unfiltered)
            },
            {record["analysis_id"]: record for record in records},
            declarations,
        )
        by_id = {outcome.analysis_id: outcome for outcome in outcomes}
        self.assertEqual(by_id["GCST90000002"].exclusion_reason, "ancestry_not_eur")

    def test_store_composition_refuses_to_run_without_manifest_rows(self) -> None:
        config = replace(
            load_candidate_configuration(self.fixture.config_path, REPO_ROOT),
            require_maf_filtered=True,
            maf_threshold="0.005",
        )
        filtered, unfiltered, records, _ = self._maf_filtered_fixture()
        # Without the manifest no Analysis can show an applied floor, so the rule
        # would exclude every one of them; that must fail, not empty the Store.
        with self.assertRaisesRegex(CandidateError, "resolver manifest rows"):
            apply_release_policy(
                [filtered, unfiltered],
                config,
                {
                    row.analysis_id: _synthetic_analysis_metadata()
                    for row in (filtered, unfiltered)
                },
                {record["analysis_id"]: record for record in records},
            )

    def test_store_composition_detail_names_a_missing_declaration(self) -> None:
        config = replace(
            load_candidate_configuration(self.fixture.config_path, REPO_ROOT),
            require_maf_filtered=True,
            maf_threshold="0.005",
        )
        filtered, unfiltered, records, declarations = self._maf_filtered_fixture()
        outcomes = apply_release_policy(
            [filtered, unfiltered],
            config,
            {
                row.analysis_id: _synthetic_analysis_metadata()
                for row in (filtered, unfiltered)
            },
            {record["analysis_id"]: record for record in records},
            declarations[:1],
        )
        by_id = {outcome.analysis_id: outcome for outcome in outcomes}
        self.assertEqual(by_id["GCST90000002"].exclusion_reason, "not_maf_filtered")
        # An absent manifest row is not the same fact as a declared NaN request.
        self.assertIn(
            "requested maf_threshold=undeclared", by_id["GCST90000002"].exclusion_detail
        )

    def test_store_composition_off_warns_about_unfiltered_inclusion(self) -> None:
        config = replace(
            load_candidate_configuration(self.fixture.config_path, REPO_ROOT),
            maf_threshold="0.005",
        )
        self.assertFalse(config.require_maf_filtered)
        filtered, unfiltered, records, declarations = self._maf_filtered_fixture()
        outcomes = apply_release_policy(
            [filtered, unfiltered],
            config,
            {
                row.analysis_id: _synthetic_analysis_metadata()
                for row in (filtered, unfiltered)
            },
            {record["analysis_id"]: record for record in records},
            declarations,
        )
        by_id = {outcome.analysis_id: outcome for outcome in outcomes}
        self.assertTrue(by_id["GCST90000002"].included)
        self.assertEqual(by_id["GCST90000002"].maf_threshold, "NaN")
        tables = build_candidate_tables(
            inventory_rows=[filtered, unfiltered],
            outcomes=outcomes,
            config=config,
            index_summary={},
        )
        self.assertIn(
            "1 included Analysis/Analyses had no MAF floor applied, so their "
            "off-reference rows enter the shared variant axis unfiltered (#203); "
            "see store_composition in validation.yaml",
            tables.warnings,
        )

    def test_store_composition_summary_values_and_missing_sums(self) -> None:
        base = replace(
            load_candidate_configuration(self.fixture.config_path, REPO_ROOT),
            require_maf_filtered=True,
            maf_threshold="0.005",
        )
        axis = replace(
            base, build_options={**base.build_options, "variant-reference": "/axis.gz"}
        )
        filtered, unfiltered, records, declarations = self._maf_filtered_fixture()
        outcomes = self._store_composition_outcomes(axis, records)
        tables = build_candidate_tables(
            inventory_rows=[filtered, unfiltered],
            outcomes=outcomes,
            config=axis,
            index_summary={},
        )
        self.assertEqual(
            tables.store_composition,
            {
                "require_maf_filtered": True,
                "maf_floor": "0.005",
                "unfiltered_included": 0,
                "unfiltered_included_off_reference_rows": 0,
                "unfiltered_routed": 1,
                "unfiltered_routed_off_reference_rows": 50,
            },
        )
        # No declared variant-reference: absence is never summed as zero.
        no_axis = build_candidate_tables(
            inventory_rows=[filtered, unfiltered],
            outcomes=outcomes,
            config=base,
            index_summary={},
        )
        self.assertIsNone(
            no_axis.store_composition["unfiltered_routed_off_reference_rows"]
        )
        # A counted Analysis without a valid off-reference count makes the sum
        # unknown, not zero.
        del records[1]["diagnostics"]["build_eligible_rows_off_variant_reference"]
        missing = build_candidate_tables(
            inventory_rows=[filtered, unfiltered],
            outcomes=self._store_composition_outcomes(axis, records),
            config=axis,
            index_summary={},
        )
        self.assertIsNone(
            missing.store_composition["unfiltered_routed_off_reference_rows"]
        )
        # Rule off: the unfiltered Analysis is included and its off-reference
        # rows are counted, while nothing is routed.
        off_axis = replace(axis, require_maf_filtered=False)
        included_tables = build_candidate_tables(
            inventory_rows=[filtered, unfiltered],
            outcomes=self._store_composition_outcomes(off_axis, records),
            config=off_axis,
            index_summary={},
        )
        self.assertEqual(included_tables.store_composition["unfiltered_included"], 1)
        self.assertEqual(included_tables.store_composition["unfiltered_routed"], 0)

    def test_store_composition_configuration_parsing(self) -> None:
        self.assertFalse(
            load_candidate_configuration(
                self.fixture.config_path, REPO_ROOT
            ).require_maf_filtered
        )
        self._set_store_composition({"require_maf_filtered": True})
        # The rule needs a positive default floor or no Analysis can be filtered.
        with self.assertRaisesRegex(ValueError, "positive.*maf_threshold"):
            load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        self._set_maf_threshold(0.005)
        self.assertTrue(
            load_candidate_configuration(
                self.fixture.config_path, REPO_ROOT
            ).require_maf_filtered
        )
        self._set_maf_threshold(0)
        with self.assertRaisesRegex(ValueError, "positive.*maf_threshold"):
            load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        for invalid, message in (
            (["require_maf_filtered"], "store_composition must be a mapping"),
            ({"unknown": True}, "unknown key"),
            ({"require_maf_filtered": "yes"}, "must be a boolean"),
            ({"require_maf_filtered": 1}, "must be a boolean"),
        ):
            with self.subTest(invalid=invalid):
                self._set_store_composition(invalid)
                with self.assertRaisesRegex(ValueError, message):
                    load_candidate_configuration(self.fixture.config_path, REPO_ROOT)

    def test_store_composition_rule_does_not_change_the_resolution_contract(self) -> None:
        self._set_maf_threshold(0.005)
        off = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        on = replace(off, require_maf_filtered=True)
        self.assertEqual(
            compute_resolution_contract(off), compute_resolution_contract(on)
        )

    def test_store_composition_documents_carry_the_rule(self) -> None:
        config = replace(
            load_candidate_configuration(self.fixture.config_path, REPO_ROOT),
            require_maf_filtered=True,
            maf_threshold="0.005",
        )
        filtered, unfiltered, records, declarations = self._maf_filtered_fixture()
        outcomes = apply_release_policy(
            [filtered, unfiltered],
            config,
            {
                row.analysis_id: _synthetic_analysis_metadata()
                for row in (filtered, unfiltered)
            },
            {record["analysis_id"]: record for record in records},
            declarations,
        )
        tables = build_candidate_tables(
            inventory_rows=[filtered, unfiltered],
            outcomes=outcomes,
            config=config,
            index_summary={},
        )
        validation = render_validation_yaml(
            tables=tables,
            index_summary={},
            validated_at="now",
            validator_name="test",
            trait_ontology_mapping=TRAIT_ONTOLOGY_PROVENANCE,
        )
        self.assertEqual(validation["store_composition"], dict(tables.store_composition))
        release = render_release_yaml(
            store_id=STORE_ID,
            config=config,
            tables=tables,
            created_at="2026-01-01T00:00:00Z",
            inventory_sha256="a" * 64,
            preflight_report=Path("preflight.json"),
            index_summary={},
            generator_version="test",
        )
        self.assertIn(
            "Store composition (#203): this shared Store admits only Analyses whose "
            "rows were MAF-filtered; an included Analysis with no applied MAF floor "
            "is excluded as not_maf_filtered.",
            release["notes"],
        )

    def test_zero_build_eligible_rows_is_excluded_and_not_missing(self) -> None:
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        config = replace(config, build_options={**config.build_options, "variant-reference": "/axis.gz"})
        first = _synthetic_inventory_row()
        second = replace(first, analysis_id="GCST90000002", data_file="/mirror/second.gz")
        records = []
        # A successful record whose own build-eligible tally is an explicit
        # integer zero contributes no association at all.
        for row, eligible, on in ((first, 50, 40), (second, 0, 0)):
            record = _synthetic_resolver_record()
            record["analysis_id"] = row.analysis_id
            record["diagnostics"].update(
                ancestry_rows_read=100, ancestry_reference_rows_matched=90,
                variant_reference_rows_matched=100,
                canonical_rows_retained=100,
                build_eligible_rows=eligible,
                build_eligible_rows_on_variant_reference=on,
                build_eligible_rows_off_variant_reference=eligible - on,
            )
            records.append(record)
        outcomes = apply_release_policy(
            [first, second], config,
            {row.analysis_id: _synthetic_analysis_metadata() for row in (first, second)},
            {record["analysis_id"]: record for record in records},
        )
        by_id = {outcome.analysis_id: outcome for outcome in outcomes}
        self.assertTrue(by_id["GCST90000001"].included)
        self.assertFalse(by_id["GCST90000002"].included)
        self.assertEqual(
            by_id["GCST90000002"].exclusion_reason, "no_build_eligible_rows"
        )
        self.assertIn(
            "no row with a finite effect and positive standard error",
            by_id["GCST90000002"].exclusion_detail,
        )
        self.assertIn("rows_read=100", by_id["GCST90000002"].exclusion_detail)
        self.assertIn(
            "canonical_rows_retained=100", by_id["GCST90000002"].exclusion_detail
        )
        tables = build_candidate_tables(
            inventory_rows=[first, second], outcomes=outcomes, config=config, index_summary={}
        )
        # The excluded Analysis is not an included Analysis with a missing
        # measurement: the zero-eligible count is reported, never a failure.
        self.assertEqual(tables.reference_overlap_errors, ())
        self.assertEqual(dict(tables.exclusion_counts), {"no_build_eligible_rows": 1})
        overlap = list(csv.DictReader(io.StringIO(tables.reference_overlap_tsv), delimiter="\t"))
        self.assertEqual([row["build_eligible_rows"] for row in overlap], ["50", "0"])
        self.assertEqual([row["exclude_from_build"] for row in overlap], ["", "true"])
        validation = render_validation_yaml(
            tables=tables, index_summary={}, validated_at="now", validator_name="test",
            trait_ontology_mapping=TRAIT_ONTOLOGY_PROVENANCE,
        )
        self.assertEqual(validation["checks"]["reference_overlap"], "passed")
        self.assertEqual(validation["errors"], [])

    def test_invalid_build_eligible_counts_refuse_or_fail_the_overlap_gate(self) -> None:
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        config = replace(config, build_options={**config.build_options, "variant-reference": "/axis.gz"})
        row = _synthetic_inventory_row()

        def record_with(diagnostics):
            record = _synthetic_resolver_record()
            record["analysis_id"] = row.analysis_id
            record["diagnostics"].pop("build_eligible_rows")
            record["diagnostics"].update(
                ancestry_rows_read=100, ancestry_reference_rows_matched=100,
            )
            record["diagnostics"].update(diagnostics)
            return record

        # An unusable count is no evidence the Analysis has rows to build, so
        # the build_eligible_rows Quality Check refuses to let it be included
        # (#285); before, it was included and only the overlap gate caught it.
        refused = {
            "absent": ({}, "unmeasured"),
            "non-integer": ({"build_eligible_rows": "50"}, "not a non-negative integer"),
            "negative": ({"build_eligible_rows": -1}, "not a non-negative integer"),
        }
        for name, (diagnostics, message) in refused.items():
            with self.subTest(case=name):
                with self.assertRaises(CandidateError) as raised:
                    apply_release_policy(
                        [row], config, {row.analysis_id: _synthetic_analysis_metadata()},
                        {row.analysis_id: record_with(diagnostics)},
                    )
                self.assertIn(row.analysis_id, str(raised.exception))
                self.assertIn(message, str(raised.exception))

        # A valid count with an unusable on/off split is the overlap gate's.
        partitions = {
            "inconsistent-partition": {
                "build_eligible_rows": 50,
                "build_eligible_rows_on_variant_reference": 20,
                "build_eligible_rows_off_variant_reference": 20,
            },
            "out-of-range-partition": {
                "build_eligible_rows": 50,
                "build_eligible_rows_on_variant_reference": 60,
                "build_eligible_rows_off_variant_reference": -10,
            },
        }
        for name, diagnostics in partitions.items():
            with self.subTest(case=name):
                outcomes = apply_release_policy(
                    [row], config, {row.analysis_id: _synthetic_analysis_metadata()},
                    {row.analysis_id: record_with(diagnostics)},
                )
                self.assertTrue(outcomes[0].included, outcomes[0].exclusion_reason)
                tables = build_candidate_tables(
                    inventory_rows=[row], outcomes=outcomes, config=config, index_summary={}
                )
                self.assertTrue(tables.reference_overlap_errors, name)
                self.assertIn(row.analysis_id, " ".join(tables.reference_overlap_errors))

    # -- EAF orientation vocabulary (issue #115 / #154) -----------------------

    def _orientation_outcome(
        self, *, eaf_orientation: str, gate_reason: str, assigned: str | None, r: float = 0.99
    ):
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        row = _synthetic_inventory_row()
        metadata = _synthetic_analysis_metadata()
        record = _synthetic_resolver_record(
            eaf_orientation=eaf_orientation,
            gate_reason=gate_reason,
            assigned_ancestry=assigned,
            eaf_orientation_r=r,
        )
        return apply_release_policy(
            [row], config, {row.analysis_id: metadata}, {row.analysis_id: record}
        )[0]

    def test_orientation_passed_eur_is_includable(self) -> None:
        outcome = self._orientation_outcome(
            eaf_orientation="passed", gate_reason="ok", assigned="EUR"
        )
        self.assertTrue(outcome.included, outcome.exclusion_reason)
        self.assertEqual(outcome.assigned_ancestry, "EUR")

    def test_orientation_failed_is_orientation_failure(self) -> None:
        # Real shape: a negative sign is also the assignment gate that names it.
        outcome = self._orientation_outcome(
            eaf_orientation="failed", gate_reason="eaf_orientation", assigned=None, r=-0.98
        )
        self.assertFalse(outcome.included)
        self.assertEqual(outcome.exclusion_reason, "orientation_failure")

    def test_orientation_failed_excludes_even_with_ok_gate(self) -> None:
        # A mildly negative correlation is `failed` without tripping the gate.
        outcome = self._orientation_outcome(
            eaf_orientation="failed", gate_reason="ok", assigned="EUR", r=-0.2
        )
        self.assertFalse(outcome.included)
        self.assertEqual(outcome.exclusion_reason, "orientation_failure")

    def test_orientation_unverified_follows_assignment_policy(self) -> None:
        # `unverified` is not a verdict: the ordinary policy decides the row.
        included = self._orientation_outcome(
            eaf_orientation="unverified", gate_reason="ok", assigned="EUR", r=float("nan")
        )
        self.assertTrue(included.included, included.exclusion_reason)
        unassigned = self._orientation_outcome(
            eaf_orientation="unverified", gate_reason="overlap", assigned=None, r=float("nan")
        )
        self.assertFalse(unassigned.included)
        self.assertEqual(unassigned.exclusion_reason, "ancestry_unassigned")

    def test_unknown_eaf_orientation_fails_loudly(self) -> None:
        # `ok` is a gate_reason, not an EafOrientationOutcome; it must not read as passed.
        with self.assertRaises(CandidateError):
            self._orientation_outcome(eaf_orientation="ok", gate_reason="ok", assigned="EUR")

    def test_validate_candidate_analyses_rejects_blank_included_required_value(self) -> None:
        header = "analysis_id\tstored_effect_scale\tsample_size_kind\tsample_size_scope\tsample_size\toriginal_effect_scale\toriginal_sd_method\tancestry_assignment_method\n"
        blank = "GCST1\tsd\ttotal\tanalysis_level\t\tsd\testimated_from_source_maf\taf_assigned\n"
        self.assertTrue(validate_candidate_analyses(header + blank))

    # -- snakemake wiring ------------------------------------------------------

    @unittest.skipUnless(
        SNAKEMAKE, "snakemake is not on PATH; run under `pixi run --environment dev`"
    )
    def test_generate_smk_dry_run_wires_every_stage(self) -> None:
        snakefile = REPO_ROOT / "workflow/generate.smk"
        work_root = self.fixture.root / "smk-work"
        registry_root = self.fixture.root / "smk-stores"
        command = [
            str(SNAKEMAKE),
            "--snakefile",
            str(snakefile),
            "--dry-run",
            "--cores",
            "1",
            "--config",
            f"store_id={STORE_ID}",
            f"config={self.fixture.config_path}",
            f"snapshot_id={SNAPSHOT_ID}",
            f"work_root={work_root}",
            f"registry_root={registry_root}",
        ]
        result = subprocess.run(
            command, cwd=str(self.fixture.root), capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        for rule in (
            "freeze", "preflight", "resolver_manifest", "resolve", "verify", "render", "emit"
        ):
            self.assertIn(rule, result.stdout)
        # The same DAG refuses to run without its required configuration.
        missing = subprocess.run(
            [str(SNAKEMAKE), "--snakefile", str(snakefile), "--dry-run", "--cores", "1"],
            cwd=str(self.fixture.root),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("store_id", missing.stderr + missing.stdout)


class ReferenceCompletionTests(unittest.TestCase):
    """A release config's completion recipe reaches the emitted bundle (#340, ADR 0037).

    The end-to-end seams: a Dense release through ``prepare``, ``resolve`` (the
    fake resolver), ``render`` and ``emit`` into a temporary registry, once with
    a qualified and active reference and once with one short of that. The
    fixture starts from the Source Collection's connector files and a release
    config only, as ``EntryPointStageTests`` does.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="reference-completion-")
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def fixture(self, name: str, *, completion: dict | None = None) -> Fixture:
        return FixtureBuilder(self.root / name).build(completion=completion, layout="dense")

    #: One run's timestamp, so two renders of one fixture compare byte for byte.
    CREATED_AT = "2026-10-22T00:00:00Z"

    def stages(self, fixture: Fixture, *names: str) -> list[subprocess.CompletedProcess]:
        results = []
        for name in names:
            result = _run_cli(fixture, "--stage", name, *("--created-at", self.CREATED_AT))
            self.assertEqual(result.returncode, 0, f"{name}: {result.stderr}{result.stdout}")
            results.append(result)
        return results

    def bundle(self, fixture: Fixture, name: str) -> dict:
        return yaml.safe_load((fixture.registry_root / STORE_ID / name).read_text(encoding="utf-8"))

    def qualified(self, **changes) -> dict:
        return {
            "reference": {
                "resource_id": COMPLETION_PANEL_RESOURCE_ID,
                "qualification": "qualified",
                "activation": "active",
                **changes,
            },
            "command": "complete-dense",
            "options": {"n-workers": 2},
        }

    def test_a_qualified_active_reference_emits_a_bundle_that_plans_both_phases(self) -> None:
        fixture = self.fixture("qualified", completion=self.qualified())

        started = time.monotonic()
        results = self.stages(fixture, "prepare", "resolve", "render", "emit")
        elapsed = time.monotonic() - started
        # Seam: one end-to-end run of the four stages, well under five seconds.
        self.assertLess(elapsed, 5.0, f"the end-to-end run took {elapsed:.1f} s")
        self.assertIn("is qualified and active", results[-2].stdout)

        build = self.bundle(fixture, "build.yaml")
        self.assertEqual(build["completion_state"], "reference_completed")
        self.assertEqual(
            build["complete"],
            {
                "command": "complete-dense",
                "options": {
                    "ld-panel": str(fixture.root / "reference" / "ld"),
                    "ancestry": "EUR",
                    "n-workers": 2,
                },
            },
        )
        self.assertEqual(
            list(build),
            ["store_id", "layout", "completion_state", "build", "complete", "post", "indexed_subsets"],
        )
        # One bundle under one id: no second id is minted and no lineage edge is claimed.
        self.assertIsNone(self.bundle(fixture, "release.yaml")["derived_from"])
        self.assertEqual(
            self.bundle(fixture, "validation.yaml")["acceptance"],
            {
                "completion_reference": {
                    "resource_id": COMPLETION_PANEL_RESOURCE_ID,
                    "declared_status": "available",
                    "qualification": "qualified",
                    "activation": "active",
                    "blocked": False,
                }
            },
        )

        # The bundle is a Release Bundle, and it plans both phases under its id.
        loaded = bundle_module.load(STORE_ID, registry_root=fixture.registry_root)
        self.assertEqual(list(bundle_module.check(loaded, registry_root=fixture.registry_root)), [])
        self.assertTrue(loaded.completes_own_store)
        self.assertIsNone(loaded.derived_from)
        artifact_root = self.root / "artifacts"
        steps = plan(loaded, artifact_root=artifact_root)
        self.assert_matches_the_single_id_golden(steps, fixture, artifact_root)
        complete = next(step for step in steps if step.name == "complete")
        self.assertEqual(complete.argv[complete.argv.index("--release-id") + 1], f"{STORE_ID}-completed")
        self.assertEqual(
            option(complete.argv, "--ld-panel"), str(fixture.root / "reference" / "ld")
        )
        self.assertEqual(option(complete.argv, "--ancestry"), "EUR")
        self.assertEqual(option(complete.argv, "--n-workers"), "2")

    #: Where the golden's bundle plans its artifacts, and the bundle's own id.
    GOLDEN_ARTIFACT_ROOT = "/data/opengwasdb/stores"
    GOLDEN_STORE_ID = "OGS-00003"
    #: The golden's LD panel, which this fixture replaces with its own.
    GOLDEN_LD_PANEL = "/data/opengwasdb/reference/ld/hgdp1kgp-hg38"
    #: Options this fixture's recipe intentionally omits from a golden step.
    OMITTED_OPTIONS: Mapping[str, tuple[str, ...]] = {"build": ("--n-workers",)}

    def fixture_option_values(self, fixture: Fixture) -> Mapping[str, Mapping[str, str]]:
        """The few options this fixture sets differently from the golden's recipe.

        The golden is a FinnGen release; this fixture is a GWAS-SSF one, so its
        reader capability is the SSF reader's.
        """
        return {
            "build": {"--source-reader-capability": "opengwasdb.gwas-ssf"},
            "complete": {
                "--ld-panel": str(fixture.root / "reference" / "ld"),
                "--n-workers": "2",
            },
        }

    def expected_tokens(
        self, step: str, tokens: Sequence[str], fixture: Fixture, artifact_root: Path,
        *, options: bool = False,
    ) -> list[str]:
        """One golden step's tokens, rewritten for this fixture's documented differences.

        Three substitutions (the Store id, the artifact root, the LD panel) and
        one omitted option are all this fixture changes. Everything else -- every
        flag, every value and every positional path -- must be the golden's
        exactly, so a step missing a required flag such as `validate`'s
        `--format` fails here.
        """
        substitutions = {
            self.GOLDEN_STORE_ID: STORE_ID,
            f"{self.GOLDEN_ARTIFACT_ROOT}/": f"{artifact_root}/",
            self.GOLDEN_LD_PANEL: str(fixture.root / "reference" / "ld"),
        }
        expected: list[str] = []
        for token in tokens:
            for before, after in substitutions.items():
                token = token.replace(before, after)
            expected.append(token)
        if not options:
            return expected
        for flag, value in self.fixture_option_values(fixture).get(step, {}).items():
            expected[expected.index(flag) + 1] = value
        for flag in self.OMITTED_OPTIONS.get(step, ()):
            position = expected.index(flag)
            del expected[position:position + 2]
        return expected

    def assert_matches_the_single_id_golden(self, steps, fixture: Fixture, artifact_root: Path) -> None:
        """The plan is the committed single-id golden's, exactly, where the two agree.

        The golden pins what a completion bundle adds (#305, ADR 0037): the step
        sequence, each step's phase, its command, its flags and their values,
        its positionals, and what it reads and writes. This fixture's id, its
        artifact root, its panel and two options are the only differences, and
        each is listed above.
        """
        golden = json.loads(
            (REPO_ROOT / "tests" / "plan" / "golden" / "OGS-00003-single-id-completed.json").read_text()
        )
        self.assertEqual([step.phase for step in steps], [entry.get("phase", "build") for entry in golden])
        self.assertEqual([step.name for step in steps], [entry["name"] for entry in golden])
        for step, expected in zip(steps, golden):
            with self.subTest(step=step.name):
                self.assertEqual(
                    step.argv,
                    self.expected_tokens(
                        step.name, expected["argv"], fixture, artifact_root, options=True
                    ),
                )
                self.assertEqual(
                    [str(path) for path in step.inputs],
                    self.expected_tokens(step.name, expected["inputs"], fixture, artifact_root),
                )
                self.assertEqual(
                    [str(path) for path in step.outputs],
                    self.expected_tokens(step.name, expected["outputs"], fixture, artifact_root),
                )
                if "published_store_arg" in expected:
                    self.assertEqual(step.published_store_arg, expected["published_store_arg"])

    def test_a_reference_short_of_qualified_and_active_emits_the_bundle_without_one(self) -> None:
        references = {
            "pending qualification": {"qualification": "pending"},
            "pending activation": {"activation": "pending"},
            "on hold and blocked": {"qualification": "hold", "activation": "blocked"},
            # A blocked reference is never opened: this one names a panel that
            # does not exist, and preflight neither requires nor finds it.
            "pending, panel absent": {"qualification": "pending", "panel": "absent"},
        }
        for case, changes in references.items():
            with self.subTest(case):
                fixture = self.fixture("observed-" + case.replace(" ", "-").replace(",", ""))
                self.stages(fixture, "prepare", "resolve", "render", "emit")
                observed = {
                    name: (fixture.registry_root / STORE_ID / name).read_bytes()
                    for name in ("build.yaml", "analyses.tsv", "release.yaml", "validation.yaml")
                }
                observed_release = self.bundle(fixture, "release.yaml")
                observed_validation = self.bundle(fixture, "validation.yaml")

                # The same fixture, the same Selection: only the recipe is added,
                # and its reference is short of qualified and active. The panel's
                # location is absent in the last case and present in the others.
                panel = fixture.root / ("missing-ld" if changes.get("panel") == "absent" else "reference/ld")
                if changes.get("panel") == "absent":
                    panel.rmdir() if panel.is_dir() else None
                document = yaml.safe_load(fixture.config_path.read_text(encoding="utf-8"))
                document["build"]["completion_state"] = "reference_completed"
                document["completion"] = self.qualified(
                    **{key: value for key, value in changes.items() if key != "panel"}
                )
                document["reference_resources"].append(
                    {
                        "resource_id": COMPLETION_PANEL_RESOURCE_ID,
                        "kind": "ld_panel",
                        "location": str(panel),
                        "location_kind": "external_directory",
                    }
                )
                fixture.config_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
                # Only the recipe changed, so the resolved records are the ones
                # this run already kept: preflight, render and emit re-read them.
                results = self.stages(fixture, "preflight", "render", "emit")

                # The recipe and the membership are the Observed-Only bundle's, byte for byte.
                for name in ("build.yaml", "analyses.tsv"):
                    self.assertEqual(
                        (fixture.registry_root / STORE_ID / name).read_bytes(),
                        observed[name],
                        f"{name} is not the bundle a config without a completion block emits",
                    )
                self.assertEqual(self.bundle(fixture, "build.yaml")["completion_state"], "observed_only")
                self.assertNotIn("complete:", (fixture.registry_root / STORE_ID / "build.yaml").read_text())

                # Everything but the paragraph that says why is unchanged.
                release = self.bundle(fixture, "release.yaml")
                paragraphs = release["notes"].split("\n\n")
                self.assertIn(
                    f"Reference completion (ADR 0037): blocked: completion reference "
                    f"{COMPLETION_PANEL_RESOURCE_ID} is qualification "
                    f"{changes.get('qualification', 'qualified')}, activation "
                    f"{changes.get('activation', 'active')}, not qualified and active; this "
                    "bundle is Observed-Only and declares no complete block.",
                    paragraphs,
                )
                self.assertEqual(
                    [p for p in paragraphs if not p.startswith("Reference completion")],
                    observed_release["notes"].split("\n\n"),
                )
                self.assertEqual(
                    {key: value for key, value in release.items() if key != "notes"},
                    {key: value for key, value in observed_release.items() if key != "notes"},
                )
                # The Validation Record says so, and records no status: the
                # declaration was never read.
                self.assertEqual(
                    self.bundle(fixture, "validation.yaml")["acceptance"]["completion_reference"],
                    {
                        "resource_id": COMPLETION_PANEL_RESOURCE_ID,
                        "qualification": changes.get("qualification", "qualified"),
                        "activation": changes.get("activation", "active"),
                        "blocked": True,
                    },
                )
                self.assertEqual(
                    {key: value for key, value in self.bundle(fixture, "validation.yaml").items()
                     if key != "acceptance"},
                    {key: value for key, value in observed_validation.items() if key != "acceptance"},
                )
                # The operator is told at render time, as the ukb-b generator said it on stderr.
                self.assertIn("not qualified and active", results[1].stdout)
                self.assertIn("observed-only, with no complete block", results[1].stdout)
                self.assertNotIn("acceptance", observed_validation)

                loaded = bundle_module.load(STORE_ID, registry_root=fixture.registry_root)
                self.assertEqual(list(bundle_module.check(loaded, registry_root=fixture.registry_root)), [])
                self.assertFalse(loaded.completes_own_store)


def option(argv: Sequence[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


class EntryPointStageTests(unittest.TestCase):
    """One entry point runs every stage for a release config, end to end (#287).

    The fixture starts with the Source Collection's two connector files and a
    release config only: nothing is frozen until the ``freeze`` stage runs.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="entry-point-")
        self.fixture = FixtureBuilder(Path(self._tmp.name)).build(freeze=False)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def stage(self, name: str, **kwargs) -> subprocess.CompletedProcess:
        result = _run_cli(self.fixture, "--stage", name, **kwargs)
        self.assertEqual(result.returncode, 0, f"{name}: {result.stderr}{result.stdout}")
        return result

    def test_each_stage_in_turn_freezes_resolves_renders_and_emits(self) -> None:
        run_root = self.fixture.work_root / STORE_ID
        self.assertFalse(self.fixture.provenance_path.exists())

        self.stage("freeze")
        provenance = yaml.safe_load(self.fixture.provenance_path.read_text())
        self.assertEqual(provenance["frozen_by"], "manifest_generation.freeze")
        self.assertEqual(provenance["selection"], SELECTION)
        report = json.loads((run_root / "freeze" / f"{SNAPSHOT_ID}.json").read_text())
        self.assertTrue(report["frozen_by_this_run"])

        self.stage("preflight")
        self.assertTrue((run_root / "preflight" / f"{SNAPSHOT_ID}.json").is_file())
        self.stage("prepare")
        self.assertTrue((run_root / "resolver" / "analyses.tsv").is_file())
        self.stage("resolve")
        self.assertTrue((run_root / "resolver" / RECEIPT_FILENAME).is_file())
        self.stage("verify")
        self.assertFalse((run_root / "bundle").exists())

        rendered = self.stage("render")
        self.assertIn("Trait Ontology Mapping:", rendered.stdout)
        self.assertIn("Release policy: 3 included, 6 excluded", rendered.stdout)
        self.assertTrue((run_root / "bundle" / "release.yaml").is_file())
        self.assertFalse((self.fixture.registry_root / STORE_ID).exists())

        self.stage("emit")
        bundle_dir = self.fixture.registry_root / STORE_ID
        self.assertEqual(
            (bundle_dir / "analyses.tsv").read_bytes(),
            (run_root / "bundle" / "analyses.tsv").read_bytes(),
        )
        included = {
            row["analysis_id"]
            for row in _read_tsv(bundle_dir / "analyses.tsv")
            if row["exclude_from_build"] != "true"
        }
        self.assertEqual(included, EXPECTED_INCLUDED)
        self.assertEqual(bundle_module.check(bundle_module.load(STORE_ID, registry_root=self.fixture.registry_root), registry_root=self.fixture.registry_root), [])

    def test_all_stages_then_a_rerun_reuses_the_frozen_inventory(self) -> None:
        self.stage("all")
        inventory_bytes = self.fixture.inventory_path.read_bytes()
        frozen_mtime = self.fixture.provenance_path.stat().st_mtime_ns
        rerun = self.stage("all")
        self.assertIn("Found the frozen Source Inventory", rerun.stdout)
        self.assertEqual(self.fixture.inventory_path.read_bytes(), inventory_bytes)
        self.assertEqual(self.fixture.provenance_path.stat().st_mtime_ns, frozen_mtime)

    def test_a_changed_selection_under_the_same_snapshot_id_is_refused(self) -> None:
        self.stage("freeze")
        document = yaml.safe_load(self.fixture.config_path.read_text())
        document["selection"] = {"analyses": ["GCST90000001", "GCST90000004"]}
        self.fixture.config_path.write_text(yaml.safe_dump(document))
        for name in ("freeze", "preflight"):
            with self.subTest(stage=name):
                result = _run_cli(self.fixture, "--stage", name)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("a changed Selection is a new freeze", result.stderr + result.stdout)

    def _assert_preflight_names(self, expected: str) -> None:
        result = _run_cli(self.fixture, "--stage", "preflight")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(expected, result.stderr)
        report = json.loads(
            (self.fixture.work_root / STORE_ID / "preflight" / f"{SNAPSHOT_ID}.json").read_text()
        )
        self.assertTrue(any(expected in failure for failure in report["failures"]), report["failures"])

    def test_preflight_refuses_a_deleted_frozen_source_metadata_table(self) -> None:
        """Preflight proves the frozen snapshot, its connectors included (#282 review)."""
        self.stage("freeze")
        self.stage("preflight")
        frozen_table = self.fixture.inventory_path.with_name(f"{SNAPSHOT_ID}.source-metadata.tsv")
        frozen_table.unlink()
        self._assert_preflight_names(f"frozen source_metadata_table {frozen_table}")

    def test_preflight_refuses_an_edited_frozen_acquisition_manifest(self) -> None:
        self.stage("freeze")
        frozen_manifest = self.fixture.inventory_path.with_name(f"{SNAPSHOT_ID}.acquisition-manifest.tsv")
        frozen_manifest.write_text(
            frozen_manifest.read_text(encoding="utf-8").replace("example.invalid", "example.org"),
            encoding="utf-8",
        )
        self._assert_preflight_names("it was edited after the freeze")

    def test_a_release_config_with_no_connectors_needs_a_frozen_inventory(self) -> None:
        document = yaml.safe_load(self.fixture.config_path.read_text())
        del document["source_collection"]["connectors"]
        self.fixture.config_path.write_text(yaml.safe_dump(document))
        result = _run_cli(self.fixture, "--stage", "freeze")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("names no source_collection.connectors", result.stderr)

    def test_an_overridden_resolver_is_recorded_and_kept_out_of_the_repositorys_registry(
        self,
    ) -> None:
        """--resolver is verification-only: its receipts never reach stores/ (#287 review)."""
        self.stage("freeze")
        self.stage("resolve")
        receipt = _receipt_of(self.fixture)
        override = receipt["resolver"]["override"]
        self.assertEqual(override["path"], str(FAKE_RESOLVER))
        self.assertEqual(override["sha256"], _sha256_bytes(FAKE_RESOLVER.read_bytes()))
        # Into a temporary registry, as every verification run emits: allowed.
        generate_candidate.refuse_overridden_resolver(
            receipt, registry_root=self.fixture.registry_root, repo_root=REPO_ROOT
        )
        # Into the repository's own registry: refused before anything is written.
        with self.assertRaisesRegex(CandidateError, "--resolver"):
            generate_candidate.refuse_overridden_resolver(
                receipt, registry_root=REPO_ROOT / "stores", repo_root=REPO_ROOT
            )
        pinned = {**receipt, "resolver": {**receipt["resolver"], "override": None}}
        generate_candidate.refuse_overridden_resolver(
            pinned, registry_root=REPO_ROOT / "stores", repo_root=REPO_ROOT
        )

    def test_a_receipt_without_the_override_marker_is_kept_out_of_the_repositorys_registry(
        self,
    ) -> None:
        """A receipt from before the marker, or with it stripped, proves no pinned resolver."""
        markerless = {"resolver": {"argv": ["opengwasdb", "resolve-analyses"]}}
        with self.assertRaisesRegex(CandidateError, "re-run the resolve stage"):
            generate_candidate.refuse_overridden_resolver(
                markerless, registry_root=REPO_ROOT / "stores", repo_root=REPO_ROOT
            )
        # A temporary registry may still take it, for verification.
        generate_candidate.refuse_overridden_resolver(
            markerless, registry_root=self.fixture.registry_root, repo_root=REPO_ROOT
        )

    def test_a_relative_resolver_is_resolved_once_for_running_and_recording(self) -> None:
        """The path recorded is the path run, whatever the caller's working directory."""
        elsewhere = self.fixture.root / "elsewhere"
        (elsewhere / "bin").mkdir(parents=True)
        shutil.copy2(FAKE_RESOLVER, elsewhere / "bin" / "resolver")
        previous = Path.cwd()
        os.chdir(elsewhere)
        try:
            args = generate_candidate.parse_args(
                [STORE_ID, "--config", str(self.fixture.config_path), "--resolver", "bin/resolver"]
            )
        finally:
            os.chdir(previous)
        absolute = str(elsewhere / "bin" / "resolver")
        self.assertEqual(args.resolver, absolute)
        self.assertEqual(generate_candidate.resolver_override(args.resolver)["path"], absolute)
        pipeline = generate_candidate.Pipeline(args, REPO_ROOT, "test")
        argv = candidate_workflow.resolver_argv(
            resolver_bin=pipeline.args.resolver,
            manifest_path=Path("analyses.tsv"),
            records_dir=Path("records"),
            config=pipeline.config,
            cores=1,
        )
        self.assertEqual(argv[0], absolute)
        # The pinned resolver is still found on PATH, by name.
        pinned = generate_candidate.parse_args([STORE_ID, "--config", str(self.fixture.config_path)])
        self.assertEqual(pinned.resolver, "opengwasdb")

    def test_a_legacy_config_is_refused_by_the_entry_point(self) -> None:
        # Through `python3 -m`: an operator must see an error, never a traceback.
        result = _run_cli_subprocess(
            self.fixture,
            config_path=REPO_ROOT / "resources/generators/gwas-catalog-eur-hybrid/config-full.yaml",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("release_config_version", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class SharedVocabularyTests(unittest.TestCase):
    """The workflow uses the contract's and Manifest Generation's vocabulary, not its own copy (#250)."""

    def test_super_population_codes_come_from_the_contract(self) -> None:
        self.assertIs(candidate_workflow.SUPERPOPULATIONS, bundle_module.SUPERPOPULATIONS)

    def test_info_score_vocabulary_comes_from_the_generation_rules(self) -> None:
        from manifest_generation import generation_rules

        self.assertIs(candidate_workflow.INFO_SCORE_KINDS, generation_rules.INFO_SCORE_KINDS)


class ContentDigestMemoTests(unittest.TestCase):
    """The contract's content digest memo never answers for bytes a file no longer holds (#282 review).

    One run hashes each reference for its settings digest, the contract it binds
    and the contract it verifies. Remembering the digest spares re-reading a
    genome-scale file, but this is an integrity boundary: within one process, a
    reference rewritten between two of those reads must be read again.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="digest-memo-")
        self.path = Path(self._tmp.name) / "ref_freqs.tsv.gz"
        self.path.write_bytes(b"fixture reference A\n")
        candidate_workflow._SHA256_BY_FILE_IDENTITY.clear()
        self.addCleanup(candidate_workflow._SHA256_BY_FILE_IDENTITY.clear)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def rewrite_with_its_mtime_restored(self) -> bytes:
        """The reviewer's edit: different bytes, the same size and the same mtime."""
        stat = self.path.stat()
        self.path.write_bytes(b"fixture reference B\n")
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        after = self.path.stat()
        self.assertEqual((after.st_size, after.st_mtime_ns), (stat.st_size, stat.st_mtime_ns))
        return self.path.read_bytes()

    def test_a_same_size_rewrite_with_its_mtime_restored_is_read_again(self) -> None:
        # One process, nothing cleared between the two reads.
        self.assertEqual(candidate_workflow.sha256_file(self.path), _sha256_bytes(b"fixture reference A\n"))
        rewritten = self.rewrite_with_its_mtime_restored()
        self.assertEqual(candidate_workflow.sha256_file(self.path), _sha256_bytes(rewritten))

    def test_a_remembered_digest_is_not_returned_after_a_rewrite(self) -> None:
        # A file whose ctime has settled is remembered; any later write changes
        # its ctime, which utime cannot restore.
        with mock.patch.object(candidate_workflow, "_DIGEST_SETTLE_NS", 20_000_000):
            time.sleep(0.03)
            candidate_workflow.sha256_file(self.path)
            self.assertEqual(len(candidate_workflow._SHA256_BY_FILE_IDENTITY), 1)
            rewritten = self.rewrite_with_its_mtime_restored()
            self.assertEqual(candidate_workflow.sha256_file(self.path), _sha256_bytes(rewritten))

    def test_a_file_that_changes_while_it_is_read_is_not_remembered(self) -> None:
        real_open = Path.open

        def open_then_rewrite(path, *args, **kwargs):
            handle = real_open(path, *args, **kwargs)
            with open(self.path, "r+b") as fh:  # the builtin: not the patched Path.open
                fh.write(b"fixture reference B\n")
            return handle

        with mock.patch.object(candidate_workflow, "_DIGEST_SETTLE_NS", 20_000_000):
            time.sleep(0.03)
            with mock.patch.object(Path, "open", open_then_rewrite):
                candidate_workflow.sha256_file(self.path)
        self.assertEqual(candidate_workflow._SHA256_BY_FILE_IDENTITY, {})


# ---------------------------------------------------------------------------
# Resolution Records kept once per Source Collection, reused by every Selection
# (#284, ADR 0036). ``FAKE_RESOLVER_LOG`` records which Analyses a run actually
# resolved, as opposed to resumed.
# ---------------------------------------------------------------------------

COLLECTION_ID = "gwas-catalog-ssf"
TRIAL_STORE_ID = "OGS-99002"
READY_IDS = [row[0] for row in READY_ANALYSES]
#: Every outcome in the trial Selection is a success, so nothing in it is
#: re-run as a controlled failure would be.
TRIAL_IDS = ["GCST90000001", "GCST90000002", "GCST90000004"]


class SharedResolutionRecordsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="resolution-records-")
        self.fixture: Fixture = FixtureBuilder(Path(self._tmp.name)).build()
        self.area = ResolutionArea.for_collection(self.fixture.resolution_root, COLLECTION_ID)
        self._runs = 0

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------------

    def _all_success_outcomes(self) -> Path:
        path = self.fixture.root / "all-success.json"
        path.write_text("{}", encoding="utf-8")
        return path

    def _trial_config(self, analysis_ids: list[str]) -> Path:
        """A second Selection over the same Source Collection: a Trial Store Release.

        Its own snapshot id and an ``analyses`` list over the same connector
        files; the entry point's freeze stage freezes it.
        """
        document = yaml.safe_load(self.fixture.config_path.read_text(encoding="utf-8"))
        document["label"] = "fixture-trial"
        document["selection"] = {"analyses": list(analysis_ids)}
        document["source_collection"]["inventory"]["snapshot_id"] = "fixture-trial"
        config_path = self.fixture.root / "config-trial.yaml"
        config_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        return config_path

    def _run(self, *args: str, runner=_run_cli, **kwargs):
        """Run the entry point; return (result, the Analyses this run resolved)."""
        self._runs += 1
        log = self.fixture.root / f"resolved-{self._runs}.log"
        result = runner(self.fixture, *args, resolved_log=log, **kwargs)
        resolved = log.read_text(encoding="utf-8").split() if log.exists() else []
        return result, sorted(resolved)

    def _receipt(self, store_id: str = STORE_ID) -> dict:
        path = self.fixture.work_root / store_id / "resolver" / RECEIPT_FILENAME
        return json.loads(path.read_text(encoding="utf-8"))

    def _set_gate(self, name: str, value: float) -> None:
        document = yaml.safe_load(self.fixture.config_path.read_text(encoding="utf-8"))
        document["ancestry_assignment"]["gates"][name] = value
        self.fixture.config_path.write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
        )

    def _assert_names_kept_records(self, receipt: dict, expected_ids: list[str]) -> None:
        self.assertEqual(receipt["analysis_ids"], expected_ids)
        self.assertEqual(list(receipt["records"]), expected_ids)
        self.assertEqual(receipt["resolution_area"], str(self.area.root))
        for analysis_id, entry in receipt["records"].items():
            path = self.area.root / entry["record"]
            self.assertTrue(path.is_file(), path)
            self.assertEqual(path, self.area.record_path(
                KeptRecord(analysis_id, entry["fingerprint_digest"], entry["record_sha256"])
            ))
            data = path.read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), entry["record_sha256"])
            record = json.loads(data)
            self.assertEqual(record["analysis_id"], analysis_id)
            self.assertEqual(
                record["fingerprints"]["fingerprint_digest"], entry["fingerprint_digest"]
            )

    def _assert_emit_fails(self, expected: str) -> None:
        before = _candidate_tables_bytes(self.fixture.registry_root)
        result, _ = self._run("--stage", "emit")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(expected, result.stderr)
        self.assertEqual(_candidate_tables_bytes(self.fixture.registry_root), before)

    # -- 1. two Selections over one collection ---------------------------------

    def test_trial_then_full_release_resolve_each_shared_analysis_once(self) -> None:
        trial, resolved = self._run(
            store_id=TRIAL_STORE_ID, config_path=self._trial_config(TRIAL_IDS)
        )
        self.assertEqual(trial.returncode, 0, trial.stderr)
        self.assertEqual(resolved, TRIAL_IDS)

        full, resolved = self._run()
        self.assertEqual(full.returncode, 0, full.stderr)
        self.assertEqual(resolved, sorted(set(READY_IDS) - set(TRIAL_IDS)))

        trial_receipt = self._receipt(TRIAL_STORE_ID)
        full_receipt = self._receipt()
        self._assert_names_kept_records(trial_receipt, TRIAL_IDS)
        self._assert_names_kept_records(full_receipt, READY_IDS)
        # The shared Analyses' records are the same records, not re-measurements.
        for analysis_id in TRIAL_IDS:
            self.assertEqual(
                trial_receipt["records"][analysis_id], full_receipt["records"][analysis_id]
            )

        # Both candidates still verify and re-emit from their own receipts.
        for store_id, config in (
            (TRIAL_STORE_ID, self.fixture.root / "config-trial.yaml"),
            (STORE_ID, self.fixture.config_path),
        ):
            again, resolved = self._run("--stage", "emit", store_id=store_id, config_path=config)
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertEqual(resolved, [])

    def test_a_selection_under_its_own_settings_is_never_re_resolved(self) -> None:
        # Trial under settings A, the full release under settings B, then the
        # trial again under A: the B run must not evict the trial's records.
        outcomes = self._all_success_outcomes()
        trial_config = self._trial_config(TRIAL_IDS)
        trial, resolved = self._run(
            store_id=TRIAL_STORE_ID, config_path=trial_config, outcomes=outcomes
        )
        self.assertEqual(trial.returncode, 0, trial.stderr)
        self.assertEqual(resolved, TRIAL_IDS)
        first = self._receipt(TRIAL_STORE_ID)

        self._set_gate("tau", 0.6)
        full, resolved = self._run(outcomes=outcomes)
        self.assertEqual(full.returncode, 0, full.stderr)
        self.assertEqual(resolved, READY_IDS)
        self.assertNotEqual(
            self._receipt()["resolver"]["settings_digest"], first["resolver"]["settings_digest"]
        )

        again, resolved = self._run(
            store_id=TRIAL_STORE_ID, config_path=trial_config, outcomes=outcomes
        )
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(resolved, [])
        # The same records, byte for byte: the fake's metrics differ on every
        # resolution, so a re-resolution would have changed every checksum.
        self.assertEqual(self._receipt(TRIAL_STORE_ID)["records"], first["records"])

    def test_re_resolving_under_one_fingerprint_changes_the_record_checksum(self) -> None:
        # The premise of the test above: a record resolved again is a new record.
        outcomes = self._all_success_outcomes()
        self.assertEqual(self._run(outcomes=outcomes)[0].returncode, 0)
        first = self._receipt()["records"]
        shutil.rmtree(self.area.root / "checkpoint")
        result, resolved = self._run(outcomes=outcomes)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(resolved, READY_IDS)
        for analysis_id, entry in self._receipt()["records"].items():
            self.assertEqual(entry["fingerprint_digest"], first[analysis_id]["fingerprint_digest"])
            self.assertNotEqual(entry["record_sha256"], first[analysis_id]["record_sha256"])

    def test_the_work_root_holds_no_records(self) -> None:
        result, _ = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        run_root = self.fixture.work_root / STORE_ID / "resolver"
        self.assertFalse((run_root / "records").exists())
        self.assertEqual(
            sorted(p.name for p in run_root.iterdir()),
            sorted(["analyses.tsv", "index.json", "resolve.log", RECEIPT_FILENAME]),
        )
        # The resolver's own records directory is the collection's checkpoint for
        # these resolver settings.
        receipt = self._receipt()
        checkpoint = self.area.checkpoint_dir(receipt["resolver"]["settings_digest"])
        self.assertEqual(checkpoint.parent, self.area.root / "checkpoint")
        self.assertIn(str(checkpoint), receipt["resolver"]["argv"])

    # -- 2. reuse and invalidation ---------------------------------------------

    def test_an_unchanged_run_re_resolves_nothing(self) -> None:
        # Both runs through `python3 -m`: this is the reuse behaviour an operator
        # sees, including the second run starting from no in-memory state.
        outcomes = self._all_success_outcomes()
        first, resolved = self._run(outcomes=outcomes, runner=_run_cli_subprocess)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(resolved, READY_IDS)
        receipt = self._receipt()

        second, resolved = self._run(outcomes=outcomes, runner=_run_cli_subprocess)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(resolved, [])
        self.assertEqual(self._receipt()["records"], receipt["records"])

    def test_a_changed_file_re_resolves_only_its_analysis(self) -> None:
        outcomes = self._all_success_outcomes()
        self.assertEqual(self._run(outcomes=outcomes)[0].returncode, 0)
        before = self._receipt()["records"]

        source = self.fixture.root / "mirror" / "GCST90000003.h.tsv.gz"
        stat = source.stat()
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000_000))
        result, resolved = self._run(outcomes=outcomes)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(resolved, ["GCST90000003"])
        after = self._receipt()["records"]
        self.assertNotEqual(
            after["GCST90000003"]["fingerprint_digest"],
            before["GCST90000003"]["fingerprint_digest"],
        )
        self.assertEqual(
            {k: v for k, v in after.items() if k != "GCST90000003"},
            {k: v for k, v in before.items() if k != "GCST90000003"},
        )

    def test_a_changed_reference_re_resolves_and_keeps_the_old_records(self) -> None:
        outcomes = self._all_success_outcomes()
        self.assertEqual(self._run(outcomes=outcomes)[0].returncode, 0)
        before = self._receipt()["records"]

        reference = self.fixture.root / "reference" / "ref_freqs.tsv.gz"
        reference.write_bytes(reference.read_bytes() + b"# a new reference build\n")
        result, resolved = self._run(outcomes=outcomes)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(resolved, READY_IDS)
        after = self._receipt()["records"]
        for analysis_id in READY_IDS:
            self.assertNotEqual(
                after[analysis_id]["fingerprint_digest"], before[analysis_id]["fingerprint_digest"]
            )
            # A record is kept, never overwritten: the old receipt's still exist.
            self.assertTrue((self.area.root / before[analysis_id]["record"]).is_file())

    def test_a_reference_rewritten_in_place_is_caught_by_a_later_invocation(self) -> None:
        # Same length, same mtime, different bytes. A process recomputes the
        # reference's digest from the file and refuses to emit; the in-process
        # runner must not carry a digest memoised by the run before it (#282
        # review).
        outcomes = self._all_success_outcomes()
        self.assertEqual(self._run(outcomes=outcomes)[0].returncode, 0)
        # The provenance parse is memoised by file identity too, so a sidecar
        # rewritten with its mtime restored would be read as the bytes it used
        # to be. The runner leaves it as empty as a fresh process would (#290
        # review).
        self.assertEqual(
            freeze_module._parsed_provenance.cache_info().currsize, 0
        )

        reference = self.fixture.root / "reference" / "ref_freqs.tsv.gz"
        stat = reference.stat()
        original = reference.read_bytes()
        reference.write_bytes(bytes([original[0] ^ 0xFF]) + original[1:])
        os.utime(reference, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(reference.stat().st_size, stat.st_size)
        self.assertEqual(reference.stat().st_mtime_ns, stat.st_mtime_ns)

        self._assert_emit_fails("ancestry_reference_sha256")

    def test_a_changed_declared_source_build_re_resolves_not_resumes(self) -> None:
        """A record measured under one source build is not reused under another (opengwasdb#277)."""
        outcomes = self._all_success_outcomes()
        self.assertEqual(self._run(outcomes=outcomes)[0].returncode, 0)
        digest = self._receipt()["resolver"]["settings_digest"]

        # The table declares no build, so each Analysis's is the default: this
        # changes every one, and no resolver setting. Only `resolve` runs, as
        # preflight refuses a build the fixture's sidecars contradict.
        document = yaml.safe_load(self.fixture.config_path.read_text(encoding="utf-8"))
        document["defaults"]["source_genome_build"] = "GRCh37"
        self.fixture.config_path.write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
        )
        result, resolved = self._run("--stage", "resolve", outcomes=outcomes)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(resolved, READY_IDS)
        self.assertEqual(self._receipt()["resolver"]["settings_digest"], digest)

    def test_a_changed_setting_re_resolves_every_analysis(self) -> None:
        outcomes = self._all_success_outcomes()
        self.assertEqual(self._run(outcomes=outcomes)[0].returncode, 0)
        self._set_gate("tau", 0.6)
        result, resolved = self._run(outcomes=outcomes)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(resolved, READY_IDS)
        # ... and the run after it, unchanged again, re-resolves nothing.
        result, resolved = self._run(outcomes=outcomes)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(resolved, [])

    # -- 3. a receipt names exactly its records, and verification holds --------

    def test_a_missing_record_fails_verification(self) -> None:
        self.assertEqual(self._run()[0].returncode, 0)
        entry = self._receipt()["records"]["GCST90000002"]
        (self.area.root / entry["record"]).unlink()
        self._assert_emit_fails("missing")

    def test_a_stale_record_fails_verification(self) -> None:
        # The source changed after its record was kept: the record's fingerprint
        # no longer matches the file, so the candidate cannot be emitted from it.
        self.assertEqual(self._run()[0].returncode, 0)
        source = self.fixture.root / "mirror" / "GCST90000001.h.tsv.gz"
        stat = source.stat()
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000_000))
        self._assert_emit_fails("re-run resolution")

    def test_an_edited_record_fails_verification_by_its_checksum(self) -> None:
        self.assertEqual(self._run()[0].returncode, 0)
        path = self.area.root / self._receipt()["records"]["GCST90000001"]["record"]
        record = json.loads(path.read_text(encoding="utf-8"))
        record["fingerprints"]["resolution_config"]["gates"]["tau"] = 0.99
        record["fingerprints"]["fingerprint_digest"] = _fingerprint_digest(record["fingerprints"])
        path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
        self._assert_emit_fails("record_sha256")

    def test_a_receipt_naming_a_record_outside_its_selection_fails(self) -> None:
        trial = self._run(store_id=TRIAL_STORE_ID, config_path=self._trial_config(TRIAL_IDS))[0]
        self.assertEqual(trial.returncode, 0, trial.stderr)
        self.assertEqual(self._run()[0].returncode, 0)
        full = self._receipt()["records"]

        receipt_path = self.fixture.work_root / TRIAL_STORE_ID / "resolver" / RECEIPT_FILENAME
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["records"]["GCST90000003"] = full["GCST90000003"]
        receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        result, _ = self._run(
            "--stage", "emit",
            store_id=TRIAL_STORE_ID,
            config_path=self.fixture.root / "config-trial.yaml",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside", result.stderr)

    def _edit_receipt(self, edit) -> None:
        path = self.fixture.work_root / STORE_ID / "resolver" / RECEIPT_FILENAME
        receipt = json.loads(path.read_text(encoding="utf-8"))
        edit(receipt)
        path.write_text(json.dumps(receipt), encoding="utf-8")

    def test_a_receipt_without_a_resolution_area_is_refused(self) -> None:
        self.assertEqual(self._run()[0].returncode, 0)
        self._edit_receipt(lambda r: r.__setitem__("resolution_area", None))
        self._assert_emit_fails("resolution_area")
        self._edit_receipt(lambda r: r.pop("resolution_area"))
        self._assert_emit_fails("resolution_area")

    def test_a_receipt_with_an_unknown_key_is_refused(self) -> None:
        self.assertEqual(self._run()[0].returncode, 0)
        self._edit_receipt(lambda r: r.__setitem__("note", "hand-added"))
        self._assert_emit_fails("unknown key")

    def test_a_record_entry_with_an_unknown_key_is_refused(self) -> None:
        self.assertEqual(self._run()[0].returncode, 0)
        self._edit_receipt(
            lambda r: r["records"]["GCST90000001"].__setitem__("reused", True)
        )
        self._assert_emit_fails("GCST90000001")

    def test_a_historical_receipt_is_refused_by_name(self) -> None:
        self.assertEqual(self._run()[0].returncode, 0)
        receipt_path = self.fixture.work_root / STORE_ID / "resolver" / RECEIPT_FILENAME
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["receipt_schema_version"] = 1
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        self._assert_emit_fails("historical")

    # -- 5. concurrency --------------------------------------------------------

    def test_a_second_run_over_a_locked_collection_area_is_refused(self) -> None:
        with self.area.exclusive("a concurrent test run"):
            result, resolved = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("locked", result.stderr)
        self.assertIn("a concurrent test run", result.stderr)
        self.assertEqual(resolved, [])
        self.assertFalse((self.fixture.registry_root / STORE_ID).exists())
        self.assertEqual(list((self.area.root / "checkpoint").rglob("*.json")), [])
        # Once the lock is released the same run proceeds.
        result, _ = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_killed_run_leaves_the_lock_with_its_running_resolver(self) -> None:
        pause = self.fixture.root / "resolver-paused"
        command, env = _cli_invocation(
            self.fixture, extra_env={"FAKE_RESOLVER_PAUSE_FILE": str(pause)}
        )
        parent = subprocess.Popen(
            command, cwd=str(REPO_ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 60
            while not (pause.exists() and pause.read_text().strip()):
                self.assertLess(time.monotonic(), deadline, "the resolver never started")
                self.assertIsNone(parent.poll(), "the run exited before resolving")
                time.sleep(0.05)
            resolver_pid = int(pause.read_text())
            parent.kill()
            parent.wait()
            # The run is gone but its resolver is still writing: no second run.
            with self.assertRaises(ResolutionAreaLocked):
                with self.area.exclusive("a second run"):
                    pass
        finally:
            pause.unlink(missing_ok=True)
            if parent.poll() is None:
                parent.kill()
                parent.wait()
        deadline = time.monotonic() + 60
        while _pid_alive(resolver_pid):
            self.assertLess(time.monotonic(), deadline, "the resolver never finished")
            time.sleep(0.05)
        with self.area.exclusive("after the resolver finished"):
            pass

    def test_resume_is_refused_because_reuse_is_automatic(self) -> None:
        result, resolved = self._run("--resume")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("reuse is automatic", result.stderr)
        self.assertEqual(resolved, [])

    def test_keeping_a_record_never_replaces_different_bytes(self) -> None:
        self.assertEqual(self._run()[0].returncode, 0)
        receipt = self._receipt()
        entry = receipt["records"]["GCST90000001"]
        kept = self.area.root / entry["record"]
        kept.write_bytes(kept.read_bytes() + b" ")
        checkpoint = self.area.checkpoint_dir(receipt["resolver"]["settings_digest"])
        with self.assertRaisesRegex(ResolutionAreaError, "differ"):
            self.area.keep(checkpoint, "GCST90000001")

    def test_verifying_a_resolved_candidate_needs_no_lock(self) -> None:
        self.assertEqual(self._run()[0].returncode, 0)
        with self.area.exclusive("another Selection resolving"):
            result, resolved = self._run("--stage", "emit")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(resolved, [])

    def test_the_lock_is_exclusive(self) -> None:
        with self.area.exclusive("first"):
            with self.assertRaises(ResolutionAreaLocked) as raised:
                with self.area.exclusive("second"):
                    pass
            self.assertIn("first", str(raised.exception))
        with self.area.exclusive("third"):
            pass


#: The pinned resolver, installed in this environment; absent, the test skips.
REAL_RESOLVER = Path(sys.executable).resolve().parent / "opengwasdb"
_PANEL_GROUPS = ("North-West Europe", "West Africa", "East Asia")
_PANEL_SUPERPOPS = {"North-West Europe": "EUR", "West Africa": "AFR", "East Asia": "EAS"}
_PANEL_VARIANTS = 60


def _panel_frequency(index: int, group: int) -> float:
    return 0.1 + 0.8 * (((index * 37 + group * 11) % 97) / 97)


def _real_ssf_body(analysis_id: str) -> bytes:
    """A tiny GWAS-SSF file the real resolver reads, with EUR-like frequencies."""
    seed = int(analysis_id[-2:])
    lines = [
        "chromosome\tbase_pair_location\teffect_allele\tother_allele\tbeta\t"
        "standard_error\teffect_allele_frequency\n"
    ]
    for index in range(_PANEL_VARIANTS):
        frequency = _panel_frequency(index, 0)
        beta = 0.01 * (((index + seed) % 7) - 3)
        lines.append(f"1\t{1000 + index}\tA\tC\t{beta:.4g}\t0.02\t{frequency:.6g}\n")
    return gzip.compress("".join(lines).encode("utf-8"), mtime=0)


def _real_ssf_body_without_af(analysis_id: str) -> bytes:
    """The same tiny GWAS-SSF file with no allele-frequency column (Sun 2018's shape)."""
    seed = int(analysis_id[-2:])
    lines = ["chromosome\tbase_pair_location\teffect_allele\tother_allele\tbeta\tstandard_error\n"]
    for index in range(_PANEL_VARIANTS):
        beta = 0.01 * (((index + seed) % 7) - 3)
        lines.append(f"1\t{1000 + index}\tA\tC\t{beta:.4g}\t0.02\n")
    return gzip.compress("".join(lines).encode("utf-8"), mtime=0)


#: Where the reference's palindromic (A/T) sites start: a source with
#: frequencies only there gives the ancestry fit nothing to read (#287 review).
_PALINDROMIC_START = 5000


def _real_ssf_body_palindromic_with_af(analysis_id: str) -> bytes:
    """Frequencies on every row, but only at palindromic reference sites (#287 review)."""
    lines = [
        "chromosome\tbase_pair_location\teffect_allele\tother_allele\tbeta\t"
        "standard_error\teffect_allele_frequency\n"
    ]
    for index in range(_PANEL_VARIANTS):
        frequency = _panel_frequency(index, 0)
        lines.append(f"1\t{_PALINDROMIC_START + index}\tA\tT\t0.01\t0.02\t{frequency:.6g}\n")
    return gzip.compress("".join(lines).encode("utf-8"), mtime=0)


def _real_ssf_body_af_without_usable_se(analysis_id: str) -> bytes:
    """Frequencies on every row; the panel rows' standard errors are unusable (#287 review).

    The rows off the panel are build-eligible, so the Analysis is not excluded
    for having no association; the fit still reads no frequency.
    """
    lines = [
        "chromosome\tbase_pair_location\teffect_allele\tother_allele\tbeta\t"
        "standard_error\teffect_allele_frequency\n"
    ]
    for index in range(_PANEL_VARIANTS):
        frequency = _panel_frequency(index, 0)
        lines.append(f"1\t{1000 + index}\tA\tC\t0.01\tNA\t{frequency:.6g}\n")
        lines.append(f"1\t{9000 + index}\tA\tC\t0.01\t0.02\t{frequency:.6g}\n")
    return gzip.compress("".join(lines).encode("utf-8"), mtime=0)


#: The no-AF fixture's bodies: two frequency-bearing sources the fit reads
#: nothing from, and sources with no frequency at all.
_FREQUENCY_BEARING_BODIES = {
    "GCST90000001": _real_ssf_body_palindromic_with_af,
    "GCST90000002": _real_ssf_body_af_without_usable_se,
}


def _no_af_fixture_body(analysis_id: str) -> bytes:
    return _FREQUENCY_BEARING_BODIES.get(analysis_id, _real_ssf_body_without_af)(analysis_id)


def _write_real_reference(fixture: Fixture) -> None:
    """A tiny ancestry-mixture reference over the panel the real bodies cover.

    The A/C sites at 1000+ are the panel the fit reads; the A/T sites at
    ``_PALINDROMIC_START``+ are on the reference too, but palindromic, so the
    fit never reads a frequency there.
    """
    reference = fixture.root / "reference"
    header = ["alid", "chromosome", "position", "effect_allele", "other_allele", "rsid"]
    rows = ["\t".join([*header, *_PANEL_GROUPS])]
    for index in range(_PANEL_VARIANTS):
        cells = [f"1:{1000 + index}:A:C", "1", str(1000 + index), "A", "C", f"rs{index}"]
        cells += [f"{_panel_frequency(index, g):.6g}" for g in range(len(_PANEL_GROUPS))]
        rows.append("\t".join(cells))
    for index in range(_PANEL_VARIANTS):
        position = _PALINDROMIC_START + index
        cells = [f"1:{position}:A:T", "1", str(position), "A", "T", f"rsp{index}"]
        cells += [f"{_panel_frequency(index, g):.6g}" for g in range(len(_PANEL_GROUPS))]
        rows.append("\t".join(cells))
    (reference / "ref_freqs.tsv.gz").write_bytes(
        gzip.compress(("\n".join(rows) + "\n").encode("utf-8"), mtime=0)
    )
    (reference / "ancestry_groups.tsv").write_text(
        "group\tsuper_pop\n"
        + "".join(f"{g}\t{_PANEL_SUPERPOPS[g]}\n" for g in _PANEL_GROUPS),
        encoding="utf-8",
    )


@unittest.skipUnless(REAL_RESOLVER.is_file(), "the pinned opengwasdb resolver is not installed")
class RealResolverReuseTests(unittest.TestCase):
    """The pinned `opengwasdb resolve-analyses` reuses a Resolution Area's records (#284)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="real-resolver-")
        self.fixture = FixtureBuilder(Path(self._tmp.name), bodies=_real_ssf_body).build()
        _write_real_reference(self.fixture)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_an_unchanged_second_run_resumes_every_record(self) -> None:
        receipts = []
        indexes = []
        for _ in range(2):
            result = _run_cli(self.fixture, "--stage", "resolve", resolver=REAL_RESOLVER)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            run_root = self.fixture.work_root / STORE_ID / "resolver"
            receipts.append(json.loads((run_root / RECEIPT_FILENAME).read_text()))
            indexes.append(json.loads((run_root / "index.json").read_text()))
        n = len(READY_ANALYSES)
        self.assertEqual(indexes[0]["n_total"], n)
        self.assertEqual(indexes[0]["n_success"], n, indexes[0])
        self.assertEqual(indexes[0]["n_resumed"], 0)
        self.assertEqual(indexes[1]["n_resumed"], n)
        self.assertEqual(receipts[1]["records"], receipts[0]["records"])


@unittest.skipUnless(REAL_RESOLVER.is_file(), "the pinned opengwasdb resolver is not installed")
class RealResolverNoAlleleFrequencyTests(unittest.TestCase):
    """Sources with no allele frequencies, such as Sun 2018 (#287's decision).

    Every Analysis is still resolved. The pinned resolver records a source with
    no frequencies as rows matched to the ancestry reference that contributed no
    frequency at all (``ancestry_sites`` 0); a release config that opts in takes
    the Source Metadata Table's ``source_superpopulation`` as Assigned Ancestry
    for such an Analysis, as ``source_trusted_no_af``, with a warning-level
    ``assigned_ancestry`` result. Without the opt-in it is ``ancestry_unassigned``.
    """

    #: One real resolution serves all three tests: the kept records, the resolver
    #: index and the receipt depend on the resolver settings, not on the release
    #: policy these tests vary, so a run per test would only re-run the pinned
    #: resolver to be handed the same records back. Each test then applies its
    #: policy variant in this process -- its own config and registry, over the
    #: class's Work Root -- and enters the pipeline at ``emit`` (#282).
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="real-resolver-no-af-")
        cls.root = Path(cls._tmp.name)
        cls.fixture = FixtureBuilder(cls.root / "fixture", bodies=_no_af_fixture_body).build()
        _write_real_reference(cls.fixture)
        # A MAF request makes the resolver count the rows with no usable
        # frequency (maf_rows_missing); 0 filters nothing (#287 review).
        document = yaml.safe_load(cls.fixture.config_path.read_text())
        document["defaults"]["maf_threshold"] = 0
        cls.fixture.config_path.write_text(yaml.safe_dump(document))
        resolved = _run_cli(cls.fixture, "--stage", "resolve", resolver=REAL_RESOLVER)
        if resolved.returncode != 0:
            raise AssertionError(f"the pinned resolver failed: {resolved.stderr}{resolved.stdout}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def setUp(self) -> None:
        # A per-test config and registry, so each test writes its own policy and
        # its own bundle; the Work Root is the class's, because it holds the run
        # artefacts the resolve stage bound -- the resolver index and the
        # receipt -- and verify recomputes the contract from the current config,
        # where the release policy does not appear.
        self.root = self.__class__.root / self._testMethodName
        self.config_path = self.root / "config.yaml"
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(self.fixture.config_path.read_text())
        self.registry_root = self.root / "stores"
        self.registry_root.mkdir(parents=True)
        self.work_root = self.fixture.work_root

    def opt_in(self) -> None:
        document = yaml.safe_load(self.config_path.read_text())
        document["release_policy"]["trust_source_ancestry_without_af"] = True
        self.config_path.write_text(yaml.safe_dump(document))

    def emitted(self) -> tuple[dict, dict, dict]:
        # The release policy is what varies here, and it lives outside the
        # resolution contract, so this re-renders from the records the class
        # already resolved rather than running the real resolver again: prepare,
        # verify, render and emit over the shared resolution (#282).
        result = _run_cli(
            self.fixture,
            "--stage",
            "emit",
            resolver=REAL_RESOLVER,
            config_path=self.config_path,
            registry_root=self.registry_root,
            work_root=self.work_root,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        bundle_dir = self.registry_root / STORE_ID
        analyses = {row["analysis_id"]: row for row in _read_tsv(bundle_dir / "analyses.tsv")}
        checks = {
            row["analysis_id"]: row
            for row in _read_tsv(bundle_dir / "sidecars" / "quality_checks.tsv")
        }
        validation = yaml.safe_load((bundle_dir / "validation.yaml").read_text())
        return analyses, checks, validation

    def test_the_record_reports_every_row_without_a_frequency(self) -> None:
        for analysis_id, _, _, _, _, _, _ in READY_ANALYSES:
            record = json.loads(_kept_record(self.fixture, analysis_id).read_text())
            diagnostics = record["diagnostics"]
            with self.subTest(analysis_id=analysis_id):
                self.assertEqual(record["status"], "success")
                self.assertIsNone(record["ancestry"]["assigned_ancestry"])
                # Every one of them looks alike to the ancestry fit: rows on the
                # reference, not one frequency read...
                self.assertEqual(diagnostics["ancestry_sites"], 0)
                self.assertGreater(diagnostics["ancestry_reference_rows_matched"], 0)
                self.assertEqual(diagnostics["maf_state"], "disabled")
                # ...and only the missing-frequency count tells them apart.
                if analysis_id in _FREQUENCY_BEARING_BODIES:
                    self.assertEqual(diagnostics["maf_rows_missing"], 0)
                else:
                    self.assertEqual(
                        diagnostics["maf_rows_missing"], diagnostics["canonical_rows_observed"]
                    )

    def test_opted_in_the_source_super_population_is_assigned_with_a_warning(self) -> None:
        self.opt_in()
        analyses, checks, validation = self.emitted()
        # Case-control with counts: in, on the source's super-population.
        for analysis_id in ("GCST90000004", "GCST90000005"):
            with self.subTest(analysis_id=analysis_id):
                row = analyses[analysis_id]
                self.assertEqual(row["exclude_from_build"], "")
                self.assertEqual(row["assigned_ancestry"], "EUR")
                self.assertEqual(row["ancestry_assignment_method"], "source_trusted_no_af")
                self.assertEqual(checks[analysis_id]["assigned_ancestry"], "warning")
                self.assertIn("source_superpopulation=EUR",
                              checks[analysis_id]["assigned_ancestry_measured"])
        # A quantitative source with no frequency still has no phenotype SD.
        self.assertEqual(
            analyses["GCST90000003"]["inclusion_reason"].split(":")[1].strip(),
            "sd_no_qualifying_evidence",
        )
        # Sources with frequencies the fit could not read never take the label.
        for analysis_id in _FREQUENCY_BEARING_BODIES:
            with self.subTest(analysis_id=analysis_id):
                self.assertEqual(checks[analysis_id]["exclusion_reason"], "ancestry_unassigned")
                self.assertEqual(checks[analysis_id]["assigned_ancestry"], "failed")
                self.assertNotEqual(
                    analyses[analysis_id]["ancestry_assignment_method"], "source_trusted_no_af"
                )
        summary = validation["quality_checks"]["assigned_ancestry"]
        self.assertEqual(summary["results"]["warning"], 7)
        self.assertEqual(summary["warned"], 2)
        self.assertTrue(
            any("source_trusted_no_af" in warning for warning in validation["warnings"]),
            validation["warnings"],
        )

    def test_without_the_opt_in_they_are_excluded_as_ancestry_unassigned(self) -> None:
        analyses, checks, _ = self.emitted()
        for analysis_id in ("GCST90000004", "GCST90000005"):
            with self.subTest(analysis_id=analysis_id):
                self.assertEqual(analyses[analysis_id]["exclude_from_build"], "true")
                self.assertEqual(checks[analysis_id]["exclusion_reason"], "ancestry_unassigned")
                self.assertEqual(checks[analysis_id]["assigned_ancestry"], "failed")


class ResolutionRootTests(unittest.TestCase):
    """Where the shared area lives: a deployment root, resolved like the Artifact Root.

    The flag, then ``OPENGWASDB_RESOLUTION_ROOT``, then ``ogstores.yaml``'s
    ``resolution_root``, then the built-in default. A release config cannot
    name it, so two configs over one collection cannot get two areas.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="resolution-root-")
        self.root = Path(self._tmp.name)
        self.fixture = FixtureBuilder(self.root / "fixture").build()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_precedence_flag_env_repo_config_default(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        self.assertEqual(resolution_root(env={}, repo_root=repo), DEFAULT_RESOLUTION_ROOT)
        (repo / "ogstores.yaml").write_text("resolution_root: /from/config\n", encoding="utf-8")
        self.assertEqual(resolution_root(env={}, repo_root=repo), Path("/from/config"))
        env = {RESOLUTION_ROOT_ENV_VAR: "/from/env"}
        self.assertEqual(resolution_root(env=env, repo_root=repo), Path("/from/env"))
        self.assertEqual(
            resolution_root("/from/flag", env=env, repo_root=repo), Path("/from/flag")
        )

    def test_a_malformed_repo_config_value_is_refused(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        for text in ("resolution_root: ''\n", "resolution_root: relative/path\n", "- a list\n"):
            (repo / "ogstores.yaml").write_text(text, encoding="utf-8")
            with self.assertRaises(ResolutionAreaError):
                resolution_root(env={}, repo_root=repo)

    def test_the_tracked_repo_config_names_the_production_root(self) -> None:
        self.assertEqual(
            resolution_root(env={}, repo_root=REPO_ROOT), Path("/data/opengwasdb/work/resolution")
        )

    def test_the_pipeline_resolves_the_area_from_the_deployment_root(self) -> None:
        args = generate_candidate.parse_args(
            [STORE_ID, "--config", str(self.fixture.config_path),
             "--resolution-root", str(self.root / "flag")]
        )
        pipeline = generate_candidate.Pipeline(args, REPO_ROOT, "test")
        self.assertEqual(pipeline.resolution_area.root, self.root / "flag" / COLLECTION_ID)

    def test_a_release_config_cannot_name_the_resolution_root(self) -> None:
        document = yaml.safe_load(self.fixture.config_path.read_text(encoding="utf-8"))
        document["output"]["resolution_root"] = str(self.root / "from-config")
        self.fixture.config_path.write_text(yaml.safe_dump(document), encoding="utf-8")
        with self.assertRaisesRegex(PreflightConfigError, "deployment"):
            load_candidate_configuration(self.fixture.config_path, REPO_ROOT)

    def test_a_collection_id_that_is_not_one_path_segment_is_refused(self) -> None:
        for bad in ("", "..", "a/b", ".hidden"):
            with self.assertRaises(ValueError):
                ResolutionArea.for_collection(self.root, bad)


class InProcessEntryPointTests(unittest.TestCase):
    """``_run_cli`` reports an exit exactly as ``python3 -m`` would (#282 review).

    The in-process runner stands in for a process, so its return code and stderr
    must be what an operator would see: an integer ``SystemExit`` is that code,
    ``None`` or ``0`` is a clean exit, and any other value is a message on stderr
    and exit code 1. Reporting a refusal as success would hide exactly the
    failures this suite exists to catch.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="in-process-exit-")
        root = Path(self._tmp.name)
        # No files: every path here is only rendered into the argv.
        self.fixture = Fixture(
            root=root,
            config_path=root / "config.yaml",
            inventory_path=root / "inventory.tsv",
            provenance_path=root / "provenance.json",
            source_metadata_table_path=root / "metadata.tsv",
            acquisition_manifest_path=root / "acquisition.tsv",
            outcomes_path=root / "outcomes.json",
            registry_root=root / "stores",
            work_root=root / "work",
            trait_table_dir=root / "traits",
            resolution_root=root / "resolution",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run_exiting(self, code) -> subprocess.CompletedProcess:
        def main(argv: list[str]) -> int:
            raise SystemExit(code)

        with mock.patch.object(generate_candidate, "main", main):
            return _run_cli(self.fixture)

    def test_a_string_exit_code_is_a_message_and_exit_code_one(self) -> None:
        result = self._run_exiting("the resolver is not installed")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stderr, "the resolver is not installed\n")

    def test_no_exit_code_or_zero_is_a_clean_exit(self) -> None:
        for code in (None, 0):
            with self.subTest(code=code):
                result = self._run_exiting(code)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")

    def test_an_integer_exit_code_is_that_code(self) -> None:
        self.assertEqual(self._run_exiting(3).returncode, 3)

    def test_a_returned_none_is_a_clean_exit(self) -> None:
        with mock.patch.object(generate_candidate, "main", lambda argv: None):
            self.assertEqual(_run_cli(self.fixture).returncode, 0)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie is finished; only its reaping is left.
    stat = Path(f"/proc/{pid}/stat")
    return not (stat.exists() and stat.read_text().split(")")[-1].split()[0] == "Z")


if __name__ == "__main__":
    unittest.main(verbosity=2)
