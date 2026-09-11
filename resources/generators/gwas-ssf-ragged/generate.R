suppressPackageStartupMessages({
  library(data.table)
  library(yaml)
  library(parallel)
})

`%||%` <- function(x, y) if (is.null(x) || length(x) == 0 || is.na(x)) y else x

parse_args <- function(args) {
  out <- list(mode = "emit", config = NULL, max_analyses = NA_integer_,
              only_analysis_id = "", parallel_workers = NA_integer_)
  for (arg in args) {
    if (grepl("^--config=", arg)) out$config <- sub("^--config=", "", arg)
    else if (grepl("^--mode=", arg)) out$mode <- sub("^--mode=", "", arg)
    else if (grepl("^--max-analyses=", arg)) {
      out$max_analyses <- as.integer(sub("^--max-analyses=", "", arg))
    } else if (grepl("^--only-analysis-id=", arg)) {
      out$only_analysis_id <- sub("^--only-analysis-id=", "", arg)
    } else if (grepl("^--parallel-workers=", arg)) {
      out$parallel_workers <- as.integer(sub("^--parallel-workers=", "", arg))
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
  if (!is.null(cfg$output$artifact_root)) {
    artifact_root <- path_abs(root, cfg$output$artifact_root)
    artifact_subdir <- cfg$output$artifact_subdir
    if (is.null(artifact_subdir) || !nzchar(artifact_subdir)) {
      stop("output.artifact_subdir is required when output.artifact_root is set")
    }
    if (grepl("^/", artifact_subdir)) {
      stop("output.artifact_subdir must be relative to output.artifact_root")
    }
    artifact_dir <- file.path(artifact_root, artifact_subdir)
  } else if (!is.null(cfg$output$data_dir)) {
    artifact_dir <- path_abs(root, cfg$output$data_dir)
    artifact_root <- dirname(artifact_dir)
    artifact_subdir <- basename(artifact_dir)
  } else {
    stop("Expected output.artifact_root/output.artifact_subdir or legacy output.data_dir")
  }

  list(
    artifact_root = artifact_root,
    artifact_subdir = artifact_subdir,
    artifact_dir = artifact_dir,
    filtered_dir = file.path(artifact_dir, "filtered"),
    work_dir = file.path(artifact_dir, "work"),
    store_uri = file.path(artifact_dir, "store", cfg$store_key)
  )
}

clean_chr <- function(x) sub("^chr", "", as.character(x), ignore.case = TRUE)

slugify <- function(x) {
  x <- gsub("[^A-Za-z0-9]+", "-", x)
  x <- gsub("(^-+|-+$)", "", x)
  ifelse(nzchar(x), x, "analysis")
}

sha256_file <- function(path) {
  unname(strsplit(system2("sha256sum", path, stdout = TRUE), "\\s+")[[1]][1])
}

script_version <- function(root) {
  path <- path_abs(root, "resources/generators/gwas-ssf-ragged/generate.R")
  paste0("sha256:", sha256_file(path))
}

sparse_region_columns <- c(
  "analysis_id", "region_id", "region_kind", "chromosome", "start", "end",
  "source_genome_build", "target_id", "target_label", "lead_variant_id",
  "pvalue_threshold", "n_variants_retained", "region_policy_id"
)

empty_sparse_regions <- function() {
  data.table(
    analysis_id = character(),
    region_id = character(),
    region_kind = character(),
    chromosome = character(),
    start = integer(),
    end = integer(),
    source_genome_build = character(),
    target_id = character(),
    target_label = character(),
    lead_variant_id = character(),
    pvalue_threshold = character(),
    n_variants_retained = integer(),
    region_policy_id = character()
  )
}

normalise_sparse_regions <- function(regions) {
  # NULL elements (a region kind that had zero hits for this Analysis) must
  # be dropped before the length check below, not just before rbindlist:
  # rbindlist(list(NULL, NULL), fill=TRUE) returns a degenerate 0-row/0-col
  # data.table, and assigning a scalar column onto that via `:=` silently
  # produces one spurious all-blank row instead of staying empty.
  regions <- Filter(Negate(is.null), regions)
  if (!length(regions)) return(empty_sparse_regions())
  out <- rbindlist(regions, fill = TRUE)
  for (col in setdiff(sparse_region_columns, names(out))) out[, (col) := ""]
  out[, ..sparse_region_columns]
}

merge_intervals_dt <- function(dt) {
  if (nrow(dt) == 0) return(dt)
  setorder(dt, chromosome, start, end)
  out <- list()
  for (ch in unique(dt$chromosome)) {
    x <- dt[chromosome == ch]
    cur_start <- x$start[1]
    cur_end <- x$end[1]
    rows <- list()
    for (i in seq_len(nrow(x))[-1]) {
      if (x$start[i] <= cur_end) {
        cur_end <- max(cur_end, x$end[i])
      } else {
        rows[[length(rows) + 1]] <- data.table(chromosome = ch, start = cur_start, end = cur_end)
        cur_start <- x$start[i]
        cur_end <- x$end[i]
      }
    }
    rows[[length(rows) + 1]] <- data.table(chromosome = ch, start = cur_start, end = cur_end)
    out[[length(out) + 1]] <- rbindlist(rows)
  }
  rbindlist(out)
}

pick_leads <- function(dt, min_bp) {
  if (nrow(dt) == 0) return(integer())
  setorder(dt, p_value)
  kept_pos <- new.env(parent = emptyenv())
  keep <- integer()
  for (i in seq_len(nrow(dt))) {
    ch <- dt$chromosome[i]
    pos <- dt$base_pair_location[i]
    prev <- get0(ch, envir = kept_pos, ifnotfound = numeric())
    if (!length(prev) || min(abs(prev - pos)) > min_bp) {
      keep <- c(keep, dt$row_id[i])
      assign(ch, c(prev, pos), envir = kept_pos)
    }
  }
  keep
}

write_yaml_file <- function(x, path) {
  dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)
  writeLines(sub("\n+$", "", as.yaml(x)), path)
}

# A Store Family declares `inputs.analysis_targets` only when its Analyses
# have a single encoding gene to derive a cis window from (SomaScan-style
# proteomics). Families with no such target (e.g. small-molecule metabolomics,
# issue #26) omit it entirely, and the generator retains only signal-derived
# (significant/suggestive trans) regions rather than fabricating a cis target.
has_gene_targets <- function(cfg) !is.null(cfg$inputs$analysis_targets)

empty_target_summary <- function() {
  data.table(
    source_analysis_id = character(), trait_id = character(), gene_id = character(),
    gene_name = character(), target_id = character(), target_label = character(),
    trait_chr = character(), trait_bp = integer(), mhc = logical(),
    target_resolution_method = character(), n_target_rows = integer()
  )
}

empty_targets_sidecar <- function() {
  data.table(
    source_analysis_id = character(), chromosome = character(),
    gene_start = integer(), gene_end = integer(), ensembl_gene_id = character(),
    gene_name = character(), mapping_status = character(),
    trait_ontology_id = character(), mhc = logical(), target_resolution_method = character()
  )
}

load_inputs <- function(cfg, root) {
  candidates <- fread(path_abs(root, cfg$inputs$candidates), sep = "\t", na.strings = "")
  targets <- if (has_gene_targets(cfg)) {
    fread(path_abs(root, cfg$inputs$analysis_targets), sep = "\t", na.strings = "")
  } else {
    empty_targets_sidecar()
  }
  list(candidates = candidates, targets = targets)
}

select_analyses <- function(cfg, candidates) {
  selected <- candidates[PUBMED.ID == as.integer(cfg$selection$pubmed_id)]
  if (!is.null(cfg$selection$store_key)) {
    selected <- selected[store_key == cfg$selection$store_key]
  }
  if (!is.null(cfg$selection$ancestry_groups)) {
    # Multi-ancestry slice (issue #27): a fixed number of analyses per
    # configured ancestry group, e.g. a small cross-ancestry pilot proving AF-
    # based ancestry assignment discriminates rather than always agreeing
    # with the source label. Additive: single-`ancestry_group` families
    # (pqtl-interval-2018) are unaffected and keep using the branch below.
    groups <- unlist(cfg$selection$ancestry_groups)
    per_group <- as.integer(cfg$selection$n_analyses_per_ancestry %||% cfg$selection$n_analyses)
    selected <- selected[ancestry_group %in% groups]
    setorder(selected, ancestry_group)
    selected <- selected[, .SD[seq_len(min(per_group, .N))], by = ancestry_group]
  } else {
    if (!is.null(cfg$selection$ancestry_group)) {
      selected <- selected[ancestry_group == cfg$selection$ancestry_group]
    }
    if (!is.null(cfg$selection$n_analyses)) {
      selected <- selected[seq_len(min(as.integer(cfg$selection$n_analyses), .N))]
    }
  }
  if (!nrow(selected)) stop("Selection produced zero analyses")
  selected[, analysis_index := .I - 1L]
  selected[]
}

summarise_targets <- function(targets, selected_ids, fail_unresolved = TRUE) {
  target_subset <- targets[source_analysis_id %in% selected_ids]
  target_subset[, chromosome := clean_chr(chromosome)]
  target_subset[, source_analysis_id := as.character(source_analysis_id)]

  mapped <- target_subset[
    mapping_status != "unresolved" &
      !is.na(chromosome) & !is.na(gene_start) & !is.na(gene_end)
  ]
  missing <- setdiff(selected_ids, mapped$source_analysis_id)
  if (length(missing) && fail_unresolved) {
    stop(
      "Selected analyses lack resolvable target coordinates: ",
      paste(missing, collapse = ", ")
    )
  }

  mapped[, .(
    trait_id = paste(unique(na.omit(trait_ontology_id)), collapse = ";"),
    gene_id = paste(unique(na.omit(ensembl_gene_id)), collapse = ";"),
    gene_name = paste(unique(na.omit(gene_name)), collapse = ";"),
    target_id = paste(unique(na.omit(ensembl_gene_id)), collapse = ";"),
    target_label = paste(unique(na.omit(gene_name)), collapse = ";"),
    trait_chr = chromosome[order(chromosome, gene_start)][1],
    trait_bp = min(gene_start, na.rm = TRUE),
    mhc = any(mhc %in% c(TRUE, "TRUE", "true", "1"), na.rm = TRUE),
    target_resolution_method = paste(unique(na.omit(target_resolution_method)), collapse = ";"),
    n_target_rows = .N
  ), by = source_analysis_id]
}

manifest_rows <- function(cfg, selected, target_summary, paths, has_targets = TRUE, canonical_table = NULL) {
  x <- merge(
    selected,
    target_summary,
    by.x = "STUDY.ACCESSION",
    by.y = "source_analysis_id",
    all.x = TRUE,
    sort = FALSE
  )
  setorder(x, analysis_index)

  slug_source <- if (has_targets) x$gene_name else x$STUDY.ACCESSION
  x[, gene_name_slug := slugify(slug_source)]
  x[, filtered_file := sprintf("%s_%s.filtered.tsv.gz", gene_name_slug, STUDY.ACCESSION)]
  x[, source_file := file.path(paths$filtered_dir, filtered_file)]
  x[, source_url := ssf_url(STUDY.ACCESSION, cfg$source$ftp_base)]
  x[, source_bundle_id := ""]
  x[, source_ancestry_label := ancestry_group]
  x[, assigned_ancestry := ancestry_group]
  x[, ancestry_assignment_method := cfg$defaults$ancestry_assignment_method %||% "source_trusted_no_af"]
  x[, original_effect_scale := cfg$defaults$original_effect_scale %||% "sd"]
  x[, original_sd := ""]
  x[, original_sd_method := cfg$defaults$original_sd_method %||% "declared_standardised"]
  x[, stored_effect_scale := cfg$defaults$stored_effect_scale %||% "sd"]
  x[, sample_size_kind := ifelse(study_design == "case-control", "case_control", "total")]
  x[, sample_size_scope := cfg$defaults$sample_size_scope %||% "analysis_level"]
  x[, license := cfg$defaults$license]
  x[, publication_doi := cfg$defaults$publication_doi %||% ""]
  x[, publication_pmid := as.character(PUBMED.ID)]
  x[, consortium := cfg$defaults$consortium %||% ""]
  x[, source_genome_build := cfg$defaults$source_genome_build %||% "GRCh38"]
  x[, analysis_group_id := cfg$defaults$analysis_group_id %||% ""]
  x[, inclusion_reason := cfg$defaults$inclusion_reason %||% ""]
  x[, exclude_from_build := ""]
  x[, checksum := ""]
  x[, checksum_algorithm := "sha256"]
  x[, size_bytes := ""]
  x[, tissue := cfg$defaults$tissue %||% ""]
  x[, context := cfg$defaults$context %||% ""]
  x[, trait_ontology_id_curie := obo_uri_to_curie(MAPPED_TRAIT_URI)]

  # Trait Ontology Mapping (CONTEXT.md): GWAS Catalog supplies MAPPED_TRAIT/
  # MAPPED_TRAIT_URI for every currently-built bundle here, so this resolves
  # as source_provided today; the canonical_table fallback exists for a
  # future GWAS Catalog study with no mapping, not a case this repo's real
  # data currently exercises.
  ontology_resolved <- resolve_trait_ontology_mappings(
    trait_labels = x$DISEASE.TRAIT,
    source_ontology_ids = x$trait_ontology_id_curie,
    source_ontology_labels = x$MAPPED_TRAIT,
    canonical_table = canonical_table
  )
  x[, trait_ontology_id := ontology_resolved$trait_ontology_id]
  x[, trait_ontology_label := ontology_resolved$trait_ontology_label]
  x[, trait_ontology_mapping_method := ontology_resolved$trait_ontology_mapping_method]
  if (has_targets) x[, n := sample_size]

  # Single-gene-target columns (trait_id, gene_id/name, cis coordinates, MHC
  # flag, resolution provenance) only exist for families with a resolvable
  # per-Analysis gene target (issue #26). A no-cis family (e.g. metabolomics)
  # never emits these rather than filling them with placeholder/NA values;
  # everything else keeps the same column order regardless.
  target_cols_after_trait_ontology <- if (has_targets) "trait_id" else character()
  target_cols_after_exclude <- if (has_targets) c("gene_id", "gene_name", "trait_chr", "trait_bp", "n") else character()
  target_cols_after_context <- if (has_targets) c("mhc", "target_resolution_method", "n_target_rows") else character()

  out_cols <- c(
    "analysis_index",
    "analysis_id" = "STUDY.ACCESSION",
    "source_analysis_id" = "STUDY.ACCESSION",
    "source_label" = "DISEASE.TRAIT",
    "analysis_label" = "DISEASE.TRAIT",
    "trait_ontology_label",
    "trait_ontology_id",
    "trait_ontology_mapping_method",
    target_cols_after_trait_ontology,
    "source_file",
    "source_url",
    "source_bundle_id",
    "filtered_file",
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
    "exclude_from_build",
    target_cols_after_exclude,
    "tissue",
    "context",
    target_cols_after_context
  )
  out_names <- ifelse(nzchar(names(out_cols)), names(out_cols), out_cols)
  ans <- data.table()
  for (i in seq_along(out_cols)) {
    ans[, (out_names[i]) := x[[out_cols[i]]]]
  }
  ans[]
}

find_reference_resource_root <- function(cfg, resource_id) {
  declared <- cfg$reference_resources %||% list()
  for (res in declared) {
    if (identical(res$resource_id, resource_id)) return(res$root)
  }
  stop("Reference resource not declared in reference_resources: ", resource_id)
}

build_qc_panel_yaml <- function(cfg) {
  qc <- cfg$filter$qc_panel
  if (is.null(qc)) return(NULL)
  list(
    enabled = qc$enabled %||% TRUE,
    resource_id = qc$resource_id
  )
}

build_ancestry_assignment_yaml <- function(cfg) {
  aa <- cfg$ancestry_assignment
  if (is.null(aa)) return(NULL)
  list(
    enabled = aa$enabled %||% TRUE,
    reference_resource_id = aa$reference_resource_id,
    maf_floor = aa$maf_floor %||% 0.01,
    gates = list(
      tau = aa$gates$tau %||% 0.50,
      delta = aa$gates$delta %||% 0.20,
      n_min = aa$gates$n_min %||% 5000L,
      residual_max = aa$gates$residual_max %||% 0.06
    )
  )
}

write_build_yaml <- function(cfg, root, release_dir, paths) {
  write_yaml_file(list(
    store_family_id = cfg$store_family_id,
    family_release_id = cfg$family_release_id,
    store_layout = "ragged-observed",
    completion_state = "observed-only",
    build = list(
      command = "build-ragged-ssf",
      arguments = list(
        "store-id" = cfg$store_family_id,
        "release-id" = cfg$family_release_id
      )
    ),
    source = list(
      root = paths$filtered_dir,
      analyses = "analyses.tsv",
      source_format = "gwas-ssf",
      source_reader_capability = "opengwasdb.gwas-ssf",
      source_genome_build = cfg$defaults$source_genome_build
    ),
    normalisation = list(
      target_reference_assembly = "GRCh38",
      liftover = NULL
    ),
    effects = list(stored_effect_scale = cfg$defaults$stored_effect_scale),
    shape = list(
      association_coverage = cfg$association_coverage,
      ragged_region_policy = cfg$filter$region_policy_id
    ),
    reference_resources = build_reference_resources_yaml(cfg),
    effect_scale_validation = build_effect_scale_validation_yaml(cfg),
    ancestry_assignment = build_ancestry_assignment_yaml(cfg),
    qc_panel = build_qc_panel_yaml(cfg),
    validation = list(required = TRUE),
    artifacts = list(
      artifact_root = paths$artifact_root,
      release_subdir = paths$artifact_subdir,
      filtered_dir = paths$filtered_dir,
      work_dir = paths$work_dir,
      store_uri = paths$store_uri,
      build_log_uri = NULL
    )
  ), file.path(release_dir, "build.yaml"))
}

emit_bundle <- function(cfg, root) {
  inputs <- load_inputs(cfg, root)
  selected <- select_analyses(cfg, inputs$candidates)
  has_targets <- has_gene_targets(cfg)
  target_summary <- if (has_targets) {
    summarise_targets(
      inputs$targets,
      selected$STUDY.ACCESSION,
      cfg$selection$fail_if_target_unresolved %||% TRUE
    )
  } else {
    empty_target_summary()
  }
  paths <- artifact_paths(cfg, root)
  canonical_table <- load_canonical_trait_table_from_cfg(cfg$reference_resources, root)
  analyses <- manifest_rows(cfg, selected, target_summary, paths, has_targets = has_targets, canonical_table = canonical_table)

  release_dir <- path_abs(root, cfg$output$release_dir)
  sidecar_dir <- file.path(release_dir, "sidecars")
  dir.create(sidecar_dir, recursive = TRUE, showWarnings = FALSE)
  fwrite(analyses, file.path(release_dir, "analyses.tsv"), sep = "\t", na = "")

  sidecars_list <- list(
    sparse_regions = "sidecars/sparse_regions.tsv",
    filter_summary = "sidecars/filter_summary.tsv",
    build_report = "sidecars/build_report.tsv",
    sd_estimation = "sidecars/sd_estimation.tsv",
    ancestry = "sidecars/ancestry.tsv"
  )
  if (has_targets) {
    target_subset <- inputs$targets[source_analysis_id %in% selected$STUDY.ACCESSION]
    fwrite(target_subset, file.path(sidecar_dir, "analysis_targets.tsv"), sep = "\t", na = "")
    sidecars_list <- c(list(analysis_targets = "sidecars/analysis_targets.tsv"), sidecars_list)
  }
  fwrite(empty_sparse_regions(), file.path(sidecar_dir, "sparse_regions.tsv"), sep = "\t")

  command <- paste("Rscript resources/generators/gwas-ssf-ragged/generate.R",
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
      name = "resources/generators/gwas-ssf-ragged/generate.R",
      version = script_version(root),
      command = command
    ),
    build_environment = build_environment_info(root),
    source_defaults = list(
      source_genome_build = cfg$defaults$source_genome_build,
      license = cfg$defaults$license,
      original_effect_scale = cfg$defaults$original_effect_scale,
      stored_effect_scale = cfg$defaults$stored_effect_scale,
      sample_size_kind = "total",
      source_ancestry_label = cfg$selection$ancestry_group,
      assigned_ancestry = cfg$selection$ancestry_group
    ),
    lineage = list(derived_from = NULL),
    sidecars = sidecars_list,
    notes = cfg$notes %||% ""
  ), file.path(release_dir, "release.yaml"))

  write_build_yaml(cfg, root, release_dir, paths)

  write_yaml_file(list(
    status = "not_run",
    validated_at = NULL,
    validator = list(name = "resources/generators/gwas-ssf-ragged/generate.R", version = NULL),
    checks = list(
      schema = "not_run",
      files = "not_run",
      reader_smoke_test = "not_run",
      ancestry = "not_run",
      effect_scale = "not_run",
      sd_estimation = "not_run",
      sparse_regions = "not_run"
    ),
    reports = list(),
    warnings = list(),
    errors = list()
  ), file.path(release_dir, "validation.yaml"))

  cat(sprintf("Emitted %d analyses to %s\n", nrow(analyses), release_dir))
  invisible(analyses)
}

