#!/usr/bin/env Rscript
suppressPackageStartupMessages(library(data.table))
source("generators/lib/metadata_resolvers/finngen_manifest.R")

check <- function(x, message) if (!isTRUE(x)) stop(message, call. = FALSE)

# Verbatim field shapes captured from FinnGen R11's public manifest.
binary <- list(phenocode="AB1_ACTINOMYCOSIS", phenotype="Actinomycosis",
  category="I Certain infectious and parasitic diseases (AB1_)",
  num_cases=113, num_controls=399149,
  path_https="https://storage.googleapis.com/finngen-public-data-r11/summary_stats/finngen_R11_AB1_ACTINOMYCOSIS.gz")
quantitative <- list(phenocode="BMI_IRN", phenotype="Body-mass index, inverse-rank normalized",
  category="Quantitative endpoints", num_cases=321672, num_controls=0,
  path_https="https://storage.googleapis.com/finngen-public-data-r11/summary_stats/finngen_R11_BMI_IRN.gz")

r <- resolve_finngen_manifest_metadata(binary)
check(r$resolution_status == "resolved" && r$stored_effect_scale == "log_or", "binary scale")
check(r$sample_size_kind == "case_control" && r$n_cases == 113 && r$n_controls == 399149, "binary counts")
r <- resolve_finngen_manifest_metadata(quantitative)
check(r$resolution_status == "resolved" && r$stored_effect_scale == "sd", "quantitative scale")
check(r$sample_size_kind == "total" && r$sample_size == 321672, "quantitative N")
r <- resolve_finngen_manifest_metadata(list(phenocode="BROKEN", category="", num_cases=0, num_controls=0))
check(r$resolution_status == "unresolved" && is.na(r$sample_size), "unresolvable row")
cat("ALL 5 CHECKS PASSED\n")
