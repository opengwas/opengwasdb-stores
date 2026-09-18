#!/usr/bin/env Rscript

suppressPackageStartupMessages({
  library(data.table)
  library(yaml)
})

`%||%` <- function(x, y) if (is.null(x)) y else x

check <- function(condition, message, ...) {
  if (!isTRUE(condition)) stop(sprintf(message, ...), call. = FALSE)
}

sha256_file <- function(path) {
  connection <- file(path, "rb")
  on.exit(close(connection), add = TRUE)
  as.character(openssl::sha256(connection))
}

run_materialiser <- function(script, store_id, registry_root, qc_panel = NULL, workers = 1L) {
  args <- c(
    script,
    store_id,
    paste0("--registry-root=", registry_root),
    paste0("--parallel-workers=", workers)
  )
  if (!is.null(qc_panel)) args <- c(args, paste0("--qc-panel=", qc_panel))
  output <- suppressWarnings(system2("Rscript", args, stdout = TRUE, stderr = TRUE))
  list(status = attr(output, "status") %||% 0L, output = paste(output, collapse = "\n"))
}

root <- normalizePath(".", winslash = "/")
script <- file.path(root, "resources", "scripts", "materialise-gwas-ssf-ragged.R")
check(file.exists(script), "materialiser script does not exist: %s", script)

tmp <- tempfile("materialise-gwas-ssf-ragged-")
dir.create(tmp, recursive = TRUE)
on.exit(unlink(tmp, recursive = TRUE), add = TRUE)

registry_root <- file.path(tmp, "stores")
store_id <- "OGS-00991"
release_dir <- file.path(registry_root, store_id)
dir.create(file.path(release_dir, "sidecars"), recursive = TRUE)

raw_source <- file.path(tmp, "source.h.tsv.gz")
raw <- data.table(
  chromosome = rep("chr1", 6),
  base_pair_location = c(100L, 175L, 300L, 300L, 400L, 500L),
  effect_allele = rep("A", 6),
  other_allele = rep("G", 6),
  beta = c(0.1, 0.2, 0.3, 0.35, 0.4, 0.5),
  standard_error = rep(0.01, 6),
  p_value = c(1e-9, 0.5, 1e-6, 2e-6, 0.8, 0.9),
  effect_allele_frequency = rep(0.2, 6),
  variant_id = paste0("v", 1:6),
  rsid = paste0("rs", 1:6),
  ignored_column = "ignored"
)
fwrite(raw, raw_source, sep = "\t")

qc_panel <- file.path(tmp, "qc-panel.tsv")
fwrite(data.table(chromosome = "1", position = 400L), qc_panel, sep = "\t")

analysis_ids <- c(store_id, "fixture-analysis-two")
region_template <- data.table(
  region_kind = c("significant_trans", "suggestive_trans", "suggestive_trans", "qc_panel"),
  chromosome = c("1", "1", "1", "1"),
  start = c(90L, 300L, 500L, 400L),
  end = c(160L, 300L, 500L, 400L),
  lead_variant_id = c("", "v3", "", "")
)
regions <- rbindlist(lapply(analysis_ids, function(id) {
  copy(region_template)[, analysis_id := id]
}), use.names = TRUE)
setcolorder(regions, c("analysis_id", names(region_template)))
fwrite(regions, file.path(release_dir, "sidecars", "sparse_regions.tsv"), sep = "\t")

write_yaml(
  list(sidecars = list(sparse_regions = "sidecars/sparse_regions.tsv")),
  file.path(release_dir, "release.yaml")
)

expected_path <- file.path(tmp, "expected.tsv.gz")
expected <- copy(raw[c(1L, 3L, 5L, 6L)])
expected[, chromosome := sub("^chr", "", chromosome, ignore.case = TRUE)]
selected_columns <- c(
  "chromosome", "base_pair_location", "effect_allele", "other_allele",
  "beta", "standard_error", "p_value", "effect_allele_frequency",
  "variant_id", "rsid"
)
fwrite(expected[, ..selected_columns], expected_path, sep = "\t")
expected_checksum <- sha256_file(expected_path)
expected_size <- file.size(expected_path)

