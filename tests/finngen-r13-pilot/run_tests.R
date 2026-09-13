#!/usr/bin/env Rscript

suppressPackageStartupMessages({
  library(data.table)
  library(yaml)
})

fail <- function(...) stop(sprintf(...), call. = FALSE)
n_checks <- 0L
check <- function(condition, ...) {
  n_checks <<- n_checks + 1L
  if (!isTRUE(condition)) fail(...)
}

root <- normalizePath(getwd(), winslash = "/")
tmp <- tempfile("finngen-r13-pilot-")
dir.create(tmp, recursive = TRUE)
on.exit(unlink(tmp, recursive = TRUE), add = TRUE)

# Three quantitative endpoints plus two candidates in each of 18 binary
# categories. Category winners have 1000 + category rank cases; the pilot
# must retain the winners from the 17 largest categories and drop category
# 01 deterministically.
quantitative <- data.table(
  phenocode = c("BMI_IRN", "HEIGHT_IRN", "WEIGHT_IRN"),
  phenotype = c(
    "Body mass index, inverse-rank normalized",
    "Height, inverse-rank normalized",
    "Weight, inverse-rank normalized"
  ),
  category = "Quantitative endpoints",
  num_cases = c(362216L, 364515L, 369975L),
  num_controls = 0L
)
binary <- rbindlist(lapply(seq_len(18L), function(i) {
  data.table(
    phenocode = c(sprintf("CAT%02d_SMALL", i), sprintf("CAT%02d_WINNER", i)),
    phenotype = c(sprintf("Category %02d small", i), sprintf("Category %02d winner", i)),
    category = sprintf("Category %02d", i),
    num_cases = c(100L + i, 1000L + i),
    num_controls = c(5000L, 5000L)
  )
}))
manifest <- rbindlist(list(quantitative, binary), use.names = TRUE)
manifest[, `:=`(
  path_bucket = paste0("gs://fixture/", phenocode, ".gz"),
  path_https = paste0("https://fixture.invalid/", phenocode, ".gz")
)]
manifest_path <- file.path(tmp, "finngen_R13_manifest.tsv")
fwrite(manifest, manifest_path, sep = "\t")
manifest_sha256 <- strsplit(system2("sha256sum", manifest_path, stdout = TRUE), "\\s+")[[1]][1]

release_dir <- file.path(tmp, "release")
artifact_dir <- file.path(tmp, "artifacts")
config <- list(
  store_family_id = "finngen-r13",
  family_release_id = "r13-pilot-20",
  source_collection_id = "finngen-r13",
  release_kind = "pilot",
  association_coverage = "full_gwas",
  description = "Offline FinnGen R13 pilot fixture",
  source = list(
    source_snapshot_id = "finngen-r13-fixture",
    manifest_path = manifest_path,
    manifest_url = "https://fixture.invalid/finngen_R13_manifest.tsv",
    manifest_sha256 = manifest_sha256
  ),
  defaults = list(
    source_genome_build = "GRCh38",
    license = "FinnGen public data"
  ),
  selection = list(binary_count = 17L),
  output = list(
    release_dir = release_dir,
    artifact_root = artifact_dir,
    artifact_subdir = "finngen-r13/releases/r13-pilot-20",
    planned_store_uri = file.path(artifact_dir, "store.opengwasdb")
  )
)
config_path <- file.path(tmp, "config.yaml")
writeLines(as.yaml(config), config_path)

result <- system2(
  "Rscript",
  c("resources/generators/lib/source-formats/finngen-r13-dense/generate.R", paste0("--config=", config_path), "--mode=emit"),
  stdout = TRUE,
  stderr = TRUE
)
status <- attr(result, "status")
check(is.null(status) || status == 0L, "generator emit failed:\n%s", paste(result, collapse = "\n"))

