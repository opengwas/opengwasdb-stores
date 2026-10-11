## R mirror of resources/generators/lib/indexed_subsets.py (stores #210).
##
## Dense and Hybrid Release Bundles declare the maintained `hm3` Indexed
## Variant Subset by default (epic opengwas/opengwasdb-stores#214). The
## declaration is read from the single maintained Reference Resource at
## resources/reference-resources/hapmap3-hg38/resource.yaml (stores #208), so no
## writer copies the list path, assembly or checksum.
##
## Ragged releases have no Dense Component and emit nothing. An operator can
## deliberately omit the default on a Dense or Hybrid release with an explicit
## `indexed_subsets: []` plus a written `indexed_subsets_opt_out` reason, which
## the writer records in release.yaml notes. An empty list without a reason, a
## malformed entry, or a Ragged declaration is an error.
##
## Keep in step with the Python helper; both read the same one declaration.

INDEXED_SUBSETS_KEY <- "indexed_subsets"
INDEXED_SUBSETS_OPT_OUT_KEY <- "indexed_subsets_opt_out"
INDEXED_SUBSET_LAYOUTS <- c("dense", "hybrid")
DEFAULT_INDEXED_SUBSET_NAME <- "hm3"
DEFAULT_INDEXED_SUBSET_RESOURCE_ID <- "hapmap3-hg38"
INDEXED_SUBSET_REQUIRED_KEYS <- c("name", "resource_id", "variant_list", "reference_assembly")

load_variant_list_resource <- function(resource_id, root) {
  path <- file.path(root, "resources", "reference-resources", resource_id, "resource.yaml")
  if (!file.exists(path)) {
    stop(sprintf(
      "resource_id '%s' names no maintained Reference Resource: %s does not exist",
      resource_id, path
    ), call. = FALSE)
  }
  document <- read_yaml(path)
  if (!identical(document$kind, "variant_list")) {
    stop(sprintf(
      "resource_id '%s' has kind '%s', not 'variant_list'",
      resource_id, as.character(document$kind %||% NA_character_)
    ), call. = FALSE)
  }
  document
}

default_hm3_declaration <- function(root) {
  document <- load_variant_list_resource(DEFAULT_INDEXED_SUBSET_RESOURCE_ID, root)
  location <- document$location
  assembly <- document$genome_build
  checksum <- document$checksums$alid_sha256
  if (is.null(location) || !nzchar(as.character(location)) ||
      is.null(assembly) || !nzchar(as.character(assembly)) ||
      is.null(checksum) || !nzchar(as.character(checksum))) {
    stop(sprintf(
      "%s/resource.yaml is missing location, genome_build or checksums.alid_sha256; the default Indexed Variant Subset cannot be declared",
      DEFAULT_INDEXED_SUBSET_RESOURCE_ID
    ), call. = FALSE)
  }
  list(
    name = DEFAULT_INDEXED_SUBSET_NAME,
    resource_id = DEFAULT_INDEXED_SUBSET_RESOURCE_ID,
    variant_list = as.character(location),
    reference_assembly = as.character(assembly),
    checksum = as.character(checksum)
  )
}

.is_sha256 <- function(value) {
  is.character(value) && length(value) == 1L && !is.na(value) &&
    grepl("^[0-9a-fA-F]{64}$", value)
}

.validate_indexed_subset_entry <- function(entry, where) {
  if (!is.list(entry) || is.null(names(entry))) {
    stop(sprintf("%s must be a mapping", where), call. = FALSE)
  }
  for (key in INDEXED_SUBSET_REQUIRED_KEYS) {
    value <- entry[[key]]
    if (is.null(value) || !is.character(value) || length(value) != 1L ||
        is.na(value) || !nzchar(trimws(value))) {
      stop(sprintf("%s requires a non-empty string '%s'", where, key), call. = FALSE)
    }
  }
  checksum <- entry[["checksum"]]
  if (!is.null(checksum) && !.is_sha256(checksum)) {
    stop(sprintf(
      "%s checksum must be a 64-character hex sha256, got '%s'", where, checksum
    ), call. = FALSE)
  }
  entry
}

