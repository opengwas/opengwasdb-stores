#!/usr/bin/env python3
"""Resolution reads through the release's Column Map and declared liftover chain (#292).

A release config names its map (`column_map: {builtin: NAME}` or `{path: FILE}`)
and may name a liftover chain Reference Resource
(`ancestry_assignment.liftover_chain_resource_id`). The resolver is passed
`--column-map` and `--chain-file`; the settings digest, the resolution contract
and each Resolution Record's fingerprint carry their checksums; the emitted
bundle binds the map in `build.yaml` and carries a non-built-in map under
`column_maps/`. A changed map or chain makes the records stale.

Expected digests and fingerprints are computed here from the files' bytes, not
by the code under test.

Run from the repository root:
    python3 tests/phase-b-candidate/test_column_map.py
"""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import yaml

from manifest_generation import candidate_workflow
from manifest_generation.candidate_workflow import (
    CandidateError,
    load_candidate_configuration,
    resolver_argv,
    resolver_settings,
    resolver_settings_digest,
)
from manifest_generation.release_config import ReleaseConfigError
from ogstores import bundle

from test_phase_b_candidate import (  # sibling suite's fixture helpers
    ANCESTRY_RESOURCE_ID,
    REPO_ROOT,
    STORE_ID,
    FixtureBuilder,
    _kept_record,
    _receipt_of,
    _run_cli,
)

MAP_CAPABILITY = "opengwasdb.column-map"
CHAIN_ID = "fixture-liftover"


def _builtin_bytes(name: str) -> bytes:
    return importlib.resources.files("opengwasdb.readers").joinpath(f"column_maps/{name}.toml").read_bytes()


