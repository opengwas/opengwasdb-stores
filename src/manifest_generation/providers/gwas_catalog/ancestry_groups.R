# GWAS Catalog ancestry grouping, sourced by ebi-studies.r and by its test
# (tests/gwas-catalog-ancestry-groups). Base R only.

ancestry_map <- c(
  "European" = "European",
  "East Asian" = "East Asian",
  "South Asian" = "South Asian",
  "South East Asian" = "South East Asian",
  "Central Asian" = "Central Asian",
  "African American or Afro-Caribbean" = "African",
  "African unspecified" = "African",
  "Sub-Saharan African" = "African",
  "Hispanic or Latin American" = "Hispanic or Latin American",
  "Greater Middle Eastern" = "Greater Middle Eastern",
  "Native American" = "Native American",
  "Oceanian" = "Oceanian",
  "Aboriginal Australian" = "Oceanian",
  "Asian unspecified" = "Asian (unspecified)",
  "Other" = "Other",
  "Other admixed ancestry" = "Multiple/Mixed",
  "NR" = "NR/Unknown"
)

major_group_for_row <- function(raw) {
  stripped <- gsub("\\([^)]*\\)", "", raw)
  toks <- trimws(unlist(strsplit(stripped, ",")))
  toks <- toks[toks != ""]
  groups <- unique(unname(ancestry_map[toks]))
  groups <- groups[!is.na(groups)]
  ## "NR" (not reported) is not an ancestry: beside a real group it is dropped
  ## (#557); alone it stays "NR/Unknown".
  if (length(groups) > 1) groups <- setdiff(groups, "NR/Unknown")
  if (length(groups) != 1) return("Multiple/Mixed")
  groups
}
