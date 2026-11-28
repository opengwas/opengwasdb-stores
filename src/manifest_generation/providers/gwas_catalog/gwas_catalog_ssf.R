# Metadata resolver for the `gwas-catalog-ssf` Source Collection (issue #48;
# see docs/release-metadata-schema.md, "Metadata resolvers"). Resolves the
# `derive`-stage analytical fields analyses.tsv needs -- stored effect scale,
# sample-size kind, sample size, and case/control counts -- from GWAS
# Catalog's own study-level metadata (the "INITIAL SAMPLE SIZE" free-text
# field), never from the harmonised summary-statistics file itself.
#
# Extracted from ebi-studies.r's original inline
# parse_sample_size()/summarise_sample_size() so the same detection logic is
# independently testable and reusable outside store-candidate categorisation.
# Deliberately depends only on base R + data.table (not tidyverse): callers
# such as generate.R run in Pixi environments that do not include the
# `reporting` feature tidyverse lives in.

suppressPackageStartupMessages(library(data.table))
source("src/manifest_generation/providers/gwas_catalog/contract.R")

# Splits one GWAS Catalog "INITIAL SAMPLE SIZE" string into its labelled
# components, e.g. "10,007 cases, 474,591 controls" -> two rows tagged
# "cases"/"controls", or "1,000 European ancestry individuals" -> one row
# tagged "sample_size" (no case/control label present). Returns NULL when
# nothing parseable is present (empty string, or no numeric component).
#
# A "cases"/"controls" bit can carry no number of its own, because the count
# sits earlier in the string: "181,522 European ancestry, unknown cases", or
# "5,085 European, African, ... and South Asian ancestry cases" (an ancestry
# list split on ", "). Its count is then the first number of the nearest
# preceding bit that has one and is not itself labelled; that bit is consumed,
# so it is not counted again as a sample_size (#557). Bits are split on ", " or
# on a bare "," before a digit, and a bit ending in "control" reads as "controls".
.parse_sample_size_components <- function(x) {
  if (is.na(x)) return(NULL)
  bits <- unlist(strsplit(gsub("(\\d),(?=\\d)", "\\1", x, perl = TRUE), ",\\s+|,(?=\\d)", perl = TRUE))
  first_number <- function(bit) {
    v <- suppressWarnings(as.numeric(strsplit(bit, " ")[[1]]))
    v <- v[!is.na(v)]
    if (length(v) == 0) NA_real_ else v[1]
  }
  # A bit holding both words ("... cases female controls") takes the later one.
  last_word <- function(bit) {
    w <- regmatches(bit, gregexpr("cases|controls|control$", bit))[[1]]
    if (length(w) == 0) NA_character_ else if (startsWith(w[length(w)], "control")) "controls" else "cases"
  }
  label <- vapply(bits, last_word, character(1), USE.NAMES = FALSE)
  num <- vapply(bits, first_number, numeric(1), USE.NAMES = FALSE)
  consumed <- rep(FALSE, length(bits))
  n <- rep(0, length(bits))
  for (i in seq_along(bits)) {
    if (is.na(label[i])) next
    if (!is.na(num[i])) { n[i] <- num[i]; next }
    j <- which(is.na(label[seq_len(i - 1)]) & !is.na(num[seq_len(i - 1)]) & !consumed[seq_len(i - 1)])
    if (length(j)) {
      j <- max(j)
      n[i] <- num[j]
      consumed[j] <- TRUE
    }
  }
  is_other <- is.na(label) & !consumed
  n[is_other] <- ifelse(is.na(num[is_other]), 0, num[is_other])
  keep <- !consumed
  out <- data.table(n = n[keep], what = ifelse(is.na(label), "sample_size", label)[keep])
  if (!nrow(out)) return(NULL)
  out[order(match(what, c("cases", "controls", "sample_size")))]
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
