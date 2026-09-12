# Pure (network-free) logic for the `opengwas-gwas-vcf-dense` Manifest
# Generator engine (issue #50): candidate selection and applying a metadata
# resolver's per-Analysis resolution onto a candidate manifest row. Kept
# separate from generators/lib/source-formats/opengwas-gwas-vcf-dense/generate.R
# (arg-parsing, file I/O, the live OpenGWAS API call) so this logic is
# independently fixture-testable, matching this repository's convention of
# keeping generator CLI drivers thin over a sourceable generators/lib/*.R
# module (see generators/lib/effect_scale_validation.R).

suppressPackageStartupMessages(library(data.table))
source("generators/lib/metadata_resolvers/canonical_trait_table.R")

select_analyses <- function(candidates, max_analyses = NA_integer_, only_analysis_id = "") {
  selected <- candidates
  if (nzchar(only_analysis_id)) {
    ids <- strsplit(only_analysis_id, ",", fixed = TRUE)[[1]]
    selected <- selected[analysis_id %in% ids]
  }
  if (!is.na(max_analyses)) selected <- selected[seq_len(min(max_analyses, .N))]
  if (!nrow(selected)) stop("Selection produced zero analyses")
  selected[]
}

# Applies each Analysis's resolution (one row per analysis_id, in the
# resolve_opengwas_api_metadata()-shaped contract: resolution_status,
# resolution_notes, stored_effect_scale, sample_size_kind, sample_size,
# n_cases, n_controls) onto the candidate row: overwrites
# stored_effect_scale/original_effect_scale/sample_size_kind/sample_size/
# n_cases/n_controls with the resolver's determination (authoritative,
# replacing the candidate manifest's blank/family-default values -- see
# issue #50), derives original_sd_method from the resolved scale, and
# excludes an unresolved Analysis from the build (issue #49 acceptance
# criterion: "not a defaulted 0 ... excluded from build or carries
# exclude_from_build") rather than leaving it silently blank. An
# analysis_id present in `candidates` but absent from `resolution`
# (resolve_batch() never got a gwasinfo record back for it at all) is
# treated identically to an explicit `unresolved` row.
apply_resolution <- function(candidates, resolution) {
  resolved_cols <- c(
    resolution_status = "resolved_status", resolution_notes = "resolved_notes",
    stored_effect_scale = "resolved_scale", sample_size_kind = "resolved_kind",
    sample_size = "resolved_n", n_cases = "resolved_ncase", n_controls = "resolved_ncontrol"
  )
  resolution <- copy(resolution)
  setnames(resolution, names(resolved_cols), unname(resolved_cols))

  # An analysis_id absent from `resolution` entirely (resolve_batch() got no
  # gwasinfo record back for it at all) already has every resolved_* column
  # at NA here, for free, via the outer join's unmatched-row behaviour --
  # matching generators/lib/metadata_resolvers/contract.R's
  # unresolved_metadata_record() shape without reconstructing it field by
  # field; only resolved_status/resolved_notes need patching in explicitly.
  merged <- merge(candidates, resolution, by = "analysis_id", all.x = TRUE, sort = FALSE)
  merged[is.na(resolved_status), `:=`(
    resolved_status = "unresolved",
    resolved_notes = "no gwasinfo record returned by the OpenGWAS API"
  )]

  # original_effect_scale mirrors stored_effect_scale: the OpenGWAS API's
  # `unit` field is the only upstream effect-scale signal this resolver has
  # (there is no richer "as originally reported" vocabulary to preserve
  # separately, unlike e.g. a source file's own declared units), and
  # OpenGWASDB stores the VCF's beta column unchanged, with no rescaling
  # step in between.
  merged[, stored_effect_scale := resolved_scale]
  merged[, original_effect_scale := resolved_scale]
  merged[, sample_size_kind := resolved_kind]
  merged[, sample_size := resolved_n]
  merged[, n_cases := resolved_ncase]
  merged[, n_controls := resolved_ncontrol]
  merged[, original_sd_method := fifelse(
    stored_effect_scale %in% c("log_or", "log_hazard"), "binary_trait",
    fifelse(stored_effect_scale == "sd", "declared_standardised", "unavailable")
  )]
  merged[, exclude_from_build := fifelse(resolved_status == "unresolved", "true", exclude_from_build)]
  merged[, inclusion_reason := fifelse(
    resolved_status == "unresolved",
    trimws(paste0(inclusion_reason, "; excluded_reason: ", resolved_notes)),
    inclusion_reason
  )]

  # One row per explained field (see docs/release-metadata-schema.md,
  # "General derivation sidecar": "field: Manifest field being explained"),
  # not one row per Analysis, so a reviewer scanning by `field` can find
  # every stored_effect_scale (or sample_size_kind) decision without
  # parsing a packed multi-field string.
  scale_rows <- merged[, .(
    analysis_id, field = "stored_effect_scale",
    value = fifelse(resolved_status == "resolved", resolved_scale, NA_character_),
    method = "opengwas_api_gwasinfo",
    evidence = NA_character_,
    notes = fifelse(resolved_status == "unresolved", resolved_notes, NA_character_)
  )]
  sample_size_rows <- merged[, .(
    analysis_id, field = "sample_size_kind",
    value = fifelse(resolved_status == "resolved", resolved_kind, NA_character_),
    method = "opengwas_api_gwasinfo",
    evidence = fifelse(resolved_status == "resolved",
      sprintf("n_cases=%s;n_controls=%s",
              fifelse(is.na(resolved_ncase), "", as.character(resolved_ncase)),
              fifelse(is.na(resolved_ncontrol), "", as.character(resolved_ncontrol))),
      NA_character_),
    notes = fifelse(resolved_status == "unresolved", resolved_notes, NA_character_)
  )]
  derivations <- rbindlist(list(scale_rows, sample_size_rows))
  setorder(derivations, analysis_id, field)

  analyses <- merged[, !c("resolved_status", "resolved_notes", "resolved_scale", "resolved_kind",
                           "resolved_n", "resolved_ncase", "resolved_ncontrol"), with = FALSE]
  list(analyses = analyses, derivations = derivations)
}

# Applies Trait Ontology Mapping (CONTEXT.md) onto a candidate/analyses table
# already carrying source_label/trait_ontology_id/trait_ontology_label
# columns. Kept separate from apply_resolution() above since it resolves an
# unrelated field group -- see
# generators/lib/metadata_resolvers/canonical_trait_table.R's
# resolve_trait_ontology_mapping() contract: source_provided when the Source
# Collection already supplies an ontology ID, canonical_table_lookup when a
# Canonical Trait Mapping Table match exists, otherwise explicit unmapped
# (opengwas-gwas-vcf-dense supplies no ontology mapping of its own today, so
# every row currently resolves to one of the latter two).
apply_trait_ontology_mapping <- function(analyses, canonical_table = NULL) {
  resolved <- resolve_trait_ontology_mappings(
    trait_labels = analyses$source_label,
    source_ontology_ids = analyses$trait_ontology_id,
    source_ontology_labels = analyses$trait_ontology_label,
    canonical_table = canonical_table
  )
  analyses <- copy(analyses)
  analyses[, trait_ontology_id := resolved$trait_ontology_id]
  analyses[, trait_ontology_label := resolved$trait_ontology_label]
  analyses[, trait_ontology_mapping_method := resolved$trait_ontology_mapping_method]
  analyses
}
