#!/usr/bin/env python3
"""This repository's architecture model claims every code file it tracks, and nothing stale (#418).

`docs/architecture/components.yaml` says which component owns which path. A
tracked code file under src/, workflow/ or tools/ that no component claims, or a
claim that matches no tracked file, fails here. The ownership rule is the project
site's (`project_site.collect.architecture_model`, in og-dev-hub, which the `dev`
environment installs; og-dev-hub#7), so this check and the site's drawing of
this repository can never disagree; the rule's own tests are og-dev-hub's.

Run from the repository root:
    python3 tests/architecture/test_architecture_ownership.py
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from project_site.collect import architecture_model as model

REPO = Path(__file__).resolve().parents[2]


class ThisRepositoryTests(unittest.TestCase):
    def test_this_repositorys_model_claims_every_code_file_and_nothing_stale(self) -> None:
        tracked = model.split_nul(
            subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"], capture_output=True, check=True).stdout.decode()
        )
        loaded = model.load_model((REPO / "docs/architecture/components.yaml").read_text(encoding="utf-8"))
        gaps = model.check_claims(model.claims_of(loaded["components"]), tracked, model.is_registry_code)
        self.assertEqual(gaps, {"unclaimed": [], "stale": []})


if __name__ == "__main__":
    unittest.main()
