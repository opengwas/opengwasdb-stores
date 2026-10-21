#!/usr/bin/env Rscript

# Materialise the filtered GWAS-SSF sources declared by an accepted Store
# Release. Filter selection has already happened: sparse_regions.tsv is the
# frozen plan describing which genomic intervals the release retains.

suppressPackageStartupMessages({
  library(data.table)
  library(yaml)
  library(parallel)
})

`%||%` <- function(x, y) if (is.null(x) || !length(x) || is.na(x)) y else x

usage <- function() {
  cat(paste(
    "Usage:",
    "  materialise-gwas-ssf-ragged STORE_ID [options]",
    "",
    "Options:",
    "  --registry-root=PATH       Registry root (default: stores)",
    "  --parallel-workers=N       Concurrent downloads (default: 4)",
    "  --only-analysis-id=ID,...  Materialise only selected Analyses",
    "  --qc-panel=PATH            Exact chromosome/position QC panel; required",
    "                             when sparse_regions.tsv contains qc_panel rows",
    "  --help                     Show this help",
    sep = "\n"
  ))
}

parse_args <- function(args) {
  out <- list(
    store_id = NULL,
    registry_root = "stores",
    parallel_workers = 4L,
    only_analysis_id = "",
    qc_panel = NULL
  )
  for (arg in args) {
    if (arg == "--help") {
      usage()
      quit(status = 0L)
    } else if (grepl("^--registry-root=", arg)) {
      out$registry_root <- sub("^--registry-root=", "", arg)
    } else if (grepl("^--parallel-workers=", arg)) {
      out$parallel_workers <- as.integer(sub("^--parallel-workers=", "", arg))
    } else if (grepl("^--only-analysis-id=", arg)) {
      out$only_analysis_id <- sub("^--only-analysis-id=", "", arg)
    } else if (grepl("^--qc-panel=", arg)) {
      out$qc_panel <- sub("^--qc-panel=", "", arg)
    } else if (!startsWith(arg, "--") && is.null(out$store_id)) {
      out$store_id <- arg
    } else {
      stop("Unknown argument: ", arg, call. = FALSE)
    }
  }
  if (is.null(out$store_id)) stop("Missing STORE_ID", call. = FALSE)
  if (!grepl("^OGS-[0-9]{5}$", out$store_id)) {
    stop("STORE_ID must match OGS- followed by five digits", call. = FALSE)
  }
  if (is.na(out$parallel_workers) || out$parallel_workers < 1L) {
    stop("--parallel-workers must be a positive integer", call. = FALSE)
  }
  out
}

require_columns <- function(table, required, label) {
  missing <- setdiff(required, names(table))
  if (length(missing)) {
    stop(label, " is missing required columns: ", paste(missing, collapse = ", "), call. = FALSE)
  }
}

clean_chr <- function(x) sub("^chr", "", as.character(x), ignore.case = TRUE)

sha256_file <- function(path) {
  connection <- file(path, "rb")
  on.exit(close(connection), add = TRUE)
  as.character(openssl::sha256(connection))
}

checksum_path <- function(path) paste0(path, ".sha256")

read_checksum <- function(path) {
  record <- checksum_path(path)
  if (!file.exists(record)) return(NULL)
  line <- readLines(record, n = 1L, warn = FALSE)
  if (!length(line)) return(NULL)
  checksum <- tolower(strsplit(trimws(line[[1]]), "\\s+")[[1]][1])
  if (!grepl("^[0-9a-f]{64}$", checksum)) return(NULL)
  checksum
}

verify_file <- function(path) {
  if (!file.exists(path)) return(FALSE)
  expected <- read_checksum(path)
  !is.null(expected) && isTRUE(sha256_file(path) == expected)
}

