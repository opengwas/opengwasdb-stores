"""The LD Store Registry's command line (docs/spec/ld-store-registry.md).

    python3 -m ld_registry write OGL-00001 /path/to/the.store [--ld-root stores/ld] [--rebuild rebuild.yaml]

`write` writes or refreshes the record from the Store at the location, then
checks it: it exits 1, naming each field, while a field a person must write is
still missing. `--rebuild` gives the record its `rebuild` section, a YAML
mapping, as the build that made the Store writes it (#541). `pixi run index` regenerates `ld-resources.tsv` from the records.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from ld_registry import REPO_ROOT
from ld_registry.record import load_record
from ld_registry.writer import write_record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m ld_registry", description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    write = commands.add_parser("write", help="write or refresh a record from an LD Store")
    write.add_argument("ld_id", help="the record's id, OGL-NNNNN")
    write.add_argument("location", type=Path, help="the LD Store's directory")
    write.add_argument("--ld-root", type=Path, default=REPO_ROOT / "stores" / "ld")
    write.add_argument("--rebuild", type=Path, help="a YAML file holding the build's `rebuild` mapping")
    args = parser.parse_args(argv)

    try:
        rebuild = yaml.safe_load(args.rebuild.read_text(encoding="utf-8")) if args.rebuild else None
        if args.rebuild and not isinstance(rebuild, dict):
            raise ValueError(f"{args.rebuild}: a rebuild is a mapping, got {type(rebuild).__name__}")
        written = write_record(args.ld_root, args.ld_id, args.location, rebuild=rebuild)
    except ValueError as exc:  # a WriterRefusal, or `ld info` failing
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {written.path}")
    if written.previous_store_bytes not in (None, written.store_bytes):
        print(f"  measured.store_bytes: {written.previous_store_bytes} -> {written.store_bytes}")
    errors = load_record(written.path)[1]
    for error in errors:
        print(f"  {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
