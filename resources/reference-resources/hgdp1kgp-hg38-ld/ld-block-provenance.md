# LD block source audit

The three recoverable GRCh38 block definitions disagree and none matches the
1,357 EUR blocks declared by the production `gpm-hg38-eur-ld` resource:

| Source | EUR rows | SHA-256 |
|---|---:|---|
| `MRCIEU/ukb-rap-utils` blob `727a55a` (`ld_regions_hg38.tsv`) | 1,336 | `7741b85dce047d50cde0065562653444b5937d02622ea77f4df885c9c3a662fd` |
| `MRCIEU/genotype-phenotype-map` blob `be73d8d` (`ld_regions_hg38_updated.tsv`) | 1,353 | `1655decc38cbaa56b14872fdc1582b7726662e803ca9569f3847133005626754` |
| `MRCIEU/genotype-phenotype-map` blob `98950de` (`ld_regions_hg38_fulllength.tsv`) | 1,397 | `539b8c4f2ac14404259cacf053fe956e4e84e441dd32a185a9a50059fe37bdb8` |

The production declaration is the calibration contract. The canonical
`ld_regions_hg38.tsv` therefore retains the recoverable ancestry-specific
AFR/EAS/SAS rows but replaces EUR with boundaries reconstructed from the 1,357
real block basenames under `/data/opengwasdb/reference/ukb-hg38/EUR`. Its
SHA-256 is `0763afc708cd665d65aa6bae7e01e2cf68f34e5aa84d4a69897639b010727f56`.
No synthetic edge blocks were manufactured.
