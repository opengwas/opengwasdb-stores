#!/usr/bin/env Rscript

args <- commandArgs(trailingOnly = TRUE)
if (!any(grepl("^--config=", args))) {
  args <- c("--config=generators/ukb-b/config.yaml", args)
}

status <- system2(
  "Rscript",
  c("generators/lib/source-formats/opengwas-gwas-vcf-dense/generate.R", args)
)
quit(status = status)
