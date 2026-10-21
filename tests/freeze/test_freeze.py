#!/usr/bin/env python3
"""Freeze: reconcile a Selection of intent against what acquisition produced (#283, ADR 0036).

Freeze reads two connector files -- the Source Metadata Table (intent) and the
Acquisition Manifest (availability) -- and a Selection, and writes the frozen
Source Inventory. It fails loudly on a selected Analysis with no acquisition
entry, an entry no Analysis claims, and a duplicate Analysis or file. The
inventory's provenance records the Selection and both inputs' checksums. It
never lists a directory: an extra file on disk changes nothing.

Run from the repository root:
    python3 tests/freeze/test_freeze.py
"""

from __future__ import annotations

import glob
import io
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import yaml

from manifest_generation.acquisition_manifest import AcquisitionManifestError
from manifest_generation import REPO_ROOT
from manifest_generation.freeze import (
    original_path,
    recorded_path,
    FreezeError,
    ReconciliationError,
    freeze,
    load_frozen_connectors,
    main,
    write_frozen_inventory,
)
from manifest_generation.selection import SelectionError, parse_selection
from manifest_generation.source_inventory import INVENTORY_COLUMNS, read_inventory, sha256_file
from manifest_generation.source_metadata_table import SourceMetadataTableError

FROZEN_AT = "2026-10-16T12:00:00Z"

TABLE_HEADER = (
    "source_analysis_id", "source_trait_label", "study_design", "sample_size",
    "n_cases", "n_controls", "publication_pmid", "store_key",
)
TABLE_ROWS = (
    ("GCST1", "Height", "quantitative", "1000", "", "", "11", "hybrid__European"),
    ("GCST2", "Asthma", "case-control", "3000", "1000", "2000", "22", "hybrid__European"),
    ("GCST3", "BMI", "quantitative", "2000", "", "", "33", "hybrid__European"),
    ("GCST4", "Gout", "case-control", "500", "100", "400", "44", "hybrid__European"),
    # Intended by the collection, but not by this Selection.
    ("GCST9", "Lipids", "quantitative", "900", "", "", "99", "hybrid__East-Asian"),
)

MANIFEST_HEADER = (
    "source_analysis_id", "status", "location", "bytes", "sha256", "source_url",
    "sidecar_location", "sidecar_url", "sidecar_bytes", "error",
    "original_location", "original_sha256", "reformat_tool",
)

SELECT_EUROPEAN = {"where": {"store_key": "hybrid__European"}}


