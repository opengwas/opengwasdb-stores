# Store Families

A Store Family is a stable product identity built from one Source Collection:
its query promise, access posture, release cadence, and build priority.

Under ADR 0022 a family is a **field on a Store Release**, not a path level.
`stores/OGS-00042/release.yaml` carries `family: finngen-r13`, resolved against
`families/finngen-r13/family.yaml`.

## Migration in progress

`families/<id>/releases/` still holds the thirteen bundles that have not yet
been migrated to flat Store Release ids -- superseded releases, `-resolved`
and `-completed-issue34` variants, full-ancestry siblings, and rebuild
bundles. They await triage: each is either migrated to `stores/`, or marked
`superseded`/`withdrawn` and retired.

Once that directory is empty, `family.yaml` flattens to `families/<id>.yaml`
and this directory becomes a plain lookup table.

Family-specific generator code has already moved to `generators/<family-id>/`.
