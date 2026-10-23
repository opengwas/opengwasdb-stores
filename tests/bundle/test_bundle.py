#!/usr/bin/env python3
"""Executable Release Bundle contract and summary tests (#127 and #131)."""

from __future__ import annotations

import builtins
import csv
import hashlib
import importlib.resources
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

import yaml
from opengwasdb.model import analyses as opengwasdb_analyses

REPO_ROOT = Path(__file__).resolve().parents[2]

import ogstores.bundle
from ogstores import bundle, paths
from ogstores.plan import plan

n_checks = 0


def record_check() -> None:
    global n_checks
    n_checks += 1


def write_bundle(
    registry: Path,
    store_id: str = "OGS-00090",
    *,
    release: dict[str, Any] | None = None,
    build: dict[str, Any] | None = None,
    analyses: str | None = None,
    status: str = "accepted",
) -> bundle.Bundle:
    """Write one minimal Release Bundle under `registry` and load it.

    Module-level so a class testing one part of the contract can sit beside the
    tests it belongs with instead of inheriting every test of the class that
    first needed the fixture.
    """
    root = registry / store_id
    root.mkdir(parents=True, exist_ok=True)
    release_data: dict[str, Any] = {
        "store_id": store_id,
        "label": "fixture",
        "association_coverage": "gwas",
        "status": status,
        "source_snapshot_id": "fixture-snapshot-v1",
        "derived_from": None,
        "created_at": "2026-09-17T00:00:00Z",
        "description": "Fixture Release Bundle",
        "generator": {"commands": ["fixtures/generate.py"]},
    }
    if release:
        release_data.update(release)
    build_data: dict[str, Any] = {
        "store_id": store_id,
        "layout": "dense",
        "completion_state": "observed_only",
        "build": {"command": "build-dense-vcf", "options": {}},
        "post": {
            "top_hits": False,
            "rho": False,
            "overview": True,
            "validate": True,
        },
    }
    if build:
        build_data.update(build)
    if analyses is None:
        analyses = (
            "analysis_id\tstored_effect_scale\tsample_size_kind\t"
            "sample_size_scope\tsample_size\toriginal_effect_scale\t"
            "original_sd_method\tassigned_ancestry\t"
            "ancestry_assignment_method\tchecksum\tchecksum_algorithm\t"
            "source_file\n"
            "FIXTURE_1\tsd\ttotal\tanalysis_level\t1000\tsd\t"
            "declared_standardised\tEUR\taf_assigned\t"
            f"{'a' * 64}\tsha256\t/data/source/fixture.tsv\n"
        )

    (root / "release.yaml").write_text(
        yaml.safe_dump(release_data), encoding="utf-8"
    )
    (root / "build.yaml").write_text(
        yaml.safe_dump(build_data), encoding="utf-8"
    )
    (root / "analyses.tsv").write_text(analyses, encoding="utf-8")
    return bundle.load(store_id, registry_root=registry)


# A Ragged Build Recipe: the only layout that can carry a coverage other than
# `gwas`, so the four-value vocabulary is exercised on it (#341).
RAGGED_BUILD: dict[str, Any] = {
    "layout": "ragged",
    "build": {"command": "build-ragged-ssf", "options": {}},
}


