## Shared generator for GWAS Catalog harmonised GWAS-SSF releases that should
## be stored as Hybrid OpenGWASDB stores (dense on-panel fill + ragged
## off-panel overflow, opengwasdb's ADR 0026).
##
## Structurally mirrors resources/generators/gwas-ssf-ragged/generate.R
## (repo/path helpers, ontology resolution, opengwasdb schema validation,
## release.yaml/build.yaml/validation.yaml bookkeeping), but a Hybrid release
## needs no sparse-region filtering: unlike Ragged, which keeps only a small
## genomic slice per Analysis, Hybrid partitions *every* observed association
## into its Dense Component (on the reference panel) or Ragged Overflow (off
## it) -- opengwasdb's builder needs the full harmonised file, not a filtered
## subset. This generator's second stage is therefore a plain `download`
## (fetch the full file, verify its header, checksum it), not `filter`.
##
## GWAS-SSF sources are natively GRCh38 (already harmonised), unlike
## GWAS-VCF's hg19 -- explodecomputer/opengwasdb#84/#85 added the
## `opengwasdb.gwas-ssf` Source Reader Capability and a per-row
## `source_assembly` build-manifest column so the Hybrid/Dense builders skip
## liftover for these sources rather than silently re-lifting (and
## mispositioning) already-hg38 coordinates. This generator's `build.yaml`
## declares both.

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

artifact_paths <- function(cfg, root) {
  artifact_root <- path_abs(root, cfg$output$artifact_root)
  artifact_subdir <- cfg$output$artifact_subdir
  if (is.null(artifact_subdir) || !nzchar(artifact_subdir)) {
    stop("output.artifact_subdir is required when output.artifact_root is set")
  }
  if (grepl("^/", artifact_subdir)) stop("output.artifact_subdir must be relative to output.artifact_root")
  artifact_dir <- file.path(artifact_root, artifact_subdir)
  list(
    artifact_root = artifact_root,
    artifact_subdir = artifact_subdir,
    artifact_dir = artifact_dir,
    download_dir = file.path(artifact_dir, "download"),
    store_uri = file.path(artifact_dir, "store", cfg$store_key)
  )
}

sha256_file <- function(path) {
  unname(strsplit(system2("sha256sum", path, stdout = TRUE), "\\s+")[[1]][1])
}

script_version <- function(root) {
  path <- path_abs(root, "resources/generators/gwas-ssf-hybrid/generate.R")
  paste0("sha256:", sha256_file(path))
}

write_yaml_file <- function(x, path) {
  dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)
  writeLines(sub("\n+$", "", as.yaml(x)), path)
}

load_inputs <- function(cfg, root) {
  fread(path_abs(root, cfg$inputs$candidates), sep = "\t", na.strings = "")
}

