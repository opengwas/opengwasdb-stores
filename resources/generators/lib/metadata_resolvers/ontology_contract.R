# Shared output-contract helper for Trait Ontology Mapping resolvers (see
# docs/release-metadata-schema.md, "Trait ontology mapping resolver", and
# CONTEXT.md's "Trait Ontology Mapping"/"Trait Ontology Mapping Method").
# Distinct from contract.R's unresolved_metadata_record(): that one covers
# the effect-scale/sample-size resolver family, this one covers ontology
# mapping, which has its own output shape.

suppressPackageStartupMessages(library(data.table))

#' GWAS Catalog's MAPPED_TRAIT_URI is a full OBO Library PURL
#' (http://www.ebi.ac.uk/efo/EFO_0008012, http://purl.obolibrary.org/obo/OBA_2050236,
#' ...), but trait_ontology_id must be a CURIE (EFO:0008012) per
#' docs/release-metadata-schema.md. Takes the URI's last path segment and
#' splits it on the last underscore into prefix/local-id, per OBO convention.
#' NA/blank/already-CURIE-shaped values (no "/") pass through unchanged, as
#' does a "/"-containing value whose last segment has no underscore (not a
#' real OBO ID shape) -- never a guessed/corrupted result.
obo_uri_to_curie <- function(x) {
  is_uri <- !is.na(x) & nzchar(x) & grepl("/", x, fixed = TRUE) & grepl("_", sub(".*/", "", x), fixed = TRUE)
  out <- x
  segment <- sub(".*/", "", x[is_uri])
  out[is_uri] <- paste0(toupper(sub("_[^_]*$", "", segment)), ":", sub(".*_", "", segment))
  out
}

#' Build the standard "could not map" record every ontology-mapping resolver
#' returns when a Trait's ontology term cannot be established -- never a
#' silently defaulted value.
#'
#' @param notes free-text reason, filled into `resolution_notes`.
#' @return a one-row data.table matching the ontology-resolver contract's
#'   shape: resolution_status ("unresolved"), trait_ontology_id (NA),
#'   trait_ontology_label (NA), trait_ontology_mapping_method ("unmapped"),
#'   resolution_notes.
unresolved_ontology_mapping_record <- function(notes) {
  data.table(
    resolution_status = "unresolved",
    trait_ontology_id = NA_character_,
    trait_ontology_label = NA_character_,
    trait_ontology_mapping_method = "unmapped",
    resolution_notes = notes
  )
}
