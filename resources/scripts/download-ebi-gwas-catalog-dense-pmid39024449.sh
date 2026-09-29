#!/usr/bin/env bash
# Acquisition for the four dense EBI GWAS Catalog GWAS-SSF stores split out of
# PMID 39024449 (Verma et al. 2024, "Diversity and scale", VA Million Veteran
# Program), planned as OGS-00012..OGS-00015.
#
# This is the production-scale sibling of the `hybrid__European` pilot
# acquisition (OGS-00004/OGS-00005): the same GWAS-SSF source files from the
# same EBI mirror, but selected per ancestry from the dense store keys the
# candidate table already partitions this publication into:
#
#   dense__pmid-39024449__European                    3,360 Analyses
#   dense__pmid-39024449__African                     1,368 Analyses
#   dense__pmid-39024449__Hispanic-or-Latin-American  1,053 Analyses
#   dense__pmid-39024449__East-Asian                    241 Analyses
#
# Two passes per store key:
#
#   1. harmonised -- the shared eur-hybrid downloader. EBI lists many of this
#      study's accessions in `harmonised_list.txt` but publishes only the raw
#      file, so this pass records them as missing and moves on.
#   2. raw fallback -- downloads the raw `<GCST>.tsv.gz` pair for every row the
#      harmonised pass did not obtain, so the study is *fully* on disk and a
#      later harmonisation step can migrate the raw files. It skips any row the
#      harmonised pass already fetched, so nothing is downloaded twice.
#
# EBI rate-limits aggressively: bursts are answered with connection resets
# (curl exit 7) and transient 404s. Both passes therefore use a low worker
# count and high curl retries, and the harmonised pass is re-run a bounded
# number of times so an accession that 404'd under load is re-attempted rather
# than frozen into the manifest as absent.
set -uo pipefail

REPO=/home/gh13047/repo/opengwasdb-stores
MIRROR=/data/opengwasdb/raw/ebi-gwas-catalog
INDEX="$MIRROR/harmonised_list-2026-09-28.txt"
LOG="$MIRROR/dense-pmid39024449-download.log"
WORKERS="${WORKERS:-3}"
ATTEMPTS="${ATTEMPTS:-2}"

cd "$REPO" || exit 1
if [ ! -f "$INDEX" ]; then
  echo "ABORT: harmonised index not found: $INDEX" >&2
  exit 1
fi

exec > >(tee -a "$LOG") 2>&1
echo "=== dense PMID 39024449 acquisition ==="
echo "started: $(date -Is); mirror=$MIRROR workers=$WORKERS attempts=$ATTEMPTS"

status=0
for slug in European African Hispanic-or-Latin-American East-Asian; do
  key="dense__pmid-39024449__${slug}"
  manifest="$MIRROR/dense-pmid39024449-${slug}-download-manifest.tsv"
  raw_manifest="$MIRROR/dense-pmid39024449-${slug}-raw-download-manifest.tsv"

  attempt=0
  harmonised_ok=0
  while :; do
    attempt=$((attempt + 1))
    echo "=== ${key} harmonised attempt ${attempt}/${ATTEMPTS} : $(date -Is) ==="
    pixi run python resources/scripts/download-ebi-gwas-catalog-eur-hybrid.py \
      --store-key "$key" \
      --dest "$MIRROR" \
      --harmonised-index "$INDEX" \
      --workers "$WORKERS" \
      --retries 12 \
      --manifest "$manifest"
    rc=$?
    if [ "$rc" -eq 0 ]; then
      harmonised_ok=1
      echo "=== ${key} harmonised complete : $(date -Is) ==="
      break
    fi
    if [ "$attempt" -ge "$ATTEMPTS" ]; then
      # A non-zero harmonised exit is expected while raw-only accessions exist;
      # the raw pass below is what resolves them.
      echo "=== ${key} harmonised finished with unresolved rows (rc=$rc); raw fallback will cover them : $(date -Is) ==="
      break
    fi
    echo "=== ${key} harmonised retrying after rc=$rc : $(date -Is) ==="
    sleep 60
  done

  echo "=== ${key} raw fallback : $(date -Is) ==="
  pixi run python resources/scripts/download-ebi-gwas-catalog-raw.py \
    --store-key "$key" \
    --dest "$MIRROR" \
    --harmonised-manifest "$manifest" \
    --workers "$WORKERS" \
    --retries 12 \
    --manifest "$raw_manifest"
  raw_rc=$?
  if [ "$raw_rc" -ne 0 ] || [ "$harmonised_ok" -ne 1 ]; then
    echo "=== ${key} finished with unresolved rows (harmonised_ok=$harmonised_ok raw_rc=$raw_rc) : $(date -Is) ==="
    status=1
  else
    echo "=== ${key} complete : $(date -Is) ==="
  fi
done

echo "=== dense PMID 39024449 acquisition finished: $(date -Is) status=$status ==="
exit "$status"
