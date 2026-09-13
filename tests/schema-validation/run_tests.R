#!/usr/bin/env Rscript
# Release-bundle-level smoke test for checks.schema validating against
# opengwasdb's shared analyses.tsv schema (issue #51). Run from the
# repository root:
#
#   Rscript tests/schema-validation/run_tests.R
#
# Proves:
#   1. A manifest that resolves cleanly against opengwasdb's shared core
#      schema emits with checks.schema=passed.
#   2. A manifest with a real schema violation -- stored_effect_scale=log_or
#      with no n_cases/n_controls, the same shape of gap issue #15's
#      ieu-a-7 example exposed -- fails loudly (`--mode=emit` exits
#      non-zero) with a clear message naming the violation, and
#      validation.yaml records checks.schema=failed with that message,
#      rather than silently staying not_run or passing regardless.
#   3. Issue #51 AC3's two named example violations -- an out-of-vocabulary
#      controlled value and a missing required column -- each fail
#      resources/generators/lib/schema_validate.py directly with a message identifying
#      the violation, checked in isolation from the full generator pipeline.
#   4. The real, already-accepted release bundles this repository tracks
#      (pqtl-interval-2018, metabolome-plasma-2023) still conform to
#      opengwasdb's shared schema (issue #51 AC4) -- checked directly
#      against resources/generators/lib/schema_validate.py without mutating the
#      tracked release bundles' own validation.yaml.

suppressPackageStartupMessages(library(yaml))

`%||%` <- function(x, y) if (is.null(x)) y else x
fail <- function(...) stop(sprintf(...), call. = FALSE)
n_checks <- 0L
check <- function(cond, ...) {
  n_checks <<- n_checks + 1L
  if (!isTRUE(cond)) fail(...)
}

fixtures_dir <- "tests/schema-validation/fixtures"
output_valid <- file.path(fixtures_dir, "output-valid")
output_invalid <- file.path(fixtures_dir, "output-invalid")
unlink(output_valid, recursive = TRUE)
unlink(output_invalid, recursive = TRUE)

run_emit <- function(config_name) {
  # config-invalid.yaml is expected to exit non-zero -- suppressWarnings()
  # silences system2()'s routine "had status 1" warning for that case.
  result <- suppressWarnings(system2(
    "Rscript",
    c("resources/generators/lib/source-formats/gwas-ssf-ragged/generate.R",
      paste0("--config=", file.path(fixtures_dir, config_name)), "--mode=emit"),
    stdout = TRUE, stderr = TRUE
  ))
  list(status = attr(result, "status") %||% 0L, output = result)
}

# --- A clean manifest emits with checks.schema=passed ---
valid_run <- run_emit("config-valid.yaml")
check(valid_run$status == 0L, "config-valid.yaml: expected --mode=emit to succeed, got status %s:\n%s",
      valid_run$status, paste(valid_run$output, collapse = "\n"))

valid_validation <- read_yaml(file.path(output_valid, "validation.yaml"))
check(identical(valid_validation$checks$schema, "passed"),
      "config-valid.yaml: expected checks.schema=passed, got %s", valid_validation$checks$schema)

# --- A manifest with a real opengwasdb schema violation fails loudly ---
invalid_run <- run_emit("config-invalid.yaml")
check(invalid_run$status != 0L, "config-invalid.yaml: expected --mode=emit to fail non-zero")
combined_output <- paste(invalid_run$output, collapse = "\n")
check(grepl("log_or", combined_output, fixed = TRUE) && grepl("n_cases", combined_output, fixed = TRUE),
      "config-invalid.yaml: expected the failure message to name the log_or/n_cases violation, got:\n%s",
      combined_output)

invalid_validation <- read_yaml(file.path(output_invalid, "validation.yaml"))
check(identical(invalid_validation$checks$schema, "failed"),
      "config-invalid.yaml: expected checks.schema=failed, got %s", invalid_validation$checks$schema)
check(length(invalid_validation$errors) > 0,
      "config-invalid.yaml: expected validation.yaml errors to record the violation, got none")
check(identical(invalid_validation$status, "failed"),
      "config-invalid.yaml: expected overall status=failed, got %s", invalid_validation$status)

# --- AC3's two named example violations, checked directly against the
# validator in isolation (not via the full generator pipeline) ---
validate_directly <- function(tsv_name) {
  result <- suppressWarnings(system2(
    "python3", c("resources/generators/lib/schema_validate.py", shQuote(file.path(fixtures_dir, tsv_name))),
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
real_bundles <- c(
  "families/pqtl-interval-2018/releases/2018-sun-pilot-100/analyses.tsv",
  Sys.glob("families/metabolome-plasma-2023/releases/*/analyses.tsv")
)
check(length(real_bundles) >= 5, "expected to find pqtl-interval-2018 + 5 metabolome-plasma-2023 release bundles, found %d", length(real_bundles))
for (bundle in real_bundles) {
  result <- system2("python3", c("resources/generators/lib/schema_validate.py", shQuote(bundle)), stdout = TRUE, stderr = TRUE)
  status <- attr(result, "status") %||% 0L
  check(status == 0L, "%s: expected to still conform to opengwasdb's shared schema, got:\n%s",
        bundle, paste(result, collapse = "\n"))
}

unlink(output_valid, recursive = TRUE)
unlink(output_invalid, recursive = TRUE)

cat(sprintf("ALL %d CHECKS PASSED\n", n_checks))
