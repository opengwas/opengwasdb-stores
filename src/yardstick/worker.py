"""`python -m yardstick.worker MODE SPEC.json`: one Store's measures in this fresh process.

MODE is `timed` (every timed measure and bytes on disk) or `rss` (peak RSS of a
full extraction) for a GWAS Store, and `ld` or `ld_rss` the same for an LD Store or
Genotype Store (`yardstick.ld`, #504). The last line of stdout is the JSON record.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from yardstick import measure


def main(argv: list[str]) -> int:
    mode, spec_path = argv
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    store_path = Path(spec["store_path"])
    if mode == "timed":
        record = measure.measure_store(store_path, spec["entry"], spec["repetitions"], spec.get("region_width_bp"))
    elif mode == "rss":
        record = measure.measure_peak_rss(store_path, spec["entry"])
    elif mode == "ld":
        from yardstick import ld

        record = ld.measure_ld_store(spec["entry"], spec["repetitions"])
    elif mode == "ld_rss":
        from yardstick import ld

        record = ld.measure_ld_peak(spec["entry"])
    else:
        raise SystemExit(f"unknown mode {mode!r}")
    print(json.dumps(record))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
