#!/usr/bin/env Rscript
# Download canonical GWAS-VCFs through ieugwasr's authenticated gwasinfo/files API.
suppressPackageStartupMessages(library(ieugwasr))

args <- commandArgs(trailingOnly = TRUE)
ids_arg <- sub("^--ids=", "", args[grepl("^--ids=", args)])
out_arg <- sub("^--out-dir=", "", args[grepl("^--out-dir=", args)])
if (!length(ids_arg) || !length(out_arg)) {
  stop("Usage: download-opengwas-vcfs.R --ids=id1,id2 --out-dir=/path")
}
ids <- strsplit(ids_arg[[1]], ",", fixed = TRUE)[[1]]
out_dir <- out_arg[[1]]
dir.create(out_dir, recursive = TRUE, showWarnings = FALSE)

for (id in ids) {
  urls <- unlist(gwasinfo_files(id), use.names = FALSE)
  objects <- sub(".*?/o/", "", urls)
  keep <- grepl(paste0("/", id, "\\.vcf\\.gz(\\.tbi)?$"), objects)
  if (sum(keep) != 2L) stop("Expected canonical VCF + index for ", id, "; found ", sum(keep))
  for (i in which(keep)) {
    destination <- file.path(out_dir, basename(objects[[i]]))
    if (!file.exists(destination) || file.info(destination)$size == 0) {
      message("Downloading ", basename(destination))
      suppressMessages(download.file(urls[[i]], destination, mode = "wb", quiet = TRUE))
    } else {
      message("Already present: ", destination)
    }
  }
}
