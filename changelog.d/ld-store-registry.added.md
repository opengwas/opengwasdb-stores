- **The LD Store Registry records the served EUR LD Store as OGL-00001, and
  `pixi run index` generates `ld-resources.tsv` and an LD section of `STORES.md`
  from its records (issue #536, epic #496, ADR 0042).** An LD Store record,
  `stores/ld/OGL-NNNNN/record.yaml`, holds what a person writes (label, status,
  licence, use, how to rebuild it) and what
  `python3 -m ld_registry write <ld_id> <location>` reads from the Store through
  `opengwasdb ld info`: its provenance, that provenance's sha256, and a stat of
  its files. The writer keeps every field a person wrote, and refuses a rewrite
  that finds another Store, naming each field that differs. The index reads only
  the records, refuses one that breaks `docs/spec/ld-store-registry.md` or
  anything else under `stores/ld/`, naming it, and writes a missing value as an
  empty cell, never `0`; CI's clean-tree check fails when either file is stale.
  `ogstores.store_info.run_engine_json` is now the one runner for the engine's
  JSON commands.
