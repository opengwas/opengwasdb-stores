#!/usr/bin/env Rscript
# Shared reference-AF effect-scale validation engine (issue #16, #17-#21).
#
# Given filtered source rows for one quantitative Analysis, a resolved
# `sample_size`, and an optional Reference Resource, this module:
#   - chooses an allele-frequency source (source-provided vs reference MAF),
#   - aligns alleles safely (rejecting palindromic/ambiguous/mismatched pairs),
#   - computes implied phenotype-SD diagnostics from SE + N + MAF, and
#   - classifies the result as passed/warning/failed/skipped with a reason.
#
# It is deliberately layout-agnostic (dense vs ragged) and generator-agnostic:
# callers pass in the retained source rows and get back one sidecar row plus,
# where applicable, an updated `original_sd` / `original_sd_method`.

suppressPackageStartupMessages(library(data.table))

`%||%` <- function(x, y) if (is.null(x) || length(x) == 0 || (length(x) == 1 && is.na(x))) y else x

phenotype_sd_bridge <- normalizePath(
  "resources/generators/lib/phenotype_sd_estimate.py", winslash = "/", mustWork = FALSE
)

# ---------------------------------------------------------------------------
# Reference AF resource access
#
# A reference AF resource is a directory tree of LD-block allele-frequency
# tables, one population/ancestry subdirectory per configured ancestry:
#
#   <root>/<ancestry_dir>/<chr>/<start>-<end>.tsv
#
# Each block file has a header row with at least CHR, SNP, OA, EA, EAF, BP
# (matching the UKB hg38 reference panel convention).
# ---------------------------------------------------------------------------

ref_af_cache_env <- new.env(parent = emptyenv())

ref_af_list_blocks <- function(root, ancestry_dir) {
  base_dir <- file.path(root, ancestry_dir)
  if (!dir.exists(base_dir)) {
    return(data.table(chromosome = character(), start = integer(), end = integer(), path = character()))
  }
  files <- list.files(base_dir, pattern = "^[0-9-]+\\.tsv$", recursive = TRUE, full.names = TRUE)
  if (!length(files)) {
    return(data.table(chromosome = character(), start = integer(), end = integer(), path = character()))
  }
  rel <- sub(paste0("^", base_dir, "/"), "", files)
  chromosome <- vapply(strsplit(rel, "/", fixed = TRUE), `[[`, character(1), 1)
  range_part <- sub("\\.tsv$", "", basename(rel))
  bounds <- strsplit(range_part, "-", fixed = TRUE)
  start <- as.integer(vapply(bounds, `[[`, character(1), 1))
  end <- as.integer(vapply(bounds, function(b) b[[length(b)]], character(1)))
  data.table(chromosome = chromosome, start = start, end = end, path = files)
}

ref_af_blocks_overlapping <- function(blocks, chrom, region_start, region_end) {
  blocks[chromosome == chrom & start <= region_end & end >= region_start]
}

ref_af_read_block <- function(path) {
  cached <- get0(path, envir = ref_af_cache_env, ifnotfound = NULL)
  if (!is.null(cached)) return(cached)
  dt <- fread(path, sep = "\t", na.strings = "", showProgress = FALSE)
  setnames(dt, tolower(names(dt)))
  out <- dt[, .(
    chromosome = as.character(chr),
    bp = as.integer(bp),
    oa = toupper(as.character(oa)),
    ea = toupper(as.character(ea)),
    eaf = suppressWarnings(as.double(eaf))
  )]
  assign(path, out, envir = ref_af_cache_env)
  out
}