class TestBundleContract(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="ogstores_bundle_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def make_bundle(
        self,
        store_id: str = "OGS-00090",
        *,
        release: dict[str, Any] | None = None,
        build: dict[str, Any] | None = None,
        analyses: str | None = None,
        status: str = "accepted",
    ) -> bundle.Bundle:
        return write_bundle(
            self.tmp_dir,
            store_id,
            release=release,
            build=build,
            analyses=analyses,
            status=status,
        )

    def test_manifest_generation_rules_are_not_the_contracts(self) -> None:
        """Manifest Generation's rules are its own (#250).

        `src/manifest_generation/generation_rules.py` requires the columns a
        generator resolves, and checks the INFO and MAF requests on every row;
        the emitter and `bundle-check` run it beside the contract. The contract
        checks a request only where the shared Analysis schema does, on the
        rows that will be built, so an excluded audit row's request, and a
        missing generation column, are not its business.
        """
        base = self.make_bundle()
        header, row = (base.root / "analyses.tsv").read_text().strip().split("\n")
        names, values = header.split("\t"), row.split("\t")
        keep = [i for i, name in enumerate(names) if name != "assigned_ancestry"]
        info = "\tinfo_score_threshold\timputation_score_column\timputation_score_kind\timputation_score_provenance"
        header = "\t".join(names[i] for i in keep) + "\tsource_reader_capability\texclude_from_build\tmaf_threshold" + info
        built = "\t".join(values[i] for i in keep) + "\topengwasdb.gwas-ssf\t\tNaN\tNaN\t\t\t"
        audit = ("\t".join(values[i] for i in keep).replace("FIXTURE_1", "FIXTURE_2")
                 + "\topengwasdb.gwas-ssf\ttrue\t0.9\t0.6\t\t\t")
        breaking = self.make_bundle(store_id="OGS-00096", analyses="\n".join([header, built, audit]) + "\n")

        self.assertEqual(bundle.check(breaking, registry_root=self.tmp_dir), [])
        record_check()

    def test_numeric_info_score_threshold_names_its_reader_in_the_row(self) -> None:
        """The pinned OpenGWASDB validator reads the reader from the row alone.

        A declared column only means something to the GWAS-SSF reader, and the
        manifest validator cannot see the Build Recipe's default, so a numeric
        threshold without a per-row `source_reader_capability` is rejected.
        """
        base = self.make_bundle()
        header, row = (base.root / "analyses.tsv").read_text().strip().split("\n")
        candidate = self.make_bundle(analyses=(
            header + "\tinfo_score_threshold\timputation_score_column"
            "\timputation_score_kind\timputation_score_provenance\n"
            + row + "\t0.6\timputation_INFO\timputation_info"
            "\tprovider specification: INFO quality\n"
        ))
        errors = list(bundle.check(candidate, registry_root=self.tmp_dir))
        self.assertTrue(any("source_reader_capability" in error for error in errors), errors)

    def test_ci_population_is_every_registered_bundle_and_each_passes(self) -> None:
        registry = REPO_ROOT / "stores"
        store_ids = sorted(
            path.name
            for path in registry.iterdir()
            if path.is_dir() and paths.is_valid_store_id(path.name)
        )
        # The five bundles migrated in #252 take the next ids after the ones
        # reserved for OGS-00018-00021. Those, and OGS-00010, are retired unused
        # (#308, #312): ids are never reused. OGS-00027 is the full eQTLGen release (#314).
        allocated = [f"OGS-{number:05d}" for number in [*range(1, 18)]]
        self.assertEqual(
            store_ids,
            [store_id for store_id in allocated if store_id not in paths.RETIRED_STORE_IDS]
            + [f"OGS-{number:05d}" for number in range(22, 28)],
        )
        for store_id in store_ids:
            errors = bundle.check(
                bundle.load(store_id, registry_root=registry),
                registry_root=registry,
            )
            record_check()
            self.assertEqual(errors, [], f"{store_id}: {errors}")

    def test_ogs_00015_frozen_dev_input_is_not_scientific_acceptance(self) -> None:
        """OGS-00015 is the owner-authorized, frozen EAS input manifest, and its Store claims no scientific acceptance.

        #247 `accepted` was the registry's fixed-input acceptance gate, not a
        flag bypass and not scientific release validation: the exact
        241-Analysis membership was accepted and frozen for dev construction.
        Its Store was then built and validated (#331), and register's
        Validation Record replaced the frozen-input one, which stays in git at
        its `acceptance.commit`. Human scientific acceptance stays pending.
        """
        loaded = bundle.load("OGS-00015", registry_root=REPO_ROOT / "stores")
        record_check()
        self.assertEqual(loaded.status, self.MVP_STATUS)
        record_check()
        self.assertTrue(loaded.release.get("accepted_at"))
        record_check()
        self.assertEqual(
            loaded.release["source_snapshot_id"],
            "dbgap-phs002453-mvp-eas-metadata-2026-10-13",
        )
        record_check()
        self.assertNotIn("candidate-pending", loaded.release["source_snapshot_id"])
        notes = loaded.release["notes"]
        record_check()
        self.assertNotIn("ONLY so", notes)
        record_check()
        self.assertIn("owner-authorized", notes)
        record_check()
        self.assertIn("accepted and frozen", notes)
        snapshot = loaded.release["source_snapshot"]
        record_check()
        self.assertEqual(
            snapshot["resolver_source_evidence_scope"], "capped_pilot_diagnostic"
        )
        self.assert_mvp_built_record("OGS-00015", 241)
        record_check()
        summary = bundle.summarise(loaded)
        self.assertEqual(summary["n_analyses"], 241)
        record_check()
        self.assertEqual(summary["n_admitted"], 241)
        record_check()
        self.assertEqual(summary["n_excluded"], 0)
        record_check()
        self.assertFalse((REPO_ROOT / "stores" / "OGS-00021").exists())

    def test_ogs_00027_is_validated_and_scientifically_accepted(self) -> None:
        """The owner accepted the full eQTLGen candidate (#314 comment 6345246939).

        `accepted` is the fixed-input gate for building. The Store was then built
        and validated at opengwasdb d8896e4, and `register` replaced the
        candidate record with its own, carrying the acceptance evidence under
        `acceptance.acceptance` with the commit it came from. The owner then
        accepted the released Store scientifically (#314 comment 6360207599),
        so `human_acceptance` is `accepted` and the Release Status moved
        accepted -> built -> validated.
        """
        loaded = bundle.load("OGS-00027", registry_root=REPO_ROOT / "stores")
        record_check()
        self.assertEqual(list(bundle.check(loaded, registry_root=REPO_ROOT / "stores")), [])
        record_check()
        self.assertEqual(loaded.status, "validated")
        record_check()
        self.assertEqual(bundle.validate_status_transition("accepted", "built"), [])
        record_check()
        self.assertEqual(bundle.validate_status_transition("built", loaded.status), [])
        record_check()
        self.assertEqual(loaded.release["accepted_at"], "2026-10-22T16:00:43Z")
        record_check()
        self.assertEqual(loaded.validation["status"], "passed")
        record_check()
        self.assertEqual(
            loaded.validation["validator"]["version"],
            "opengwasdb@d8896e46677ae9d54f1fc08a974e8c7e41d105cc",
        )
        observed = loaded.validation["observed"]
        record_check()
        self.assertEqual(
            (observed["n_analyses"], observed["n_variants"], observed["n_associations"]),
            (19250, 8927061, 127112772),
        )
        carried = loaded.validation["acceptance"]
        record_check()
        self.assertEqual(carried["commit"], "2c0396c7280f38b0b9ca1465a5a5ed2a48c995ce")
        acceptance = carried["acceptance"]
        record_check()
        self.assertEqual(acceptance["status"], "accepted")
        record_check()
        self.assertIs(acceptance["membership_accepted"], True)
        record_check()
        self.assertIs(acceptance["membership_frozen"], True)
        record_check()
        self.assertEqual(acceptance["frozen_at"], "2026-10-22T16:00:43Z")
        record_check()
        self.assertEqual(acceptance["human_acceptance"], "accepted")
        record_check()
        self.assertEqual(acceptance["human_acceptance_date"], "2026-10-23")
        record_check()
        self.assertIn("issuecomment-6360207599", acceptance["human_acceptance_authorization"])
        record_check()
        self.assertIn("issuecomment-6345246939", acceptance["owner_authorization"])
        record_check()
        self.assertEqual(acceptance["owner_authorization_date"], "2026-10-22")
        summary = bundle.summarise(loaded)
        record_check()
        self.assertEqual((summary["n_analyses"], summary["n_admitted"], summary["n_excluded"]), (19250, 19250, 0))

    MVP_OBSERVED = {
        "OGS-00012": ("eur", 1669),
        "OGS-00013": ("afr", 1368),
        "OGS-00014": ("amr", 1053),
        "OGS-00015": ("eas", 241),
        "OGS-00016": ("meta", 1691),
    }
    # The MVP observed bundles' Release Status: the #221 batch built and
    # validated their Stores, which #331 published and recorded.
    MVP_STATUS = "validated"
    # The commit of each bundle's frozen-input Validation Record, which
    # register's record replaced and names as its `acceptance.commit`.
    MVP_FROZEN_INPUT_RECORD = {
        **dict.fromkeys(("OGS-00012", "OGS-00013", "OGS-00014", "OGS-00016"), "9c9b92bc4ffd112de30ab661924079140ecd45aa"),
        "OGS-00015": "fa2ea2cde7eeb06d1e52ad67c30e501743c66009",
    }
    # The runtime whose register wrote each bundle's Validation Record. The
    # #221 batch built the observed Stores, so that record names its runtime.
    # OGS-00012's completion published a Store of its own, and register wrote
    # the record for what it published (ADR 0037).
    MVP_RECORD_RUNTIME = {
        **dict.fromkeys(("OGS-00013", "OGS-00014", "OGS-00015", "OGS-00016"), "4e922566d7fa4c13a11be3637069da63e48be28c"),
        "OGS-00012": "d405de0a7e1be29b1fc1d2e0dfb1090f607ba08c",
    }

    def assert_mvp_built_record(self, store_id: str, n_analyses: int) -> None:
        """Register's passing Validation Record, over the frozen-input one.

        The record names the runtime that published the Store it describes.
        The owner authorization and the pending human acceptance that the
        frozen-input record held are in the notes, and that record itself in
        git at its `acceptance.commit`.
        """
        loaded = bundle.load(store_id, registry_root=REPO_ROOT / "stores")
        validation = loaded.validation
        record_check()
        self.assertEqual(validation["status"], "passed", store_id)
        record_check()
        self.assertEqual(
            validation["validator"],
            {"name": "opengwasdb validate", "version": f"opengwasdb@{self.MVP_RECORD_RUNTIME[store_id]}"},
            store_id,
        )
        record_check()
        self.assertEqual(validation["observed"]["n_analyses"], n_analyses, store_id)
        record_check()
        self.assertEqual(validation["acceptance"]["commit"], self.MVP_FROZEN_INPUT_RECORD[store_id], store_id)
        notes = " ".join(loaded.release["notes"].split())  # OGS-00015's notes are wrapped
        for authorization in ("6156153525", "6156153173"):
            record_check()
            self.assertIn(authorization, notes, store_id)
        record_check()
        self.assertIn("Human scientific acceptance remains pending.", notes, store_id)
        record_check()
        self.assertIn(f"/data/opengwasdb/stores/{store_id}/store.opengwasdb", notes, store_id)

    def test_mvp_observed_stores_cover_the_6022_partition_without_exclusions(self) -> None:
        total = 0
        for store_id, (_, expected) in self.MVP_OBSERVED.items():
            summary = bundle.summarise(
                bundle.load(store_id, registry_root=REPO_ROOT / "stores")
            )
            record_check()
            self.assertEqual(summary["n_manifest"], expected, store_id)
            record_check()
            self.assertEqual(summary["n_admitted"], expected, store_id)
            record_check()
            self.assertEqual(summary["n_excluded"], 0, store_id)
            total += summary["n_admitted"]
        record_check()
        self.assertEqual(total, 6022)
        # The MVP completion children are deferred, not registered.
        for child in ("OGS-00018", "OGS-00019", "OGS-00020", "OGS-00021"):
            record_check()
            self.assertFalse((REPO_ROOT / "stores" / child).exists(), child)

    def test_mvp_observed_stores_are_frozen_dev_inputs(self) -> None:
        for store_id, (slug, n_analyses) in self.MVP_OBSERVED.items():
            loaded = bundle.load(store_id, registry_root=REPO_ROOT / "stores")
            record_check()
            self.assertEqual(loaded.status, self.MVP_STATUS, store_id)
            record_check()
            self.assertTrue(loaded.release.get("accepted_at"), store_id)
            record_check()
            self.assertEqual(
                loaded.release["source_snapshot_id"],
                f"dbgap-phs002453-mvp-{slug}-metadata-2026-10-13",
                store_id,
            )
            record_check()
            self.assertNotIn("ONLY so", loaded.release["notes"], store_id)
            record_check()
            self.assertEqual(
                loaded.release["source_snapshot"]["resolver_source_evidence_scope"],
                "capped_pilot_diagnostic",
                store_id,
            )
            self.assert_mvp_built_record(store_id, n_analyses)

    def test_ogs_00014_preserves_unassigned_amr_analyses(self) -> None:
        rows = opengwasdb_analyses.read_analyses(
            REPO_ROOT / "stores" / "OGS-00014" / "analyses.tsv"
        ).rows
        record_check()
        self.assertEqual(len(rows), 1053)
        assigned = [r for r in rows if (r.get("assigned_ancestry") or "").strip()]
        unassigned = [r for r in rows if not (r.get("assigned_ancestry") or "").strip()]
        record_check()
        self.assertEqual(len(assigned), 850)
        record_check()
        self.assertEqual(len(unassigned), 203)
        record_check()
        self.assertTrue(all(r["assigned_ancestry"] == "AMR" for r in assigned))
        record_check()
        self.assertTrue(
            all(r["ancestry_assignment_method"] == "af_assigned" for r in assigned)
        )
        record_check()
        self.assertTrue(
            all(r["ancestry_assignment_method"] == "unassigned" for r in unassigned)
        )
        record_check()
        self.assertEqual(
            {r["source_ancestry_label"] for r in rows}, {"AMR"}
        )

    def test_mvp_source_label_tuples_per_store(self) -> None:
        # The corrected re-emission binds every row to its authority mapping
        # source label and the canonical (ancestry, method) tuple.
        expected = {
            "OGS-00012": ({"EUR": 1669}, {"EUR/af_assigned": 1669}),
            "OGS-00013": ({"AFR": 1368}, {"AFR/af_assigned": 1368}),
            "OGS-00014": ({"AMR": 1053}, {"AMR/af_assigned": 850, "/unassigned": 203}),
            "OGS-00016": ({"META": 1691}, {"/unassigned": 1691}),
        }
        for store_id, (labels, tuples) in expected.items():
            rows = opengwasdb_analyses.read_analyses(
                REPO_ROOT / "stores" / store_id / "analyses.tsv"
            ).rows
            label_counts: dict[str, int] = {}
            tuple_counts: dict[str, int] = {}
            for row in rows:
                label_counts[row["source_ancestry_label"]] = (
                    label_counts.get(row["source_ancestry_label"], 0) + 1
                )
                key = f"{row['assigned_ancestry']}/{row['ancestry_assignment_method']}"
                tuple_counts[key] = tuple_counts.get(key, 0) + 1
            record_check()
            self.assertEqual(label_counts, labels, store_id)
            record_check()
            self.assertEqual(tuple_counts, tuples, store_id)

    def test_ogs_00016_meta_is_canonically_empty_with_core_inference(self) -> None:
        rows = opengwasdb_analyses.read_analyses(
            REPO_ROOT / "stores" / "OGS-00016" / "analyses.tsv"
        ).rows
        record_check()
        self.assertEqual(len(rows), 1691)
        record_check()
        self.assertTrue(
            all(not (r.get("assigned_ancestry") or "").strip() for r in rows)
        )
        record_check()
        self.assertTrue(
            all(r["ancestry_assignment_method"] == "unassigned" for r in rows)
        )
        record_check()
        self.assertEqual(
            {r["source_ancestry_label"] for r in rows}, {"META"}
        )
        notes = bundle.load("OGS-00016", registry_root=REPO_ROOT / "stores").release["notes"]
        record_check()
        self.assertIn("raw core inference", notes)
        record_check()
        self.assertIn("EUR", notes)

    def test_mvp_nworkers_16_is_execution_only_amendment(self) -> None:
        # Owner-authorized execution-only amendment: n-workers 16 on the four MVP
        # observed builds. The accepted input manifest and identity are unchanged
        # (ADR 0004 material choices untouched; core ADR 0024 makes the worker
        # count a pure runtime knob).
        import hashlib

        analyses_sha = {
            "OGS-00012": "21cb48c43686dbc2b222e2a260ff8fa920d4367790111c3c0ebf3e3386625696",
            "OGS-00013": "2748ded2d45aceba88519e3d5736ee3c0ebf593373ec90ec5fadafe704d6d6c1",
            "OGS-00014": "ee22eb1f3a8cca2f0c11f71a5024388ef089fdc20524055751bee92a88f3a89d",
            "OGS-00016": "d1f40e98d6e28094dc4050628f54fa0532ce5b7087d4edbdf6e9027a068bdccf",
        }
        for store_id, digest in analyses_sha.items():
            build = yaml.safe_load(
                (REPO_ROOT / "stores" / store_id / "build.yaml").read_text(encoding="utf-8")
            )
            record_check()
            self.assertEqual(build["build"]["options"]["n-workers"], 16, store_id)
            loaded = bundle.load(store_id, registry_root=REPO_ROOT / "stores")
            argv = next(step for step in plan(loaded) if step.name == "build").argv
            record_check()
            self.assertEqual(argv[argv.index("--n-workers") + 1], "16", store_id)
            record_check()
            self.assertEqual(
                hashlib.sha256(
                    (REPO_ROOT / "stores" / store_id / "analyses.tsv").read_bytes()
                ).hexdigest(),
                digest,
                store_id,
            )
            record_check()
            self.assertEqual(loaded.status, self.MVP_STATUS, store_id)
            record_check()
            self.assertTrue(loaded.release.get("accepted_at"), store_id)
        # EAS and FinnGen recipes are untouched by the amendment.
        eas = yaml.safe_load((REPO_ROOT / "stores" / "OGS-00015" / "build.yaml").read_text())
        record_check()
        self.assertNotIn("n-workers", eas["build"]["options"])
        finngen = yaml.safe_load((REPO_ROOT / "stores" / "OGS-00017" / "build.yaml").read_text())
        record_check()
        self.assertEqual(finngen["build"]["options"]["n-workers"], 32)
        # The amendment is recorded and cites the governing ADRs.
        doc = (REPO_ROOT / "docs" / "ogs-00012-16-nworkers-16-amendment.md").read_text(
            encoding="utf-8"
        )
        record_check()
        self.assertIn("execution-only", doc.lower())
        record_check()
        self.assertIn("adr/0004-store-release-identity.md", doc)
        record_check()
        self.assertIn("0024-parallel-dense-vcf-build-disk-spill", doc)

    def test_ogs_00007_trait_ontology_is_source_provided_not_a_gene(self) -> None:
        """Issue #141: the Trait columns carry the source EFO term and label.

        The target gene's Ensembl identity must not appear in
        trait_ontology_id, and the authority name must not appear in
        trait_ontology_label. GCST90240123 is an aggregate over the
        seven-member 14-3-3 family and is identified by its SomaScan SeqId, not
        by one arbitrary member gene.
        """

        def curie(uri: str) -> str:
            segment = uri.rstrip("/").rsplit("/", 1)[-1]
            prefix, _, local = segment.rpartition("_")
            return f"{prefix.upper()}:{local}" if prefix else uri

        release_root = REPO_ROOT / "stores" / "OGS-00007"
        analyses = opengwasdb_analyses.read_analyses(
            release_root / "analyses.tsv"
        )
        targets = opengwasdb_analyses.read_analyses(
            release_root / "sidecars" / "analysis_targets.tsv"
        )
        targets_by_id = {row["source_analysis_id"]: row for row in targets.rows}

        self.assertTrue(
            set(analyses.fieldnames).isdisjoint(
                opengwasdb_analyses.RETIRED_ANALYSIS_COLUMNS
            )
        )
        for row in analyses.rows:
            target = targets_by_id[row["source_analysis_id"]]
            record_check()
            self.assertEqual(row["trait_ontology_id"], curie(target["trait_ontology_id"]))
            self.assertEqual(row["trait_ontology_label"], target["trait_ontology_name"])
            self.assertEqual(row["trait_ontology_mapping_method"], "source_provided")
            self.assertFalse(row["trait_ontology_id"].upper().startswith("ENSEMBL:"))
            self.assertNotEqual(row["trait_ontology_label"], "Ensembl")

        aggregate = next(
            row for row in analyses.rows if row["analysis_id"] == "GCST90240123"
        )
        target = targets_by_id["GCST90240123"]
        record_check()
        self.assertEqual(aggregate["trait_ontology_id"], "EFO:0020109")
        self.assertEqual(
            aggregate["trait_ontology_label"], "14-3-3 protein family measurement"
        )
        # The assay is the identity; the SomaScan SeqId is SomaLogic's stable
        # assay identifier. The protein that SomaScan.db maps the probe to is
        # target annotation, not the seven-member family's identity (the
        # tracked sidecar even disagrees with itself: source_target_symbol is
        # YWHAB while gene_name is YWHAH).
        record_check()
        self.assertEqual(aggregate["analysis_label"], "4707-50")
        self.assertNotEqual(aggregate["analysis_label"], target["gene_name"])
        self.assertEqual(aggregate["trait_chr"], target["chromosome"])
        self.assertEqual(aggregate["trait_bp"], target["gene_start"])

    def test_gene_shaped_trait_ontology_id_is_rejected(self) -> None:
        """Issue #141: a gene identifier in trait_ontology_id must not validate."""
        base = (
            "analysis_id\tstored_effect_scale\tsample_size_kind\t"
            "sample_size_scope\tsample_size\toriginal_effect_scale\t"
            "original_sd_method\tassigned_ancestry\t"
            "ancestry_assignment_method\tchecksum\tchecksum_algorithm\t"
            "source_file"
        )

        def analyses_with(ontology_id: str, label: str) -> str:
            return (
                f"{base}\ttrait_ontology_id\ttrait_ontology_label\n"
                "FIXTURE_1\tsd\ttotal\tanalysis_level\t1000\tsd\t"
                "declared_standardised\tEUR\taf_assigned\t"
                f"{'a' * 64}\tsha256\t/data/source/fixture.tsv\t{ontology_id}\t{label}\n"
            )

        for gene_shaped in (
            "ENSEMBL:ENSG00000128245",
            "ENSG00000128245",
            "ENSG00000128245.14",
            "HGNC:7533",
            "UNIPROT:P61981",
        ):
            checked = self.make_bundle(
                analyses=analyses_with(gene_shaped, "14-3-3 protein eta measurement")
            )
            errors = bundle.check(checked, registry_root=self.tmp_dir)
            record_check()
            self.assertTrue(
                any("gene-shaped trait_ontology_id" in error for error in errors),
                (gene_shaped, errors),
            )

        authority_label = self.make_bundle(
            store_id="OGS-00092",
            analyses=analyses_with("EFO:0020109", "Ensembl"),
        )
        label_errors = bundle.check(authority_label, registry_root=self.tmp_dir)
        record_check()
        self.assertTrue(
            any(
                "trait_ontology_label" in error and "authority" in error
                for error in label_errors
            ),
            label_errors,
        )

        # A list is checked term by term: a gene identifier is not hidden by
        # following an ontology term (#286). A label is checked whole, as #141
        # defines it, because a term's own label may contain a comma.
        listed_gene = self.make_bundle(
            store_id="OGS-00094",
            analyses=analyses_with(
                "EFO:0020109, ENSEMBL:ENSG00000128245",
                "14-3-3 protein family measurement, 14-3-3 protein eta",
            ),
        )
        listed_gene_errors = bundle.check(listed_gene, registry_root=self.tmp_dir)
        record_check()
        self.assertTrue(
            any("gene-shaped trait_ontology_id" in e for e in listed_gene_errors),
            listed_gene_errors,
        )
        record_check()
        self.assertEqual(
            bundle.trait_ontology_id_terms(" EFO:1 ,EFO:2, , MONDO:3"),
            ["EFO:1", "EFO:2", "", "MONDO:3"],
        )
        self.assertEqual(bundle.trait_ontology_id_terms(""), [])
        self.assertFalse(
            bundle.is_authority_name_trait_ontology_label("body mass index, Ensembl")
        )
        self.assertTrue(bundle.is_authority_name_trait_ontology_label(" Ensembl "))

        ok = self.make_bundle(
            store_id="OGS-00093",
            analyses=analyses_with("EFO:0020109", "14-3-3 protein family measurement"),
        )
        self.assertEqual(bundle.check(ok, registry_root=self.tmp_dir), [])

    def _trait_ontology_bundle(self, store_id: str, ontology_id: str, label: str) -> bundle.Bundle:
        return self.make_bundle(
            store_id=store_id,
            analyses=(
                "analysis_id\tstored_effect_scale\tsample_size_kind\t"
                "sample_size_scope\tsample_size\toriginal_effect_scale\t"
                "original_sd_method\tassigned_ancestry\t"
                "ancestry_assignment_method\tchecksum\tchecksum_algorithm\t"
                "source_file\ttrait_ontology_id\ttrait_ontology_label\n"
                "FIXTURE_1\tsd\ttotal\tanalysis_level\t1000\tsd\t"
                "declared_standardised\tEUR\taf_assigned\t"
                f"{'a' * 64}\tsha256\t/data/source/fixture.tsv\t{ontology_id}\t{label}\n"
            ),
        )

    def test_trait_ontology_id_is_a_curie_or_a_list_of_curies(self) -> None:
        """#286: each term of trait_ontology_id is a CURIE; a list is ", "-joined."""
        for number, ontology_id in enumerate(
            (
                "EFO:0004839",
                "Orphanet:101960",
                "OBA:VT0001253",
                "MONDO:0005150, EFO:1001492, MONDO:0005417",
            )
        ):
            with self.subTest(ontology_id=ontology_id):
                ok = self._trait_ontology_bundle(f"OGS-0010{number}", ontology_id, "a trait")
                record_check()
                self.assertEqual(bundle.check(ok, registry_root=self.tmp_dir), [])

        for number, ontology_id in enumerate(
            (
                "http://www.ebi.ac.uk/efo/EFO_0004839",
                "http://purl.obolibrary.org/obo/MONDO_0005148",
                "MONDO:0005150, http://www.ebi.ac.uk/efo/EFO_1001492",
                "EFO:0004471,EFO:0004340",
                "EFO:0004471, ",
                "EFO_0004471",
            )
        ):
            with self.subTest(ontology_id=ontology_id):
                bad = self._trait_ontology_bundle(f"OGS-0011{number}", ontology_id, "a trait")
                result = bundle.check(bad, registry_root=self.tmp_dir)
                record_check()
                self.assertTrue(
                    any("is not a CURIE" in error for error in result), (ontology_id, result)
                )
                self.assertEqual(list(result.tolerated), [])

    def test_ogs_00011_raw_uris_are_tolerated_and_disclosed(self) -> None:
        """#286: OGS-00011 was accepted with raw URIs; they are tolerated, never rewritten.

        The tolerance is scoped to that one release by name, like #134's, and is
        reported on check()'s second channel with its count (#142).
        """
        self.assertEqual(
            bundle.LEGACY_NON_CURIE_TRAIT_ONTOLOGY_RELEASES, frozenset({"OGS-00011"})
        )
        registry = REPO_ROOT / "stores"
        result = bundle.check(
            bundle.load("OGS-00011", registry_root=registry), registry_root=registry
        )
        record_check()
        self.assertEqual(result, [], result)
        gaps = [gap for gap in result.tolerated if gap.citation == "#286"]
        self.assertEqual(len(gaps), 1, result.tolerated)
        self.assertEqual(gaps[0].count, 2972)
        self.assertEqual(gaps[0].detail, "non-CURIE trait_ontology_id values")

    def test_one_pass_accumulates_independent_errors(self) -> None:
        checked = self.make_bundle()
        bad_release = dict(checked.release)
        for key in ("label", "source_snapshot_id", "generator"):
            bad_release.pop(key)
        bad_release.update({"store_id": "OGS-99999", "status": "invented"})
        bad_build = dict(checked.build)
        bad_build.update({"store_id": "OGS-99998", "layout": "other"})
        checked = replace(
            checked,
            release=bad_release,
            build=bad_build,
            analyses_path=checked.root / "absent.tsv",
        )

        errors = bundle.check(checked, previous_status="also-invented", registry_root=self.tmp_dir)
        record_check()
        self.assertIsInstance(errors, list)
        for fragment in (
            "'label'",
            "'source_snapshot_id'",
            "'generator'",
            "invalid status",
            "release.yaml store_id",
            "build.yaml store_id",
            "invalid layout",
            "invalid current status",
            "analyses file does not exist",
        ):
            self.assertTrue(any(fragment in error for error in errors), (fragment, errors))

    def test_check_never_raises_for_wrong_document_shapes_or_validator_failure(self) -> None:
        checked = self.make_bundle()
        hostile = replace(
            checked,
            release=["not", "a", "mapping"],  # type: ignore[arg-type]
            build="not a mapping",  # type: ignore[arg-type]
        )
        with mock.patch.object(
            opengwasdb_analyses,
            "validate_analyses",
            side_effect=RuntimeError("validator exploded"),
        ):
            errors = bundle.check(hostile, previous_status=hostile, registry_root=self.tmp_dir)
        record_check()
        self.assertIsInstance(errors, list)
        self.assertGreaterEqual(len(errors), 4)
        self.assertTrue(any("release.yaml must contain a mapping" in error for error in errors))
        self.assertTrue(any("build.yaml must contain a mapping" in error for error in errors))
        self.assertTrue(any("validator exploded" in error for error in errors))

    def test_required_identity_and_provenance_keys(self) -> None:
        checked = self.make_bundle()
        release = dict(checked.release)
        for key in bundle.RELEASE_REQUIRED_KEYS:
            release.pop(key, None)
        errors = bundle.check(replace(checked, release=release), registry_root=self.tmp_dir)
        record_check()
        for key in bundle.RELEASE_REQUIRED_KEYS:
            self.assertTrue(any(repr(key) in error for error in errors), (key, errors))

    def test_generator_requires_an_executed_command_log(self) -> None:
        """The reshaped generation record is a command log, not a dead name path."""
        checked = self.make_bundle()
        for bad in (None, [], ["   "], ["one", 2], "not-a-list"):
            errors = bundle.check(
                replace(
                    checked,
                    release={**checked.release, "generator": {"commands": bad}},
                ),
                registry_root=self.tmp_dir,
            )
            record_check()
            self.assertTrue(
                any("commands" in error for error in errors), (bad, errors)
            )

    def test_artifact_root_is_not_part_of_the_bundle_contract(self) -> None:
        checked = self.make_bundle()
        self.assertNotIn("artifacts", bundle.BUILD_REQUIRED_KEYS)
        self.assertEqual(bundle.check(checked, registry_root=self.tmp_dir), [])

        errors = bundle.check(
            replace(
                checked,
                build={
                    **checked.build,
                    "artifacts": {"root": "/data/opengwasdb/stores"},
                },
            ),
            registry_root=self.tmp_dir,
        )
        record_check()
        self.assertTrue(any("must not declare 'artifacts'" in error for error in errors))

    def _subset_declaration(self, **overrides: Any) -> dict[str, Any]:
        declaration: dict[str, Any] = {
            "name": "hm3",
            "resource_id": "hapmap3-hg38",
            "variant_list": (
                "/data/opengwasdb/reference/hapmap3-hg38/hm3.grch38.alid.txt"
            ),
            "reference_assembly": "GRCh38",
        }
        declaration.update(overrides)
        return declaration

    def test_indexed_subsets_are_optional_and_valid_on_dense_and_hybrid(self) -> None:
        checked = self.make_bundle()
        self.assertEqual(bundle.check(checked, registry_root=self.tmp_dir), [])
        record_check()

        dense = self.make_bundle(build={
            "indexed_subsets": [self._subset_declaration()],
        })
        self.assertEqual(bundle.check(dense, registry_root=self.tmp_dir), [])
        record_check()

        hybrid = self.make_bundle(build={
            "layout": "hybrid",
            "build": {"command": "build-hybrid", "options": {}},
            "indexed_subsets": [self._subset_declaration(name="eur", checksum="a" * 64)],
        })
        self.assertEqual(bundle.check(hybrid, registry_root=self.tmp_dir), [])
        record_check()

        empty = self.make_bundle(build={"indexed_subsets": []})
        self.assertEqual(bundle.check(empty, registry_root=self.tmp_dir), [])
        record_check()

    def test_indexed_subsets_reject_ragged_layout(self) -> None:
        ragged = self.make_bundle(build={
            "layout": "ragged",
            "build": {"command": "build-ragged-ssf", "options": {}},
            "indexed_subsets": [self._subset_declaration()],
        })
        errors = bundle.check(ragged, registry_root=self.tmp_dir)
        record_check()
        self.assertTrue(
            any("indexed_subsets" in error and "ragged" in error for error in errors),
            errors,
        )

    def test_a_declaration_passes_subset_options_through_but_not_any_value(self) -> None:
        """`options` reaches the CLI verbatim, so a value that cannot render is refused (#225)."""
        for policy in ("refuse", "skip"):
            accepted = self.make_bundle(build={
                "indexed_subsets": [
                    self._subset_declaration(options={"overflow-only": policy})
                ],
            })
            self.assertEqual(bundle.check(accepted, registry_root=self.tmp_dir), [])
            record_check()

        for value in ({"policy": "skip"}, None, []):
            refused = self.make_bundle(build={
                "indexed_subsets": [
                    self._subset_declaration(options={"overflow-only": value})
                ],
            })
            errors = bundle.check(refused, registry_root=self.tmp_dir)
            record_check()
            self.assertEqual(len(errors), 1, errors)
            self.assertIn("overflow-only", errors[0])
            self.assertIn("options", errors[0])

    def test_indexed_subsets_reject_malformed_declarations(self) -> None:
        cases: list[dict[str, Any]] = [
            {"indexed_subsets": "not-a-list"},
            {"indexed_subsets": ["not-a-mapping"]},
            {"indexed_subsets": [self._subset_declaration(name="")]},
            {"indexed_subsets": [self._subset_declaration(name="../escape")]},
            {
                "indexed_subsets": [
                    self._subset_declaration(name="dup"),
                    self._subset_declaration(name="dup"),
                ]
            },
            {"indexed_subsets": [self._subset_declaration(checksum="nothex")]},
            {"indexed_subsets": [self._subset_declaration(unknown_key=1)]},
            {"indexed_subsets": [self._subset_declaration(options=["a"])]},
            {
                "indexed_subsets": [
                    self._subset_declaration(options={"overwrite": True})
                ]
            },
            {
                "indexed_subsets": [
                    {
                        key: value
                        for key, value in self._subset_declaration().items()
                        if key != "resource_id"
                    }
                ]
            },
            {
                "indexed_subsets": [
                    {
                        key: value
                        for key, value in self._subset_declaration().items()
                        if key != "variant_list"
                    }
                ]
            },
            {
                "indexed_subsets": [
                    {
                        key: value
                        for key, value in self._subset_declaration().items()
                        if key != "reference_assembly"
                    }
                ]
            },
            {"indexed_subsets": None},
            {
                "indexed_subsets": [
                    self._subset_declaration(options={"variant-list": "/evil"})
                ]
            },
            {
                "indexed_subsets": [
                    self._subset_declaration(
                        options={"--reference-assembly": "hg19"}
                    )
                ]
            },
        ]
        for overrides in cases:
            checked = self.make_bundle(build=overrides)
            errors = bundle.check(checked, registry_root=self.tmp_dir)
            record_check()
            self.assertTrue(errors, overrides)

    def test_retired_analysis_columns_come_from_upstream_and_name_the_release(self) -> None:
        checked = self.make_bundle()
        lines = checked.analyses_path.read_text(encoding="utf-8").splitlines()
        retired = opengwasdb_analyses.RETIRED_ANALYSIS_COLUMNS
        retired_header = "\t".join(retired)
        retired_values = "\t".join("retired" for _ in retired)
        checked.analyses_path.write_text(
            f"{lines[0]}\t{retired_header}\n"
            f"{lines[1]}\t{retired_values}\n",
            encoding="utf-8",
        )

        errors = bundle.check(checked, registry_root=self.tmp_dir)
        record_check()
        for column in retired:
            self.assertIn(
                f"Store Release {checked.store_id} analyses.tsv contains retired "
                f"Analysis column {column!r}",
                errors,
            )

        sentinel = "upstream_only_retired"
        with mock.patch.object(
            opengwasdb_analyses,
            "RETIRED_ANALYSIS_COLUMNS",
            (sentinel,),
        ):
            sentinel_bundle = self.make_bundle(
                store_id="OGS-00091",
                analyses=f"{lines[0]}\t{sentinel}\n{lines[1]}\tretired\n",
            )
            sentinel_errors = bundle.check(
                sentinel_bundle,
                registry_root=self.tmp_dir,
            )
        self.assertIn(
            f"Store Release OGS-00091 analyses.tsv contains retired "
            f"Analysis column {sentinel!r}",
            sentinel_errors,
        )

    def test_check_enforces_required_analysis_values_on_standard_releases(self) -> None:
        """Required Analysis columns must carry values, not merely exist in the header."""
        checked = self.make_bundle()
        blank_row = (
            "analysis_id\tstored_effect_scale\tsample_size_kind\t"
            "sample_size_scope\tsample_size\toriginal_effect_scale\t"
            "original_sd_method\tassigned_ancestry\t"
            "ancestry_assignment_method\tchecksum\tchecksum_algorithm\t"
            "source_file\n"
            "FIXTURE_1\t\ttotal\tanalysis_level\t\tsd\t"
            "declared_standardised\tEUR\taf_assigned\t"
            f"{'a' * 64}\tsha256\t/data/source/fixture.tsv\n"
        )
        checked.analyses_path.write_text(blank_row, encoding="utf-8")
        errors = bundle.check(checked, registry_root=self.tmp_dir)
        record_check()
        self.assertIn(
            "analysis 'FIXTURE_1' has no value for required column 'stored_effect_scale'",
            errors,
        )
        self.assertIn(
            "analysis 'FIXTURE_1' has no value for required column 'sample_size'",
            errors,
        )

    def test_new_ragged_besd_bundle_with_blank_required_values_is_rejected(self) -> None:
        """A new ragged-BESD release cannot silently inherit blank required values."""
        blank_analyses = (
            "analysis_id\tstored_effect_scale\tsample_size_kind\t"
            "sample_size_scope\tsample_size\toriginal_effect_scale\t"
            "original_sd_method\tassigned_ancestry\t"
            "ancestry_assignment_method\n"
            "FIXTURE_1\t\t\t\t\t\t\t\t\n"
        )
        new_besd = self.make_bundle(
            store_id="OGS-00095",
            release={
                "source_snapshot": {
                    "besd_prefix": "/data/test/pilot",
                    "source_genome_build": "hg19",
                }
            },
            build={
                "layout": "ragged",
                "completion_state": "observed_only",
                "build": {"command": "build-ragged-besd", "options": {}},
                "post": {
                    "top_hits": False,
                    "rho": False,
                    "overview": False,
                    "validate": True,
                },
            },
            analyses=blank_analyses,
        )
        errors = bundle.check(new_besd, registry_root=self.tmp_dir)
        record_check()
        self.assertIn(
            "analysis 'FIXTURE_1' has no value for required column 'stored_effect_scale'",
            errors,
        )
        self.assertIn(
            "analysis 'FIXTURE_1' has no value for required column 'sample_size'",
            errors,
        )
        self.assertEqual(
            bundle.LEGACY_BLANK_ANALYSIS_RELEASES,
            frozenset({"OGS-00001", "OGS-00002"}),
        )

    def test_legacy_blank_analysis_exemption_is_reported_not_silently_dropped(self) -> None:
        """Issue #142: each exemption's tolerated count must be surfaced, never zero.

        The #134 exemption itself is unchanged (still exactly OGS-00001 and
        OGS-00002, still suppressing only blank required Analysis values, still no
        fabricated value). Both legacy releases also carry eQTLGen's
        ``<probe>::<tissue>`` ids, disclosed as a second tolerated gap (#319). What
        must not happen is a clean ``[]`` that erases either gap, so the gate's
        second channel reports both counts.
        """
        registry = REPO_ROOT / "stores"
        for store_id in ("OGS-00001", "OGS-00002"):
            result = bundle.check(
                bundle.load(store_id, registry_root=registry),
                registry_root=registry,
            )
            record_check()
            # Primary channel: the exemptions still yield a clean pass.
            self.assertEqual(result, [], f"{store_id}: {result}")
            # Second channel: both tolerated gaps are reported, not silently zero.
            self.assertEqual(len(result.tolerated), 2, result.tolerated)
            by_citation = {gap.citation: gap for gap in result.tolerated}
            self.assertEqual(sorted(by_citation), ["#134", "#319"], result.tolerated)
            blank = by_citation["#134"]
            self.assertEqual(blank.store_id, store_id)
            self.assertEqual(blank.count, 70)
            self.assertEqual(blank.detail, "blank required Analysis values")
            namespaced = by_citation["#319"]
            self.assertEqual(namespaced.store_id, store_id)
            self.assertEqual(namespaced.count, 10)
            self.assertEqual(namespaced.detail, "analysis_id values containing ':'")

    def test_a_bundle_store_release_build_will_not_build_discloses_its_blanks(self) -> None:
        """Required values must be present only in a bundle the release workflow builds (#250, #247).

        A candidate, superseded or withdrawn bundle may hold blanks; they are
        reported on the second channel, never silently dropped.
        """
        unresolved = (
            "analysis_id\tstored_effect_scale\tsample_size_kind\t"
            "sample_size_scope\tsample_size\toriginal_effect_scale\t"
            "original_sd_method\tassigned_ancestry\t"
            "ancestry_assignment_method\n"
            "FIXTURE_1\t\t\t\t\t\t\t\t\n"
        )
        for status in sorted(bundle.VALID_STATUSES - bundle.BUILDABLE_STATUSES):
            with self.subTest(status=status):
                # A `superseded` release must name its successor, so the fixture
                # registers one to name (#321).
                release = (
                    {"superseded_by": write_bundle(self.tmp_dir, "OGS-00089").store_id}
                    if status == "superseded"
                    else None
                )
                checked = self.make_bundle(
                    status=status, release=release, analyses=unresolved
                )
                result = bundle.check(checked, registry_root=self.tmp_dir)
                record_check()
                self.assertEqual(result, [])
                self.assertEqual(len(result.tolerated), 1, result.tolerated)
                self.assertEqual(result.tolerated[0].citation, "not buildable")
                self.assertGreater(result.tolerated[0].count, 0)

    def test_assigned_ancestry_must_use_the_super_population_vocabulary(self) -> None:
        header = (
            "analysis_id\tstored_effect_scale\tsample_size_kind\t"
            "sample_size_scope\tsample_size\toriginal_effect_scale\t"
            "original_sd_method\tassigned_ancestry\t"
            "ancestry_assignment_method\tchecksum\tchecksum_algorithm\t"
            "source_file\n"
        )

        def analyses_with(ancestry: str) -> str:
            return header + (
                "FIXTURE_1\tsd\ttotal\tanalysis_level\t1000\tsd\t"
                f"declared_standardised\t{ancestry}\taf_assigned\t"
                f"{'a' * 64}\tsha256\t/data/source/fixture.tsv\n"
            )

        # A free-text Source Ancestry Label is not an Assigned Ancestry. The
        # two vocabularies are related but distinct, so "European" is rejected
        # in exactly the same way an unknown string would be.
        for source_label in ("European", "eur", "Finnish"):
            checked = self.make_bundle(analyses=analyses_with(source_label))
            errors = bundle.check(checked, registry_root=self.tmp_dir)
            record_check()
            self.assertTrue(
                any(
                    f"has assigned_ancestry {source_label!r}" in error
                    for error in errors
                ),
                (source_label, errors),
            )

        # The super-population code, and empty for unassigned, are both valid.
        for valid in ("EUR", "AFR", ""):
            checked = self.make_bundle(analyses=analyses_with(valid))
            errors = bundle.check(checked, registry_root=self.tmp_dir)
            record_check()
            self.assertFalse(
                any("assigned_ancestry" in error for error in errors), errors
            )

    def test_non_case_control_counts_must_be_absent_not_zero(self) -> None:
        header = (
            "analysis_id\tstored_effect_scale\tsample_size_kind\t"
            "sample_size_scope\tsample_size\tn_cases\tn_controls\t"
            "original_effect_scale\toriginal_sd_method\tassigned_ancestry\t"
            "ancestry_assignment_method\tchecksum\tchecksum_algorithm\t"
            "source_file\n"
        )

        def analyses_row(scale: str, kind: str, cases: str, controls: str,
                         sd_method: str) -> str:
            return header + (
                f"FIXTURE_1\t{scale}\t{kind}\tanalysis_level\t1000\t{cases}\t"
                f"{controls}\tsd\t{sd_method}\tEUR\taf_assigned\t"
                f"{'a' * 64}\tsha256\t/data/source/fixture.tsv\n"
            )

        # Zero is a fabricated case count on a non-case-control Analysis.
        zeroed = self.make_bundle(
            analyses=analyses_row("sd", "total", "0", "0", "declared_standardised")
        )
        errors = bundle.check(zeroed, registry_root=self.tmp_dir)
        record_check()
        self.assertTrue(any("n_cases='0'" in error for error in errors), errors)
        self.assertTrue(any("n_controls='0'" in error for error in errors), errors)

        # Blank is the honest value when the counts do not apply...
        blank = self.make_bundle(
            analyses=analyses_row("sd", "total", "", "", "declared_standardised")
        )
        blank_errors = bundle.check(blank, registry_root=self.tmp_dir)
        record_check()
        self.assertFalse(
            any("n_cases" in error or "n_controls" in error for error in blank_errors),
            blank_errors,
        )

        # ...and a real case-control Analysis keeps its counts.
        case_control = self.make_bundle(
            analyses=analyses_row("log_or", "case_control", "10", "20", "binary_trait")
        )
        case_control_errors = bundle.check(case_control, registry_root=self.tmp_dir)
        record_check()
        self.assertFalse(
            any(
                "n_cases" in error or "n_controls" in error
                for error in case_control_errors
            ),
            case_control_errors,
        )

    def test_super_populations_match_the_tracked_ancestry_reference_resource(self) -> None:
        resource = yaml.safe_load(
            (
                REPO_ROOT
                / "resources"
                / "reference-resources"
                / "ukb-ancestry-mixture-hg38"
                / "resource.yaml"
            ).read_text(encoding="utf-8")
        )
        record_check()
        self.assertEqual(
            set(ogstores.bundle.SUPERPOPULATIONS), set(resource["super_populations"])
        )

    def test_source_ancestry_label_map_targets_only_super_populations(self) -> None:
        map_path = (
            REPO_ROOT
            / "resources"
            / "reference-resources"
            / "ukb-ancestry-mixture-hg38"
            / "source_label_map.tsv"
        )
        with map_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle, delimiter="\t"))
        record_check()
        self.assertTrue(rows, "the source-label map is the tracked vocabulary evidence")
        self.assertTrue(
            all(row["super_population"] in ogstores.bundle.SUPERPOPULATIONS for row in rows),
            rows,
        )
        self.assertIn(
            {"source_label": "European", "super_population": "EUR"}, rows
        )

    def test_store_id_must_match_format_directory_and_both_documents(self) -> None:
        checked = self.make_bundle()
        for malformed in ("OGS-1", "ogs-00090", "OGS-00090\n"):
            errors = bundle.check(replace(checked, store_id=malformed), registry_root=self.tmp_dir)
            record_check()
            self.assertTrue(any("malformed store_id" in error for error in errors))

        mismatch = replace(
            checked,
            root=self.tmp_dir / "different-directory",
            release={**checked.release, "store_id": "OGS-00091"},
            build={**checked.build, "store_id": "OGS-00092"},
        )
        errors = bundle.check(mismatch, registry_root=self.tmp_dir)
        record_check()
        self.assertTrue(any("store directory" in error for error in errors))
        self.assertTrue(any("release.yaml store_id" in error for error in errors))
        self.assertTrue(any("build.yaml store_id" in error for error in errors))

    def test_release_status_vocabulary_and_transitions(self) -> None:
        for status in bundle.VALID_STATUSES:
            checked = self.make_bundle(
                store_id=f"OGS-{91 + len(list(self.tmp_dir.iterdir())):05d}",
                status=status,
            )
            errors = bundle.check(checked, registry_root=self.tmp_dir)
            record_check()
            self.assertFalse(any("invalid status" in error for error in errors), errors)

        checked = self.make_bundle(store_id="OGS-00100")
        errors = bundle.check(
            replace(checked, release={**checked.release, "status": "ready"}),
            previous_status="validated",
            registry_root=self.tmp_dir,
        )
        record_check()
        self.assertTrue(any("invalid status" in error for error in errors))
        self.assertTrue(any("invalid target status" in error for error in errors))

        errors = bundle.check(checked, previous_status="validated", registry_root=self.tmp_dir)
        record_check()
        self.assertTrue(any("illegal status transition" in error for error in errors))

    def test_store_release_build_builds_accepted_built_and_validated_bundles_only(self) -> None:
        """Acceptance is a check, not a convention (#247).

        `built` and `validated` stay buildable so that a re-run, or a forced
        replacement, of a release still goes through.
        """
        buildable = {
            "accepted": True,
            "built": True,
            "validated": True,
            "candidate": False,
            "superseded": False,
            "withdrawn": False,
        }
        self.assertEqual(set(buildable), bundle.VALID_STATUSES, "every Release Status needs a verdict")
        for status, expected in buildable.items():
            with self.subTest(status=status):
                made = self.make_bundle(store_id="OGS-00105", status=status)
                record_check()
                self.assertEqual(bundle.is_buildable(made), expected)
                refusal = bundle.build_refusal(made)
                if expected:
                    self.assertIsNone(refusal)
                else:
                    self.assertIn("OGS-00105", refusal)
                    self.assertIn(f"Release Status '{status}'", refusal)

    def test_a_candidate_refusal_says_how_to_accept_it(self) -> None:
        made = self.make_bundle(store_id="OGS-00106", status="candidate")
        refusal = bundle.build_refusal(made)
        record_check()
        self.assertIn("status: accepted", refusal)
        self.assertIn("accepted_at", refusal)
        self.assertIn(str(made.root / "release.yaml"), refusal)

    def test_a_retired_release_is_never_accepted_again(self) -> None:
        """`superseded` and `withdrawn` cannot return to `accepted`, so no acceptance is offered."""
        for status in ("superseded", "withdrawn"):
            with self.subTest(status=status):
                refusal = bundle.build_refusal(self.make_bundle(store_id="OGS-00107", status=status))
                record_check()
                self.assertIn("never returns to 'accepted'", refusal)
                self.assertNotIn("status: accepted", refusal)

    def test_a_bundle_without_a_valid_release_status_is_not_buildable(self) -> None:
        made = self.make_bundle(store_id="OGS-00108")
        for release in ({k: v for k, v in made.release.items() if k != "status"}, {**made.release, "status": "ready"}):
            with self.subTest(status=release.get("status")):
                unknown = replace(made, release=release)
                record_check()
                self.assertFalse(bundle.is_buildable(unknown))
                self.assertIn("no valid Release Status", bundle.build_refusal(unknown))

    def test_partition_and_refuse_unbuildable_name_every_refused_bundle(self) -> None:
        accepted = self.make_bundle(store_id="OGS-00109", status="accepted")
        candidate = self.make_bundle(store_id="OGS-00110", status="candidate")
        withdrawn = self.make_bundle(store_id="OGS-00111", status="withdrawn")

        buildable, refusals = bundle.partition_buildable([accepted, candidate, withdrawn])
        record_check()
        self.assertEqual([b.store_id for b in buildable], ["OGS-00109"])
        self.assertEqual(refusals, [bundle.build_refusal(candidate), bundle.build_refusal(withdrawn)])

        bundle.refuse_unbuildable([accepted])
        with self.assertRaises(bundle.NotBuildableError) as raised:
            bundle.refuse_unbuildable([accepted, candidate, withdrawn])
        record_check()
        message = str(raised.exception)
        self.assertNotIn("OGS-00109", message)
        self.assertIn(bundle.build_refusal(candidate), message)
        self.assertIn(bundle.build_refusal(withdrawn), message)
        self.assertIn("Nothing was written", message)

    def test_validated_release_needs_a_passing_validation_record(self) -> None:
        """`validated` comes only from a passed or passed_with_warnings record (#195)."""
        for record_status, gives_validated in (
            ("passed", True),
            ("passed_with_warnings", True),
            ("failed", False),
            ("not_run", False),
        ):
            with self.subTest(record_status=record_status):
                made = self.make_bundle(store_id="OGS-00103", status="validated")
                (made.root / "validation.yaml").write_text(
                    yaml.safe_dump({"status": record_status}), encoding="utf-8"
                )
                checked = bundle.load(made.store_id, registry_root=self.tmp_dir)
                errors = bundle.check(checked, registry_root=self.tmp_dir)
                record_check()
                if gives_validated:
                    self.assertEqual(errors, [])
                else:
                    self.assertTrue(
                        any("validated" in e and record_status in e for e in errors),
                        f"a {record_status} record must not give validated: {errors}",
                    )

    def test_built_release_may_carry_any_documented_verdict(self) -> None:
        """A failed or not_run record leaves a release at `built`; that stays valid."""
        for record_status in ("passed", "passed_with_warnings", "failed", "not_run"):
            with self.subTest(record_status=record_status):
                made = self.make_bundle(store_id="OGS-00104", status="built")
                (made.root / "validation.yaml").write_text(
                    yaml.safe_dump({"status": record_status}), encoding="utf-8"
                )
                checked = bundle.load(made.store_id, registry_root=self.tmp_dir)
                record_check()
                self.assertEqual(bundle.check(checked, registry_root=self.tmp_dir), [])

    def test_candidate_allows_unresolved_rows_without_validation_record(self) -> None:
        unresolved = (
            "analysis_id\tstored_effect_scale\tsample_size_kind\t"
            "sample_size_scope\tsample_size\toriginal_effect_scale\t"
            "original_sd_method\tassigned_ancestry\t"
            "ancestry_assignment_method\n"
            "FIXTURE_1\t\t\t\t\t\t\t\t\n"
        )
        checked = self.make_bundle(status="candidate", analyses=unresolved)
        errors = bundle.check(checked, registry_root=self.tmp_dir)
        record_check()
        self.assertEqual(errors, [])

    def test_derived_from_resolves_to_registered_release(self) -> None:
        parent = self.make_bundle(store_id="OGS-00101")
        child = self.make_bundle(
            store_id="OGS-00102",
            release={"derived_from": parent.store_id},
            build={
                "layout": "ragged",
                "completion_state": "reference_completed",
                "complete": {"command": "complete-ragged", "options": {}},
                "post": {
                    "top_hits": False,
                    "rho": False,
                    "overview": False,
                    "validate": True,
                },
            },
        )
        errors = bundle.check(child, registry_root=self.tmp_dir)
        record_check()
        self.assertEqual(errors, [])

        cases = (
            (child.store_id, "cannot reference itself"),
            ("bad-parent", "not a valid store_id"),
            ("OGS-00999", "unresolvable derived_from"),
        )
        for parent_id, fragment in cases:
            errors = bundle.check(
                replace(child, release={**child.release, "derived_from": parent_id}),
                registry_root=self.tmp_dir,
            )
            record_check()
            self.assertTrue(any(fragment in error for error in errors), errors)

    def test_a_single_id_completed_release_declares_both_phases(self) -> None:
        """No `derived_from`: the bundle builds its own Store, then completes it (ADR 0037).

        Such a bundle must carry the `build` block its build phase runs and the
        `complete` block its completion phase runs, and a refusal names the
        one that is missing. A two-id child keeps its meaning: with
        `derived_from` it needs only its `complete` block.
        """
        build_block = {"command": "build-dense-vcf", "options": {"source-assembly": "hg38"}}
        complete_block = {"command": "complete-dense", "options": {"ld-panel": "/data/ld", "ancestry": "EUR"}}
        single_id = self.make_bundle(
            store_id="OGS-00103",
            build={
                "completion_state": "reference_completed",
                "build": build_block,
                "complete": complete_block,
            },
        )
        record_check()
        self.assertEqual(bundle.check(single_id, registry_root=self.tmp_dir), [])

        for missing in ("build", "complete"):
            without = {key: value for key, value in single_id.build.items() if key != missing}
            errors = bundle.check(replace(single_id, build=without), registry_root=self.tmp_dir)
            record_check()
            self.assertEqual(len(errors), 1, errors)
            self.assertIn(f"{missing!r}", errors[0])
            self.assertIn("derived_from", errors[0])

        parent = self.make_bundle(store_id="OGS-00104")
        two_id = self.make_bundle(
            store_id="OGS-00105",
            release={"derived_from": parent.store_id},
            build={"completion_state": "reference_completed", "complete": complete_block},
        )
        two_id = replace(two_id, build={key: value for key, value in two_id.build.items() if key != "build"})
        record_check()
        self.assertEqual(bundle.check(two_id, registry_root=self.tmp_dir), [])

    def test_besd_prefix_is_checked_as_metadata_without_being_inspected(self) -> None:
        checked = self.make_bundle(
            build={
                "layout": "ragged",
                "build": {"command": "build-ragged-besd", "options": {}},
                "post": {
                    "top_hits": False,
                    "rho": False,
                    "overview": False,
                    "validate": True,
                },
            },
        )
        errors = bundle.check(checked, registry_root=self.tmp_dir)
        record_check()
        self.assertTrue(any("source_snapshot.besd_prefix" in error for error in errors))

        checked = replace(
            checked,
            release={
                **checked.release,
                "source_snapshot": {
                    "besd_prefix": "/data/opengwasdb/raw/fixture/missing-prefix"
                },
            },
        )
        errors = bundle.check(checked, registry_root=self.tmp_dir)
        record_check()
        self.assertEqual(errors, [])

    def test_declared_bundle_files_checksum_syntax_and_shared_schema(self) -> None:
        checked = self.make_bundle(
            release={
                "source_snapshot": {"manifest_sha256": "not-a-checksum"},
                "generator": {"commands": ["fixture"], "version": "sha256:short"},
            },
            analyses=(
                "analysis_id\tstored_effect_scale\tassigned_ancestry\t"
                "ancestry_assignment_method\toriginal_sd_method\tchecksum\t"
                "checksum_algorithm\n"
                "FIXTURE_1\twrong\tEUR\taf_assigned\tunavailable\tbad\tsha256\n"
            ),
        )
        with mock.patch.object(
            opengwasdb_analyses,
            "validate_analyses",
            wraps=opengwasdb_analyses.validate_analyses,
        ) as delegated:
            errors = bundle.check(checked, registry_root=self.tmp_dir)
        record_check()
        delegated.assert_called_once()
        for fragment in (
            "manifest_sha256",
            "generator version",
            "missing required column(s)",
            "invalid stored_effect_scale",
            "invalid sha256 checksum",
        ):
            self.assertTrue(any(fragment in error for error in errors), (fragment, errors))

    def test_malformed_yaml_is_reported_by_check(self) -> None:
        checked = self.make_bundle()
        (checked.root / "release.yaml").write_text("store_id: [unterminated\n", encoding="utf-8")
        (checked.root / "build.yaml").write_text("- not\n- a\n- mapping\n", encoding="utf-8")
        loaded = bundle.load(checked.store_id, registry_root=self.tmp_dir)
        errors = bundle.check(loaded, registry_root=self.tmp_dir)
        record_check()
        self.assertTrue(any("release.yaml is malformed YAML" in error for error in errors))
        self.assertTrue(any("expected a YAML mapping" in error for error in errors))

    def test_check_never_opens_a_store_or_inspects_an_artifact_path(self) -> None:
        registry = REPO_ROOT / "stores"
        loaded = [
            bundle.load(path.name, registry_root=registry)
            for path in sorted(registry.glob("OGS-*"))
        ]
        original_open = builtins.open
        original_is_file = Path.is_file
        original_is_dir = Path.is_dir

        def reject_artifact(path: object) -> None:
            # The artifact root and a Store path, not every path under /data/opengwasdb:
            # a checkout or scratch directory there holds this repository's own bundles.
            text = str(path)
            location = Path(text)
            root = Path(paths.DEFAULT_ARTIFACT_ROOT)
            if (
                location == root
                or root in location.parents
                or any(".opengwasdb" in part for part in location.parts)
            ):
                raise AssertionError(f"artifact path inspected: {text}")

        def guarded_open(path: object, *args: Any, **kwargs: Any) -> Any:
            reject_artifact(path)
            return original_open(path, *args, **kwargs)

        def guarded_is_file(path: Path) -> bool:
            reject_artifact(path)
            return original_is_file(path)

        def guarded_is_dir(path: Path) -> bool:
            reject_artifact(path)
            return original_is_dir(path)

        artifact_helpers = (
            "store_path",
            "source_dir",
            "work_dir",
            "records_dir",
            "partial_store_path",
            "parent_store_path",
        )
        helper_patches = [
            mock.patch.object(paths, name, wraps=getattr(paths, name))
            for name in artifact_helpers
        ]
        helper_spies = [patch.start() for patch in helper_patches]
        try:
            with mock.patch("builtins.open", side_effect=guarded_open), \
                 mock.patch.object(Path, "is_file", guarded_is_file), \
                 mock.patch.object(Path, "is_dir", guarded_is_dir):
                for checked in loaded:
                    self.assertEqual(
                        bundle.check(checked, registry_root=registry), [], checked.store_id
                    )
            record_check()
            self.assertTrue(all(spy.call_count == 0 for spy in helper_spies))
        finally:
            for patch in helper_patches:
                patch.stop()


