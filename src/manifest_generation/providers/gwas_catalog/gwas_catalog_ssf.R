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
# list split on ", ").
#
# Every decision binds to one clause: a count and the words that label it
# within one comma-delimited bit. A count is claimed only by a label in its own
# clause, or, for a numberless label, by the nearest preceding unlabelled
# clause with nothing labelled between; that clause is consumed, so it is not
# counted again as a sample_size (#557). What fits none of these shapes keeps
# the reading the plain ", " split gives it; no count is invented.
#
# Shapes read beyond the plain one:
# - a clause with one label takes its first number, however many times the
#   label repeats ("500 incident cases including recurrent cases");
# - a clause with two labels gives each the nearest unclaimed number before it
#   ("2,853 ... cases and 37,405 ... controls"); labels joined by and/or share
#   one number ("2,253 Korean ancestry cases and controls"); labels one word
#   apart repeat a typo ("... female cases female controls"), so only the later
#   one counts, and takes the clause's number if it has one;
# - "cases,44,187 ..." with no space is a clause boundary after "cases" or
#   "controls" before a count, kept only when a label in that clause or a
#   numberless label after it claims the count (else "cases,1000 Genomes" stays
#   one clause); a final clause with no number that ends in the singular
#   "control" is a label only when it claims such a clause (GCST90301705-7).
.parse_sample_size_components <- function(x) {
  if (is.na(x)) return(NULL)
  s <- gsub(" ", " ", x, fixed = TRUE)
  s <- gsub("(\\d),(?=\\d)", "\\1", s, perl = TRUE)
  base_bits <- unlist(strsplit(s, ",\\s+", perl = TRUE))
  if (!length(base_bits)) return(NULL)
  bare_boundary <- "(?<=\\bcases),(?=\\d)|(?<=\\bcontrols),(?=\\d)"
  pieces <- rbindlist(lapply(base_bits, function(b) {
    p <- strsplit(b, bare_boundary, perl = TRUE)[[1]]
    if (!length(p)) p <- ""
    data.table(text = p, bare = c(FALSE, rep(TRUE, length(p) - 1)))
  }))
  pieces[, undone := FALSE]

  label_word <- function(w) if (startsWith(w, "control")) "controls" else "cases"
  # Numbers and labels of one clause, with their positions.
  scan_clause <- function(b) {
    tok <- gregexpr("\\S+", b, perl = TRUE)[[1]]
    toks <- if (tok[1] == -1) character(0) else substring(b, tok, tok + attr(tok, "match.length") - 1)
    val <- suppressWarnings(as.numeric(toks))
    lab <- gregexpr("\\bcases\\b|\\bcontrols\\b", b, perl = TRUE)[[1]]
    has_lab <- lab[1] != -1
    list(
      npos = as.integer(tok)[!is.na(val)], nval = val[!is.na(val)],
      lpos = if (has_lab) as.integer(lab) else integer(0),
      lend = if (has_lab) as.integer(lab) + attr(lab, "match.length") - 1L else integer(0),
      lab = if (has_lab) vapply(substring(b, lab, lab + attr(lab, "match.length") - 1), label_word, "") else character(0)
    )
  }

  # One record per component: the clause it came from, its label (NA if none)
  # and its own number (NA if none).
  read_clause <- function(i, b) {
    sc <- scan_clause(b)
    first_num <- if (length(sc$nval)) sc$nval[1] else NA_real_
    if (!length(sc$lab)) {
      singular <- grepl("\\bcontrol$", b, perl = TRUE) && !length(sc$nval)
      return(data.table(clause = i, label = NA_character_, num = first_num, singular = singular))
    }
    distinct <- unique(sc$lab)
    if (length(distinct) == 1) {
      return(data.table(clause = i, label = distinct, num = first_num, singular = FALSE))
    }
    fp <- vapply(distinct, function(l) which(sc$lab == l)[1], 1L)
    between <- trimws(substring(b, sc$lend[fp[1]] + 1L, sc$lpos[fp[2]] - 1L))
    if (grepl("^[[:alpha:]]+$", between) && !between %in% c("and", "or")) {
      # Repeated typo: only the later label counts.
      return(data.table(clause = i, label = distinct[2], num = first_num, singular = FALSE))
    }
    claimed <- rep(FALSE, length(sc$nval))
    nums <- rep(NA_real_, length(distinct))
    for (k in seq_along(distinct)) {
      cand <- which(sc$npos < sc$lpos[fp[k]] & !claimed)
      if (length(cand)) {
        nums[k] <- sc$nval[max(cand)]
        claimed[max(cand)] <- TRUE
      } else if (k > 1 && between %in% c("and", "or", "&", "/") && !is.na(nums[k - 1])) {
        nums[k] <- nums[k - 1]
      }
    }
    data.table(clause = i, label = distinct, num = nums, singular = FALSE)
  }

  repeat {
    # Clauses: the plain bits, each bare-comma piece its own clause unless undone.
    text <- character(0); bare <- logical(0); first_piece <- integer(0)
    for (k in seq_len(nrow(pieces))) {
      if (pieces$bare[k] && pieces$undone[k] && length(text)) {
        text[length(text)] <- paste0(text[length(text)], ",", pieces$text[k])
      } else {
        text <- c(text, pieces$text[k]); bare <- c(bare, pieces$bare[k]); first_piece <- c(first_piece, k)
      }
    }
    comps <- rbindlist(lapply(seq_along(text), function(i) read_clause(i, text[i])))
    used <- rep(FALSE, length(text))
    labelled <- seq_along(text) %in% comps$clause[!is.na(comps$label)]
    # Numberless labels (and singular "control") look back through unlabelled clauses.
    for (r in seq_len(nrow(comps))) {
      if (!(comps$singular[r] || (!is.na(comps$label[r]) && is.na(comps$num[r])))) next
      j <- comps$clause[r] - 1L
      while (j >= 1 && !labelled[j]) {
        c_j <- comps[clause == j]
        if (!used[j] && !is.na(c_j$num[1])) {
          if (!comps$singular[r] || bare[j]) {
            comps$num[r] <- c_j$num[1]
            used[j] <- TRUE
            if (comps$singular[r]) { comps$label[r] <- "controls"; labelled[comps$clause[r]] <- TRUE }
          }
          break
        }
        j <- j - 1L
      }
    }
    # A bare-comma clause stays only if a label claims it.
    unjustified <- which(bare & !labelled & !used)
    if (!length(unjustified)) break
    pieces[first_piece[unjustified], undone := TRUE]
  }
  comps <- comps[!(is.na(label) & used[clause])]
  comps[, n := ifelse(is.na(num), 0, num)]
  out <- comps[, .(n, what = ifelse(is.na(label), "sample_size", label))]
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