refresh_artifact_paths <- function(cfg, root) {
  release_dir <- path_abs(root, cfg$output$release_dir)
  paths <- artifact_paths(cfg, root)
  analyses_path <- file.path(release_dir, "analyses.tsv")
  if (!file.exists(analyses_path)) stop("Missing analyses.tsv: ", analyses_path)

  analyses <- fread(analyses_path, sep = "\t", na.strings = "")
  if (!"filtered_file" %in% names(analyses)) {
    stop("analyses.tsv must contain filtered_file to refresh artifact paths")
  }
  analyses[, source_file := file.path(paths$filtered_dir, filtered_file)]
  fwrite(analyses, analyses_path, sep = "\t", na = "")

  summary_path <- file.path(release_dir, "sidecars", "filter_summary.tsv")
  if (file.exists(summary_path)) {
    summary <- fread(summary_path, sep = "\t", na.strings = "")
    source_files <- analyses$source_file
    names(source_files) <- analyses$analysis_id
    if ("output_file" %in% names(summary)) {
      summary[, output_file := source_files[analysis_id]]
      fwrite(summary, summary_path, sep = "\t", na = "")
    }
  }

  write_build_yaml(cfg, root, release_dir, paths)
  cat(sprintf("Refreshed artifact paths under %s\n", paths$artifact_dir))
  invisible(paths)
}

