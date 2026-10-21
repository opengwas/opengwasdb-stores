#!/usr/bin/env python3
"""The eQTLGen BESD generator's `--mode emit-registry` (#248) and its ids (#319).

The generator keeps its working bundle in its work root,
`<artifact-root>/<store-id>/generate`. `emit-registry` projects the current
Release Bundle contract from it and the config's `build:` block, and emits it
through the one emitter (manifest_generation.emit). How that bundle
lands is the emitter's, tested in tests/emit; this checks the generator's
wiring, without the BESD subset or Store build the default mode runs.

Issue #319: the Build Recipe passes no `tissue` to `build-ragged-besd`, so the
built Store names each Analysis by bare probe id and leaves `tissue` empty; the
generator writes the config's tissue into the Release Manifest itself.

Run from the repository root:
    python3 tests/eqtlgen-besd-ragged/test_emit_registry.py
"""

from __future__ import annotations

import csv
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from manifest_generation.providers.eqtlgen.generate import write_release_manifest_analyses
from ogstores import bundle

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ("-m", "manifest_generation.providers.eqtlgen.generate")

#: The built Store's own table: bare probe ids, tissue blank (no `--tissue`).
STORE_ANALYSES_TSV = (
    "analysis_id\tstored_effect_scale\tsample_size_kind\tsample_size_scope\tsample_size\t"
    "original_effect_scale\toriginal_sd_method\tassigned_ancestry\tancestry_assignment_method\t"
    "source_file\ttissue\n"
    "ENSG00000000001\tsd\ttotal\tanalysis_level\t31684\tsd\tdeclared_standardised\tEUR\t"
    "source_fallback\t/data/besd/pilot\t\n"
)

#: The Release Manifest the generator must emit: the same row, tissue filled.
ANALYSES_TSV = STORE_ANALYSES_TSV.replace("\t\n", "\twhole_blood\n")


class EqtlgenEmitRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.artifact_root = self.root / "artifacts"
        self.registry = self.root / "stores"
        work = self.artifact_root / "OGS-99071" / "generate"
        (work / "sidecars").mkdir(parents=True)
        store_analyses = self.root / "store-analyses.tsv"
        store_analyses.write_text(STORE_ANALYSES_TSV, encoding="utf-8")
        write_release_manifest_analyses(
            store_analyses, work / "analyses.tsv", tissue="whole_blood"
        )
        (work / "sidecars" / "selection.tsv").write_text("probe_id\nENSG00000000001\n", encoding="utf-8")
        (work / "validation.yaml").write_text(yaml.safe_dump({"status": "passed", "validated_at": None}), encoding="utf-8")
        self.config = self.root / "config.yaml"
        self.config.write_text(
            yaml.safe_dump(
                {
                    "store_family_id": "eqtlgen-cis-pilot",
                    "family_release_id": "pilot-fixture",
                    "label": "pilot-fixture",
                    "access_posture": "public",
                    "description": "eQTLGen fixture candidate",
                    "source": {
                        "source_snapshot_id": "eqtlgen-fixture",
                        "besd_prefix": "/data/besd/pilot",
                        "source_genome_build": "hg19",
                        "tissue": "whole_blood",
                    },
                    "output": {"store_id": "OGS-99071", "registry_root": str(self.registry)},
                    "build": {
                        "layout": "ragged",
                        "completion_state": "observed_only",
                        "command": "build-ragged-besd",
                        "options": {"source-build": "hg19"},
                        "post": {"top_hits": False, "overview": False},
                    },
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_emit_registry_emits_the_working_bundle_as_a_candidate(self) -> None:
        env = {**os.environ, "OPENGWASDB_ARTIFACT_ROOT": str(self.artifact_root)}
        res = subprocess.run(
            [sys.executable, *GENERATOR, f"--config={self.config}", "--mode=emit-registry"],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True,
        )

        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        release = yaml.safe_load((self.registry / "OGS-99071" / "release.yaml").read_text(encoding="utf-8"))
        self.assertEqual(release["status"], "candidate")
        self.assertEqual(release["label"], "pilot-fixture")
        self.assertTrue(any(c.endswith("--mode=emit-registry") for c in release["generator"]["commands"]), release)
        build = yaml.safe_load((self.registry / "OGS-99071" / "build.yaml").read_text(encoding="utf-8"))
        self.assertEqual(build["build"]["command"], "build-ragged-besd")
        self.assertEqual(release["source_snapshot"]["besd_prefix"], "/data/besd/pilot")

        # Issue #319: no `tissue` option, so the built Store's ids are the bare
        # probe ids, and the tissue lives in the manifest's own column.
        self.assertNotIn("tissue", build["build"]["options"], build["build"])
        analyses_text = (self.registry / "OGS-99071" / "analyses.tsv").read_text(encoding="utf-8")
        self.assertEqual(analyses_text, ANALYSES_TSV)
        rows = list(csv.DictReader(io.StringIO(analyses_text), delimiter="\t"))
        self.assertEqual([row["analysis_id"] for row in rows], ["ENSG00000000001"])
        self.assertEqual({row["tissue"] for row in rows}, {"whole_blood"})

        result = bundle.check(
            bundle.load("OGS-99071", registry_root=self.registry), registry_root=self.registry
        )
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
