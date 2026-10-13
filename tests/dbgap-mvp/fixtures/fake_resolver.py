#!/usr/bin/env python3
"""Hermetic fake `opengwasdb resolve-analyses` for pilot wrapper tests.

Behaviour is selected by ``FAKE_RESOLVER_MODE``:
  ok      write one per-Analysis record + index.json, exit 0
  fail    exit 1
  sleep   spawn a grandchild that sleeps, then sleep (for wall-timeout tests)
  memory  allocate committed memory and hold (for RSS-breach tests)

It reads only the manifest and the paths it is given; it never touches a real
association body.
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import time


def main() -> int:
    mode = os.environ.get("FAKE_RESOLVER_MODE", "ok")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    # args[0] == "resolve-analyses"
    manifest = args[1]
    records = args[2]

    if mode == "fail":
        print("fake resolver: deliberate failure", file=sys.stderr)
        return 1
    if mode == "sleep":
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        time.sleep(600)
        return 0
    if mode == "memory":
        blocks = []
        while True:
            block = bytearray(200 * 1024 * 1024)
            for offset in range(0, len(block), 4096):
                block[offset] = 1
            blocks.append(block)
            time.sleep(0.05)

    if mode == "read_denied":
        with open("/data/opengwasdb/raw/not-allowed/secret.tsv.gz", "rb") as handle:
            handle.read(1)
        return 0

    os.makedirs(records, exist_ok=True)
    with open(manifest, newline="", encoding="utf-8") as handle:
        ids = [row["analysis_id"] for row in csv.DictReader(handle, delimiter="\t")]
    for analysis_id in ids:
        with open(os.path.join(records, f"{analysis_id}.json"), "w", encoding="utf-8") as handle:
            json.dump({"analysis_id": analysis_id, "status": "success"}, handle)
    with open(os.path.join(records, "index.json"), "w", encoding="utf-8") as handle:
        json.dump({"n_total": len(ids), "n_success": len(ids)}, handle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