# Selects a deterministic prefix of the configured candidate pool (same
# ordering as the candidates table itself, matching pqtl-interval-2018's
# pilot-10 convention) -- optionally restricted to one `study_design`, since
# case-control Analyses need no phenotype-SD estimation at all (binary_trait,
# ADR-0029 tier) while quantitative Analyses in this multi-publication pool
# have no per-family curated SD method the way a single-paper Store Family
# does, and would need real reference-AF SD estimation (issue #16's engine)
# to honestly populate original_sd_method -- deliberately out of scope for
# this first Hybrid pilot (see the release notes and the family's follow-up
# issue), which restricts `selection.study_design: case-control` to sidestep
# it rather than fabricate a `declared_standardised` assumption this pool has
# no evidence for.
# GWAS Catalog's harmonisation pipeline does not cover every accession --
# older (pre-harmonisation-era) studies commonly have no `harmonised/*.h.tsv.gz`
# file at all, unpredictably by table order (confirmed directly: every one of
# the 10 oldest GCST000xxx-GCST0012xx case-control accessions in this pool
# 404s). A deterministic head-of-table prefix (pqtl-interval-2018's
# pilot-10 convention) is therefore not safe here the way it is for a family
# whose filtered files are already known to exist locally -- this probes each
# candidate's harmonised URL with a cheap HTTP HEAD before counting it toward
# `n_analyses`, in table order, so the selection is still deterministic for a
# fixed candidates table and FTP contents.
# A harmonised file existing is not sufficient: some case-control studies'
# harmonised GWAS-SSF reports `odds_ratio` (plus a separate `logor` column)
# rather than `beta` -- confirmed directly (GCST006980/GCST007228/GCST008225
# in this pool all lack a `beta` column entirely). opengwasdb.readers.gwas_ssf
# requires `beta`, so those sources cannot feed this builder. A full download
# is too slow to use as a pre-filter across a whole candidate pool (~1 min
# each for these file sizes); this reads only the first 64 KB (a `curl`
# range request, piped to `zcat`) -- enough to decompress and read the header
# line for any of these files in well under a second -- rather than the whole
# multi-hundred-MB body.
harmonised_header_ok <- function(url) {
  header_line <- suppressWarnings(tryCatch(
    system(
      sprintf(
        "curl -s --max-time 20 -r 0-65535 %s | zcat 2>/dev/null | head -1",
        shQuote(url)
      ),
      intern = TRUE
    ),
    error = function(e) character()
  ))
  if (!length(header_line) || !nzchar(header_line[1])) return(FALSE)
  header <- strsplit(header_line[1], "\t", fixed = TRUE)[[1]]
  !length(setdiff(required_ssf_columns, header))
}

select_analyses <- function(cfg, candidates) {
  pool <- candidates[store_key == cfg$selection$store_key]
  if (!is.null(cfg$selection$ancestry_group)) {
    pool <- pool[ancestry_group == cfg$selection$ancestry_group]
  }
  if (!is.null(cfg$selection$study_design)) {
    pool <- pool[study_design == cfg$selection$study_design]
  }
  pool <- pool[!is.na(sample_size) & sample_size > 0]
  if (identical(cfg$selection$study_design, "case-control")) {
    pool <- pool[!is.na(n_cases) & !is.na(n_controls) & n_cases > 0 & n_controls > 0]
  }
  if (!nrow(pool)) stop("Selection produced zero candidate analyses")

  n_wanted <- as.integer(cfg$selection$n_analyses %||% nrow(pool))
  keep <- logical(nrow(pool))
  n_found <- 0L
  n_checked <- 0L
  for (i in seq_len(nrow(pool))) {
    if (n_found >= n_wanted) break
    n_checked <- n_checked + 1L
    if (harmonised_header_ok(ssf_url(pool$STUDY.ACCESSION[i], cfg$source$ftp_base))) {
      keep[i] <- TRUE
      n_found <- n_found + 1L
    }
  }
  cat(sprintf(
    "Selection: checked %d candidate(s), found %d with a usable harmonised file (wanted %d)\n",
    n_checked, n_found, n_wanted
  ))
  selected <- pool[keep]
  if (!nrow(selected)) stop("Selection produced zero analyses with an available harmonised file")
  selected[, analysis_index := .I - 1L]
  selected[]
}

