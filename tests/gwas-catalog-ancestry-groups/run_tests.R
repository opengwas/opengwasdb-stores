#!/usr/bin/env Rscript
# Unit test for the GWAS Catalog ancestry grouping used by ebi-studies.r (#557).
# Run from the repository root: Rscript tests/gwas-catalog-ancestry-groups/run_tests.R
#
# A row's BROAD ANCESTRAL CATEGORY can be compound ("European, NR"). "NR" (not
# reported) is not an ancestry, so it is dropped when another group is present.

source("src/manifest_generation/providers/gwas_catalog/ancestry_groups.R")

n_checks <- 0L
check_group <- function(raw, expected) {
  n_checks <<- n_checks + 1L
  got <- major_group_for_row(raw)
  if (!identical(got, expected)) {
    stop(sprintf("major_group_for_row(%s): expected %s, got %s", dQuote(raw, FALSE), expected, got), call. = FALSE)
  }
}

check_group("European, NR", "European")
check_group("NR", "NR/Unknown")
check_group("European", "European")
check_group("European, East Asian", "Multiple/Mixed")
check_group("European, NR, East Asian", "Multiple/Mixed")
check_group("NR, European", "European")
check_group("NR, NR", "NR/Unknown")
check_group("African American or Afro-Caribbean, NR", "African")
check_group("African unspecified, Sub-Saharan African", "African")
check_group("European (Finnish), NR", "European")

cat(sprintf("ALL %d CHECKS PASSED\n", n_checks))
