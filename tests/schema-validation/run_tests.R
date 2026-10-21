#!/usr/bin/env Rscript
# Release-bundle-level smoke test for checks.schema validating against
# opengwasdb's shared analyses.tsv schema (issue #51). Run from the
# repository root:
#
#   Rscript tests/schema-validation/run_tests.R
#
# Proves (the generator-driven checks -- a clean manifest emitting with
# checks.schema=passed and a log_or row without counts failing the emit -- ran
# the R gwas-ssf-ragged generator, retired with #300):
#   3. Issue #51 AC3's two named example violations -- an out-of-vocabulary
#      controlled value and a missing required column -- each fail
#      src/manifest_generation/schema_validate.py directly with a message identifying
#      the violation, checked in isolation from the full generator pipeline.
#   4. The real, already-accepted release bundles this repository tracks --
#      every registered Store Release under stores/ -- still conform to
#      opengwasdb's shared schema (issue #51 AC4) -- checked directly
#      against src/manifest_generation/schema_validate.py without mutating the
#      tracked release bundles' own validation.yaml. The two legacy BESD Trial
#      Store Releases are excluded: their blank required values are the known
#      issue #134 gap, tolerated only for those ids.

suppressPackageStartupMessages(library(yaml))

`%||%` <- function(x, y) if (is.null(x)) y else x
fail <- function(...) stop(sprintf(...), call. = FALSE)
n_checks <- 0L
check <- function(cond, ...) {
  n_checks <<- n_checks + 1L
  if (!isTRUE(cond)) fail(...)
}

fixtures_dir <- "tests/schema-validation/fixtures"
# --- AC3's two named example violations, checked directly against the
# validator in isolation (not via the full generator pipeline) ---
validate_directly <- function(tsv_name) {
  result <- suppressWarnings(system2(
    "python3", c("-m", "manifest_generation.schema_validate", shQuote(file.path(fixtures_dir, tsv_name))),
    stdout = TRUE, stderr = TRUE
  ))
  list(status = attr(result, "status") %||% 0L, output = result)
}

bad_vocab <- validate_directly("bad-vocab.tsv")
check(bad_vocab$status != 0L, "bad-vocab.tsv: expected schema_validate.py to fail on an out-of-vocabulary original_sd_method")
check(any(grepl("original_sd_method", bad_vocab$output, fixed = TRUE)),
      "bad-vocab.tsv: expected the error to name original_sd_method, got:\n%s",
      paste(bad_vocab$output, collapse = "\n"))

missing_column <- validate_directly("missing-column.tsv")
check(missing_column$status != 0L, "missing-column.tsv: expected schema_validate.py to fail on a missing required column")
check(any(grepl("sample_size_scope", missing_column$output, fixed = TRUE)),
      "missing-column.tsv: expected the error to name the missing sample_size_scope column, got:\n%s",
      paste(missing_column$output, collapse = "\n"))

# --- Real, already-accepted release bundles still conform (issue #51 AC4) ---
# Every registered Release Bundle, read from the one home for Release Bundles
# (stores/, #252), except the two legacy BESD Trial Store Releases whose blank
# required Analysis values are the documented issue #134 gap.
LEGACY_BESD_RELEASES <- c("OGS-00001", "OGS-00002")
real_bundles <- Filter(
  function(path) !(basename(dirname(path)) %in% LEGACY_BESD_RELEASES),
  Sys.glob("stores/OGS-*/analyses.tsv")
)
check(length(real_bundles) >= 5, "expected to find the registered Release Bundles under stores/, found %d", length(real_bundles))
check(all(c("OGS-00006", "OGS-00007") %in% basename(dirname(real_bundles))),
      "expected the metabolome-plasma-2023 and pqtl-interval-2018 bundles among them")
for (bundle in real_bundles) {
  result <- system2("python3", c("-m", "manifest_generation.schema_validate", shQuote(bundle)), stdout = TRUE, stderr = TRUE)
  status <- attr(result, "status") %||% 0L
  check(status == 0L, "%s: expected to still conform to opengwasdb's shared schema, got:\n%s",
        bundle, paste(result, collapse = "\n"))
}

cat(sprintf("ALL %d CHECKS PASSED\n", n_checks))