manifest_rows <- function(cfg, selected, paths, canonical_table = NULL) {
  x <- copy(selected)
  setorder(x, analysis_index)

  x[, downloaded_file := sprintf("%s.h.tsv.gz", STUDY.ACCESSION)]
  x[, source_file := file.path(paths$download_dir, downloaded_file)]
  x[, source_url := ssf_url(STUDY.ACCESSION, cfg$source$ftp_base)]
  x[, source_bundle_id := ""]
  x[, source_ancestry_label := ancestry_group]
  x[, assigned_ancestry := ancestry_group]
  x[, ancestry_assignment_method := cfg$defaults$ancestry_assignment_method %||% "source_trusted_no_af"]
  x[, original_effect_scale := cfg$defaults$original_effect_scale]
  x[, original_sd := ""]
  x[, original_sd_method := cfg$defaults$original_sd_method]
  x[, stored_effect_scale := cfg$defaults$stored_effect_scale]
  # The candidates table represents "not a case-control study" as n_cases=0/
  # n_controls=0, not NA -- honest here would be blank (schema doc: these
  # counts are "Required when stored_effect_scale = log_or/log_hazard",
  # optional otherwise), not a fabricated zero case count.
  x[, n_cases := fifelse(stored_effect_scale %in% c("log_or", "log_hazard"), n_cases, NA_real_)]
  x[, n_controls := fifelse(stored_effect_scale %in% c("log_or", "log_hazard"), n_controls, NA_real_)]
  x[, sample_size_kind := cfg$defaults$sample_size_kind]
  x[, sample_size_scope := cfg$defaults$sample_size_scope %||% "analysis_level"]
  x[, license := cfg$defaults$license]
  x[, publication_doi := ""]
  x[, publication_pmid := as.character(PUBMED.ID)]
  x[, consortium := ""]
  x[, source_genome_build := cfg$defaults$source_genome_build %||% "GRCh38"]
  x[, analysis_group_id := cfg$defaults$analysis_group_id %||% ""]
  x[, inclusion_reason := cfg$defaults$inclusion_reason %||% ""]
  x[, exclude_from_build := ""]
  x[, checksum := ""]
  x[, checksum_algorithm := "sha256"]
  x[, size_bytes := ""]
  x[, trait_ontology_id_curie := obo_uri_to_curie(MAPPED_TRAIT_URI)]

  ontology_resolved <- resolve_trait_ontology_mappings(
    trait_labels = x$DISEASE.TRAIT,
    source_ontology_ids = x$trait_ontology_id_curie,
    source_ontology_labels = x$MAPPED_TRAIT,
    canonical_table = canonical_table
  )
  x[, trait_ontology_id := ontology_resolved$trait_ontology_id]
  x[, trait_ontology_label := ontology_resolved$trait_ontology_label]
  x[, trait_ontology_mapping_method := ontology_resolved$trait_ontology_mapping_method]

  out_cols <- c(
    "analysis_index",
    "analysis_id" = "STUDY.ACCESSION",
    "source_analysis_id" = "STUDY.ACCESSION",
    "source_label" = "DISEASE.TRAIT",
    "analysis_label" = "DISEASE.TRAIT",
    "trait_ontology_label",
    "trait_ontology_id",
    "trait_ontology_mapping_method",
    "source_file",
    "source_url",
    "source_bundle_id",
    "downloaded_file",
    "checksum",
    "checksum_algorithm",
    "size_bytes",
    "source_genome_build",
    "license",
    "publication_doi",
    "publication_pmid",
    "consortium",
    "first_author" = "FIRST.AUTHOR",
    "source_ancestry_label",
    "assigned_ancestry",
    "ancestry_assignment_method",
    "original_effect_scale",
    "original_sd",
    "original_sd_method",
    "stored_effect_scale",
    "sample_size_kind",
    "sample_size_scope",
    "sample_size",
    "n_cases",
    "n_controls",
    "analysis_group_id",
    "inclusion_reason",
    "exclude_from_build"
  )
  out_names <- ifelse(nzchar(names(out_cols)), names(out_cols), out_cols)
  ans <- data.table()
  for (i in seq_along(out_cols)) ans[, (out_names[i]) := x[[out_cols[i]]]]
  ans[]
}

