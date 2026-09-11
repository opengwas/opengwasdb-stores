#!/usr/bin/env Rscript

suppressPackageStartupMessages({
  library(data.table)
  library(yaml)
})

parse_args <- function(args) {
  parsed <- list(config = NULL, mode = "emit")
  for (arg in args) {
    if (grepl("^--config=", arg)) parsed$config <- sub("^--config=", "", arg)
    else if (grepl("^--mode=", arg)) parsed$mode <- sub("^--mode=", "", arg)
    else stop("Unknown argument: ", arg)
  }
  if (is.null(parsed$config)) stop("Missing --config=<path>")
  parsed
}

repo_root <- function() {
  normalizePath(system2("git", c("rev-parse", "--show-toplevel"), stdout = TRUE)[[1]], winslash = "/")
}

path_abs <- function(root, path) {
  if (grepl("^/", path)) normalizePath(path, winslash = "/", mustWork = FALSE)
  else normalizePath(file.path(root, path), winslash = "/", mustWork = FALSE)
}

write_yaml_file <- function(value, path) {
  dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)
  writeLines(sub("\n+$", "", as.yaml(value)), path)
}

sha256_file <- function(path) {
  unname(strsplit(system2("sha256sum", path, stdout = TRUE), "\\s+")[[1]][1])
}

script_version <- function(root) {
  paste0("sha256:", sha256_file(path_abs(root, "resources/generators/finngen-r13-dense/generate.R")))
}

write_build_yaml <- function(cfg, release_dir) {
  write_yaml_file(list(
    store_family_id = cfg$store_family_id,
    family_release_id = cfg$family_release_id,
    store_layout = "dense-observed",
    completion_state = "observed-only",
    build = list(
      command = "build-dense-vcf",
      arguments = list(
        "store-id" = cfg$store_family_id,
        "release-id" = cfg$family_release_id
      )
    ),
    source = list(
      root = file.path(cfg$output$artifact_root, cfg$output$artifact_subdir, "source"),
      analyses = "analyses.tsv",
      source_format = "finngen-r13-tabular",
      source_reader_capability = "opengwasdb.finngen-r13",
      source_genome_build = "GRCh38"
    ),
    normalisation = list(target_reference_assembly = "GRCh38", liftover = "none"),
    effects = list(stored_effect_scale = NULL),
    shape = list(association_coverage = cfg$association_coverage),
    reference_resources = cfg$reference_resources %||% list(),
    ancestry_assignment = cfg$ancestry_assignment %||% list(enabled = FALSE),
    effect_scale_validation = cfg$effect_scale_validation %||% list(enabled = FALSE),
    validation = list(required = TRUE),
    artifacts = list(
      artifact_root = cfg$output$artifact_root,
      release_subdir = cfg$output$artifact_subdir,
      source_dir = file.path(cfg$output$artifact_root, cfg$output$artifact_subdir, "source"),
      store_uri = cfg$output$planned_store_uri
    ),
    notes = cfg$build_notes %||% ""
  ), file.path(release_dir, "build.yaml"))
}

update_schema_check <- function(release_dir, root) {
  schema <- validate_against_opengwasdb_schema(file.path(release_dir, "analyses.tsv"), root)
  validation_path <- file.path(release_dir, "validation.yaml")
  validation <- if (file.exists(validation_path)) read_yaml(validation_path) else list(checks = list())
  if (is.null(validation$checks)) validation$checks <- list()
  validation$checks$schema <- if (schema$passed) "passed" else "failed"
  validation$errors <- if (schema$passed) list() else as.list(schema$errors)
  validation$status <- if (schema$passed) "candidate" else "failed"
  write_yaml_file(validation, validation_path)
  if (!schema$passed) {
    stop("analyses.tsv failed opengwasdb's shared analyses.tsv schema:\n", paste(" -", schema$errors, collapse = "\n"))
  }
}