class TestAssociationCoverage(unittest.TestCase):
    """`release.yaml` declares what associations its Store holds (#341).

    The four values are `gwas`, `cis`, `cis_and_signals` and `signals_only`;
    the Analysis Catalog reads the declared value and opengwasdb#298 makes it a
    build input (ADR 0040). It is a descriptive fact, so backfilling it is not a
    new Store Release (ADR 0004).
    """

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="ogstores_bundle_coverage_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_the_four_values_pass_the_contract(self) -> None:
        """Each value passes on a layout that can carry it: `gwas` on Dense, the rest on Ragged."""
        for value in ("gwas", "cis", "cis_and_signals", "signals_only"):
            with self.subTest(value=value):
                checked = write_bundle(
                    self.tmp_dir,
                    "OGS-00110",
                    release={"association_coverage": value},
                    build=None if value == "gwas" else RAGGED_BUILD,
                )
                self.assertEqual(checked.association_coverage, value)
                self.assertEqual(bundle.check(checked, registry_root=self.tmp_dir), [])
                record_check()

    def test_a_dense_or_hybrid_release_carries_gwas_only(self) -> None:
        """A cell for every (variant, Analysis) cannot hold anything but `gwas` (opengwasdb#298)."""
        for layout, command in (("dense", "build-dense-vcf"), ("hybrid", "build-hybrid")):
            build = {"layout": layout, "build": {"command": command, "options": {}}}
            for value in ("cis", "cis_and_signals", "signals_only"):
                with self.subTest(layout=layout, value=value):
                    errors = bundle.check(
                        write_bundle(
                            self.tmp_dir,
                            "OGS-00116",
                            release={"association_coverage": value},
                            build=build,
                        ),
                        registry_root=self.tmp_dir,
                    )
                    record_check()
                    self.assertTrue(
                        any("association_coverage" in error and "gwas" in error for error in errors),
                        (layout, value, errors),
                    )
            # The builder's own vocabulary, not a second one: `gwas` passes.
            self.assertEqual(
                bundle.check(
                    write_bundle(
                        self.tmp_dir,
                        "OGS-00117",
                        release={"association_coverage": "gwas"},
                        build=build,
                    ),
                    registry_root=self.tmp_dir,
                ),
                [],
            )
            record_check()

    def test_a_missing_declaration_is_refused(self) -> None:
        checked = write_bundle(self.tmp_dir, "OGS-00111")
        release = dict(checked.release)
        release.pop("association_coverage")
        errors = bundle.check(
            replace(checked, release=release), registry_root=self.tmp_dir
        )
        record_check()
        self.assertTrue(any("association_coverage" in error for error in errors), errors)

    def test_an_unknown_value_is_refused(self) -> None:
        """The retired registry spelling is not a value a reader may see (#341)."""
        for value in ("full_gwas", "full", "cis_plus_signals", "trans", "top_hits"):
            with self.subTest(value=value):
                checked = write_bundle(
                    self.tmp_dir, "OGS-00112", release={"association_coverage": value}
                )
                errors = bundle.check(checked, registry_root=self.tmp_dir)
                record_check()
                self.assertTrue(
                    any("association_coverage" in error for error in errors), (value, errors)
                )

    def test_a_non_string_value_is_refused(self) -> None:
        for value in (True, 1, None, ["gwas"], {"coverage": "gwas"}):
            with self.subTest(value=value):
                checked = write_bundle(
                    self.tmp_dir, "OGS-00113", release={"association_coverage": value}
                )
                errors = bundle.check(checked, registry_root=self.tmp_dir)
                record_check()
                self.assertTrue(
                    any("association_coverage" in error for error in errors), (value, errors)
                )

    def test_a_malformed_declaration_stops_a_reader_instead_of_being_coerced(self) -> None:
        """`check()` is not every reader's gate: `.association_coverage` raises (§ADR 0040).

        The catalog reads the declared value, so reading an unknown or absent one
        as a default would show a layout's coverage as the Store's.
        """
        absent = write_bundle(self.tmp_dir, "OGS-00114")
        release = dict(absent.release)
        release.pop("association_coverage")
        with self.assertRaises(ValueError) as caught:
            replace(absent, release=release).association_coverage
        self.assertIn("association_coverage", str(caught.exception))
        self.assertIn(absent.store_id, str(caught.exception))

        unknown = write_bundle(
            self.tmp_dir, "OGS-00115", release={"association_coverage": "full_gwas"}
        )
        with self.assertRaises(ValueError) as caught:
            unknown.association_coverage
        self.assertIn("full_gwas", str(caught.exception))

    def test_every_committed_bundle_declares_its_true_coverage(self) -> None:
        """The 22 committed bundles, checked against their configs and Collections.

        Every Dense and Hybrid Store is genome-wide. Chen 2023's four
        region-filtered Stores keep significant signals and their flanks only;
        OGS-00007 keeps Sun's cis windows plus its significant signals; and the
        eQTLGen Stores are cis-only, which their BESD sources show.
        """
        expected = {
            "OGS-00001": "cis",
            "OGS-00002": "cis",
            "OGS-00003": "gwas",
            "OGS-00004": "gwas",
            "OGS-00005": "gwas",
            "OGS-00006": "signals_only",
            "OGS-00007": "cis_and_signals",
            "OGS-00008": "gwas",
            "OGS-00009": "gwas",
            "OGS-00011": "gwas",
            "OGS-00012": "gwas",
            "OGS-00013": "gwas",
            "OGS-00014": "gwas",
            "OGS-00015": "gwas",
            "OGS-00016": "gwas",
            "OGS-00017": "gwas",
            "OGS-00022": "gwas",
            "OGS-00023": "gwas",
            "OGS-00024": "signals_only",
            "OGS-00025": "signals_only",
            "OGS-00026": "signals_only",
            "OGS-00027": "cis",
        }
        registry = REPO_ROOT / "stores"
        declared = {
            store_id: bundle.load(store_id, registry_root=registry).association_coverage
            for store_id in sorted(expected)
        }
        self.assertEqual(declared, expected)