write_build_yaml <- function(cfg, root, release_dir, paths) {
  write_yaml_file(list(
    store_family_id = cfg$store_family_id,
    family_release_id = cfg$family_release_id,
    store_layout = "hybrid-observed",
    completion_state = "observed-only",
    build = list(
      command = "build-hybrid",
      arguments = list(
        "store-id" = cfg$store_family_id,
        "release-id" = cfg$family_release_id
      )
    ),
    source = list(
      root = paths$download_dir,
      analyses = "analyses.tsv",
      source_format = "gwas-ssf",
      source_reader_capability = cfg$hybrid$source_reader_capability %||% "opengwasdb.gwas-ssf",
      source_genome_build = cfg$defaults$source_genome_build
    ),
    normalisation = list(
      target_reference_assembly = "GRCh38",
      liftover = NULL,
      source_assembly = cfg$hybrid$source_assembly %||% "hg38"
    ),
    effects = list(stored_effect_scale = cfg$defaults$stored_effect_scale),
    shape = list(association_coverage = cfg$association_coverage),
    reference_resources = build_reference_resources_yaml(cfg),
    effect_scale_validation = build_effect_scale_validation_yaml(cfg),
    validation = list(required = TRUE),
    artifacts = list(
      artifact_root = paths$artifact_root,
      release_subdir = paths$artifact_subdir,
      download_dir = paths$download_dir,
      store_uri = paths$store_uri,
      build_log_uri = NULL
    )
  ), file.path(release_dir, "build.yaml"))
}

emit_bundle <- function(cfg, root) {
  candidates <- load_inputs(cfg, root)
  selected <- select_analyses(cfg, candidates)
  paths <- artifact_paths(cfg, root)
  canonical_table <- load_canonical_trait_table_from_cfg(cfg$reference_resources, root)
  analyses <- manifest_rows(cfg, selected, paths, canonical_table = canonical_table)

  release_dir <- path_abs(root, cfg$output$release_dir)
  sidecar_dir <- file.path(release_dir, "sidecars")
  dir.create(sidecar_dir, recursive = TRUE, showWarnings = FALSE)
  fwrite(analyses, file.path(release_dir, "analyses.tsv"), sep = "\t", na = "")

  command <- paste("Rscript resources/generators/gwas-ssf-hybrid/generate.R",
                   paste0("--config=", cfg$.config_path), "--mode=emit")
  write_yaml_file(list(
    metadata_schema_version = 1,
    store_family_id = cfg$store_family_id,
    family_release_id = cfg$family_release_id,
    store_key = cfg$store_key,
    status = "candidate",
    source_collection_id = cfg$source_collection_id,
    source_snapshot_id = cfg$source$source_snapshot_id,
    release_kind = cfg$release_kind,
    association_coverage = cfg$association_coverage,
    description = cfg$description,
    created_at = format(Sys.time(), "%Y-%m-%dT%H:%M:%SZ", tz = "UTC"),
    accepted_at = NULL,
    generator = list(
      name = "resources/generators/gwas-ssf-hybrid/generate.R",
      version = script_version(root),
      command = command
    ),
    build_environment = build_environment_info(root),
    source_defaults = list(
      source_genome_build = cfg$defaults$source_genome_build,
      license = cfg$defaults$license,
      original_effect_scale = cfg$defaults$original_effect_scale,
      stored_effect_scale = cfg$defaults$stored_effect_scale,
      sample_size_kind = cfg$defaults$sample_size_kind,
      source_ancestry_label = cfg$selection$ancestry_group,
      assigned_ancestry = cfg$selection$ancestry_group
    ),
    lineage = list(derived_from = NULL),
    sidecars = list(
      download_summary = "sidecars/download_summary.tsv",
      build_report = "sidecars/build_report.tsv",
      sd_estimation = "sidecars/sd_estimation.tsv"
    ),
    notes = cfg$notes %||% ""
  ), file.path(release_dir, "release.yaml"))

  write_build_yaml(cfg, root, release_dir, paths)

  write_yaml_file(list(
    status = "not_run",
    validated_at = NULL,
    validator = list(name = "resources/generators/gwas-ssf-hybrid/generate.R", version = NULL),
    checks = list(schema = "not_run", files = "not_run", reader_smoke_test = "not_run"),
    reports = list(),
    warnings = list(),
    errors = list()
  ), file.path(release_dir, "validation.yaml"))

  cat(sprintf("Emitted %d analyses to %s\n", nrow(analyses), release_dir))
  invisible(analyses)
}

