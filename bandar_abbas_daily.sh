#!/usr/bin/env bash
# bandar_abbas_daily.sh — Daily Sentinel-1 imagery for Port of Bandar Abbas.
#
# For each of the last N days (default 7), downloads the first scene whose
# footprint fully covers the Bandar Abbas port bbox, then renders a
# false-color crop of that geographic area using visualisation.py.
#
# Bandar Abbas bbox (Shahid Rajaee + old port + anchorage):
#   W=56.05  S=27.10  E=56.29  N=27.25
#
# Usage:
#   bash bandar_abbas_daily.sh
#   bash bandar_abbas_daily.sh --days 14
#   bash bandar_abbas_daily.sh --with-safe   # also download full SAFE (~1 GB/scene)
#
# Credentials needed in .env:
#   CDSE_USER, CDSE_PASSWORD       — Copernicus Data Space (required for SAFE)
#   SH_CLIENT_ID, SH_CLIENT_SECRET — Sentinel Hub (required for previews)
#
# Outputs land next to source files (data/previews/ or data/safe/):
#   <scene>_falsecolor.jpg

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
BBOX_W=56.05
BBOX_S=27.10
BBOX_E=56.29
BBOX_N=27.25
DAYS=7
WITH_SAFE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --days)      DAYS="$2"; shift 2 ;;
    --with-safe) WITH_SAFE=true;  shift ;;
    *)           echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

# --------------------------------------------------------------------------- #
# Bootstrap
# --------------------------------------------------------------------------- #
if [[ -f .env ]]; then
  set -a
  # shellcheck source=.env
  source .env
  set +a
fi

if ! command -v python3 &>/dev/null; then
  echo "ERROR: python3 not found. Install Python 3 to use this script." >&2
  exit 1
fi

echo "=== Bandar Abbas Daily SAR — $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
echo "    bbox  : W=${BBOX_W}  S=${BBOX_S}  E=${BBOX_E}  N=${BBOX_N}"
echo "    days  : ${DAYS}"
echo "    SAFE  : ${WITH_SAFE}"
echo

# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #
echo "--- Downloading scenes (last ${DAYS} days, full-coverage filter) ---"

SAFE_FLAG=""
[[ "$WITH_SAFE" == "false" ]] && SAFE_FLAG="--no-safe"

if python3 download_sar.py \
    --days  "${DAYS}" \
    --bbox  "${BBOX_W}" "${BBOX_S}" "${BBOX_E}" "${BBOX_N}" \
    --full-coverage \
    ${SAFE_FLAG}; then
  echo "Download step complete."
else
  echo "WARNING: download_sar.py exited with an error."
  echo "  → Check that CDSE_USER / CDSE_PASSWORD / SH_CLIENT_ID / SH_CLIENT_SECRET"
  echo "    are set in .env, then re-run."
fi
echo

# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #
echo "--- Discovering scenes in data/ ---"

# parse the listing: lines starting with an 8-hex-char ID
SCENE_IDS=$(python3 visualisation.py --list 2>/dev/null \
  | awk '/^[0-9a-f]{8}[[:space:]]/ { print $1 }')

if [[ -z "$SCENE_IDS" ]]; then
  echo "No scenes found in data/ — nothing to render."
  echo "(Download may have failed, or no Sentinel-1 passes covered this bbox.)"
  exit 0
fi

echo "Found scene IDs: $(echo "$SCENE_IDS" | tr '\n' ' ')"
echo

RENDERED=0
FAILED=0

while IFS= read -r SID; do
  [[ -z "$SID" ]] && continue
  echo "--- Rendering ${SID} (crop to Bandar Abbas bbox) ---"
  if python3 visualisation.py "${SID}" \
      --crop-bbox "${BBOX_W}" "${BBOX_S}" "${BBOX_E}" "${BBOX_N}"; then
    RENDERED=$((RENDERED + 1))
  else
    echo "WARNING: render failed for scene ${SID}"
    FAILED=$((FAILED + 1))
  fi
  echo
done <<< "$SCENE_IDS"

# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #
echo "=== Done: ${RENDERED} scene(s) rendered, ${FAILED} failed ==="
if [[ "$RENDERED" -gt 0 ]]; then
  echo "Images written to:"
  find data/ -name '*_falsecolor*.jpg' -newer .env 2>/dev/null \
    | sort | sed 's/^/  /'
fi