# Look up reference allele frequencies for a set of positions on ONE
# chromosome, by reading only the LD-block files whose coordinate range
# overlaps the requested positions. Returns one row per reference variant
# found at a requested position (duplicated positions, e.g. multi-allelic
# sites, are preserved so the caller can align against every candidate).
#
# Note: `chrom` is intentionally not named `chromosome` here, because
# data.table's NSE would otherwise resolve `chromosome` inside `rows[...]` to
# the *column* of the same name rather than this argument, silently turning
# the chromosome filter into a no-op tautology.
ref_af_lookup <- function(resource, chrom, positions) {
  if (is.null(resource)) {
    return(list(status = "no_resource", rows = ref_af_read_block_empty()))
  }
  blocks <- ref_af_list_blocks(resource$root, resource$ancestry_dir)
  if (!nrow(blocks)) {
    return(list(status = "no_resource_data", rows = ref_af_read_block_empty()))
  }
  positions <- unique(as.integer(positions))
  overlap <- ref_af_blocks_overlapping(blocks, chrom, min(positions), max(positions))
  if (!nrow(overlap)) {
    return(list(status = "no_overlapping_blocks", rows = ref_af_read_block_empty()))
  }
  rows <- rbindlist(lapply(overlap$path, ref_af_read_block))
  rows <- rows[chromosome == chrom & bp %in% positions]
  list(status = "ok", rows = rows)
}

ref_af_read_block_empty <- function() {
  data.table(chromosome = character(), bp = integer(), oa = character(), ea = character(), eaf = double())
}

# ---------------------------------------------------------------------------
# Allele alignment
# ---------------------------------------------------------------------------

is_snp_allele <- function(x) grepl("^[ACGT]$", x)

is_palindromic <- function(a, b) {
  (a == "A" & b == "T") | (a == "T" & b == "A") | (a == "C" & b == "G") | (a == "G" & b == "C")
}

# Classify one source (effect_allele, other_allele) pair against one
# reference (ea, oa) pair. Returns a single controlled status string.
classify_allele_pair <- function(effect_allele, other_allele, ref_ea, ref_oa) {
  ea <- toupper(effect_allele)
  oa <- toupper(other_allele)
  if (!is_snp_allele(ea) || !is_snp_allele(oa) || !is_snp_allele(ref_ea) || !is_snp_allele(ref_oa)) {
    return("unusable_allele_format")
  }
  if (is_palindromic(ea, oa)) return("ambiguous_palindromic")
  if (ea == ref_ea && oa == ref_oa) return("forward")
  if (ea == ref_oa && oa == ref_ea) return("swapped")
  "mismatch"
}

# Align source rows (chromosome, base_pair_location, effect_allele,
# other_allele) against reference rows (chromosome, bp, ea, oa, eaf). When
# more than one reference row shares a position (multi-allelic site), the
# first row that produces a safe (forward/swapped) match is used.
align_to_reference <- function(ssf_rows, ref_rows) {
  n <- nrow(ssf_rows)
  status <- rep("no_overlap", n)
  aligned_af <- rep(NA_real_, n)
  if (!nrow(ref_rows) || !n) {
    return(data.table(match_status = status, reference_af = aligned_af))
  }
  setkey(ref_rows, bp)
  for (i in seq_len(n)) {
    candidates <- ref_rows[bp == ssf_rows$base_pair_location[i]]
    if (!nrow(candidates)) next
    resolved <- NA_character_
    af <- NA_real_
    for (j in seq_len(nrow(candidates))) {
      cls <- classify_allele_pair(
        ssf_rows$effect_allele[i], ssf_rows$other_allele[i],
        candidates$ea[j], candidates$oa[j]
      )
      if (cls %in% c("forward", "swapped")) {
        resolved <- cls
        af <- if (cls == "forward") candidates$eaf[j] else 1 - candidates$eaf[j]
        break
      }
      if (is.na(resolved)) resolved <- cls
    }
    status[i] <- resolved
    aligned_af[i] <- af
  }
  data.table(match_status = status, reference_af = aligned_af)
}

# ---------------------------------------------------------------------------
# AF source precedence (issue #20)
# ---------------------------------------------------------------------------

