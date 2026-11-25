#!/usr/bin/env python3
"""Measure a built LD panel directory: its blocks, its variants and its bytes (#540).

    python3 tools/ld-panel/measure_panel.py <panel-dir>...

Each <panel-dir> is one ancestry's directory, `<chr>/<start>-<end>.{tsv,ldeig.npz,...}`.
Read-only: it stats files and counts the rows of each block's `.tsv`. It prints
one JSON object per directory, `{"path", "n_blocks", "n_variants", "bytes"}`:

- `n_blocks`: the `.ldeig.npz` files, one per block;
- `n_variants`: the data rows of every block's `.tsv` (its header excluded);
- `bytes`: `ogstores.store_size.store_size_on_disk`, the one definition of a
  directory's size (every file under it, from a stat of each).

The values go in the panel's `resources/reference-resources/<id>/resource.yaml`
(`ld.n_blocks`, `ld.n_variants`, `ld.bytes`), measured on the host.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from ogstores.store_size import store_size_on_disk


def measure(panel: Path) -> dict[str, object]:
    n_blocks = n_variants = 0
    for root, _dirs, files in os.walk(panel):
        for name in files:
            path = Path(root, name)
            if name.endswith(".ldeig.npz"):
                n_blocks += 1
            elif name.endswith(".tsv") and Path(root) != panel:
                with path.open("rb") as handle:
                    n_variants += sum(1 for _ in handle) - 1
    return {"path": str(panel), "n_blocks": n_blocks, "n_variants": n_variants, "bytes": store_size_on_disk(panel)}


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    for arg in argv:
        print(json.dumps(measure(Path(arg))), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
