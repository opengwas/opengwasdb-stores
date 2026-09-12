#!/usr/bin/env Rscript
suppressPackageStartupMessages(library(data.table))
source("generators/lib/metadata_resolvers/ontology_contract.R")
source("generators/lib/metadata_resolvers/canonical_trait_table.R")

check <- function(x, message) if (!isTRUE(x)) stop(message, call. = FALSE)

# obo_uri_to_curie(): real shapes observed in gwas-ssf-ragged bundles.
check(
  obo_uri_to_curie("http://www.ebi.ac.uk/efo/EFO_0008012") == "EFO:0008012",
  "EFO PURL -> CURIE"
)
check(
  obo_uri_to_curie("http://purl.obolibrary.org/obo/OBA_2050236") == "OBA:2050236",
  "OBA PURL -> CURIE"
)
check(is.na(obo_uri_to_curie(NA_character_)), "NA passes through unchanged")
check(identical(obo_uri_to_curie(""), ""), "blank passes through unchanged")
check(
  identical(obo_uri_to_curie("EFO:0008012"), "EFO:0008012"),
  "already-CURIE-shaped value (no '/') passes through unchanged"
)
check(
  identical(obo_uri_to_curie("http://example.org/FOO"), "http://example.org/FOO"),
  "a '/'-containing value whose last segment has no underscore is not a real OBO ID shape -- passes through unchanged, never corrupted into FOO:FOO"
)

# resolve_trait_ontology_mapping(): source-provided path -- an ontology ID
# already supplied by the Source Collection wins outright, no table lookup.
r <- resolve_trait_ontology_mapping(
  trait_label = "Type 2 diabetes", source_ontology_id = "EFO:0001360",
  source_ontology_label = "type II diabetes mellitus", canonical_table = NULL
)
check(
  r$resolution_status == "resolved" && r$trait_ontology_mapping_method == "source_provided" &&
    r$trait_ontology_id == "EFO:0001360",
  "source-provided ontology ID wins outright"
)

# Fabricated fixture table, not real curated data -- exercises the exact-
# match lookup contract documented in
# reference-resources/canonical-trait-mapping-efo/README.md.
fixture_path <- tempfile(fileext = ".tsv")
writeLines(c(
  "trait_label\ttrait_ontology_id\ttrait_ontology_label",
  "Fixture height\tEFO:9999001\tFixture height ontology label",
  "  Fixture Height  \tEFO:9999002\tshould never be reached (first match wins)"
), fixture_path)
canonical_table <- load_canonical_trait_table(fixture_path)

r <- resolve_trait_ontology_mapping(
  trait_label = "fixture height", source_ontology_id = NA_character_,
  source_ontology_label = NA_character_, canonical_table = canonical_table
)
check(
  r$resolution_status == "resolved" && r$trait_ontology_mapping_method == "canonical_table_lookup" &&
    r$trait_ontology_id == "EFO:9999001",
  "canonical-table exact match, case/whitespace-insensitive, first match wins"
)

r <- resolve_trait_ontology_mapping(
  trait_label = "Fixture nonexistent trait", source_ontology_id = NA_character_,
  source_ontology_label = NA_character_, canonical_table = canonical_table
)
check(
  r$resolution_status == "unresolved" && r$trait_ontology_mapping_method == "unmapped" &&
    is.na(r$trait_ontology_id) && !is.na(r$resolution_notes),
  "no source ID and no table match -> explicit unmapped, never a guess"
)

r <- resolve_trait_ontology_mapping(
  trait_label = NA_character_, source_ontology_id = NA_character_,
  source_ontology_label = NA_character_, canonical_table = NULL
)
check(
  r$resolution_status == "unresolved" && r$trait_ontology_mapping_method == "unmapped",
  "no source ID, no table declared, no trait label -> unmapped"
)

# resolve_trait_ontology_mappings(): vectorised wrapper both generators call.
mappings <- resolve_trait_ontology_mappings(
  trait_labels = c("fixture height", "unmapped one"),
  source_ontology_ids = c(NA_character_, NA_character_),
  source_ontology_labels = c(NA_character_, NA_character_),
  canonical_table = canonical_table
)
check(
  nrow(mappings) == 2 &&
    mappings$trait_ontology_mapping_method[1] == "canonical_table_lookup" &&
    mappings$trait_ontology_mapping_method[2] == "unmapped",
  "vectorised wrapper resolves each row independently"
)

cat("ALL 10 CHECKS PASSED\n")
