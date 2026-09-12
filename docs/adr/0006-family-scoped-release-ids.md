# Family-scoped release IDs

Store Release identifiers are scoped to their Store Family and should use source-natural names such as `phase-1`, `r14`, `2026-07-05`, or `v0.1.0` depending on the family. The globally meaningful identity is the pair of Store Family ID and Family Release ID, avoiding a forced versioning scheme across one-off, periodic, snapshot, and internally versioned store families.

---

**Superseded by [0022](0022-flat-opaque-store-ids.md).** Release IDs are now globally unique opaque `OGS-` identifiers; Store Family is a field, not a path level.
