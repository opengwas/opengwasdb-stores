suppressPackageStartupMessages({
  library(data.table)
  library(yaml)
})

`%||%` <- function(x, y) if (is.null(x) || length(x) == 0 || is.na(x)) y else x

parse_args <- function(args) {
  out <- list(mode = "emit", config = NULL, max_analyses = NA_integer_, only_analysis_id = "")
  for (arg in args) {
    if (grepl("^--config=", arg)) out$config <- sub("^--config=", "", arg)
    else if (grepl("^--mode=", arg)) out$mode <- sub("^--mode=", "", arg)
    else if (grepl("^--max-analyses=", arg)) {
      out$max_analyses <- as.integer(sub("^--max-analyses=", "", arg))
    } else if (grepl("^--only-analysis-id=", arg)) {
      out$only_analysis_id <- sub("^--only-analysis-id=", "", arg)
    } else {
      stop("Unknown argument: ", arg)
    }
  }
  if (is.null(out$config)) stop("Missing --config=<path>")
  out
}

repo_root <- function() {
  root <- tryCatch(
    system2("git", c("rev-parse", "--show-toplevel"), stdout = TRUE, stderr = FALSE),
    error = function(e) character()
  )
  if (length(root) && nzchar(root[[1]])) normalizePath(root[[1]], winslash = "/")
  else normalizePath(getwd(), winslash = "/")
}

path_abs <- function(root, path) {
  if (grepl("^/", path)) normalizePath(path, winslash = "/", mustWork = FALSE)
  else normalizePath(file.path(root, path), winslash = "/", mustWork = FALSE)
}

write_yaml_file <- function(x, path) {
  dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)
  writeLines(sub("\n+$", "", as.yaml(x)), path)
}

sha256_file <- function(path) {
  unname(strsplit(system2("sha256sum", path, stdout = TRUE), "\\s+")[[1]][1])
}

script_version <- function(root) {
  path <- path_abs(root, "resources/generators/lib/source-formats/opengwas-gwas-vcf-dense/generate.R")
  paste0("sha256:", sha256_file(path))
}

# The candidates file is a full-shape analyses.tsv (see
# docs/release-metadata-schema.md), not a lighter pre-schema candidates
# table like gwas-ssf-ragged's store-candidates-analyses.tsv: this Source
# Collection has no local bulk study-metadata table to derive candidates
# from (the source GWAS-VCFs live on a separate host this registry does not
# have direct access to), so the input is the batch's existing manifest
# with the fields this generator resolves left blank or at a
# family-config-level default.
load_candidates <- function(cfg, root) {
  candidates <- fread(path_abs(root, cfg$inputs$candidates), sep = "\t", na.strings = "")
  # A blank column all-NA across every row (e.g. exclude_from_build,
  # inclusion_reason on a not-yet-resolved candidate manifest) is read back
  # as logical NA rather than character by data.table's type inference;
  # this generator always writes character values into these columns, so
  # coerce up front rather than erroring deep inside apply_resolution().
  for (col in c("exclude_from_build", "inclusion_reason")) {
    if (col %in% names(candidates)) candidates[, (col) := as.character(get(col))]
  }
  candidates
}

# Resolves stored_effect_scale/sample_size_kind/sample_size/n_cases/
# n_controls for every id via resources/generators/lib/metadata_resolvers/
# opengwas_api.R (issue #49/#50), batched through fetch_opengwas_gwasinfo()
# rather than one request per Analysis. An id the API returns no record for
# at all (withdrawn, no access, typo) is resolved the same way an
# API-returned-but-uninformative record would be: an explicit `unresolved`
# row, never silently skipped or defaulted.
resolve_batch <- function(ids, jwt = Sys.getenv("OPENGWAS_JWT")) {
  gwasinfo_by_id <- fetch_opengwas_gwasinfo(ids, jwt = jwt)
  rows <- lapply(ids, function(id) {
    resolved <- resolve_opengwas_api_metadata(gwasinfo_by_id[[id]])
    resolved[, analysis_id := id]
    resolved
  })
  rbindlist(rows)
}

write_build_yaml <- function(cfg, release_dir) {
  write_yaml_file(list(
    store_family_id = cfg$store_family_id,
    family_release_id = cfg$family_release_id,
    store_layout = "dense-observed",
    completion_state = "observed-only",
    builder = list(
      package = "opengwasdb",
      entrypoint = "opengwasdb.layouts.dense.build_vcf:build_dense_from_vcf_manifest"
    ),
    source = list(
      source_format = "gwas-vcf",
      source_reader_capability = "opengwasdb.gwas-vcf",
      source_genome_build = cfg$defaults$source_genome_build
    ),
    normalisation = list(
      target_reference_assembly = cfg$defaults$target_reference_assembly %||% "GRCh38",
      liftover = cfg$defaults$liftover,
      liftover_chain = cfg$defaults$liftover_chain
    ),
    effects = list(stored_effect_scale = NULL),
    shape = list(association_coverage = cfg$association_coverage),
    reference_resources = if (is.null(cfg$reference_resources)) list() else cfg$reference_resources,
    ancestry_assignment = if (is.null(cfg$ancestry_assignment)) list(enabled = FALSE) else cfg$ancestry_assignment,
    effect_scale_validation = if (is.null(cfg$effect_scale_validation)) list(enabled = FALSE) else cfg$effect_scale_validation,
    validation = list(required = TRUE),
    artifacts = list(
      artifact_root = cfg$output$artifact_root,
      release_subdir = cfg$output$artifact_subdir,
      store_uri = cfg$output$planned_store_uri
    ),
    notes = cfg$build_notes %||% ""
  ), file.path(release_dir, "build.yaml"))
}

