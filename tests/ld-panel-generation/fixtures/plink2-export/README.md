# A recorded PLINK 2 additive export (EUR chr22)

`eur-chr22-22943803-22946000.raw` and `.pvar` are a **verbatim** `plink2
--export A` export, recorded from the production HGDP+1kGP EUR genotypes, so a
test can assert what the column header really names instead of what the
constructor assumed it names (#348).

How it was recorded, on the production host on 2026-11-01, by the #348
exploration (`notes/348-panels.md` in the epic run, script `tmp/348-explore/blk.py`):

```sh
plink2 --pfile <hgdp1kgp-hg38>/pgen/EUR/chr22 --keep <EUR.keep> \
       --chr 22 --from-bp 22943803 --to-bp 22946000 \
       --make-pgen --out blk
plink2 --pfile blk --export A --out blk
```

PLINK v2.0.0-a.6.9LM, 771 samples, 4 variants. The export's own log is quoted in
the exploration's `blk.log`; nothing here was edited, sorted or reformatted.

The property the fixtures exist for: each raw column is named
`<ID>_<counted allele>`, and this plink2 counts **REF**, not ALT. Column
`22:22943803:G:A_G` therefore holds the REF (G) dosage, and `22:22943875:A:G_A`
holds the REF (A) dosage. `tests/ld-panel-generation/run_tests.py` recomputes the
effect-allele frequencies and the LD between the variants by hand from these
dosages and asserts the literals.

**Sanitised identifiers (#348 review).** The recording's first two columns are the
production cohort's sample identifiers (`HGDP…`, `1kGP…`), and a fixture in this
repository needs none of them. They were replaced in place with synthetic ones,
`S0001`…`S0771`, for both FID and IID; the header line, every other column and
every dosage are byte-identical to the recording, which is what the tests assert
on. Nothing else was edited.

Recorded and sanitised files (md5): `.raw` `716c20defcb28416d1e5b92aa49a617e`
(the recording, before sanitisation, was `957afb45240ac1037af770a4ad089c6f`),
`.pvar` `8c190de046962beaddf86f7ee7f097d8`.