# Minimum GWAS-SSF columns opengwasdb.readers.gwas_ssf.GwasSsfReader actually
# reads (issue #84) -- checked against every downloaded header so a source
# file with an unexpected shape fails loudly here, at download time, rather
# than surfacing as a confusing zero-association build later.
required_ssf_columns <- c(
  "chromosome", "base_pair_location", "effect_allele", "other_allele",
  "beta", "standard_error"
)

download_one <- function(row, download_dir, timeout_seconds) {
  out_path <- file.path(download_dir, row$downloaded_file)
  t0 <- Sys.time()
  options(timeout = timeout_seconds)
  download.file(row$source_url, out_path, mode = "wb", quiet = TRUE)
  elapsed <- as.numeric(difftime(Sys.time(), t0, units = "secs"))
  header <- names(fread(out_path, nrows = 0))
  missing <- setdiff(required_ssf_columns, header)
  if (length(missing)) {
    unlink(out_path)
    stop(sprintf(
      "%s: downloaded file missing required GWAS-SSF column(s): %s",
      row$analysis_id, paste(missing, collapse = ", ")
    ))
  }
  data.table(
    analysis_id = row$analysis_id, status = "ok", error = "",
    source_url = row$source_url, output_file = row$source_file,
    output_bytes = file.size(out_path), checksum_algorithm = "sha256",
    checksum = sha256_file(out_path), download_seconds = round(elapsed, 1)
  )
}

download_release <- function(cfg, root, max_analyses = NA_integer_, only_analysis_id = "") {
  release_dir <- path_abs(root, cfg$output$release_dir)
  analyses_path <- file.path(release_dir, "analyses.tsv")
  if (!file.exists(analyses_path)) emit_bundle(cfg, root)
  analyses <- fread(analyses_path, sep = "\t", na.strings = "", colClasses = list(character = "exclude_from_build"))
  for (col in c("checksum", "size_bytes")) {
    if (!col %in% names(analyses)) analyses[, (col) := ""]
    analyses[, (col) := fifelse(is.na(as.character(get(col))), "", as.character(get(col)))]
  }

  todo <- analyses
  if (nzchar(only_analysis_id)) {
    ids <- strsplit(only_analysis_id, ",", fixed = TRUE)[[1]]
    todo <- todo[analysis_id %in% ids]
  }
  if (!is.na(max_analyses)) todo <- todo[seq_len(min(max_analyses, .N))]
  partial_run <- !is.na(max_analyses) || nzchar(only_analysis_id)

  paths <- artifact_paths(cfg, root)
  dir.create(paths$download_dir, recursive = TRUE, showWarnings = FALSE)
  timeout_seconds <- cfg$download$timeout_seconds %||% 3600

  cat(sprintf("Downloading %d full harmonised GWAS-SSF file(s)\n", nrow(todo)))
  summaries <- vector("list", nrow(todo))
  for (i in seq_len(nrow(todo))) {
    row <- todo[i]
    summaries[[i]] <- tryCatch(
      download_one(row, paths$download_dir, timeout_seconds),
      error = function(e) {
        data.table(
          analysis_id = row$analysis_id, status = "failed", error = conditionMessage(e),
          source_url = row$source_url, output_file = row$source_file,
          output_bytes = NA_real_, checksum_algorithm = "sha256", checksum = "",
          download_seconds = NA_real_
        )
      }
    )
    result <- summaries[[i]]
    if (identical(result$status, "ok")) {
      cat(sprintf("[%d/%d] %s OK (%.1f MB, %.1fs)\n", i, nrow(todo), row$analysis_id,
                  result$output_bytes / 1e6, result$download_seconds))
    } else {
      cat(sprintf("[%d/%d] %s FAILED: %s\n", i, nrow(todo), row$analysis_id, result$error))
    }
  }
  summary_dt <- rbindlist(summaries, fill = TRUE)
  sidecar_dir <- file.path(release_dir, "sidecars")
  dir.create(sidecar_dir, recursive = TRUE, showWarnings = FALSE)
  summary_name <- if (partial_run) "download_summary.partial.tsv" else "download_summary.tsv"
  fwrite(summary_dt, file.path(sidecar_dir, summary_name), sep = "\t", na = "")

  if (!partial_run) {
    ok <- summary_dt[status == "ok"]
    if (nrow(ok) != nrow(summary_dt)) {
      stop(sprintf(
        "%d/%d downloads failed; see %s before re-running", nrow(summary_dt) - nrow(ok),
        nrow(summary_dt), file.path(sidecar_dir, summary_name)
      ))
    }
    for (i in seq_len(nrow(ok))) {
      analyses[analysis_id == ok$analysis_id[i], `:=`(
        checksum = ok$checksum[i], size_bytes = as.character(ok$output_bytes[i])
      )]
    }
    fwrite(analyses, analyses_path, sep = "\t", na = "")
    cat(sprintf("Downloaded %d analyses; updated %s\n", nrow(summary_dt), analyses_path))
  } else {
    cat(sprintf(
      "Downloaded %d analyses as a partial run; wrote %s and left %s unchanged\n",
      nrow(summary_dt), summary_name, analyses_path
    ))
  }
  invisible(summary_dt)
}

