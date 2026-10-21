#!/usr/bin/env bash
# Acquisition for the full FinnGen R13 Source Collection (#288).
#
# Downloads every endpoint in the pinned public endpoint manifest into the
# release's source directory and writes the Acquisition Manifest the shared
# freeze reads (docs/spec/source-connectors.md). Files already present -- the
# endpoints the r13-pilot-20 Trial Store Release staged, and anything an
# interrupted run completed -- are checksummed and kept, not re-fetched.
#
# The acquirer exits non-zero if any endpoint has no usable file, but it always
# writes the manifest first, so the absences are recorded evidence rather than a
# lost run. Because every completed file is reused by path, the loop below
# retries only what is missing.
set -uo pipefail

# The checkout this script lives in: src/manifest_generation/providers/<provider>/.
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)
RELEASE=/data/opengwasdb/raw/finngen-r13/releases/r13-full
MANIFEST="${MANIFEST:-$RELEASE/manifest/finngen_R13_manifest.tsv}"
SOURCE_DIR="${SOURCE_DIR:-$RELEASE/source}"
# The connector file the release config names; read-only once committed.
OUT="${OUT:-/data/opengwasdb/raw/finngen-r13/connectors/2026-10-21/finngen-r13-full.acquisition-manifest.tsv}"
LOG="${LOG:-/data/opengwasdb/raw/finngen-r13/releases/r13-full/acquire.log}"
WORKERS="${WORKERS:-8}"
ATTEMPTS="${ATTEMPTS:-10}"

cd "$REPO" || exit 1

exec > >(tee -a "$LOG") 2>&1
echo "=== FinnGen R13 full acquisition ==="
echo "started: $(date -Is); workers=$WORKERS attempts=$ATTEMPTS"
echo "manifest: $MANIFEST"
echo "source:   $SOURCE_DIR"
echo "out:      $OUT"

status=0
for attempt in $(seq 1 "$ATTEMPTS"); do
  echo "=== attempt ${attempt}/${ATTEMPTS} : $(date -Is) ==="
  pixi run -e dev python3 -m manifest_generation.providers.finngen.acquire \
    --manifest="$MANIFEST" \
    --source-dir="$SOURCE_DIR" \
    --out="$OUT" \
    --workers="$WORKERS"
  rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "=== FinnGen R13 full acquisition complete : $(date -Is) ==="
    exit 0
  fi
  status=1
  echo "=== attempt ${attempt} failed (rc=$rc); retrying : $(date -Is) ==="
  sleep 60
done

echo "=== FinnGen R13 full acquisition FAILED after ${ATTEMPTS} attempts : $(date -Is) ==="
exit "$status"