class TestTrialAndSupersededBy(unittest.TestCase):
    """`release.yaml` records whether a release is a trial, and what replaced it (#321).

    Both are descriptive facts about a Store Release, not serving decisions
    (ADR 0008): the Analysis Catalog's serving policy reads them, so
    `bundle.check()` holds them to a checked vocabulary rather than free text.
    """

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="ogstores_bundle_markers_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_trial_must_be_a_boolean(self) -> None:
        """A quoted `trial: "yes"` and a numeric `trial: 1` are refused, not read either way."""
        for value in ("yes", "true", 1, 0):
            checked = write_bundle(
                self.tmp_dir, release={"trial": value}
            )
            errors = bundle.check(checked, registry_root=self.tmp_dir)
            record_check()
            self.assertTrue(any("trial" in error for error in errors), (value, errors))

    def test_trial_is_false_when_the_field_is_absent(self) -> None:
        """An unmarked release is not a trial, so no bundle needed a `trial: false` backfill."""
        self.assertFalse(write_bundle(self.tmp_dir, "OGS-00091").trial)
        self.assertFalse(
            write_bundle(self.tmp_dir, "OGS-00092", release={"trial": False}).trial
        )

    def test_a_malformed_trial_stops_a_reader_instead_of_reading_false(self) -> None:
        """`check()` is not every reader's gate: `.trial` refuses a value it cannot read.

        Coercing `trial: "yes"`, `1` or `null` to false is exactly the silent
        substitution CONTRIBUTING forbids: a Trial Store Release would be served.
        """
        for value in ("yes", 1, 0, None):
            checked = write_bundle(
                self.tmp_dir, "OGS-00102", release={"trial": value}
            )
            with self.assertRaises(ValueError, msg=repr(value)) as caught:
                checked.trial
            self.assertIn("trial", str(caught.exception), value)
            self.assertIn(checked.store_id, str(caught.exception), value)

    def test_a_quoted_trial_spelling_is_refused_as_written(self) -> None:
        """The literal text `trial: "yes"` is a string, so it is refused (#321)."""
        checked = write_bundle(self.tmp_dir, "OGS-00103", release={"trial": True})
        path = checked.root / "release.yaml"
        path.write_text(
            path.read_text(encoding="utf-8").replace("trial: true", 'trial: "yes"'),
            encoding="utf-8",
        )
        reloaded = bundle.load("OGS-00103", registry_root=self.tmp_dir)
        errors = bundle.check(reloaded, registry_root=self.tmp_dir)
        record_check()
        self.assertTrue(any("trial" in error for error in errors), errors)

    def test_an_unquoted_yaml_boolean_spelling_is_read_as_true(self) -> None:
        """YAML 1.1's `yes` is a boolean, which is what the documentation says (#321)."""
        checked = write_bundle(self.tmp_dir, "OGS-00104", release={"trial": True})
        path = checked.root / "release.yaml"
        path.write_text(
            path.read_text(encoding="utf-8").replace("trial: true", "trial: yes"),
            encoding="utf-8",
        )
        reloaded = bundle.load("OGS-00104", registry_root=self.tmp_dir)
        self.assertIs(reloaded.trial, True)
        self.assertEqual(bundle.check(reloaded, registry_root=self.tmp_dir), [])
        record_check()

    def test_a_trial_store_release_passes_the_contract(self) -> None:
        trial = write_bundle(self.tmp_dir, "OGS-00093", release={"trial": True})
        self.assertTrue(trial.trial)
        self.assertEqual(bundle.check(trial, registry_root=self.tmp_dir), [])
        record_check()

    def test_a_superseded_release_names_its_successor(self) -> None:
        """A `superseded` release with no successor leaves a reader nowhere to go (#323)."""
        write_bundle(self.tmp_dir, "OGS-00094")
        orphan = write_bundle(self.tmp_dir, "OGS-00095", status="superseded")
        errors = bundle.check(orphan, registry_root=self.tmp_dir)
        record_check()
        self.assertTrue(any("superseded_by" in error for error in errors), errors)

    def test_superseded_by_is_only_for_a_superseded_release(self) -> None:
        """A live release that names a successor contradicts its own Release Status."""
        successor = write_bundle(self.tmp_dir, "OGS-00096")
        live = write_bundle(
            self.tmp_dir, "OGS-00097", release={"superseded_by": successor.store_id}
        )
        errors = bundle.check(live, registry_root=self.tmp_dir)
        record_check()
        self.assertTrue(any("superseded_by" in error for error in errors), errors)

    def test_a_superseded_pair_passes_the_contract(self) -> None:
        successor = write_bundle(self.tmp_dir, "OGS-00100")
        old = write_bundle(
            self.tmp_dir,
            "OGS-00101",
            status="superseded",
            release={"superseded_by": successor.store_id},
        )
        self.assertEqual(old.superseded_by, successor.store_id)
        self.assertEqual(bundle.check(old, registry_root=self.tmp_dir), [])
        record_check()

    def test_superseded_by_must_name_a_registered_release(self) -> None:
        """A successor that is not in the registry is a dangling pointer, not a fact."""
        for value, fragment in (
            ("OGS-00999", "unresolvable superseded_by"),
            ("not-a-store-id", "not a valid store_id"),
        ):
            old = write_bundle(
                self.tmp_dir,
                release={"superseded_by": value},
                status="superseded",
            )
            errors = bundle.check(old, registry_root=self.tmp_dir)
            record_check()
            self.assertTrue(any(fragment in error for error in errors), (value, errors))

    # The Trial Store Releases of the committed registry, as a literal: the
    # nine releases that exercise a Source Collection, Source Format or Store
    # Layout while the generic release pipeline is developed (CONTEXT.md).
    TRIAL_STORE_RELEASES = (
        "OGS-00001",
        "OGS-00002",
        "OGS-00003",
        "OGS-00004",
        "OGS-00005",
        "OGS-00007",
        "OGS-00008",
        "OGS-00022",
        "OGS-00023",
    )

    def test_the_committed_registry_marks_exactly_the_trial_store_releases(self) -> None:
        """Marked in the bundle, not inferred from a "pilot" label (issue #321)."""
        registry = REPO_ROOT / "stores"
        marked, errors = [], []
        for path in sorted(registry.iterdir()):
            if not path.is_dir() or not paths.is_valid_store_id(path.name):
                continue
            loaded = bundle.load(path.name, registry_root=registry)
            if loaded.trial:
                marked.append(path.name)
                result = bundle.check(loaded, registry_root=registry)
                record_check()
                errors.extend(f"{path.name}: {error}" for error in result)
        self.assertEqual(tuple(marked), self.TRIAL_STORE_RELEASES)
        self.assertEqual(errors, [])


