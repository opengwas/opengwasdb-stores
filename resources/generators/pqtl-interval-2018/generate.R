#!/usr/bin/env Rscript

args <- commandArgs(trailingOnly = TRUE)
if (!any(grepl("^--config=", args))) {
  args <- c("--config=resources/generators/pqtl-interval-2018/config.yaml", args)
}

status <- system2(
  "Rscript",
  c("resources/generators/lib/source-formats/gwas-ssf-ragged/generate.R", args)
)
quit(status = status)
