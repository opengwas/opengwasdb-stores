# One family record, and no Source Collection tier

Amends [0009](0009-source-formats-and-reader-capabilities.md) and [0010](0010-single-source-store-families.md).

Store Family records live in a single `resources/families.yaml`, one entry per family. The former `families/<id>/family.yaml` tree and the `source-collections/` tree are both folded into it, and `families/_candidates/` is retired.

A Store Family is a product identity, not a format. Three families in this registry are built from one Source Collection through one Source Format and one Source Reader Capability -- `gwas-catalog-eur-hybrid`, `metabolome-plasma-2023` and `pqtl-interval-2018` are all EBI GWAS Catalog GWAS-SSF -- and promise `full-gwas`, `signals_only` and `cis_and_signals` respectively. The format is what a builder sees; the family is what a query user sees, and a query user never learns the format. A family is also the unit of continuity across releases: `ukb-b` has four, and the promise has to survive all of them, which is why it cannot be a field restated on each one.

`source_format` is retired as a separate field. It and `source_reader_capability` were one fact written twice (`finngen-r13-tabular` / `opengwasdb.finngen-r13`), and only the capability is load-bearing because only it becomes an argument to an `opengwasdb` command. A `null` capability means the source is not read through the SourceReader registry at all -- BESD, which `build-ragged-besd` reads directly -- which the capability field expresses without a second field beside it.

The Source Collection stops being a directory and becomes a string. The tree recorded a provider, an access posture, a default licence, a format pair, an inventory and a status. The format pair is now one field; provider, posture and licence are inlined per family, and the licence is in any case already materialised per row in every `analyses.tsv`. The remaining two were inert: every collection declared `inventory: null` except one whose `inventory.tsv` was a header line with no rows, and all four declared `status: candidate` including those that had produced built Store Releases. No code ever read the directory -- each generator took `source_collection_id` from its own configuration and copied it into `release.yaml` as a string.

Repeating provider, licence and capability across the three GWAS Catalog families is accepted deliberately. Three short fields in one file are cheaper to hold in the head, and easier to grep for drift, than a directory tier existing to normalise them.

ADR 0010's rule that a Store Family is built from exactly one Source Collection now holds by construction rather than by inspection: the collection is a field on the family, so a family cannot name two.

A real Source Inventory would reopen this. When acquisition enumerates a collection at scale -- the GWAS Catalog download is 2.35 TB across 17,300 files -- the result cannot be a line in a YAML file. It returns as a data file, `resources/inventories/<collection-id>.tsv`, referenced from the family entry. That is a place to put rows, not a metadata tier, and it does not restore `source-collections/`.

A **Candidate Store Family** needs no home of its own. Its concrete expression is a candidate Store Release: `stores/<id>/` with `status: candidate`, which the Release Status vocabulary already carries. What is under consideration and what has been built then appear in one generated master list. A proposal with no candidate release attached is a roadmap item and belongs in an issue tracker; `families/_candidates/` held nothing but a README for the life of the repository.

---

**Amended by issue #134.** Monolithic multi-Analysis source collections read directly by the builder (`source_reader_capability: null`, such as BESD triples) record source identity and provenance at the release level via `source_snapshot` in `release.yaml` (for example `besd_prefix`), rather than per-row `source_file`, `checksum`, and `size_bytes` columns in `analyses.tsv`. `bundle.check()` verifies the non-empty presence of this snapshot metadata without inspecting external source artifacts.

