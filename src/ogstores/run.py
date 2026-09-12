"""Execute one Step, record it, and refuse to damage anything.

Runs the argv, captures stdout/stderr/timing/exit status, and writes
`records/<step>.json` including the argv as executed and the `opengwasdb`
revision actually used -- which `register` later compares against `plan()`'s
planned argv, so drift between what was documented and what ran is caught.

Two safety rules live here, and they are what make a failed phase harmless:

* a build writes to `store.opengwasdb.partial` and is renamed into place only
  on success;
* the rename refuses to overwrite an existing Store unless explicitly forced.

It does not read a step's output back, interpret it, or re-validate it.
"""

from __future__ import annotations

raise NotImplementedError("Phase A implementation pending; see docs/spec/store-release-workflow.md")
