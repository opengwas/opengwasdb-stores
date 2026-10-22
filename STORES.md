# OpenGWASDB Store Releases

Generated master list of Store Releases in this registry.

> Generated from Release Bundles in `stores/`. Do not edit by hand; regenerate with `pixi run index`.

| Store ID | Label | Layout | Completion | Status | Format | Analyses (admitted) | Excluded | Variants | Associations | Validated |
|:---|:---|:---|:---|:---|:---|:---|---:|---:|---:|:---|
| `OGS-00001` | pilot-10 | ragged | observed_only | built | 0.1.0 | 10 | 0 | 86,376 | 86,373 | passed |
| `OGS-00002` | pilot-10-completed | ragged | reference_completed | validated | 0.1.0 | 10 | 0 | 207,764 | 207,761 | passed |
| `OGS-00003` | r13-pilot-20 | dense | observed_only | built | 0.1.0 | 10 | 0 | 21,230,615 | 212,306,150 | passed |
| `OGS-00004` | eur-hybrid-pilot-10 | hybrid | observed_only | built | - | 9 | 1 | - | - | passed_with_warnings |
| `OGS-00005` | eur-hybrid-quant-pilot-10 | hybrid | observed_only | validated | - | 10 | 0 | 14,763,864 | - | passed_with_warnings |
| `OGS-00006` | 2023-chen-full-european | ragged | observed_only | validated | - | 1,400 | 0 | 12,086,463 | 58,054,212 | passed_with_warnings |
| `OGS-00007` | 2018-sun-pilot-10 | ragged | observed_only | built | - | 10 | 0 | - | - | passed |
| `OGS-00008` | ukb-b-pilot-10 | dense | observed_only | candidate | - | 10 | 0 | - | - | - |
| `OGS-00009` | ukb-b-full | dense | reference_completed | accepted | - | 2,514 | 0 | - | - | passed_with_warnings |
| `OGS-00011` | eur-hybrid-full | hybrid | observed_only | accepted | - | 3,169 | 1,614 | - | - | passed_with_warnings |
| `OGS-00012` | mvp-phs002453-eur | dense | observed_only (completion pending) | validated | - | 1,669 | 0 | 20,137,907 | 33,610,166,783 | passed |
| `OGS-00013` | mvp-phs002453-afr | dense | observed_only | validated | - | 1,368 | 0 | 36,964,976 | 50,568,087,168 | passed |
| `OGS-00014` | mvp-phs002453-amr | dense | observed_only | validated | - | 1,053 | 0 | 27,860,266 | 29,336,860,098 | passed |
| `OGS-00015` | mvp-phs002453-eas | dense | observed_only | validated | - | 241 | 0 | 9,335,909 | 2,249,954,069 | passed |
| `OGS-00016` | mvp-phs002453-meta-observed-only | dense | observed_only | validated | - | 1,691 | 0 | 27,929,380 | 47,228,581,580 | passed |
| `OGS-00017` | r13-full | dense | reference_completed | accepted | - | 2,754 | 0 | - | - | passed |
| `OGS-00022` | eur-hybrid-pilot-10-completed | hybrid | reference_completed | validated | - | 9 | 1 | - | - | passed |
| `OGS-00023` | eur-hybrid-quant-pilot-10-completed | hybrid | reference_completed | validated | - | 10 | 0 | - | - | passed |
| `OGS-00024` | 2023-chen-full-african | ragged | observed_only | built | - | 1,034 | 0 | - | - | passed_with_warnings |
| `OGS-00025` | 2023-chen-full-east-asian | ragged | observed_only | built | - | 1,038 | 0 | - | - | passed |
| `OGS-00026` | 2023-chen-full-south-asian | ragged | observed_only | built | - | 971 | 0 | - | - | passed_with_warnings |

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
| `OGS-00008` | Ben Elsworth | NA | NA | NA | EUR | 352094-462933 | NA |
| `OGS-00009` | Ben Elsworth | NA | NA | NA | EUR | 10339-463010 | NA |
| `OGS-00011` | mixed (662) | mixed (758) | NA | NA | NA | 15-2586691 | https://ftp.ebi.ac.uk/pub/databases/gwas/summary_statistics/ |
| `OGS-00012` | NA | NA | NA | NA | EUR | 5266-451195 | NA |
| `OGS-00013` | NA | NA | NA | NA | AFR | 3435-121774 | NA |
| `OGS-00014` | NA | NA | NA | NA | NA | 1454-59807 | NA |
| `OGS-00015` | NA | NA | NA | NA | EAS | 2291-6665 | NA |
| `OGS-00016` | NA | NA | NA | NA | NA | 5954-636153 | NA |
| `OGS-00017` | NA | NA | NA | NA | NA | 68562-500186 | https://storage.googleapis.com/finngen-public-data-r13/summary_stats/ |
| `OGS-00022` | mixed (8) | mixed (8) | NA | NA | EUR | 6753-482730 | http://ftp.ebi.ac.uk/pub/databases/gwas/summary_statistics/ |
| `OGS-00023` | mixed (8) | mixed (8) | NA | NA | EUR | 511-300447 | http://ftp.ebi.ac.uk/pub/databases/gwas/summary_statistics/ |
| `OGS-00024` | Chen Y | 36635386 | plasma | metabolomics | AFR | 30-60 | http://ftp.ebi.ac.uk/pub/databases/gwas/summary_statistics/ |
| `OGS-00025` | Chen Y | 36635386 | plasma | metabolomics | EAS | 51-104 | http://ftp.ebi.ac.uk/pub/databases/gwas/summary_statistics/ |
| `OGS-00026` | Chen Y | 36635386 | plasma | metabolomics | SAS | 56-108 | http://ftp.ebi.ac.uk/pub/databases/gwas/summary_statistics/ |

## Tolerated gaps

These bundles pass the Release Bundle gate only because a named exemption tolerates known-missing required Analysis values. The values are genuinely unavailable and left blank rather than fabricated (issue #134); the tolerance is the visible remainder, not a clean pass.

| Store ID | Tolerated gap |
|:---|:---|
| `OGS-00001` | 70 blank required Analysis values (tolerated under #134) |
| `OGS-00002` | 70 blank required Analysis values (tolerated under #134) |
| `OGS-00011` | 2972 non-CURIE trait_ontology_id values (tolerated under #286) |
