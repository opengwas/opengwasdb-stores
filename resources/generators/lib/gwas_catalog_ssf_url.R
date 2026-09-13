## Shared GWAS Catalog harmonised GWAS-SSF URL construction, used by every
## generator that downloads directly from a GCST accession (gwas-ssf-ragged,
## gwas-ssf-hybrid): GWAS Catalog buckets harmonised files into fixed
## 1000-accession directories, e.g. GCST90000001-GCST90001000/GCST90000059/.

bucket_of <- function(gcst) {
  accession_digits <- sub("^GCST", "", gcst)
  n <- as.numeric(accession_digits)
  lo <- floor((n - 1) / 1000) * 1000 + 1
  width <- nchar(accession_digits)
  sprintf(
    "GCST%s-GCST%s",
    sprintf(paste0("%0", width, "d"), lo),
    sprintf(paste0("%0", width, "d"), lo + 999)
  )
}

ssf_url <- function(gcst, ftp_base) {
  sprintf("%s/%s/%s/harmonised/%s.h.tsv.gz", ftp_base, bucket_of(gcst), gcst, gcst)
}
