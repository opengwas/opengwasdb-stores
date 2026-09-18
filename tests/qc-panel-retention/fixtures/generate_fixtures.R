#!/usr/bin/env Rscript
# Deterministically (re)generates fixtures for
# tests/qc-panel-retention/run_tests.R (issue #28/#30).
#
# Run from the repository root:
#   Rscript tests/qc-panel-retention/fixtures/generate_fixtures.R
#
# Builds a single no-target Analysis (issue #26) with ZERO significant or
# suggestive hits, so the existing signal-driven sparse-region policy alone
# would retain nothing at all for it. 15 of its rows sit at positions that
# are also in a small fixture QC panel; another 15 "null" rows sit elsewhere
# and are in neither the panel nor any signal-driven region. Proves that
# enabling `filter.qc_panel` retains exactly the 15 panel positions
# regardless of significance, and that a Store Family which does not
# configure it keeps retaining nothing, as before this issue.

suppressPackageStartupMessages(library(data.table))

fixtures_dir <- "tests/qc-panel-retention/fixtures"
source_file <- file.path(
  fixtures_dir, "source", "GCST900001-GCST901000", "GCST900001", "harmonised", "GCST900001.h.tsv.gz"
)
dir.create(dirname(source_file), recursive = TRUE, showWarnings = FALSE)

# 15 rows the QC panel covers, all well above genome-wide significance
# thresholds (p_value = 0.4) so only qc_panel retention -- not the
# significant/suggestive regions -- could possibly keep them.
panel_positions <- seq(10000000, 10000000 + 14 * 200000, by = 200000)
panel_rows <- data.table(
  chromosome = "1", base_pair_location = panel_positions,
  effect_allele = "A", other_allele = "G", beta = 0.02, standard_error = 0.05,
  p_value = 0.4, effect_allele_frequency = "", variant_id = "", rsid = ""
)

# 15 null rows: neither significant/suggestive nor in the QC panel.
null_positions <- seq(80000000, 80000000 + 14 * 200000, by = 200000)
null_rows <- data.table(
  chromosome = "1", base_pair_location = null_positions,
  effect_allele = "A", other_allele = "G", beta = 0.01, standard_error = 0.05,
  p_value = 0.6, effect_allele_frequency = "", variant_id = "", rsid = ""
)

full <- rbindlist(list(panel_rows, null_rows))
fwrite(full, source_file, sep = "\t")

candidates <- data.table(
  STUDY.ACCESSION = "GCST900001", PUBMED.ID = 99999999L, FIRST.AUTHOR = "Fixture",
  STUDY = "QC panel retention fixture", DISEASE.TRAIT = "Fixture metabolite level",
  MAPPED_TRAIT = "fixture metabolite measurement", ancestry_group = "European",
  ancestry_fraction = 1, is_molecular = TRUE, molecular_subtype = "metabolomics",
  store_type = "ragged", store_key = "ragged__pmid-99999999__European",
  molecular_type = "metabolomics", study_design = "quantitative",
  # Deliberately 0, not NA: the candidates table represents "not a
  # case-control study" as zero counts, and the generator must emit blank
  # Assigned Metadata rather than a fabricated zero (issue #133).
  n_cases = 0L, n_controls = 0L, sample_size = 5000L,
  n_variants = nrow(full), association_count = nrow(full[p_value <= 5e-8]),
  MAPPED_TRAIT_URI = "http://purl.obolibrary.org/obo/fixture_0000001"
)
fwrite(candidates, file.path(fixtures_dir, "candidates.tsv"), sep = "\t", na = "")

# Fixture QC panel: exactly the 15 "panel" positions above, distinct from the
# real resources/reference-resources/qc-panel-hg38/qc_panel.tsv (this is a tiny
# deterministic panel for testing the wiring, not real biology).
qc_panel <- data.table(
  alid = sprintf("1:%d:A:G", panel_positions),
  chromosome = "1", position = panel_positions,
  effect_allele = "A", other_allele = "G", min_superpop_maf = 0.2
)
fwrite(qc_panel, file.path(fixtures_dir, "qc_panel.tsv"), sep = "\t", na = "")

cat(sprintf(
  "Wrote %d source rows (%d panel-position, %d null), 1 candidate, and a %d-row fixture QC panel\n",
  nrow(full), nrow(panel_rows), nrow(null_rows), nrow(qc_panel)
))
