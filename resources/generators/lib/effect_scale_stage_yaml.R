## Shared build.yaml/release.yaml bookkeeping for generators that run the
## reference-AF effect-scale validation stage (resources/generators/lib/effect_scale_validation.R),
## used by gwas-ssf-ragged and gwas-ssf-hybrid. Depends on write_yaml_file()
## and %||% already being defined by the sourcing script (every generator
## defines these itself, matching this repository's existing convention for
## small generic helpers -- see resources/generators/lib/gwas_catalog_ssf_url.R's header
## comment for the same reasoning applied to a domain-specific helper).

build_reference_resources_yaml <- function(cfg) {
  declared <- cfg$reference_resources %||% list()
  lapply(declared, function(res) {
    list(
      resource_id = res$resource_id,
      kind = res$kind %||% "reference_af",
      ancestry = res$ancestry,
      genome_build = res$genome_build,
      variant_id_convention = res$variant_id_convention,
      allele_columns = res$allele_columns,
      location = res$root,
      location_kind = res$location_kind %||% "external_directory",
      fine_group_map = res$fine_group_map
    )
  })
}

build_effect_scale_validation_yaml <- function(cfg) {
  esv <- cfg$effect_scale_validation
  if (is.null(esv)) return(NULL)
  list(
    enabled = esv$enabled %||% TRUE,
    reference_resources = esv$reference_resources %||% list(),
    thresholds = list(
      maf_min = esv$maf_min %||% 0.01,
      maf_max = esv$maf_max %||% 0.5,
      min_overlap_variants = esv$min_overlap_variants %||% 20L,
      sd_tolerance = esv$sd_tolerance %||% 0.15,
      warning_multiplier = esv$warning_multiplier %||% 2.0,
      dispersion_max = esv$dispersion_max %||% 0.5
    )
  )
}

set_sd_estimation_sidecar_pointer <- function(release_dir) {
  path <- file.path(release_dir, "release.yaml")
  if (!file.exists(path)) return(invisible())
  current <- read_yaml(path)
  if (is.null(current$sidecars)) current$sidecars <- list()
  if (is.null(current$sidecars$sd_estimation)) {
    current$sidecars$sd_estimation <- "sidecars/sd_estimation.tsv"
    write_yaml_file(current, path)
  }
}
