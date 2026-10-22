#!/usr/bin/env python3
"""Resolution reads through the release's Column Map and declared liftover chain (#292).

A release config names its map (`column_map: {builtin: NAME}` or `{path: FILE}`)
and may name a liftover chain Reference Resource
(`ancestry_assignment.liftover_chain_resource_id`). The resolver is passed
`--column-map` and `--chain-file`; the settings digest, the resolution contract
and each Resolution Record's fingerprint carry their checksums; the emitted
bundle binds the map in `build.yaml` and carries a non-built-in map under
`column_maps/`. A changed map or chain makes the records stale.

Composing a release's resolver settings reads no liftover chain file, so the
chain is only ever read *before resolution*: its declaration's checksum is what the
settings digest covers, and the `resolve` stage refuses an absent chain or one
which is not those bytes, by name. Both are tested here, the second through the
entry point.

Expected digests and fingerprints are computed here from the files' bytes, not
by the code under test.

Run from the repository root:
    python3 tests/phase-b-candidate/test_column_map.py
"""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import re
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
    RECEIPT_FILENAME,
    CandidateError,
    declared_setting_checksums,
    derive_resolver_manifest,
    read_analysis_metadata,
    verify_liftover_chain,
    verify_records,
    load_candidate_configuration,
    resolver_argv,
    resolver_settings,
    resolver_settings_digest,
)
from manifest_generation.release_config import ReleaseConfigError
from manifest_generation.source_inventory import read_inventory
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
#: The chain Reference Resource this repository declares, with its committed
#: ``resource.yaml`` (``from_genome_build`` GRCh37, ``to_genome_build`` GRCh38).
DECLARED_CHAIN_ID = "liftover-hg19-to-hg38"


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

    def test_the_digest_covers_the_declared_chain_checksum_not_its_bytes(self) -> None:
        declaration = self._declare_chain()
        config = self._config(declaration)
        settings = resolver_settings(config)
        before = resolver_settings_digest(
            settings, declared_checksums=declared_setting_checksums(config)
        )
        # Composing the routing key reads no chain file: it is gone, and the
        # digest is what the declaration said it would be.
        self.chain_path.unlink()
        self.assertEqual(
            before,
            resolver_settings_digest(
                settings, declared_checksums=declared_setting_checksums(config)
            ),
        )
        # A declaration naming another file is another routing key.
        document = yaml.safe_load(declaration.read_text(encoding="utf-8"))
        document["sha256"] = "1" * 64
        declaration.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        self.assertNotEqual(
            before,
            resolver_settings_digest(
                settings, declared_checksums=declared_setting_checksums(config)
            ),
        )

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

    def test_composing_the_settings_reads_no_chain_bytes(self) -> None:
        declaration = self._declare_chain()
        self.chain_path.write_bytes(b"not the declared chain\n")
        self.assertEqual(
            self._flag(resolver_settings(self._config(declaration)), "--chain-file"),
            str(self.chain_path),
        )

    def test_a_chain_that_is_not_the_declared_file_is_refused_before_resolution(self) -> None:
        config = self._config(self._declare_chain(sha256="0" * 64))
        with self.assertRaisesRegex(CandidateError, "has sha256 .*declares " + "0" * 64):
            verify_liftover_chain(config)

    def test_a_chain_file_that_is_absent_is_refused_by_name_before_resolution(self) -> None:
        config = self._config(self._declare_chain())
        self.chain_path.unlink()
        with self.assertRaisesRegex(
            CandidateError,
            re.escape(f"liftover chain {self.chain_path} (Reference Resource {CHAIN_ID!r}) is not a file"),
        ):
            verify_liftover_chain(config)

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


class ChainVerificationTests(unittest.TestCase):
    """The declared chain's bytes are checked before resolution, and refused by name (#292)."""

    setUp = ColumnMapTests.setUp
    tearDown = ColumnMapTests.tearDown
    _edit_config = ColumnMapTests._edit_config

    def _declare_chain_at(self, location: Path) -> None:
        """The repository's own chain declaration, pointed at a location of the test's choosing."""

        def edit(document: dict) -> None:
            document["reference_resources"].append(
                {"resource_id": DECLARED_CHAIN_ID, "kind": "liftover_chain", "location": str(location)}
            )
            document["ancestry_assignment"]["liftover_chain_resource_id"] = DECLARED_CHAIN_ID

        self._edit_config(edit)

    def _declared_sha256(self) -> str:
        declaration = yaml.safe_load(
            (
                REPO_ROOT / "resources" / "reference-resources" / DECLARED_CHAIN_ID / "resource.yaml"
            ).read_text(encoding="utf-8")
        )
        return str(declaration["sha256"])

    def test_a_chain_that_is_not_the_declared_bytes_stops_the_resolve_stage(self) -> None:
        self._declare_chain_at(self.chain_path)  # the fixture's bytes, not the host chain's
        resolved_log = self.fixture.root / "resolved.log"
        run = _run_cli(self.fixture, "--stage", "resolve", resolved_log=resolved_log)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn(f"liftover chain {self.chain_path}", run.stderr)
        self.assertIn(f"declares {self._declared_sha256()}", run.stderr)
        # Refused before the resolver was started.
        self.assertFalse(resolved_log.exists())
        self.assertFalse((self.fixture.work_root / STORE_ID / "resolver" / "resolve.log").exists())

    def test_an_absent_chain_stops_the_resolve_stage_by_name(self) -> None:
        absent = self.fixture.root / "reference" / "absent.over.chain.gz"
        self._declare_chain_at(absent)
        run = _run_cli(self.fixture, "--stage", "resolve")
        self.assertNotEqual(run.returncode, 0)
        self.assertIn(f"liftover chain {absent}", run.stderr)
        self.assertIn("is not a file", run.stderr)
        self.assertFalse((self.fixture.registry_root / STORE_ID).exists())