## Returns list(subsets = <list or NULL>, note = <string or NULL>).
## subsets NULL means "emit no indexed_subsets key at all" (Ragged).
## subsets list() means "emit the explicit empty list of a reviewable opt-out".
resolve_indexed_subsets <- function(document, layout, root) {
  present <- INDEXED_SUBSETS_KEY %in% names(document)
  declared <- document[[INDEXED_SUBSETS_KEY]]
  reason <- document[[INDEXED_SUBSETS_OPT_OUT_KEY]]
  reason_text <- if (is.null(reason)) NULL else trimws(as.character(reason))
  if (!is.null(reason_text) && !nzchar(reason_text)) reason_text <- NULL

  if (present && is.null(declared)) {
    stop(sprintf(
      "%s must be a list of declarations, not null; omit the key to accept the default or set [] to opt out",
      INDEXED_SUBSETS_KEY
    ), call. = FALSE)
  }

  if (!(layout %in% INDEXED_SUBSET_LAYOUTS)) {
    if (present && length(declared) > 0) {
      stop(sprintf(
        "%s may be declared only for %s layout, not '%s'; a Ragged Store Release has no Dense Component to index",
        INDEXED_SUBSETS_KEY, paste(INDEXED_SUBSET_LAYOUTS, collapse = "/"), layout
      ), call. = FALSE)
    }
    if (!is.null(reason_text)) {
      stop(sprintf(
        "%s is only meaningful for a Dense or Hybrid release, not '%s'",
        INDEXED_SUBSETS_OPT_OUT_KEY, layout
      ), call. = FALSE)
    }
    return(list(subsets = NULL, note = NULL))
  }

  if (!present) {
    if (!is.null(reason_text)) {
      stop(sprintf(
        "%s needs an explicit %s: [] to omit the default",
        INDEXED_SUBSETS_OPT_OUT_KEY, INDEXED_SUBSETS_KEY
      ), call. = FALSE)
    }
    return(list(subsets = list(default_hm3_declaration(root)), note = NULL))
  }

  if (!is.list(declared)) {
    stop(sprintf(
      "%s must be a list of declarations, not %s",
      INDEXED_SUBSETS_KEY, class(declared)[[1]]
    ), call. = FALSE)
  }

  if (length(declared) == 0) {
    if (is.null(reason_text)) {
      stop(sprintf(
        "%s: [] deliberately omits the default Indexed Variant Subset; record why with a non-empty %s reason",
        INDEXED_SUBSETS_KEY, INDEXED_SUBSETS_OPT_OUT_KEY
      ), call. = FALSE)
    }
    return(list(subsets = list(), note = reason_text))
  }

  if (!is.null(reason_text)) {
    stop(sprintf(
      "%s contradicts a non-empty %s",
      INDEXED_SUBSETS_OPT_OUT_KEY, INDEXED_SUBSETS_KEY
    ), call. = FALSE)
  }
  validated <- lapply(seq_along(declared), function(index) {
    .validate_indexed_subset_entry(
      declared[[index]],
      sprintf("%s[%d]", INDEXED_SUBSETS_KEY, index)
    )
  })
  list(subsets = validated, note = NULL)
}

indexed_subsets_opt_out_note <- function(reason) {
  paste0(
    "Indexed Variant Subsets (#210): the default hm3 Indexed Variant Subset ",
    "is intentionally omitted: ", reason
  )
}

## release.yaml notes with the reviewed opt-out sentence appended, when present.
notes_with_indexed_subsets_opt_out <- function(notes, layout, cfg, root) {
  resolved <- resolve_indexed_subsets(cfg, layout, root)
  if (is.null(resolved$note)) return(notes)
  paste0(
    notes, if (nzchar(notes)) "\n\n" else "",
    indexed_subsets_opt_out_note(resolved$note)
  )
}
