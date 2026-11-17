#!/usr/bin/env bash
# Replace the regenerated swap_alleles copies and the rescue manifest in the live derived tree
# (opengwasdb-stores#455). Run only once the owner has approved this command, host, and effect.
#
#   tools/replace_rescued_copies.sh [--dry-run] --scratch DIR --receipt FILE --before FILE [--live DIR]
#
#   --scratch  the regenerated tree: <DIR>/<bucket>/<accession>/harmonised/*.h.tsv.gz and
#              <DIR>/eur-hybrid-rescue-orientation-manifest.tsv (what the rescue wrote with --derived-base)
#   --receipt  tools/verify_rescued_gwas_files.py's successful full-verification receipt.
#              Its file list must cover all replacements in the manifest, without duplicates.
#   --before   where the current live manifest is saved first (refused if it exists and differs)
#   --live     default /data/opengwasdb/derived/ebi-gwas-catalog
#   --dry-run  do every check and print the plan; write nothing
#
# Order, and what a failure leaves:
#   0. checks (no writes): every receipt row passed full verification and covers exactly the
#      manifest's replacements; only unchanged frequency-only and approved excluded rows are
#      exempt. Every receipt file exists in scratch with its sha256 and has a live directory.
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

# A verifier writes a receipt even on failure: a checksum alone is not approval.
# Keep this validation before the backup and before creating any .part file.
python3 - "$receipt" "$smanifest" "$lmanifest" <<'PY'
import csv
import sys


def read_rows(path, required):
    with open(path, newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        header = reader.fieldnames or []
        if not required <= set(header) or len(header) != len(set(header)):
            sys.exit(f"{path}: unexpected TSV header")
        rows = {}
        for row in reader:
            aid = row["analysis_id"]
            if None in row or None in row.values() or not aid or aid in rows:
                sys.exit(f"{path}: malformed or duplicate row for {aid!r}")
            rows[aid] = row
        return rows


receipt = read_rows(sys.argv[1], {
    "analysis_id", "sha256", "mismatching_rows", "header_identical", "row_counts_equal",
})
for aid, row in receipt.items():
    if (row["mismatching_rows"], row["header_identical"], row["row_counts_equal"]) != ("0", "true", "true"):
        sys.exit(f"{aid}: receipt does not record successful full verification")

columns = {"analysis_id", "data_file", "sha256", "status", "error", "yaml_file", "data_bytes", "yaml_bytes"}
scratch = read_rows(sys.argv[2], columns)
live = read_rows(sys.argv[3], columns)
if scratch.keys() != live.keys():
    sys.exit("scratch and live manifest membership differs")

replacements = set()
for aid, row in scratch.items():
    if aid in {"GCST90275159", "GCST90275160"}:
        if row["status"] != "excluded" or any(row[c] for c in ("data_file", "yaml_file", "sha256", "data_bytes", "yaml_bytes")):
            sys.exit(f"{aid}: approved exclusion must have no file, checksum or size")
    elif row["error"] == "rescued via flip_frequency" and row == live[aid]:
        continue
    elif row["status"] == "already_present" and row["error"] == "rescued via swap_alleles" and row["data_file"] and row["sha256"]:
        # Include already-installed rows too, so retrying cannot hide an omission.
        replacements.add(aid)
    else:
        sys.exit(f"{aid}: manifest row is neither a replacement, unchanged frequency-only row nor approved exclusion")
if receipt.keys() != replacements:
    sys.exit(f"receipt membership differs from replacements: missing={sorted(replacements - receipt.keys())}, extra={sorted(receipt.keys() - replacements)}")
PY

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
