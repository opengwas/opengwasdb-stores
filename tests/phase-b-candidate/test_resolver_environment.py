#!/usr/bin/env python3
"""The resolver runs one numerical thread per worker (#300 review).

``opengwasdb resolve-analyses`` parallelises by worker processes. Without a
limit, each worker's numerical libraries start a thread per core: 32 workers
on the 224-core generation host ran about 190 threads each, and the host's
load reached about 2,000. The resolver subprocess therefore gets
``OMP_NUM_THREADS``, ``OPENBLAS_NUM_THREADS``, ``MKL_NUM_THREADS`` and
``NUMEXPR_NUM_THREADS`` set to 1, unless the operator has set one already.

Run from the repository root:
    python3 tests/phase-b-candidate/test_resolver_environment.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from manifest_generation.candidate_workflow import run_resolver

VARIABLES = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")
#: A stand-in resolver that prints the thread variables it was started with.
PRINT_ENVIRONMENT = (
    "import os; print(' '.join(f'{name}={os.environ.get(name)}' for name in "
    + repr(VARIABLES)
    + "))"
)


class ResolverEnvironmentTests(unittest.TestCase):
    def run_printing_resolver(self) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "resolve.log"
            run = run_resolver([sys.executable, "-c", PRINT_ENVIRONMENT], cwd=Path(tmp), log_path=log)
            self.assertEqual(run.returncode, 0)
            return log.read_text(encoding="utf-8").strip()

    def test_the_resolver_runs_one_numerical_thread_per_worker(self) -> None:
        with mock.patch.dict(os.environ):
            for name in VARIABLES:
                os.environ.pop(name, None)
            self.assertEqual(
                self.run_printing_resolver(),
                "OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1",
            )

    def test_an_operators_own_setting_is_kept(self) -> None:
        with mock.patch.dict(os.environ, {"OMP_NUM_THREADS": "4"}):
            for name in VARIABLES[1:]:
                os.environ.pop(name, None)
            self.assertEqual(
                self.run_printing_resolver(),
                "OMP_NUM_THREADS=4 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1",
            )


if __name__ == "__main__":
    unittest.main()