region_counts <- function(ssf_rows, ranges) {
  if (!nrow(ranges)) return(integer())
  vapply(seq_len(nrow(ranges)), function(i) {
    sum(ssf_rows$chromosome == ranges$chromosome[i] &
          ssf_rows$base_pair_location >= ranges$start[i] &
          ssf_rows$base_pair_location <= ranges$end[i])
  }, integer(1))
}

# Loads the fixed QC panel (issue #28/#29) a Store Family opts into via
# `filter.qc_panel`, keyed off a `kind: qc_panel` entry in `reference_resources`
# (mirroring how ancestry_assignment/effect_scale_validation reference a
# declared Reference Resource rather than a raw path). Returns NULL when a
# family does not configure a QC panel, so retention stays purely additive.
load_qc_panel <- function(cfg, root) {
  qc <- cfg$filter$qc_panel
  if (is.null(qc) || !isTRUE(qc$enabled %||% TRUE)) return(NULL)
  panel_path <- path_abs(root, find_reference_resource_root(cfg, qc$resource_id))
  panel <- fread(panel_path, sep = "\t", na.strings = "")
  panel[, .(chromosome = clean_chr(chromosome), position = as.integer(position))]
}

process_one <- function(row, targets, cfg, root, filtered_dir, work_dir, qc_panel = NULL) {
  analysis_id <- row$analysis_id
  url <- row$source_url
  full_download <- file.path(work_dir, paste0(analysis_id, ".h.tsv.gz"))
  filtered_output <- file.path(filtered_dir, row$filtered_file)
  range_policy <- cfg$filter$region_policy_id
  dir.create(filtered_dir, recursive = TRUE, showWarnings = FALSE)
  dir.create(work_dir, recursive = TRUE, showWarnings = FALSE)
  on.exit({
    if (file.exists(full_download)) unlink(full_download)
  }, add = TRUE)

  t0 <- Sys.time()
  options(timeout = cfg$filter$download_timeout_seconds %||% 3600)
  download.file(url, full_download, mode = "wb", quiet = TRUE)
  t_download <- as.numeric(difftime(Sys.time(), t0, units = "secs"))
  downloaded_bytes <- file.size(full_download)

  t1 <- Sys.time()
  header <- names(fread(full_download, nrows = 0))
  selected_columns <- c(
    "chromosome", "base_pair_location", "effect_allele", "other_allele",
    "beta", "standard_error", "p_value", "effect_allele_frequency",
    "variant_id", "rsid"
  )
  selected_columns <- intersect(selected_columns, header)
  required_source_columns <- c(
    "chromosome", "base_pair_location", "effect_allele", "other_allele",
    "beta", "standard_error", "p_value"
  )
  missing_source_columns <- setdiff(required_source_columns, selected_columns)
  if (length(missing_source_columns)) {
    stop("Source file missing required columns: ", paste(missing_source_columns, collapse = ", "))
  }
  ssf_rows <- fread(
    full_download,
    select = selected_columns,
    colClasses = list(character = "chromosome"),
    showProgress = FALSE
  )
  ssf_rows <- ssf_rows[!is.na(base_pair_location) & !is.na(p_value)]
  ssf_rows[, chromosome := clean_chr(chromosome)]
  ssf_rows[, row_id := .I]
  n_in <- nrow(ssf_rows)

  target_rows <- targets[source_analysis_id == analysis_id &
                           mapping_status != "unresolved" &
                           !is.na(chromosome) & !is.na(gene_start) & !is.na(gene_end)]
  target_rows[, chromosome := clean_chr(chromosome)]
  cis <- target_rows[, .(
    chromosome,
    start = pmax(1L, as.integer(gene_start - cfg$filter$cis_flank_bp)),
    end = as.integer(gene_end + cfg$filter$cis_flank_bp),
    target_id = ensembl_gene_id,
    target_label = gene_name
  )]
  cis_merged <- merge_intervals_dt(cis[, .(chromosome, start, end)])
  ssf_rows[, is_cis := FALSE]
  for (i in seq_len(nrow(cis_merged))) {
    ssf_rows[chromosome == cis_merged$chromosome[i] &
        base_pair_location >= cis_merged$start[i] &
        base_pair_location <= cis_merged$end[i], is_cis := TRUE]
  }

  significant_trans_hits <- ssf_rows[p_value <= cfg$filter$significant_p & !is_cis]
  trans_ranges <- data.table(chromosome = character(), start = integer(), end = integer())
  ssf_rows[, in_trans := FALSE]
  if (nrow(significant_trans_hits)) {
    trans_ranges <- merge_intervals_dt(significant_trans_hits[, .(
      chromosome,
      start = pmax(1L, as.integer(base_pair_location - cfg$filter$trans_flank_bp)),
      end = as.integer(base_pair_location + cfg$filter$trans_flank_bp)
    )])
    for (i in seq_len(nrow(trans_ranges))) {
      ssf_rows[chromosome == trans_ranges$chromosome[i] &
          base_pair_location >= trans_ranges$start[i] &
          base_pair_location <= trans_ranges$end[i], in_trans := TRUE]
    }
  }

  suggestive_trans_candidates <- ssf_rows[
    p_value > cfg$filter$significant_p &
      p_value <= cfg$filter$suggestive_p &
      !is_cis & !in_trans
  ]
  sugg_ids <- pick_leads(
    suggestive_trans_candidates[, .(chromosome, base_pair_location, p_value, row_id)],
    cfg$filter$suggestive_lead_min_distance_bp
  )
  ssf_rows[, is_suggestive_lead := row_id %in% sugg_ids]

  # Unconditional QC-panel retention (issue #28/#30): additive to the
  # signal-driven regions above, so an Analysis with few or no significant
  # hits still keeps a stable, ancestry-informative variant backbone for the
  # effect-scale/ancestry stages. `qc_panel` is NULL for Store Families that
  # do not configure `filter.qc_panel`, in which case this is always FALSE
  # and behaviour is identical to before this issue.
  ssf_rows[, is_qc_panel := FALSE]
  if (!is.null(qc_panel) && nrow(qc_panel)) {
    ssf_rows[qc_panel, on = c("chromosome", "base_pair_location" = "position"), is_qc_panel := TRUE]
  }

  kept_rows <- ssf_rows[is_cis | in_trans | is_suggestive_lead | is_qc_panel]
  fwrite(kept_rows[, ..selected_columns], filtered_output, sep = "\t")
  unlink(full_download)
  t_filter <- as.numeric(difftime(Sys.time(), t1, units = "secs"))

  cis_region <- if (nrow(cis_merged)) {
    cis_counts <- region_counts(kept_rows, cis_merged)
    cis_merged[, .(
      analysis_id = analysis_id,
      region_id = sprintf("%s__cis__%03d", analysis_id, .I),
      region_kind = "cis",
      chromosome,
      start,
      end,
      source_genome_build = row$source_genome_build,
      target_id = paste(unique(na.omit(cis$target_id)), collapse = ";"),
      target_label = paste(unique(na.omit(cis$target_label)), collapse = ";"),
      lead_variant_id = "",
      pvalue_threshold = "",
      n_variants_retained = cis_counts,
      region_policy_id = range_policy
    )]
  } else NULL
  trans_region <- if (nrow(trans_ranges)) {
    trans_counts <- region_counts(kept_rows[in_trans == TRUE], trans_ranges)
    trans_ranges[, .(
      analysis_id = analysis_id,
      region_id = sprintf("%s__sig_trans__%03d", analysis_id, .I),
      region_kind = "significant_trans",
      chromosome,
      start,
      end,
      source_genome_build = row$source_genome_build,
      target_id = "",
      target_label = "",
      lead_variant_id = "",
      pvalue_threshold = as.character(cfg$filter$significant_p),
      n_variants_retained = trans_counts,
      region_policy_id = range_policy
    )]
  } else NULL
  suggestive_region <- if (length(sugg_ids)) {
    lead_variant <- if ("variant_id" %in% names(ssf_rows)) {
      as.character(ssf_rows$variant_id)
    } else {
      rep("", nrow(ssf_rows))
    }
    if ("rsid" %in% names(ssf_rows)) {
      rsid <- as.character(ssf_rows$rsid)
      lead_variant <- ifelse(is.na(lead_variant) | lead_variant == "", rsid, lead_variant)
    }
    lead_variant[is.na(lead_variant)] <- ""
    ssf_rows[, lead_variant_resolved := lead_variant]
    ssf_rows[row_id %in% sugg_ids, .(
      analysis_id = analysis_id,
      region_id = sprintf("%s__suggestive__%03d", analysis_id, seq_len(.N)),
      region_kind = "suggestive_trans",
      chromosome,
      start = as.integer(base_pair_location),
      end = as.integer(base_pair_location),
      source_genome_build = row$source_genome_build,
      target_id = "",
      target_label = "",
      lead_variant_id = lead_variant_resolved,
      pvalue_threshold = as.character(cfg$filter$suggestive_p),
      n_variants_retained = 1L,
      region_policy_id = range_policy
    )]
  } else NULL
  qc_panel_region <- if (kept_rows[, sum(is_qc_panel)] > 0) {
    kept_rows[is_qc_panel == TRUE, .(
      start = min(base_pair_location), end = max(base_pair_location), n_variants_retained = .N
    ), by = chromosome][, .(
      analysis_id = analysis_id,
      region_id = sprintf("%s__qc_panel__%03d", analysis_id, .I),
      region_kind = "qc_panel",
      chromosome,
      start,
      end,
      source_genome_build = row$source_genome_build,
      target_id = "",
      target_label = "",
      lead_variant_id = "",
      pvalue_threshold = "",
      n_variants_retained,
      region_policy_id = range_policy
    )]
  } else NULL
  ranges <- normalise_sparse_regions(list(cis_region, trans_region, suggestive_region, qc_panel_region))

  data <- data.table(
    analysis_id = analysis_id,
    status = "ok",
    error = "",
    attempt = 1L,
    retry_count = 0L,
    source_url = url,
    downloaded_bytes = downloaded_bytes,
    input_rows = n_in,
    retained_rows = nrow(kept_rows),
    retained_fraction = round(nrow(kept_rows) / n_in, 6),
    cis_rows = kept_rows[, sum(is_cis)],
    significant_trans_regions = nrow(trans_ranges),
    significant_trans_rows = kept_rows[, sum(in_trans)],
    suggestive_leads = length(sugg_ids),
    qc_panel_rows = kept_rows[, sum(is_qc_panel)],
    output_file = row$source_file,
    output_bytes = file.size(filtered_output),
    checksum_algorithm = "sha256",
    checksum = sha256_file(filtered_output),
    download_seconds = round(t_download, 1),
    filter_seconds = round(t_filter, 1),
    total_seconds = round(t_download + t_filter, 1)
  )
  list(summary = data, ranges = ranges)
}

