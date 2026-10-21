#!/usr/bin/env python3
"""Gate-aware OpenGWASDB consumer smoke test for generated matrix-free blocks."""
from __future__ import annotations
import argparse, subprocess
from pathlib import Path
import numpy as np
from opengwasdb.completion.ld_panel import list_all_blocks, load_ld_eigenvectors

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--panel", type=Path, required=True); ap.add_argument("--ancestry", required=True)
    ap.add_argument("--limit", type=int, default=3)
    args = ap.parse_args()
    gate = subprocess.run(["gh", "issue", "view", "10", "-R", "explodecomputer/opengwasdb",
                           "--json", "state", "--jq", ".state"], capture_output=True, text=True)
    if gate.returncode or gate.stdout.strip() != "CLOSED":
        raise SystemExit("external gate explodecomputer/opengwasdb#10 is not merged/closed")
    blocks = [b for chrom in (args.panel / args.ancestry).iterdir() if chrom.is_dir()
              for b in list_all_blocks(args.panel, args.ancestry, chrom.name)][:args.limit]
    if not blocks: raise SystemExit("no generated blocks loaded")
    for block in blocks:
        written = np.load(block.ldeig_npz_path)
        values, vectors = load_ld_eigenvectors(block, thresh=1.0)
        if not np.array_equal(values, written["values"][:len(values)]) or not np.array_equal(vectors, written["vectors"]):
            raise SystemExit(f"consumer mismatch: {block.block_id}")
        print(f"ok {block.block_id} {vectors.shape}")
    return 0
if __name__ == "__main__": raise SystemExit(main())