class TestAnalysisIdRule(unittest.TestCase):
    """`analysis_id` is one path segment, so `<store_id>:<analysis_id>` splits (#319).

    The Analysis Catalog names an Analysis `<store_id>:<analysis_id>`, and a client splits
    that on `:` to get its two parts -- R `strsplit`, JavaScript `split(":")`,
    Python `str.split(":")`. `analysis_id` is also the filename of the resolver's
    per-Analysis record, and `:` is not legal in a Windows filename. So the value
    must match `^[A-Za-z0-9][A-Za-z0-9._-]*$`. The two legacy eQTLGen releases
    keep their `<probe>::<tissue>` ids, disclosed as a tolerated gap rather than
    edited (ADR 0004).
    """

    setUp = TestBundleContract.setUp
    tearDown = TestBundleContract.tearDown
    make_bundle = TestBundleContract.make_bundle

    def check_id(self, analysis_id: str, *, excluded: bool = False) -> bundle.CheckResult:
        """`check()` on a one-row bundle whose only Analysis is named `analysis_id`."""
        base = self.make_bundle()
        header, row = (base.root / "analyses.tsv").read_text().strip().split("\n")
        values = row.replace("FIXTURE_1", analysis_id)
        if excluded:
            header += "\texclude_from_build"
            values += "\ttrue"
        checked = self.make_bundle(analyses=header + "\n" + values + "\n")
        record_check()
        return bundle.check(checked, registry_root=self.tmp_dir)

    def test_an_analysis_id_outside_the_pattern_is_refused_and_names_its_row(self) -> None:
        for analysis_id in (
            "ENSG00000175445::whole_blood",
            "EFO:0001073",
            "ukb b",
            "ukb/b",
            ".hidden",
            "-leading",
        ):
            with self.subTest(analysis_id=analysis_id):
                errors = self.check_id(analysis_id)
                self.assertEqual(len(errors), 1, errors)
                self.assertIn(f"analysis_id {analysis_id!r}", errors[0])
                self.assertIn("on analyses.tsv row 1", errors[0])

    def test_an_excluded_row_is_checked_too(self) -> None:
        """An excluded Analysis is still named by the Analysis Catalog, so it is still checked."""
        errors = self.check_id("ENSG00000175445::whole_blood", excluded=True)
        self.assertEqual(len(errors), 1, errors)

    def test_ordinary_analysis_ids_pass(self) -> None:
        for analysis_id in ("GCST000998", "ENSG00000175445", "ukb-b-19953", "a.b_c-1"):
            with self.subTest(analysis_id=analysis_id):
                self.assertEqual(self.check_id(analysis_id), [])

    def test_the_two_legacy_releases_tolerate_their_namespaced_ids(self) -> None:
        """Only OGS-00001 and OGS-00002 are exempt, and only as a disclosed gap."""
        self.assertEqual(
            bundle.LEGACY_ANALYSIS_ID_RELEASES, frozenset({"OGS-00001", "OGS-00002"})
        )
        for store_id in ("OGS-00001", "OGS-00002"):
            with self.subTest(store_id=store_id):
                base = self.make_bundle(store_id=store_id)
                header, row = (base.root / "analyses.tsv").read_text().strip().split("\n")
                analyses = (
                    header + "\n"
                    + row.replace("FIXTURE_1", "ENSG00000175445::whole_blood") + "\n"
                )
                checked = self.make_bundle(store_id=store_id, analyses=analyses)
                result = bundle.check(checked, registry_root=self.tmp_dir)
                record_check()
                self.assertEqual(result, [], result)
                self.assertEqual(len(result.tolerated), 1, result.tolerated)
                gap = result.tolerated[0]
                self.assertEqual(gap.store_id, store_id)
                self.assertEqual(gap.count, 1)
                self.assertEqual(gap.citation, "#319")
                self.assertEqual(gap.detail, "analysis_id values containing ':'")


