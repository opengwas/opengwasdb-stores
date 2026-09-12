#!/usr/bin/env Rscript
# Release-bundle-level smoke test for the reference-AF effect-scale
# validation stage (issues #16-#21). Run from the repository root:
#
#   Rscript tests/effect-scale-validation/run_tests.R
#
# This regenerates the deterministic fixtures, runs the generator's
# `--mode=effect-scale` stage against them exactly as a real Store Family
# would, and asserts on the emitted release-bundle outputs (sidecar,
# analyses.tsv, validation.yaml) rather than on internal helper functions.

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

fixtures_dir <- "tests/effect-scale-validation/fixtures"
release_dir <- file.path(fixtures_dir, "release")
config_path <- file.path(fixtures_dir, "config.yaml")

status <- system2("Rscript", c(shQuote(file.path(fixtures_dir, "generate_fixtures.R"))))
if (status != 0) fail("Fixture generation failed")

status <- system2("Rscript", c(
  "generators/lib/source-formats/gwas-ssf-ragged/generate.R",
  paste0("--config=", config_path), "--mode=effect-scale"
))
if (status != 0) fail("generate.R --mode=effect-scale exited non-zero")

sidecar <- fread(file.path(release_dir, "sidecars", "sd_estimation.tsv"), sep = "\t", na.strings = "")
setkey(sidecar, analysis_id)
analyses <- fread(file.path(release_dir, "analyses.tsv"), sep = "\t", na.strings = "")
setkey(analyses, analysis_id)
validation <- read_yaml(file.path(release_dir, "validation.yaml"))
release <- read_yaml(file.path(release_dir, "release.yaml"))

row <- function(id) as.list(sidecar[id])

# --- #19: implied phenotype-SD diagnostics for declared-standardised traits
r <- row("FIXT001")
check(r$status == "passed", "FIXT001 (implied SD ~1) expected passed, got %s", r$status)
check(abs(r$implied_sd_median - 1) < 1e-6, "FIXT001 implied_sd_median should be ~1, got %s", r$implied_sd_median)
check(r$original_sd_method == "declared_standardised",
      "FIXT001 declared_standardised must not be overwritten by QC, got %s", r$original_sd_method)

r <- row("FIXT002")
check(r$status == "failed", "FIXT002 (implied SD ~3) expected failed, got %s", r$status)

r <- row("FIXT010")
check(r$status == "warning", "FIXT010 (high dispersion) expected warning, got %s", r$status)
check(!is.na(r$sd_dispersion) && r$sd_dispersion > 0.5, "FIXT010 dispersion should exceed threshold")

# --- #18: allele-matching / overlap evidence
r <- row("FIXT003")
check(r$n_variants_considered == 16, "FIXT003 n_variants_considered should be 16, got %s", r$n_variants_considered)
check(r$n_variants_overlapping == 14, "FIXT003 n_variants_overlapping should be 14, got %s", r$n_variants_overlapping)
check(r$n_variants_excluded_ambiguous == 2, "FIXT003 ambiguous exclusions should be 2, got %s", r$n_variants_excluded_ambiguous)
check(r$n_variants_excluded_mismatch == 2, "FIXT003 mismatch exclusions should be 2, got %s", r$n_variants_excluded_mismatch)
check(r$n_variants_excluded_maf == 2, "FIXT003 MAF exclusions should be 2, got %s", r$n_variants_excluded_maf)
check(r$n_variants_retained == 8, "FIXT003 retained should be 8, got %s", r$n_variants_retained)
check(r$status == "passed", "FIXT003 expected passed overall, got %s", r$status)

# --- #20: AF source precedence + non-quantitative skip
r <- row("FIXT004")
check(r$status == "skipped" && r$skip_reason == "non_quantitative_effect_scale",
      "FIXT004 (log_or) should be skipped as non-quantitative")

r <- row("FIXT005")
check(r$af_source == "source", "FIXT005 with usable source AF should record af_source=source, got %s", r$af_source)
check(!nzchar(r$ancestry_reference_id %||% ""), "FIXT005 should not consult a reference resource")

r <- row("FIXT006")
check(r$af_source == "reference", "FIXT006 (empty AF column) should fall back to reference AF, got %s", r$af_source)

r <- row("FIXT007")
check(r$af_source == "reference", "FIXT007 (no AF column) should fall back to reference AF, got %s", r$af_source)

r <- row("FIXT008")
check(r$status == "skipped" && r$skip_reason == "no_reference_resource_for_ancestry",
      "FIXT008 (unmapped ancestry) should be skipped with no_reference_resource_for_ancestry")

r <- row("FIXT009")
check(r$status == "skipped" && r$skip_reason == "low_overlap",
      "FIXT009 (2 retained variants) should be skipped with low_overlap")

# --- estimation path populates original_sd for non-declared traits
r <- row("FIXT011")
check(r$original_sd_method == "estimated_from_reference_maf",
      "FIXT011 should be estimated_from_reference_maf, got %s", r$original_sd_method)
check(abs(r$original_sd - 1.7) < 1e-6, "FIXT011 original_sd should be ~1.7, got %s", r$original_sd)
check(abs(as.list(analyses["FIXT011"])$original_sd - 1.7) < 1e-6,
      "FIXT011 analyses.tsv original_sd should be updated to ~1.7")

r <- row("FIXT012")
check(r$original_sd_method == "estimated_from_source_maf",
      "FIXT012 should be estimated_from_source_maf, got %s", r$original_sd_method)
check(abs(r$original_sd - 2.1) < 1e-6, "FIXT012 original_sd should be ~2.1, got %s", r$original_sd)

# --- regression: alignment must be scoped per-chromosome, not just row 1's
r <- row("FIXT013")
check(r$n_variants_retained == 10,
      "FIXT013 (chr1 + chr2 rows) should retain all 10 variants, got %s", r$n_variants_retained)
check(r$status == "passed", "FIXT013 expected passed, got %s", r$status)

r <- row("FIXT014")
check(r$status == "passed", "FIXT014 beta fallback expected passed, got %s", r$status)
check(r$original_sd_method == "estimated_from_beta_distribution",
      "FIXT014 should reach estimated_from_beta_distribution, got %s", r$original_sd_method)
check(r$af_source == "beta_distribution", "FIXT014 should record beta_distribution evidence")

# --- #21: validation.yaml reflects empirical status, not vocab-only
check(validation$checks$effect_scale == "failed",
      "validation.yaml checks.effect_scale should be failed (FIXT002 present), got %s", validation$checks$effect_scale)
check(validation$checks$sd_estimation == "failed",
      "validation.yaml checks.sd_estimation should be failed, got %s", validation$checks$sd_estimation)
check(any(grepl("FIXT002", unlist(validation$warnings))), "validation.yaml warnings should mention FIXT002")
check(any(grepl("FIXT010", unlist(validation$warnings))), "validation.yaml warnings should mention FIXT010")
check(identical(release$sidecars$sd_estimation, "sidecars/sd_estimation.tsv"),
      "release.yaml should point at the sd_estimation sidecar")

cat(sprintf("ALL %d CHECKS PASSED\n", n_checks))