merge_intervals <- function(ranges) {
  if (!nrow(ranges)) return(ranges[, .(chromosome, start, end)])
  ranges <- unique(ranges[, .(
    chromosome = clean_chr(chromosome),
    start = as.numeric(start),
    end = as.numeric(end)
  )])
  ranges <- ranges[!is.na(start) & !is.na(end)]
  setorder(ranges, chromosome, start, end)
  merged <- vector("list", nrow(ranges))
  n_merged <- 0L
  for (i in seq_len(nrow(ranges))) {
    current <- ranges[i]
    if (n_merged == 0L || current$chromosome != merged[[n_merged]]$chromosome ||
        current$start > merged[[n_merged]]$end + 1) {
      n_merged <- n_merged + 1L
      merged[[n_merged]] <- current
    } else {
      merged[[n_merged]]$end <- max(merged[[n_merged]]$end, current$end)
    }
  }
  rbindlist(merged[seq_len(n_merged)])
}

positions_in_intervals <- function(chromosome, position, ranges) {
  keep <- rep(FALSE, length(position))
  if (!nrow(ranges) || !length(position)) return(keep)
  for (chr in intersect(unique(chromosome), unique(ranges$chromosome))) {
    row_indices <- which(chromosome == chr)
    chr_ranges <- ranges[chromosome == chr]
    interval <- findInterval(position[row_indices], chr_ranges$start)
    has_start <- interval > 0L
    matches <- rep(FALSE, length(row_indices))
    matches[has_start] <- position[row_indices][has_start] <= chr_ranges$end[interval[has_start]]
    keep[row_indices] <- matches
  }
  keep
}

download_source <- function(url, destination) {
  if (startsWith(url, "file://")) {
    source <- URLdecode(sub("^file://", "", url))
    if (!file.copy(source, destination, overwrite = TRUE, copy.mode = FALSE)) {
      stop("failed to copy ", url, call. = FALSE)
    }
  } else {
    options(timeout = 3600L)
    status <- tryCatch(
      suppressWarnings(download.file(url, destination, mode = "wb", quiet = TRUE, method = "libcurl")),
      error = function(error) {
        stop("download failed for ", url, ": ", conditionMessage(error), call. = FALSE)
      }
    )
    if (status != 0L) {
      stop("download failed for ", url, " (status ", status, ")", call. = FALSE)
    }
  }
  invisible(destination)
}

selected_columns <- c(
  "chromosome", "base_pair_location", "effect_allele", "other_allele",
  "beta", "standard_error", "p_value", "effect_allele_frequency",
  "variant_id", "rsid"
)
required_source_columns <- c(
  "chromosome", "base_pair_location", "effect_allele", "other_allele",
  "beta", "standard_error", "p_value"
)

