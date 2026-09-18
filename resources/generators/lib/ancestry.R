# Single home for the mapping from this registry's free-text Source Ancestry
# Labels to the super-population codes Assigned Ancestry uses
# (docs/release-metadata-schema.md; resources/reference-resources/
# ukb-ancestry-mixture-hg38/resource.yaml). Both the R generators and
# resources/generators/lib/source-formats/gwas-ssf-ragged/ancestry-assign.py
# read the same tracked TSV so the two vocabularies cannot drift apart
# (opengwasdb-stores#133).
#
# Ambiguous source labels (for example "Asian (unspecified)") are deliberately
# absent: normalise_assigned_ancestry() fails loudly rather than pass one
# through, because an unnormalised value is not a valid Assigned Ancestry.

source_label_map_path <- function(root) {
  file.path(
    root, "resources", "reference-resources",
    "ukb-ancestry-mixture-hg38", "source_label_map.tsv"
  )
}

read_source_label_map <- function(root) {
  map <- data.table::fread(
    source_label_map_path(root), sep = "\t", colClasses = "character"
  )
  stats::setNames(map$super_population, map$source_label)
}

# Translate Source Ancestry Labels to super-population codes. A label absent
# from the tracked map is an error, not a silent pass-through.
normalise_assigned_ancestry <- function(label, map) {
  label <- as.character(label)
  out <- unname(map[label])
  missing <- is.na(out) & !is.na(label)
  if (any(missing)) {
    stop(sprintf(
      "no tracked Source Ancestry Label -> super-population mapping for: %s",
      paste(unique(label[missing]), collapse = ", ")
    ))
  }
  out
}

# The store-level `source_defaults.assigned_ancestry` default. A family config
# that names one source ancestry group gets its normalised code; a multi-group
# config leaves the default NULL, exactly as before this mapping existed.
normalise_config_assigned_ancestry <- function(cfg, map) {
  label <- cfg$selection$ancestry_group
  if (is.null(label) || !nzchar(label)) return(label)
  normalise_assigned_ancestry(label, map)
}
