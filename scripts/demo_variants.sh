#!/usr/bin/env bash
# Runs the variant checks against saved local copies (fixtures/), all into the same db so history builds up:
#   1. first run   2. unchanged re-run   3. changed source   4. failed file
set -euo pipefail
cd "$(dirname "$0")/.."

PAGE=https://createto.ca/projects/st-lawrence-centre-for-the-arts
OUT=${OUT:-examples/variants}
rm -rf "$OUT"

step() {
  echo; echo "=== $1 ==="
  uv run python -m createto run --data-dir "$OUT" --seed "$PAGE" --replay "fixtures/$2" --since 2021-01-01
}

step "1. first run" base
step "2. unchanged re-run" base
step "3. changed source (phase edited, PDF amended)" changed_source
step "4. failed file (corrupt PDF + HTTP 500)" failed_file

# keep the reports + exports, drop the raw blobs/db to keep the repo small
[ "${KEEP_DB:-}" ] || rm -rf "$OUT/blobs" "$OUT"/createto.db*
