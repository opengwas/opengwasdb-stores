#!/usr/bin/env bash
# Phase A build of the full FinnGen R13 Release Bundle (OGS-00016, r13-full).
#
# `pixi run release OGS-00016` plans the recipe and runs it:
#
#   build_manifest -> build-dense-vcf -> top-hits -> overview -> validate -> register
#
# The recipe reads all 2,754 endpoint files under
# /data/opengwasdb/finngen-r13/releases/r13-full/source and writes
# /data/opengwasdb/stores/OGS-00016/store.opengwasdb. This is a multi-day,
# ~0.5 TB job: the pilot measured 20 Analyses / 21.2M variants in ~1.3 h at 10
# workers, and OpenGWASDB's union-axis Pass 1 is intentionally serial, so wall
# time does not scale only with worker count. Run it detached and resumable.
#
# WARNING recorded by the pilot assessment (OGS-00003, families/.../r13-pilot-20):
# the full R13 collection received a NO-GO because effect-scale/sd_estimation
# failed for finngen-r13-HEIGHT_IRN. The build itself is unchanged by that
# finding; the Store is still built and validated. Re-run the assessment after
# the build if the discrepancy has not been resolved.
set -uo pipefail

REPO=/home/gh13047/repo/opengwasdb-stores
STORE_ID=OGS-00016
ARTIFACT_ROOT=/data/opengwasdb/stores
SOURCE_DIR=/data/opengwasdb/finngen-r13/releases/r13-full/source
LOG="$ARTIFACT_ROOT/$STORE_ID/build.log"

cd "$REPO" || exit 1
mkdir -p "$ARTIFACT_ROOT/$STORE_ID"

exec > >(tee -a "$LOG") 2>&1
echo "=== $STORE_ID Phase A build ==="
echo "started: $(date -Is)"

status_line=$(grep -m1 '^status:' "stores/$STORE_ID/release.yaml" || true)
echo "bundle: $status_line"
case "$status_line" in
  *accepted*|*built*|*validated*) ;;
  *) echo "ABORT: bundle is not accepted; refusing to build a candidate." >&2; exit 1 ;;
esac

n_sources=$(find "$SOURCE_DIR" -name '*.gz' 2>/dev/null | wc -l)
echo "source files: $n_sources"
if [ "$n_sources" -lt 2754 ]; then
  echo "ABORT: expected 2754 acquired source files, found $n_sources." >&2
  exit 1
fi

echo "=== pixi run release $STORE_ID : $(date -Is) ==="
pixi run release "$STORE_ID"
rc=$?
echo "=== exit ${rc} : $(date -Is) ==="
exit "$rc"
