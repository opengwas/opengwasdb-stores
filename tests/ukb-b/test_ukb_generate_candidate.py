#!/usr/bin/env python3
"""Hermetic tests for the restored ukb-b candidate emitter (issues #219, #312).

Fixture-scale and network-free. They pin the contracts the tickets name:
exact membership reconciliation, identity (path/size/sha256) taken from the
frozen inventory rather than the resolution table, preserved meaningful
exclusions, and refusal to emit a schema-invalid or duplicate bundle. How the
bundle lands in the registry is the shared emitter's, tested in tests/emit.

Since #312 the generator emits one bundle per Store id (ADR 0037): a qualified,
active completion reference adds the `complete` block to the Store's own
bundle, and anything short of that leaves today's observed-only bundle byte for
byte. `TestRegisteredOGS00009` holds the real registered bundle to the same.

Run from the repository root:

    python3 tests/ukb-b/test_ukb_generate_candidate.py
    python3 tests/ukb-b/test_ukb_generate_candidate.py --write-goldens   # by hand only
"""

from __future__ import annotations

import csv
import io
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

import yaml

from manifest_generation.providers.ukb_b import generate_candidate as candidate
from manifest_generation.providers.ukb_b import inventory as ukb_inventory
from ogstores import bundle
from ogstores import plan as plan_module

INVENTORY_COLUMNS = ukb_inventory.INVENTORY_COLUMNS

#: The tree before #312. The observed-only bundle is pinned against the bytes
#: this revision emits from the same fixture, so the pinned values come from the
#: base code and never from the code under test.
BASE_REVISION = "a465599"

CREATED_AT = "2026-10-12T00:00:00Z"
FIXTURE_STORE_ID = "OGS-99001"
FIXTURE_IDS = ("ukb-b-1", "ukb-b-2", "ukb-b-3")
FIXTURE_RESOURCE_ID = "fixture-eur-ld"
FIXTURE_LD_PANEL = "/fixture/reference/ld"

#: The ids the two-id UKB-B plan used: OGS-00010 (completed child of the old
#: OGS-00009) and the reserved successors OGS-00030/31. Retired unused by #312;
#: ids are never reused (ADR 0022).
RETIRED_IDS = ("OGS-00010", "OGS-00030", "OGS-00031")

#: What the base code emits for the fixture, committed so the suite needs no
#: git history and no network. See `fixtures/observed-bundle/README.md`.
GOLDEN_ROOT = Path(__file__).resolve().parent / "fixtures" / "observed-bundle"
PINNED_FILES = ("release.yaml", "build.yaml", "analyses.tsv", "validation.yaml")