emit_bundle <- function(cfg, root, config_path) {
  manifest_path <- path_abs(root, cfg$source$manifest_path)
  manifest_sha256 <- sha256_file(manifest_path)
  if (!identical(manifest_sha256, cfg$source$manifest_sha256)) {
    stop(
      "FinnGen source manifest SHA-256 mismatch: got ", manifest_sha256,
      ", expected ", cfg$source$manifest_sha256
    )
  }
  manifest <- fread(manifest_path, sep = "\t", na.strings = "")
  selected <- select_finngen_r13_pilot(manifest, as.integer(cfg$selection$binary_count %||% 17L))
  artifact_source_dir <- file.path(cfg$output$artifact_root, cfg$output$artifact_subdir, "source")
  analyses <- finngen_release_rows(selected, artifact_source_dir, cfg$defaults)
  release_dir <- path_abs(root, cfg$output$release_dir)
  dir.create(file.path(release_dir, "sidecars"), recursive = TRUE, showWarnings = FALSE)
  fwrite(analyses, file.path(release_dir, "analyses.tsv"), sep = "\t", na = "")
  fwrite(finngen_derivations(analyses), file.path(release_dir, "sidecars", "derivations.tsv"), sep = "\t", na = "")
  selection_audit <- data.table(
    selection_rank = seq_len(nrow(analyses)),
    analysis_id = analyses$analysis_id,
    phenocode = analyses$source_analysis_id,
    trait_type = ifelse(analyses$stored_effect_scale == "log_or", "case_control", "quantitative"),
    source_category = analyses$source_category,
    n_cases = analyses$n_cases,
    n_controls = analyses$n_controls,
    selection_rule = analyses$inclusion_reason
  )
  fwrite(selection_audit, file.path(release_dir, "sidecars", "selection.tsv"), sep = "\t", na = "")

  write_yaml_file(list(
    metadata_schema_version = 1,
    store_family_id = cfg$store_family_id,
    family_release_id = cfg$family_release_id,
    status = "candidate",
    source_collection_id = cfg$source_collection_id,
    source_snapshot_id = cfg$source$source_snapshot_id,
    source_snapshot = list(
      manifest_url = cfg$source$manifest_url,
      manifest_sha256 = cfg$source$manifest_sha256,
      manifest_size_bytes = file.size(manifest_path),
      manifest_etag = cfg$source$manifest_etag %||% NULL,
      manifest_last_modified = cfg$source$manifest_last_modified %||% NULL
    ),
    release_kind = cfg$release_kind,
    association_coverage = cfg$association_coverage,
    description = cfg$description,
    created_at = format(Sys.time(), "%Y-%m-%dT%H:%M:%SZ", tz = "UTC"),
    accepted_at = NULL,
    generator = list(
      name = "resources/generators/finngen-r13-dense/generate.R",
      version = script_version(root),
      command = paste("Rscript resources/generators/finngen-r13-dense/generate.R", paste0("--config=", config_path), "--mode=emit")
    ),
    build_environment = build_environment_info(root),
    source_defaults = list(
      source_genome_build = cfg$defaults$source_genome_build,
      license = cfg$defaults$license,
      sample_size_kind = NULL
    ),
    lineage = list(derived_from = NULL),
    sidecars = list(
      selection = "sidecars/selection.tsv",
      derivations = "sidecars/derivations.tsv",
      downloads = "sidecars/downloads.tsv",
      ancestry = "sidecars/ancestry.tsv",
      sd_estimation = "sidecars/sd_estimation.tsv",
      build_report = "sidecars/build_report.tsv"
    ),
    notes = cfg$notes %||% ""
  ), file.path(release_dir, "release.yaml"))
  write_build_yaml(cfg, release_dir)
  write_yaml_file(list(
    status = "candidate",
    validated_at = NULL,
    validator = list(name = "resources/generators/finngen-r13-dense/generate.R", version = NULL),
    checks = list(
      selection = "passed",
      metadata_resolution = "passed",
      schema = "not_run",
      files = "not_run",
      reader_smoke_test = "not_run",
      ancestry = "not_run",
      effect_scale = "not_run",
      sd_estimation = "not_run"
    ),
    reports = list(
      selection = "sidecars/selection.tsv",
      metadata_resolution = "sidecars/derivations.tsv"
    ),
    warnings = list(),
    errors = list()
  ), file.path(release_dir, "validation.yaml"))
  update_schema_check(release_dir, root)
  cat(sprintf("Emitted FinnGen R13 pilot with %d Analyses to %s\n", nrow(analyses), release_dir))
}

args <- parse_args(commandArgs(trailingOnly = TRUE))
root <- repo_root()
source(path_abs(root, "resources/lib/metadata_resolvers/finngen_manifest.R"))
source(path_abs(root, "resources/lib/build_environment.R"))
source(path_abs(root, "resources/lib/schema_validate.R"))
source(path_abs(root, "resources/lib/finngen_r13_pilot.R"))
cfg <- read_yaml(path_abs(root, args$config))

if (args$mode == "emit") {
  emit_bundle(cfg, root, args$config)
} else if (args$mode == "validate") {
  update_schema_check(path_abs(root, cfg$output$release_dir), root)
  cat("FinnGen R13 release schema is valid\n")
} else {
  stop("Unknown --mode=", args$mode)
}
