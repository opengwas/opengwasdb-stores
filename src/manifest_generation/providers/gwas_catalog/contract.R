# Shared output-contract helper for metadata resolvers (see
# docs/release-metadata-schema.md, "Metadata resolvers"). Every resolver
# (resolve_gwas_catalog_ssf_metadata(), resolve_opengwas_api_metadata(), ...)
# returns the same seven-column shape; this builds the `unresolved` case so
# each resolver doesn't repeat that shape by hand.
#
# It lives beside gwas_catalog_ssf.R, the resolver that outlives the legacy R
# generators (ADR 0038). The legacy FinnGen and OpenGWAS resolvers in
# resources/generators/lib/metadata_resolvers/ source it from here, by this one
# path, until #288 and #289 retire them.

suppressPackageStartupMessages(library(data.table))

#' Build the standard "could not resolve" record every metadata resolver
#' returns when the underlying provider metadata does not support a
#' resolution -- never a silently defaulted value.
#'
#' @param notes free-text reason, filled into `resolution_notes`.
#' @return a one-row data.table matching the resolver contract's shape:
#'   resolution_status ("unresolved"), resolution_notes, stored_effect_scale,
#'   sample_size_kind, sample_size, n_cases, n_controls (all NA).
unresolved_metadata_record <- function(notes) {
  data.table(
    resolution_status = "unresolved",
    resolution_notes = notes,
    stored_effect_scale = NA_character_,
    sample_size_kind = NA_character_,
    sample_size = NA_real_,
    n_cases = NA_real_,
    n_controls = NA_real_
  )
}
