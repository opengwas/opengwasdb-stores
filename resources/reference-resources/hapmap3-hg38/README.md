# hapmap3-hg38

The maintained HapMap3 GRCh38 variant list used as the build input for the
default `hm3` Indexed Variant Subset (epic
[opengwas/opengwasdb-stores#214](https://github.com/opengwas/opengwasdb-stores/issues/214),
ticket [#208](https://github.com/opengwas/opengwasdb-stores/issues/208)).

It is a `kind: variant_list` Reference Resource: the large data lives outside
Git under the configured reference root, and `resource.yaml` records what it is,
where it came from, how many rows it has, and the checksums of the exact bytes.

```text
/data/opengwasdb/reference/hapmap3-hg38/
  hm3.grch38.alid.txt   1,206,903 canonical ALIDs; SHA-256 4a7c82aa…
  w_hm3.snplist.gz      unmodified LDSC source snapshot; MD5 153ecc2b…
```

Nothing in this directory is an artifact store: neither the 18.9 MB list nor the
5.2 MB source is tracked in the repository (ADR 0015).

## The list

One canonical ALID per nonblank line, in coordinate order:

```text
1:817186:A:G
1:817341:A:G
...
8_KI270821v1_alt:559250:G:T
```

`variant_id_convention` is the one `opengwasdb` canonicalises to: `chr:pos:a1:a2`
with `a1 = min(allele1, allele2)` alphabetically first and no `chr` prefix.
GRCh38 alt contigs (`7_KI270803v1_alt`, `8_KI270821v1_alt`) are canonical labels
and are included; the list is autosome-and-alt-contig only because HapMap3 is,
not because a filter here dropped sex chromosomes.

## Provenance and the reconciliation balance

The source is the canonical LDSC HapMap3 SNP list (`w_hm3.snplist.gz`, columns
`SNP A1 A2`, 1,217,311 rsIDs), mirrored from
`https://zenodo.org/api/records/7773502/files/w_hm3.snplist.gz/content` and
recorded by publisher MD5 `153ecc2bcfa740afafe656e6a384d769`.

The ALID list is the **allele-safe rsID resolution of that list against the
OGS-00009 GRCh38 Store Variant Table**, performed by the sibling benchmark
script `benchmarks/benchmark_indexed_subset.py` (`read_hapmap3` →
`resolve_hapmap3` → `write_alid_list`) at revision `a36ea78`, delivered by
opengwas/opengwasdb#269/#270. `opengwasdb build-indexed-subset --variant-list`
does **not** derive ALIDs from rsIDs; it accepts the resulting already-canonical
list. The list is therefore not a pure function of the source file: a variant
whose rsID has no allele-compatible row on that axis has no ALID here and is not
invented.

| step | count |
|---|---:|
| rsIDs requested | 1,217,311 |
| absent from the OGS-00009 axis | 10,385 |
| allele-incompatible with the axis | 23 |
| **resolved ALIDs (= maintained `row_count`)** | **1,206,903** |

`multiple_store_rows` = 2,743 counts rsIDs that resolved to an allele-compatible
row among several Store rows sharing the rsID (an rsID with more than one
allele-compatible row is refused by the resolver, not guessed); they are
resolved, not dropped, so the balance is
`1,217,311 − 10,385 − 23 = 1,206,903`. The reconciliation is recorded in
`resource.yaml:derivation` and `provenance.json`, transcribed from the
independent #267 benchmark
(`opengwasdb docs/benchmark-output/opengwasdb_267_indexed_subset_benchmark.json`,
`input.counts_reconciled`).

The maintained list is byte-for-byte the 1,206,903-row list benchmarked in
opengwas/opengwasdb#262/#267. The declared `alid_sha256` is over those exact
bytes; if the list ever deliberately changes, a new checksum and fresh
reconciliation counts must be recorded together.

## Verify a local copy

```sh
pixi run python tools/reference-resources/hapmap3-hg38/verify_hm3_resource.py
```

The verifier refuses loudly — naming the path and line number — when the list is
missing, malformed, duplicated, non-canonical, the wrong length, or does not
match the declared SHA-256, and when the declared source checksum or the rsID
reconciliation does not balance. It applies the pinned `opengwasdb`
`parse_canonical_alid` round-trip, so it reads the same canonical-ALID rule the
consumer does; `chr1:…`, `23:…`, lower-case alleles and unsorted allele pairs are
all non-canonical. It exits `0` with a one-line summary on success and `2` on
refusal.

Useful options:

```sh
# a copy held elsewhere, before moving it into place
... verify_hm3_resource.py --alid-file /path/to/hm3.grch38.alid.txt

# require the LDSC source snapshot to be present and matching too
... verify_hm3_resource.py --require-source
```

## Consumer contract for #209+

`opengwas/opengwasdb-stores#209` resolves this declaration and passes it to
`opengwasdb build-indexed-subset`:

```text
resource_id            hapmap3-hg38
kind                   variant_list
location               /data/opengwasdb/reference/hapmap3-hg38/hm3.grch38.alid.txt
genome_build           GRCh38   ->  --reference-assembly GRCh38
row_count              1206903
checksums.alid_sha256  4a7c82aa625a006ff437dcaa2769b00e056413d54c72e98f7378327c69513398
```

A plan declares the subset name `hm3` and reads the ALID path and reference
assembly from the resource rather than hard-coding either. A Store axis that
lacks some of these ALIDs is the ordinary "absent from the axis" case the subset
builder counts and records; it does not change this list.

Consuming this resource needs an `opengwasdb` runtime that provides
`build-indexed-subset` (opengwas/opengwasdb#264–#271). The repository's tracked
current-format runtime now provides it (stores#218; `opengwasdb 0.3.0`,
revision `4e92256`), so the supported release entry point needs no `PATH`
overlay. The entry point still probes for the command and refuses loudly when
the runtime does not provide it; see [docs/runtime.md](../../../docs/runtime.md).