validate_emit <- function(cfg, root) {
  release_dir <- path_abs(root, cfg$output$release_dir)
  analyses_path <- file.path(release_dir, "analyses.tsv")
  analyses <- fread(analyses_path, sep = "\t", na.strings = "", colClasses = list(character = "exclude_from_build"))

  required <- c(
    "analysis_index", "analysis_id", "source_analysis_id", "source_label",
    "analysis_label", "trait_ontology_label", "trait_ontology_id",
    "trait_ontology_mapping_method", "source_file", "source_genome_build",
    "license", "first_author", "downloaded_file"
  )
  missing_cols <- setdiff(required, names(analyses))
  if (length(missing_cols)) stop("analyses.tsv missing columns: ", paste(missing_cols, collapse = ", "))
  if (anyDuplicated(analyses$analysis_id)) stop("analysis_id must be unique")
  if (!identical(analyses$analysis_index, seq_len(nrow(analyses)) - 1L)) {
    stop("analysis_index must be dense 0..n-1")
  }

  schema <- validate_against_opengwasdb_schema(analyses_path, root)
  if (!schema$passed) {
    merge_validation_checks(
      release_dir, list(schema = "failed", errors = schema$errors),
      "resources/generators/gwas-ssf-hybrid/generate.R"
    )
    stop(
      "analyses.tsv failed opengwasdb's shared analyses.tsv schema:\n",
      paste(" -", schema$errors, collapse = "\n")
    )
  }
  merge_validation_checks(
    release_dir, list(schema = "passed"),
    "resources/generators/gwas-ssf-hybrid/generate.R"
  )
  cat(sprintf(
    "Release bundle schema check passed for %d analyses (registry structure + opengwasdb shared core)\n",
    nrow(analyses)
  ))
}

merge_validation_checks <- function(release_dir, checks, validator_name) {
  path <- file.path(release_dir, "validation.yaml")
  current <- if (file.exists(path)) read_yaml(path) else list(status = "not_run", checks = list())
  known_checks <- c("schema", "files", "reader_smoke_test", "effect_scale", "sd_estimation")
  for (key in intersect(names(checks), known_checks)) current$checks[[key]] <- checks[[key]]
  current$warnings <- as.list(unique(c(unlist(as.list(current$warnings)), checks$warnings)))
  current$errors <- as.list(unique(c(unlist(as.list(current$errors)), checks$errors)))

  status_rank <- c(not_run = 0L, passed = 1L, passed_with_warnings = 2L, failed = 3L)
  check_values <- unlist(current$checks[!vapply(current$checks, is.null, logical(1))])
  check_values <- check_values[check_values %in% names(status_rank)]
  if (length(check_values)) current$status <- names(which.max(status_rank[check_values]))
  current$validated_at <- format(Sys.time(), "%Y-%m-%dT%H:%M:%SZ", tz = "UTC")
  current$validator <- list(name = validator_name, version = NULL)
  write_yaml_file(current, path)
}

