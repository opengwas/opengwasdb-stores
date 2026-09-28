#!/usr/bin/env Rscript
suppressPackageStartupMessages(library(data.table))
source("resources/generators/lib/metadata_resolvers/ontology_contract.R")
source("resources/generators/lib/metadata_resolvers/canonical_trait_table.R")

n_checks <- 0L
check <- function(x, message) {
  n_checks <<- n_checks + 1L
  if (!isTRUE(x)) stop(message, call. = FALSE)
}

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
# match lookup contract and the optional provenance columns documented in
# resources/reference-resources/canonical-trait-mapping-efo/README.md.
fixture_path <- tempfile(fileext = ".tsv")
writeLines(c(
  paste(
    "trait_label", "trait_ontology_id", "trait_ontology_label",
    "ontology_release", "chooser_id", "confidence", "runner_up_margin",
    "review_status", "reviewer", "reviewed_at",
    sep = "\t"
  ),
  paste(
    "Fixture height", "EFO:9999001", "Fixture height ontology label",
    "2024-01-01", "fixture-chooser", "0.97", "0.42", "human_reviewed",
    "Fixture Reviewer", "2024-02-01",
    sep = "\t"
  ),
  paste(
    "  Fixture Height  ", "EFO:9999002",
    "should never be reached (first match wins)",
    "2024-01-01", "fixture-chooser", "0.90", "0.10", "auto_accepted", "", "",
    sep = "\t"
  ),
  paste(
    "Fixture hand curated", "EFO:9999003", "Fixture hand-curated ontology label",
    "", "", "", "", "", "", "",
    sep = "\t"
  )
), fixture_path)
canonical_table <- load_canonical_trait_table(fixture_path)

# The resolver loads the provenance columns but reads only the three lookup
# columns by name -- the added columns must not leak into its output.
check(
  all(c("ontology_release", "chooser_id", "confidence", "runner_up_margin",
        "review_status", "reviewer", "reviewed_at") %in% names(canonical_table)),
  "load_canonical_trait_table() keeps the provenance columns"
)

# A row with full provenance resolves as canonical_table_lookup with the
# correct identifier and label.
r <- resolve_trait_ontology_mapping(
  trait_label = "fixture height", source_ontology_id = NA_character_,
  source_ontology_label = NA_character_, canonical_table = canonical_table
)
check(
  r$resolution_status == "resolved" && r$trait_ontology_mapping_method == "canonical_table_lookup" &&
    r$trait_ontology_id == "EFO:9999001" &&
    r$trait_ontology_label == "Fixture height ontology label",
  "canonical-table exact match, case/whitespace-insensitive, first match wins"
)
check(
  identical(
    names(r),
    c("resolution_status", "trait_ontology_id", "trait_ontology_label",
      "trait_ontology_mapping_method", "resolution_notes")
  ),
  "the resolver ignores the added provenance columns in its output shape"
)

# A hand-curated row with every provenance column empty still resolves.
r <- resolve_trait_ontology_mapping(
  trait_label = "fixture hand curated", source_ontology_id = NA_character_,
  source_ontology_label = NA_character_, canonical_table = canonical_table
)
check(
  r$resolution_status == "resolved" && r$trait_ontology_mapping_method == "canonical_table_lookup" &&
    r$trait_ontology_id == "EFO:9999003" &&
    r$trait_ontology_label == "Fixture hand-curated ontology label",
  "a hand-curated row with empty provenance columns still resolves"
)

# A legacy table with no provenance columns at all (the pre-widening shape)
# still loads and resolves -- missing columns are as acceptable as empty ones.
legacy_path <- tempfile(fileext = ".tsv")
writeLines(c(
  "trait_label\ttrait_ontology_id\ttrait_ontology_label",
  "Fixture legacy\tEFO:9999004\tFixture legacy ontology label"
), legacy_path)
legacy_table <- load_canonical_trait_table(legacy_path)
r <- resolve_trait_ontology_mapping(
  trait_label = "fixture legacy", source_ontology_id = NA_character_,
  source_ontology_label = NA_character_, canonical_table = legacy_table
)
check(
  r$trait_ontology_mapping_method == "canonical_table_lookup" &&
    r$trait_ontology_id == "EFO:9999004",
  "a table with missing provenance columns still resolves"
)
unlink(legacy_path)

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

cat(sprintf("ALL %d CHECKS PASSED\n", n_checks))
