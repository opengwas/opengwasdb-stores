# Metadata resolver for the `opengwas-gwas-vcf` Source Collection (issue
# #49; see docs/release-metadata-schema.md, "Metadata resolvers"). Resolves
# `stored_effect_scale`/`sample_size_kind`/`sample_size`/`n_cases`/
# `n_controls` from the OpenGWAS API's own study-level metadata ("gwasinfo"
# record), never from the source GWAS-VCF file's `##SAMPLE` header. This is
# the concrete motivating example from issue #15: `ieu-a-7` (Coronary Heart
# Disease) declares `StudyType=Continuous` in its VCF header with no
# case/control counts at all, while the OpenGWAS API JSON for the same
# study correctly reports `ncase=60801`/`ncontrol=123504`.
#
# Field names below (id, ncase, ncontrol, sample_size, unit, ...) match the
# OpenGWAS API's own published GwasInfo schema
# (https://api.opengwas.io/api/swagger.json, definitions.GwasInfo).
#
# stored_effect_scale is resolved from the `unit` field, not merely from
# ncase/ncontrol presence (issue #50 finding, confirmed against live API
# data): the ukb-b batch's BOLT-LMM/PHESANT pipeline reports every trait,
# including Binary-category traits with real ncase/ncontrol, on the SD
# scale (unit="SD") -- inferring log_or from ncase/ncontrol alone is wrong
# for that batch specifically. `unit` is absent for some datasets (e.g. the
# API's own literal placeholder string "NA"), so ncase/ncontrol presence
# remains a secondary fallback for datasets with no usable `unit` value.
#
# Conforms to the resolver contract established by
# resources/generators/lib/metadata_resolvers/gwas_catalog_ssf.R (issue #48): one
# resolved record per Analysis, resolution_status always explicit.

suppressPackageStartupMessages(library(data.table))
source("resources/generators/lib/metadata_resolvers/contract.R")

# Maps the OpenGWAS API's free-text `unit` field to this resolver's
# controlled stored_effect_scale vocabulary. Returns NA (not a guess) when
# `unit` is absent, the API's literal placeholder string "NA", or an
# unrecognised value, so callers know to fall back to a secondary signal
# rather than trusting a misclassification. Real observed values include
# "SD", "SD (kg/m^2)" (a quantitative trait's native unit appended), "log
# odds", and "logOR".
.classify_stored_effect_scale_from_unit <- function(unit) {
  if (is.null(unit) || length(unit) == 0 || is.na(unit)) return(NA_character_)
  normalised <- gsub("[^a-z]", "", tolower(unit))
  if (!nzchar(normalised) || normalised == "na") return(NA_character_)
  if (grepl("loghazard|hazardratio", normalised)) return("log_hazard")
  if (grepl("logodds|logor", normalised)) return("log_or")
  if (grepl("^sd", normalised)) return("sd")
  NA_character_
}

#' Resolve one study's analytical metadata from an already-fetched OpenGWAS
#' API "gwasinfo" record (see fetch_opengwas_gwasinfo() below to obtain
#' one). Deliberately takes the parsed record rather than an id and doing
#' the HTTP call itself, so this resolver stays independently testable via
#' fixture without a live network call or an API token.
#'
#' @param gwasinfo a named list as returned by one element of
#'   jsonlite::fromJSON() on an OpenGWAS API gwasinfo response: relevant
#'   fields are `unit`, `ncase`, `ncontrol`, `sample_size` (all
#'   optional/nullable per the API's own schema).
#' @return a one-row data.table, same shape as
#'   resolve_gwas_catalog_ssf_metadata(): resolution_status,
#'   resolution_notes, stored_effect_scale, sample_size_kind, sample_size,
#'   n_cases, n_controls.
resolve_opengwas_api_metadata <- function(gwasinfo) {
  if (is.null(gwasinfo) || length(gwasinfo) == 0) {
    return(unresolved_metadata_record("no gwasinfo record returned by the OpenGWAS API"))
  }

  # The API represents "not applicable"/"unknown" counts inconsistently
  # (absent field, NULL, NA, or 0) depending on dataset vintage; treat all
  # of those as "not usable" rather than trusting a bare 0 case/control
  # count or sample size.
  as_count <- function(x) {
    if (is.null(x)) return(NA_real_)
    v <- suppressWarnings(as.numeric(x))
    if (length(v) == 0 || is.na(v) || v <= 0) NA_real_ else v
  }

  n_cases <- as_count(gwasinfo$ncase)
  n_controls <- as_count(gwasinfo$ncontrol)
  sample_size <- as_count(gwasinfo$sample_size)
  is_case_control <- !is.na(n_cases) && !is.na(n_controls)

  scale_from_unit <- .classify_stored_effect_scale_from_unit(gwasinfo$unit)
  stored_effect_scale <- if (!is.na(scale_from_unit)) {
    scale_from_unit
  } else if (is_case_control) {
    "log_or"
  } else {
    "sd"
  }

  if (is_case_control) {
    return(data.table(
      resolution_status = "resolved",
      resolution_notes = NA_character_,
      stored_effect_scale = stored_effect_scale,
      sample_size_kind = "case_control",
      sample_size = if (!is.na(sample_size)) sample_size else n_cases + n_controls,
      n_cases = n_cases,
      n_controls = n_controls
    ))
  }

  if (!is.na(sample_size)) {
    return(data.table(
      resolution_status = "resolved",
      resolution_notes = NA_character_,
      stored_effect_scale = stored_effect_scale,
      sample_size_kind = "total",
      sample_size = sample_size,
      n_cases = NA_real_,
      n_controls = NA_real_
    ))
  }

  unresolved_metadata_record("gwasinfo record had neither a usable ncase/ncontrol pair nor a usable sample_size")
}

