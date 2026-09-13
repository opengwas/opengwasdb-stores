#!/usr/bin/env Rscript

args <- commandArgs(trailingOnly = TRUE)
if (!any(grepl("^--config=", args))) {
  args <- c("--config=resources/generators/ukb-b/config.yaml", args)
}

status <- system2(
  "Rscript",
  c("resources/generators/lib/source-formats/opengwas-gwas-vcf-dense/generate.R", args)
)
quit(status = status)