class MappedCandidateTests(unittest.TestCase):
    """One candidate generated through a carried map, shared by every read-only assertion on it."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="column-map-candidate-")
        cls.fixture = FixtureBuilder(Path(cls._tmp.name)).build()
        cls.map_path = cls.fixture.root / "maps" / "fixture-map.toml"
        cls.map_path.parent.mkdir()
        cls.map_path.write_bytes(_carried_map_bytes())
        document = yaml.safe_load(cls.fixture.config_path.read_text(encoding="utf-8"))
        document["build"]["options"]["source-reader-capability"] = MAP_CAPABILITY
        document["column_map"] = {"path": str(cls.map_path)}
        cls.fixture.config_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        result = _run_cli(cls.fixture)
        assert result.returncode == 0, result.stderr
        cls.directory = cls.fixture.registry_root / STORE_ID

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_records_carry_the_maps_fingerprint(self) -> None:
        wanted = {"name": "fixture-map", "sha256": _sha256(self.map_path.read_bytes())}
        ids = sorted(_receipt_of(self.fixture)["records"])
        self.assertTrue(ids)
        for analysis_id in ids:
            record = json.loads(_kept_record(self.fixture, analysis_id).read_text())
            self.assertEqual(record["fingerprints"]["resolution_config"]["column_map"], wanted)

    def test_the_candidate_binds_the_map_carries_it_and_passes_the_contract_and_the_render_carries_it_too(self) -> None:
        data = self.map_path.read_bytes()
        build = yaml.safe_load((self.directory / "build.yaml").read_text())
        self.assertEqual(
            build["column_map"], {"name": "fixture-map", "sha256": _sha256(data), "file": "fixture-map.toml"}
        )
        self.assertEqual((self.directory / "column_maps" / "fixture-map.toml").read_bytes(), data)
        loaded = bundle.load(STORE_ID, registry_root=self.fixture.registry_root)
        self.assertEqual(list(bundle.check(loaded)), [])
        rendered = self.fixture.work_root / STORE_ID / "bundle"
        self.assertEqual((rendered / "column_maps" / "fixture-map.toml").read_bytes(), data)

    def test_verification_refuses_a_record_made_under_another_map_than_the_binding(self) -> None:
        other = self.fixture.root / "maps" / "other-map.toml"
        other.write_bytes(_carried_map_bytes("other-map"))
        config = load_candidate_configuration(self.fixture.config_path, REPO_ROOT)
        config = replace(config, base=replace(config.base, column_map_path=other))
        rows = read_inventory(self.fixture.inventory_path)
        manifest = derive_resolver_manifest(
            rows, config, read_analysis_metadata(self.fixture.provenance_path, [r.analysis_id for r in rows if r.ready])
        )
        run_root = self.fixture.work_root / STORE_ID / "resolver"

        _, failures = verify_records(
            manifest,
            config=config,
            manifest_path=run_root / "analyses.tsv",
            receipt_path=run_root / RECEIPT_FILENAME,
            index_path=run_root / "index.json",
        )

        mismatches = [f for f in failures if "resolution_config.column_map is" in f]
        self.assertEqual(len(mismatches), len(_receipt_of(self.fixture)["records"]))
        self.assertIn("'sha256': '" + _sha256(self.map_path.read_bytes()) + "'", mismatches[0])
        self.assertIn("'name': 'other-map'", mismatches[0])


class ChangedMapTests(unittest.TestCase):
    """A changed map makes the Resolution Records stale; an unchanged one resumes them."""

    def test_a_changed_map_is_refused_at_emit_and_re_resolved_while_an_unchanged_one_is_resumed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="column-map-changed-") as tmp:
            fixture = FixtureBuilder(Path(tmp)).build()
            map_path = fixture.root / "maps" / "fixture-map.toml"
            map_path.parent.mkdir()
            map_path.write_bytes(_carried_map_bytes())
            document = yaml.safe_load(fixture.config_path.read_text(encoding="utf-8"))
            document["build"]["options"]["source-reader-capability"] = MAP_CAPABILITY
            document["column_map"] = {"path": str(map_path)}
            fixture.config_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
            logs = [fixture.root / f"resolved-{n}.log" for n in range(3)]

            self.assertEqual(_run_cli(fixture, resolved_log=logs[0]).returncode, 0)
            first = set(logs[0].read_text().split())
            self.assertEqual(_run_cli(fixture, resolved_log=logs[1]).returncode, 0)
            # Unchanged: only a controlled-failure record is re-run under --resume.
            again = set(logs[1].read_text().split()) if logs[1].exists() else set()
            self.assertLess(again, first)

            map_path.write_bytes(_carried_map_bytes(note="# a changed rule\n"))
            stale = _run_cli(fixture, "--stage", "emit")
            self.assertNotEqual(stale.returncode, 0)
            self.assertIn("resolution contract changed", stale.stderr)
            self.assertTrue((fixture.registry_root / STORE_ID).is_dir())  # the earlier candidate is kept

            self.assertEqual(_run_cli(fixture, resolved_log=logs[2]).returncode, 0)
            self.assertEqual(set(logs[2].read_text().split()), first)


class BindingTests(unittest.TestCase):
    """A built-in map, or none, in a generated candidate."""

    setUp = ColumnMapTests.setUp
    tearDown = ColumnMapTests.tearDown
    _edit_config = ColumnMapTests._edit_config
    _read_through_map = ColumnMapTests._read_through_map

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
