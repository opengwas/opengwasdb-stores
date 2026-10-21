#!/usr/bin/env python3
"""The eQTLGen BESD generator's `--mode emit-registry` (#248).

The generator keeps its working bundle in its work root,
`<artifact-root>/<store-id>/generate`. `emit-registry` projects the current
Release Bundle contract from it and the config's `build:` block, and emits it
through the one emitter (manifest_generation.emit). How that bundle
lands is the emitter's, tested in tests/emit; this checks the generator's
wiring, without the BESD subset or Store build the default mode runs.

Run from the repository root:
    python3 tests/eqtlgen-besd-ragged/test_emit_registry.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ("-m", "manifest_generation.providers.eqtlgen.generate")

ANALYSES_TSV = (
    "analysis_id\tstored_effect_scale\tsample_size_kind\tsample_size_scope\tsample_size\t"
    "original_effect_scale\toriginal_sd_method\tassigned_ancestry\tancestry_assignment_method\tsource_file\n"
    "ENSG00000000001::whole_blood\tsd\ttotal\tanalysis_level\t31684\tsd\tdeclared_standardised\tEUR\t"
    "source_fallback\t/data/besd/pilot\n"
)


class EqtlgenEmitRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.artifact_root = self.root / "artifacts"
        self.registry = self.root / "stores"
        work = self.artifact_root / "OGS-99071" / "generate"
        (work / "sidecars").mkdir(parents=True)
        (work / "analyses.tsv").write_text(ANALYSES_TSV, encoding="utf-8")
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
                    },
                    "output": {"store_id": "OGS-99071", "registry_root": str(self.registry)},
                    "build": {
                        "layout": "ragged",
                        "completion_state": "observed_only",
                        "command": "build-ragged-besd",
                        "options": {"source-build": "hg19", "tissue": "whole_blood"},
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
        self.assertEqual((self.registry / "OGS-99071" / "analyses.tsv").read_text(encoding="utf-8"), ANALYSES_TSV)


if __name__ == "__main__":
    unittest.main()