materialise_one <- function(row, regions, qc_panel) {
  analysis_id <- row$analysis_id[[1]]
  destination <- row$source_file[[1]]
  if (!grepl("^/", destination)) {
    stop(analysis_id, ": source_file must be an absolute path: ", destination, call. = FALSE)
  }
  if (!nzchar(row$source_url[[1]])) {
    stop(analysis_id, ": source_url is empty", call. = FALSE)
  }
  if (verify_file(destination)) {
    return(list(analysis_id = analysis_id, status = "verified", message = destination))
  }

  parent <- dirname(destination)
  dir.create(parent, recursive = TRUE, showWarnings = FALSE)
  download_path <- tempfile(
    pattern = paste0(".", basename(destination), "."),
    tmpdir = parent,
    fileext = ".download.tsv.gz"
  )
  gzip_output <- endsWith(tolower(destination), ".gz")
  output_path <- tempfile(
    pattern = paste0(".", basename(destination), "."),
    tmpdir = parent,
    fileext = if (gzip_output) ".partial.gz" else ".partial"
  )
  checksum_output <- tempfile(
    pattern = paste0(".", basename(destination), ".sha256."),
    tmpdir = parent,
    fileext = ".partial"
  )
  on.exit(unlink(c(download_path, output_path, checksum_output)), add = TRUE)

  download_source(row$source_url[[1]], download_path)
  header <- names(fread(download_path, nrows = 0L, showProgress = FALSE))
  missing <- setdiff(required_source_columns, header)
  if (length(missing)) {
    stop(analysis_id, ": downloaded GWAS-SSF is missing columns: ",
         paste(missing, collapse = ", "), call. = FALSE)
  }
  columns <- intersect(selected_columns, header)
  source <- fread(
    download_path,
    select = columns,
    colClasses = list(character = "chromosome"),
    showProgress = FALSE
  )
  source <- source[!is.na(base_pair_location) & !is.na(p_value)]
  source[, chromosome := clean_chr(chromosome)]

  target_analysis_id <- analysis_id
  analysis_regions <- regions[analysis_id == target_analysis_id]
  interval_ranges <- merge_intervals(
    analysis_regions[!region_kind %in% c("qc_panel", "suggestive_trans")]
  )
  keep <- positions_in_intervals(
    source$chromosome,
    as.numeric(source$base_pair_location),
    interval_ranges
  )

  suggestive <- analysis_regions[region_kind == "suggestive_trans"]
  if (nrow(suggestive)) {
    source_position_key <- paste(source$chromosome, source$base_pair_location, sep = ":")
    blank_leads <- is.na(suggestive$lead_variant_id) | suggestive$lead_variant_id == ""
    if (any(blank_leads)) {
      position_keys <- paste(suggestive$chromosome[blank_leads], suggestive$start[blank_leads], sep = ":")
      keep <- keep | source_position_key %chin% position_keys
    }
    if (any(!blank_leads)) {
      source_lead <- rep("", nrow(source))
      if ("variant_id" %in% names(source)) source_lead <- as.character(source$variant_id)
      if ("rsid" %in% names(source)) {
        use_rsid <- is.na(source_lead) | source_lead == ""
        source_lead[use_rsid] <- as.character(source$rsid[use_rsid])
      }
      source_lead[is.na(source_lead)] <- ""
      source_keys <- paste(source_position_key, source_lead, sep = ":")
      lead_keys <- paste(
        suggestive$chromosome[!blank_leads], suggestive$start[!blank_leads],
        suggestive$lead_variant_id[!blank_leads], sep = ":"
      )
      keep <- keep | source_keys %chin% lead_keys
    }
  }

  if (nrow(analysis_regions[region_kind == "qc_panel"])) {
    source_keys <- paste(source$chromosome, source$base_pair_location, sep = ":")
    keep <- keep | source_keys %chin% qc_panel
  }

  fwrite(
    source[keep, ..columns], output_path, sep = "\t",
    compress = if (gzip_output) "gzip" else "none"
  )
  checksum <- sha256_file(output_path)
  writeLines(paste0(checksum, "  ", basename(destination)), checksum_output, useBytes = TRUE)
  if (!file.rename(output_path, destination)) {
    stop(analysis_id, ": could not atomically publish ", destination, call. = FALSE)
  }
  if (!file.rename(checksum_output, checksum_path(destination))) {
    stop(analysis_id, ": could not publish checksum for ", destination, call. = FALSE)
  }
  list(analysis_id = analysis_id, status = "materialised", message = destination)
}

args <- parse_args(commandArgs(trailingOnly = TRUE))
registry_root <- normalizePath(args$registry_root, winslash = "/", mustWork = FALSE)
release_dir <- file.path(registry_root, args$store_id)
release_path <- file.path(release_dir, "release.yaml")
manifest_path <- file.path(release_dir, "analyses.tsv")
if (!file.exists(release_path)) stop("Missing release bundle: ", release_path, call. = FALSE)
if (!file.exists(manifest_path)) stop("Missing analyses manifest: ", manifest_path, call. = FALSE)