class FreezeFixture(unittest.TestCase):
    """Small connector files over a mirror directory that freeze must never list."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.mirror = self.tmp / "mirror"
        self.mirror.mkdir()
        self.table_rows = [list(row) for row in TABLE_ROWS]
        self.manifest_rows = [
            self.ready("GCST1", "1-GCST1-EFO_1.h.tsv.gz", b"alpha"),
            # The same bytes under a second accession: reported, never collapsed.
            self.ready("GCST2", "GCST2.h.tsv.gz", b"alpha"),
            # A reformatted file, with its lineage.
            dict(
                self.ready("GCST3", "GCST3.tsv.gz", b"gamma"),
                original_location=str(self.mirror / "original" / "all.tsv.gz"),
                original_sha256="c" * 64,
                reformat_tool="split-by-analysis 1.2.0",
            ),
            # Acquisition tried and recorded why there is no file.
            {"source_analysis_id": "GCST4", "status": "data_absent_upstream",
             "source_url": "https://example.org/GCST4.h.tsv.gz", "error": "HTTP 404"},
            # Claimed by an Analysis the table intends but this Selection leaves out.
            self.ready("GCST9", "GCST9.h.tsv.gz", b"delta"),
        ]

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def ready(self, analysis_id: str, name: str, content: bytes) -> dict[str, str]:
        data = self.mirror / name
        data.write_bytes(content)
        sidecar = self.mirror / f"{name}-meta.yaml"
        sidecar.write_text("genome_assembly: GRCh38\n", encoding="utf-8")
        return {
            "source_analysis_id": analysis_id,
            "status": "ok",
            "location": str(data),
            "bytes": str(len(content)),
            "sha256": sha256_file(data),
            "source_url": f"https://example.org/{name}",
            "sidecar_location": str(sidecar),
            "sidecar_url": f"https://example.org/{name}-meta.yaml",
            "sidecar_bytes": str(sidecar.stat().st_size),
        }

    def write_inputs(self) -> tuple[Path, Path]:
        table = self.tmp / "source-metadata.tsv"
        table.write_text(
            "\n".join("\t".join(row) for row in [list(TABLE_HEADER)] + self.table_rows) + "\n",
            encoding="utf-8",
        )
        manifest = self.tmp / "acquisition-manifest.tsv"
        manifest.write_text(
            "\n".join(
                ["\t".join(MANIFEST_HEADER)]
                + ["\t".join(row.get(column, "") for column in MANIFEST_HEADER) for row in self.manifest_rows]
            ) + "\n",
            encoding="utf-8",
        )
        return table, manifest

    def freeze(self, selection=SELECT_EUROPEAN):
        table, manifest = self.write_inputs()
        return freeze(
            snapshot_id="fixture-snapshot",
            source_collection_id="gwas-catalog-ssf",
            metadata_table=table,
            acquisition_manifest=manifest,
            selection=parse_selection(selection),
            frozen_at=FROZEN_AT,
        )

    def write(self, frozen, name: str = "out") -> tuple[Path, Path, str]:
        out = self.tmp / name
        inventory = out / "fixture-snapshot.tsv"
        provenance = out / "fixture-snapshot.meta.yaml"
        digest = write_frozen_inventory(frozen, inventory, provenance)
        return inventory, provenance, digest


class FreezeSuccessTest(FreezeFixture):
    def test_inventory_has_one_row_per_selected_analysis(self) -> None:
        inventory, _, digest = self.write(self.freeze())
        self.assertEqual(digest, sha256_file(inventory))
        header = inventory.read_text(encoding="utf-8").splitlines()[0].split("\t")
        self.assertEqual(tuple(header), INVENTORY_COLUMNS)
        rows = {row.analysis_id: row for row in read_inventory(inventory)}
        self.assertEqual(sorted(rows), ["GCST1", "GCST2", "GCST3", "GCST4"])

    def test_intent_comes_from_the_table_and_files_from_the_manifest(self) -> None:
        inventory, _, _ = self.write(self.freeze())
        row = {row.analysis_id: row for row in read_inventory(inventory)}["GCST2"]
        self.assertEqual(
            (row.publication_pmid, row.trait, row.study_design, row.sample_size),
            ("22", "Asthma", "case-control", "3000"),
        )
        entry = self.manifest_rows[1]
        self.assertEqual(row.readiness_status, "ok")
        self.assertEqual(row.data_file, entry["location"])
        self.assertEqual(row.data_url, entry["source_url"])
        self.assertEqual(row.data_bytes, entry["bytes"])
        self.assertEqual(row.sha256, entry["sha256"])
        self.assertEqual(row.yaml_file, entry["sidecar_location"])
        self.assertEqual(row.yaml_url, entry["sidecar_url"])
        self.assertEqual(row.yaml_bytes, entry["sidecar_bytes"])

    def test_an_explained_absence_stays_a_non_ready_inventory_fact(self) -> None:
        inventory, provenance, _ = self.write(self.freeze())
        row = {row.analysis_id: row for row in read_inventory(inventory)}["GCST4"]
        self.assertFalse(row.ready)
        self.assertEqual((row.readiness_status, row.error, row.data_file), ("data_absent_upstream", "HTTP 404", ""))
        document = yaml.safe_load(provenance.read_text(encoding="utf-8"))
        self.assertEqual(document["readiness_counts"], {"data_absent_upstream": 1, "ok": 3})
        self.assertEqual(document["ready_rows"], 3)

    def test_provenance_records_the_selection_and_both_checksums(self) -> None:
        table, manifest = self.write_inputs()
        inventory, provenance, digest = self.write(self.freeze())
        document = yaml.safe_load(provenance.read_text(encoding="utf-8"))
        self.assertEqual(document["snapshot_id"], "fixture-snapshot")
        self.assertEqual(document["source_collection_id"], "gwas-catalog-ssf")
        self.assertEqual(document["frozen_at"], FROZEN_AT)
        self.assertEqual(document["selection"], SELECT_EUROPEAN)
        self.assertEqual(document["rows"], 4)
        self.assertEqual(document["inventory_tsv_sha256"], digest)
        original_table = document["source_metadata_table"]["original"]
        self.assertEqual(
            (original_table["path"], original_table["sha256"], original_table["rows"]),
            (str(table), sha256_file(table), 5),
        )
        original_manifest = document["acquisition_manifest"]["original"]
        self.assertEqual(
            (original_manifest["path"], original_manifest["sha256"], original_manifest["rows"]),
            (str(manifest), sha256_file(manifest), 5),
        )
        # No writer recorded the passes this fixture manifest came from.
        self.assertIsNone(original_manifest["pass_lineage"])
        # The intended-but-unselected entry is accounted for, not dropped silently.
        self.assertEqual(document["acquisition_manifest"]["unselected_entries"], 1)

    def test_the_selected_connector_rows_are_frozen_beside_the_inventory(self) -> None:
        inventory, provenance, _ = self.write(self.freeze())
        table = inventory.with_name("fixture-snapshot.source-metadata.tsv")
        manifest = inventory.with_name("fixture-snapshot.acquisition-manifest.tsv")
        self.assertEqual(
            [line.split("\t")[0] for line in table.read_text(encoding="utf-8").splitlines()],
            ["source_analysis_id", "GCST1", "GCST2", "GCST3", "GCST4"],
        )
        self.assertEqual(
            [line.split("\t")[0] for line in manifest.read_text(encoding="utf-8").splitlines()],
            ["source_analysis_id", "GCST1", "GCST2", "GCST3", "GCST4"],
        )
        # The frozen rows are the original rows, byte for byte.
        self.assertIn("GCST2\tAsthma\tcase-control\t3000\t1000\t2000\t22\thybrid__European\n",
                      table.read_text(encoding="utf-8"))
        document = yaml.safe_load(provenance.read_text(encoding="utf-8"))
        self.assertEqual(
            document["source_metadata_table"]["frozen"],
            {"path": "fixture-snapshot.source-metadata.tsv", "sha256": sha256_file(table),
             "bytes": table.stat().st_size, "rows": 4},
        )
        self.assertEqual(
            document["acquisition_manifest"]["frozen"],
            {"path": "fixture-snapshot.acquisition-manifest.tsv", "sha256": sha256_file(manifest),
             "bytes": manifest.stat().st_size, "rows": 4},
        )

    def test_the_acquisition_pass_lineage_is_checksummed_when_it_exists(self) -> None:
        _, manifest = self.write_inputs()
        lineage = manifest.with_name("acquisition-manifest.meta.yaml")
        lineage.write_text("passes: []\n", encoding="utf-8")
        _, provenance, _ = self.write(self.freeze())
        recorded = yaml.safe_load(provenance.read_text(encoding="utf-8"))["acquisition_manifest"]["original"]
        self.assertEqual(recorded["pass_lineage"], {"path": str(lineage), "sha256": sha256_file(lineage)})

    def test_generation_can_read_the_frozen_connectors_alone(self) -> None:
        table, manifest = self.write_inputs()
        inventory, provenance, _ = self.write(self.freeze())
        table.unlink()
        manifest.unlink()
        frozen_table, frozen_manifest = load_frozen_connectors(provenance)
        self.assertEqual(frozen_table.analysis_ids, ("GCST1", "GCST2", "GCST3", "GCST4"))
        self.assertEqual(frozen_manifest.entry("GCST2").bytes, 5)
        # The frozen connectors alone reproduce the inventory.
        again = freeze(
            snapshot_id="fixture-snapshot",
            source_collection_id="gwas-catalog-ssf",
            metadata_table=frozen_table.path,
            acquisition_manifest=frozen_manifest.path,
            selection=parse_selection({"all": True}),
            frozen_at=FROZEN_AT,
        )
        second, _, _ = self.write(again, "again")
        self.assertEqual(inventory.read_bytes(), second.read_bytes())

    def test_an_edited_frozen_connector_is_refused(self) -> None:
        inventory, provenance, _ = self.write(self.freeze())
        frozen = inventory.with_name("fixture-snapshot.acquisition-manifest.tsv")
        frozen.write_text(frozen.read_text(encoding="utf-8").replace("HTTP 404", "HTTP 500"), encoding="utf-8")
        with self.assertRaises(FreezeError) as caught:
            load_frozen_connectors(provenance)
        self.assertIn("fixture-snapshot.acquisition-manifest.tsv", str(caught.exception))

    def test_a_provenance_rewritten_without_changing_its_size_is_reparsed(self) -> None:
        """The parse cache is keyed by content, so stale integrity evidence is never served (#290 review)."""
        _, provenance, _ = self.write(self.freeze())
        # A first read fills the cache with the valid provenance.
        load_frozen_connectors(provenance)

        stat = provenance.stat()
        text = provenance.read_text(encoding="utf-8")
        document = yaml.safe_load(text)
        recorded = document["acquisition_manifest"]["frozen"]["sha256"]
        doctored = "0" * 64 if recorded != "0" * 64 else "1" * 64
        provenance.write_text(text.replace(recorded, doctored), encoding="utf-8")
        os.utime(provenance, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(provenance.stat().st_size, stat.st_size)
        self.assertEqual(provenance.stat().st_mtime_ns, stat.st_mtime_ns)

        with self.assertRaisesRegex(FreezeError, "sha256"):
            load_frozen_connectors(provenance)

    def test_provenance_records_lineage_and_duplicate_content(self) -> None:
        _, provenance, _ = self.write(self.freeze())
        document = yaml.safe_load(provenance.read_text(encoding="utf-8"))
        self.assertEqual(
            document["lineage"],
            [{"analysis_id": "GCST3", "original_location": str(self.mirror / "original" / "all.tsv.gz"),
              "original_sha256": "c" * 64, "reformat_tool": "split-by-analysis 1.2.0"}],
        )
        self.assertEqual(
            [group["analysis_ids"] for group in document["duplicate_content_groups"]], [["GCST1", "GCST2"]]
        )

    def test_a_list_selection_is_recorded_and_honoured(self) -> None:
        inventory, provenance, _ = self.write(self.freeze({"analyses": ["GCST3", "GCST1"]}))
        self.assertEqual([row.analysis_id for row in read_inventory(inventory)], ["GCST1", "GCST3"])
        document = yaml.safe_load(provenance.read_text(encoding="utf-8"))
        self.assertEqual(document["selection"], {"analyses": ["GCST3", "GCST1"]})
        self.assertEqual(document["acquisition_manifest"]["unselected_entries"], 3)

    def test_freezing_twice_gives_the_same_bytes(self) -> None:
        first = self.write(self.freeze(), "first")
        second = self.write(self.freeze(), "second")
        self.assertEqual(first[0].read_bytes(), second[0].read_bytes())
        self.assertEqual(
            first[1].read_text(encoding="utf-8").replace("/first/", "/second/"),
            second[1].read_text(encoding="utf-8"),
        )


class RecordedOriginalPathTest(unittest.TestCase):
    """A connector original committed in the checkout is recorded relative to it (#300).

    A frozen snapshot is committed, so its provenance must name the same
    original in every checkout: an absolute path into one checkout would name
    a file another checkout does not have. An original elsewhere -- a read-only
    directory on /data -- keeps its absolute path.
    """

    def test_an_original_in_the_checkout_is_recorded_relative_to_it(self) -> None:
        inside = REPO_ROOT / "resources/generators/gwas-catalog-ragged/connectors/sun-2018.source-metadata.tsv"
        self.assertEqual(
            recorded_path(inside), "resources/generators/gwas-catalog-ragged/connectors/sun-2018.source-metadata.tsv"
        )
        self.assertEqual(original_path(recorded_path(inside)), inside)
        outside = Path("/data/opengwasdb/raw/ebi-gwas-catalog/connectors/2026-10-20/x.tsv")
        self.assertEqual(recorded_path(outside), str(outside))
        self.assertEqual(original_path(str(outside)), outside)


class FreezeFailureTest(FreezeFixture):
    def assertFreezeFails(self, error: type[Exception], *fragments: str, selection=SELECT_EUROPEAN) -> None:
        with self.assertRaises(error) as caught:
            self.freeze(selection)
        for fragment in fragments:
            self.assertIn(fragment, str(caught.exception))

    def test_a_selected_analysis_with_no_file_fails(self) -> None:
        self.manifest_rows = [row for row in self.manifest_rows if row["source_analysis_id"] != "GCST3"]
        self.assertFreezeFails(ReconciliationError, "no Acquisition Manifest entry", "GCST3")

    def test_a_file_no_analysis_claims_fails(self) -> None:
        self.manifest_rows.append(self.ready("GCST404", "GCST404.h.tsv.gz", b"stray"))
        self.assertFreezeFails(ReconciliationError, "no Analysis", "GCST404")

    def test_every_refusal_is_a_freeze_error(self) -> None:
        for error in (ReconciliationError, SelectionError, SourceMetadataTableError, AcquisitionManifestError):
            self.assertTrue(issubclass(error, FreezeError), error)

    def test_both_failures_are_named_together(self) -> None:
        self.manifest_rows = [row for row in self.manifest_rows if row["source_analysis_id"] != "GCST3"]
        self.manifest_rows.append(self.ready("GCST404", "GCST404.h.tsv.gz", b"stray"))
        self.assertFreezeFails(ReconciliationError, "GCST3", "GCST404")

    def test_a_duplicate_analysis_in_the_table_fails(self) -> None:
        self.table_rows.append(list(TABLE_ROWS[0]))
        self.assertFreezeFails(SourceMetadataTableError, "duplicate", "GCST1")

    def test_a_duplicate_analysis_in_the_manifest_fails(self) -> None:
        self.manifest_rows.append(dict(self.ready("GCST1", "GCST1-again.h.tsv.gz", b"again")))
        self.assertFreezeFails(AcquisitionManifestError, "duplicate", "GCST1")

    def test_a_duplicate_file_fails(self) -> None:
        self.manifest_rows[2]["location"] = self.manifest_rows[0]["location"]
        self.assertFreezeFails(AcquisitionManifestError, "duplicate", self.manifest_rows[0]["location"])

    def test_a_list_naming_an_unknown_analysis_fails(self) -> None:
        self.assertFreezeFails(SelectionError, "GCST404", selection={"analyses": ["GCST1", "GCST404"]})


class FreezeNeverListsADirectoryTest(FreezeFixture):
    def test_an_extra_file_on_disk_changes_nothing(self) -> None:
        before = self.write(self.freeze(), "before")
        (self.mirror / "GCST5.h.tsv.gz").write_bytes(b"not in any manifest")
        (self.mirror / "GCST5.h.tsv.gz-meta.yaml").write_text("genome_assembly: GRCh38\n", encoding="utf-8")
        after = self.write(self.freeze(), "after")
        self.assertEqual(before[0].read_bytes(), after[0].read_bytes())
        self.assertEqual(before[2], after[2])

    def test_freeze_calls_no_directory_listing(self) -> None:
        def tripwire(*_args, **_kwargs):
            raise AssertionError("freeze listed a directory")

        with ExitStack() as stack:
            for target in ("os.listdir", "os.scandir", "os.walk", "glob.glob", "glob.iglob"):
                stack.enter_context(mock.patch(target, tripwire))
            for name in ("iterdir", "glob", "rglob", "walk"):
                if hasattr(Path, name):
                    stack.enter_context(mock.patch.object(Path, name, tripwire))
            # The patches are live, not vacuous.
            self.assertIs(os.listdir, tripwire)
            self.assertIs(glob.glob, tripwire)
            frozen = self.freeze()
        self.assertEqual(len(frozen.rows), 4)

    def test_freeze_reads_no_source_file(self) -> None:
        """Only the two connector files are opened; the mirror is never read."""
        real_open = io.open
        opened: list[str] = []

        def recording_open(file, *args, **kwargs):
            opened.append(str(file))
            return real_open(file, *args, **kwargs)

        with mock.patch("builtins.open", recording_open), mock.patch("io.open", recording_open):
            self.freeze()
        # The recorder saw the connector files, so it was live, not vacuous...
        self.assertIn(str(self.tmp / "source-metadata.tsv"), opened)
        self.assertIn(str(self.tmp / "acquisition-manifest.tsv"), opened)
        # ...and saw nothing under the mirror.
        self.assertFalse([path for path in opened if path.startswith(str(self.mirror))], opened)


class FreezeCommandTest(FreezeFixture):
    def run_main(self, *extra: str) -> tuple[int, str, str]:
        table, manifest = self.write_inputs()
        config = self.tmp / "release.yaml"
        config.write_text(yaml.safe_dump({"selection": SELECT_EUROPEAN}), encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([
                "--source-metadata-table", str(table),
                "--acquisition-manifest", str(manifest),
                "--selection", str(config),
                "--snapshot-id", "fixture-snapshot",
                "--source-collection-id", "gwas-catalog-ssf",
                "--out-dir", str(self.tmp / "cli"),
                "--frozen-at", FROZEN_AT,
                *extra,
            ])
        return code, out.getvalue(), err.getvalue()

    def test_the_command_writes_the_inventory_and_its_provenance(self) -> None:
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        inventory = self.tmp / "cli" / "fixture-snapshot.tsv"
        provenance = self.tmp / "cli" / "fixture-snapshot.meta.yaml"
        self.assertEqual(len(read_inventory(inventory)), 4)
        self.assertTrue((self.tmp / "cli" / "fixture-snapshot.source-metadata.tsv").is_file())
        self.assertTrue((self.tmp / "cli" / "fixture-snapshot.acquisition-manifest.tsv").is_file())
        self.assertEqual(yaml.safe_load(provenance.read_text(encoding="utf-8"))["selection"], SELECT_EUROPEAN)
        self.assertIn("fixture-snapshot", out)

    def test_the_command_never_overwrites_a_frozen_inventory(self) -> None:
        self.assertEqual(self.run_main()[0], 0)
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)

    def test_a_leftover_frozen_connector_also_blocks_the_command(self) -> None:
        leftover = self.tmp / "cli" / "fixture-snapshot.source-metadata.tsv"
        leftover.parent.mkdir()
        leftover.write_text("source_analysis_id\nGCST1\n", encoding="utf-8")
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("fixture-snapshot.source-metadata.tsv", err)
        self.assertFalse((self.tmp / "cli" / "fixture-snapshot.tsv").exists())

    def test_the_command_reports_a_failed_freeze(self) -> None:
        self.manifest_rows.append(self.ready("GCST404", "GCST404.h.tsv.gz", b"stray"))
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("GCST404", err)
        self.assertFalse((self.tmp / "cli" / "fixture-snapshot.tsv").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