analyses <- fread(file.path(release_dir, "analyses.tsv"), sep = "\t", na.strings = "")
expected_ids <- c(
  "finngen-r13-BMI_IRN", "finngen-r13-HEIGHT_IRN", "finngen-r13-WEIGHT_IRN",
  sprintf("finngen-r13-CAT%02d_WINNER", 18:2)
)
check(identical(analyses$analysis_id, expected_ids),
      "selection order mismatch; got %s", paste(analyses$analysis_id, collapse = ","))
check(nrow(analyses) == 20L, "expected 20 analyses, got %d", nrow(analyses))
check(sum(analyses$stored_effect_scale == "log_or") == 17L, "expected 17 binary analyses")
check(sum(analyses$stored_effect_scale == "sd") == 3L, "expected three quantitative analyses")
check(all(analyses[stored_effect_scale == "log_or"]$sample_size_kind == "case_control"),
      "binary sample_size_kind should be case_control")
check(all(analyses[stored_effect_scale == "log_or"]$sample_size ==
            analyses[stored_effect_scale == "log_or"]$n_cases +
            analyses[stored_effect_scale == "log_or"]$n_controls),
      "binary sample_size should equal cases + controls")
check(all(is.na(analyses[stored_effect_scale == "sd"]$n_cases)) &&
        all(is.na(analyses[stored_effect_scale == "sd"]$n_controls)),
      "quantitative total N must not be represented as case/control counts")
check(all(analyses$source_genome_build == "GRCh38"), "source assembly should be GRCh38")
check(all(analyses$source_reader_capability == "opengwasdb.finngen-r13"),
      "release should declare the native FinnGen reader capability")
selection_audit <- fread(file.path(release_dir, "sidecars", "selection.tsv"), sep = "\t")
check(identical(selection_audit$analysis_id, expected_ids),
      "selection audit should preserve the frozen Analysis order")

validation <- read_yaml(file.path(release_dir, "validation.yaml"))
check(identical(validation$checks$selection, "passed"), "selection check should pass")
check(identical(validation$checks$metadata_resolution, "passed"),
      "metadata resolution check should pass")
check(identical(validation$checks$schema, "passed"), "shared schema check should pass")

# Public validate mode must expose a shared-schema failure and persist the
# failed state rather than silently leaving checks.schema unchanged.
bad <- copy(analyses)
bad[, sample_size_scope := NULL]
fwrite(bad, file.path(release_dir, "analyses.tsv"), sep = "\t", na = "")
bad_result <- suppressWarnings(system2(
  "Rscript",
  c("resources/generators/lib/source-formats/finngen-r13-dense/generate.R", paste0("--config=", config_path), "--mode=validate"),
  stdout = TRUE,
  stderr = TRUE
))
bad_status <- attr(bad_result, "status")
check(!is.null(bad_status) && bad_status != 0L, "schema-invalid manifest should fail validation")
failed_validation <- read_yaml(file.path(release_dir, "validation.yaml"))
check(identical(failed_validation$checks$schema, "failed"),
      "validation.yaml should record checks.schema=failed")

# The source snapshot is pinned by content, not merely by a mutable URL/path.
bad_checksum_config <- config
bad_checksum_config$source$manifest_sha256 <- paste(rep("0", 64), collapse = "")
bad_checksum_config$output$release_dir <- file.path(tmp, "wrong-checksum-release")
bad_checksum_path <- file.path(tmp, "wrong-checksum-config.yaml")
writeLines(as.yaml(bad_checksum_config), bad_checksum_path)
checksum_result <- suppressWarnings(system2(
  "Rscript",
  c(
    "resources/generators/lib/source-formats/finngen-r13-dense/generate.R",
    paste0("--config=", bad_checksum_path),
    "--mode=emit"
  ),
  stdout = TRUE,
  stderr = TRUE
))
checksum_status <- attr(checksum_result, "status")
check(!is.null(checksum_status) && checksum_status != 0L,
      "generator should reject a source manifest that does not match its pinned SHA-256")

cat(sprintf("ALL %d CHECKS PASSED\n", n_checks))
