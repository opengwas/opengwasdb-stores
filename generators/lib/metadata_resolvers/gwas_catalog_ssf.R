# Metadata resolver for the `gwas-catalog-ssf` Source Collection (issue #48;
# see docs/release-metadata-schema.md, "Metadata resolvers"). Resolves the
# `derive`-stage analytical fields analyses.tsv needs -- stored effect scale,
# sample-size kind, sample size, and case/control counts -- from GWAS
# Catalog's own study-level metadata (the "INITIAL SAMPLE SIZE" free-text
# field), never from the harmonised summary-statistics file itself.
#
# Extracted from resources/scripts/ebi-studies.r's original inline
# parse_sample_size()/summarise_sample_size() so the same detection logic is
# independently testable and reusable outside store-candidate categorisation.
# Deliberately depends only on base R + data.table (not tidyverse): callers
# such as generate.R run in Pixi environments that do not include the
# `reporting` feature tidyverse lives in.

suppressPackageStartupMessages(library(data.table))
source("generators/lib/metadata_resolvers/contract.R")

# Splits one GWAS Catalog "INITIAL SAMPLE SIZE" string into its labelled
# components, e.g. "10,007 cases, 474,591 controls" -> two rows tagged
# "cases"/"controls", or "1,000 European ancestry individuals" -> one row
# tagged "sample_size" (no case/control label present). Returns NULL when
# nothing parseable is present (empty string, or no numeric component).
.parse_sample_size_components <- function(x) {
  if (is.na(x)) return(NULL)
  bits <- unlist(strsplit(gsub("(\\d),(?=\\d)", "\\1", x, perl = TRUE), ", "))
  cases_bits <- grep("cases", bits, value = TRUE)
  controls_bits <- grep("controls", bits, value = TRUE)
  other_bits <- bits[!bits %in% c(cases_bits, controls_bits)]

  parse_bit <- function(bit_strings, label) {
    if (length(bit_strings) == 0) return(NULL)
    n <- vapply(strsplit(bit_strings, " "), function(toks) {
      v <- suppressWarnings(as.numeric(toks))
      v <- v[!is.na(v)]
      if (length(v) == 0) 0 else v[1]
    }, numeric(1))
    data.table(n = n, what = label)
  }

  out <- rbindlist(list(
    parse_bit(cases_bits, "cases"),
    parse_bit(controls_bits, "controls"),
    parse_bit(other_bits, "sample_size")
  ))
  if (!nrow(out)) return(NULL)
  out
}

#' Resolve one GWAS Catalog study's analytical metadata from its raw
#' "INITIAL SAMPLE SIZE" free-text field.
#'
#' Conforms to the metadata-resolver contract in
#' docs/release-metadata-schema.md: `resolution_status` is always explicit
#' (`resolved`/`unresolved`), and every other field is left `NA` rather than
#' defaulted to a plausible-looking value when resolution fails.
#'
#' @param initial_sample_size character scalar, the GWAS Catalog studies
#'   table's INITIAL SAMPLE SIZE field for one STUDY ACCESSION.
#' @return a one-row data.table with columns `resolution_status`,
#'   `resolution_notes`, `stored_effect_scale`, `sample_size_kind`,
#'   `sample_size`, `n_cases`, `n_controls`.
resolve_gwas_catalog_ssf_metadata <- function(initial_sample_size) {
  parsed <- .parse_sample_size_components(initial_sample_size)
  if (is.null(parsed)) {
    return(unresolved_metadata_record("INITIAL SAMPLE SIZE had no parseable case/control/quantitative counts"))
  }

  n_cases <- sum(parsed$n[parsed$what == "cases"])
  n_controls <- sum(parsed$n[parsed$what == "controls"])
  n_quantitative <- sum(parsed$n[parsed$what == "sample_size"])
  total <- n_cases + n_controls + n_quantitative
  if (total == 0) {
    return(unresolved_metadata_record("parsed sample-size components summed to zero"))
  }

  is_case_control <- (n_cases + n_controls) > 0
  data.table(
    resolution_status = "resolved",
    resolution_notes = NA_character_,
    stored_effect_scale = if (is_case_control) "log_or" else "sd",
    sample_size_kind = if (is_case_control) "case_control" else "total",
    sample_size = total,
    n_cases = if (is_case_control) n_cases else NA_real_,
    n_controls = if (is_case_control) n_controls else NA_real_
  )
}