# Decide whether usable source-provided allele frequencies exist. A value is
# usable when it parses as numeric and lies strictly within (0, 1).
resolve_source_af <- function(ssf_rows) {
  if (!"effect_allele_frequency" %in% names(ssf_rows)) {
    return(list(usable = rep(FALSE, nrow(ssf_rows)), any_usable = FALSE))
  }
  vals <- suppressWarnings(as.double(ssf_rows$effect_allele_frequency))
  usable <- !is.na(vals) & vals > 0 & vals < 1
  list(usable = usable, any_usable = any(usable), values = vals)
}

# ---------------------------------------------------------------------------
# Implied phenotype-SD diagnostics
# ---------------------------------------------------------------------------

estimate_phenotype_sd <- function(method, n, se = NULL, af = NULL, beta = NULL) {
  if (!requireNamespace("jsonlite", quietly = TRUE)) {
    stop("The OpenGWASDB phenotype-SD bridge requires the jsonlite R package")
  }
  python_bin <- Sys.which("python3")
  if (!nzchar(python_bin)) stop("python3 not found on PATH; required for phenotype-SD estimation")
  sample_size_arg <- if (length(n) == 1L && is.finite(n)) as.character(n) else "nan"
  args <- c(phenotype_sd_bridge, "--method", method, "--sample-size", sample_size_arg)
  if (!is.null(se)) args <- c(args, "--se", jsonlite::toJSON(as.double(se), auto_unbox = TRUE, digits = NA))
  if (!is.null(af)) args <- c(args, "--af", jsonlite::toJSON(as.double(af), auto_unbox = TRUE, digits = NA))
  if (!is.null(beta)) args <- c(args, "--beta", jsonlite::toJSON(as.double(beta), auto_unbox = TRUE, digits = NA))
  output <- suppressWarnings(system2(python_bin, shQuote(args), stdout = TRUE, stderr = TRUE))
  status <- attr(output, "status")
  if (!is.null(status) && status != 0L) stop("OpenGWASDB phenotype-SD estimator failed: ", paste(output, collapse = "\n"))
  jsonlite::fromJSON(paste(output, collapse = "\n"), simplifyVector = TRUE)
}

robust_dispersion <- function(x) {
  med <- stats::median(x)
  if (!is.finite(med) || med == 0) return(NA_real_)
  stats::mad(x, center = med) / abs(med)
}

classify_status <- function(n_retained, thresholds, median_sd, dispersion, original_sd_method) {
  if (n_retained < thresholds$min_overlap_variants) {
    return(list(status = "skipped", reason = "low_overlap"))
  }
  if (!is.finite(median_sd)) {
    return(list(status = "failed", reason = "no_implied_sd"))
  }
  if (!is.finite(dispersion) || dispersion > thresholds$dispersion_max) {
    return(list(status = "warning", reason = "unstable_implied_sd"))
  }
  if (identical(original_sd_method, "declared_standardised")) {
    delta <- abs(median_sd - 1)
    if (delta <= thresholds$sd_tolerance) return(list(status = "passed", reason = ""))
    if (delta <= thresholds$sd_tolerance * thresholds$warning_multiplier) {
      return(list(status = "warning", reason = "scale_inconsistent"))
    }
    return(list(status = "failed", reason = "scale_inconsistent"))
  }
  list(status = "passed", reason = "")
}

default_thresholds <- function(cfg) {
  cfg <- cfg %||% list()
  list(
    maf_min = as.double(cfg$maf_min %||% 0.01),
    maf_max = as.double(cfg$maf_max %||% 0.5),
    min_overlap_variants = as.integer(cfg$min_overlap_variants %||% 20L),
    sd_tolerance = as.double(cfg$sd_tolerance %||% 0.15),
    warning_multiplier = as.double(cfg$warning_multiplier %||% 2.0),
    dispersion_max = as.double(cfg$dispersion_max %||% 0.5),
    beta_distribution_fallback = isTRUE(cfg$beta_distribution_fallback$enabled),
    beta_sample_kind = as.character(cfg$beta_distribution_fallback$sample_kind %||% "")
  )
}

