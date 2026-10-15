"""CLI bridge to opengwasdb's shared `analyses.tsv` schema (issue #51).

R generator scripts have no Python import machinery, so this is a thin CLI
wrapper around `opengwasdb.model.analyses.validate_analyses()` they can shell
out to, rather than hand-maintaining a duplicate required-column/vocabulary
list in R that can silently drift from opengwasdb's own (the problem issue
#51 exists to close). All interpretation of what's required and which
vocabularies apply lives in opengwasdb; this script only reads a manifest and
reports what opengwasdb says about it.

Usage:

    python3 resources/generators/lib/schema_validate.py <analyses.tsv path>

Exits 0 with no output when the file conforms to the shared core schema.
Exits 1 with one violation per stdout line otherwise.
"""
from __future__ import annotations

import argparse
import sys

from opengwasdb.model.analyses import read_analyses, validate_analyses


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("analyses_path", help="Path to an emitted analyses.tsv")
    args = parser.parse_args(argv)

    table = read_analyses(args.analyses_path)
    errors = validate_analyses(table)
    for error in errors:
        print(error)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
