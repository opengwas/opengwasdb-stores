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

# Each generator keeps its working bundle in <artifact-root>/<id>/generate (#248).
Sys.setenv(OPENGWASDB_ARTIFACT_ROOT = file.path(tmp, "artifact-root"))
work_root_for <- function(store_id) file.path(tmp, "artifact-root", store_id, "generate")
release_dir <- work_root_for("OGS-99003")
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
    store_id = "OGS-99003",
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
bad_checksum_config$output$store_id <- "OGS-99004"
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

# Full mode selects every endpoint in the frozen manifest, in manifest order,
# and records the full-release inclusion reason. The pilot selection above must
# stay untouched by this branch.
full_release_dir <- work_root_for("OGS-99001")
full_config <- config
full_config$family_release_id <- "r13-full"
full_config$release_kind <- "full"
full_config$selection <- list(mode = "full")
full_config$output$store_id <- "OGS-99001"
full_config$output$artifact_subdir <- "finngen-r13/releases/r13-full"
full_config_path <- file.path(tmp, "full-config.yaml")
writeLines(as.yaml(full_config), full_config_path)
full_result <- system2(
  "Rscript",
  c(
    "resources/generators/lib/source-formats/finngen-r13-dense/generate.R",
    paste0("--config=", full_config_path),
    "--mode=emit"
  ),
  stdout = TRUE,
  stderr = TRUE
)
full_status <- attr(full_result, "status")
check(is.null(full_status) || full_status == 0L,
      "full-mode emit failed:\n%s", paste(full_result, collapse = "\n"))
full_analyses <- fread(file.path(full_release_dir, "analyses.tsv"), sep = "\t", na.strings = "")
check(nrow(full_analyses) == nrow(manifest), "full mode should select every manifest row")
check(identical(full_analyses$source_analysis_id, manifest$phenocode),
      "full mode should preserve the manifest's own endpoint order")
check(all(grepl("^all endpoints in the frozen FinnGen R13", full_analyses$inclusion_reason)),
      "full mode should record the full-release inclusion reason")

# emit-registry projects the acquired working bundle into the current
# stores/<OGS-id>/ contract without re-resolving a row. The fixture has no
# downloads, so stamp well-formed checksums to satisfy the emitted-bundle
# precondition, then assert the flat identity/build documents.
registry_root <- file.path(tmp, "registry")
full_config$label <- "r13-full"
full_config$access_posture <- "public"
full_config$output$store_id <- "OGS-99001"
full_config$output$registry_root <- registry_root
full_config$build <- list(
  layout = "dense",
  completion_state = "observed_only",
  command = "build-dense-vcf",
  options = list(
    `source-reader-capability` = "opengwasdb.finngen-r13",
    `source-assembly` = "hg38",
    `n-workers` = 8L
  ),
  post = list(top_hits = TRUE, overview = TRUE)
)
stamped <- copy(full_analyses)
stamped[, checksum := as.character(checksum)]
stamped[is.na(checksum), checksum := paste(rep("a", 64), collapse = "")]
fwrite(stamped, file.path(full_release_dir, "analyses.tsv"), sep = "\t", na = "")
writeLines(as.yaml(full_config), full_config_path)
registry_result <- system2(
  "Rscript",
  c(
    "resources/generators/lib/source-formats/finngen-r13-dense/generate.R",
    paste0("--config=", full_config_path),
    "--mode=emit-registry"
  ),
  stdout = TRUE,
  stderr = TRUE
)
registry_status <- attr(registry_result, "status")
check(is.null(registry_status) || registry_status == 0L,
      "emit-registry failed:\n%s", paste(registry_result, collapse = "\n"))
bundle_dir <- file.path(registry_root, "OGS-99001")
# The projection itself (status, recipe, hm3 default, opt-out) is the shared
# emitter's, tested in tests/emit; here, what is FinnGen's own.
registry_release <- read_yaml(file.path(bundle_dir, "release.yaml"))
check(identical(registry_release$status, "candidate"), "emit-registry should emit a candidate")
commands <- unlist(registry_release$generator$commands)
check(length(commands) == 3L, "the command log should be emit, acquire and emit-registry, got:\n%s",
      paste(commands, collapse = "\n"))
check(grepl("--mode=emit$", commands[[1]]), "the emit step comes from the working bundle's own log")
check(identical(commands[[2]], paste(
        "python resources/generators/lib/source-formats/finngen-r13-dense/acquire.py",
        '--release-dir="$(python3 resources/generators/lib/emit.py work-root OGS-99001)"',
        "--workers=8")),
      "the acquire step is recorded symbolically, never with an artifact root")
check(identical(registry_release$source_snapshot$manifest_sha256, manifest_sha256),
      "the source snapshot pins the FinnGen manifest by content")
check(nrow(fread(file.path(bundle_dir, "analyses.tsv"), sep = "\t")) == nrow(manifest),
      "registry analyses.tsv should carry every manifest row")

# The legacy family-scoped Dense writer declares the default hm3 subset (#210).
legacy_build <- read_yaml(file.path(full_release_dir, "build.yaml"))
check(identical(legacy_build$indexed_subsets[[1]]$name, "hm3"),
      "legacy dense build.yaml should declare the default hm3 Indexed Variant Subset (#210)")

cat(sprintf("ALL %d CHECKS PASSED\n", n_checks))