empty_sidecar_row <- function() {
  data.table(
    analysis_id = character(), source_analysis_id = character(),
    status = character(), skip_reason = character(), af_source = character(),
    ancestry_reference_id = character(),
    original_sd = double(), original_sd_method = character(),
    n_variants_considered = integer(), n_variants_overlapping = integer(),
    n_variants_excluded_ambiguous = integer(), n_variants_excluded_mismatch = integer(),
    n_variants_excluded_missing_af = integer(), n_variants_excluded_maf = integer(),
    n_variants_retained = integer(),
    maf_min = double(), maf_max = double(),
    implied_sd_median = double(), sd_dispersion = double(),
    sd_notes = character(), estimator_version = character()
  )
}

# Resolve a Reference Resource (as loaded from build.yaml/config) for a given
# assigned ancestry label. Returns NULL when none is configured.
resolve_reference_resource <- function(reference_resources, assigned_ancestry) {
  if (!length(reference_resources) || is.null(assigned_ancestry) || !nzchar(assigned_ancestry)) return(NULL)
  for (res in reference_resources) {
    if (identical(res$ancestry, assigned_ancestry)) return(res)
  }
  NULL
}

# Assess one Analysis. `row` is the analyses.tsv row (as a one-row
# data.table/list) and `ssf_rows` is the retained filtered source table for
# that Analysis (chromosome, base_pair_location, effect_allele, other_allele,
# standard_error, effect_allele_frequency if present).
assess_analysis_effect_scale <- function(row, ssf_rows, reference_resources, thresholds) {
  base <- list(
    analysis_id = row$analysis_id,
    source_analysis_id = row$source_analysis_id %||% row$analysis_id,
    ancestry_reference_id = "",
    original_sd = suppressWarnings(as.double(row$original_sd)),
    original_sd_method = row$original_sd_method,
    n_variants_considered = nrow(ssf_rows),
    n_variants_overlapping = NA_integer_,
    n_variants_excluded_ambiguous = NA_integer_,
    n_variants_excluded_mismatch = NA_integer_,
    n_variants_excluded_missing_af = NA_integer_,
    n_variants_excluded_maf = NA_integer_,
    n_variants_retained = 0L,
    maf_min = thresholds$maf_min,
    maf_max = thresholds$maf_max,
    implied_sd_median = NA_real_,
    sd_dispersion = NA_real_,
    estimator_version = ""
  )

  if (row$stored_effect_scale %in% c("log_or", "log_hazard")) {
    return(as.data.table(c(base, list(
      status = "skipped", skip_reason = "non_quantitative_effect_scale",
      af_source = "", sd_notes = "log-OR/log-hazard analyses are not quantitative SD estimation targets"
    ))))
  }

  if (!nrow(ssf_rows)) {
    return(as.data.table(c(base, list(
      status = "skipped", skip_reason = "no_retained_variants", af_source = "",
      sd_notes = "No retained source rows available for this Analysis"
    ))))
  }

  source_af <- resolve_source_af(ssf_rows)
  af_source <- if (source_af$any_usable) "source" else ""
  reference <- NULL
  if (!nzchar(af_source)) {
    reference <- resolve_reference_resource(reference_resources, row$assigned_ancestry)
    if (is.null(reference)) {
      beta_allowed <- isTRUE(thresholds$beta_distribution_fallback) &&
        identical(thresholds$beta_sample_kind, "random_common_variants") &&
        "is_random_common_variant" %in% names(ssf_rows) &&
        nrow(ssf_rows) > 0L && all(ssf_rows$is_random_common_variant %in% TRUE)
      if (beta_allowed && "beta" %in% names(ssf_rows)) {
        beta <- suppressWarnings(as.double(ssf_rows$beta))
        beta <- beta[is.finite(beta)]
        estimate <- estimate_phenotype_sd(
          "estimated_from_beta_distribution", suppressWarnings(as.double(row$sample_size)), beta = beta
        )
        base$n_variants_overlapping <- 0L
        base$n_variants_excluded_ambiguous <- 0L
        base$n_variants_excluded_mismatch <- 0L
        base$n_variants_excluded_missing_af <- nrow(ssf_rows)
        base$n_variants_excluded_maf <- 0L
        base$n_variants_retained <- length(beta)
        base$original_sd <- if (is.null(estimate$sd)) NA_real_ else as.double(estimate$sd)
        base$original_sd_method <- estimate$method
        base$implied_sd_median <- base$original_sd
        base$sd_dispersion <- if (is.null(estimate$dispersion)) NA_real_ else as.double(estimate$dispersion)
        base$estimator_version <- estimate$estimator_version
        ok <- identical(estimate$method, "estimated_from_beta_distribution") &&
          length(beta) >= thresholds$min_overlap_variants
        return(as.data.table(c(base, list(
          status = if (ok) "passed" else "skipped",
          skip_reason = if (ok) "" else "low_overlap",
          af_source = "beta_distribution",
          sd_notes = if (!is.null(estimate$notes)) estimate$notes else sprintf(
            "beta-distribution fallback over %d random common variants", length(beta)
          )
        ))))
      }
      return(as.data.table(c(base, list(
        status = "skipped", skip_reason = "no_reference_resource_for_ancestry", af_source = "",
        sd_notes = sprintf("No configured reference AF resource for assigned_ancestry='%s'", row$assigned_ancestry %||% "")
      ))))
    }
    af_source <- "reference"
    base$ancestry_reference_id <- reference$resource_id
  }

  if (af_source == "source") {
    usable_af <- source_af$values
    n_excluded_missing_af <- sum(!source_af$usable)
    eligible <- ssf_rows[source_af$usable]
    eligible_af <- usable_af[source_af$usable]
    match_status <- rep("forward", nrow(eligible))
    n_overlapping <- nrow(eligible)
    n_ambiguous <- 0L
    n_mismatch <- 0L
    reference_af <- eligible_af
  } else {
    # Retained ragged rows can span multiple chromosomes for one Analysis
    # (cis window plus significant/suggestive trans regions elsewhere in the
    # genome), so alignment must be scoped to each chromosome separately
    # rather than assuming the first row's chromosome applies to every row.
    match_status <- rep("no_overlap", nrow(ssf_rows))
    match_af <- rep(NA_real_, nrow(ssf_rows))
    chromosomes <- unique(as.character(ssf_rows$chromosome))
    for (chrom in chromosomes) {
      idx <- which(as.character(ssf_rows$chromosome) == chrom)
      lookup <- ref_af_lookup(reference, chrom, ssf_rows$base_pair_location[idx])
      aligned <- align_to_reference(ssf_rows[idx], lookup$rows)
      match_status[idx] <- aligned$match_status
      match_af[idx] <- aligned$reference_af
    }
    n_overlapping <- sum(match_status != "no_overlap")
    n_ambiguous <- sum(match_status == "ambiguous_palindromic")
    n_mismatch <- sum(match_status %in% c("mismatch", "unusable_allele_format"))
    n_excluded_missing_af <- 0L
    safe <- match_status %in% c("forward", "swapped")
    eligible <- ssf_rows[safe]
    reference_af <- match_af[safe]
  }

  base$n_variants_overlapping <- n_overlapping
  base$n_variants_excluded_ambiguous <- n_ambiguous
  base$n_variants_excluded_mismatch <- n_mismatch
  base$n_variants_excluded_missing_af <- n_excluded_missing_af

  maf <- pmin(reference_af, 1 - reference_af)
  within_bounds <- !is.na(maf) & maf >= thresholds$maf_min & maf <= thresholds$maf_max
  base$n_variants_excluded_maf <- sum(!within_bounds)

  se <- suppressWarnings(as.double(eligible$standard_error[within_bounds]))
  af_final <- reference_af[within_bounds]
  n <- suppressWarnings(as.double(row$sample_size))
  valid <- !is.na(se) & !is.na(af_final) & is.finite(n) & n > 0
  method <- if (af_source == "source") "estimated_from_source_maf" else "estimated_from_reference_maf"
  estimate <- estimate_phenotype_sd(method, n, se = se[valid], af = af_final[valid])
  implied <- se[valid] * sqrt(2 * n * af_final[valid] * (1 - af_final[valid]))
  implied <- implied[is.finite(implied)]

  base$n_variants_retained <- length(implied)
  median_sd <- if (length(implied)) stats::median(implied) else NA_real_
  dispersion <- if (is.null(estimate$dispersion)) NA_real_ else as.double(estimate$dispersion)
  base$implied_sd_median <- median_sd
  base$sd_dispersion <- dispersion
  base$estimator_version <- estimate$estimator_version

  decision <- classify_status(length(implied), thresholds, median_sd, dispersion, row$original_sd_method)

  updated_sd <- base$original_sd
  updated_method <- base$original_sd_method
  if (decision$status %in% c("passed", "warning") &&
      !identical(row$original_sd_method, "declared_standardised") &&
      (is.na(updated_sd) || !nzchar(as.character(row$original_sd)))) {
    updated_sd <- as.double(estimate$sd)
    updated_method <- estimate$method
  }
  base$original_sd <- updated_sd
  base$original_sd_method <- updated_method

  notes <- if (nzchar(decision$reason)) {
    sprintf("%s (median implied SD=%.3f, dispersion=%.3f, n=%d)", decision$reason,
            median_sd %||% NA_real_, dispersion %||% NA_real_, length(implied))
  } else {
    sprintf("median implied SD=%.3f, dispersion=%.3f, n=%d", median_sd, dispersion %||% NA_real_, length(implied))
  }

  as.data.table(c(base, list(status = decision$status, skip_reason = if (decision$status == "skipped") decision$reason else "",
                              af_source = af_source, sd_notes = notes)))
}

