# Single-source store families

Each Store Family is built from one Source Collection, and all analyses in a Source Collection share one Source Format and one Source Reader Capability. A Source Collection may feed many Store Families, but a Store Family does not combine multiple Source Collections. Store Releases can vary the produced shape, such as dense versus ragged or observed-only versus reference-completed, without requiring cross-source configuration inside a single family.

---

**Amended by [0024](0024-one-family-record-no-source-collection-tier.md).** The Source Collection is a field on the family record, not a directory, so this rule now holds by construction.

**Superseded by [0028](0028-store-family-tier-retired.md).** The Store Family tier is retired; a Store Release is built from one Source Collection.