emit_bundle <- function(cfg, root, max_analyses = NA_integer_, only_analysis_id = "") {
  candidates <- load_candidates(cfg, root)
  selected <- select_analyses(candidates, max_analyses, only_analysis_id)

  resolution <- resolve_batch(selected$analysis_id)
  applied <- apply_resolution(selected, resolution)
  analyses <- applied$analyses

  # analysis_label (ADR 0034, shared core): this Source Collection has no
  # separate curated display name from source_label, so mirror it directly
  # -- same convention gwas-ssf-ragged uses (docs/release-metadata-schema.md).
  analyses[, analysis_label := source_label]

  # Trait Ontology Mapping (CONTEXT.md): this Source Collection supplies no
  # trait ontology mapping of its own (issue: opengwasdb-stores#63 gap
  # analysis), so every row falls through to the Canonical Trait Mapping
  # Table when declared, or explicit `unmapped` otherwise -- never silently
  # blank with no record of why.
  canonical_table <- load_canonical_trait_table_from_cfg(cfg$reference_resources, root)
  analyses <- apply_trait_ontology_mapping(analyses, canonical_table)

  release_dir <- path_abs(root, cfg$output$release_dir)
  sidecar_dir <- file.path(release_dir, "sidecars")
  dir.create(sidecar_dir, recursive = TRUE, showWarnings = FALSE)
  fwrite(analyses, file.path(release_dir, "analyses.tsv"), sep = "\t", na = "")
  fwrite(applied$derivations, file.path(sidecar_dir, "derivations.tsv"), sep = "\t", na = "")

  is_excluded <- !is.na(analyses$exclude_from_build) & analyses$exclude_from_build == "true"
  n_excluded <- sum(is_excluded)
  n_resolved <- nrow(analyses) - n_excluded
  status <- if (n_excluded == 0) "passed" else "passed_with_warnings"
  warnings_list <- if (n_excluded == 0) list() else as.list(sprintf(
    "%s excluded_from_build: %s", analyses[is_excluded]$analysis_id,
    analyses[is_excluded]$inclusion_reason
  ))

  command <- paste("Rscript resources/generators/lib/source-formats/opengwas-gwas-vcf-dense/generate.R",
                   paste0("--config=", cfg$.config_path), "--mode=emit")
  write_yaml_file(list(
    metadata_schema_version = 1,
    store_family_id = cfg$store_family_id,
    family_release_id = cfg$family_release_id,
    status = "candidate",
    source_collection_id = cfg$source_collection_id,
    source_snapshot_id = cfg$source$source_snapshot_id,
    release_kind = cfg$release_kind,
    association_coverage = cfg$association_coverage,
    description = cfg$description,
    created_at = format(Sys.time(), "%Y-%m-%dT%H:%M:%SZ", tz = "UTC"),
    accepted_at = NULL,
    generator = list(
      name = "resources/generators/lib/source-formats/opengwas-gwas-vcf-dense/generate.R",
      version = script_version(root),
      command = command
    ),
    build_environment = build_environment_info(root),
    source_defaults = list(
      source_genome_build = cfg$defaults$source_genome_build,
      license = cfg$defaults$license,
      sample_size_kind = NULL
    ),
    lineage = list(derived_from = cfg$lineage_derived_from %||% NULL),
    sidecars = list(
      derivations = "sidecars/derivations.tsv",
      ancestry = "sidecars/ancestry.tsv",
      sd_estimation = "sidecars/sd_estimation.tsv"
    ),
    notes = cfg$notes %||% ""
  ), file.path(release_dir, "release.yaml"))

  write_build_yaml(cfg, release_dir)

  write_yaml_file(list(
    status = status,
    validated_at = format(Sys.time(), "%Y-%m-%dT%H:%M:%SZ", tz = "UTC"),
    validator = list(name = "resources/generators/lib/source-formats/opengwas-gwas-vcf-dense/generate.R", version = NULL),
    checks = list(
      schema = "not_run",
      files = "not_run",
      reader_smoke_test = "not_run",
      ancestry = "not_run",
      effect_scale = "not_run",
      sd_estimation = "not_run"
    ),
    reports = list(metadata_resolution = "sidecars/derivations.tsv"),
    warnings = warnings_list,
    errors = list()
  ), file.path(release_dir, "validation.yaml"))

  cat(sprintf(
    "Emitted %d analyses (%d resolved, %d excluded_from_build) to %s\n",
    nrow(analyses), n_resolved, n_excluded, release_dir
  ))
  invisible(analyses)
}

