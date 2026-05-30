#!/usr/bin/env bash
# bandar_abbas_daily.sh — Daily Sentinel-1 imagery for Port of Bandar Abbas.
#
# For each of the last N days (default 7), downloads the first scene whose
# footprint fully covers the Bandar Abbas port bbox, then renders a
# false-color crop of that geographic area using visualisation.py.
#
# Bandar Abbas bbox (Shahid Rajaee + old port + anchorage):
#   W=56.12176  S=26.94227  E=56.55762  N=27.24841
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
BBOX_W=56.12176
BBOX_S=26.94227
BBOX_E=56.55762
BBOX_N=27.24841
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
echo "--- Rendering all scenes (crop to Bandar Abbas bbox) ---"

# --all processes every scene in data/ in one call.
# --trim-safe removes raw measurement/ TIFFs (~1 GB) after tiling.
# If a zip was previously trimmed, visualisation.py auto-downloads it first.
python3 visualisation.py --all \
  --crop-bbox "${BBOX_W}" "${BBOX_S}" "${BBOX_E}" "${BBOX_N}" \
  --trim-safe \
  || echo "WARNING: one or more scenes failed — check output above."

# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #
echo
echo "=== Done — $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
echo "Images:"
find data/ -name '*_falsecolor*.jpg' 2>/dev/null | sort | sed 's/^/  /'
