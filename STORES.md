# OpenGWASDB Store Releases

Generated master list of Store Releases in this registry.

> Generated from Release Bundles in `stores/`. Do not edit by hand; regenerate with `pixi run index`.

| Store ID | Label | Family | Layout | Completion | Status | Format | Analyses | Variants | Associations | Validated |
|:---|:---|:---|:---|:---|:---|:---|---:|---:|---:|:---|
| `OGS-00001` | pilot-10 | eqtlgen-cis-pilot | ragged | observed_only | built | 1.0 | 10 | 86,376 | 86,373 | passed |
| `OGS-00002` | pilot-10-completed | eqtlgen-cis-pilot | ragged | reference_completed | validated | 1.0 | 10 | 207,764 | 207,761 | passed |
| `OGS-00003` | r13-pilot-20 | finngen-r13 | dense | observed_only | built | 1.0 | 10 | 21,230,615 | 212,306,150 | passed |
| `OGS-00004` | eur-hybrid-pilot-10 | gwas-catalog-eur-hybrid | hybrid | observed_only | built | - | 10 | - | - | passed_with_warnings |
| `OGS-00005` | eur-hybrid-quant-pilot-10 | gwas-catalog-eur-hybrid | hybrid | observed_only | built | - | 10 | - | - | passed_with_warnings |
| `OGS-00006` | 2023-chen-full-european | metabolome-plasma-2023 | ragged | observed_only | built | - | 1,400 | - | - | passed_with_warnings |
| `OGS-00007` | 2018-sun-pilot-10 | pqtl-interval-2018 | ragged | observed_only | built | - | 10 | - | - | passed |

## Derived membership summaries

Every value below is derived from the Release Bundle's `analyses.tsv`; `NA` means the column is absent or has an empty value.

| Store ID | Author | Publication PMID | Tissue | Context | Population | Sample size | Download source |
|:---|:---|:---|:---|:---|:---|:---|:---|
| `OGS-00001` | NA | NA | whole_blood | NA | NA | NA | NA |
| `OGS-00002` | NA | NA | whole_blood | NA | NA | NA | NA |
| `OGS-00003` | NA | NA | NA | NA | EUR | 362216-500186 | https://storage.googleapis.com/finngen-public-data-r13/summary_stats/ |
| `OGS-00004` | mixed (8) | mixed (8) | NA | NA | EUR | 6753-482730 | http://ftp.ebi.ac.uk/pub/databases/gwas/summary_statistics/ |
| `OGS-00005` | mixed (8) | mixed (8) | NA | NA | EUR | 511-300447 | http://ftp.ebi.ac.uk/pub/databases/gwas/summary_statistics/ |
| `OGS-00006` | Chen Y | 36635386 | plasma | metabolomics | EUR | 3441-8299 | http://ftp.ebi.ac.uk/pub/databases/gwas/summary_statistics/ |
| `OGS-00007` | Sun BB | 29875488 | plasma | SomaScan | EUR | 3301 | http://ftp.ebi.ac.uk/pub/databases/gwas/summary_statistics/ |
