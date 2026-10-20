#!/usr/bin/env python3
"""The release config: one schema for every Store Release Manifest Generation runs (#287).

A release config is the one connector file written per Store Release (ADR 0036):
identity, the Source Collection and its connector files or frozen inventory, the
Selection, the Build Recipe shape, Quality Check overrides, Reference Resources
(the Canonical Trait Mapping Table among them) and the collection-wide defaults.
Loading is a gate: an unknown key at any level, a missing required field and the
legacy Selection grammar are refused, never ignored.

Run from the repository root:
    python3 tests/release-config/test_release_config.py
"""

from __future__ import annotations

import copy
import csv
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable

import yaml

from manifest_generation.release_config import (
    RELEASE_CONFIG_VERSION,
    ReleaseConfigError,
    load_release_config,
)
from manifest_generation.selection import SelectionError
from manifest_generation.source_inventory import frozen_for_release_problems, sha256_file

REPO_ROOT = Path(__file__).resolve().parents[2]


def minimal_config(root: Path) -> dict[str, Any]:
    """The smallest valid release config, with every path under ``root``."""
    (root / "reference").mkdir(exist_ok=True)
    return {
        "release_config_version": RELEASE_CONFIG_VERSION,
        "label": "fixture-release",
        "description": "A fixture release.",
        "source_collection": {
            "source_collection_id": "gwas-catalog-ssf",
            "inventory": {"snapshot_id": "fixture-snapshot", "directory": str(root / "inventory")},
        },
        "selection": {"where": {"store_key": "hybrid__European"}},
        "build": {
            "layout": "hybrid",
            "completion_state": "observed_only",
            "command": "build-hybrid",
            "options": {"source-reader-capability": "opengwasdb.gwas-ssf"},
            "post": {"top_hits": False, "overview": True},
        },
        "defaults": {
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
                "resource_id": "fixture-ancestry-mixture",
                "kind": "ancestry_mixture",
                "location": str(root / "reference" / "ref_freqs.tsv.gz"),
                "fine_group_map": str(root / "reference" / "groups.tsv"),
            },
            {
                "resource_id": "canonical-trait-mapping-efo",
                "kind": "trait_ontology_mapping",
                "location": "resources/reference-resources/canonical-trait-mapping-efo",
            },
        ],
        "ancestry_assignment": {"reference_resource_id": "fixture-ancestry-mixture"},
        "trait_ontology_mapping": {"reference_resource_id": "canonical-trait-mapping-efo"},
        "output": {"work_root": str(root / "work")},
    }


class ReleaseConfigLoadingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="release-config-")
        self.root = Path(self._tmp.name)
        self.document = minimal_config(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def load(self, edit: Callable[[dict[str, Any]], None] | None = None):
        document = copy.deepcopy(self.document)
        if edit is not None:
            edit(document)
        path = self.root / "release.yaml"
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        return load_release_config(path, REPO_ROOT)

    def assertRefused(self, edit, *fragments: str, error=ReleaseConfigError) -> None:
        with self.assertRaises(error) as caught:
            self.load(edit)
        for fragment in fragments:
            self.assertIn(fragment, str(caught.exception))

    # -- valid configs --------------------------------------------------------

    def test_a_minimal_config_loads_with_its_defaults(self) -> None:
        config = self.load()
        self.assertEqual(config.label, "fixture-release")
        self.assertEqual(config.access_posture, "public")
        self.assertEqual(config.source_collection_id, "gwas-catalog-ssf")
        self.assertIsNone(config.connectors)
        self.assertEqual(config.inventory_snapshot_id, "fixture-snapshot")
        self.assertEqual(config.inventory_path, self.root / "inventory" / "fixture-snapshot.tsv")
        self.assertEqual(
            config.inventory_provenance_path, self.root / "inventory" / "fixture-snapshot.meta.yaml"
        )
        self.assertEqual(config.selection.as_dict(), {"where": {"store_key": "hybrid__European"}})
        self.assertEqual(config.build.layout, "hybrid")
        self.assertEqual(config.build.post, {"top_hits": False, "overview": True})
        # A Hybrid release gets the maintained hm3 Indexed Variant Subset by default.
        self.assertEqual([entry["name"] for entry in config.indexed_subsets], ["hm3"])
        self.assertEqual(config.method_tiers["quantitative"].stored_effect_scale, "sd")
        self.assertEqual(config.info_score_threshold, "0.6")
        self.assertIsNone(config.maf_threshold)
        self.assertEqual(config.selection_superpopulation, "EUR")
        self.assertFalse(config.require_maf_filtered)
        self.assertFalse(config.trust_source_ancestry_without_af)
        self.assertEqual(config.quality_check_overrides, {})
        self.assertEqual(
            config.canonical_trait_mapping_table,
            REPO_ROOT / "resources/reference-resources/canonical-trait-mapping-efo",
        )
        self.assertEqual(
            config.required_resource_ids,
            ("fixture-ancestry-mixture", "canonical-trait-mapping-efo"),
        )
        self.assertEqual(config.work_root, self.root / "work")
        self.assertEqual(config.cores, 1)

    def test_connectors_and_every_optional_block_load(self) -> None:
        def edit(document):
            document["access_posture"] = "controlled"
            document["notes"] = "Fixture notes."
            document["source_collection"]["connectors"] = {
                "source_metadata_table": str(self.root / "table.tsv"),
                "acquisition_manifest": str(self.root / "manifest.tsv"),
            }
            document["selection"] = {"analyses": ["GCST1", "GCST2"]}
            document["defaults"].update(
                source_genome_build="GRCh38",
                license="EBI GWAS Catalog terms of use",
                publication_doi="10.1/x",
                consortium="Fixture consortium",
                sample_size_scope="analysis_level",
                analysis_group_id="fixture-group",
                info_score_threshold=0.8,
                maf_threshold=0.005,
            )
            document["release_policy"].update(
                store_composition={"require_maf_filtered": True},
                trust_source_ancestry_without_af=True,
                maf_filter_exempt_genotyping_technologies=["Whole genome sequencing"],
            )
            document["quality_checks"] = {"build_eligible_rows": 5}
            document["ancestry_assignment"].update(
                maf_floor=0.01, extraction_panel=None,
                gates={"tau": 0.5, "delta": 0.2, "n_min": 200, "residual_max": 0.06},
            )
            document["effect_scale_validation"] = {
                "maf_min": 0.01, "maf_max": 0.5, "dispersion_max": 0.5, "reference_resources": [],
            }
            document["runtime"] = {"cores": 8, "min_free_gb": 10}

        config = self.load(edit)
        self.assertEqual(config.access_posture, "controlled")
        self.assertEqual(config.connectors.source_metadata_table, self.root / "table.tsv")
        self.assertEqual(config.selection.analyses, ("GCST1", "GCST2"))
        self.assertEqual(config.defaults["license"], "EBI GWAS Catalog terms of use")
        self.assertEqual(config.maf_threshold, "0.005")
        self.assertEqual(config.info_score_threshold, "0.8")
        self.assertTrue(config.require_maf_filtered)
        self.assertTrue(config.trust_source_ancestry_without_af)
        self.assertEqual(config.quality_check_overrides, {"build_eligible_rows": 5})
        self.assertEqual(config.ancestry_gates["n_min"], 200)
        self.assertEqual(config.cores, 8)

    def test_a_ragged_release_declares_no_indexed_subset(self) -> None:
        def edit(document):
            document["build"].update(layout="ragged", command="build-ragged-ssf")

        self.assertIsNone(self.load(edit).indexed_subsets)

    # -- refusals -------------------------------------------------------------

    def test_an_unknown_key_is_refused_at_every_level(self) -> None:
        cases = {
            "top level": lambda d: d.update(release_kind="full"),
            "source_collection": lambda d: d["source_collection"].update(store_key="x"),
            "inventory": lambda d: d["source_collection"]["inventory"].update(path="x.tsv"),
            "build": lambda d: d["build"].update(artifacts={}),
            "defaults": lambda d: d["defaults"].update(ancestry_assignment_method="x"),
            "method tier": lambda d: d["defaults"]["by_study_design"]["quantitative"].update(x=1),
            "release_policy": lambda d: d["release_policy"].update(candidates="x.tsv"),
            "store_composition": lambda d: d["release_policy"].update(
                store_composition={"require_maf_filtered": False, "x": 1}
            ),
            "reference resource": lambda d: d["reference_resources"][0].update(root="/x"),
            "ancestry_assignment": lambda d: d["ancestry_assignment"].update(enabled=True),
            "gates": lambda d: d["ancestry_assignment"].update(gates={"tau": 0.5, "x": 1}),
            "trait_ontology_mapping": lambda d: d["trait_ontology_mapping"].update(x=1),
            "output": lambda d: d["output"].update(registry_root="stores"),
            "runtime": lambda d: d.update(runtime={"cores": 1, "x": 1}),
        }
        for where, edit in cases.items():
            with self.subTest(where=where):
                self.assertRefused(edit, "unknown key")

    def test_a_missing_required_field_is_refused(self) -> None:
        cases = {
            "release_config_version": lambda d: d.pop("release_config_version"),
            "label": lambda d: d.pop("label"),
            "description": lambda d: d.update(description=""),
            "source_collection_id": lambda d: d["source_collection"].pop("source_collection_id"),
            "inventory": lambda d: d["source_collection"].pop("inventory"),
            "snapshot_id": lambda d: d["source_collection"]["inventory"].pop("snapshot_id"),
            "selection": lambda d: d.pop("selection"),
            "layout": lambda d: d["build"].pop("layout"),
            "command": lambda d: d["build"].pop("command"),
            "by_study_design": lambda d: d["defaults"].pop("by_study_design"),
            "sample_size_kind": lambda d: d["defaults"]["by_study_design"]["quantitative"].pop(
                "sample_size_kind"
            ),
            "selection_superpopulation": lambda d: d["release_policy"].pop(
                "selection_superpopulation"
            ),
            "reference_resources": lambda d: d.pop("reference_resources"),
            "trait_ontology_mapping": lambda d: d.pop("trait_ontology_mapping"),
            "work_root": lambda d: d["output"].pop("work_root"),
            "acquisition_manifest": lambda d: d["source_collection"].update(
                connectors={"source_metadata_table": "t.tsv"}
            ),
        }
        for field, edit in cases.items():
            with self.subTest(field=field):
                self.assertRefused(edit, field)

    def test_the_legacy_selection_grammar_is_refused(self) -> None:
        for legacy in (
            {"store_key": "hybrid__European"},
            {"study_design": "case-control", "n_analyses": 10},
            {"pubmed_id": 29875488, "ancestry_group": "European"},
        ):
            with self.subTest(selection=legacy):
                self.assertRefused(
                    lambda d, legacy=legacy: d.update(selection=legacy),
                    "legacy Selection",
                    error=SelectionError,
                )

    def test_another_schema_version_is_refused(self) -> None:
        self.assertRefused(lambda d: d.update(release_config_version=2), "release_config_version")

    def test_the_pre_287_candidate_workflow_config_is_refused(self) -> None:
        legacy = REPO_ROOT / "resources/generators/gwas-catalog-eur-hybrid/config-full.yaml"
        with self.assertRaises(ReleaseConfigError) as caught:
            load_release_config(legacy, REPO_ROOT)
        self.assertIn("release_config_version", str(caught.exception))

    def test_a_resource_reference_must_name_a_declared_resource_of_its_kind(self) -> None:
        self.assertRefused(
            lambda d: d["ancestry_assignment"].update(reference_resource_id="absent"),
            "absent", "not a declared Reference Resource",
        )
        self.assertRefused(
            lambda d: d["trait_ontology_mapping"].update(
                reference_resource_id="fixture-ancestry-mixture"
            ),
            "trait_ontology_mapping",
        )
        self.assertRefused(
            lambda d: d.update(
                effect_scale_validation={"reference_resources": [
                    {"ancestry": "EUR", "resource_id": "absent"}
                ]}
            ),
            "absent",
        )
        self.assertRefused(
            lambda d: d["reference_resources"].append(dict(d["reference_resources"][0])),
            "twice",
        )

    def test_values_outside_their_vocabulary_are_refused(self) -> None:
        cases = {
            "access_posture": lambda d: d.update(access_posture="open"),
            "selection_superpopulation": lambda d: d["release_policy"].update(
                selection_superpopulation="European"
            ),
            "source_genome_build": lambda d: d["defaults"].update(source_genome_build="hg38"),
            "maf_threshold": lambda d: d["defaults"].update(maf_threshold=0.7),
            "info_score_threshold": lambda d: d["defaults"].update(info_score_threshold=2),
            "quality_checks": lambda d: d.update(quality_checks={"no_such_check": 1}),
            "require_maf_filtered": lambda d: d["release_policy"].update(
                store_composition={"require_maf_filtered": True}
            ),
            "trust_source_ancestry_without_af": lambda d: d["release_policy"].update(
                trust_source_ancestry_without_af="yes"
            ),
            # Only a MAF request makes the record count rows with no frequency.
            "defaults.maf_threshold": lambda d: d["release_policy"].update(
                trust_source_ancestry_without_af=True
            ),
            "cores": lambda d: d.update(runtime={"cores": 0}),
            "source_collection_id": lambda d: d["source_collection"].update(
                source_collection_id="a/b"
            ),
        }
        for field, edit in cases.items():
            with self.subTest(field=field):
                self.assertRefused(edit, field)

    def test_the_resolution_root_is_not_a_release_config_key(self) -> None:
        self.assertRefused(
            lambda d: d["output"].update(resolution_root="/x"), "resolution_root", "deployment"
        )

    def test_a_config_that_is_not_a_mapping_is_refused(self) -> None:
        path = self.root / "list.yaml"
        path.write_text("- a\n- b\n", encoding="utf-8")
        with self.assertRaises(ReleaseConfigError):
            load_release_config(path, REPO_ROOT)
        with self.assertRaises(ReleaseConfigError):
            load_release_config(self.root / "absent.yaml", REPO_ROOT)


GENERATORS = REPO_ROOT / "resources" / "generators"
EUR_HYBRID = GENERATORS / "gwas-catalog-eur-hybrid"


def shipped_release_configs() -> list[Path]:
    return sorted(GENERATORS.glob("*/release-*.yaml"))


def bundle_analyses(store_id: str) -> list[str]:
    with (REPO_ROOT / "stores" / store_id / "analyses.tsv").open(encoding="utf-8") as fh:
        return [row["analysis_id"] for row in csv.DictReader(fh, delimiter="\t")]


class ShippedReleaseConfigTests(unittest.TestCase):
    """Every committed release config loads and names a frozen inventory that is its own."""

    def test_every_shipped_release_config_loads_and_its_inventory_is_its_own(self) -> None:
        configs = shipped_release_configs()
        self.assertGreaterEqual(len(configs), 3)
        for path in configs:
            with self.subTest(config=path.name):
                config = load_release_config(path, REPO_ROOT)
                provenance = yaml.safe_load(config.inventory_provenance_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    frozen_for_release_problems(provenance, config.inventory_provenance_path, config),
                    [],
                )
                self.assertEqual(
                    provenance["inventory_tsv_sha256"], sha256_file(config.inventory_path)
                )
                self.assertTrue(config.canonical_trait_mapping_table.is_dir())

    def test_every_shipped_snapshot_names_permanent_connector_originals(self) -> None:
        """Provenance names where the connector originals live, never a scratch path.

        On a host that has them, each original (and the acquisition pass
        lineage) still has the sha256 the freeze recorded; the frozen subsets
        beside the inventory are checked by ``load_frozen_connectors``.
        """
        from manifest_generation.freeze import load_frozen_connectors

        scratch = ("/tmp/", "/data/opengwasdb/work/")
        for path in shipped_release_configs():
            config = load_release_config(path, REPO_ROOT)
            provenance = yaml.safe_load(config.inventory_provenance_path.read_text(encoding="utf-8"))
            recorded = [
                provenance["source_metadata_table"]["original"],
                provenance["acquisition_manifest"]["original"],
            ]
            lineage = provenance["acquisition_manifest"]["original"]["pass_lineage"]
            if lineage is not None:
                recorded.append(lineage)
            for entry in recorded:
                with self.subTest(config=path.name, original=entry["path"]):
                    self.assertFalse(entry["path"].startswith(scratch), entry["path"])
                    original = Path(entry["path"])
                    if original.is_file():
                        self.assertEqual(sha256_file(original), entry["sha256"])
            table, manifest = load_frozen_connectors(config.inventory_provenance_path)
            self.assertEqual(len(table.rows), provenance["rows"])
            self.assertEqual(len(manifest.entries), provenance["rows"])

    def test_the_ogs_00011_config_reproduces_its_inventory_and_resolver_settings(self) -> None:
        from manifest_generation.candidate_workflow import (
            load_candidate_configuration,
            resolver_settings,
        )

        path = EUR_HYBRID / "release-eur-hybrid-full.yaml"
        config = load_candidate_configuration(path, REPO_ROOT)
        # The shared freeze of this Selection is the candidate-table freeze's
        # inventory, byte for byte.
        self.assertEqual(
            config.base.inventory_path.read_bytes(),
            (REPO_ROOT / "resources/inventories/gwas-catalog-ssf-eur-hybrid-2026-09-22.tsv").read_bytes(),
        )
        self.assertTrue(config.require_maf_filtered)
        self.assertEqual(config.target_ancestry, "EUR")
        self.assertEqual(len(config.effect_placeholder_exclusions), 27)
        self.assertEqual(set(config.maf_filter_exempt_analyses), {"GCST90428462", "GCST90428463"})
        # The resolver is passed exactly the settings OGS-00011 was resolved
        # under, so its Resolution Records are reused rather than re-measured.
        release = yaml.safe_load((REPO_ROOT / "stores/OGS-00011/release.yaml").read_text())
        resolved = next(
            command for command in release["generator"]["commands"]
            if command.startswith("opengwasdb resolve-analyses")
        ).split()
        settings = resolved[4:resolved.index("--n-workers")]
        self.assertEqual(resolver_settings(config), settings)

    def test_the_pilot_configs_select_exactly_their_trial_store_releases(self) -> None:
        for name, store_id in (
            ("release-eur-hybrid-pilot-10.yaml", "OGS-00004"),
            ("release-eur-hybrid-quant-pilot-10.yaml", "OGS-00005"),
        ):
            with self.subTest(config=name):
                config = load_release_config(EUR_HYBRID / name, REPO_ROOT)
                self.assertEqual(config.selection.form, "analyses")
                self.assertEqual(sorted(config.selection.analyses), sorted(bundle_analyses(store_id)))


if __name__ == "__main__":
    unittest.main()