sd_estimation_sidecar_columns <- c(
  "analysis_id", "source_analysis_id", "status", "skip_reason", "af_source",
  "ancestry_reference_id", "original_sd", "original_sd_method",
  "n_variants_considered", "n_variants_overlapping",
  "n_variants_excluded_ambiguous", "n_variants_excluded_mismatch",
  "n_variants_excluded_missing_af", "n_variants_excluded_maf",
  "n_variants_retained", "maf_min", "maf_max",
  "implied_sd_median", "sd_dispersion", "sd_notes", "estimator_version"
)

normalise_sidecar <- function(rows) {
  if (!length(rows)) return(empty_sidecar_row())
  out <- rbindlist(rows, fill = TRUE)
  for (col in setdiff(sd_estimation_sidecar_columns, names(out))) out[, (col) := NA]
  out[, ..sd_estimation_sidecar_columns]
}

# Summarise sidecar rows into an overall effect-scale / sd-estimation
# validation check + warning list (issue #21).
summarise_effect_scale_checks <- function(sidecar) {
  if (!nrow(sidecar)) {
    return(list(effect_scale = "not_run", sd_estimation = "not_run", warnings = character()))
  }
  attempted <- sidecar[!status %in% c("skipped")]
  n_failed <- sum(sidecar$status == "failed")
  n_warned <- sum(sidecar$status == "warning")
  warnings <- character()
  if (nrow(sidecar[status == "failed"])) {
    warnings <- c(warnings, sprintf(
      "%s: %s (%s)", sidecar[status == "failed"]$analysis_id,
      sidecar[status == "failed"]$sd_notes, "effect_scale_failed"
    ))
  }
  if (nrow(sidecar[status == "warning"])) {
    warnings <- c(warnings, sprintf(
      "%s: %s (%s)", sidecar[status == "warning"]$analysis_id,
      sidecar[status == "warning"]$sd_notes, "effect_scale_warning"
    ))
  }
  skipped_no_resource <- sidecar[status == "skipped" & skip_reason == "no_reference_resource_for_ancestry"]
  if (nrow(skipped_no_resource)) {
    warnings <- c(warnings, sprintf(
      "%s: skipped, no reference AF resource for assigned ancestry", skipped_no_resource$analysis_id
    ))
  }
  low_overlap <- sidecar[status == "skipped" & skip_reason == "low_overlap"]
  if (nrow(low_overlap)) {
    warnings <- c(warnings, sprintf("%s: skipped, low reference-AF overlap", low_overlap$analysis_id))
  }

  effect_scale <- if (n_failed > 0) "failed" else if (n_warned > 0) "passed_with_warnings" else "passed"
  sd_estimation <- effect_scale
  list(effect_scale = effect_scale, sd_estimation = sd_estimation, warnings = warnings)
}