validate_emit <- function(cfg, root) {
  release_dir <- path_abs(root, cfg$output$release_dir)
  # exclude_from_build's only non-blank value is the literal string "true"
  # (see docs/release-metadata-schema.md); fread's type inference otherwise
  # reads a column of blanks + "true" as logical, and a subsequent `!=
  # "true"` comparison against a *logical* TRUE coerces it to the string
  # "TRUE" (not "true"), silently treating every row as buildable.
  analyses <- fread(file.path(release_dir, "analyses.tsv"), sep = "\t", na.strings = "",
                     colClasses = list(character = "exclude_from_build"))
  required <- c(
    "analysis_id", "source_analysis_id", "source_label", "analysis_label", "source_file",
    "source_genome_build", "trait_ontology_mapping_method",
    "stored_effect_scale", "sample_size_kind",
    "sample_size_scope", "sample_size", "n_cases", "n_controls", "exclude_from_build"
  )
  missing_cols <- setdiff(required, names(analyses))
  if (length(missing_cols)) stop("analyses.tsv missing columns: ", paste(missing_cols, collapse = ", "))
  if (anyDuplicated(analyses$analysis_id)) stop("analysis_id must be unique")

  buildable <- analyses[exclude_from_build != "true" | is.na(exclude_from_build)]
  if (any(is.na(buildable$sample_size))) {
    stop("sample_size is required for every buildable analysis (unresolved rows must carry exclude_from_build)")
  }
  if (any(!buildable$stored_effect_scale %in% c("sd", "log_or", "log_hazard"))) {
    stop("stored_effect_scale must be sd, log_or, or log_hazard for every buildable analysis")
  }
  needs_counts <- buildable[stored_effect_scale %in% c("log_or", "log_hazard")]
  if (nrow(needs_counts) && (any(is.na(needs_counts$n_cases)) || any(is.na(needs_counts$n_controls)))) {
    stop("n_cases/n_controls are required for every buildable log_or/log_hazard analysis")
  }
  # Excluded rows are registry candidates deliberately withheld from the
  # builder because their required Analytical Metadata could not be resolved;
  # OpenGWASDB's shared build-input schema therefore applies to buildable rows.
  schema_path <- file.path(release_dir, "analyses.tsv")
  temporary_schema_path <- NULL
  if (nrow(buildable) != nrow(analyses)) {
    temporary_schema_path <- tempfile(fileext = ".analyses.tsv")
    fwrite(buildable, temporary_schema_path, sep = "\t", na = "")
    schema_path <- temporary_schema_path
  }
  schema <- validate_against_opengwasdb_schema(schema_path, root)
  if (!is.null(temporary_schema_path)) unlink(temporary_schema_path)
  validation_path <- file.path(release_dir, "validation.yaml")
  validation <- if (file.exists(validation_path)) read_yaml(validation_path) else list(checks = list())
  validation$checks$schema <- if (schema$passed) "passed" else "failed"
  validation$errors <- if (schema$passed) list() else as.list(schema$errors)
  write_yaml_file(validation, validation_path)
  if (!schema$passed) {
    stop("analyses.tsv failed opengwasdb's shared analyses.tsv schema:\n",
         paste(" -", schema$errors, collapse = "\n"))
  }
  cat(sprintf(
    "Release bundle schema smoke check passed for %d analyses (%d buildable, %d excluded)\n",
    nrow(analyses), nrow(buildable), nrow(analyses) - nrow(buildable)
  ))
}

args <- parse_args(commandArgs(trailingOnly = TRUE))
root <- repo_root()
source(path_abs(root, "resources/generators/lib/metadata_resolvers/opengwas_api.R"))
source(path_abs(root, "resources/generators/lib/metadata_resolvers/ontology_contract.R"))
source(path_abs(root, "resources/generators/lib/metadata_resolvers/canonical_trait_table.R"))
source(path_abs(root, "resources/generators/lib/build_environment.R"))
source(path_abs(root, "resources/generators/lib/opengwas_gwas_vcf_dense.R"))
source(path_abs(root, "resources/generators/lib/schema_validate.R"))
cfg <- read_yaml(path_abs(root, args$config))
cfg$.config_path <- args$config

if (args$mode == "emit") {
  emit_bundle(cfg, root, args$max_analyses, args$only_analysis_id)
  validate_emit(cfg, root)
} else if (args$mode == "validate") {
  validate_emit(cfg, root)
} else if (args$mode == "refresh-build") {
  release_dir <- path_abs(root, cfg$output$release_dir)
  write_build_yaml(cfg, release_dir)
  cat("Refreshed ", file.path(release_dir, "build.yaml"), "\n", sep = "")
} else {
  stop("Unknown --mode=", args$mode)
}