# Conservative default worker count for `filter_release()`'s parallel
# analyses (issue #32). Each worker holds its own concurrent connection to
# EBI's public FTP, so this stays well below typical core counts rather than
# maxing out `parallel::detectCores()` — a handful of workers already
# reclaims most of the wall-clock win without risking rate-limiting/blocking
# from the remote host.
default_parallel_workers <- 4L

failed_summary_row <- function(row, message) {
  data.table(
    analysis_id = row$analysis_id,
    status = "failed",
    error = message,
    attempt = 1L,
    retry_count = 0L,
    source_url = row$source_url,
    downloaded_bytes = NA_real_,
    input_rows = NA_integer_,
    retained_rows = NA_integer_,
    retained_fraction = NA_real_,
    cis_rows = NA_integer_,
    significant_trans_regions = NA_integer_,
    significant_trans_rows = NA_integer_,
    suggestive_leads = NA_integer_,
    qc_panel_rows = NA_integer_,
    output_file = row$source_file,
    output_bytes = NA_real_,
    checksum_algorithm = "sha256",
    checksum = "",
    download_seconds = NA_real_,
    filter_seconds = NA_real_,
    total_seconds = NA_real_
  )
}

filter_release <- function(cfg, root, max_analyses = NA_integer_, only_analysis_id = "",
                            parallel_workers = NA_integer_) {
  release_dir <- path_abs(root, cfg$output$release_dir)
  analyses_path <- file.path(release_dir, "analyses.tsv")
  if (!file.exists(analyses_path)) emit_bundle(cfg, root)
  analyses <- fread(analyses_path, sep = "\t", na.strings = "")
  for (col in c("checksum", "size_bytes")) {
    if (!col %in% names(analyses)) analyses[, (col) := ""]
    analyses[, (col) := fifelse(is.na(as.character(get(col))), "", as.character(get(col)))]
  }
  targets_sidecar_path <- file.path(release_dir, "sidecars", "analysis_targets.tsv")
  targets <- if (file.exists(targets_sidecar_path)) {
    fread(targets_sidecar_path, sep = "\t", na.strings = "")
  } else {
    empty_targets_sidecar()
  }

  todo <- analyses
  if (nzchar(only_analysis_id)) {
    ids <- strsplit(only_analysis_id, ",", fixed = TRUE)[[1]]
    todo <- todo[analysis_id %in% ids]
  }
  if (!is.na(max_analyses)) todo <- todo[seq_len(min(max_analyses, .N))]
  partial_run <- !is.na(max_analyses) || nzchar(only_analysis_id)

  workers <- if (!is.na(parallel_workers)) {
    parallel_workers
  } else {
    as.integer(cfg$filter$parallel_workers %||% default_parallel_workers)
  }
  workers <- max(1L, min(workers, nrow(todo)))

  paths <- artifact_paths(cfg, root)
  filtered_dir <- paths$filtered_dir
  work_dir <- paths$work_dir
  dir.create(filtered_dir, recursive = TRUE, showWarnings = FALSE)
  dir.create(work_dir, recursive = TRUE, showWarnings = FALSE)
  qc_panel <- load_qc_panel(cfg, root)

  run_row <- function(i) {
    row <- todo[i]
    tryCatch(
      process_one(row, targets, cfg, root, filtered_dir, work_dir, qc_panel),
      error = function(e) {
        list(summary = failed_summary_row(row, conditionMessage(e)), ranges = NULL)
      }
    )
  }
  cat(sprintf("Filtering %d analyses with %d parallel worker(s)\n", nrow(todo), workers))
  results <- if (workers > 1L) {
    mclapply(seq_len(nrow(todo)), run_row, mc.cores = workers, mc.preschedule = FALSE)
  } else {
    lapply(seq_len(nrow(todo)), run_row)
  }
  # A forked worker that dies outright (e.g. OOM-killed) hands mclapply() a
  # bare condition object instead of run_row()'s usual list(summary=, ranges=)
  # — normal per-analysis errors are already caught inside run_row(). Convert
  # that case to an ordinary failed row rather than letting it corrupt
  # rbindlist() below or silently drop the analysis from the summary.
  results <- lapply(seq_along(results), function(i) {
    result <- results[[i]]
    if (is.list(result) && !inherits(result, "condition") && !is.null(result$summary)) {
      result
    } else {
      row <- todo[i]
      message <- if (inherits(result, "condition")) conditionMessage(result) else "worker exited unexpectedly"
      list(summary = failed_summary_row(row, message), ranges = NULL)
    }
  })

  summaries <- list()
  ranges <- list()
  for (i in seq_len(nrow(todo))) {
    row <- todo[i]
    result <- results[[i]]
    # `gene_name` only exists for target-resolving families (issue #26); for
    # a no-target family this label falls back to the analysis ID alone.
    progress_label <- if ("gene_name" %in% names(row)) row$gene_name else row$analysis_id
    cat(sprintf("[%d/%d] %s %s\n", i, nrow(todo), row$analysis_id, progress_label))
    if (identical(result$summary$status, "failed")) {
      cat("  ERROR: ", result$summary$error, "\n", sep = "")
    } else if (identical(result$summary$status, "ok")) {
      cat(sprintf(
        "  retained %s/%s rows (%.3f%%), output %.1f KB, %.1fs total\n",
        format(result$summary$retained_rows, big.mark = ","),
        format(result$summary$input_rows, big.mark = ","),
        100 * result$summary$retained_fraction,
        result$summary$output_bytes / 1000,
        result$summary$total_seconds
      ))
    }
    summaries[[length(summaries) + 1]] <- result$summary
    if (!is.null(result$ranges)) ranges[[length(ranges) + 1]] <- result$ranges
  }
  summary_dt <- rbindlist(summaries, fill = TRUE)
  ranges_dt <- normalise_sparse_regions(ranges)
  sidecar_dir <- file.path(release_dir, "sidecars")
  summary_name <- if (partial_run) "filter_summary.partial.tsv" else "filter_summary.tsv"
  ranges_name <- if (partial_run) "sparse_regions.partial.tsv" else "sparse_regions.tsv"
  fwrite(summary_dt, file.path(sidecar_dir, summary_name), sep = "\t", na = "")
  fwrite(ranges_dt, file.path(sidecar_dir, ranges_name), sep = "\t", na = "")

  if (!partial_run) {
    ok <- summary_dt[status == "ok"]
    for (i in seq_len(nrow(ok))) {
      analyses[analysis_id == ok$analysis_id[i], `:=`(
        checksum = ok$checksum[i],
        size_bytes = as.character(ok$output_bytes[i])
      )]
    }
    fwrite(analyses, analyses_path, sep = "\t", na = "")
    cat(sprintf("Filtered %d analyses; updated %s\n", nrow(summary_dt), analyses_path))
  } else {
    cat(sprintf(
      "Filtered %d analyses as a partial run; wrote %s and left %s unchanged\n",
      nrow(summary_dt), summary_name, analyses_path
    ))
  }
  invisible(summary_dt)
}