# ---------------------------------------------------------------------------
# Config -> Reference Resource list
#
# Reads the `reference_resources` and `effect_scale_validation` blocks of a
# generator config (see docs/release-metadata-schema.md) into the shape
# `assess_analysis_effect_scale()` expects: one list per ancestry with
# `ancestry`, `resource_id`, `root`, `ancestry_dir`.
# ---------------------------------------------------------------------------

resolve_reference_resources_from_config <- function(cfg) {
  declared <- cfg$reference_resources %||% list()
  by_id <- list()
  for (res in declared) by_id[[res$resource_id]] <- res
  mapping <- (cfg$effect_scale_validation$reference_resources %||% list())
  out <- list()
  for (m in mapping) {
    declaration <- by_id[[m$resource_id]]
    if (is.null(declaration)) {
      stop("effect_scale_validation.reference_resources refers to unknown resource_id: ", m$resource_id)
    }
    out[[length(out) + 1]] <- list(
      ancestry = m$ancestry,
      resource_id = declaration$resource_id,
      root = declaration$root,
      ancestry_dir = declaration$ancestry_dir %||% m$ancestry
    )
  }
  out
}

# ---------------------------------------------------------------------------
# Release-bundle orchestration
#
# Runs the effect-scale validation stage over every row of an emitted
# release's `analyses.tsv`, reading each Analysis's retained filtered source
# file, writing `sidecars/sd_estimation.tsv`, and updating `original_sd` /
# `original_sd_method` in place where estimation (not just QC) applies.
# ---------------------------------------------------------------------------

