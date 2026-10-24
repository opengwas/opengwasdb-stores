# eQTLGen cis (OGS-00027)

The full eQTLGen cis-eQTL Store: 19,250 Analyses, one per gene. The release config is
`release-full.yaml`; the connectors are in `connectors/` and are documented, with the
freeze, in `docs/spec/source-connectors.md` (the eQTLGen section).

## Inputs written before a release (never overwritten)

All live under `/data/opengwasdb/raw/eqtlgen/` unless noted; each has its lineage beside it.

| File | Written by | Holds |
|---|---|---|
| `2019-12-11-cis-eQTLsFDR-ProbeLevel-CohortInfoRemoved-BonferroniAdded.txt.gz` | download | the published cis-eQTL file (3,880,187,026 bytes, sha256 `1133a89f…6f814`) |
| `full-19250.sample-size.tsv.gz` (+ `.provenance.yaml`) | `providers.eqtlgen.sample_size_table` | per-pair N (`NrSamples`) aligned to the `full-19250` file set, grouped by probe in `.epi` order |
| `/data/opengwasdb/reference/ancestry-mixture/ref_eaf.united-kingdom.hg38.tsv.gz` (+ `.meta.yaml`) | `tools/reference-resources/ukb-ancestry-mixture-hg38-uk-eaf/build_uk_eaf.py` | the `--eaf-reference`: the UK Biobank "United Kingdom" frequencies, never an hgdp1kgp-hg38 panel (#348) |

The adapter reads the sample-size table for each Analysis's `sample_size` (the maximum
published pair N), so the Analysis N and the Store's `n` plane share one source.

## Resource requirements

- **`sample_size_table` is not bounded streaming.** It holds every published pair the file
  set could hold in memory to sort and join them: **7.75 GB peak RSS, about 11 minutes on
  one core** for `full-19250` (budget 10 GB). It refuses a damaged `.besd` before reading
  the published file.
- **The full build** (`build-ragged-besd` with the per-pair N and the `.esi` EAF) took 21.5
  minutes at **24.8 GB peak RSS** and wrote a 2.63 GB Store (check build, #329's branch).
- **The adapter** reads the table once: about 45 s, one core.