refresh_build_mode <- function(cfg, root) {
  release_dir <- path_abs(root, cfg$output$release_dir)
  paths <- artifact_paths(cfg, root)
  write_build_yaml(cfg, root, release_dir, paths)
  cat(sprintf("Refreshed build.yaml for %s\n", release_dir))
}

# resources/lib/effect_scale_validation.R's phenotype-SD estimator shells out
# to a Python bridge with every retained variant's se/af/beta serialised as a
# JSON array on the command line (estimate_phenotype_sd()) -- fine for
# Ragged's sparse per-Analysis subset (hundreds to low-thousands of
# variants), but a full genome-wide harmonised file easily retains hundreds
# of thousands of variants after MAF/palindrome filtering, and the
# estimated_from_source_maf/estimated_from_reference_maf tiers serialise
# *both* an --se and an --af JSON array onto the same command line. Confirmed
# empirically against this system (not guessed): 3,000 realistic-precision
# se+af pairs (~111 KB serialised total) succeeds, 4,000 (~148 KB) reliably
# fails with "cannot popen" -- well below this system's actual OS ARG_MAX
# (2 MB), so this is an R/libc popen() command-string limit, not the shell
# argument-list-length framing the error text suggests. Rather than changing
# the shared engine's contract (used unchanged by gwas-ssf-ragged, whose
# sparse per-Analysis files never approach this), this samples a bounded,
# deterministic subset of each downloaded file's rows before handing it to
# run_effect_scale_stage(). SD_ESTIMATION_MAX_VARIANTS keeps a wide safety
# margin under the observed failure point while staying far above
# thresholds$min_overlap_variants (a robust median-implied-SD estimate needs
# nowhere near this many sites).
SD_ESTIMATION_MAX_VARIANTS <- 2000L

thin_for_sd_estimation <- function(full_path, out_path, max_variants = SD_ESTIMATION_MAX_VARIANTS) {
  wanted_cols <- c(
    "chromosome", "base_pair_location", "effect_allele", "other_allele",
    "beta", "standard_error", "effect_allele_frequency"
  )
  header <- names(fread(full_path, nrows = 0))
  cols <- intersect(wanted_cols, header)
  rows <- fread(full_path, select = cols, showProgress = FALSE)
  rows <- rows[!is.na(beta) & !is.na(standard_error)]
  if (nrow(rows) > max_variants) {
    set.seed(1L)  # deterministic sample -- reproducible across re-runs
    rows <- rows[sample.int(nrow(rows), max_variants)]
  }
  fwrite(rows, out_path, sep = "\t")
  nrow(rows)
}