run_effect_scale_stage <- function(release_dir, filtered_dir, reference_resources, thresholds,
                                    filtered_file_col = "filtered_file") {
  analyses_path <- file.path(release_dir, "analyses.tsv")
  analyses <- fread(analyses_path, sep = "\t", na.strings = "")
  analyses[, original_sd := suppressWarnings(as.double(original_sd))]
  analyses[, original_sd_method := as.character(original_sd_method)]

  sidecar_rows <- vector("list", nrow(analyses))
  for (i in seq_len(nrow(analyses))) {
    row <- as.list(analyses[i])
    ssf_path <- file.path(filtered_dir, row[[filtered_file_col]])
    ssf_rows <- if (nzchar(row[[filtered_file_col]] %||% "") && file.exists(ssf_path)) {
      fread(ssf_path, sep = "\t", na.strings = "", showProgress = FALSE)
    } else {
      data.table()
    }
    result <- assess_analysis_effect_scale(row, ssf_rows, reference_resources, thresholds)
    sidecar_rows[[i]] <- result
    data.table::set(analyses, i, "original_sd", result$original_sd)
    data.table::set(analyses, i, "original_sd_method", result$original_sd_method)
  }

  sidecar <- normalise_sidecar(sidecar_rows)
  fwrite(analyses, analyses_path, sep = "\t", na = "")
  sidecar_dir <- file.path(release_dir, "sidecars")
  dir.create(sidecar_dir, showWarnings = FALSE, recursive = TRUE)
  fwrite(sidecar, file.path(sidecar_dir, "sd_estimation.tsv"), sep = "\t", na = "")

  checks <- summarise_effect_scale_checks(sidecar)
  list(analyses = analyses, sidecar = sidecar, checks = checks)
}
