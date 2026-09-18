#!/usr/bin/env Rscript
# Release-bundle-level smoke test for unconditional QC-panel retention
# (issue #28/#30). Run from the repository root:
#
#   Rscript tests/qc-panel-retention/run_tests.R
#
# Proves two things against the same zero-significant-hit fixture Analysis:
#   1. With `filter.qc_panel` enabled, the 15 QC-panel positions are retained
#      even though none of them are significant/suggestive, and none of the
#      15 non-panel "null" positions are retained.
#   2. With `filter.qc_panel` absent (the family's prior behaviour), nothing
#      is retained at all -- QC-panel retention is additive/opt-in, not a
#      change to existing behaviour.

suppressPackageStartupMessages({
  library(data.table)
  library(yaml)
})

fail <- function(...) stop(sprintf(...), call. = FALSE)
n_checks <- 0L
check <- function(cond, ...) {
  n_checks <<- n_checks + 1L
  if (!isTRUE(cond)) fail(...)
}

fixtures_dir <- "tests/qc-panel-retention/fixtures"
output_with <- file.path(fixtures_dir, "output-with-panel")
output_without <- file.path(fixtures_dir, "output-without-panel")
unlink(output_with, recursive = TRUE)
unlink(output_without, recursive = TRUE)

status <- system2("Rscript", c(shQuote(file.path(fixtures_dir, "generate_fixtures.R"))))
if (status != 0) fail("Fixture generation failed")

run_release <- function(config_name) {
  cfg <- read_yaml(file.path(fixtures_dir, config_name))
  cfg$source$ftp_base <- paste0("file://", normalizePath(file.path(fixtures_dir, "source")))
  tmp_config <- tempfile(fileext = ".yaml")
  writeLines(as.yaml(cfg), tmp_config)
  run_mode <- function(mode) {
    status <- system2(
      "Rscript",
      c("resources/generators/lib/source-formats/gwas-ssf-ragged/generate.R",
        paste0("--config=", tmp_config), paste0("--mode=", mode))
    )
    if (status != 0) fail("generate.R --mode=%s exited non-zero for %s", mode, config_name)
  }
  run_mode("emit")
  run_mode("validate")
  run_mode("filter")
}

run_release("config-with-panel.yaml")
run_release("config-without-panel.yaml")

# --- With the QC panel enabled: exactly the 15 panel positions retained ---
regions_with <- fread(file.path(output_with, "sidecars", "sparse_regions.tsv"), sep = "\t", na.strings = "")
analyses_with <- fread(file.path(output_with, "analyses.tsv"), sep = "\t", na.strings = "")
check(all(analyses_with$assigned_ancestry == "EUR"),
      "assigned_ancestry should be the super-population code EUR, not the source label European")
check(all(is.na(analyses_with$n_cases)) && all(is.na(analyses_with$n_controls)),
      "a non-case-control Analysis must leave n_cases/n_controls blank, not 0")
filtered_with <- fread(file.path(output_with, "filtered", analyses_with$filtered_file[1]), sep = "\t")
summary_with <- fread(file.path(output_with, "sidecars", "filter_summary.tsv"), sep = "\t", na.strings = "")

check(nrow(regions_with[region_kind == "cis"]) == 0, "no cis regions expected (no gene target)")
check(nrow(regions_with[region_kind %in% c("significant_trans", "suggestive_trans")]) == 0,
      "no signal-driven regions expected (zero significant/suggestive hits)")
qc_regions <- regions_with[region_kind == "qc_panel"]
check(nrow(qc_regions) == 1, "expected exactly one qc_panel region row (single chromosome), got %d", nrow(qc_regions))
check(qc_regions$n_variants_retained[1] == 15, "expected 15 qc_panel variants retained, got %d", qc_regions$n_variants_retained[1])
check(qc_regions$chromosome[1] == "1", "qc_panel region should be on chromosome 1")

check(nrow(filtered_with) == 15, "expected exactly 15 retained rows (the QC panel), got %d", nrow(filtered_with))
check(all(filtered_with$base_pair_location >= 10000000 & filtered_with$base_pair_location <= 10000000 + 14 * 200000),
      "every retained row should be a QC-panel position, not a null position")
check(all(filtered_with$p_value == 0.4), "QC-panel rows should be retained despite p_value=0.4 (not significant)")

check(summary_with$qc_panel_rows[1] == 15, "filter_summary.qc_panel_rows should be 15, got %d", summary_with$qc_panel_rows[1])
check(summary_with$retained_rows[1] == 15, "filter_summary.retained_rows should be 15, got %d", summary_with$retained_rows[1])

build_with <- read_yaml(file.path(output_with, "build.yaml"))
check(identical(build_with$qc_panel$enabled, TRUE), "build.yaml qc_panel.enabled should be TRUE")
check(identical(build_with$qc_panel$resource_id, "qc-panel-fixture"), "build.yaml qc_panel.resource_id should round-trip")
check(any(vapply(build_with$reference_resources, function(r) identical(r$kind, "qc_panel"), logical(1))),
      "build.yaml reference_resources should include the qc_panel entry")

# --- Without the QC panel configured: nothing retained (prior behaviour) ---
regions_without <- fread(file.path(output_without, "sidecars", "sparse_regions.tsv"), sep = "\t", na.strings = "")
analyses_without <- fread(file.path(output_without, "analyses.tsv"), sep = "\t", na.strings = "")
filtered_without <- fread(file.path(output_without, "filtered", analyses_without$filtered_file[1]), sep = "\t")
summary_without <- fread(file.path(output_without, "sidecars", "filter_summary.tsv"), sep = "\t", na.strings = "")

check(nrow(regions_without) == 0, "expected zero sparse regions with no signal and no QC panel configured")
check(nrow(filtered_without) == 0, "expected zero retained rows with no signal and no QC panel configured")
check(summary_without$qc_panel_rows[1] == 0, "filter_summary.qc_panel_rows should be 0 when not configured")
check(summary_without$retained_rows[1] == 0, "filter_summary.retained_rows should be 0 when not configured")

build_without <- read_yaml(file.path(output_without, "build.yaml"))
check(is.null(build_without$qc_panel), "build.yaml qc_panel should be absent when not configured")

# --- Source Ancestry Label -> super-population mapping (issue #133) ---
source("resources/generators/lib/ancestry.R")
ancestry_map <- read_source_label_map(normalizePath("."))
check(
  identical(
    unname(ancestry_map[c("European", "African", "East Asian", "South Asian")]),
    c("EUR", "AFR", "EAS", "SAS")
  ),
  "the tracked source-label map should translate common labels to super-population codes"
)
check(
  inherits(
    try(normalise_assigned_ancestry("Asian (unspecified)", ancestry_map), silent = TRUE),
    "try-error"
  ),
  "an unmapped source label must fail loudly rather than pass through as an Assigned Ancestry"
)

cat(sprintf("ALL %d CHECKS PASSED\n", n_checks))