class TestQualityCheckSidecar(unittest.TestCase):
    """`sidecars/quality_checks.tsv` is an optional contract file (#285, ADR 0036).

    Absent is valid. Present, it must name every Analysis of `analyses.tsv` in
    the same order, use the controlled result vocabulary, and agree with
    `analyses.tsv` and `exclusions.tsv` on which Analyses are excluded and why.
    """

    HEADER = (
        "analysis_id\texclude_from_build\texclusion_reason\t"
        "build_eligible_rows\tbuild_eligible_rows_measured\n"
    )
    ROWS = (
        "FIXTURE_1\t\t\tpassed\t1200\n",
        "FIXTURE_2\ttrue\tno_build_eligible_rows\tfailed\t0\n",
    )
    EXCLUSIONS = (
        "analysis_id\tsource_analysis_id\tstudy_design\tcategory\treason\tdetail\t"
        "resolver_status\texclude_from_build\n"
        "FIXTURE_2\tFIXTURE_2\tquantitative\teffect_scale\tno_build_eligible_rows\t"
        "none\tsuccess\ttrue\n"
    )

    setUp = TestBundleContract.setUp
    tearDown = TestBundleContract.tearDown
    make_bundle = TestBundleContract.make_bundle

    def sidecar_bundle(
        self,
        sidecar: str | None,
        *,
        exclusions: str | None = EXCLUSIONS,
    ) -> bundle.Bundle:
        base = self.make_bundle(store_id="OGS-00097")
        header, row = (base.root / "analyses.tsv").read_text().strip().split("\n")
        analyses = (
            header + "\texclude_from_build\n"
            + row + "\t\n"
            + row.replace("FIXTURE_1", "FIXTURE_2") + "\ttrue\n"
        )
        loaded = self.make_bundle(store_id="OGS-00097", analyses=analyses)
        sidecars = loaded.root / "sidecars"
        sidecars.mkdir(exist_ok=True)
        if sidecar is not None:
            (sidecars / "quality_checks.tsv").write_text(sidecar, encoding="utf-8")
        if exclusions is not None:
            (sidecars / "exclusions.tsv").write_text(exclusions, encoding="utf-8")
        return loaded

    def errors(self, sidecar: str | None, **kwargs: Any) -> list[str]:
        loaded = self.sidecar_bundle(sidecar, **kwargs)
        return list(bundle.check(loaded, registry_root=self.tmp_dir))

    def assert_refused(self, sidecar: str, expected: str, **kwargs: Any) -> None:
        errors = self.errors(sidecar, **kwargs)
        self.assertTrue(
            any("quality_checks.tsv" in error and expected in error for error in errors),
            errors,
        )
        record_check()

    def test_a_valid_sidecar_and_an_absent_one_pass(self) -> None:
        self.assertEqual(self.errors(self.HEADER + "".join(self.ROWS)), [])
        self.assertEqual(self.errors(None), [])
        # exclusions.tsv is itself optional; the sidecar then agrees with analyses.tsv alone.
        self.assertEqual(self.errors(self.HEADER + "".join(self.ROWS), exclusions=None), [])
        record_check()

    def test_a_missing_row_fails(self) -> None:
        self.assert_refused(self.HEADER + self.ROWS[0], "FIXTURE_2")

    def test_a_wrong_order_fails(self) -> None:
        self.assert_refused(self.HEADER + self.ROWS[1] + self.ROWS[0], "order")

    def test_an_extra_row_fails(self) -> None:
        extra = self.ROWS[0].replace("FIXTURE_1", "FIXTURE_3")
        self.assert_refused(self.HEADER + "".join(self.ROWS) + extra, "FIXTURE_3")

    def test_a_reason_disagreeing_with_the_exclusions_sidecar_fails(self) -> None:
        wrong = self.ROWS[1].replace("no_build_eligible_rows", "orientation_failure")
        self.assert_refused(self.HEADER + self.ROWS[0] + wrong, "exclusions.tsv")

    def test_membership_disagreeing_with_analyses_fails(self) -> None:
        included_as_excluded = "FIXTURE_1\ttrue\tno_build_eligible_rows\tfailed\t0\n"
        self.assert_refused(
            self.HEADER + included_as_excluded + self.ROWS[1], "exclude_from_build",
            exclusions=None,
        )
        excluded_without_reason = "FIXTURE_2\ttrue\t\tfailed\t0\n"
        self.assert_refused(
            self.HEADER + self.ROWS[0] + excluded_without_reason, "exclusion_reason",
            exclusions=None,
        )

    def test_a_result_outside_the_vocabulary_fails(self) -> None:
        self.assert_refused(
            self.HEADER + self.ROWS[0].replace("passed", "ok") + self.ROWS[1], "'ok'"
        )

    def test_columns_must_lead_with_membership_and_pair_each_check(self) -> None:
        self.assert_refused(
            self.HEADER.replace("exclusion_reason\t", "") + "".join(
                row.replace("\tno_build_eligible_rows", "").replace("\t\tpassed", "\tpassed")
                for row in self.ROWS
            ),
            "columns",
        )
        unpaired = self.HEADER.replace("\tbuild_eligible_rows_measured", "")
        self.assert_refused(
            unpaired + "".join(row.rsplit("\t", 1)[0] + "\n" for row in self.ROWS),
            "columns",
        )