validate_emit <- function(cfg, root) {
  release_dir <- path_abs(root, cfg$output$release_dir)
  analyses_path <- file.path(release_dir, "analyses.tsv")
  analyses <- fread(analyses_path, sep = "\t", na.strings = "")
  has_targets <- has_gene_targets(cfg)
  # Registry-only and family-specific structural requirements (issue #51):
  # opengwasdb's shared core schema deliberately does not know about these
  # (source location/provenance columns, this generator's dense
  # analysis_index invariant, or the single-gene-target columns), so they
  # stay this repository's own concern rather than being pushed onto
  # opengwasdb (see docs/adr/0017-opengwasdb-owns-shared-analysis-schema.md's
  # column-class split). Shared-core required columns and controlled
  # vocabularies (stored_effect_scale, sample_size_kind/scope/size,
  # original_effect_scale, original_sd_method, ancestry_assignment_method)
  # are validated below against opengwasdb's own schema instead of a
  # hand-rolled, driftable copy of the same list.
  required <- c(
    "analysis_index", "analysis_id", "source_analysis_id", "source_label",
    "analysis_label", "trait_ontology_label", "trait_ontology_id",
    "trait_ontology_mapping_method", "source_file",
    "source_genome_build", "license", "first_author", "filtered_file"
  )
  # trait_id/gene_id/gene_name/trait_chr/trait_bp/n/mhc are single-gene-target
  # columns (issue #26): required only for Store Families with a resolvable
  # per-Analysis gene target, absent entirely (not NA-filled) otherwise.
  if (has_targets) {
    required <- c(required, "trait_id", "gene_id", "gene_name", "trait_chr", "trait_bp", "n", "mhc")
  }
  missing_cols <- setdiff(required, names(analyses))
  if (length(missing_cols)) stop("analyses.tsv missing columns: ", paste(missing_cols, collapse = ", "))
  if (anyDuplicated(analyses$analysis_id)) stop("analysis_id must be unique")
  if (!identical(analyses$analysis_index, seq_len(nrow(analyses)) - 1L)) {
    stop("analysis_index must be dense 0..n-1")
  }
  if (has_targets && any(is.na(analyses$trait_chr) | is.na(analyses$trait_bp))) {
    stop("trait_chr and trait_bp are required for every selected analysis")
  }

  schema <- validate_against_opengwasdb_schema(analyses_path, root)
  if (!schema$passed) {
    merge_validation_checks(
      release_dir, list(schema = "failed", errors = schema$errors),
      "resources/generators/gwas-ssf-ragged/generate.R"
    )
    stop(
      "analyses.tsv failed opengwasdb's shared analyses.tsv schema:\n",
      paste(" -", schema$errors, collapse = "\n")
    )
  }
  merge_validation_checks(
    release_dir, list(schema = "passed"),
    "resources/generators/gwas-ssf-ragged/generate.R"
  )

  cat(sprintf(
    "Release bundle schema check passed for %d analyses (registry structure + opengwasdb shared core)\n",
    nrow(analyses)
  ))
}

