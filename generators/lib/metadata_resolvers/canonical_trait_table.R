# Trait Ontology Mapping resolver backed by a curated Canonical Trait Mapping
# Table Reference Resource (reference-resources/canonical-trait-mapping-efo/,
# see its README.md for the lookup contract). See
# docs/release-metadata-schema.md, "Trait ontology mapping resolver", and
# docs/adr/0021-trait-ontology-mapping-lookup-lives-in-registry.md for why
# this lookup lives here rather than in OpenGWASDB.
suppressPackageStartupMessages(library(data.table))
source("generators/lib/metadata_resolvers/ontology_contract.R")

.normalise_trait_label <- function(x) trimws(tolower(x))

#' Load a Canonical Trait Mapping Table (e.g.
#' reference-resources/canonical-trait-mapping-efo/mapping.tsv) for use with
#' resolve_trait_ontology_mapping().
#'
#' @param path repo-relative or absolute path to the table's TSV file.
#' @return a data.table with columns trait_label, trait_ontology_id,
#'   trait_ontology_label, plus a normalised join key.
load_canonical_trait_table <- function(path) {
  table <- fread(path, sep = "\t", na.strings = "", colClasses = "character")
  table[, .trait_label_key := .normalise_trait_label(trait_label)]
  table
}

#' Vectorised wrapper around resolve_trait_ontology_mapping() for a whole
#' analyses table at once, so generators don't each write their own
#' per-row loop.
#'
#' @param trait_labels,source_ontology_ids,source_ontology_labels equal-length
#'   character vectors, one entry per Analysis.
#' @param canonical_table as per resolve_trait_ontology_mapping().
#' @return a data.table with one row per input: trait_ontology_id,
#'   trait_ontology_label, trait_ontology_mapping_method.
resolve_trait_ontology_mappings <- function(trait_labels, source_ontology_ids,
                                             source_ontology_labels,
                                             canonical_table = NULL) {
  resolved <- rbindlist(lapply(seq_along(trait_labels), function(i) {
    resolve_trait_ontology_mapping(
      trait_label = trait_labels[i],
      source_ontology_id = source_ontology_ids[i],
      source_ontology_label = source_ontology_labels[i],
      canonical_table = canonical_table
    )
  }))
  resolved[, .(trait_ontology_id, trait_ontology_label, trait_ontology_mapping_method)]
}

#' Find one declared Reference Resource entry (as used in a generator's
#' `reference_resources` config list) by resource_id, or NULL if absent.
find_reference_resource_cfg <- function(reference_resources, resource_id) {
  for (r in reference_resources) {
    if (identical(r$resource_id, resource_id)) return(r)
  }
  NULL
}

#' Load the Canonical Trait Mapping Table declared under `resource_id:
#' canonical-trait-mapping-efo` in a generator's `reference_resources`
#' config, or NULL when not declared -- callers then resolve every Trait as
#' `unmapped` (when unsourced) rather than erroring, since not every family
#' needs this fallback (e.g. gwas-ssf-ragged's GWAS Catalog source already
#' supplies its own mapping for every currently-built bundle).
#'
#' @param reference_resources a generator config's `reference_resources`
#'   list (`cfg$reference_resources`).
#' @param root repo root, as returned by generate.R's repo_root(), used to
#'   resolve a repo-relative `location`. Resolved locally rather than via a
#'   caller-supplied path_abs(), so this file has no hidden dependency on
#'   which generator sources it.
load_canonical_trait_table_from_cfg <- function(reference_resources, root,
                                                 resource_id = "canonical-trait-mapping-efo") {
  resource <- find_reference_resource_cfg(reference_resources, resource_id)
  if (is.null(resource)) return(NULL)
  location <- resource$location
  path <- if (grepl("^/", location)) location else file.path(root, location)
  load_canonical_trait_table(path)
}

#' Resolve one Analysis's Trait Ontology Mapping.
#'
#' Conforms to the ontology-resolver contract in
#' docs/release-metadata-schema.md: `resolution_status` is always explicit
#' (`resolved`/`unresolved`), and a Trait that can't be mapped gets an
#' explicit `unmapped` record, never a defaulted value.
#'
#' @param trait_label the Analysis's own trait/analyte label, used as the
#'   lookup key when no source-provided ontology ID exists.
#' @param source_ontology_id ontology ID already supplied by the Source
#'   Collection (e.g. GWAS Catalog's MAPPED_TRAIT_URI, CURIE-normalised),
#'   or NA/blank when the source supplies none.
#' @param source_ontology_label ontology label paired with
#'   `source_ontology_id`, or NA/blank.
#' @param canonical_table a table loaded via load_canonical_trait_table(),
#'   or NULL/empty when no fallback table is configured.
#' @return a one-row data.table with columns resolution_status,
#'   trait_ontology_id, trait_ontology_label,
#'   trait_ontology_mapping_method, resolution_notes.
resolve_trait_ontology_mapping <- function(trait_label, source_ontology_id,
                                            source_ontology_label,
                                            canonical_table = NULL) {
  if (!is.na(source_ontology_id) && nzchar(source_ontology_id)) {
    return(data.table(
      resolution_status = "resolved",
      trait_ontology_id = source_ontology_id,
      trait_ontology_label = source_ontology_label,
      trait_ontology_mapping_method = "source_provided",
      resolution_notes = NA_character_
    ))
  }

  if (is.null(canonical_table) || !nrow(canonical_table) || is.na(trait_label) || !nzchar(trait_label)) {
    return(unresolved_ontology_mapping_record(
      "no source-provided ontology ID and no canonical-table match available"
    ))
  }

  key <- .normalise_trait_label(trait_label)
  hit <- canonical_table[.trait_label_key == key]
  if (!nrow(hit)) {
    return(unresolved_ontology_mapping_record(
      sprintf("trait_label %s not found in canonical trait mapping table", shQuote(trait_label))
    ))
  }

  data.table(
    resolution_status = "resolved",
    trait_ontology_id = hit$trait_ontology_id[1],
    trait_ontology_label = hit$trait_ontology_label[1],
    trait_ontology_mapping_method = "canonical_table_lookup",
    resolution_notes = NA_character_
  )
}
