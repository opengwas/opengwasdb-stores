#!/usr/bin/env python3
"""The FinnGen acquisition Manifest: what acquisition produced, in the shared format (#288).

The FinnGen acquisition script reads the provider's endpoint manifest and, for
each Analysis, records the file it fetched (or found) -- location, size, sha256,
URL -- or the reason it has no usable file. This suite drives the real
command line against a local HTTP fixture server, so it exercises the resumable
download, the canonical Acquisition Manifest the shared freeze reads, and the
explained absence of an endpoint the provider no longer serves.

Run from the repository root:
    python3 tests/finngen-r13-pipeline/test_acquisition_manifest.py
"""

from __future__ import annotations

import csv
import hashlib
import http.server
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import ClassVar

import yaml

from manifest_generation.acquisition_manifest import (
    load_acquisition_manifest,
    pass_lineage_path,
)

ROOT = Path(__file__).resolve().parents[2]
CLI = ("-m", "manifest_generation.providers.finngen.acquire")


class EndpointHandler(http.server.BaseHTTPRequestHandler):
    """Serves ``/present.gz`` and answers everything else with 404."""

    payload = b""
    requests: ClassVar[list[tuple[str, str]]] = []

    def do_GET(self) -> None:
        self.requests.append((self.path, self.headers.get("Range") or ""))
        if self.path != "/present.gz":
            self.send_error(404)
            return
        start = 0
        range_header = self.headers.get("Range")
        if range_header:
            start = int(range_header.removeprefix("bytes=").removesuffix("-"))
            self.send_response(206)
            self.send_header(
                "Content-Range", f"bytes {start}-{len(self.payload) - 1}/{len(self.payload)}"
            )
        else:
            self.send_response(200)
        body = self.payload[start:]
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", '"fixture-etag"')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        return


def _write_manifest(path: Path, rows: list[tuple[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(
            ["phenocode", "phenotype", "category", "num_cases", "num_controls",
             "path_bucket", "path_https"]
        )
        for phenocode, url in rows:
            writer.writerow(
                [phenocode, f"Trait {phenocode}", "Category", "100", "1000",
                 url.replace("https://", "gs://"), url]
            )


def _read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


class FinnGenAcquisitionManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = (b"FinnGen fixture association bytes\n" * 8192) + b"done\n"
        EndpointHandler.payload = cls.payload
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), EndpointHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self) -> None:
        EndpointHandler.requests = []
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.source_dir = self.tmp / "source"
        self.source_dir.mkdir()
        self.manifest = self.tmp / "finngen_R13_manifest.tsv"
        self.out = self.tmp / "acquisition-manifest.tsv"
        base = f"http://127.0.0.1:{self.server.server_port}"
        _write_manifest(
            self.manifest,
            [("PRESENT", f"{base}/present.gz"), ("ABSENT", f"{base}/absent.gz")],
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_acquire(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, *CLI, "--manifest", str(self.manifest),
             "--source-dir", str(self.source_dir), "--out", str(self.out), *args],
            cwd=str(ROOT), text=True, capture_output=True, check=False,
        )

    def test_a_fetched_file_is_ready_and_an_unserved_one_is_an_explained_absence(self) -> None:
        run = self.run_acquire("--workers", "2")
        # The missing endpoint is explained in the manifest, and the run still
        # exits non-zero so an unattended acquisition retries it.
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)

        manifest = load_acquisition_manifest(self.out)
        present, absent = manifest.entry("PRESENT"), manifest.entry("ABSENT")
        self.assertEqual(present.status, "ok")
        self.assertEqual(present.location, str(self.source_dir / "finngen_R13_PRESENT.gz"))
        self.assertEqual(present.bytes, len(self.payload))
        self.assertEqual(present.sha256, hashlib.sha256(self.payload).hexdigest())
        self.assertEqual(present.source_url, f"http://127.0.0.1:{self.server.server_port}/present.gz")

        self.assertEqual(absent.status, "data_absent_upstream")
        self.assertEqual(absent.location, "")
        self.assertEqual(absent.bytes, None)
        self.assertEqual(absent.sha256, "")
        self.assertIn("404", absent.error)

        rows = _read_tsv(self.out)
        self.assertEqual([row["source_analysis_id"] for row in rows], ["PRESENT", "ABSENT"])
        self.assertEqual(manifest.columns, tuple(rows[0]))
        self.assertNotIn("analysis_id", manifest.columns)

    def test_the_pass_sidecar_pins_the_provider_manifest_and_counts(self) -> None:
        self.run_acquire()
        sidecar = pass_lineage_path(self.out)
        self.assertTrue(sidecar.is_file())
        document = yaml.safe_load(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(document["rows"], 2)
        self.assertEqual(document["readiness_counts"], {"ok": 1, "data_absent_upstream": 1})
        self.assertEqual(document["provider_manifest"]["path"], str(self.manifest))
        self.assertEqual(
            document["provider_manifest"]["sha256"],
            hashlib.sha256(self.manifest.read_bytes()).hexdigest(),
        )
        self.assertEqual(document["provider_manifest"]["rows"], 2)
        self.assertEqual(
            document["sha256"],
            hashlib.sha256(self.out.read_bytes()).hexdigest(),
        )

    def test_a_present_file_is_checksummed_and_not_refetched(self) -> None:
        self.run_acquire()
        EndpointHandler.requests = []
        second = self.run_acquire()
        self.assertEqual(second.returncode, 1)  # ABSENT is still an explained absence
        self.assertFalse(
            any(path == "/present.gz" for path, _range in EndpointHandler.requests)
        )
        self.assertEqual(load_acquisition_manifest(self.out).entry("PRESENT").status,
                         "already_present")

    def test_a_partial_download_resumes_from_its_offset(self) -> None:
        target = self.source_dir / "finngen_R13_PRESENT.gz"
        target.with_name(target.name + ".part").write_bytes(self.payload[: len(self.payload) // 3])
        run = self.run_acquire("--only-analysis-id", "PRESENT")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(target.read_bytes(), self.payload)
        self.assertFalse(target.with_name(target.name + ".part").exists())
        self.assertEqual(
            EndpointHandler.requests,
            [("/present.gz", f"bytes={len(self.payload) // 3}-")],
        )
        self.assertEqual(load_acquisition_manifest(self.out).entry("PRESENT").status, "ok")

    def test_a_targeted_run_writes_only_the_analyses_it_was_asked_for(self) -> None:
        run = self.run_acquire("--only-analysis-id", "PRESENT")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(
            [entry.source_analysis_id for entry in load_acquisition_manifest(self.out).entries],
            ["PRESENT"],
        )

    def test_a_failed_transfer_is_recorded_with_its_error(self) -> None:
        _write_manifest(
            self.manifest,
            [("PRESENT", f"http://127.0.0.1:{self.server.server_port}/present.gz"),
             ("UNREACHABLE", "http://127.0.0.1:1/unreachable.gz")],
        )
        run = self.run_acquire("--only-analysis-id", "UNREACHABLE")
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        entry = load_acquisition_manifest(self.out).entry("UNREACHABLE")
        self.assertEqual(entry.status, "data_failed")
        self.assertTrue(entry.error)

    def test_the_written_manifest_is_what_the_shared_freeze_reads(self) -> None:
        self.run_acquire()
        self.assertEqual(
            load_acquisition_manifest(self.out).readiness_counts,
            {"ok": 1, "data_absent_upstream": 1},
        )


if __name__ == "__main__":
    unittest.main()