# Merges `checks` (any of the canonical checks.* keys, e.g. schema/
# effect_scale/sd_estimation) plus accumulated warnings/errors into an
# existing validation.yaml without discarding checks recorded by a different
# stage -- the emit-time schema check, the effect-scale stage, and the build
# step each own different keys and must not clobber one another.
merge_validation_checks <- function(release_dir, checks, validator_name) {
  path <- file.path(release_dir, "validation.yaml")
  current <- if (file.exists(path)) read_yaml(path) else list(status = "not_run", checks = list())
  known_checks <- c(
    "schema", "files", "reader_smoke_test", "ancestry",
    "effect_scale", "sd_estimation", "sparse_regions"
  )
  for (key in intersect(names(checks), known_checks)) {
    current$checks[[key]] <- checks[[key]]
  }
  # yaml::read_yaml() parses a single-element YAML sequence as a bare
  # character vector, not a list -- as.list() (rather than an `is.list()`
  # guard that resets to list()) normalises both shapes without discarding
  # a lone pre-existing warning/error recorded by an earlier stage.
  current$warnings <- as.list(unique(c(unlist(as.list(current$warnings)), checks$warnings)))
  current$errors <- as.list(unique(c(unlist(as.list(current$errors)), checks$errors)))

  status_rank <- c(not_run = 0L, passed = 1L, passed_with_warnings = 2L, failed = 3L)
  check_values <- unlist(current$checks[!vapply(current$checks, is.null, logical(1))])
  check_values <- check_values[check_values %in% names(status_rank)]
  if (length(check_values)) {
    current$status <- names(which.max(status_rank[check_values]))
  }
  current$validated_at <- format(Sys.time(), "%Y-%m-%dT%H:%M:%SZ", tz = "UTC")
  current$validator <- list(name = validator_name, version = NULL)
  write_yaml_file(current, path)
}

