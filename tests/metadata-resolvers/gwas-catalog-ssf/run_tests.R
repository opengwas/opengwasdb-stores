#!/usr/bin/env Rscript
# Unit-level smoke test for the `gwas-catalog-ssf` metadata resolver (issue
# #48). Run from the repository root:
#
#   Rscript tests/metadata-resolvers/gwas-catalog-ssf/run_tests.R
#
# Exercises resolve_gwas_catalog_ssf_metadata() directly against real GWAS
# Catalog "INITIAL SAMPLE SIZE" strings (observed in
# resources/data/gwas-catalog-v1.0.3.1-studies-r2026-07-10.tsv, not hand
# invented) rather than the raw ~120MB studies table itself, which is not
# checked into this repository and is not available in CI.

suppressPackageStartupMessages(library(data.table))

source("generators/lib/metadata_resolvers/gwas_catalog_ssf.R")

fail <- function(...) stop(sprintf(...), call. = FALSE)
n_checks <- 0L
check <- function(cond, ...) {
  n_checks <<- n_checks + 1L
  if (!isTRUE(cond)) fail(...)
}

# --- Quantitative study (GCST90040727-style): no case/control labels.
r <- resolve_gwas_catalog_ssf_metadata("1,000 European ancestry individuals")
check(r$resolution_status == "resolved", "quantitative: expected resolved, got %s", r$resolution_status)
check(r$stored_effect_scale == "sd", "quantitative: expected stored_effect_scale=sd, got %s", r$stored_effect_scale)
check(r$sample_size_kind == "total", "quantitative: expected sample_size_kind=total, got %s", r$sample_size_kind)
check(r$sample_size == 1000, "quantitative: expected sample_size=1000, got %s", r$sample_size)
check(is.na(r$n_cases) && is.na(r$n_controls), "quantitative: n_cases/n_controls should be NA, got %s/%s", r$n_cases, r$n_controls)

# --- Case-control study: simple "cases, controls" shape.
r <- resolve_gwas_catalog_ssf_metadata("10,007 cases, 474,591 controls")
check(r$resolution_status == "resolved", "case-control: expected resolved, got %s", r$resolution_status)
check(r$stored_effect_scale == "log_or", "case-control: expected stored_effect_scale=log_or, got %s", r$stored_effect_scale)
check(r$sample_size_kind == "case_control", "case-control: expected sample_size_kind=case_control, got %s", r$sample_size_kind)
check(r$sample_size == 484598, "case-control: expected sample_size=484598, got %s", r$sample_size)
check(r$n_cases == 10007, "case-control: expected n_cases=10007, got %s", r$n_cases)
check(r$n_controls == 474591, "case-control: expected n_controls=474591, got %s", r$n_controls)

# --- Case-control study: multi-ancestry compound shape, several
# cases/controls pairs summed into one total (this is the first time this
# resolver's summation-across-ancestry-groups branch is exercised at all).
r <- resolve_gwas_catalog_ssf_metadata(paste0(
  "10,006 African American or Afro-Caribbean cases, ",
  "98,788 African American or Afro-Caribbean controls, ",
  "3,941 Hispanic or Latin American cases, ",
  "50,015 Hispanic or Latin American controls, ",
  "223 East Asian ancestry cases, 6,250 East Asian ancestry controls, ",
  "30,059 European ancestry cases, 381,553 European ancestry controls"
))
check(r$resolution_status == "resolved", "compound case-control: expected resolved, got %s", r$resolution_status)
check(r$stored_effect_scale == "log_or", "compound case-control: expected stored_effect_scale=log_or, got %s", r$stored_effect_scale)
check(r$n_cases == 10006 + 3941 + 223 + 30059, "compound case-control: n_cases sum mismatch, got %s", r$n_cases)
check(r$n_controls == 98788 + 50015 + 6250 + 381553, "compound case-control: n_controls sum mismatch, got %s", r$n_controls)

# --- Unresolvable: an empty INITIAL SAMPLE SIZE field (a real occurrence in
# the GWAS Catalog studies table, not a hand-invented edge case).
r <- resolve_gwas_catalog_ssf_metadata("")
check(r$resolution_status == "unresolved", "empty: expected unresolved, got %s", r$resolution_status)
check(!is.na(r$resolution_notes) && nzchar(r$resolution_notes), "empty: resolution_notes should explain the failure")
check(is.na(r$stored_effect_scale) && is.na(r$sample_size_kind) && is.na(r$sample_size),
      "empty: resolved fields should all be NA, never a silent default")
check(is.na(r$n_cases) && is.na(r$n_controls), "empty: n_cases/n_controls should be NA")

# --- Defensive: a genuinely missing (NA) field, distinct from an empty
# string, must resolve the same way (never error, never default).
r <- resolve_gwas_catalog_ssf_metadata(NA_character_)
check(r$resolution_status == "unresolved", "NA input: expected unresolved, got %s", r$resolution_status)

cat(sprintf("ALL %d CHECKS PASSED\n", n_checks))