destinations <- file.path(tmp, "materialised", c("filtered.tsv.gz", "filtered-two.tsv.gz"))
destination <- destinations[[1]]
manifest <- data.table(
  analysis_id = analysis_ids,
  source_url = paste0("file://", raw_source),
  source_file = destinations,
  exclude_from_build = ""
)
manifest_path <- file.path(release_dir, "analyses.tsv")
fwrite(manifest, manifest_path, sep = "\t")
manifest_before <- readLines(manifest_path, warn = FALSE)

# A release whose frozen filter plan contains QC-panel regions must declare
# the exact panel used for point-level retention.
missing_qc <- run_materialiser(script, store_id, registry_root)
check(missing_qc$status != 0L, "materialiser should reject missing --qc-panel")
check(grepl("--qc-panel is required", missing_qc$output, fixed = TRUE),
      "missing-QC error was not actionable:\n%s", missing_qc$output)
check(!any(file.exists(destinations)), "missing-QC failure must not create destinations")

# First run exercises parallel downloads, filtering, verification, and atomic publication.
first <- run_materialiser(script, store_id, registry_root, qc_panel, workers = 2L)
check(first$status == 0L, "first materialisation failed:\n%s", first$output)
check(all(file.exists(destinations)), "materialised outputs do not exist")
check(all(vapply(destinations, sha256_file, character(1)) == expected_checksum),
      "materialised checksum differs")
check(all(file.size(destinations) == expected_size), "materialised size differs")
checksum_paths <- paste0(destinations, ".sha256")
check(all(file.exists(checksum_paths)), "checksum files were not written")
checksum_lines <- vapply(checksum_paths, readLines, character(1))
check(all(checksum_lines == paste0(expected_checksum, "  ", basename(destinations))),
      "checksum-file contents differ")
check(grepl("materialised=2", first$output, fixed = TRUE), "first-run summary missing materialised=2")
check(identical(readLines(manifest_path, warn = FALSE), manifest_before),
      "materialiser must not modify analyses.tsv")
check(!length(Sys.glob(file.path(dirname(destination), ".*.partial*"))),
      "partial output was not cleaned up")
check(!length(Sys.glob(file.path(dirname(destination), ".*.download*"))),
      "transient download was not cleaned up")

# A second run verifies and skips an already-correct output without touching it.
mtime <- file.info(destination)$mtime
Sys.sleep(1.1)
second <- run_materialiser(script, store_id, registry_root, qc_panel)
check(second$status == 0L, "second materialisation failed:\n%s", second$output)
check(identical(file.info(destination)$mtime, mtime), "valid output was rewritten instead of skipped")
check(grepl("verified=2", second$output, fixed = TRUE), "second-run summary missing verified=2")

# A corrupt existing destination is rebuilt and its checksum file is refreshed.
writeBin(charToRaw("corrupt"), destination)
repair <- run_materialiser(script, store_id, registry_root, qc_panel)
check(repair$status == 0L, "repair materialisation failed:\n%s", repair$output)
check(sha256_file(destination) == expected_checksum, "corrupt destination was not repaired")
check(readLines(paste0(destination, ".sha256")) == paste0(expected_checksum, "  ", basename(destination)),
      "checksum file was not refreshed")

# A filter failure publishes neither output nor checksum and cleans all temps.
bad_source <- file.path(tmp, "bad-source.tsv.gz")
fwrite(data.table(chromosome = "1", base_pair_location = 100L), bad_source, sep = "\t")
unlink(c(destination, paste0(destination, ".sha256")))
manifest[analysis_id == store_id, source_url := paste0("file://", bad_source)]
fwrite(manifest, manifest_path, sep = "\t")
bad <- run_materialiser(script, store_id, registry_root, qc_panel)
check(bad$status != 0L, "invalid source should fail")
check(grepl("missing columns", bad$output, ignore.case = TRUE),
      "filter failure was not reported:\n%s", bad$output)
check(!file.exists(destination), "failed output must not be published")
check(!file.exists(paste0(destination, ".sha256")), "failed checksum must not be published")
check(!length(Sys.glob(file.path(dirname(destination), ".*.partial*"))),
      "partial output remained after filtering failure")
check(!length(Sys.glob(file.path(dirname(destination), ".*.download*"))),
      "download remained after filtering failure")

cat("materialise-gwas-ssf-ragged tests passed\n")