#' Fetches gwasinfo records for one or more OpenGWAS study IDs from the
#' live API (`POST /api/gwasinfo?id=<id1>&id=<id2>...`; the API's `id`
#' query parameter must be POSTed and repeated once per id -- a GET with no
#' `id` parameter, or an `id` value the query encoder collapses to a single
#' parameter, silently returns every dataset the caller has access to
#' instead of filtering, so this is not just a style choice). Requires an
#' OpenGWAS API JWT (see https://api.opengwas.io/) supplied via the
#' OPENGWAS_JWT environment variable or the `jwt` argument. Automatically
#' chunks `ids` into batches of `chunk_size` (default 100, the API's own
#' flat-cost tier boundary per its published cost table) so a full Store
#' Family batch can be resolved in a handful of requests rather than one
#' per Analysis.
#'
#' This is registry-side metadata acquisition (resolving Analytical Metadata
#' before a build starts, per docs/adr/0017-opengwasdb-owns-shared-analysis-schema.md),
#' not a reusable source reader in the sense
#' docs/adr/0012-manifest-generators-own-orchestration-only.md reserves for
#' OpenGWASDB: nothing here reads GWAS summary-statistics rows, and
#' OpenGWASDB's build engine never needs to call this API itself (by build
#' time, the resolved scalars already live in analyses.tsv). Registry-side
#' network acquisition of external build-input data already has precedent
#' in this repository (resources/scripts/ld-panel/acquire_hgdp1kgp.py).
#'
#' Not exercised by this repository's automated tests: this repository's
#' test suites are documented as data-only and network-free (see
#' resources/scripts/run_all_tests.py), and a live call additionally needs a
#' per-user secret token. resolve_opengwas_api_metadata() above is the
#' tested, independently callable part of this resolver; this function is
#' the thin, untested transport it is deliberately decoupled from.
#'
#' @param ids character vector of OpenGWAS study IDs, e.g. "ieu-a-7".
#' @param jwt OpenGWAS API bearer token.
#' @param chunk_size maximum ids per request.
#' @return a named list of parsed gwasinfo records, keyed by id. An id the
#'   API returned no record for (withdrawn, no access, typo, ...) is simply
#'   absent from the result -- callers must check for missing ids
#'   themselves rather than assume every requested id comes back.
fetch_opengwas_gwasinfo <- function(ids, jwt = Sys.getenv("OPENGWAS_JWT"), chunk_size = 100L) {
  if (!requireNamespace("httr", quietly = TRUE) || !requireNamespace("jsonlite", quietly = TRUE)) {
    stop("fetch_opengwas_gwasinfo() requires the httr and jsonlite packages")
  }
  if (!nzchar(jwt)) {
    stop("fetch_opengwas_gwasinfo() requires an OpenGWAS API JWT (set OPENGWAS_JWT or pass jwt=)")
  }
  if (length(ids) == 0) return(list())

  records <- list()
  chunks <- split(ids, ceiling(seq_along(ids) / chunk_size))
  for (chunk in chunks) {
    query <- setNames(as.list(chunk), rep("id", length(chunk)))
    response <- httr::POST(
      "https://api.opengwas.io/api/gwasinfo",
      query = query,
      httr::add_headers(Authorization = paste("Bearer", jwt))
    )
    httr::stop_for_status(response)
    parsed <- jsonlite::fromJSON(
      httr::content(response, "text", encoding = "UTF-8"),
      simplifyVector = FALSE
    )
    for (record in parsed) records[[record$id]] <- record
  }
  records
}
