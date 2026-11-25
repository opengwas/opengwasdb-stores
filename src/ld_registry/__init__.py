"""The LD Store Registry: one record per LD Store, and the index generated from them.

`stores/ld/OGL-NNNNN/record.yaml` records one LD Store; `ld-resources.tsv` and
the LD section of `STORES.md` are generated from the records alone. The spec is
docs/spec/ld-store-registry.md, the decision ADR 0042 (#536, epic #496).

This package may import from `ogstores`; `ogstores` never imports it.
"""

from pathlib import Path

#: The repository's root: `stores/ld/` and the tools are found from it.
REPO_ROOT = Path(__file__).resolve().parents[2]
