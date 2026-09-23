# gnomad-v4-common-hg38

Per-ancestry common-variant axes for the Dense Component of a Hybrid or Dense
Store Release. Each `<SUPERPOP>-variants.txt.gz` is a newline-delimited list of
canonical GRCh38 ALIDs — the plain-list form
`opengwasdb.variants.reference.read_variant_reference` detects by content — so a
build can be handed `--variant-reference` for the ancestry it is building and
skip Pass 1 (the variant union and liftover) entirely.

```sh
pixi run python resources/reference-resources/gnomad-v4-common-hg38/build_common_panel.py \
  --out-dir /data/opengwasdb/reference/gnomad-v4-common-hg38 \
  --min-maf 0.01 --jobs 12
```

## Why this exists

It replaces `alid-panel/EUR-variants.tsv.gz` as the axis for the full GWAS
Catalog EUR Hybrid release (opengwasdb-stores#155). That panel has two
limitations this one does not:

| | `alid-panel/EUR-variants.tsv.gz` | this resource (EUR) |
|---|---|---|
| variants | 9,847,807 | **13,415,936** |
| chromosomes | 1–22 | **1–22, X, Y, MT** |
| real MAF floor | **3.24%**, not 1% | 1% as declared |
| source | HGDP+1kGP, 772 EUR samples | gnomAD v4.1, 34,029 NFE genomes |

The old panel's floor was not the 1% it appeared to be: it was built with
`--mac 50` against 772 samples, and 1% of 772 samples is 15 allele copies. No
thresholding choice can make ~800 samples resolve a 1% variant — gnomAD's
76,215 genomes can.

Measured over a 24-source sample of the release's own inventory, **80.27%** of
autosomal source variants resolve against this EUR axis versus **78.11%** against
the old one.

## Outputs

| file | variants | composition |
|---|---|---|
| `AFR-variants.txt.gz` | 20,149,213 | `afr` |
| `AMR-variants.txt.gz` | 13,487,396 | `amr` |
| `EAS-variants.txt.gz` | 10,718,294 | `eas` |
| `EUR-variants.txt.gz` | 13,415,936 | `nfe` + `fin` |
| `MID-variants.txt.gz` | 14,133,778 | `mid` |
| `SAS-variants.txt.gz` | 12,266,874 | `sas` |
| `ALL-variants.txt.gz` | 27,447,322 | union of the above |
| `gnomad-groups/*.txt.gz` | 10.7–20.1 M each | the ten raw gnomAD groups |

**No `NAF` axis is emitted** — gnomAD v4.1 has no North African ancestry group.
The absence is recorded in `manifest.json` rather than left as a missing file.

`gnomad-groups/` keeps the ten raw gnomAD groups separate because the
super-population composition is a release decision that may change, and
re-deriving a group costs an hour of streaming while reading a list that already
exists costs nothing.

## How it is built

Nothing is mirrored. The gnomAD sites VCFs are ~550 GB only because of VEP
annotation this axis never reads, and they are bgzip+tabix indexed, so
`bcftools` streams them over HTTPS a 10 Mb region at a time (322 shards, ~52
min at 12 jobs) and each shard is reduced to ALIDs on the way past. Total output
is 957 MB, all of it inspectable with `zcat`.

Two guards, because a frequency alone is not evidence a population was actually
observed:

- **`--min-maf 0.01`** is applied *within one ancestry group*, not to a global
  frequency. A variant common in AFR and rare in EUR is on the AFR axis only.
- **`--min-an-fraction 0.5`** rejects a site whose group allele number is below
  half that shard's maximum for the group. The callable set varies by region and
  by ploidy (chrX non-PAR is haploid in males), so the cap is per-shard rather
  than a global constant that would reject correct sites at coverage edges.

### The mitochondrion is a deliberate simplification

gnomAD's chrM release is a different callset with a different model —
homoplasmy and heteroplasmy, `AF_hom` — and carries **no per-ancestry allele
frequency at all**. Its 425 variants above 1% are admitted to *every* ancestry's
axis on the global homoplasmic frequency. That is not a per-ancestry claim and
`manifest.json` says so.

### Canonical labels are `X`, `Y`, `MT`

The axis emits the labels `opengwasdb.variants.normalise` canonicalises to
(ADR 0052). `MT` is the one that bites: `normalise_chromosome` maps `M`, `MT`,
`25` and `26` all *to* `MT`, so an axis spelling the mitochondrion `M` resolves
nothing. This build shipped that bug for one revision — 425 dead rows out of
13.4 M — which is why `write_alids` now asserts every emitted label against the
closed set `{X, Y, MT}` before writing:

```
refusing to write EUR-variants.txt.gz: non-canonical chromosome label(s) ['M'];
  expected autosomes or ['MT', 'X', 'Y']
```

A spelling no reader can resolve now fails loudly instead of producing an axis
whose rows are silently dead.

## Status: the aliases are not in a released opengwasdb yet

The axis carries the canonical labels, but `normalise_chromosome` in the *pinned*
revision does not yet alias the numeric PLINK encodings, so a source spelling the
chromosome `23` does not resolve against it:

| source spelling | variants | resolves on the pinned rev | resolves once ADR 0052 lands |
|---|---|---|---|
| `23` | 57,796 | **0.00%** | 99.40% |
| `23`,`25` | 282,191 | **0.00%** | 76.50% |
| `25`,`23` | 15,559 | **0.00%** | 8.34% |
| `25`,`23`,`24` | 659,461 | **0.00%** | 44.68% |

Over a stratified 384-file sample of the release's 3,262 sources, 18.0% carry
X-chromosome rows and the spelling is near evenly split between `23` and `X`.

The canonicalisation is **ADR 0052**, in development as
[opengwas/opengwasdb#216](https://github.com/opengwas/opengwasdb/issues/216) on
`feature/216-chromosome-aliases`. Completing it needs no change to this axis or
to the bundle that points at it — the rows simply start resolving, and X/Y/MT
variants move from the Ragged Overflow into the Dense Component. The consequence
to record is that a store built before ADR 0052 and one built after are not the
same store, so the pinned revision belongs in the build evidence either way.

**The autosomal rows are unaffected and usable now.**

## Verification

```sh
pixi run python resources/reference-resources/gnomad-v4-common-hg38/verify_axis_coverage.py \
  --inventory resources/inventories/gwas-catalog-ssf-eur-hybrid-2026-09-22.tsv \
  --axis /data/opengwasdb/reference/gnomad-v4-common-hg38/EUR-variants.txt.gz \
  --axis /data/opengwasdb/reference/alid-panel/EUR-variants.tsv.gz \
  --sample 40
```

This resource is an **axis**, not a frequency table and not an LD panel. It says
which variants a matrix has rows for, not how common they are and not how they
covary. Reference completion is unaffected and still needs an LD panel matched
to the Analysis's Assigned Ancestry; this axis adds no X-chromosome LD blocks,
so X associations remain observed-only.