# Reference-AF phenotype-SD estimation for quantitative Analyses (issue #70):
# this multi-publication pool has no per-family curated effect-scale method
# the way a single-paper Store Family does, so a quantitative Analysis's
# original_sd_method has to be established empirically rather than assumed.
# Reuses resources/lib/effect_scale_validation.R's engine unchanged --
# already proven end-to-end by gwas-ssf-ragged -- pointed at a bounded
# subsample of each full harmonised file (thin_for_sd_estimation() above),
# not the full file itself.
effect_scale_stage <- function(cfg, root) {
  release_dir <- path_abs(root, cfg$output$release_dir)
  analyses_path <- file.path(release_dir, "analyses.tsv")
  paths <- artifact_paths(cfg, root)
  analyses <- fread(analyses_path, sep = "\t", na.strings = "", colClasses = list(character = "exclude_from_build"))
  analyses[, sd_estimation_file := sprintf("%s.sd-sample.tsv.gz", analysis_id)]
  for (i in seq_len(nrow(analyses))) {
    row <- analyses[i]
    n_sampled <- thin_for_sd_estimation(
      file.path(paths$download_dir, row$downloaded_file),
      file.path(paths$download_dir, row$sd_estimation_file)
    )
    cat(sprintf("[%d/%d] %s: sampled %d variants for SD estimation\n", i, nrow(analyses), row$analysis_id, n_sampled))
  }
  fwrite(analyses, analyses_path, sep = "\t", na = "")

  reference_resources <- resolve_reference_resources_from_config(cfg)
  thresholds <- default_thresholds(cfg$effect_scale_validation)
  result <- run_effect_scale_stage(
    release_dir, paths$download_dir, reference_resources, thresholds,
    filtered_file_col = "sd_estimation_file"
  )

  # A quantitative Analysis whose empirical SD estimation is skipped or fails
  # leaves original_sd_method=unavailable (run_effect_scale_stage()'s initial
  # value for every non-declared_standardised row that never got a passing
  # estimate) -- opengwasdb's builder raises loudly on that value rather than
  # silently defaulting (issue #18), so such a row is excluded from this
  # build rather than crashing the whole 10-Analysis batch over one
  # unresolved row, matching this schema's own documented exclude_from_build
  # semantics ("retained for audit but intentionally skipped by the build").
  analyses <- result$analyses
  newly_unavailable <- analyses$original_sd_method == "unavailable" &
    (is.na(analyses$exclude_from_build) | analyses$exclude_from_build != "true")
  if (any(newly_unavailable)) {
    analyses[newly_unavailable, exclude_from_build := "true"]
    analyses[newly_unavailable, inclusion_reason := paste0(
      inclusion_reason, "; excluded_reason: phenotype-SD estimation unavailable (checks.sd_estimation)"
    )]
    fwrite(analyses, file.path(release_dir, "analyses.tsv"), sep = "\t", na = "")
    cat(sprintf(
      "%d analysis(es) excluded from build: phenotype-SD estimation unavailable\n",
      sum(newly_unavailable)
    ))
  }
  set_sd_estimation_sidecar_pointer(release_dir)
  merge_validation_checks(
    release_dir, result$checks,
    "resources/generators/gwas-ssf-hybrid/generate.R"
  )
  status_counts <- table(result$sidecar$status)
  cat(sprintf(
    "Effect-scale validation: %d analyses (%s); checks.effect_scale=%s checks.sd_estimation=%s\n",
    nrow(result$sidecar),
    paste(sprintf("%s=%d", names(status_counts), status_counts), collapse = ", "),
    result$checks$effect_scale, result$checks$sd_estimation
  ))
  invisible(result)
}

args <- parse_args(commandArgs(trailingOnly = TRUE))
root <- repo_root()
source(path_abs(root, "resources/lib/gwas_catalog_ssf_url.R"))
source(path_abs(root, "resources/lib/effect_scale_validation.R"))
source(path_abs(root, "resources/lib/effect_scale_stage_yaml.R"))
source(path_abs(root, "resources/lib/build_environment.R"))
source(path_abs(root, "resources/lib/schema_validate.R"))
source(path_abs(root, "resources/lib/metadata_resolvers/ontology_contract.R"))
source(path_abs(root, "resources/lib/metadata_resolvers/canonical_trait_table.R"))
cfg <- read_yaml(path_abs(root, args$config))
cfg$.config_path <- args$config

if (args$mode == "emit") {
  emit_bundle(cfg, root)
  validate_emit(cfg, root)
} else if (args$mode == "download") {
  download_release(cfg, root, args$max_analyses, args$only_analysis_id)
} else if (args$mode == "validate") {
  validate_emit(cfg, root)
} else if (args$mode == "refresh-build") {
  refresh_build_mode(cfg, root)
} else if (args$mode == "effect-scale") {
  effect_scale_stage(cfg, root)
} else if (args$mode == "all") {
  emit_bundle(cfg, root)
  validate_emit(cfg, root)
  download_release(cfg, root, args$max_analyses, args$only_analysis_id)
} else {
  stop("Unknown --mode=", args$mode)
}
