#!/usr/bin/env bash
# Acquisition for the full FinnGen R13 release (OGS-00017), the
# production-scale sibling of the 20-Analysis r13-pilot-20 trial (OGS-00003).
#
# `generate.R --config=config-full.yaml --mode=emit` freezes all 2,754 endpoints
# of the pinned public manifest into the generator's work root,
# `<artifact-root>/OGS-00017/generate/analyses.tsv` (#248); this script downloads
# every endpoint's summary-statistics `.gz` into the release's artifact source
# directory using the pilot's resumable acquirer. Files already present (for
# example the 19 endpoints already staged under /data/opengwasdb/raw) are
# checksummed and kept, not re-fetched.
#
# The acquirer exits non-zero if any artifact is missing or failed, so the loop
# below re-runs it; because every completed file is cached by path and checksum,
# a retry resumes rather than restarting.
set -uo pipefail

REPO=/home/gh13047/repo/opengwasdb-stores
LOG=/data/opengwasdb/finngen-r13/releases/r13-full/acquire.log
WORKERS="${WORKERS:-8}"
ATTEMPTS="${ATTEMPTS:-10}"

cd "$REPO" || exit 1
RELEASE_DIR="$(python3 -m manifest_generation.emit work-root OGS-00017)" || exit 1

exec > >(tee -a "$LOG") 2>&1
echo "=== FinnGen R13 full acquisition ==="
echo "started: $(date -Is); release=$RELEASE_DIR workers=$WORKERS attempts=$ATTEMPTS"

if [ ! -f "$RELEASE_DIR/analyses.tsv" ]; then
  echo "ABORT: release manifest missing: $RELEASE_DIR/analyses.tsv" >&2
  exit 1
fi

status=0
for attempt in $(seq 1 "$ATTEMPTS"); do
  echo "=== attempt ${attempt}/${ATTEMPTS} : $(date -Is) ==="
  pixi run python3 -m manifest_generation.providers.finngen.acquire \
    --release-dir="$RELEASE_DIR" \
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