def _carried_map_bytes(name: str = "fixture-map", note: str = "") -> bytes:
    """The GWAS-SSF built-in under another name, so OpenGWASDB's loader accepts it."""
    text = _builtin_bytes("gwas-ssf").decode().replace('name = "gwas-ssf"', f'name = "{name}"', 1)
    return (text + note).encode()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ColumnMapTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="column-map-")
        self.fixture = FixtureBuilder(Path(self._tmp.name)).build()
        self.map_path = self.fixture.root / "maps" / "fixture-map.toml"
        self.map_path.parent.mkdir()
        self.map_path.write_bytes(_carried_map_bytes())
        self.chain_path = self.fixture.root / "reference" / "hg19ToHg38.over.chain.gz"
        self.chain_path.write_bytes(b"fixture chain\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- configuration ------------------------------------------------------------

    def _edit_config(self, edit) -> None:
        document = yaml.safe_load(self.fixture.config_path.read_text(encoding="utf-8"))
        edit(document)
        self.fixture.config_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    def _read_through_map(self, **column_map: object) -> None:
        def edit(document: dict) -> None:
            document["build"]["options"]["source-reader-capability"] = MAP_CAPABILITY
            document["column_map"] = column_map

        self._edit_config(edit)

    def _declare_chain(self, *, to_build: str = "GRCh38", sha256: str | None = None) -> Path:
        """A chain resource in the config, and its declaration (the repo's lives under /data)."""
        declaration = self.fixture.root / "chain-declaration" / "resource.yaml"
        declaration.parent.mkdir(exist_ok=True)
        declaration.write_text(
            yaml.safe_dump(
                {
                    "resource_id": CHAIN_ID,
                    "kind": "liftover_chain",
                    "from_genome_build": "GRCh37",
                    "to_genome_build": to_build,
                    "sha256": sha256 or _sha256(self.chain_path.read_bytes()),
                }
            ),
            encoding="utf-8",
        )

        def edit(document: dict) -> None:
            document["reference_resources"].append(
                {"resource_id": CHAIN_ID, "kind": "liftover_chain", "location": str(self.chain_path)}
            )
            document["ancestry_assignment"]["liftover_chain_resource_id"] = CHAIN_ID

        self._edit_config(edit)
        return declaration

    def _config(self, declaration: Path | None = None):
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        if declaration is not None:
            config = replace(config, liftover_chain_declaration=declaration)
        return config

    def _flag(self, settings: list[str], flag: str) -> str | None:
        return settings[settings.index(flag) + 1] if flag in settings else None

    def test_a_release_with_no_map_or_chain_passes_neither_flag(self) -> None:
        settings = resolver_settings(self._config())
        self.assertNotIn("--column-map", settings)
        self.assertNotIn("--chain-file", settings)

    def test_a_built_in_map_is_passed_by_name(self) -> None:
        self._read_through_map(builtin="gwas-ssf")
        self.assertEqual(self._flag(resolver_settings(self._config()), "--column-map"), "gwas-ssf")

    def test_a_carried_map_is_passed_by_its_path(self) -> None:
        self._read_through_map(path=str(self.map_path))
        argv = resolver_argv(
            resolver_bin="opengwasdb", manifest_path=Path("m.tsv"), records_dir=Path("r"),
            config=self._config(), cores=1,
        )
        self.assertEqual(self._flag(argv, "--column-map"), str(self.map_path))

    def test_a_map_is_read_only_through_the_column_map_capability(self) -> None:
        self._edit_config(lambda d: d.update(column_map={"builtin": "gwas-ssf"}))
        with self.assertRaisesRegex(ReleaseConfigError, "column_map.*opengwasdb.column-map"):
            self._config()
        self._edit_config(lambda d: d["build"]["options"].update({"source-reader-capability": MAP_CAPABILITY}))
        self._edit_config(lambda d: d.pop("column_map"))
        with self.assertRaisesRegex(ReleaseConfigError, "needs a .*column_map"):
            self._config()

    def test_a_map_is_a_built_in_or_a_path_not_both(self) -> None:
        self._read_through_map(builtin="gwas-ssf", path=str(self.map_path))
        with self.assertRaisesRegex(ReleaseConfigError, "exactly one of builtin"):
            self._config()

    def test_an_unreadable_map_is_refused_when_resolving(self) -> None:
        self._read_through_map(path=str(self.fixture.root / "absent.toml"))
        with self.assertRaisesRegex(CandidateError, "--column-map .*neither a file nor a built-in"):
            resolver_settings_digest(resolver_settings(self._config()))

    # -- the settings digest ----------------------------------------------------------

    def test_the_digest_covers_a_carried_maps_bytes(self) -> None:
        self._read_through_map(path=str(self.map_path))
        settings = resolver_settings(self._config())
        before = resolver_settings_digest(settings)
        self.assertEqual(before, resolver_settings_digest(settings))
        self.map_path.write_bytes(_carried_map_bytes(note="# changed\n"))
        self.assertNotEqual(before, resolver_settings_digest(settings))

    def test_the_digest_covers_a_built_in_maps_shipped_checksum_not_its_name(self) -> None:
        settings = ["--column-map", "gwas-ssf"]
        with mock.patch.object(candidate_workflow, "_column_map_sha256", return_value="a" * 64) as sha:
            first = resolver_settings_digest(settings)
        sha.assert_called_once_with("gwas-ssf")
        with mock.patch.object(candidate_workflow, "_column_map_sha256", return_value="b" * 64):
            self.assertNotEqual(first, resolver_settings_digest(settings))
        # And unpatched, a name is not refused as "not a file".
        resolver_settings_digest(settings)

    def test_the_digest_covers_the_chains_bytes(self) -> None:
        settings = ["--chain-file", str(self.chain_path)]
        before = resolver_settings_digest(settings)
        self.chain_path.write_bytes(b"another chain\n")
        self.assertNotEqual(before, resolver_settings_digest(settings))

    # -- the declared liftover chain ----------------------------------------------------

    def test_a_declared_chain_is_passed_as_chain_file(self) -> None:
        declaration = self._declare_chain()
        self.assertEqual(
            self._flag(resolver_settings(self._config(declaration)), "--chain-file"), str(self.chain_path)
        )

    def test_a_chain_lifting_to_another_build_than_the_reference_is_refused(self) -> None:
        declaration = self._declare_chain(to_build="GRCh37")
        with self.assertRaisesRegex(CandidateError, "lifts to GRCh37.*Ancestry Reference Panel"):
            resolver_settings(self._config(declaration))

    def test_a_chain_that_is_not_the_declared_file_is_refused(self) -> None:
        declaration = self._declare_chain(sha256="0" * 64)
        with self.assertRaisesRegex(CandidateError, "has sha256 .*declares " + "0" * 64):
            resolver_settings(self._config(declaration))

    def test_the_chain_resource_must_be_a_liftover_chain(self) -> None:
        self._declare_chain()
        self._edit_config(
            lambda d: d["ancestry_assignment"].update(liftover_chain_resource_id=ANCESTRY_RESOURCE_ID)
        )
        with self.assertRaisesRegex(ReleaseConfigError, "not a 'liftover_chain' one"):
            self._config()

    def test_the_committed_chain_declaration_names_the_host_chain_and_its_builds(self) -> None:
        declaration = yaml.safe_load(
            (REPO_ROOT / "resources/reference-resources/liftover-hg19-to-hg38/resource.yaml").read_text()
        )
        self.assertEqual(declaration["kind"], "liftover_chain")
        self.assertEqual(declaration["location"], "/data/opengwasdb/reference/liftover/hg19ToHg38.over.chain.gz")
        self.assertEqual((declaration["from_genome_build"], declaration["to_genome_build"]), ("GRCh37", "GRCh38"))
        self.assertRegex(declaration["sha256"], r"^[0-9a-f]{64}$")

    # -- records and the candidate -------------------------------------------------------

    def _resolved_ids(self) -> list[str]:
        return sorted(_receipt_of(self.fixture)["records"])

    def test_records_carry_the_maps_fingerprint(self) -> None:
        self._read_through_map(path=str(self.map_path))
        self.assertEqual(_run_cli(self.fixture).returncode, 0)
        wanted = {"name": "fixture-map", "sha256": _sha256(self.map_path.read_bytes())}
        for analysis_id in self._resolved_ids():
            record = json.loads(_kept_record(self.fixture, analysis_id).read_text())
            self.assertEqual(record["fingerprints"]["resolution_config"]["column_map"], wanted)

    def test_a_record_made_under_another_map_than_the_binding_is_refused(self) -> None:
        self._read_through_map(path=str(self.map_path))
        self.assertEqual(_run_cli(self.fixture).returncode, 0)
        analysis_id = self._resolved_ids()[0]

        def other_map(record: dict) -> None:
            fingerprints = record["fingerprints"]
            fingerprints["resolution_config"]["column_map"]["sha256"] = "f" * 64
            fingerprints["fingerprint_digest"] = candidate_workflow.recompute_fingerprint_digest(fingerprints)

        from test_phase_b_candidate import _forge_kept_record

        _forge_kept_record(self.fixture, analysis_id, other_map)
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        binding = candidate_workflow.column_map_binding(config)
        record = json.loads(_kept_record(self.fixture, analysis_id).read_text())
        failures = candidate_workflow._verify_column_map([record], binding)
        self.assertEqual(len(failures), 1)
        self.assertIn(f"{analysis_id}: resolver resolution_config.column_map is", failures[0])

    def test_a_changed_map_makes_the_resolution_records_stale(self) -> None:
        self._read_through_map(path=str(self.map_path))
        self.assertEqual(_run_cli(self.fixture).returncode, 0)
        before = self.fixture.registry_root / STORE_ID
        self.map_path.write_bytes(_carried_map_bytes(note="# a changed rule\n"))

        result = _run_cli(self.fixture, "--stage", "emit")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("resolution contract changed", result.stderr)
        self.assertTrue(before.is_dir())  # the earlier candidate is kept

    def test_a_changed_map_is_re_resolved_not_resumed(self) -> None:
        self._read_through_map(path=str(self.map_path))
        first_log, second_log = self.fixture.root / "first.log", self.fixture.root / "second.log"
        self.assertEqual(_run_cli(self.fixture, resolved_log=first_log).returncode, 0)
        resolved = sorted(first_log.read_text().split())
        self.assertEqual(resolved, sorted(set(resolved)))
        self.map_path.write_bytes(_carried_map_bytes(note="# a changed rule\n"))

        second = _run_cli(self.fixture, resolved_log=second_log)

        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(sorted(second_log.read_text().split()), resolved)

    def test_an_unchanged_map_is_resumed(self) -> None:
        self._read_through_map(path=str(self.map_path))
        log = self.fixture.root / "resolved.log"
        self.assertEqual(_run_cli(self.fixture, resolved_log=log).returncode, 0)
        first = set(log.read_text().split())
        log.unlink()
        self.assertEqual(_run_cli(self.fixture, resolved_log=log).returncode, 0)
        # Only a controlled-failure record is re-run under --resume.
        again = set(log.read_text().split()) if log.exists() else set()
        self.assertLess(again, first)

    def test_the_candidate_binds_a_carried_map_and_carries_it(self) -> None:
        self._read_through_map(path=str(self.map_path))
        self.assertEqual(_run_cli(self.fixture).returncode, 0)
        directory = self.fixture.registry_root / STORE_ID
        build = yaml.safe_load((directory / "build.yaml").read_text())
        data = self.map_path.read_bytes()
        self.assertEqual(
            build["column_map"], {"name": "fixture-map", "sha256": _sha256(data), "file": "fixture-map.toml"}
        )
        self.assertEqual((directory / "column_maps" / "fixture-map.toml").read_bytes(), data)
        loaded = bundle.load(STORE_ID, registry_root=self.fixture.registry_root)
        self.assertEqual(list(bundle.check(loaded)), [])

    def test_the_rendered_bundle_in_the_work_root_carries_the_map_too(self) -> None:
        self._read_through_map(path=str(self.map_path))
        self.assertEqual(_run_cli(self.fixture).returncode, 0)
        rendered = self.fixture.work_root / STORE_ID / "bundle"
        self.assertEqual((rendered / "column_maps" / "fixture-map.toml").read_bytes(), self.map_path.read_bytes())

    def test_a_built_in_map_is_bound_by_name_and_shipped_checksum_and_carries_no_file(self) -> None:
        self._read_through_map(builtin="gwas-ssf")
        self.assertEqual(_run_cli(self.fixture).returncode, 0)
        directory = self.fixture.registry_root / STORE_ID
        build = yaml.safe_load((directory / "build.yaml").read_text())
        self.assertEqual(
            build["column_map"], {"name": "gwas-ssf", "sha256": _sha256(_builtin_bytes("gwas-ssf"))}
        )
        self.assertFalse((directory / "column_maps").exists())
        loaded = bundle.load(STORE_ID, registry_root=self.fixture.registry_root)
        self.assertEqual(list(bundle.check(loaded)), [])

    def test_a_release_with_no_map_binds_none(self) -> None:
        self.assertEqual(_run_cli(self.fixture).returncode, 0)
        directory = self.fixture.registry_root / STORE_ID
        self.assertNotIn("column_map", yaml.safe_load((directory / "build.yaml").read_text()))
        self.assertFalse((directory / "column_maps").exists())


class FakeResolverFingerprintTests(unittest.TestCase):
    """The stand-in fingerprints a chain and a map as the pinned resolver does."""

    FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_resolver.py"

    def _resolve_one(self, *flags: str, capability: str = MAP_CAPABILITY) -> dict:
        with tempfile.TemporaryDirectory(prefix="column-map-") as tmp:
            root = Path(tmp)
            (root / "ref.tsv").write_text("x\n")
            (root / "groups.tsv").write_text("y\n")
            (root / "analyses.tsv").write_text(
                "analysis_id\tsource_file\tsource_genome_build\tsource_reader_capability\n"
                f"A1\t{root / 'absent.tsv.gz'}\tGRCh37\t{capability}\n"
            )
            run = subprocess.run(
                [
                    sys.executable, str(self.FAKE), str(root / "analyses.tsv"), str(root / "records"),
                    "--ancestry-reference", str(root / "ref.tsv"), "--ancestry-groups", str(root / "groups.tsv"),
                    "--reference-build", "GRCh38", *flags,
                ],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            return json.loads((root / "records" / "A1.json").read_text())["fingerprints"]

    def test_the_chain_is_fingerprinted_by_path_checksum_size_and_mtime(self) -> None:
        with tempfile.TemporaryDirectory(prefix="column-map-") as tmp:
            chain = Path(tmp) / "x.over.chain.gz"
            chain.write_bytes(b"chain")
            fingerprints = self._resolve_one("--chain-file", str(chain))
            self.assertEqual(
                {k: v for k, v in fingerprints["assembly"].items() if k.startswith("chain_")},
                {
                    "chain_file": str(chain),
                    "chain_sha256": _sha256(b"chain"),
                    "chain_bytes": 5,
                    "chain_mtime_ns": chain.stat().st_mtime_ns,
                },
            )

    def test_no_chain_is_all_none(self) -> None:
        assembly = self._resolve_one()["assembly"]
        self.assertEqual({v for k, v in assembly.items() if k.startswith("chain_")}, {None})

    def test_a_mapped_row_records_the_maps_name_and_checksum(self) -> None:
        got = self._resolve_one("--column-map", "gwas-ssf")["resolution_config"]
        self.assertEqual(got["column_map"], {"name": "gwas-ssf", "sha256": _sha256(_builtin_bytes("gwas-ssf"))})

    def test_a_row_read_another_way_records_no_map(self) -> None:
        got = self._resolve_one("--column-map", "gwas-ssf", capability="opengwasdb.gwas-ssf")
        self.assertNotIn("column_map", got["resolution_config"])


if __name__ == "__main__":
    unittest.main()
