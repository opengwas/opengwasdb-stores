#!/usr/bin/env bash
# Replace the regenerated swap_alleles copies and the rescue manifest in the live derived tree
# (opengwasdb-stores#455). Run only once the owner has approved this command, host, and effect.
#
#   tools/replace_rescued_copies.sh [--dry-run] --scratch DIR --receipt FILE --before FILE [--live DIR]
#
#   --scratch  the regenerated tree: <DIR>/<bucket>/<accession>/harmonised/*.h.tsv.gz and
#              <DIR>/eur-hybrid-rescue-orientation-manifest.tsv (what the rescue wrote with --derived-base)
#   --receipt  tools/verify_rescued_gwas_files.py's receipt: the file list (analysis_id) and each
#              regenerated file's sha256. The list comes from here, never from a directory listing.
#   --before   where the current live manifest is saved first (refused if it exists and differs)
#   --live     default /data/opengwasdb/derived/ebi-gwas-catalog
#   --dry-run  do every check and print the plan; write nothing
#
# Order, and what a failure leaves:
#   0. checks (no writes): every receipt file exists in scratch with the receipt's sha256, its live
#      directory exists, the scratch manifest names the same files and checksums.
#      Failure: nothing changed.
#   1. save the live manifest to --before.   Failure: nothing changed (a stray --before copy only).
#   2. copy every file to <target>.part beside its target and verify each .part's sha256.
#      Failure: the live files and manifest are unchanged; the .part files are removed by this script
#      (if it was killed, delete *.h.tsv.gz.part under --live). Re-run.
#   3. rename every .part over its target (atomic per file).
#      Failure part-way: SOME copies are new and the manifest is still OLD (it names old checksums).
#      Recover by re-running: steps 0-3 are idempotent and finish the rest. Do not run anything that
#      rebuilds OGS-00011 until it completes.
#   4. write the manifest last (.part, verify, rename).
#      Failure: all copies are new, the manifest is old: re-run, or copy --before back only if the
#      copies are to be reverted (they are not kept: regenerate from scratch with the rescue).
set -euo pipefail

dry=0 scratch="" receipt="" before="" live=/data/opengwasdb/derived/ebi-gwas-catalog
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) dry=1 ;;
    --scratch) scratch=$2; shift ;;
    --receipt) receipt=$2; shift ;;
    --before) before=$2; shift ;;
    --live) live=$2; shift ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$scratch" ] && [ -n "$receipt" ] && [ -n "$before" ] || { echo "need --scratch, --receipt and --before" >&2; exit 2; }
man=eur-hybrid-rescue-orientation-manifest.tsv
smanifest=$scratch/$man lmanifest=$live/$man
[ -f "$receipt" ] && [ -f "$smanifest" ] && [ -f "$lmanifest" ] || { echo "receipt, scratch manifest or live manifest missing" >&2; exit 1; }

staged=()
cleanup() { local p; for p in "${staged[@]:-}"; do if [ -n "$p" ]; then rm -f -- "$p"; fi; done; return 0; }
trap cleanup EXIT

# column index of a header name in a TSV's first line
col() { awk -F'\t' -v n="$2" 'NR==1{sub(/\r$/,"");for(i=1;i<=NF;i++)if($i==n){print i;exit}}' "$1"; }
rid=$(col "$receipt" analysis_id); rsha=$(col "$receipt" sha256)
mid=$(col "$smanifest" analysis_id); mdata=$(col "$smanifest" data_file); msha=$(col "$smanifest" sha256)
[ -n "$rid$rsha$mid$mdata$msha" ] && [ -n "$rid" ] && [ -n "$rsha" ] && [ -n "$mid" ] && [ -n "$mdata" ] && [ -n "$msha" ] || { echo "unexpected TSV header" >&2; exit 1; }

ids=() shas=() srcs=() dsts=()
while IFS=$'\t' read -r id sha; do
  row=$(awk -F'\t' -v id="$id" -v a="$mid" -v b="$mdata" -v c="$msha" 'NR>1&&$a==id{print $b"\t"$c}' "$smanifest")
  [ "$(printf '%s\n' "$row" | grep -c .)" = 1 ] || { echo "$id: not exactly one scratch manifest row" >&2; exit 1; }
  dst=${row%%$'\t'*}; msum=${row##*$'\t'}
  [ "$msum" = "$sha" ] || { echo "$id: scratch manifest sha256 differs from the receipt" >&2; exit 1; }
  case "$dst" in "$live"/*) ;; *) echo "$id: $dst is not under $live" >&2; exit 1 ;; esac
  src=$scratch/${dst#"$live"/}
  [ -f "$src" ] || { echo "$id: $src missing" >&2; exit 1; }
  [ -d "$(dirname "$dst")" ] || { echo "$id: live directory $(dirname "$dst") missing" >&2; exit 1; }
  [ "$(sha256sum "$src" | cut -d' ' -f1)" = "$sha" ] || { echo "$id: $src does not match the receipt's sha256" >&2; exit 1; }
  ids+=("$id") shas+=("$sha") srcs+=("$src") dsts+=("$dst")
done < <(awk -F'\t' -v a="$rid" -v b="$rsha" 'NR>1{print $a"\t"$b}' "$receipt")
n=${#ids[@]}; [ "$n" -gt 0 ] || { echo "receipt lists no file" >&2; exit 1; }
echo "checked: $n files match the receipt"
if [ "$dry" = 1 ]; then printf 'would replace %s\n' "${dsts[@]}"; echo "would replace $lmanifest (saved to $before first)"; exit 0; fi

# 1. save the live manifest
if [ -e "$before" ]; then
  cmp -s -- "$before" "$lmanifest" || { echo "$before exists and differs from the live manifest; not overwriting" >&2; exit 1; }
else
  cp -- "$lmanifest" "$before"
fi

# 2. stage and verify every .part
for i in "${!ids[@]}"; do
  part=${dsts[$i]}.part
  staged+=("$part")
  cp -- "${srcs[$i]}" "$part"
  [ "$(sha256sum "$part" | cut -d' ' -f1)" = "${shas[$i]}" ] || { echo "${ids[$i]}: staged copy fails its checksum" >&2; exit 1; }
done
echo "staged and verified $n files"

# 3. rename all
for i in "${!ids[@]}"; do mv -- "${dsts[$i]}.part" "${dsts[$i]}"; done
staged=()
echo "replaced $n files"

# 4. manifest last
staged=("$lmanifest.part")
cp -- "$smanifest" "$lmanifest.part"
[ "$(sha256sum "$lmanifest.part" | cut -d' ' -f1)" = "$(sha256sum "$smanifest" | cut -d' ' -f1)" ] || { echo "manifest copy fails its checksum" >&2; exit 1; }
mv -- "$lmanifest.part" "$lmanifest"
staged=()
echo "replaced $lmanifest"