def write_tsv(path: Path, columns, rows) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(columns), delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def make_source(directory: Path, analysis_id: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{analysis_id}.vcf.gz"
    path.write_bytes(f"content-{analysis_id}".encode())
    return path


def inventory_row(source: Path, analysis_id: str) -> dict[str, str]:
    stat = source.stat()
    return {
        "analysis_id": analysis_id,
        "source_label": f"label {analysis_id}",
        "source_genome_build": "GRCh37",
        "source_file": str(source),
        "data_bytes": str(stat.st_size),
        "sha256": ukb_inventory.sha256_file(source),
        "checksum_algorithm": "sha256",
        "mtime_ns": str(stat.st_mtime_ns),
        "index_file": str(source) + ".tbi",
        "index_status": "ok",
        "readiness_status": "ok",
        "exclusion_reason": "",
    }


def resolution_row(analysis_id: str, *, excluded: bool = False) -> dict[str, str]:
    row = {name: "" for name in candidate.ANALYSES_COLUMNS}
    row.update(
        {
            "analysis_id": analysis_id,
            "source_analysis_id": analysis_id,
            "source_label": f"label {analysis_id}",
            "analysis_label": f"label {analysis_id}",
            "trait_ontology_mapping_method": "unmapped",
            # A recorded resolver path from another host must NOT survive into the bundle.
            "source_file": f"/local-scratch/opengwas/igd/{analysis_id}/{analysis_id}.vcf.gz",
            "original_effect_scale": "sd",
            "original_sd_method": "declared_standardised",
            "stored_effect_scale": "sd",
            "sample_size_kind": "case_control",
            "sample_size_scope": "analysis_level",
            "sample_size": "1000",
            "n_cases": "100",
            "n_controls": "900",
            "inclusion_reason": "declared intent",
        }
    )
    if excluded:
        row["exclude_from_build"] = "true"
        row["inclusion_reason"] = "declared intent; excluded_reason: no gwasinfo record"
    return row


def base_config() -> dict:
    return {
        "label": "ukb-b-test",
        "description": "fixture candidate",
        "access_posture": "public",
        "source_snapshot_id": "fixture",
        "source_collection_id": "opengwas-gwas-vcf",
        "resolution_snapshot_id": "fixture-resolution",
        "build_options": {"source-reader-capability": "opengwasdb.gwas-vcf", "source-assembly": "hg19", "n-workers": 1},
        "post": {"top_hits": True, "overview": True},
        "indexed_subsets": [
            {
                "name": "hm3",
                "resource_id": "hapmap3-hg38",
                "variant_list": "/data/opengwasdb/reference/hapmap3-hg38/hm3.grch38.alid.txt",
                "reference_assembly": "GRCh38",
                "checksum": "4a7c82aa625a006ff437dcaa2769b00e056413d54c72e98f7378327c69513398",
            }
        ],
        "defaults": {
            "license": "UK Biobank terms of access",
            "consortium": "MRC-IEU",
            "first_author": "Ben Elsworth",
            "analysis_group_id": "ukb-b",
            "source_genome_build": "GRCh37",
            "source_ancestry_label": "European",
            "assigned_ancestry": "EUR",
            "ancestry_assignment_method": "source_fallback",
        },
    }


def make_fixture(root: Path) -> Path:
    """Lay out what `main()` reads under `root`, as a checkout does; return the config path.

    Paths in the config are relative to `root`, which each run passes as
    `--repo-root`, so the base code and the code under test read the same
    files. `root` also declares the fixture's LD panel Reference Resource.
    """
    sources = {analysis_id: make_source(root / "source", analysis_id) for analysis_id in FIXTURE_IDS}
    write_tsv(root / "inventory.tsv", INVENTORY_COLUMNS, [inventory_row(sources[i], i) for i in FIXTURE_IDS])
    resolution = [resolution_row(i) for i in FIXTURE_IDS]
    write_tsv(root / "resolution.tsv", list(resolution[0]), resolution)
    declared = [
        {"analysis_id": i, "source_label": f"label {i}", "source_genome_build": "GRCh37", "inclusion_reason": "declared intent"}
        for i in FIXTURE_IDS
    ]
    write_tsv(root / "declared.tsv", list(declared[0]), declared)
    write_ld_resource(root, ancestry_dir="EUR")
    config_p = root / "config.yaml"
    config_p.write_text(
        yaml.safe_dump(
            {**base_config(), "inventory": "inventory.tsv", "resolution": "resolution.tsv", "declared": "declared.tsv"},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return config_p


def write_ld_resource(root: Path, *, ancestry_dir: str) -> None:
    resource = root / "resources" / "reference-resources" / FIXTURE_RESOURCE_ID / "resource.yaml"
    resource.parent.mkdir(parents=True, exist_ok=True)
    resource.write_text(
        yaml.safe_dump(
            {
                "resource_id": FIXTURE_RESOURCE_ID,
                "kind": "ld_panel",
                "ancestry": "EUR",
                "genome_build": "GRCh38",
                "location": FIXTURE_LD_PANEL,
                "location_kind": "external_directory",
                "ancestry_dir": ancestry_dir,
                "status": "available",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def fixture_argv(root: Path) -> list[str]:
    """`main()`'s argv for the fixture; `--config` and `--registry-root` resolve against `--repo-root`.

    The temp directory is the last token, so the YAML line-wrapping of the
    recorded command, decided only by the text before each break, never
    depends on how long the host's temp path is.
    """
    return [
        FIXTURE_STORE_ID,
        "--config", "config.yaml",
        "--registry-root", "stores",
        "--created-at", CREATED_AT,
        "--repo-root", str(root),
    ]


def normalise_golden(text: str, fixture: Path) -> str:
    """Replace what the fixture's temp directory, or the generator's own bytes, decide.

    `{fixture}` is the temp directory. `inventory_sha256` hashes an inventory
    that records that directory and each source's mtime. The generator version
    is the sha256 of the generator module, which any change to it moves.
    """
    text = text.replace(str(fixture), "{fixture}")
    text = re.sub(r"^(\s*inventory_sha256: )[0-9a-f]{64}$", r"\1{fixture-digest}", text, flags=re.MULTILINE)
    return re.sub(r"^(\s*version: )sha256:[0-9a-f]{64}$", r"\1{generator-digest}", text, flags=re.MULTILINE)


def emit_with_the_base_generator(work: Path, argv: list[str]) -> None:
    """Emit the fixture with the generator as it stood before #312.

    Only `--write-goldens` calls this; the suite never reads the base tree.
    `git archive` unpacks `BASE_REVISION`'s `src/`, and running the generator
    with `-m` from there imports that tree's `manifest_generation` and
    `ogstores`, ahead of this checkout's editable install.
    """
    archive = work / "base"
    archive.mkdir()
    try:
        tarball = subprocess.run(
            ["git", "archive", BASE_REVISION, "src", "pixi.toml"], cwd=REPO_ROOT, check=True, capture_output=True
        ).stdout
    except subprocess.CalledProcessError as exc:
        raise AssertionError(
            f"cannot read the base tree {BASE_REVISION}: {exc.stderr.decode().strip()}; "
            "regenerating the goldens needs that commit in the clone"
        ) from exc
    with tarfile.open(fileobj=io.BytesIO(tarball)) as tar:
        tar.extractall(archive, filter="data")
    # The package refuses to import outside a checkout; the generator reads neither.
    (archive / "stores").mkdir()
    result = subprocess.run(
        [sys.executable, "-m", "manifest_generation.providers.ukb_b.generate_candidate", *argv],
        cwd=archive / "src",
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"the base generator failed: {result.stderr}")


def write_goldens() -> list[Path]:
    """Emit the fixture from `BASE_REVISION` and write the observed-bundle goldens.

    Run by hand when the fixture changes:

        pixi run -e dev python3 tests/ukb-b/test_ukb_generate_candidate.py --write-goldens
    """
    with tempfile.TemporaryDirectory() as td:
        fixture = Path(td) / "fixture"
        fixture.mkdir()
        make_fixture(fixture)
        emit_with_the_base_generator(Path(td), fixture_argv(fixture))
        written = []
        for name in PINNED_FILES:
            destination = GOLDEN_ROOT / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            emitted = (fixture / "stores" / FIXTURE_STORE_ID / name).read_text(encoding="utf-8")
            destination.write_text(normalise_golden(emitted, fixture), encoding="utf-8")
            written.append(destination)
        return written


def phase_names(steps) -> dict[str, list[str]]:
    """Every step's name, grouped by the phase it says it belongs to."""
    phases: dict[str, list[str]] = {}
    for step in steps:
        phases.setdefault(step.phase, []).append(step.name)
    return phases


def option(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


class UkbCandidateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source_dir = self.root / "source"
        self.ids = ["ukb-b-1", "ukb-b-2", "ukb-b-3"]
        self.sources = {i: make_source(self.source_dir, i) for i in self.ids}
        self.inventory = [inventory_row(self.sources[i], i) for i in self.ids]
        self.declared = {
            row["analysis_id"]: {"analysis_id": row["analysis_id"], "source_label": row["source_label"], "source_genome_build": "GRCh37", "inclusion_reason": "declared intent"}
            for row in self.inventory
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _build(self, resolution=None, ancestry=None):
        resolution = resolution if resolution is not None else {i: resolution_row(i) for i in self.ids}
        return candidate.build_analyses_rows(
            inventory=self.inventory,
            resolution=resolution,
            declared=self.declared,
            config=base_config(),
            ancestry_records=ancestry,
        )

    def test_exact_membership_and_identity_from_inventory(self) -> None:
        rows, warnings = self._build()
        self.assertEqual([r["analysis_id"] for r in rows], self.ids)
        self.assertEqual(warnings, [])
        candidate.validate_membership(rows, self.inventory)
        for row in rows:
            inv = next(i for i in self.inventory if i["analysis_id"] == row["analysis_id"])
            self.assertEqual(row["source_file"], inv["source_file"])
            self.assertEqual(row["checksum"], inv["sha256"])
            self.assertEqual(row["size_bytes"], inv["data_bytes"])
            self.assertNotIn("/local-scratch/", row["source_file"])
            self.assertEqual(row["assigned_ancestry"], "EUR")
            self.assertEqual(row["ancestry_assignment_method"], "source_fallback")

    def test_missing_resolution_record_is_an_explicit_exclusion(self) -> None:
        resolution = {i: resolution_row(i) for i in self.ids if i != "ukb-b-2"}
        rows, warnings = self._build(resolution=resolution)
        candidate.validate_membership(rows, self.inventory)
        excluded = [r for r in rows if r["exclude_from_build"] == "true"]
        self.assertEqual([r["analysis_id"] for r in excluded], ["ukb-b-2"])
        self.assertIn("no recorded resolution record", excluded[0]["inclusion_reason"])
        self.assertEqual(len(warnings), 1)

    def test_recorded_exclusion_is_preserved(self) -> None:
        resolution = {i: resolution_row(i) for i in self.ids}
        resolution["ukb-b-3"] = resolution_row("ukb-b-3", excluded=True)
        rows, warnings = self._build(resolution=resolution)
        row = next(r for r in rows if r["analysis_id"] == "ukb-b-3")
        self.assertEqual(row["exclude_from_build"], "true")
        self.assertIn("no gwasinfo record", row["inclusion_reason"])
        self.assertEqual(len(warnings), 1)

    def test_resolution_record_outside_inventory_is_refused(self) -> None:
        resolution = {i: resolution_row(i) for i in self.ids}
        resolution["ukb-b-999"] = resolution_row("ukb-b-999")
        with self.assertRaises(candidate.CandidateError):
            self._build(resolution=resolution)

    def test_ancestry_records_override_the_batch_fallback(self) -> None:
        ancestry = {"ukb-b-1": {"analysis_id": "ukb-b-1", "assigned_ancestry": "AFR", "ancestry_assignment_method": "af_assigned"}}
        rows, _ = self._build(ancestry=ancestry)
        row = next(r for r in rows if r["analysis_id"] == "ukb-b-1")
        self.assertEqual(row["assigned_ancestry"], "AFR")
        self.assertEqual(row["ancestry_assignment_method"], "af_assigned")
        other = next(r for r in rows if r["analysis_id"] == "ukb-b-2")
        self.assertEqual(other["ancestry_assignment_method"], "source_fallback")

    def test_validate_membership_rejects_a_leaked_identity(self) -> None:
        rows, _ = self._build()
        rows[0]["source_file"] = "/local-scratch/elsewhere.vcf.gz"
        with self.assertRaises(candidate.CandidateError):
            candidate.validate_membership(rows, self.inventory)

    def test_validate_membership_rejects_a_mismatched_checksum(self) -> None:
        rows, _ = self._build()
        rows[0]["checksum"] = "0" * 64
        with self.assertRaises(candidate.CandidateError):
            candidate.validate_membership(rows, self.inventory)

    def _write_inputs(self) -> Path:
        """The frozen inventory, resolution, declared selection and config, as main() reads them."""
        inventory_p = self.root / "inventory.tsv"
        write_tsv(inventory_p, INVENTORY_COLUMNS, self.inventory)
        resolution = [resolution_row(i) for i in self.ids]
        resolution_p = self.root / "resolution.tsv"
        write_tsv(resolution_p, list(resolution[0]), resolution)
        declared = list(self.declared.values())
        declared_p = self.root / "declared.tsv"
        write_tsv(declared_p, list(declared[0]), declared)
        config_p = self.root / "config.yaml"
        config_p.write_text(
            yaml.safe_dump(
                {**base_config(), "inventory": str(inventory_p), "resolution": str(resolution_p), "declared": str(declared_p)}
            ),
            encoding="utf-8",
        )
        return config_p

    def test_main_emits_the_candidate_and_re_emits_over_it(self) -> None:
        config_p = self._write_inputs()
        registry = self.root / "stores"
        argv = ["OGS-99001", "--config", str(config_p), "--registry-root", str(registry), "--created-at", "2026-10-12T00:00:00Z"]

        self.assertEqual(candidate.main(argv), 0)
        loaded = bundle.load("OGS-99001", registry_root=registry)
        self.assertEqual(list(bundle.check(loaded, registry_root=registry)), [])
        self.assertEqual(loaded.status, "candidate")
        self.assertEqual(
            loaded.release["generator"]["commands"],
            ["python3 -m manifest_generation.providers.ukb_b.generate_candidate " + " ".join(argv)],
        )

        # A candidate is re-emitted in place, not refused (#248).
        self.assertEqual(candidate.main(argv), 0)
        self.assertEqual(bundle.load("OGS-99001", registry_root=registry).status, "candidate")

    def test_shared_schema_skips_excluded_rows(self) -> None:
        resolution = {i: resolution_row(i) for i in self.ids}
        resolution["ukb-b-2"] = resolution_row("ukb-b-2", excluded=True)
        rows, _ = self._build(resolution=resolution)
        # an excluded row may legitimately carry blank required metadata
        excluded = next(r for r in rows if r["analysis_id"] == "ukb-b-2")
        excluded["stored_effect_scale"] = ""
        candidate._validate_shared_schema(rows)

    def _af(self, analysis_id: str) -> dict[str, str]:
        return {"analysis_id": analysis_id, "assigned_ancestry": "EUR", "ancestry_assignment_method": "af_assigned"}

    def test_ancestry_validation_full_af_coverage_passes(self) -> None:
        records = {i: self._af(i) for i in self.ids}
        ok, warnings = candidate.ancestry_validation(self.ids, records)
        self.assertTrue(ok)
        self.assertEqual(warnings, [])

    def test_ancestry_validation_partial_coverage_is_not_run(self) -> None:
        records = {i: self._af(i) for i in self.ids if i != "ukb-b-2"}
        ok, warnings = candidate.ancestry_validation(self.ids, records)
        self.assertFalse(ok)
        self.assertEqual(len(warnings), 1)
        self.assertIn("not fully AF-assigned", warnings[0])
        self.assertIn("1 of 3", warnings[0])

    def test_ancestry_validation_source_fallback_is_not_run(self) -> None:
        records = {i: self._af(i) for i in self.ids}
        records["ukb-b-3"] = {"analysis_id": "ukb-b-3", "assigned_ancestry": "EUR", "ancestry_assignment_method": "source_fallback"}
        ok, warnings = candidate.ancestry_validation(self.ids, records)
        self.assertFalse(ok)
        self.assertIn("non-af_assigned or unassigned", warnings[0])

    def test_ancestry_validation_unassigned_is_not_run(self) -> None:
        records = {i: self._af(i) for i in self.ids}
        records["ukb-b-3"] = {"analysis_id": "ukb-b-3", "assigned_ancestry": "", "ancestry_assignment_method": "unassigned"}
        ok, _warnings = candidate.ancestry_validation(self.ids, records)
        self.assertFalse(ok)

    def test_ancestry_validation_no_records_is_not_run(self) -> None:
        ok, warnings = candidate.ancestry_validation(self.ids, None)
        self.assertFalse(ok)
        self.assertIn("source_fallback", warnings[0])

    def test_ancestry_records_outside_inventory_are_rejected(self) -> None:
        records = {i: self._af(i) for i in self.ids}
        records["ukb-b-999"] = self._af("ukb-b-999")
        with self.assertRaises(candidate.CandidateError):
            self._build(ancestry=records)

    def test_render_validation_only_passes_ancestry_when_checked(self) -> None:
        passed = candidate.render_validation_yaml(
            n_rows=3, n_buildable=3, warnings=[], ancestry_checked=True, generated_at="t"
        )
        self.assertEqual(passed["checks"]["ancestry"], "passed")
        not_run = candidate.render_validation_yaml(
            n_rows=3, n_buildable=3, warnings=["w"], ancestry_checked=False, generated_at="t", reports={"ancestry": "x.tsv"}
        )
        self.assertEqual(not_run["checks"]["ancestry"], "not_run")
        self.assertEqual(not_run["reports"], {"ancestry": "x.tsv"})

    def test_release_yaml_records_ancestry_records_provenance_when_supplied(self) -> None:
        config = base_config()
        identity = {"path": "ancestry.tsv", "sha256": "12" * 32, "rows": 3, "admitted_rows": 3}
        release = candidate.render_release_yaml(
            store_id="OGS-99001",
            config=config,
            inventory_path=Path("inventory.tsv"),
            inventory_sha256="ab" * 32,
            resolution_path=Path("resolution.tsv"),
            resolution_sha256="cd" * 32,
            n_rows=3,
            n_buildable=3,
            generator_version="sha256:" + "ef" * 32,
            created_at="2026-10-12T00:00:00Z",
            ancestry_records_identity=identity,
        )
        self.assertEqual(release["source_snapshot"]["ancestry_records"], identity)
        without = candidate.render_release_yaml(
            store_id="OGS-99001",
            config=config,
            inventory_path=Path("inventory.tsv"),
            inventory_sha256="ab" * 32,
            resolution_path=Path("resolution.tsv"),
            resolution_sha256="cd" * 32,
            n_rows=3,
            n_buildable=3,
            generator_version="sha256:" + "ef" * 32,
            created_at="2026-10-12T00:00:00Z",
        )
        self.assertNotIn("ancestry_records", without["source_snapshot"])


class TestOneBundlePerStore(unittest.TestCase):
    """#312: one Store id, one bundle (ADR 0037), as #308 did for the dbGaP MVP.

    A qualified and active completion reference puts both phases in the
    Store's own bundle; anything short of that emits today's observed-only
    bundle exactly. No completed id is minted either way.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.config_p = make_fixture(self.root)
        self.config = yaml.safe_load(self.config_p.read_text(encoding="utf-8"))
        self.registry = self.root / "stores"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def emit(self) -> Path:
        self.config_p.write_text(yaml.safe_dump(self.config, sort_keys=False), encoding="utf-8")
        self.assertEqual(candidate.main(fixture_argv(self.root)), 0)
        return self.registry / FIXTURE_STORE_ID

    def qualify(self, **changes) -> None:
        self.config["completion_reference"] = {
            "resource_id": FIXTURE_RESOURCE_ID,
            "qualification": "qualified",
            "activation": "active",
            **changes,
        }

    def document(self, name: str) -> dict:
        return yaml.safe_load((self.registry / FIXTURE_STORE_ID / name).read_text(encoding="utf-8"))

    def emitted(self, name: str) -> str:
        return normalise_golden((self.registry / FIXTURE_STORE_ID / name).read_text(encoding="utf-8"), self.root)

    def golden(self, name: str) -> str:
        return (GOLDEN_ROOT / name).read_text(encoding="utf-8")

    def bundle_directories(self) -> list[str]:
        return sorted(path.name for path in self.registry.iterdir() if not path.name.startswith("."))

    def test_a_qualified_active_reference_emits_one_bundle_with_both_phases(self) -> None:
        self.qualify()
        self.emit()

        self.assertEqual(self.bundle_directories(), [FIXTURE_STORE_ID])
        self.assertIsNone(self.document("release.yaml")["derived_from"])
        build = self.document("build.yaml")
        self.assertEqual(build["completion_state"], "reference_completed")
        self.assertEqual(
            build["complete"],
            {"command": "complete-dense", "options": {"ld-panel": FIXTURE_LD_PANEL, "ancestry": "EUR"}},
        )
        # Everything else in the recipe is the observed bundle's, as the base code emitted it.
        observed = yaml.safe_load(self.golden("build.yaml"))
        self.assertEqual(
            {key: value for key, value in build.items() if key not in ("completion_state", "complete")},
            {key: value for key, value in observed.items() if key != "completion_state"},
        )
        self.assertEqual(list(build), ["store_id", "layout", "completion_state", "build", "complete", "post", "indexed_subsets"])
        for name in ("release.yaml", "analyses.tsv", "validation.yaml"):
            self.assertEqual(self.emitted(name), self.golden(name), f"{name} differs from the base code's bytes")
        text = "".join((self.registry / FIXTURE_STORE_ID / name).read_text(encoding="utf-8") for name in PINNED_FILES)
        for retired in RETIRED_IDS:
            self.assertNotIn(retired, text)
        self.assertNotIn("derived_from: OGS", text)

    def test_an_unqualified_reference_emits_todays_observed_bundle_byte_for_byte(self) -> None:
        references = {
            "no reference": None,
            "pending qualification": {"qualification": "pending"},
            "pending activation": {"activation": "pending"},
            "no resource": {"resource_id": None},
        }
        for case, changes in references.items():
            with self.subTest(case):
                self.config.pop("completion_reference", None)
                if changes is not None:
                    self.qualify(**changes)
                self.emit()

                for name in PINNED_FILES:
                    self.assertEqual(self.emitted(name), self.golden(name), f"{name} differs from the base code's bytes")
                self.assertNotIn("complete", self.document("build.yaml"))
                self.assertEqual(self.bundle_directories(), [FIXTURE_STORE_ID])

    def test_a_config_that_still_reserves_a_completed_id_is_refused(self) -> None:
        for key in ("completed_store_id", "planned_completed_store_id"):
            with self.subTest(key):
                self.config.pop("completed_store_id", None)
                self.config.pop("planned_completed_store_id", None)
                self.config[key] = "OGS-00031"
                with self.assertRaisesRegex(candidate.CandidateError, "ADR 0037"):
                    self.emit()
                self.assertFalse(self.registry.exists())

    def test_a_config_naming_another_store_id_is_refused(self) -> None:
        self.config["store_id"] = "OGS-00009"
        with self.assertRaisesRegex(candidate.CandidateError, "OGS-00009"):
            self.emit()
        self.assertFalse(self.registry.exists())

        self.config["store_id"] = FIXTURE_STORE_ID
        self.emit()
        self.assertEqual(self.bundle_directories(), [FIXTURE_STORE_ID])

    def test_a_panel_whose_directory_is_not_its_ancestry_is_refused(self) -> None:
        # complete-dense joins one --ancestry into the panel path and the Analyses'
        # ancestry filter, which is what kept the `ukb` directory unusable (#219).
        write_ld_resource(self.root, ancestry_dir="ukb")
        self.qualify()
        with self.assertRaisesRegex(candidate.CandidateError, "ancestry_dir"):
            self.emit()
        self.assertFalse(self.registry.exists())

    def test_the_single_id_bundle_passes_the_contract_and_plans_both_phases(self) -> None:
        self.qualify()
        self.emit()

        loaded = bundle.load(FIXTURE_STORE_ID, registry_root=self.registry)
        self.assertEqual(list(bundle.check(loaded, registry_root=self.registry)), [])
        self.assertTrue(loaded.completes_own_store)
        steps = plan_module.plan(loaded, artifact_root=self.root / "artifacts")
        self.assertEqual(
            phase_names(steps),
            {
                "build": ["build", "top-hits", "indexed-subset-hm3", "overview", "validate"],
                "completion": [
                    "complete",
                    "completion-indexed-subset-hm3",
                    "completion-overview",
                    "completion-validate",
                    "completion-observed-view-check",
                ],
            },
        )
        complete = next(step for step in steps if step.name == "complete")
        self.assertEqual(complete.argv[:2], ["opengwasdb", "complete-dense"])
        self.assertEqual(option(complete.argv, "--ld-panel"), FIXTURE_LD_PANEL)
        self.assertEqual(option(complete.argv, "--ancestry"), "EUR")


class TestRegisteredOGS00009(unittest.TestCase):
    """The real OGS-00009 (#312): the frozen 2,514 Analyses, completed under its own id."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.config = yaml.safe_load((REPO_ROOT / candidate.DEFAULT_CONFIG).read_text(encoding="utf-8"))
        cls.loaded = bundle.load("OGS-00009", registry_root=REPO_ROOT / "stores")
        resource_id = cls.config["completion_reference"]["resource_id"]
        cls.declaration = yaml.safe_load(
            (REPO_ROOT / "resources" / "reference-resources" / resource_id / "resource.yaml").read_text(encoding="utf-8")
        )

    def test_the_tracked_config_names_ogs00009_and_reserves_no_completed_id(self) -> None:
        self.assertEqual(self.config["store_id"], "OGS-00009")
        for key in ("completed_store_id", "planned_completed_store_id"):
            self.assertNotIn(key, self.config)
        self.assertEqual(self.config["completion_reference"]["resource_id"], "ukb-hg38-eur-ld")
        self.assertEqual(self.config["completion_reference"]["qualification"], "qualified")
        self.assertEqual(self.config["completion_reference"]["activation"], "active")

    def test_ogs00009_is_the_frozen_inventory_under_one_single_id_bundle(self) -> None:
        self.assertEqual(list(bundle.check(self.loaded, registry_root=REPO_ROOT / "stores")), [])
        self.assertEqual(self.loaded.status, "candidate")
        self.assertEqual(self.loaded.completion_state, "reference_completed")
        self.assertIsNone(self.loaded.derived_from)
        self.assertTrue(self.loaded.completes_own_store)
        inventory = ukb_inventory.read_inventory(REPO_ROOT / self.config["inventory"])
        members, excluded = bundle.membership_ids(self.loaded)
        self.assertEqual(len(inventory), 2514)
        self.assertEqual(sorted(members + excluded), sorted(row["analysis_id"] for row in inventory))

    def test_ogs00009_plans_both_phases_against_the_declared_panel(self) -> None:
        steps = plan_module.plan(self.loaded, artifact_root="/fixture/artifacts")
        phases = phase_names(steps)
        self.assertEqual(phases["build"][0], "build")
        self.assertEqual(phases["completion"][0], "complete")
        self.assertEqual(phases["completion"][-1], "completion-observed-view-check")
        complete = next(step for step in steps if step.name == "complete")
        self.assertEqual(complete.argv[:2], ["opengwasdb", "complete-dense"])
        self.assertEqual(option(complete.argv, "--ld-panel"), self.declaration["location"])
        self.assertEqual(self.declaration["location"], "/data/opengwasdb/reference/ld")
        self.assertEqual(option(complete.argv, "--ancestry"), self.declaration["ancestry_dir"])
        self.assertEqual(option(complete.argv, "--ancestry"), "EUR")

    def test_the_retired_ids_have_no_bundle(self) -> None:
        for retired in RETIRED_IDS:
            self.assertFalse((REPO_ROOT / "stores" / retired).exists(), retired)


if __name__ == "__main__":
    if "--write-goldens" in sys.argv[1:]:
        for path in write_goldens():
            print(path.relative_to(REPO_ROOT))
    else:
        unittest.main(verbosity=2)
