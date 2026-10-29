# Store registry boundary

This repository owns the Store Registry: store intentions, source manifests, build recipes, release lineage, and prioritisation. The OpenGWASDB store format implementation and low-level query engine remain outside this repository so this repo can coordinate many stores without becoming the storage engine itself.

**Amended by [0028](0028-store-family-tier-retired.md).** Prioritisation is no longer registry metadata: what should be built next is decided in the issue tracker, not recorded as a registry field. The registry records intentions, lineage, and recipes; it does not carry an ordered priority list.
