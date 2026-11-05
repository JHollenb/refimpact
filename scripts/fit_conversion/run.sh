#!/usr/bin/env bash
# usage: run.sh <name>   Beast job: fits first-down and this-drive tables on 2024-2026.
set -eu
NAME=$1
ROOT=/mnt/usb-storage/ref-impact-fit
export OMP_NUM_THREADS=4 OPENBLAS_NUM_THREADS=4 POLARS_MAX_THREADS=4 TMPDIR=/mnt/usb-storage/tmp
mkdir -p "$TMPDIR"
echo "=== $NAME $(date -u +%FT%TZ) $(hostname)"
exec "$ROOT/venv/bin/python" -u worker.py --pbp "$ROOT/pbp" --out "$ROOT/out/$NAME"
