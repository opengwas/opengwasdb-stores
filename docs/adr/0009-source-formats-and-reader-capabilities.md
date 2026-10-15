# Source formats and reader capabilities

The registry distinguishes Source Collections from Source Formats and Source Reader Capabilities. Each Source Collection is homogeneous: all analyses in it share one Source Format and one Source Reader Capability, so Store Families and Store Releases do not need to repeat reader configuration.

---

**Amended by [0024](0024-one-family-record-no-source-collection-tier.md).** `source_format` is retired: `source_reader_capability` names the format, and a `null` capability means the builder reads the source directly.

**Amended by [0036](0036-manifest-generation-is-one-pipeline-fed-by-source-connectors.md).** A tabular Source Collection's reader capability is OpenGWASDB's generic tabular reader together with the collection's Column Map, carried in the Release Bundle; a dedicated reader remains only for a format a Column Map cannot describe, such as GWAS-VCF or BESD.
