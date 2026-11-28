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
# preceding unlabelled bit that has one, searching back only through unlabelled
# bits (never across another cases/controls bit); that bit is consumed, so it
# is not counted again as a sample_size (#557).
#
# Shapes read beyond the plain one:
# - a bit holding both labels and as many numbers ("2,853 European ancestry
#   cases and 37,405 European ancestry controls") gives each number to the
#   label that follows it; with one number ("2,253 Korean ancestry cases and
#   controls") each label takes it; with none (a repeated typo, "... female
#   cases female controls") the later label looks back for its count;
# - "cases,44,187 ..." with no space is split after "cases"/"controls" before a
#   count that a later label claims, and only after "cases" is a trailing
#   singular "control" a label (GCST90301705-7).
.parse_sample_size_components <- function(x) {
  if (is.na(x)) return(NULL)
  s <- gsub(" ", " ", x, fixed = TRUE)
  s <- gsub("(\\d),(?=\\d)", "\\1", s, perl = TRUE)
  # A bare comma after a label, before a count that a later label claims.
  bare_comma <- grepl("\\bcases,\\d", s, perl = TRUE)
  s <- gsub("(\\b(?:cases|controls)),(?=\\d[^,]*(?:,.*)?(?:\\b(?:cases|controls)\\b|\\bcontrol$))", "\\1, ", s, perl = TRUE)
  bits <- unlist(strsplit(s, ",\\s+", perl = TRUE))
  label_pattern <- if (bare_comma) "\\bcases\\b|\\bcontrols\\b|\\bcontrol$" else "\\bcases\\b|\\bcontrols\\b"
  numbers_in <- function(bit) {
    v <- suppressWarnings(as.numeric(strsplit(bit, " ")[[1]]))
    v[!is.na(v)]
  }
  classify_case_control_label <- function(word) if (startsWith(word, "control")) "controls" else "cases"

  # One component per bit, or two for a dual-label bit with two numbers.
  comp_bit <- integer(0); comp_label <- character(0); comp_num <- numeric(0)
  for (i in seq_along(bits)) {
    words <- regmatches(bits[i], gregexpr(label_pattern, bits[i], perl = TRUE))[[1]]
    nums <- numbers_in(bits[i])
    if (length(words) == 0) {
      comp_bit <- c(comp_bit, i); comp_label <- c(comp_label, NA_character_)
      comp_num <- c(comp_num, if (length(nums)) nums[1] else NA_real_)
    } else if (length(words) >= 2 && length(nums) >= 1) {
      # Each label takes the number that precedes it; with fewer numbers than
      # labels ("2,253 Korean ancestry cases and controls") each takes the first.
      for (k in seq_along(words)) {
        comp_bit <- c(comp_bit, i); comp_label <- c(comp_label, classify_case_control_label(words[k]))
        comp_num <- c(comp_num, if (length(nums) == length(words)) nums[k] else nums[1])
      }
    } else {
      comp_bit <- c(comp_bit, i)
      comp_label <- c(comp_label, classify_case_control_label(words[length(words)]))
      comp_num <- c(comp_num, if (length(nums)) nums[1] else NA_real_)
    }
  }
  consumed <- rep(FALSE, length(comp_bit))
  n <- ifelse(is.na(comp_num), 0, comp_num)
  for (i in seq_along(comp_bit)) {
    if (is.na(comp_label[i]) || !is.na(comp_num[i])) next
    j <- i - 1
    while (j >= 1 && is.na(comp_label[j])) {
      if (!consumed[j] && !is.na(comp_num[j])) {
        n[i] <- comp_num[j]
        consumed[j] <- TRUE
        break
      }
      j <- j - 1
    }
  }
  keep <- !consumed
  out <- data.table(n = n[keep], what = ifelse(is.na(comp_label), "sample_size", comp_label)[keep])
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
