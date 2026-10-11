#!/usr/bin/env Rscript
# R mirror of the generator-side default Indexed Variant Subset resolver
# (stores #210, epic #214). Every Dense and Hybrid R writer sources
# resources/generators/lib/indexed_subsets.R and calls resolve_indexed_subsets();
# this pins the values and the opt-out/error contract without building a fixture.
#
#   Rscript tests/indexed-subsets/test_generator_defaults.R

suppressPackageStartupMessages(library(yaml))
source("resources/generators/lib/indexed_subsets.R")

fail <- function(...) stop(sprintf(...), call. = FALSE)
n_checks <- 0L
check <- function(cond, ...) {
  n_checks <<- n_checks + 1L
  if (!isTRUE(cond)) fail(...)
}

root <- normalizePath(".")
EXPECTED <- list(
  name = "hm3",
  resource_id = "hapmap3-hg38",
  variant_list = "/data/opengwasdb/reference/hapmap3-hg38/hm3.grch38.alid.txt",
  reference_assembly = "GRCh38",
  checksum = "4a7c82aa625a006ff437dcaa2769b00e056413d54c72e98f7378327c69513398"
)

check(identical(default_hm3_declaration(root), EXPECTED),
      "default hm3 must come from the maintained hapmap3-hg38 resource")

for (layout in c("dense", "hybrid")) {
  resolved <- resolve_indexed_subsets(list(), layout, root)
  check(identical(resolved$subsets, list(EXPECTED)),
        sprintf("%s must default to the hm3 declaration", layout))
  check(is.null(resolved$note), sprintf("%s default has no opt-out note", layout))
}

ragged <- resolve_indexed_subsets(list(), "ragged", root)
check(is.null(ragged$subsets), "a Ragged release emits no indexed_subsets key")
check(is.null(ragged$note), "a Ragged release has no opt-out note")

opt_out <- resolve_indexed_subsets(
  list(indexed_subsets = list(), indexed_subsets_opt_out = "  predates the list  "),
  "hybrid", root
)
check(identical(opt_out$subsets, list()), "an opt-out is the explicit empty list")
check(identical(opt_out$note, "predates the list"), "the opt-out reason is trimmed")

error_of <- function(expr) tryCatch({ expr; NULL }, error = function(e) conditionMessage(e))
check(!is.null(error_of(resolve_indexed_subsets(list(indexed_subsets = list()), "dense", root))),
      "an empty list without a reason is an error")
check(!is.null(error_of(resolve_indexed_subsets(list(indexed_subsets = NULL), "hybrid", root))),
      "a null declaration is malformed")
check(!is.null(error_of(resolve_indexed_subsets(list(indexed_subsets = list(list(name = "hm3"))), "dense", root))),
      "an entry missing a required field is malformed")
check(!is.null(error_of(resolve_indexed_subsets(list(indexed_subsets = list(EXPECTED)), "ragged", root))),
      "a Ragged declaration is an error")

rendered <- as.yaml(list(indexed_subsets = list(EXPECTED)))
check(grepl("indexed_subsets:", rendered, fixed = TRUE), "yaml renders the indexed_subsets key")
check(grepl("resource_id: hapmap3-hg38", rendered, fixed = TRUE), "yaml renders the maintained resource_id")

# The release.yaml note helper the writers use: silence when the default
# applies, an appended reviewable sentence when the opt-out is recorded.
check(identical(notes_with_indexed_subsets_opt_out("existing", "hybrid", list(), root), "existing"),
      "no opt-out note is added when the default applies")
noted <- notes_with_indexed_subsets_opt_out(
  "existing", "hybrid",
  list(indexed_subsets = list(), indexed_subsets_opt_out = "predates the list"),
  root
)
check(grepl("Indexed Variant Subsets (#210)", noted, fixed = TRUE), "opt-out note names the issue")
check(grepl("intentionally omitted: predates the list", noted, fixed = TRUE),
      "opt-out note records the written reason")

cat(sprintf("ALL %d CHECKS PASSED\n", n_checks))
