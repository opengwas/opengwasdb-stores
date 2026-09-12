# Resolver for one row of FinnGen's public summary-statistics manifest.
suppressPackageStartupMessages(library(data.table))
source("generators/lib/metadata_resolvers/contract.R")

resolve_finngen_manifest_metadata <- function(record) {
  if (is.null(record) || !length(record)) {
    return(unresolved_metadata_record("no FinnGen endpoint manifest record"))
  }
  value <- function(name) {
    x <- record[[name]]
    if (is.null(x) || !length(x)) NA else x[[1]]
  }
  count <- function(name, allow_zero = FALSE) {
    x <- suppressWarnings(as.double(value(name)))
    if (!length(x) || is.na(x) || (!allow_zero && x <= 0) || (allow_zero && x < 0)) NA_real_ else x
  }
  category <- trimws(tolower(as.character(value("category"))))
  n_cases <- count("num_cases")
  n_controls <- count("num_controls", allow_zero = TRUE)
  quantitative <- identical(category, "quantitative endpoints")

  if (quantitative && !is.na(n_cases)) {
    return(data.table(
      resolution_status = "resolved", resolution_notes = NA_character_,
      stored_effect_scale = "sd", sample_size_kind = "total", sample_size = n_cases,
      n_cases = NA_real_, n_controls = NA_real_
    ))
  }
  if (!quantitative && !is.na(n_cases) && !is.na(n_controls) && n_controls > 0) {
    return(data.table(
      resolution_status = "resolved", resolution_notes = NA_character_,
      stored_effect_scale = "log_or", sample_size_kind = "case_control",
      sample_size = n_cases + n_controls, n_cases = n_cases, n_controls = n_controls
    ))
  }
  unresolved_metadata_record(
    "FinnGen manifest row was neither a quantitative endpoint with num_cases=N nor a case-control endpoint with positive counts"
  )
}
