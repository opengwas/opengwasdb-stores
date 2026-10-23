#!/usr/bin/env python3
"""HM3 Indexed Variant Subset registry migration tests (stores #211, epic #214).

Executable proof that the migration is complete and inert:

1. every registered Dense-component Release Bundle (Dense or Hybrid ``layout``)
   declares the default ``hm3`` Indexed Variant Subset, and every Ragged-only
   bundle declares none;
2. the migrated bundles declare the exact maintained HapMap3 GRCh38
   resource (``resource_id``, ``variant_list`` path, assembly and checksum);
3. no unrelated Build Recipe option or ``post`` value changed, and the
   ``indexed_subsets`` key is the only addition;
4. ``plan()`` inserts exactly one ``indexed-subset-hm3`` Step after the
   build/complete Step and before ``overview`` and ``validate``, and Ragged
   plans are untouched;
5. the registry audit prints the full in-scope list and fails loudly on a dense
   or hybrid bundle that is neither migrated nor explicitly opted out, on a
   Ragged bundle that declares a subset, and on a declaration that does not
   match the maintained resource.

Run from repository root:
    pixi run python tests/hm3-migration/test_hm3_migration.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

from ogstores import audit_indexed_subsets as audit
from ogstores import bundle
from ogstores.plan import plan, render_options

REPO_ROOT = Path(__file__).resolve().parents[2]

n_checks = 0


def record_check() -> None:
    global n_checks
    n_checks += 1


# The registered bundles with a Dense Component (stores#211 scope), and the
# Ragged-only bundles that must stay untouched. OGS-00010 was in scope until
# #312 retired it: OGS-00009 completes its own Store (ADR 0037).
EXPECTED_IN_SCOPE = (
    "OGS-00003",
    "OGS-00004",
    "OGS-00005",
    "OGS-00008",
    "OGS-00009",
    "OGS-00011",
    "OGS-00012",
    "OGS-00013",
    "OGS-00014",
    "OGS-00015",
    "OGS-00016",
    "OGS-00017",
)
EXPECTED_RAGGED = (
    "OGS-00001",
    "OGS-00002",
    "OGS-00006",
    "OGS-00007",
    "OGS-00024",
    "OGS-00025",
    "OGS-00026",
)

# Dense-component Release Bundles that explicitly declare no subset. They were
# migrated in #252 from the retired family-first archive with an unchanged
# recipe, and no evidence records an Indexed Variant Subset ever being built
# against them, so `indexed_subsets: []` is the answer rather than a guess.
EXPECTED_OPTED_OUT = ("OGS-00022", "OGS-00023")

HAPMAP3_RESOURCE_ID = "hapmap3-hg38"
HAPMAP3_VARIANT_LIST = "/data/opengwasdb/reference/hapmap3-hg38/hm3.grch38.alid.txt"
HAPMAP3_ASSEMBLY = "GRCh38"
HAPMAP3_CHECKSUM = (
    "4a7c82aa625a006ff437dcaa2769b00e056413d54c72e98f7378327c69513398"
)
HAPMAP3_DECLARATION: dict[str, Any] = {
    "name": "hm3",
    "resource_id": HAPMAP3_RESOURCE_ID,
    "variant_list": HAPMAP3_VARIANT_LIST,
    "reference_assembly": HAPMAP3_ASSEMBLY,
    "checksum": HAPMAP3_CHECKSUM,
}

# The one declaration that is not the plain migration: OGS-00011's Hybrid subset
# also declares how to treat a requested ALID only its shared axis carries
# (stores #225, opengwas/opengwasdb#324). 17,151 of its 1,206,903 HM3 ALIDs are
# such ALIDs, so the subset cannot be built on the default policy.
HAPMAP3_OVERFLOW_ONLY_OPTIONS: dict[str, Any] = {"overflow-only": "skip"}
DECLARATION_OPTIONS: dict[str, dict[str, Any]] = {
    "OGS-00011": HAPMAP3_OVERFLOW_ONLY_OPTIONS,
}

# The pre-#211 Build Recipe values, frozen so a future edit that quietly changes
# a builder option or a post choice fails here as well as in the plan goldens.
EXPECTED_UNRELATED: dict[str, dict[str, Any]] = {
    "OGS-00003": {
        "phase": "build",
        "command": "build-dense-vcf",
        "options": {
            "source-reader-capability": "opengwasdb.finngen-r13",
            "source-assembly": "hg38",
            "n-workers": 10,
        },
        "post": {"top_hits": True, "overview": True},
    },
    "OGS-00004": {
        "phase": "build",
        "command": "build-hybrid",
        "options": {
            "variant-reference": "/data/opengwasdb/reference/alid-panel/EUR-variants.tsv.gz",
            "source-reader-capability": "opengwasdb.gwas-ssf",
            "source-assembly": "hg38",
        },
        "post": {"top_hits": False, "overview": True},
    },
    "OGS-00005": {
        "phase": "build",
        "command": "build-hybrid",
        "options": {
            "variant-reference": "/data/opengwasdb/wip/hybrid-ieua/panel_alids.txt",
            "source-reader-capability": "opengwasdb.gwas-ssf",
            "source-assembly": "hg38",
        },
        "post": {"top_hits": False, "overview": True},
    },
    "OGS-00008": {
        "phase": "build",
        "command": "build-dense-vcf",
        "options": {
            "source-reader-capability": "opengwasdb.gwas-vcf",
            "source-assembly": "hg19",
            "n-workers": 10,
        },
        "post": {"top_hits": True, "overview": True},
    },
    "OGS-00009": {
        "phase": "build",
        "command": "build-dense-vcf",
        "options": {
            "source-reader-capability": "opengwasdb.gwas-vcf",
            "source-assembly": "hg19",
            "n-workers": 32,
        },
        "post": {"top_hits": True, "overview": True},
        # #312 re-emitted OGS-00009 as one single-id release (ADR 0037): the
        # same build phase, then completion under the same id, at 32 workers.
        "complete": {
            "command": "complete-dense",
            "options": {"ld-panel": "/data/opengwasdb/reference/ld", "ancestry": "EUR", "n-workers": 32},
        },
    },
    "OGS-00011": {
        "phase": "build",
        "command": "build-hybrid",
        "options": {
            "variant-reference": "/data/opengwasdb/reference/gnomad-v4-common-hg38/EUR-variants.txt.gz",
            "n-workers": 24,
            "checkpoint": True,
            # #225 adds the rsid-conflict policy as a Release Erratum: the sources
            # name some variants two ways, and the build refused them rather than
            # choose a name (opengwasdb#306).
            "on-rsid-conflict": "drop",
            "source-reader-capability": "opengwasdb.gwas-ssf",
            "source-assembly": "hg38",
        },
        "post": {"top_hits": False, "overview": True},
        # #334 amends OGS-00011's accepted bundle to declare completion
        # (ADR 0037): the same build phase, then completion against the UKB EUR
        # panel at the build's 24 workers.
        "complete": {
            "command": "complete-hybrid",
            "options": {"ld-panel": "/data/opengwasdb/reference/ld", "ancestry": "EUR", "n-workers": 24},
        },
    },
    "OGS-00012": {
        "phase": "build",
        "command": "build-dense-vcf",
        "options": {
            "source-reader-capability": "opengwasdb.gwas-ssf",
            "source-assembly": "GRCh38",
            "n-workers": 16,
        },
        "post": {"top_hits": True, "overview": True},
        # #227 amends OGS-00012's accepted bundle to declare completion
        # (ADR 0037): the same build phase, then completion against the UKB EUR
        # panel at 32 workers.
        "complete": {
            "command": "complete-dense",
            "options": {"ld-panel": "/data/opengwasdb/reference/ld", "ancestry": "EUR", "n-workers": 32},
        },
    },
    "OGS-00013": {
        "phase": "build",
        "command": "build-dense-vcf",
        "options": {
            "source-reader-capability": "opengwasdb.gwas-ssf",
            "source-assembly": "GRCh38",
            "n-workers": 16,
        },
        "post": {"top_hits": True, "overview": True},
    },
    "OGS-00014": {
        "phase": "build",
        "command": "build-dense-vcf",
        "options": {
            "source-reader-capability": "opengwasdb.gwas-ssf",
            "source-assembly": "GRCh38",
            "n-workers": 16,
        },
        "post": {"top_hits": True, "overview": True},
        # #229 amends OGS-00014's accepted bundle to declare completion
        # (ADR 0037): the same build phase, then completion against the
        # HGDP+1kGP AMR panel at 32 workers.
        "complete": {
            "command": "complete-dense",
            "options": {
                "ld-panel": "/data/opengwasdb/reference/hgdp1kgp-hg38-amr/panel",
                "ancestry": "AMR",
                "n-workers": 32,
            },
        },
    },
    "OGS-00015": {
        "phase": "build",
        "command": "build-dense-vcf",
        "options": {
            "source-reader-capability": "opengwasdb.gwas-ssf",
            "source-assembly": "GRCh38",
        },
        "post": {"top_hits": True, "overview": True},
    },
    "OGS-00016": {
        "phase": "build",
        "command": "build-dense-vcf",
        "options": {
            "source-reader-capability": "opengwasdb.gwas-ssf",
            "source-assembly": "GRCh38",
            "n-workers": 16,
        },
        "post": {"top_hits": True, "overview": True},
    },
    "OGS-00017": {
        "phase": "build",
        "command": "build-dense-vcf",
        "options": {
            "source-reader-capability": "opengwasdb.finngen-r13",
            "source-assembly": "hg38",
            "n-workers": 32,
        },
        "post": {"top_hits": True, "overview": True},
        # #223 amends OGS-00017's accepted bundle to declare completion
        # (ADR 0037): the same build phase, then completion against the UKB EUR
        # panel at 32 workers.
        "complete": {
            "command": "complete-dense",
            "options": {"ld-panel": "/data/opengwasdb/reference/ld", "ancestry": "EUR", "n-workers": 32},
        },
    },
}

REAL_RESOURCE_YAML = (
    REPO_ROOT
    / "resources"
    / "reference-resources"
    / HAPMAP3_RESOURCE_ID
    / "resource.yaml"
)


def load_build(store_id: str) -> dict[str, Any]:
    return yaml.safe_load(
        (REPO_ROOT / "stores" / store_id / "build.yaml").read_text(encoding="utf-8")
    )


class TestRegistryMigration(unittest.TestCase):
    """The Dense-component bundles that declare hm3, and every Ragged-only bundle."""

    def test_audit_passes_on_the_real_registry(self) -> None:
        report = audit.audit_registry(REPO_ROOT / "stores", REPO_ROOT)
        record_check()
        self.assertTrue(report.ok, [b.detail for b in report.failures])
        record_check()
        self.assertEqual(
            [b.store_id for b in report.in_scope],
            list(EXPECTED_IN_SCOPE) + list(EXPECTED_OPTED_OUT),
        )
        record_check()
        self.assertEqual([b.store_id for b in report.ragged], list(EXPECTED_RAGGED))

    def test_all_dense_component_bundles_declare_hm3(self) -> None:
        for store_id in EXPECTED_IN_SCOPE:
            report = audit.audit_registry(REPO_ROOT / "stores", REPO_ROOT)
            row = next(b for b in report.bundles if b.store_id == store_id)
            record_check()
            self.assertEqual(row.outcome, "declared", store_id)
            record_check()
            self.assertTrue(row.dense_component, store_id)
            record_check()
            self.assertEqual(row.declared, ("hm3",), store_id)

    def test_opted_out_bundles_declare_an_empty_list(self) -> None:
        for store_id in EXPECTED_OPTED_OUT:
            build = load_build(store_id)
            record_check()
            self.assertEqual(build.get("indexed_subsets"), [], store_id)
            record_check()
            self.assertIn(build.get("layout"), {"dense", "hybrid"}, store_id)

    def test_ragged_only_bundles_declare_none(self) -> None:
        for store_id in EXPECTED_RAGGED:
            build = load_build(store_id)
            record_check()
            self.assertNotIn("indexed_subsets", build, store_id)
            record_check()
            self.assertEqual(build.get("layout"), "ragged", store_id)

    def test_every_declaration_is_the_maintained_hapmap3_resource(self) -> None:
        for store_id in EXPECTED_IN_SCOPE:
            build = load_build(store_id)
            record_check()
            declarations = build.get("indexed_subsets")
            self.assertEqual(len(declarations), 1, store_id)
            declaration = dict(declarations[0])
            options = declaration.pop("options", None)
            record_check()
            self.assertEqual(declaration, HAPMAP3_DECLARATION, store_id)
            record_check()
            self.assertEqual(
                options, DECLARATION_OPTIONS.get(store_id), store_id
            )

    def test_only_indexed_subsets_was_added(self) -> None:
        """The key is the only addition; options, command and post are unchanged.

        Note: OGS-00012/13/14/16 additionally carry the owner-authorized
        execution-only `n-workers: 16` amendment, and OGS-00009 (#312),
        OGS-00011 (#334, plus #225's rsid-conflict policy), OGS-00012 (#227),
        OGS-00014 (#229) and OGS-00017 (#223) their completion phase.
        """
        for store_id, expected in EXPECTED_UNRELATED.items():
            build = load_build(store_id)
            phase = expected["phase"]
            completion = {"complete"} if "complete" in expected else set()
            record_check()
            self.assertEqual(
                set(build),
                {"store_id", "layout", "completion_state", phase, "post", "indexed_subsets"} | completion,
                store_id,
            )
            if completion:
                record_check()
                self.assertEqual(build["complete"], expected["complete"], store_id)
            record_check()
            self.assertEqual(build[phase]["command"], expected["command"], store_id)
            record_check()
            self.assertEqual(build[phase]["options"], expected["options"], store_id)
            record_check()
            self.assertEqual(build["post"], expected["post"], store_id)

    def test_plan_builds_hm3_before_overview_and_validation(self) -> None:
        for store_id in EXPECTED_IN_SCOPE:
            loaded = bundle.load(store_id, registry_root=REPO_ROOT / "stores")
            names = [step.name for step in plan(loaded)]
            record_check()
            self.assertEqual(names.count("indexed-subset-hm3"), 1, store_id)
            subset_at = names.index("indexed-subset-hm3")
            phase = next(
                name for name in ("build", "complete") if name in names
            )
            record_check()
            self.assertLess(names.index(phase), subset_at, store_id)
            for later in ("overview", "validate"):
                if later in names:
                    record_check()
                    self.assertLess(subset_at, names.index(later), store_id)
            subset = next(
                step for step in plan(loaded) if step.name == "indexed-subset-hm3"
            )
            declaration = loaded.build["indexed_subsets"][0]
            record_check()
            self.assertEqual(
                subset.argv,
                [
                    "opengwasdb",
                    "build-indexed-subset",
                    f"/data/opengwasdb/stores/{store_id}/store.opengwasdb",
                    "hm3",
                    "--variant-list",
                    HAPMAP3_VARIANT_LIST,
                    "--reference-assembly",
                    HAPMAP3_ASSEMBLY,
                    *render_options(declaration.get("options")),
                ],
                store_id,
            )

    def test_ragged_plans_have_no_subset_step(self) -> None:
        for store_id in EXPECTED_RAGGED:
            loaded = bundle.load(store_id, registry_root=REPO_ROOT / "stores")
            names = [step.name for step in plan(loaded)]
            record_check()
            self.assertFalse(
                any(name.startswith("indexed-subset") for name in names), store_id
            )

    def test_report_prints_the_full_in_scope_list(self) -> None:
        report = audit.audit_registry(REPO_ROOT / "stores", REPO_ROOT)
        rendered = audit.render_report(report, REPO_ROOT)
        for store_id in (*EXPECTED_IN_SCOPE, *EXPECTED_RAGGED):
            record_check()
            self.assertIn(store_id, rendered)
        record_check()
        self.assertIn("12/14 Dense-component bundles declare hm3; 2 explicitly opted out", rendered)
        record_check()
        self.assertIn("7/7 Ragged-only bundles declare none", rendered)
        record_check()
        self.assertIn("every registered Release Bundle", rendered)


class TestAuditFailures(unittest.TestCase):
    """A synthetic registry proves every failure class is loud and specific."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ogstores_hm3_migration_"))
        resource_dir = (
            self.tmp / "resources" / "reference-resources" / HAPMAP3_RESOURCE_ID
        )
        resource_dir.mkdir(parents=True)
        shutil.copy(REAL_RESOURCE_YAML, resource_dir / "resource.yaml")
        self.stores = self.tmp / "stores"
        self.stores.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_bundle(
        self, store_id: str, build: dict[str, Any], status: str = "candidate"
    ) -> None:
        root = self.stores / store_id
        root.mkdir(parents=True)
        (root / "release.yaml").write_text(
            yaml.safe_dump({"store_id": store_id, "status": status}),
            encoding="utf-8",
        )
        (root / "build.yaml").write_text(
            yaml.safe_dump(build, sort_keys=False), encoding="utf-8"
        )

    def dense(self, store_id: str, **overrides: Any) -> dict[str, Any]:
        build: dict[str, Any] = {
            "store_id": store_id,
            "layout": "dense",
            "completion_state": "observed_only",
            "build": {"command": "build-dense-vcf"},
            "post": {"top_hits": True, "overview": True},
        }
        build.update(overrides)
        return build

    def audit(self) -> audit.AuditReport:
        return audit.audit_registry(self.stores, self.tmp)

    def test_missing_key_on_dense_is_a_loud_miss(self) -> None:
        self.write_bundle("OGS-00090", self.dense("OGS-00090"))
        report = self.audit()
        record_check()
        self.assertFalse(report.ok)
        record_check()
        self.assertEqual(report.failures[0].outcome, "miss")
        record_check()
        self.assertIn("no explicit opt-out", report.failures[0].detail)

    def test_empty_list_is_a_reviewable_opt_out(self) -> None:
        self.write_bundle("OGS-00091", self.dense("OGS-00091", indexed_subsets=[]))
        report = self.audit()
        record_check()
        self.assertTrue(report.ok, [b.detail for b in report.failures])
        record_check()
        self.assertEqual(report.bundles[0].outcome, "opt-out")

    def test_named_but_not_hm3_is_a_miss(self) -> None:
        self.write_bundle(
            "OGS-00092",
            self.dense(
                "OGS-00092",
                indexed_subsets=[dict(HAPMAP3_DECLARATION, name="eur")],
            ),
        )
        report = self.audit()
        record_check()
        self.assertFalse(report.ok)
        record_check()
        self.assertEqual(report.failures[0].outcome, "miss")
        record_check()
        self.assertIn("not 'hm3'", report.failures[0].detail)

    def test_ragged_declaration_is_refused(self) -> None:
        self.write_bundle(
            "OGS-00093",
            {
                "store_id": "OGS-00093",
                "layout": "ragged",
                "completion_state": "observed_only",
                "build": {"command": "build-ragged-ssf"},
                "post": {"top_hits": False, "overview": False},
                "indexed_subsets": [HAPMAP3_DECLARATION],
            },
        )
        report = self.audit()
        record_check()
        self.assertFalse(report.ok)
        record_check()
        self.assertEqual(report.failures[0].outcome, "ragged-declared")

    def test_wrong_variant_list_is_caught_without_reading_the_list(self) -> None:
        self.write_bundle(
            "OGS-00094",
            self.dense(
                "OGS-00094",
                indexed_subsets=[
                    dict(HAPMAP3_DECLARATION, variant_list="/tmp/not-hm3.alid.txt")
                ],
            ),
        )
        report = self.audit()
        record_check()
        self.assertFalse(report.ok)
        record_check()
        self.assertEqual(report.failures[0].outcome, "malformed")
        record_check()
        self.assertIn("differs from", report.failures[0].detail)

    def test_wrong_checksum_is_caught(self) -> None:
        self.write_bundle(
            "OGS-00095",
            self.dense(
                "OGS-00095",
                indexed_subsets=[dict(HAPMAP3_DECLARATION, checksum="a" * 64)],
            ),
        )
        report = self.audit()
        record_check()
        self.assertFalse(report.ok)
        record_check()
        self.assertEqual(report.failures[0].outcome, "malformed")

    def test_unknown_layout_is_caught(self) -> None:
        self.write_bundle(
            "OGS-00096",
            {
                "store_id": "OGS-00096",
                "layout": "triangular",
                "completion_state": "observed_only",
                "build": {"command": "build-dense-vcf"},
                "post": {"top_hits": True, "overview": True},
            },
        )
        report = self.audit()
        record_check()
        self.assertFalse(report.ok)
        record_check()
        self.assertEqual(report.failures[0].outcome, "unknown-layout")

    def test_report_renders_every_failure(self) -> None:
        self.write_bundle("OGS-00097", self.dense("OGS-00097"))
        rendered = audit.render_report(self.audit(), self.tmp)
        record_check()
        self.assertIn("FAILURES:", rendered)
        record_check()
        self.assertIn("OGS-00097", rendered)


def main() -> None:
    suite = unittest.TestSuite()
    suite.addTests(
        unittest.defaultTestLoader.loadTestsFromTestCase(TestRegistryMigration)
    )
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestAuditFailures))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful():
        sys.exit(1)
    print(f"ALL {n_checks} CHECKS PASSED")


if __name__ == "__main__":
    main()
