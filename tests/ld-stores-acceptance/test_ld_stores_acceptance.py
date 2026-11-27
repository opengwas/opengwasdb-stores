#!/usr/bin/env python3
"""The real-data acceptance of a Store rebuilt from its record (#541): what the engine's command line shows.

It runs only on a host that holds both Stores, and is skipped everywhere else,
CI included:

    OPENGWASDB_LD_REBUILT_ID=OGL-00009 \\
    OPENGWASDB_LD_REBUILT_STORE=/data/opengwasdb/work/.../hgdp1kgp-v2-AMR-k10.store \\
        pixi run -e dev python3 tests/ld-stores-acceptance/test_ld_stores_acceptance.py

`pixi run ld-stores rebuild <ld_id> --out <dir>` rebuilds a Store and runs
these same checks. Checks that need the engine's internals (its validation rule
list, the direct decoder against zarr) are the engine's, in opengwasdb's
`tests/ld/acceptance/test_hgdp1kgp_stores.py`, run against any Store path.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from ld_registry import compare, record
from ld_registry.recipe import run_command
from ld_registry.rebuild import plan_rebuild

REPO_ROOT = Path(__file__).resolve().parents[2]
LD_ID = os.environ.get("OPENGWASDB_LD_REBUILT_ID", "")
REBUILT = Path(os.environ.get("OPENGWASDB_LD_REBUILT_STORE", "/nonexistent"))


@unittest.skipUnless(LD_ID and REBUILT.is_dir(), "set OPENGWASDB_LD_REBUILT_ID and OPENGWASDB_LD_REBUILT_STORE")
class TestARebuiltStore(unittest.TestCase):
    def test_it_validates_and_its_ld_info_is_the_records_but_for_the_builds_own_fields(self) -> None:
        document, errors = record.load_record(record.record_path(REPO_ROOT / record.LD_ROOT, LD_ID))
        self.assertEqual(errors, [])
        assert document is not None
        original = Path(document["location"])
        self.assertNotEqual(REBUILT.absolute(), original, "the rebuilt Store is a new Store")
        # The rebuild as `pixi run ld-stores rebuild` planned it, into the rebuilt Store's directory.
        plan = plan_rebuild(document, REBUILT.parent)
        self.assertEqual(plan.store, REBUILT.absolute(), "the rebuilt Store keeps its original's name")

        run_command(plan.validate)  # this run is apart from the rebuild, so it validates on its own
        found, problems = compare.accept(original, REBUILT, recorded_sha256=plan.recorded_sha256, keep=plan.keep)
        print(compare.render(found) or "no difference")
        self.assertEqual(problems, [])
        self.assertEqual(compare.failures(found), [], compare.render(compare.failures(found)))


if __name__ == "__main__":
    unittest.main()
