# QC panel (hg38)

A fixed, reproducible list of variants used to give AF-based ancestry
assignment (issue #23/#25) and reference-AF effect-scale validation
(issue #16) a stable variant backbone, independent of how many
genome-wide-significant hits a given Analysis happens to have (issue #28).

`qc_panel.tsv` (10,000 rows, ~450 KB) is tracked directly in this repository
— see `resource.yaml` for the Reference Resource declaration Store Families
reference via `filter.qc_panel.resource_id` (issue #30).

## Selection procedure

Run by `build_qc_panel.py` against `ukb-ancestry-mixture-hg38`:

```sh
pixi run python resources/reference-resources/qc-panel-hg38/build_qc_panel.py \
  --out=resources/reference-resources/qc-panel-hg38/qc_panel.tsv
```

1. Average `ukb-ancestry-mixture-hg38`'s 21 fine-group frequencies into one
   frequency per super-population (AFR, AMR, EAS, EUR, MID, NAF, SAS).
2. Keep a variant only if its minor allele frequency is at least
   `--freq-floor` (default `0.05`) in **every** super-population — informative
   everywhere, not just on average.
3. Walk survivors in genome order and greedily keep at most one variant per
   `--min-spacing-bp` (default `250,000`) window, per chromosome, so kept
   variants are not clustered by LD.
4. If more than `--target-size` (default `10,000`) variants survive, evenly
   subsample down to the target so panel size stays stable across
   reference-panel updates that only add density.

Default parameters (chosen 2026-08-03) selected 4,088,118 of 5,810,529
reference variants at the frequency floor, 10,660 after spacing pruning, then
thinned to exactly 10,000. Chromosome coverage is proportional to chromosome
length (836 variants on chr1 down to 128 on chr21). The script prints a
sanity check confirming every selected variant clears the frequency floor in
every super-population; re-running it should reproduce this exactly, since
selection is deterministic given the same reference file and parameters.

Re-run with the same arguments (bumping `resource.yaml`'s `version` if the
selected variant set changes materially) whenever
`ukb-ancestry-mixture-hg38` is rebuilt.

## Panel columns

| Column | Description |
|---|---|
| `alid` | Canonical variant ID, `chr:pos:A1:A2` with `A1 = min(effect_allele, other_allele)`, matching `ukb-ancestry-mixture-hg38`'s convention. |
| `chromosome` | GRCh38 chromosome (autosomes 1-22 only by default). |
| `position` | GRCh38 base-pair position. |
| `effect_allele` / `other_allele` | Alleles as oriented in `ukb-ancestry-mixture-hg38`. |
| `min_superpop_maf` | The variant's minor allele frequency in whichever super-population it is rarest in — always `>= --freq-floor` by construction. |

## Out of scope

This panel only selects *which* variants to retain; whether/how a Store
Family's filter step retains them in a given release's filtered files is
issue #30, not this script.