release <- read_yaml(release_path)
regions_relative <- release$sidecars$sparse_regions %||% ""
if (!nzchar(regions_relative)) {
  stop("release.yaml must declare sidecars.sparse_regions", call. = FALSE)
}
regions_path <- if (grepl("^/", regions_relative)) {
  regions_relative
} else {
  file.path(release_dir, regions_relative)
}
if (!file.exists(regions_path)) stop("Missing sparse-region plan: ", regions_path, call. = FALSE)

manifest <- fread(manifest_path, sep = "\t", na.strings = "", colClasses = "character")
require_columns(manifest, c("analysis_id", "source_url", "source_file"), "analyses.tsv")
if (anyDuplicated(manifest$analysis_id)) stop("analyses.tsv contains duplicate analysis_id values", call. = FALSE)
if (anyDuplicated(manifest$source_file)) stop("analyses.tsv contains duplicate source_file values", call. = FALSE)

if ("exclude_from_build" %in% names(manifest)) {
  excluded <- tolower(trimws(manifest$exclude_from_build)) %in% c("true", "1", "yes")
  manifest <- manifest[!excluded]
}
if (nzchar(args$only_analysis_id)) {
  requested <- strsplit(args$only_analysis_id, ",", fixed = TRUE)[[1]]
  unknown <- setdiff(requested, manifest$analysis_id)
  if (length(unknown)) stop("Unknown analysis_id: ", paste(unknown, collapse = ", "), call. = FALSE)
  manifest <- manifest[analysis_id %in% requested]
}

regions <- fread(regions_path, sep = "\t", na.strings = "", colClasses = "character")
require_columns(regions, c("analysis_id", "region_kind", "chromosome", "start", "end"),
                "sparse_regions.tsv")
if (!"lead_variant_id" %in% names(regions)) regions[, lead_variant_id := ""]
regions[, chromosome := clean_chr(chromosome)]
regions <- regions[analysis_id %in% manifest$analysis_id]

qc_keys <- character()
has_qc_regions <- nrow(regions[region_kind == "qc_panel"]) > 0L
if (has_qc_regions) {
  if (is.null(args$qc_panel)) {
    stop("--qc-panel is required because sparse_regions.tsv contains qc_panel rows", call. = FALSE)
  }
  qc_path <- normalizePath(args$qc_panel, winslash = "/", mustWork = FALSE)
  if (!file.exists(qc_path)) stop("Missing QC panel: ", qc_path, call. = FALSE)
  qc <- fread(qc_path, sep = "\t", na.strings = "", colClasses = "character")
  require_columns(qc, c("chromosome", "position"), "QC panel")
  qc_keys <- unique(paste(clean_chr(qc$chromosome), qc$position, sep = ":"))
} else if (!is.null(args$qc_panel)) {
  warning("--qc-panel ignored because sparse_regions.tsv contains no qc_panel rows", call. = FALSE)
}

if (!nrow(manifest)) {
  cat("materialise-gwas-ssf-ragged: materialised=0 verified=0 failed=0\n")
  quit(status = 0L)
}
workers <- min(args$parallel_workers, nrow(manifest))
run_one <- function(i) {
  row <- manifest[i]
  tryCatch(
    materialise_one(row, regions, qc_keys),
    error = function(error) list(
      analysis_id = row$analysis_id[[1]],
      status = "failed",
      message = conditionMessage(error)
    )
  )
}
results <- if (workers > 1L) {
  mclapply(seq_len(nrow(manifest)), run_one, mc.cores = workers, mc.preschedule = FALSE)
} else {
  lapply(seq_len(nrow(manifest)), run_one)
}

for (result in results) {
  cat(sprintf("[%s] %s: %s\n", result$status, result$analysis_id, result$message))
}
statuses <- vapply(results, `[[`, character(1), "status")
cat(sprintf(
  "materialise-gwas-ssf-ragged: materialised=%d verified=%d failed=%d\n",
  sum(statuses == "materialised"), sum(statuses == "verified"), sum(statuses == "failed")
))
if (any(statuses == "failed")) quit(status = 1L)