_OMIT: Any = object()


class TestColumnMapContract(unittest.TestCase):
    """A mapped Source Collection's Column Map travels in the bundle (#292, ADR 0036).

    A bundle with an Analysis read through `opengwasdb.column-map` binds its map
    in `build.yaml` (`column_map: {name, sha256, file}`) and carries it at
    `column_maps/<file>`; a built-in is bound without `file`. `check()`
    validates the map shallowly; OpenGWASDB validates it fully at resolution
    and build.
    """

    MAP = (
        "column_map_version = 1\n"
        'name = "fixture-map"\n'
        "\n[columns]\n"
        'chromosome = "chrom"\nposition = "pos"\n'
        'effect_allele = "ea"\nother_allele = "oa"\n'
        "\n[optional_columns]\n"
        'beta = "beta"\nstandard_error = "se"\n'
        "\n[conventions]\n"
        'missing_values = ["", "NA"]\nunmapped_columns = "ignore"\n'
        'alleles = "effect_and_other"\nposition_notation = "integer"\n'
        'chromosome_spelling = "adr-0052"\ngenome_build = "GRCh38"\n'
    )
    FILE = "fixture-map.toml"
    COLUMN_MAP = "opengwasdb.column-map"

    setUp = TestBundleContract.setUp
    tearDown = TestBundleContract.tearDown
    make_bundle = TestBundleContract.make_bundle

    @staticmethod
    def sha256(text: str | bytes) -> str:
        data = text.encode("utf-8") if isinstance(text, str) else text
        return hashlib.sha256(data).hexdigest()

    def binding(self, content: str | bytes = MAP, **overrides: Any) -> dict[str, Any]:
        return {"name": "fixture-map", "sha256": self.sha256(content), "file": self.FILE, **overrides}

    def mapped_bundle(
        self,
        *,
        row_capabilities: tuple[str, ...] | None = (COLUMN_MAP,),
        recipe_capability: str | None = None,
        binding: Any = _OMIT,
        maps: Mapping[str, str | bytes] | None = None,
        options: Mapping[str, Any] | None = None,
        recipe: Mapping[str, Any] | None = None,
    ) -> bundle.Bundle:
        """One Analysis per entry of `row_capabilities`; `None` omits the column."""
        store_id = "OGS-00095"
        base = self.make_bundle(store_id=store_id)
        header, row = (base.root / "analyses.tsv").read_text().strip().split("\n")
        rows = [row]
        if row_capabilities is not None:
            header += "\tsource_reader_capability"
            rows = [
                row.replace("FIXTURE_1", f"FIXTURE_{number}") + f"\t{capability}"
                for number, capability in enumerate(row_capabilities, 1)
            ]
        build_options: dict[str, Any] = dict(options or {})
        if recipe_capability is not None:
            build_options["source-reader-capability"] = recipe_capability
        build: dict[str, Any] = {
            "build": {"command": "build-dense-vcf", "options": build_options},
            **(recipe or {}),
        }
        if binding is _OMIT:
            binding = self.binding()
        if binding is not None:
            build["column_map"] = binding
        loaded = self.make_bundle(
            store_id=store_id, analyses="\n".join([header, *rows]) + "\n", build=build
        )
        if maps is None:
            carries = isinstance(binding, Mapping) and binding.get("file")
            maps = {self.FILE: self.MAP} if carries else {}
        directory = loaded.root / "column_maps"  # one store id serves every call
        if directory.is_symlink() or directory.is_file():
            directory.unlink()
        shutil.rmtree(directory, ignore_errors=True)
        for name, content in maps.items():
            directory.mkdir(exist_ok=True)
            data = content.encode("utf-8") if isinstance(content, str) else content
            (directory / name).write_bytes(data)
        return loaded

    def result(self, **kwargs: Any) -> bundle.CheckResult:
        return bundle.check(self.mapped_bundle(**kwargs), registry_root=self.tmp_dir)

    @staticmethod
    def packaged_sha256(name: str) -> str:
        """The sha256 of the built-in map file the installed opengwasdb ships."""
        packaged = importlib.resources.files("opengwasdb.readers").joinpath("column_maps", f"{name}.toml")
        return hashlib.sha256(packaged.read_bytes()).hexdigest()

    def errors(self, **kwargs: Any) -> list[str]:
        return list(self.result(**kwargs))

    def assert_refused(self, expected: str, **kwargs: Any) -> list[str]:
        errors = self.errors(**kwargs)
        self.assertTrue(any(expected in error for error in errors), errors)
        record_check()
        return errors

    def test_a_mapped_bundle_with_its_map_and_checksum_passes(self) -> None:
        self.assertEqual(self.errors(), [])
        # The binding is OpenGWASDB's fingerprint, `{name, sha256}` over the
        # file's bytes, plus where the bundle carries it.
        loaded = self.mapped_bundle()
        written = (loaded.root / "column_maps" / "fixture-map.toml").read_bytes()
        self.assertEqual(
            loaded.build["column_map"],
            {"name": "fixture-map", "sha256": hashlib.sha256(written).hexdigest(),
             "file": "fixture-map.toml"},
        )
        record_check()

    def test_a_map_name_is_any_name_opengwasdb_accepts(self) -> None:
        """The fingerprint's `name` is the map's own; only `file` takes the bundle's grammar."""
        named = self.MAP.replace('"fixture-map"', '"MVP GIA / phs002453"')
        self.assertEqual(
            self.errors(
                binding=self.binding(named, name="MVP GIA / phs002453"),
                maps={self.FILE: named},
            ),
            [],
        )
        record_check()

    def test_rows_without_a_reader_inherit_the_recipe_capability(self) -> None:
        # No column, a blank cell, and a padded recipe value all inherit it.
        self.assertEqual(self.errors(row_capabilities=None, recipe_capability=self.COLUMN_MAP), [])
        self.assertEqual(
            self.errors(
                row_capabilities=("opengwasdb.gwas-ssf", ""),
                recipe_capability=f" {self.COLUMN_MAP} ",
            ),
            [],
        )
        record_check()
        self.assert_refused(
            "binds no 'column_map'",
            row_capabilities=("",), recipe_capability=f" {self.COLUMN_MAP} ", binding=None,
        )
        # Every row names a dedicated reader, so nothing inherits the recipe's map.
        self.assertEqual(
            self.errors(
                row_capabilities=("opengwasdb.gwas-ssf",),
                recipe_capability=self.COLUMN_MAP, binding=None,
            ),
            [],
        )
        record_check()
        self.assert_refused(
            "no Analysis reads through it",
            row_capabilities=("opengwasdb.gwas-ssf",), recipe_capability=self.COLUMN_MAP,
        )

    def test_a_missing_map_is_refused(self) -> None:
        self.assert_refused("binds no 'column_map'", binding=None)
        self.assert_refused("binds column_maps/fixture-map.toml, but the bundle does not carry it", maps={})

    def test_a_checksum_mismatch_is_refused(self) -> None:
        self.assert_refused(
            "column_maps/fixture-map.toml has sha256",
            maps={self.FILE: self.MAP + "# edited after binding\n"},
        )

    def test_an_unreferenced_map_is_refused(self) -> None:
        self.assert_refused(
            "no Analysis reads through it", row_capabilities=("opengwasdb.gwas-ssf",)
        )
        self.assert_refused(
            "column_maps/other-map.toml is carried but not bound",
            maps={self.FILE: self.MAP, "other-map.toml": self.MAP},
        )
        self.assert_refused(
            "column_maps/fixture-map.toml is carried but not bound",
            row_capabilities=("opengwasdb.gwas-ssf",), binding=None,
            maps={self.FILE: self.MAP},
        )

    def test_dedicated_and_built_in_readers_need_no_file(self) -> None:
        for capability in ("opengwasdb.gwas-ssf", "opengwasdb.finngen-r13"):
            self.assertEqual(self.errors(row_capabilities=(capability,), binding=None), [])
            self.assertEqual(
                self.errors(row_capabilities=None, recipe_capability=capability, binding=None),
                [],
            )
        builtin = {"name": "gwas-ssf", "sha256": self.packaged_sha256("gwas-ssf")}
        self.assertEqual(self.errors(binding=builtin), [])
        record_check()
        self.assert_refused(
            "column_maps/gwas-ssf.toml is carried but not bound",
            binding=builtin, maps={"gwas-ssf.toml": self.MAP},
        )
        self.assert_refused(
            "names no built-in map and carries no file",
            binding={"name": "fixture-map", "sha256": self.sha256(self.MAP)},
        )
        # A carried copy of a built-in is an ordinary carried map.
        copy = self.MAP.replace('"fixture-map"', '"gwas-ssf"')
        self.assertEqual(
            self.errors(binding=self.binding(copy, name="gwas-ssf"), maps={self.FILE: copy}),
            [],
        )
        record_check()

    def test_a_built_in_binding_is_verified_against_the_packaged_map(self) -> None:
        """Its sha256 is that of the map file the pinned opengwasdb ships (#292 part 2)."""
        packaged = self.packaged_sha256("gwas-ssf")
        result = self.result(binding={"name": "gwas-ssf", "sha256": packaged})
        self.assertEqual((list(result), list(result.tolerated)), ([], []))
        record_check()
        errors = self.assert_refused(
            "built-in Column Map 'gwas-ssf'", binding={"name": "gwas-ssf", "sha256": "b" * 64}
        )
        self.assertTrue(any(packaged in error and "b" * 64 in error for error in errors), errors)

    def test_the_map_is_validated_shallowly(self) -> None:
        def bound(content: str | bytes) -> dict[str, Any]:
            return {"binding": self.binding(content), "maps": {self.FILE: content}}

        self.assert_refused("fixture-map.toml is not UTF-8 TOML", **bound("column_map_version = \n"))
        self.assert_refused("fixture-map.toml is not UTF-8 TOML", **bound(b"\xff\xfe"))
        self.assert_refused(
            "fixture-map.toml is missing 'column_map_version'",
            **bound(self.MAP.replace("column_map_version = 1\n", "")),
        )
        self.assert_refused(
            "names itself 'other-map'; build.yaml binds 'fixture-map'",
            **bound(self.MAP.replace("fixture-map", "other-map")),
        )
        # Anything deeper is OpenGWASDB's to refuse: an unknown key passes here.
        self.assertEqual(self.errors(**bound(self.MAP + 'unknown_key = "x"\n')), [])
        record_check()

    def test_a_malformed_binding_is_refused(self) -> None:
        good = self.sha256(self.MAP)
        self.assert_refused("must be a Column Map binding", binding="fixture-map")
        self.assert_refused("sha256 must be", binding=self.binding(sha256=None))
        self.assert_refused("sha256 must be", binding=self.binding(sha256=good.upper()))
        self.assert_refused("sha256 must be", binding=self.binding(sha256=good[:40]))
        self.assert_refused("unknown key(s) ['path']", binding=self.binding(path="x.toml"))
        self.assert_refused("name must be a non-empty string", binding=self.binding(name=" "))
        for bad in ("../fixture-map.toml", "sub/fixture-map.toml", "fixture-map.yaml"):
            self.assert_refused(
                f"file {bad!r} must be", binding=self.binding(file=bad), maps={}
            )
        # Null is malformed, not "no map": omit the key to bind none.
        self.assert_refused("not null", binding=None, recipe={"column_map": None})

    def test_every_defect_is_reported_in_one_pass(self) -> None:
        errors = self.errors(
            binding=self.binding(sha256="A" * 64),
            maps={self.FILE: self.MAP, "stray.toml": self.MAP},
            options={"column-map": "x"},
        )
        for expected in ("sha256 must be", "stray.toml is carried but not bound", "must not set"):
            self.assertTrue(any(expected in error for error in errors), (expected, errors))
        record_check()
        errors = self.errors(maps={self.FILE: self.MAP + "#x\n", "stray.toml": ""})
        for expected in ("has sha256", "stray.toml is carried but not bound"):
            self.assertTrue(any(expected in error for error in errors), (expected, errors))
        record_check()

    def test_the_directory_and_map_must_be_real(self) -> None:
        loaded = self.mapped_bundle(maps={})
        outside = self.tmp_dir / "outside"
        outside.mkdir()
        (outside / self.FILE).write_text(self.MAP, encoding="utf-8")
        (loaded.root / "column_maps").symlink_to(outside, target_is_directory=True)
        errors = list(bundle.check(loaded, registry_root=self.tmp_dir))
        self.assertTrue(any("column_maps/ must be a directory" in e for e in errors), errors)
        record_check()

        loaded = self.mapped_bundle(maps={})
        (loaded.root / "column_maps").write_text("", encoding="utf-8")
        errors = list(bundle.check(loaded, registry_root=self.tmp_dir))
        self.assertTrue(any("column_maps/ must be a directory" in e for e in errors), errors)
        record_check()

        loaded = self.mapped_bundle(maps={})
        (loaded.root / "column_maps").mkdir()
        (loaded.root / "column_maps" / self.FILE).symlink_to(outside / self.FILE)
        errors = list(bundle.check(loaded, registry_root=self.tmp_dir))
        self.assertTrue(
            any("column_maps/fixture-map.toml must be a regular file" in e for e in errors), errors
        )
        record_check()

    def test_options_cannot_pass_a_map_around_the_binding(self) -> None:
        """`--column-map` is composed from the checksummed binding, never passed through."""
        self.assert_refused(
            "build.options must not set ['column-map']",
            options={"column-map": "/elsewhere/map.toml"},
        )
        self.assert_refused("build.options must not set ['--column-map']", options={"--column-map": "gwas-ssf"})
        self.assert_refused(
            "complete.options must not set ['column-map']",
            recipe={
                "completion_state": "reference_completed",
                "complete": {"command": "complete-dense", "options": {"column-map": "x"}},
            },
        )
        for key in ("variant_reference", "variant-reference"):
            self.assert_refused(
                f"build.{key}.options must not set ['column-map']",
                recipe={
                    "build": {
                        "command": "build-dense-vcf",
                        "options": {"variant-reference": "/ref/panel.txt"},
                        key: {"output": "/ref/panel.txt", "options": {"column-map": "x"}},
                    }
                },
            )


