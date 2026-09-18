# materialise-gwas-ssf-ragged tests

Offline black-box tests for `resources/scripts/materialise-gwas-ssf-ragged.R`.

The fixture verifies the core source-materialisation interface:

- download from each accepted manifest row's `source_url`;
- apply the release's frozen `sidecars/sparse_regions.tsv` filter plan plus
  exact QC-panel positions;
- write only the canonical GWAS-SSF columns to `source_file`;
- atomically publish the filtered output and a sibling `.sha256` file;
- skip an output whose sibling checksum still matches;
- repair a corrupt output; and
- clean transient files and refuse publication on filtering failure.

The suite uses `file://` input and temporary directories, so it performs no
network access and never touches production artifacts.
