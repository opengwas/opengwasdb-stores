#!/usr/bin/env python3
"""Reference completion in the release config and the bundle it renders (#340).

The ukb-b generator implemented ADR 0037 first, for OGS-00009 (#312). This is
that logic's home in the shared pipeline: a release config declares a completion
recipe and the Reference Resource it completes against, and the bundle the
pipeline emits declares both phases when -- and only when -- that reference is
`qualified` and `active`.

Fixture-scale and network-free. The Reference Resource declaration is a fixture
under the fixture's own repository root, so nothing here reads `/data`.

Run from the repository root:
    python3 tests/release-config/test_completion.py
"""

from __future__ import annotations

import copy
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable

import yaml

from manifest_generation import completion
from manifest_generation.candidate_workflow import (
    CandidateConfiguration,
    CandidateTables,
    load_candidate_configuration,
    render_build_yaml,
    render_release_yaml,
    render_validation_yaml,
)
from manifest_generation.release_config import (
    RELEASE_CONFIG_VERSION,
    ReleaseConfigError,
    load_release_config,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

LD_RESOURCE = "fixture-eur-ld"
PANEL = "/fixture/reference/ld"
STORE_ID = "OGS-99001"


def write_ld_resource(
    root: Path,
    *,
    resource_id: str = LD_RESOURCE,
    kind: str = "ld_panel",
    ancestry: str = "EUR",
    ancestry_dir: str | None = "EUR",
    status: str | None = "available",
    location: str = PANEL,
) -> Path:
    """The Reference Resource declaration `parse_completion` reads, as tracked ones are."""
    directory = root / "resources" / "reference-resources" / resource_id
    directory.mkdir(parents=True, exist_ok=True)
    document: dict[str, Any] = {
        "resource_id": resource_id,
        "kind": kind,
        "ancestry": ancestry,
        "genome_build": "GRCh38",
        "location": location,
        "location_kind": "external_directory",
    }
    if ancestry_dir is not None:
        document["ancestry_dir"] = ancestry_dir
    if status is not None:
        document["status"] = status
    path = directory / "resource.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def write_hm3_resource(root: Path) -> Path:
    """The maintained hm3 declaration a Dense config's default Indexed Variant Subset reads."""
    directory = root / "resources" / "reference-resources" / "hapmap3-hg38"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "resource.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "resource_id": "hapmap3-hg38",
                "kind": "variant_list",
                "location": "/fixture/reference/hapmap3-hg38/hm3.grch38.alid.txt",
                "genome_build": "GRCh38",
                "checksums": {"alid_sha256": "4a7c82aa625a006ff437dcaa2769b00e056413d54c72e98f7378327c69513398"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def minimal_config(root: Path) -> dict[str, Any]:
    """A Dense release config: the only layout a single-id completion is planned for."""
    return {
        "release_config_version": RELEASE_CONFIG_VERSION,
        "label": "fixture-release",
        "access_posture": "public",
        "description": "A fixture release.",
        "source_collection": {
            "source_collection_id": "gwas-catalog-ssf",
            "inventory": {"snapshot_id": "fixture-snapshot", "directory": str(root / "inventory")},
        },
        "selection": {"analyses": ["GCST1", "GCST2", "GCST3"]},
        "build": {
            "layout": "dense",
            "completion_state": "observed_only",
            "command": "build-dense-vcf",
            "options": {
                "source-reader-capability": "opengwasdb.gwas-ssf",
                "source-assembly": "hg38",
                "n-workers": 4,
            },
            "post": {"top_hits": True, "overview": True},
        },
        "defaults": {
            "source_genome_build": "GRCh38",
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
            {
                "resource_id": LD_RESOURCE,
                "kind": "ld_panel",
                "location": str(root / "reference" / "ld"),
                "location_kind": "external_directory",
            },
        ],
        "ancestry_assignment": {"reference_resource_id": "fixture-ancestry-mixture"},
        "trait_ontology_mapping": {"reference_resource_id": "canonical-trait-mapping-efo"},
        "output": {"work_root": str(root / "work")},
    }


def qualified_completion(**changes: Any) -> dict[str, Any]:
    """A completion recipe whose reference is qualified and active."""
    reference = {
        "resource_id": LD_RESOURCE,
        "declared_status": "available",
        "qualification": "qualified",
        "activation": "active",
        **changes.pop("reference", {}),
    }
    return {"reference": reference, "command": "complete-dense", "options": {"n-workers": 32}, **changes}


def candidate_tables() -> CandidateTables:
    """A minimal validated table set: the completion tests render documents, not membership."""
    return CandidateTables(
        analyses_tsv="",
        source_readiness_tsv="",
        ancestry_tsv="",
        sd_estimation_tsv="",
        exclusions_tsv="",
        reference_overlap_tsv="",
        reference_overlap={},
        reference_overlap_errors=(),
        store_composition={},
        inventory_rows=3,
        included_rows=3,
        excluded_rows=0,
        exclusion_counts={},
        ancestry_check="passed",
        sd_check="not_run",
        effect_scale_check="not_run",
        warnings=(),
    )


def render_release(config: CandidateConfiguration) -> dict[str, Any]:
    return render_release_yaml(
        store_id=STORE_ID,
        config=config,
        tables=candidate_tables(),
        created_at="2026-10-22T00:00:00Z",
        inventory_sha256="a" * 64,
        preflight_report=Path("preflight.json"),
        index_summary={},
        generator_version="test",
    )


def render_validation(completion=None) -> dict[str, Any]:
    return render_validation_yaml(
        tables=candidate_tables(),
        index_summary={},
        validated_at="2026-10-22T00:00:00Z",
        validator_name="test",
        trait_ontology_mapping={
            "canonical_trait_mapping_table": None,
            "method_counts": {},
            "source_id_without_label": 0,
            "rejected_source_terms": [],
        },
        completion=completion,
    )


class FixtureCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="completion-")
        self.root = Path(self._tmp.name)
        write_ld_resource(self.root)
        write_hm3_resource(self.root)
        (self.root / "reference" / "ld").mkdir(parents=True)
        (self.root / "reference" / "ref_freqs.tsv.gz").write_bytes(b"fixture\n")
        (self.root / "reference" / "groups.tsv").write_text(
            "fine_group\tsuper_population\nEUR_A\tEUR\n", encoding="utf-8"
        )
        self.document = minimal_config(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def edited(self, edit: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        document = copy.deepcopy(self.document)
        if edit is not None:
            edit(document)
        return document

    def write(self, document: dict[str, Any]) -> Path:
        path = self.root / "release.yaml"
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        return path

    # -- loaders ---------------------------------------------------------------

    def load(self, edit: Callable[[dict[str, Any]], None] | None = None):
        return load_release_config(self.write(self.edited(edit)), self.root)

    def candidate(self, edit: Callable[[dict[str, Any]], None] | None = None) -> CandidateConfiguration:
        return load_candidate_configuration(self.write(self.edited(edit)), self.root)

    def assertRefused(self, edit, *fragments: str) -> None:
        with self.assertRaises(ReleaseConfigError) as caught:
            self.load(edit)
        for fragment in fragments:
            self.assertIn(fragment, str(caught.exception))


class CompletionRecipeTests(FixtureCase):
    """The config gate: what a qualified, active completion recipe is, and what is refused."""

    def with_completion(self, block: Any) -> Callable[[dict[str, Any]], None]:
        def edit(document: dict[str, Any]) -> None:
            document["build"]["completion_state"] = "reference_completed"
            document["completion"] = block

        return edit

    def declaring_observed_only(self, block: Any) -> Callable[[dict[str, Any]], None]:
        def edit(document: dict[str, Any]) -> None:
            document["build"]["completion_state"] = "observed_only"
            document["completion"] = block

        return edit

    def observed_only(self, document: dict[str, Any]) -> None:
        document["build"]["completion_state"] = "observed_only"

    def reference_completed(self, document: dict[str, Any]) -> None:
        document["build"]["completion_state"] = "reference_completed"

    def test_a_qualified_active_reference_loads_as_a_recipe(self) -> None:
        block = qualified_completion()
        config = self.load(self.with_completion(block))

        recipe = config.completion
        self.assertIsNotNone(recipe)
        self.assertEqual(recipe.reference, block["reference"])
        self.assertEqual(recipe.command, "complete-dense")
        self.assertEqual(recipe.options, {"n-workers": 32})
        # The panel is the location the release config declares; the ancestry and
        # the directory complete-dense reads are its Reference Resource's.
        self.assertEqual(recipe.panel_location, str(self.root / "reference" / "ld"))
        self.assertEqual(recipe.ancestry, "EUR")
        self.assertEqual(
            recipe.declaration,
            self.root / "resources" / "reference-resources" / LD_RESOURCE / "resource.yaml",
        )

    def test_the_panel_is_a_resource_preflight_checks(self) -> None:
        without = self.load()
        with_completion = self.load(self.with_completion(qualified_completion()))
        self.assertNotIn(LD_RESOURCE, without.required_resource_ids)
        self.assertIn(LD_RESOURCE, with_completion.required_resource_ids)

    def test_the_complete_block_is_the_declared_panel_and_the_configs_flags(self) -> None:
        recipe = self.load(self.with_completion(qualified_completion())).completion
        self.assertEqual(
            completion.completion_block(recipe),
            {
                "command": "complete-dense",
                "options": {
                    "ld-panel": str(self.root / "reference" / "ld"),
                    "ancestry": "EUR",
                    "n-workers": 32,
                },
            },
        )

    def test_the_declared_status_is_the_resources_and_stays_apart_from_qualification(self) -> None:
        # `status: available` is not a qualification: a reference whose
        # declaration is available is still blocked while qualification is pending.
        recipe = self.load(
            self.with_completion(
                qualified_completion(
                    reference={"declared_status": "available", "qualification": "pending"}
                )
            )
        ).completion
        self.assertEqual(recipe.reference["declared_status"], "available")
        self.assertEqual(recipe.reference["qualification"], "pending")
        self.assertIsNone(completion.completion_block(recipe))
        self.assertIn("qualification pending", completion.blocked_reason(recipe.reference))

    # -- malformed references --------------------------------------------------

    def test_a_malformed_completion_reference_is_refused(self) -> None:
        well_formed = qualified_completion()["reference"]
        cases = {
            "the block is not a mapping": (["reference"], "completion"),
            "the reference is not a mapping": ({**qualified_completion(), "reference": LD_RESOURCE}, "completion.reference"),
            "a misspelt reference key": (
                {**qualified_completion(), "reference": {**well_formed, "qualifcation": "qualified"}},
                "qualifcation",
            ),
            "no resource_id": (
                {**qualified_completion(), "reference": {k: v for k, v in well_formed.items() if k != "resource_id"}},
                "resource_id",
            ),
            "no declared status": (
                {**qualified_completion(), "reference": {k: v for k, v in well_formed.items() if k != "declared_status"}},
                "declared_status",
            ),
            "no activation": (
                {**qualified_completion(), "reference": {k: v for k, v in well_formed.items() if k != "activation"}},
                "activation",
            ),
            "an unknown qualification": (
                {**qualified_completion(), "reference": {**well_formed, "qualification": "Qualified"}},
                "'Qualified'",
            ),
            "an unknown activation": (
                {**qualified_completion(), "reference": {**well_formed, "activation": "activ"}},
                "'activ'",
            ),
        }
        for case, (block, named) in cases.items():
            with self.subTest(case):
                self.assertRefused(self.with_completion(block), named)

    def test_a_misspelt_completion_key_is_refused(self) -> None:
        def edit(document: dict[str, Any]) -> None:
            document["completion"] = qualified_completion()
            document["completion"]["referance"] = document["completion"].pop("reference")

        self.assertRefused(edit, "referance")

    def test_a_reference_to_an_undeclared_or_wrong_kind_resource_is_refused(self) -> None:
        self.assertRefused(
            self.with_completion(qualified_completion(reference={"resource_id": "nonesuch"})),
            "nonesuch",
            "not a declared Reference Resource",
        )
        self.assertRefused(
            self.with_completion(
                qualified_completion(reference={"resource_id": "canonical-trait-mapping-efo"})
            ),
            "trait_ontology_mapping",
            "not a 'ld_panel'",
        )

    def test_a_missing_or_wrong_kind_declaration_is_refused(self) -> None:
        (self.root / "resources" / "reference-resources" / LD_RESOURCE / "resource.yaml").unlink()
        self.assertRefused(self.with_completion(qualified_completion()), "resource.yaml")

        write_ld_resource(self.root, kind="ancestry_mixture")
        self.assertRefused(self.with_completion(qualified_completion()), LD_RESOURCE, "ld_panel")

    def test_a_panel_whose_directory_is_not_its_ancestry_is_refused(self) -> None:
        # complete-dense joins one --ancestry into the panel path and the
        # Analyses' ancestry filter, which is what kept the `ukb` directory
        # unusable (#219).
        write_ld_resource(self.root, ancestry_dir="ukb")
        self.assertRefused(self.with_completion(qualified_completion()), "ancestry_dir")

    def test_a_reference_ancestry_that_is_not_the_selections_is_refused(self) -> None:
        def edit(document: dict[str, Any]) -> None:
            document["build"]["completion_state"] = "reference_completed"
            document["completion"] = qualified_completion()

        write_ld_resource(self.root, ancestry="AFR", ancestry_dir="AFR")
        self.assertRefused(edit, "AFR", "selection_superpopulation", "'EUR'")

    def test_a_declared_status_the_resource_does_not_declare_is_refused(self) -> None:
        write_ld_resource(self.root, status="absent")
        self.assertRefused(
            self.with_completion(qualified_completion()), "declared_status", "'available'"
        )

    # -- contradictory recipes ------------------------------------------------

    def test_complete_options_may_not_name_the_declared_panel(self) -> None:
        for key in ("ld-panel", "ancestry"):
            with self.subTest(key):
                self.assertRefused(
                    self.with_completion(qualified_completion(options={"n-workers": 32, key: "/elsewhere"})),
                    key,
                )

    def test_the_completion_command_is_the_dense_one(self) -> None:
        self.assertRefused(
            self.with_completion(qualified_completion(command="complete-hybrid")),
            "complete-hybrid",
            "complete-dense",
        )

    def test_only_a_dense_layout_may_declare_completion(self) -> None:
        def edit(document: dict[str, Any]) -> None:
            document["build"].update(layout="hybrid", command="build-hybrid")
            document["build"].pop("indexed_subsets", None)
            document["completion"] = qualified_completion()

        self.assertRefused(edit, "hybrid", "dense layout")

    def test_a_config_that_still_reserves_a_completed_id_is_refused(self) -> None:
        for key in ("completed_store_id", "planned_completed_store_id"):
            with self.subTest(key):
                def edit(document: dict[str, Any], key: str = key) -> None:
                    document[key] = "OGS-00031"

                self.assertRefused(edit, key, "ADR 0037")

    def test_declaring_the_completion_state_and_the_recipe_must_agree(self) -> None:
        # A config that means to complete says so, and says what it completes
        # against; one observed-only by policy may not also declare a recipe,
        # which would read as a completion that silently never happens.
        self.assertRefused(
            lambda document: document["build"].update(completion_state="reference_completed"),
            "reference_completed",
            "completion",
        )
        self.assertRefused(
            self.declaring_observed_only(qualified_completion()),
            "observed_only",
            "contradict",
        )

    def test_an_unknown_completion_state_is_refused(self) -> None:
        self.assertRefused(
            lambda document: document["build"].update(completion_state="completed"),
            "completed",
            "observed_only",
            "reference_completed",
        )


class CompletionBundleTests(FixtureCase):
    """The bundle a config renders: both phases when qualified and active, else observed-only."""

    def with_completion(self, block: Any) -> Callable[[dict[str, Any]], None]:
        def edit(document: dict[str, Any]) -> None:
            document["build"]["completion_state"] = "reference_completed"
            document["completion"] = block

        return edit

    def build_document(self, config: CandidateConfiguration) -> dict[str, Any]:
        return render_build_yaml(STORE_ID, config)

    def test_a_qualified_active_reference_emits_both_phases(self) -> None:
        config = self.candidate(self.with_completion(qualified_completion()))
        build = self.build_document(config)

        self.assertEqual(build["completion_state"], "reference_completed")
        self.assertEqual(
            build["complete"],
            {
                "command": "complete-dense",
                "options": {
                    "ld-panel": str(self.root / "reference" / "ld"),
                    "ancestry": "EUR",
                    "n-workers": 32,
                },
            },
        )
        self.assertEqual(
            list(build),
            ["store_id", "layout", "completion_state", "build", "complete", "post", "indexed_subsets"],
        )
        # The build block is byte-identical to the observed-only one: completing a
        # published Store must schedule no rebuild (ADR 0037, #305).
        observed = self.build_document(self.candidate())
        self.assertEqual(build["build"], observed["build"])
        self.assertEqual(
            {key: value for key, value in build.items() if key not in ("completion_state", "complete")},
            {key: value for key, value in observed.items() if key != "completion_state"},
        )
        self.assertIsNone(render_release(config)["derived_from"])

    def test_the_qualified_reference_is_recorded_and_named_in_the_notes(self) -> None:
        config = self.candidate(self.with_completion(qualified_completion()))
        release = render_release(config)
        self.assertIn(LD_RESOURCE, release["notes"])
        self.assertIn("both phases", release["notes"])
        self.assertEqual(
            completion.acceptance(config.completion),
            {
                "completion_reference": {
                    "resource_id": LD_RESOURCE,
                    "declared_status": "available",
                    "qualification": "qualified",
                    "activation": "active",
                    "blocked": False,
                }
            },
        )

    def test_a_reference_short_of_qualified_and_active_emits_observed_only(self) -> None:
        references = {
            "pending qualification": {"qualification": "pending"},
            "pending activation": {"activation": "pending"},
            "on hold and blocked": {"qualification": "hold", "activation": "blocked"},
            "not applicable": {"qualification": "not_applicable"},
        }
        observed_config = self.candidate()
        observed_build = self.build_document(observed_config)
        observed_release = render_release(observed_config)
        for case, changes in references.items():
            with self.subTest(case):
                config = self.candidate(
                    self.with_completion(qualified_completion(reference=changes))
                )
                build = self.build_document(config)
                self.assertEqual(build, {**observed_build, "completion_state": "observed_only"})
                self.assertNotIn("complete", build)

                # Everything but the paragraph that says why is the bundle a
                # config with no completion block emits.
                release = render_release(config)
                reason = completion.blocked_reason(config.completion.reference)
                paragraphs = release["notes"].split("\n\n")
                self.assertIn(
                    f"Reference completion (ADR 0037): blocked: {reason}; this bundle is "
                    "Observed-Only and declares no complete block.",
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

                # …and the Validation Record says so, where the ukb-b generator did.
                acceptance = completion.acceptance(config.completion)
                self.assertIs(acceptance["completion_reference"]["blocked"], True)
                for named in (LD_RESOURCE, changes.get("qualification", "qualified"),
                              changes.get("activation", "active")):
                    self.assertIn(named, reason)

    def test_a_config_with_no_completion_block_is_unchanged(self) -> None:
        config = self.candidate()
        self.assertIsNone(config.completion)
        self.assertIsNone(completion.completion_block(None))
        self.assertEqual(self.build_document(config)["completion_state"], "observed_only")
        self.assertNotIn("complete", self.build_document(config))
        self.assertNotIn("Reference completion", render_release(config)["notes"])

    def test_render_validation_yaml_records_a_declared_reference_only(self) -> None:
        self.assertNotIn("acceptance", render_validation())
        config = self.candidate(self.with_completion(qualified_completion()))
        record = render_validation(completion=config.completion)
        self.assertEqual(
            record["acceptance"]["completion_reference"]["resource_id"], LD_RESOURCE
        )
        self.assertIs(record["acceptance"]["completion_reference"]["blocked"], False)


if __name__ == "__main__":
    unittest.main()