class TestBundleSummary(unittest.TestCase):
    """Value-level collapse rules for the issue #131 bundle summary."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="ogstores_summary_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def make_bundle(self, fieldnames: list[str], rows: list[dict[str, str]]) -> bundle.Bundle:
        analyses_path = self.tmp_dir / "analyses.tsv"
        with analyses_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=fieldnames,
                delimiter="\t",
                lineterminator="\n",
            )
            writer.writeheader()
            writer.writerows(rows)
        return bundle.Bundle(
            store_id="OGS-00990",
            root=self.tmp_dir,
            release={},
            build={},
            analyses_path=analyses_path,
        )

    def test_constant_values_are_preserved_verbatim(self) -> None:
        checked = self.make_bundle(
            list(bundle.SUMMARY_COLUMNS),
            [
                {column: "recorded value" for column in bundle.SUMMARY_COLUMNS},
                {column: "recorded value" for column in bundle.SUMMARY_COLUMNS},
            ],
        )
        summary = bundle.summarise(checked)
        record_check()
        self.assertEqual(summary["n_analyses"], 2)
        for column in bundle.SUMMARY_COLUMNS:
            self.assertEqual(summary[column], "recorded value")

    def test_varying_identifiers_are_mixed_counts_never_ranges(self) -> None:
        checked = self.make_bundle(
            ["publication_pmid"],
            [
                {"publication_pmid": "100"},
                {"publication_pmid": "300"},
                {"publication_pmid": "300"},
            ],
        )
        summary = bundle.summarise(checked)
        record_check()
        self.assertEqual(summary["publication_pmid"], "mixed (2)")
        self.assertNotIn("100-300", str(summary["publication_pmid"]))

    def test_varying_quantity_uses_numeric_minimum_and_maximum(self) -> None:
        checked = self.make_bundle(
            ["sample_size"],
            [{"sample_size": "100"}, {"sample_size": "9"}, {"sample_size": "20"}],
        )
        record_check()
        self.assertEqual(bundle.summarise(checked)["sample_size"], "9-100")

    def test_varying_urls_use_common_host_and_component_prefix(self) -> None:
        checked = self.make_bundle(
            ["source_url"],
            [
                {"source_url": "https://example.org/releases/a/one.tsv"},
                {"source_url": "https://example.org/releases/a/two.tsv"},
            ],
        )
        record_check()
        self.assertEqual(
            bundle.summarise(checked)["source_url"],
            "https://example.org/releases/a/",
        )

    def test_urls_without_a_common_authority_are_mixed(self) -> None:
        checked = self.make_bundle(
            ["source_url"],
            [
                {"source_url": "https://one.example/a.tsv"},
                {"source_url": "https://two.example/b.tsv"},
            ],
        )
        record_check()
        self.assertEqual(bundle.summarise(checked)["source_url"], "mixed (2)")

    def test_absent_empty_and_header_only_metadata_are_na(self) -> None:
        absent = self.make_bundle(["analysis_id"], [{"analysis_id": "one"}])
        self.assertEqual(bundle.summarise(absent)["first_author"], "NA")

        sparse = self.make_bundle(
            ["first_author"],
            [{"first_author": "One A"}, {"first_author": ""}],
        )
        self.assertEqual(bundle.summarise(sparse)["first_author"], "NA")

        empty = self.make_bundle(list(bundle.SUMMARY_COLUMNS), [])
        summary = bundle.summarise(empty)
        record_check()
        self.assertEqual(summary["n_analyses"], 0)
        self.assertTrue(all(summary[column] == "NA" for column in bundle.SUMMARY_COLUMNS))

    def test_zero_is_not_treated_as_absence(self) -> None:
        checked = self.make_bundle(["sample_size"], [{"sample_size": "0"}])
        record_check()
        self.assertEqual(bundle.summarise(checked)["sample_size"], "0")

    def test_invalid_quantity_and_url_fail_instead_of_being_coerced(self) -> None:
        bad_quantity = self.make_bundle(
            ["sample_size"],
            [{"sample_size": "10"}, {"sample_size": "unknown"}],
        )
        with self.assertRaisesRegex(ValueError, "non-numeric sample_size"):
            bundle.summarise(bad_quantity)

        bad_url = self.make_bundle(
            ["source_url"],
            [{"source_url": "https://example.org/a"}, {"source_url": "not-a-url"}],
        )
        record_check()
        with self.assertRaisesRegex(ValueError, "invalid source_url URL"):
            bundle.summarise(bad_url)

    def test_pooled_publications_remain_visibly_mixed(self) -> None:
        checked = self.make_bundle(
            ["first_author", "publication_pmid"],
            [
                {"first_author": "One A", "publication_pmid": "111"},
                {"first_author": "Two B", "publication_pmid": "222"},
            ],
        )
        summary = bundle.summarise(checked)
        record_check()
        self.assertEqual(summary["first_author"], "mixed (2)")
        self.assertEqual(summary["publication_pmid"], "mixed (2)")

    def test_registered_bundle_summaries_match_regression_golden(self) -> None:
        expected = yaml.safe_load(
            (REPO_ROOT / "tests" / "bundle" / "golden" / "summaries.yaml").read_text(
                encoding="utf-8"
            )
        )
        actual = {
            path.name: bundle.summarise(
                bundle.load(path.name, registry_root=REPO_ROOT / "stores")
            )
            for path in sorted((REPO_ROOT / "stores").glob("OGS-*"))
            if paths.is_valid_store_id(path.name)
        }
        record_check()
        self.assertEqual(actual, expected)
        self.assertGreaterEqual(
            sum(value == "NA" for value in actual["OGS-00001"].values()),
            5,
            "missing Analytical Metadata must remain visibly sparse",
        )

    def test_summarise_opens_only_the_membership_table(self) -> None:
        checked = self.make_bundle(["analysis_id"], [{"analysis_id": "one"}])
        real_open = builtins.open

        def guarded_open(path: object, *args: Any, **kwargs: Any) -> Any:
            self.assertEqual(Path(path), checked.analyses_path)
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=guarded_open):
            summary = bundle.summarise(checked)
        record_check()
        self.assertEqual(summary["n_analyses"], 1)


class TestArtifactPaths(unittest.TestCase):
    def test_artifact_paths_remain_pure_functions(self) -> None:
        root = Path("/custom/artifacts")
        store_id = "OGS-00042"
        self.assertEqual(paths.store_dir(store_id, root), root / store_id)
        self.assertEqual(paths.source_dir(store_id, root), root / store_id / "source")
        self.assertEqual(paths.work_dir(store_id, root), root / store_id / "work")
        self.assertEqual(
            paths.store_path(store_id, root), root / store_id / "store.opengwasdb"
        )
        record_check()


class TestArtifactRootResolution(unittest.TestCase):
    """`paths.artifact_root()` resolves configuration, not the Release Bundle (issue #126)."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="ogstores_config_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _write_config(self, text: str) -> Path:
        config_p = self.tmp_dir / paths.REPO_CONFIG_FILENAME
        config_p.write_text(text, encoding="utf-8")
        return config_p

    def test_workflow_config_override_wins(self) -> None:
        """A workflow config override beats the environment and the config file."""
        self._write_config("artifact_root: /from/file\n")
        resolved = paths.artifact_root(
            config_override="/from/override",
            env={paths.ARTIFACT_ROOT_ENV_VAR: "/from/env"},
            repo_root=self.tmp_dir,
        )
        record_check()
        self.assertEqual(resolved, Path("/from/override"))

    def test_environment_variable_beats_repo_config_file(self) -> None:
        """The environment variable beats the config file and the default."""
        self._write_config("artifact_root: /from/file\n")
        resolved = paths.artifact_root(
            env={paths.ARTIFACT_ROOT_ENV_VAR: "/from/env"},
            repo_root=self.tmp_dir,
        )
        record_check()
        self.assertEqual(resolved, Path("/from/env"))

    def test_repository_config_file_beats_default(self) -> None:
        """With no override or environment variable, the config file wins."""
        self._write_config("artifact_root: /from/file\n")
        resolved = paths.artifact_root(env={}, repo_root=self.tmp_dir)
        record_check()
        self.assertEqual(resolved, Path("/from/file"))

    def test_built_in_default_when_unconfigured(self) -> None:
        """With no override, environment variable or config file, the default is used."""
        resolved = paths.artifact_root(env={}, repo_root=self.tmp_dir)
        record_check()
        self.assertEqual(resolved, paths.DEFAULT_ARTIFACT_ROOT)

    def test_environment_variable_reads_process_environment(self) -> None:
        """When no mapping is supplied, the process environment is consulted."""
        with mock.patch.dict(os.environ, {paths.ARTIFACT_ROOT_ENV_VAR: "/from/process"}, clear=False):
            resolved = paths.artifact_root(repo_root=self.tmp_dir)
        record_check()
        self.assertEqual(resolved, Path("/from/process"))

    def test_tracked_repo_config_is_the_production_root(self) -> None:
        """The committed ogstores.yaml supplies the default production artifact root."""
        record_check()
        self.assertTrue(paths.repo_config_path(REPO_ROOT).is_file())
        self.assertEqual(paths.artifact_root(env={}), paths.DEFAULT_ARTIFACT_ROOT)

    def test_config_file_without_key_falls_back_to_default(self) -> None:
        """A config file that names no artifact_root does not override the default."""
        self._write_config("unrelated: true\n")
        resolved = paths.artifact_root(env={}, repo_root=self.tmp_dir)
        record_check()
        self.assertEqual(resolved, paths.DEFAULT_ARTIFACT_ROOT)

    def test_malformed_config_file_raises(self) -> None:
        """A malformed config file fails loudly rather than building in the wrong place."""
        self._write_config("- not\n- a\n- mapping\n")
        with self.assertRaises(ValueError):
            paths.artifact_root(env={}, repo_root=self.tmp_dir)
        record_check()
        self._write_config("artifact_root: 42\n")
        with self.assertRaises(ValueError):
            paths.artifact_root(env={}, repo_root=self.tmp_dir)
        record_check()


class TestImportCost(unittest.TestCase):
    """Importing `ogstores` leaves opengwasdb unloaded."""

    def test_the_release_path_does_not_import_opengwasdb(self) -> None:
        """The entry point, each Snakemake parse and the supervisor import `ogstores`; opengwasdb would cost each ~1.5s."""
        code = (
            "import sys; "
            "import ogstores, ogstores.release, ogstores._pdeath_supervisor; "
            "print(sorted(m for m in sys.modules if m.split('.')[0] == 'opengwasdb'))"
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "[]")
        record_check()


def main() -> None:
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
    print(f"ALL {n_checks} CHECKS PASSED")


if __name__ == "__main__":
    main()