effect_scale_stage <- function(cfg, root) {
  release_dir <- path_abs(root, cfg$output$release_dir)
  paths <- artifact_paths(cfg, root)
  reference_resources <- resolve_reference_resources_from_config(cfg)
  thresholds <- default_thresholds(cfg$effect_scale_validation)
  result <- run_effect_scale_stage(release_dir, paths$filtered_dir, reference_resources, thresholds)
  set_sd_estimation_sidecar_pointer(release_dir)
  merge_validation_checks(
    release_dir, result$checks,
    "resources/generators/gwas-ssf-ragged/generate.R"
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

refresh_build_mode <- function(cfg, root) {
  release_dir <- path_abs(root, cfg$output$release_dir)
  paths <- artifact_paths(cfg, root)
  write_build_yaml(cfg, root, release_dir, paths)
  cat(sprintf("Refreshed build.yaml for %s\n", release_dir))
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
} else if (args$mode == "filter") {
  filter_release(cfg, root, args$max_analyses, args$only_analysis_id, args$parallel_workers)
} else if (args$mode == "validate") {
  validate_emit(cfg, root)
} else if (args$mode == "refresh-artifacts") {
  refresh_artifact_paths(cfg, root)
  validate_emit(cfg, root)
} else if (args$mode == "refresh-build") {
  refresh_build_mode(cfg, root)
} else if (args$mode == "effect-scale") {
  effect_scale_stage(cfg, root)
} else if (args$mode == "all") {
  emit_bundle(cfg, root)
  validate_emit(cfg, root)
  filter_release(cfg, root, args$max_analyses, args$only_analysis_id, args$parallel_workers)
} else {
  stop("Unknown --mode=", args$mode)
}
