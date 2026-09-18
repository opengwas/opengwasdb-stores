# Source formats and reader capabilities

The registry distinguishes Source Collections from Source Formats and Source Reader Capabilities. Each Source Collection is homogeneous: all analyses in it share one Source Format and one Source Reader Capability, so Store Families and Store Releases do not need to repeat reader configuration.

---

**Amended by [0024](0024-one-family-record-no-source-collection-tier.md).** `source_format` is retired: `source_reader_capability` names the format, and a `null` capability means the builder reads the source directly.
