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

source("src/manifest_generation/providers/gwas_catalog/gwas_catalog_ssf.R")

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

# --- Numberless "cases"/"controls" bits (#557). GWAS Catalog sometimes writes the
# count apart from the word: "181,522 European ancestry, unknown cases". The
# count is then the number in the nearest preceding bit that carries one, and
# that bit is not also counted into sample_size. Strings are copied from the
# 2026-11-19 studies file; expected values are read off them by hand.
parsed_totals <- function(x) {
  p <- .parse_sample_size_components(x)
  tot <- function(w) sum(p$n[p$what == w])
  c(cases = tot("cases"), controls = tot("controls"), sample_size = tot("sample_size"))
}
check_parse <- function(label, x, cases, controls, sample_size = 0) {
  got <- parsed_totals(x)
  check(got[["cases"]] == cases, "%s: expected cases=%s, got %s", label, cases, got[["cases"]])
  check(got[["controls"]] == controls, "%s: expected controls=%s, got %s", label, controls, got[["controls"]])
  check(got[["sample_size"]] == sample_size, "%s: expected sample_size part=%s, got %s", label, sample_size, got[["sample_size"]])
}

gcst90132314 <- "181,522 European ancestry, unknown cases, 984,168 European ancestry, unknown controls"
check_parse("GCST90132314", gcst90132314, 181522, 984168)
r <- resolve_gwas_catalog_ssf_metadata(gcst90132314)
check(r$resolution_status == "resolved", "GCST90132314: expected resolved, got %s", r$resolution_status)
check(r$stored_effect_scale == "log_or", "GCST90132314: expected log_or, got %s", r$stored_effect_scale)
check(r$sample_size_kind == "case_control", "GCST90132314: expected case_control, got %s", r$sample_size_kind)
check(r$n_cases == 181522 && r$n_controls == 984168, "GCST90132314: case/control counts, got %s/%s", r$n_cases, r$n_controls)
check(r$sample_size == 1165690, "GCST90132314: expected sample_size=1165690, got %s", r$sample_size)

# Two ancestry words before the numberless bit (GCST90132315).
check_parse("GCST90132315",
  "210,842 European ancestry, East Asian ancestry, unknown cases, 1,167,328 European ancestry, East Asian ancestry, unknown controls",
  210842, 1167328)
# Ancestry list that itself contains ", " (GCST90566806, GCST90627741, GCST90444202).
check_parse("GCST90566806",
  paste0("186,917 European, East Asian, African, African American, South Asian, Hispanic or admixed ancestry cases, ",
         "796,809 European, East Asian, African, African American, South Asian, Hispanic or admixed ancestry controls"),
  186917, 796809)
check_parse("GCST90132193",
  paste0("5,085 European, African, Greater Middle Eastern (Middle Eastern, North African or Persian), Hispanic or Latin American, ",
         "Native American, South East Asian, East Asian and South Asian ancestry cases, ",
         "571,727 European, African, Greater Middle Eastern (Middle Eastern, North African or Persian), Hispanic or Latin American, ",
         "Native American, South East Asian, East Asian and South Asian ancestry controls"),
  5085, 571727)
# One comma only: the numberless bit directly follows its number (GCST90103739).
check_parse("GCST90103739", "3,739 Chinese, Korean ancestry cases, 7,241 Chinese, Korean ancestry controls", 3739, 7241)
# A sex word and a repeated "cases" in the controls bit (GCST90483474).
check_parse("GCST90483474",
  paste0("10,886 European, South Asian,  African American or Afro-Caribbean and other admixed ancestry female cases, ",
         "995,982 European, South Asian,  African American or Afro-Caribbean and other admixed ancestry female cases female controls"),
  10886, 995982)
# No space after the comma and a singular "control" (GCST90301705).
check_parse("GCST90301705",
  "43,794 European ancestry, African ancestry, Asian ancestry cases,44,187 European ancestry, African ancestry, Asian ancestry control",
  43794, 44187)
# "control" inside a bit is not a controls label: GCST007241 is quantitative.
check_parse("GCST007241",
  "1,456 British ancestry obese children, 6,460 European ancestry control adults", 0, 0, 7916)
# A bit holding both labels and two numbers gives each number to the label
# that follows it (GCST004785; GCST004462 has a non-breaking space after the comma).
check_parse("GCST004785",
  "2,853 European ancestry cases and 37,405 European ancestry controls", 2853, 37405)
check_parse("GCST004462",
  "16,924 European ancestry cases,\u00a068,502 European ancestry controls", 16924, 68502)
# The numberless search stops at a labelled bit: no cross-label borrowing.
check_parse("no cross-label search", "100 individuals, 50 cases, unknown controls", 50, 0, 100)
# A bare comma is a boundary only after "cases", before a count; "control" is a
# label only in that shape; "noncontrol" is never one.
check_parse("bare comma not a boundary", "1,200 Europeans,1000 Genomes ancestry", 0, 0, 1200)
check_parse("glycemic control", "1,200 Europeans under glycemic control", 0, 0, 1200)
check_parse("noncontrol", "500 cases, 700 noncontrol", 500, 0, 700)

# One number shared by "cases and controls" keeps reading as both (base behaviour).
check_parse("GCST009654", "2,253 Korean ancestry cases and controls", 2253, 2253)
# A bare comma after "controls" before a count that "cases" claims (GCST004296):
# cases 15,979+641+837+277+197, controls 102,776+5,234+3,293+3,081+758.
check_parse("GCST004296",
  paste0("15,979 European ancestry cases, 102,776 European ancestry controls, 641 African American cases, ",
         "5,234 African American controls, 837 Japanese ancestry cases, 3,293 Japanese controls, ",
         "277 Hispanic cases, 3,081 Hispanic controls,197 Brazilian ancestry cases, 758 Brazilian ancestry controls."),
  17931, 115142)
# Every decision binds to one clause (round-2 review). A bare comma or a
# singular "control" in one clause does not change how another clause is read.
check_parse("glycemic control after a bare comma",
  "500 cases,1000 Genomes ancestry, 700 Europeans under glycemic control", 500, 0, 700)
check_parse("glycemic control after a labelled bare clause",
  "50 cases,70 controls, 100 individuals under glycemic control", 50, 70, 100)
check_parse("1000 Genomes is not a count",
  "50 cases,1000 Genomes participants, 200 controls", 50, 200)
# One count per clause: repeated labels do not multiply it.
check_parse("repeated label", "500 incident cases including recurrent cases, 700 European controls", 500, 700)
# The repeated typo applies only to a clause with no number.
check_parse("typo clause with a number", "100 female cases, 200 female cases female controls", 100, 200)
# A number belongs to the label after it in its own clause.
check_parse("number after both labels", "50 cases and unknown controls (total 100 participants)", 50, 0)

# Existing shapes keep their counts.
check_parse("simple", "10,007 cases, 474,591 controls", 10007, 474591)
check_parse("quantitative", "1,000 European ancestry individuals", 0, 0, 1000)

cat(sprintf("ALL %d CHECKS PASSED\n", n_checks))
