#!/usr/bin/env Rscript

args <- commandArgs(trailingOnly = TRUE)
if (!any(grepl("^--config=", args))) {
  args <- c("--config=generators/finngen-r13/config-pilot-20.yaml", args)
}

status <- system2(
  "Rscript",
  c("generators/lib/source-formats/finngen-r13-dense/generate.R", args)
)
quit(status = status)
