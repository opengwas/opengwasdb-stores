# Pure selection and manifest-application logic for the FinnGen R13 pilot.

suppressPackageStartupMessages(library(data.table))

select_finngen_r13_pilot <- function(manifest, binary_count = 17L) {
  required <- c("phenocode", "phenotype", "category", "num_cases", "num_controls", "path_https")
  missing <- setdiff(required, names(manifest))
  if (length(missing)) stop("FinnGen manifest missing columns: ", paste(missing, collapse = ", "))

  manifest <- copy(manifest)
  manifest[, manifest_order := .I]
  manifest[, num_cases := suppressWarnings(as.double(num_cases))]
  manifest[, num_controls := suppressWarnings(as.double(num_controls))]

  quantitative <- manifest[
    category == "Quantitative endpoints" & !is.na(num_cases) & num_cases > 0 & num_controls == 0
  ][order(phenocode)]
  if (nrow(quantitative) != 3L) {
    stop("FinnGen R13 pilot requires exactly three resolved quantitative endpoints; found ", nrow(quantitative))
  }

  eligible_binary <- manifest[
    category != "Quantitative endpoints" & !is.na(num_cases) & num_cases > 0 &
      !is.na(num_controls) & num_controls > 0
  ]
  winners <- eligible_binary[
    order(category, -num_cases, phenocode), .SD[1L], by = category
  ][order(-num_cases, category, phenocode)]
  if (nrow(winners) < binary_count) {
    stop("FinnGen R13 pilot requires ", binary_count, " resolved binary categories; found ", nrow(winners))
  }
  selected_binary <- winners[seq_len(binary_count)]
  rbindlist(list(quantitative, selected_binary), use.names = TRUE, fill = TRUE)[, manifest_order := NULL][]
}

finngen_release_rows <- function(selected, artifact_source_dir, defaults) {
  rows <- lapply(seq_len(nrow(selected)), function(i) {
    source <- as.list(selected[i])
    resolved <- resolve_finngen_manifest_metadata(source)
    if (!identical(resolved$resolution_status[[1]], "resolved")) {
      stop("Selected FinnGen endpoint could not be resolved: ", source$phenocode, ": ", resolved$resolution_notes[[1]])
    }
    scale <- resolved$stored_effect_scale[[1]]
    is_binary <- identical(scale, "log_or")
    data.table(
      analysis_id = paste0("finngen-r13-", source$phenocode),
      source_analysis_id = source$phenocode,
      source_label = source$phenotype,
      analysis_label = source$phenotype,
      trait_ontology_label = NA_character_,
      trait_ontology_id = NA_character_,
      trait_ontology_mapping_method = "unmapped",
      source_file = file.path(artifact_source_dir, paste0("finngen_R13_", source$phenocode, ".gz")),
      source_url = source$path_https,
      source_category = source$category,
      source_reader_capability = "opengwasdb.finngen-r13",
      source_bundle_id = NA_character_,
      checksum = NA_character_,
      checksum_algorithm = "sha256",
      size_bytes = NA_real_,
      source_genome_build = defaults$source_genome_build,
      license = defaults$license,
      publication_doi = NA_character_,
      publication_pmid = NA_character_,
      consortium = "FinnGen",
      first_author = NA_character_,
      source_ancestry_label = "Finnish",
      assigned_ancestry = NA_character_,
      ancestry_assignment_method = "unassigned",
      original_effect_scale = scale,
      original_sd = NA_real_,
      original_sd_method = if (is_binary) "binary_trait" else "declared_standardised",
      stored_effect_scale = scale,
      sample_size_kind = resolved$sample_size_kind[[1]],
      sample_size_scope = "analysis_level",
      sample_size = resolved$sample_size[[1]],
      n_cases = resolved$n_cases[[1]],
      n_controls = resolved$n_controls[[1]],
      analysis_group_id = source$category,
      inclusion_reason = if (is_binary) {
        "largest-case endpoint in its category; category retained among the 17 largest winners"
      } else {
        "all FinnGen R13 inverse-rank-normalised quantitative endpoints"
      },
      exclude_from_build = NA_character_
    )
  })
  rbindlist(rows, use.names = TRUE, fill = TRUE)
}

finngen_derivations <- function(analyses) {
  rbindlist(lapply(seq_len(nrow(analyses)), function(i) {
    row <- analyses[i]
    data.table(
      analysis_id = row$analysis_id,
      field = c("stored_effect_scale", "sample_size_kind"),
      value = c(row$stored_effect_scale, row$sample_size_kind),
      method = "finngen_public_endpoint_manifest",
      evidence = c(
        paste0("category=", row$source_category),
        paste0("sample_size=", row$sample_size, ";n_cases=", row$n_cases %||% "", ";n_controls=", row$n_controls %||% "")
      ),
      notes = "resolved from pinned FinnGen endpoint manifest"
    )
  }))
}

`%||%` <- function(x, y) {
  if (is.null(x) || !length(x)) return(y)
  if (is.atomic(x) && length(x) == 1L && is.na(x)) y else x
}
