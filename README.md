# OrmuzOsint — Sentinel-1 SAR Toolkit for Ship Spotting

Open-source toolkit for downloading and rendering [Sentinel-1](https://sentinels.copernicus.eu/web/sentinel/missions/sentinel-1)
synthetic-aperture radar imagery over the **Strait of Hormuz** (or any other
bounding box). Two small Python scripts:

1. **`download_sar.py`** — pulls recent Sentinel-1 IW GRD (VV+VH) scenes from
   the Copernicus Data Space Ecosystem, plus a clipped GeoTIFF preview from
   the Sentinel Hub Process API.
2. **`visualisation.py`** — renders that data into false-colour JPGs you can
   browse for ships, ideally at native ~10 m resolution via automatic tiling.

Built with free public APIs only. MIT-licensed.

> **Why SAR over Hormuz?** Roughly 20% of global oil flows through this
> strait. SAR works through cloud and at night — and Sentinel-1 is the only
> open, free, near-real-time SAR mission. It's the workhorse of maritime
> OSINT.

---

## Table of contents

- [What this does](#what-this-does)
- [What this does *not* do](#what-this-does-not-do)
- [Quickstart](#quickstart)
- [Setup](#setup)
- [Usage: `download_sar.py`](#usage-download_sarpy)
- [Usage: `visualisation.py`](#usage-visualisationpy)
- [How it works](#how-it-works)
- [Output layout](#output-layout)
- [Resource considerations](#resource-considerations)
- [Limitations and caveats](#limitations-and-caveats)
- [Where to go next](#where-to-go-next)
- [Troubleshooting](#troubleshooting)
- [License](#license)
- [Acknowledgments](#acknowledgments)

---

## What this does

- **Discover** all Sentinel-1 IW GRD scenes (Interferometric Wide swath,
  Ground Range Detected, dual-pol VV+VH) intersecting an area of interest
  within a date window.
- **Download** the full SAFE products (~1 GB each, calibration-ready) from
  Copernicus.
- **Render** clipped, lightweight previews server-side via Sentinel Hub.
- **Visualise** scenes locally as false-colour JPGs, with a recipe tuned for
  ship detection on water (R = VV, G = VH, B = VH/VV ratio).
- **Tile** native-resolution renders so any image, however large, fits in
  digestible per-tile JPGs with a manifest.
- **Filter** the catalog so you only see scenes whose footprint fully covers
  your AOI.

Caching is implicit: re-running over overlapping date ranges or re-rendering
the same scene is idempotent — already-downloaded products are skipped.

## What this does *not* do

- No automated ship detection (CFAR, deep learning, etc.). The render gives
  you visually obvious bright targets; turning that into a vessel list is a
  separate step.
- No AIS correlation, transponder spoofing checks, or "dark vessel" flagging.
- No georeferencing of the output JPGs into a GIS. The preview GeoTIFFs are
  georeferenced; the rendered JPGs from the full SAFE are in image
  coordinates only.
- No SLC processing (interferometry, polarimetric decomposition). GRD only.

---

## Quickstart

```bash
git clone https://github.com/<your-account>/OrmuzOsint.git
cd OrmuzOsint
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env: fill in CDSE_USER, CDSE_PASSWORD, SH_CLIENT_ID, SH_CLIENT_SECRET

# Pull the last 2 weeks of scenes intersecting the default Hormuz bbox
python download_sar.py --days 14

# See what's downloaded
python visualisation.py --list

# Render a scene (use the 8-char ID from --list, or a unique prefix)
python visualisation.py <ID>
```

That's it. You'll get JPGs under `data/safe/` (native ~10 m, tiled if the
scene exceeds 8000 px) and `data/previews/` (clipped to the bbox, lower
resolution but instantly viewable).

---

## Setup

### Prerequisites

- **Python 3.10+** (uses `tuple[...]` PEP 604 typing).
- **A few gigabytes of disk** — each full SAFE product is ~1 GB.
- **About 3 GB of RAM** for native-resolution rendering of a full IW GRDH
  scene.

### Accounts

All free:

| Service | What it gives you | Sign up |
|---|---|---|
| Copernicus Data Space Ecosystem (CDSE) | OData catalog + product downloads | <https://dataspace.copernicus.eu> |
| Sentinel Hub OAuth client (on CDSE) | Process API for clipped previews | CDSE Dashboard → User Settings → OAuth Clients |
| aisstream.io *(optional, future AIS work)* | Live terrestrial AIS WebSocket | <https://aisstream.io> |

### `.env` file

Copy `.env.example` to `.env` and fill in the four CDSE/Sentinel Hub fields.
Both scripts read it automatically via `python-dotenv`.

```dotenv
CDSE_USER=you@example.com
CDSE_PASSWORD=...
SH_CLIENT_ID=...
SH_CLIENT_SECRET=...
```

`.env` is gitignored by default — never commit it.

### Installation

```bash
pip install -r requirements.txt
```

This pulls in:

- `requests`, `python-dotenv`, `tqdm` — downloads
- `numpy`, `tifffile`, `imagecodecs`, `Pillow` — visualisation

`imagecodecs` is mandatory: the Sentinel Hub Process API returns ZSTD-
compressed TIFFs and `tifffile` needs `imagecodecs` to decode them.

---

## Usage: `download_sar.py`

Queries the CDSE OData catalog for Sentinel-1 IW GRDH dual-polarisation
(VV+VH) scenes, then for each one:

1. Downloads the full `.SAFE.zip` from CDSE.
2. Renders a clipped GeoTIFF preview (3 bands: VV, VH, VV/VH ratio; σ⁰
   linear power, float32) via the Sentinel Hub Process API.

Both outputs are cached by filename, so re-runs only fetch what's missing.

### Flags

```text
--start YYYY-MM-DD       Start date (UTC, inclusive)
--days N                 Convenience: last N days (mutually exclusive with --start)
--end YYYY-MM-DD         End date (UTC, exclusive). Defaults to now.

--bbox MIN_LON MIN_LAT MAX_LON MAX_LAT
                         Area of interest. Default: Strait of Hormuz
                         (54.5, 25.0, 57.5, 27.5).
--full-coverage          Only keep scenes whose footprint fully contains
                         the bbox (default: any-intersection).

--data-dir PATH          Output directory (default: ./data).
--no-safe                Skip full SAFE downloads (previews only).
--no-preview             Skip preview generation (SAFE only).
--list-only              Print matching scenes and exit. No downloads.
-v, --verbose            DEBUG-level logging.
```

### Examples

```bash
# Last 2 weeks over Hormuz, full pipeline
python download_sar.py --days 14

# Specific date range, dry-run first
python download_sar.py --start 2026-05-01 --end 2026-05-24 --list-only
python download_sar.py --start 2026-05-01 --end 2026-05-24

# A different AOI (e.g. Bab-el-Mandeb)
python download_sar.py --days 14 --bbox 42.5 12.0 44.0 13.5

# Only scenes that fully cover a small AOI
python download_sar.py --days 30 \
    --bbox 55.5 26.0 57.0 27.0 --full-coverage --list-only

# Light pipeline: only the previews (no 1 GB SAFE downloads)
python download_sar.py --days 14 --no-safe
```

### About `--full-coverage`

The CDSE OData spec only reliably supports `OData.CSC.Intersects(area=...)`
server-side. To find scenes that *fully cover* a bbox, the script does:

1. Query the catalog for any intersection (server-side).
2. For each result, parse the returned `GeoFootprint` (GeoJSON polygon).
3. Test that all four bbox corners lie inside the footprint.

This is exact for the quasi-trapezoidal Sentinel-1 IW footprints (they're
always convex), and adds zero extra API calls.

A single Sentinel-1 IW scene covers ~250 km swath × ~150–250 km along-track.
If your bbox is larger than that on either axis, **no single scene will
fully cover it** — the script warns when the filter empties the result set.
For the Hormuz default bbox (300 × 280 km), `--full-coverage` is generally
too strict; shrink the AOI to the actual strait (~55.5 26.0 57.0 27.0).

---

## Usage: `visualisation.py`

Reads the data in `./data/` and renders false-colour JPGs.

### Flags

```text
ID                       Scene ID (or unique prefix). See --list.
--list                   List scenes in data/ and exit.
--max-dim N              Max pixel dim per tile when rendering from SAFE
                         (default 8000). Larger = fewer tiles + more RAM;
                         smaller = more tiles + less RAM.
--prefer {safe,preview}  Which source to use when both exist (default safe).
--bbox MIN_LON MIN_LAT MAX_LON MAX_LAT
                         Reference area for --full-coverage. Default: Hormuz.
--full-coverage          When listing or selecting, only consider scenes
                         whose footprint fully contains the bbox (uses the
                         GeoFootprint cached in the sidecar JSON).
-v, --verbose            DEBUG-level logging.
```

### Examples

```bash
# Show every scene
python visualisation.py --list

# Show only scenes that fully cover the strait
python visualisation.py --list --bbox 55.5 26.0 57.0 27.0 --full-coverage

# Render a scene
python visualisation.py 163390df

# Render at lower native resolution (no tiling); fits in one ~5000×3000 JPG
python visualisation.py 163390df --max-dim 30000

# Force the small preview render (much faster, lower res)
python visualisation.py 163390df --prefer preview
```

### Output filenames

When the scene needs no tiling (one tile covers it):

```text
<scene_name>_falsecolor.jpg
```

When tiling is needed, every tile is its own JPG, with position encoded in
the filename so tiles relate back to the original image:

```text
<scene>_falsecolor_r{row:02d}c{col:02d}_y{y0:06d}-{y1:06d}_x{x0:06d}-{x1:06d}.jpg
```

A companion manifest is also written:

```text
<scene>_falsecolor_tiles.json
```

It records the full image dimensions, grid shape, `max_dim` used, and the
filename → pixel-range mapping for every tile.

---

## How it works

### The Sentinel-1 IW GRDH product, briefly

Sentinel-1 acquires C-band SAR data in the Interferometric Wide (IW) swath
mode over most of Earth's land and coastal seas. The Ground Range Detected
(GRD) High-resolution (H) product is:

- 250 km swath, ~10 m × 10 m pixel spacing
- Two polarisations: VV (vertical-transmit / vertical-receive) and VH
  (vertical-transmit / horizontal-receive)
- Revisit: every ~6 days at the equator with one satellite; the Hormuz
  area sees a useful pass every 2–4 days with the current Sentinel-1A +
  Sentinel-1C pair, but only some are dual-pol VV+VH.

The raw measurement is a digital number (DN) per pixel — proportional to,
but not directly, the radar backscatter. Calibration is required.

### From DN to σ⁰

The standard normalised radar cross-section, σ⁰ (sigma-nought, linear
power), is computed via the per-scene calibration LUT:

```text
σ⁰(line, pixel) = DN(line, pixel)² / sigmaNought(line, pixel)²
```

`sigmaNought` is provided as a sparse 2D grid in
`annotation/calibration/calibration-*.xml`. The visualisation script
parses it, bilinearly interpolates it onto every output pixel coordinate,
and applies the formula. No SNAP, no rasterio, no GDAL — pure NumPy.

A practical note: γ⁰ (gamma-nought) = σ⁰ / cos(θ_incidence) differs from
σ⁰ by 0.6–1.6 dB across the Sentinel-1 IW incidence range. After a dB
stretch for visualisation, the difference is invisible. The script uses
σ⁰ for simplicity; if you need true γ⁰ for quantitative work you'll
also need to parse `annotation/<pol>.xml` for the incidence angle grid.

### False-colour recipe

```text
R = VV       stretched   -25..0 dB  →  0..255
G = VH       stretched   -30..-5 dB →  0..255
B = VH / VV  stretched   -10..+5 dB →  0..255
```

What you see on the map:

| Feature | Why |
|---|---|
| **Dark blue / black water** | Calm water reflects radar away from the satellite — low σ⁰ in both polarisations. |
| **Bright yellow / white points** | Metal ship hulls and superstructures cause strong corner-reflector returns — high VV *and* high VH. |
| **Olive / khaki land** | Vegetated or built-up land has medium VV and slightly elevated VH. |
| **Red / orange streaks** | Rough sea or wind streaks — high VV, low VH (ratio is low). |

### Tiling

A full IW GRDH scene is ~25,000 × 16,500 px. Rendering monolithically would
need ~10 GB of RAM and produce an unwieldy JPG. Instead, the visualisation
script splits the image into a grid where every tile is ≤ `--max-dim` px
on each axis (default 8000), processes one tile at a time, and writes each
to its own JPG. The calibration LUT is sampled at original-image coordinates
for every tile so seams are pixel-accurate.

Default Hormuz scene → 4 × 3 = 12 tiles, ~5 MB each, ~3 GB peak RAM.

---

## Output layout

After running both scripts:

```text
data/
├── safe/
│   ├── S1A_IW_GRDH_1SDV_20260520T024545_..._C5BD.SAFE.zip        # raw GRD product (~1 GB)
│   ├── S1A_IW_GRDH_1SDV_20260520T024545_..._C5BD.SAFE.json       # sidecar: catalog metadata + GeoFootprint
│   ├── S1A_IW_GRDH_1SDV_..._falsecolor_r00c00_y000000-006250_x000000-005500.jpg   # tile
│   ├── S1A_IW_GRDH_1SDV_..._falsecolor_r00c01_y000000-006250_x005500-011000.jpg
│   ├── … (12 tiles total)
│   └── S1A_IW_GRDH_1SDV_..._falsecolor_tiles.json                # tile manifest
└── previews/
    ├── S1A_IW_GRDH_1SDV_..._C5BD.SAFE.tif                        # 3-band σ⁰ GeoTIFF, clipped to bbox
    ├── S1A_IW_GRDH_1SDV_..._C5BD.SAFE.json                       # sidecar (same content)
    └── S1A_IW_GRDH_1SDV_..._falsecolor.jpg                       # rendered preview
```

The sidecar JSON contains the full CDSE catalog entry — `Id`, `Name`,
`ContentDate`, `Footprint` (WKT), `GeoFootprint` (GeoJSON), `S3Path`. Useful
for downstream tooling and required for `--full-coverage` filtering.

---

## Resource considerations

### Storage

- Per scene: ~1 GB SAFE + ~30 MB preview GeoTIFF + ~30–60 MB rendered tiles.
- A month of Hormuz coverage: typically 8–20 scenes → 10–25 GB.

### Memory (visualisation)

- Reading both VV and VH measurement TIFFs from a SAFE: ~1.6 GB peak.
- Processing one ≤ 8000 × 8000 tile: ~1.5 GB extra.
- **Peak: ~3 GB** with default `--max-dim 8000`.
- Set `--max-dim 4000` if you're tight on RAM (more tiles, half the peak).
- Set `--max-dim 30000` for a single-file monolithic render if you have
  10+ GB of RAM and want one giant JPG.

### API quotas

- **CDSE OData**: very permissive for personal use; rate-limited but you'd
  have to be aggressive to hit it.
- **Sentinel Hub Process API**: free tier gives 30,000 Processing Units
  (PU) per month. A single Hormuz-bbox preview costs ~2–5 PU. The script
  uses ~3 PU per scene → comfortably under quota for daily collection.

---

## Limitations and caveats

- **Small/wooden/fiberglass vessels are often invisible.** Dhows and small
  fishing boats — exactly the ones often most operationally interesting in
  Hormuz — have weak radar cross-sections and frequently disappear into
  sea clutter, especially in rough seas. Sentinel-1 reliably shows
  tankers and cargo ships; everything else is partial.
- **No AIS correlation.** The whole point of free SAR for ship-spotting is
  identifying "dark" vessels (no AIS broadcast). This toolkit does the SAR
  half; you need separate AIS data (aisstream.io, Global Fishing Watch
  API) to compute the gap.
- **Revisit cadence.** Hormuz gets a useful Sentinel-1 pass every 2–4 days
  on average with the current Sentinel-1A + Sentinel-1C constellation; only
  about half of those are dual-pol VV+VH. Plan accordingly.
- **Footprint ≠ swath.** A scene's footprint can be ~250 × 250 km, but each
  scene is acquired along an oriented ground track. The Hormuz default
  bbox (300 × 280 km) is larger than a single scene's along-track length,
  so `--full-coverage` against the default bbox will routinely return
  zero scenes.
- **Output JPGs are not georeferenced.** The preview GeoTIFF *is*
  georeferenced (and Sentinel Hub orthorectifies it), but the rendered
  JPGs are in image coordinates only. The tile manifest gives you the
  pixel ranges; converting to lon/lat for the SAFE-rendered JPGs requires
  the GCPs from `annotation/*.xml`.
- **σ⁰ vs γ⁰.** The script renders σ⁰. For quantitative work
  (cross-scene comparisons, time-series at fixed targets) γ⁰ is the
  better-behaved quantity. Adding it requires parsing the incidence-angle
  grid — straightforward but not implemented here.

---

## Where to go next

If you want to turn this from a viewer into a real ship-tracking system:

1. **Ship detection (CFAR)** — run a Constant False Alarm Rate detector on
   the calibrated VV σ⁰ image. SUMO, `pyroSAR`, or a hand-rolled CFAR are
   all reasonable. Each detection is an (x, y) pixel + estimated length.
2. **Georeferencing detections** — use the GCPs in
   `annotation/*-grd-*.xml` to convert (line, pixel) → (lon, lat).
3. **AIS fusion** — pull AIS from aisstream.io (or Global Fishing Watch) for
   ±15 minutes around the scene acquisition time, filter to the bbox, and
   correlate. SAR detections with no matching AIS track within e.g. 200 m
   are "dark" candidates.
4. **Time-series** — assemble σ⁰ stacks at fixed coordinates (anchorages,
   port gates) to track traffic patterns over weeks.
5. **Classification** — train or fine-tune a CNN on labelled SAR ship
   chips. Public starting datasets: OpenSARShip, FUSAR-Ship, HRSID, SSDD.

---

## Troubleshooting

### `ModuleNotFoundError: No module named 'compression'`

`tifffile` is trying to decode a ZSTD-compressed TIFF (Sentinel Hub
default) and falling through to a Python 3.14-only stdlib module. Install
the optional codec backend:

```bash
pip install imagecodecs
```

### "No scenes found" but I'm sure there are some

Try `--days 30` and `--list-only` to widen the window. If the catalog is
still empty, the CDSE catalog occasionally lags ingestion by a few hours —
check <https://dataspace.copernicus.eu/news>.

### `--full-coverage` returns zero scenes

Your bbox is larger than a single Sentinel-1 IW scene footprint
(~250 × 250 km maximum). Shrink the bbox or drop the flag.

### Out of memory during rendering

Lower `--max-dim`:

```bash
python visualisation.py <ID> --max-dim 4000   # ~1.5 GB peak instead of ~3 GB
```

### Token / 401 errors

CDSE access tokens expire after 10 minutes. The script refreshes them
before each SAFE download, but if a single SAFE takes longer than ~10
minutes (very large product, slow connection) the download may fail
partway. Re-run — the partial file will be cleaned up and the download
resumes from scratch.

---

## License

[MIT](./LICENSE). Do what you want with it.

The data itself remains subject to its sources' terms:

- Sentinel-1 imagery — Copernicus Open Access, free for any use including
  commercial, with attribution.
- Sentinel Hub Process API — free tier subject to fair use.

---

## Acknowledgments

- **ESA / Copernicus** — Sentinel-1 mission and free open data policy.
- **Copernicus Data Space Ecosystem** — catalog and product distribution.
- **Sentinel Hub** — Process API for on-the-fly imagery.
- **`tifffile`** by Christoph Gohlke — robust BigTIFF reading.
- The broader **OSINT / SAR community** — Bellingcat, TankerTrackers,
  Global Fishing Watch, and the academic literature on SAR ship
  detection that this rests on.