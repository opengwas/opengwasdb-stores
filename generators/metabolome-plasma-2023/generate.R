#!/usr/bin/env Rscript

args <- commandArgs(trailingOnly = TRUE)
if (!any(grepl("^--config=", args))) {
  args <- c("--config=families/metabolome-plasma-2023/generators/config.yaml", args)
}

status <- system2(
  "Rscript",
  c("generators/lib/source-formats/gwas-ssf-ragged/generate.R", args)
)
quit(status = status)
