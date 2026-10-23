#!/usr/bin/env python3
"""Two eQTLGen-shaped probes through the shared pipeline, end to end (#291).

eQTLGen is one BESD file set holding an Analysis per probe. Its connectors say
so: a Source Metadata Table with a ``source_probe_id`` and the study's sample
size on every row, and an Acquisition Manifest in which every Analysis claims
the one file set (its prefix, with the members' own checksums). They go through
the one entry point -- ``prepare``, ``resolve`` against the stage tests' fake
resolver, ``render`` and ``emit`` -- into a temporary registry, so this suite
pins that the pipeline needs nothing eQTLGen-specific beyond its connectors and
its release config. It also pins the two BESD-specific steps inside that
pipeline: a prefix is verified through its member files, and the rendered
``release.yaml`` carries the prefix and build ``build-ragged-besd`` reads. Each
Analysis is named by its bare probe id and states its tissue in its own column,
and the Build Recipe passes ``build-ragged-besd`` no ``tissue``, which would name
it ``<probe>::<tissue>``: an ``analysis_id`` never contains ``:`` (issue #319).

The fixture machinery is the phase-b-candidate suite's, loaded by path because
its directory is not an importable package (see ``tests/packaging/test_imports``).

Run from the repository root:
    python3 tests/eqtlgen-pipeline/test_pipeline_end_to_end.py
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from manifest_generation.acquisition_manifest import ACQUISITION_MANIFEST
from manifest_generation.connector_tsv import write_connector
from manifest_generation.source_metadata_table import SOURCE_METADATA_TABLE
from ogstores import bundle as bundle_module
from ogstores import plan as plan_module

REPO_ROOT = Path(__file__).resolve().parents[2]
STAGE_TEST = REPO_ROOT / "tests" / "phase-b-candidate" / "test_phase_b_candidate.py"

STORE_ID = "OGS-99191"
SNAPSHOT_ID = "eqtlgen-fixture-2026-10-22"
TRAIT_TABLE_RESOURCE_ID = "canonical-trait-mapping-efo"
TISSUE = "whole_blood"
STUDY_SAMPLE_SIZE = "31684"
#: (probe, chromosome, position): two of OGS-00001's ten probes.
PROBES = [
    ("ENSG00000175445", "8", "19791998"),
    ("ENSG00000134243", "1", "109896382"),
]
#: The frozen inventory is in Analysis-id order, so every stage is.
PROBES.sort()
#: Bare probe ids: an ``analysis_id`` never contains ``:`` (issue #319).
ANALYSIS_IDS = [probe for probe, _, _ in PROBES]
#: The three members of the fixture file set, with literal bytes.
MEMBER_BODIES = {
    ".besd": b"fixture besd bytes " * 20,
    ".epi": b"8\tENSG00000175445\t0\t19791998\tENSG00000175445\t+\n"
            b"1\tENSG00000134243\t0\t109896382\tENSG00000134243\t-\n",
    ".esi": b"1\trs1\t0\t100\tA\tG\t0.25\n8\trs2\t0\t200\tC\tT\t0.5\n",
}
TRAIT_TABLE_TEXT = (
    "trait_label\ttrait_ontology_id\ttrait_ontology_label\n"
    "weight\tEFO:0004338\tbody weight\n"
)


def _stage_module():
    """The phase-b-candidate stage test, loaded for its fixture machinery."""
    spec = importlib.util.spec_from_file_location("p291_stage_fixture", STAGE_TEST)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve their annotations through sys.modules during exec.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def members_json(source_dir: Path, prefix_name: str) -> str:
    return json.dumps(
        {
            suffix: {
                "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
            }
            for suffix, body in MEMBER_BODIES.items()
        },
        sort_keys=True,
        separators=(",", ":"),
    )


class EqtlgenEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.stage = _stage_module()

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="eqtlgen-e2e-")
        self.tmp = Path(self._tmp.name)
        self.fixture = self.build_fixture()
        self.prefix = self.tmp / "source" / "pilot"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- the fixture ------------------------------------------------------

    def build_fixture(self):
        """Write the file set, the two connector files, the references and a release config."""
        stage = self.stage
        root = self.tmp
        reference = root / "reference"
        reference.mkdir(parents=True)
        (reference / "ref_freqs.tsv.gz").write_bytes(b"fixture reference\n")
        (reference / "ancestry_groups.tsv").write_text(
            "fine_group\tsuper_population\nEUR_A\tEUR\n", encoding="utf-8"
        )
        trait_table_dir = root / "resources" / "reference-resources" / TRAIT_TABLE_RESOURCE_ID
        trait_table_dir.mkdir(parents=True)
        (trait_table_dir / "resource.yaml").write_text(
            f"resource_id: {TRAIT_TABLE_RESOURCE_ID}\nkind: trait_ontology_mapping\nversion: 1\n"
            f"location: resources/reference-resources/{TRAIT_TABLE_RESOURCE_ID}/mapping.tsv\n",
            encoding="utf-8",
        )
        (trait_table_dir / "mapping.tsv").write_text(TRAIT_TABLE_TEXT, encoding="utf-8")

        source_dir = root / "source"
        source_dir.mkdir()
        prefix = source_dir / "pilot"
        for suffix, body in MEMBER_BODIES.items():
            Path(f"{prefix}{suffix}").write_bytes(body)
        members = members_json(source_dir, "pilot")
        data = MEMBER_BODIES[".besd"]

        acquisition_rows = []
        table_rows = []
        for (probe, chromosome, position), analysis_id in zip(PROBES, ANALYSIS_IDS):
            acquisition_rows.append({
                "source_analysis_id": analysis_id,
                "status": "ok",
                "location": str(prefix),
                "bytes": str(len(data)),
                "sha256": hashlib.sha256(data).hexdigest(),
                "original_location": "/mirror/eqtlgen-sparse",
                "original_sha256": "a" * 64,
                "reformat_tool": "manifest_generation.providers.eqtlgen.acquire fixture",
                "source_probe_id": probe,
                "members": members,
            })
            table_rows.append({
                "source_analysis_id": analysis_id,
                "source_probe_id": probe,
                "source_trait_label": probe,
                "study_design": "quantitative",
                "sample_size": STUDY_SAMPLE_SIZE,
                "tissue": TISSUE,
                "target_ensembl_gene_id": probe,
                "target_chromosome": chromosome,
                "target_start": position,
                "target_end": position,
            })
        table_header = [
            "source_analysis_id", "source_probe_id", "source_trait_label", "study_design",
            "sample_size", "tissue", "target_ensembl_gene_id", "target_chromosome",
            "target_start", "target_end",
        ]
        acquisition_header = [
            "source_analysis_id", "status", "location", "bytes", "sha256",
            "original_location", "original_sha256", "reformat_tool", "source_probe_id", "members",
        ]
        table_path = root / "eqtlgen.source-metadata.tsv"
        write_connector(table_path, SOURCE_METADATA_TABLE, table_header, table_rows)
        acquisition_path = root / "eqtlgen.acquisition-manifest.tsv"
        write_connector(acquisition_path, ACQUISITION_MANIFEST, acquisition_header, acquisition_rows)

        inventory_dir = root / "inventory"
        inventory_dir.mkdir()
        work_root = root / "work"
        registry_root = root / "stores"
        registry_root.mkdir()
        config = {
            "release_config_version": 1,
            "label": "pilot-fixture",
            "association_coverage": "cis",
            "access_posture": "public",
            "description": "two eQTLGen-shaped fixture probes",
            "source_collection": {
                "source_collection_id": "eqtlgen-cis",
                "connectors": {
                    "source_metadata_table": str(table_path),
                    "acquisition_manifest": str(acquisition_path),
                },
                "inventory": {"snapshot_id": SNAPSHOT_ID, "directory": str(inventory_dir)},
            },
            "selection": {"all": True},
            "build": {
                "layout": "ragged",
                "completion_state": "observed_only",
                "command": "build-ragged-besd",
                "options": {"source-build": "hg19"},
                "post": {"top_hits": False, "overview": False},
            },
            "defaults": {
                "source_genome_build": "GRCh37",
                "license": "eQTLGen Consortium",
                "consortium": "eQTLGen",
                "sample_size_scope": "analysis_level",
                "tissue": TISSUE,
                "by_study_design": {
                    "quantitative": {
                        "stored_effect_scale": "sd",
                        "original_effect_scale": "sd",
                        "original_sd_method": "estimated_from_source_maf",
                        "sample_size_kind": "total",
                    },
                },
            },
            "release_policy": {"selection_superpopulation": "EUR"},
            "reference_resources": [
                {
                    "resource_id": "ukb-ancestry-mixture-hg38",
                    "kind": "ancestry_mixture",
                    "location": str(reference / "ref_freqs.tsv.gz"),
                    "fine_group_map": str(reference / "ancestry_groups.tsv"),
                },
                {
                    "resource_id": TRAIT_TABLE_RESOURCE_ID,
                    "kind": "trait_ontology_mapping",
                    "location": str(trait_table_dir),
                },
            ],
            "ancestry_assignment": {
                "reference_resource_id": "ukb-ancestry-mixture-hg38",
                "maf_floor": 0.01,
                "extraction_panel": None,
                "gates": {"tau": 0.50, "delta": 0.20, "n_min": 5000, "residual_max": 0.06},
            },
            "effect_scale_validation": {"reference_resources": []},
            "trait_ontology_mapping": {"reference_resource_id": TRAIT_TABLE_RESOURCE_ID},
            "runtime": {"cores": 1, "min_free_gb": 0},
            "output": {"work_root": str(work_root)},
        }
        config_path = root / "release-pilot-fixture.yaml"
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        outcomes_path = root / "outcomes.json"
        outcomes_path.write_text(json.dumps({}), encoding="utf-8")
        return stage.Fixture(
            root=root,
            config_path=config_path,
            inventory_path=inventory_dir / f"{SNAPSHOT_ID}.tsv",
            provenance_path=inventory_dir / f"{SNAPSHOT_ID}.meta.yaml",
            source_metadata_table_path=table_path,
            acquisition_manifest_path=acquisition_path,
            outcomes_path=outcomes_path,
            registry_root=registry_root,
            work_root=work_root,
            trait_table_dir=trait_table_dir,
            resolution_root=root / "resolution",
        )

    # -- the run ----------------------------------------------------------

    def test_two_probes_go_through_prepare_resolve_render_and_emit(self) -> None:
        result = self.stage._run_cli(self.fixture, "--stage", "all", store_id=STORE_ID)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        run_root = self.fixture.work_root / STORE_ID

        # prepare: BESD's reader, the file set's prefix, each Analysis's probe,
        # the tier, and the study's sample size on every probe.
        manifest = self.stage._read_tsv(run_root / "resolver" / "analyses.tsv")
        self.assertEqual([row["analysis_id"] for row in manifest], ANALYSIS_IDS)
        self.assertEqual([row["source_probe_id"] for row in manifest], [p for p, _, _ in PROBES])
        for row in manifest:
            self.assertEqual(row["source_reader_capability"], "opengwasdb.besd")
            self.assertEqual(row["source_file"], str(self.prefix))
            self.assertEqual(row["source_genome_build"], "GRCh37")
            self.assertEqual(row["stored_effect_scale"], "sd")
            self.assertEqual(row["original_sd_method"], "estimated_from_source_maf")
            self.assertEqual(row["sample_size"], STUDY_SAMPLE_SIZE)
        receipt = json.loads((run_root / "resolver" / "resolution_receipt.json").read_text())
        self.assertEqual(sorted(receipt["records"]), sorted(ANALYSIS_IDS))

        bundle_dir = self.fixture.registry_root / STORE_ID
        release = yaml.safe_load((bundle_dir / "release.yaml").read_text())
        self.assertEqual(release["status"], "candidate")
        # render: the prefix and the build the BESD builder reads.
        self.assertEqual(release["source_snapshot"]["besd_prefix"], str(self.prefix))
        self.assertEqual(release["source_snapshot"]["source_genome_build"], "hg19")
        build = yaml.safe_load((bundle_dir / "build.yaml").read_text())
        self.assertEqual(build["build"]["command"], "build-ragged-besd")
        # No `tissue` option: with one the builder names each Analysis
        # `<probe>::<tissue>` (issue #319).
        self.assertEqual(build["build"]["options"], {"source-build": "hg19"})

        analyses = self.stage._read_tsv(bundle_dir / "analyses.tsv")
        self.assertEqual([row["analysis_id"] for row in analyses], ANALYSIS_IDS)
        for row, (probe, chromosome, position) in zip(analyses, PROBES):
            self.assertEqual(row["analysis_label"], probe)
            self.assertEqual(row["tissue"], TISSUE)
            self.assertEqual((row["trait_chr"], row["trait_bp"]), (chromosome, position))
            self.assertEqual(row["source_reader_capability"], "opengwasdb.besd")
            self.assertEqual(row["stored_effect_scale"], "sd")
            self.assertEqual(row["sample_size"], STUDY_SAMPLE_SIZE)
            self.assertEqual(row["source_file"], str(self.prefix))
            self.assertEqual(row["assigned_ancestry"], "EUR")
            self.assertEqual(row["exclude_from_build"], "")

        # Analyses sharing one file set are not duplicates of each other: no
        # duplicate-content review is raised for them.
        self.assertNotIn("duplicate-content", release["notes"])
        self.assertEqual(
            yaml.safe_load(self.fixture.provenance_path.read_text())["duplicate_content_groups"], []
        )

        loaded = bundle_module.load(STORE_ID, registry_root=self.fixture.registry_root)
        self.assertEqual(
            bundle_module.check(loaded, registry_root=self.fixture.registry_root), []
        )
        # The bundle's plan finds the BESD prefix it reads.
        self.assertEqual(plan_module._besd_prefix(loaded), self.prefix)

    # -- a prefix is verified through its member files ------------------

    def test_an_unchanged_file_set_resumes_and_resolves_nothing_again(self) -> None:
        first = self.stage._run_cli(self.fixture, "--stage", "all", store_id=STORE_ID)
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        resolved = self.tmp / "resolved.log"
        again = self.stage._run_cli(
            self.fixture, "--stage", "all", store_id=STORE_ID, resolved_log=resolved,
        )
        self.assertEqual(again.returncode, 0, again.stderr + again.stdout)
        self.assertFalse(resolved.exists() and resolved.read_text().strip(),
                         "an unchanged file set re-resolved an Analysis")

    def test_a_changed_member_file_makes_the_record_stale(self) -> None:
        first = self.stage._run_cli(self.fixture, "--stage", "all", store_id=STORE_ID)
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        member = Path(f"{self.prefix}.esi")
        stat = member.stat()
        # The same size with a new mtime, as a rewrite of the member would leave it.
        os.utime(member, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        stale = self.stage._run_cli(self.fixture, "--stage", "verify", store_id=STORE_ID)
        self.assertNotEqual(stale.returncode, 0)
        output = stale.stderr + stale.stdout
        self.assertIn("pilot.esi mtime", output)
        self.assertIn("re-run resolution", output)
        # A member that grew since the freeze is refused before resolution reads it.
        member.write_bytes(MEMBER_BODIES[".esi"] + b"2\trs3\t0\t300\tA\tT\t0.1\n")
        refused = self.stage._run_cli(self.fixture, "--stage", "preflight", store_id=STORE_ID)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("changed size since the freeze", refused.stderr + refused.stdout)
        self.assertIn(".esi", refused.stderr + refused.stdout)

    def test_a_missing_member_file_fails_preflight_by_name(self) -> None:
        first = self.stage._run_cli(self.fixture, "--stage", "freeze", store_id=STORE_ID)
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        Path(f"{self.prefix}.epi").unlink()
        refused = self.stage._run_cli(self.fixture, "--stage", "preflight", store_id=STORE_ID)
        self.assertNotEqual(refused.returncode, 0)
        output = refused.stderr + refused.stdout
        self.assertIn("source file(s) are missing", output)
        self.assertIn(".epi", output)


if __name__ == "__main__":
    unittest.main()
