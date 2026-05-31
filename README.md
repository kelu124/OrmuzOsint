# OrmuzOsint — Sentinel-1 SAR + AIS Fusion for Dark-Vessel Detection

Open-source toolkit for **fusing Sentinel-1 SAR imagery with live AIS data** over the Strait of Hormuz (or any maritime AOI). Built for OSINT analysis: detect every ship the radar sees, cross-reference with what's broadcasting AIS, and surface the gap — the "dark" vessels.

All data sources are free. No paid satellite imagery, no commercial AIS subscriptions, no GIS suite required.

---

## Why this exists

Synthetic Aperture Radar (SAR) sees ships day or night, through clouds and haze. AIS tells you who's *claiming* to be where. The interesting boats are the ones SAR sees but AIS doesn't — IRGC fast craft running dark, sanctioned tankers spoofing positions, fishing vessels with disabled transponders.

This project gives you the three pieces you need:

1. **Download** the latest Sentinel-1 SAR scenes over any bounding box
2. **Render** them as high-resolution false-color JPGs where ships appear as bright point targets
3. **Collect** AIS broadcasts from the same area in parallel, ready to overlay

The default AOI is the Strait of Hormuz, but every script accepts `--bbox` so you can point it anywhere.

---

## Repository layout

```
.
├── download_sar.py        # Sentinel-1 scene downloader (SAFE + preview)
├── visualisation.py       # False-color renderer with native-res tiling, EXIF metadata
├── ais_listener.py        # Multi-source AIS collector (aisstream / NMEA-TCP / replay)
├── bandar_abbas_daily.sh  # Daily Bandar Abbas port imagery workflow
├── ais_sources/
│   └── README.md          # Annotated list of 15 AIS data sources
├── .env.example           # Credentials template
├── requirements.txt       # Minimal pip dependencies
├── requirements_full.txt  # Full dependencies including optional extras
├── LICENSE                # MIT
└── data/                  # Created on first run
    ├── safe/              # SAFE zips + rendered JPGs + tile manifests
    └── previews/          # Clipped GeoTIFF previews + sidecars
```

---

## Quick start

```bash
git clone https://github.com/kelu124/OrmuzOsint.git
cd OrmuzOsint
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env — see Credentials section below

# Download last 7 days of Sentinel-1 scenes over Hormuz
python download_sar.py --days 7

# List what arrived
python visualisation.py --list

# Render a scene (use the ID printed by --list)
python visualisation.py <ID>

# In a second terminal: collect live AIS
python ais_listener.py --bbox 54.5 25.0 57.5 27.5
```

---

## Credentials

All services are free. Create accounts and populate `.env`:

```dotenv
# Copernicus Data Space (https://dataspace.copernicus.eu — free account)
CDSE_USER=your@email.com
CDSE_PASSWORD=yourpassword

# Sentinel Hub OAuth2 client — CDSE Dashboard → User Settings → OAuth Clients → Create
SH_CLIENT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
SH_CLIENT_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# aisstream.io (https://aisstream.io — free, sign in with GitHub)
AISSTREAM_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

| Service | Used by | What it gives you | Cost |
|---|---|---|---|
| CDSE (OData) | `download_sar.py` | Full Sentinel-1 SAFE zips (~1 GB each) | Free |
| Sentinel Hub | `download_sar.py` | Clipped GeoTIFF previews (~30–100 MB, ~130 m/px) | Free: 30 000 processing units/month |
| aisstream.io | `ais_listener.py` | Global real-time AIS via WebSocket | Free, no quota |

The **Sentinel Hub OAuth client** is separate from your CDSE login. Log in to the [CDSE Dashboard](https://shapps.dataspace.copernicus.eu/dashboard/), go to **User Settings → OAuth Clients → Create**, and paste the `client_id` and `client_secret` into `.env`.

---

## Pipeline overview

```
Copernicus Data Space                          aisstream.io / NMEA receivers
        │                                               │
        ▼                                               ▼
  download_sar.py                              ais_listener.py
  • OData catalog query                        • Multi-source async
  • SAFE zip download (~1 GB)                  • Bbox filtering
  • Sentinel Hub preview GeoTIFF               • Hourly JSONL output
  • 429 retry with backoff                     • Auto-reconnect
        │                                               │
        ▼                                               ▼
  data/safe/*.SAFE.zip               ais_data/YYYY-MM-DD/HH.jsonl
  data/previews/*.SAFE.tif
        │
        ▼
  visualisation.py
  • σ0 calibration from LUT
  • False-color RGB at native 10 m
  • Geographic crop (--crop-bbox)
  • Tiling ≤ --max-dim px
  • EXIF metadata per tile
  • Idempotent (trimmed zip = done)
        │
        ▼
  data/safe/*_falsecolor*.jpg
  data/safe/*_falsecolor_tiles.json
```

The two halves (SAR + AIS) run independently. Cross-reference by acquisition timestamp: for each rendered scene, look up the acquisition time in `data/safe/<scene>.json` (`ContentDate.Start`) and pull AIS records from the matching `ais_data/YYYY-MM-DD/HH.jsonl` within ±5 minutes.

---

## `download_sar.py` — Sentinel-1 downloader

Queries the CDSE OData API for `S1*_IW_GRDH_1SDV_*` scenes (IW swath mode, high-resolution ground-range detected, dual-polarization VV+VH) that intersect a bounding box in a given time window, then downloads two products per scene:

- **Full SAFE zip** (`data/safe/<scene>.SAFE.zip`, ~1 GB) — complete Sentinel-1 product with raw measurement TIFFs, calibration LUTs, annotation XMLs, and manifests. Used by `visualisation.py` for native ~10 m rendering.
- **Clipped GeoTIFF preview** (`data/previews/<scene>.SAFE.tif`, ~30–100 MB) — Sentinel Hub Process API renders just the requested bbox as a 3-band FLOAT32 raster (σ0 VV, σ0 VH, VV/VH ratio), orthorectified, at ~130 m/px. Good for quick inspection.

Both are cached: re-running over overlapping date windows skips files that already exist. A `<scene>.json` sidecar is written alongside each file with catalog metadata.

### Usage

```bash
# Last 7 days, default Hormuz bbox
python download_sar.py --days 7

# Explicit date window
python download_sar.py --start 2026-05-01 --end 2026-05-31

# Custom bbox (Singapore Strait)
python download_sar.py --days 14 --bbox 103.5 1.0 104.5 1.5

# Skip SAFE downloads — only fetch previews (much faster, less disk)
python download_sar.py --days 7 --no-safe

# Skip previews — only fetch full SAFEs
python download_sar.py --days 7 --no-preview

# List matching scenes without downloading anything
python download_sar.py --days 14 --list-only

# Require scene footprint to fully contain the bbox (strict filter)
python download_sar.py --days 30 --full-coverage

# Convert any already-downloaded .tif previews to false-color JPGs
python download_sar.py --convert-previews

# Verbose (shows OData filter, per-request detail)
python download_sar.py --days 7 -v
```

### Full flag reference

| Flag | Type | Default | Description |
|---|---|---|---|
| `--days N` | int | — | Pull the last N days ending now. Mutually exclusive with `--start`. |
| `--start YYYY-MM-DD` | date | — | Window start (UTC, inclusive). Mutually exclusive with `--days`. |
| `--end YYYY-MM-DD` | date | now | Window end (UTC, exclusive). |
| `--bbox W S E N` | 4 floats | `54.5 25.0 57.5 27.5` | Area of interest (min_lon min_lat max_lon max_lat). |
| `--full-coverage` | flag | off | Client-side filter: keep only scenes whose footprint fully contains the bbox. |
| `--data-dir PATH` | path | `./data` | Root output directory. |
| `--no-safe` | flag | off | Skip full SAFE zip downloads. |
| `--no-preview` | flag | off | Skip Sentinel Hub preview downloads. |
| `--list-only` | flag | off | Print matching scenes; do not download. |
| `--convert-previews` | flag | off | Convert `.tif` previews in `data/previews/` to false-color JPGs (can be combined with download or run standalone). |
| `-v` / `--verbose` | flag | off | DEBUG-level logging (shows OData filter string, per-chunk download detail). |

### API endpoints

| Endpoint | Purpose |
|---|---|
| `POST https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token` | OAuth2 token (password grant for SAFE downloads; client_credentials for Sentinel Hub) |
| `GET https://catalogue.dataspace.copernicus.eu/odata/v1/Products` | OData catalog search |
| `GET https://download.dataspace.copernicus.eu/odata/v1/Products({id})/$value` | SAFE zip download (streamed) |
| `POST https://sh.dataspace.copernicus.eu/api/v1/process` | Sentinel Hub Process API (clipped preview GeoTIFF) |

### OData filter

The catalog query uses:

```
Collection/Name eq 'SENTINEL-1'
  and contains(Name,'_IW_GRDH_1SDV_')
  and ContentDate/Start gt <start>
  and ContentDate/Start lt <end>
  and OData.CSC.Intersects(area=geography'SRID=4326;POLYGON(…)')
```

`--full-coverage` applies an additional client-side filter after the API response: all four corners of the bbox must lie inside the scene's GeoFootprint polygon (ray-casting test).

### Sidecar JSON schema

Written alongside every downloaded file as `<scene>.json`:

```json
{
  "Id":            "uuid-string",
  "Name":          "S1A_IW_GRDH_1SDV_20260520T024545_…_C5BD.SAFE",
  "ContentDate":   { "Start": "2026-05-20T02:45:45.000Z", "End": "…" },
  "OriginDate":    "2026-05-20T06:12:00.000Z",
  "ContentLength": 1073741824,
  "Footprint":     "POLYGON((…))",
  "GeoFootprint":  { "type": "Polygon", "coordinates": [[…]] },
  "S3Path":        "s3://…"
}
```

### Rate limits and 429 handling

The **Sentinel Hub Process API** enforces free-tier rate limits. When a 429 is received:

1. The `Retry-After` response header is read — it contains the wait time in **milliseconds** (Sentinel Hub convention)
2. If the header is absent, exponential backoff is used, starting at 60 s and doubling each attempt (cap: 3600 s)
3. A log line is emitted: `Rate limit hit — waiting 42s before retry (attempt 2/5)`
4. The OAuth token is refreshed before the retry (tokens can expire during long waits)
5. After 5 failed attempts the error is raised

The **CDSE catalog and download endpoints** do not implement special 429 handling beyond the default `raise_for_status()` — they are rarely rate-limited for personal use.

### Preview GeoTIFF format

- Dimensions: 2500 × 2000 px (capped by Sentinel Hub free tier at 2500 × 2500)
- Bands: band 1 = σ0 VV, band 2 = σ0 VH, band 3 = σ0 VV / σ0 VH (linear power, FLOAT32)
- CRS: EPSG:4326 (WGS-84 geographic)
- Resolution: ~130 m/px over the default Hormuz bbox; finer for smaller bboxes
- Spatial reference tags: `ModelPixelScaleTag` (33550) + `ModelTiepointTag` (33922) — read by `visualisation.py` for geographic crop

### Expected scene counts

Sentinel-1A + Sentinel-1C together revisit Hormuz roughly every 3 days (combining ascending and descending orbits). About half of passes are dual-pol VV+VH. Expect **3–8 matching scenes per 14-day window** depending on orbit scheduling.

### Notes on `--full-coverage`

A Sentinel-1 IW swath is ~250 km wide and ~150–250 km along-track. The default Hormuz bbox (~330 × 280 km) is larger than a single scene along-track, so `--full-coverage` against it will frequently return zero results. The script warns when this happens. For a fully-covered workflow, shrink the bbox to ~150 km on the longest axis, or omit `--full-coverage` and let `visualisation.py --crop-bbox` handle geographic clipping at render time.

---

## `visualisation.py` — false-color renderer

Reads what `download_sar.py` left in `data/`, applies σ0 calibration (from the SAFE's calibration LUT), and writes a false-color JPG per scene with EXIF metadata. Operates only on SAFE zips; preview TIFFs are a fallback that triggers an automatic SAFE download when credentials are available.

### False-color recipe

| Channel | Source | dB stretch |
|---|---|---|
| **R** | σ0 VV | −25 to 0 dB |
| **G** | σ0 VH | −30 to −5 dB |
| **B** | VH / VV ratio | −10 to +5 dB |

Open water: dark blue-black (low backscatter, high cross-pol ratio). Land: olive-yellow. **Ships: bright yellow point targets** — strong metal return in both polarizations.

Calibration: σ0 = DN² / sigmaNought², where sigmaNought is bilinearly interpolated from the calibration LUT at each pixel's original-image coordinates (correct even for cropped tiles).

### Native-resolution tiling

A Sentinel-1 IW GRDH scene is ~25 000 × 16 500 px at native ~10 m. Rather than downsampling, the renderer keeps full resolution and splits into tiles where each tile's largest dimension ≤ `--max-dim` (default 8 000 px). With the default that produces typically a 4 × 3 = 12 tile grid.

### Filename convention

Single tile (image fits in one tile):
```
<scene>_falsecolor.jpg
```

Multi-tile grid:
```
<scene>_falsecolor_r{row:02d}c{col:02d}_y{y0}-{y1}_x{x0}-{x1}.jpg
```

Example: `S1A_IW_GRDH_1SDV_20260520T024545_..._falsecolor_r01c02_y005578-011156_x012896-019344.jpg`

A companion `<scene>_falsecolor_tiles.json` manifest records the full grid layout:

```json
{
  "scene": "S1A_IW_GRDH_1SDV_20260520T024545_…_C5BD.SAFE",
  "full_image": { "width": 25789, "height": 16734 },
  "grid": { "rows": 3, "cols": 4, "max_dim": 8000 },
  "tiles": [
    {
      "row": 0, "col": 0,
      "y_range": [0, 5578], "x_range": [0, 6448],
      "width": 6448, "height": 5578,
      "file": "…_falsecolor_r00c00_y000000-005578_x000000-006448.jpg"
    }
  ]
}
```

### EXIF metadata

Every output JPG is tagged with structured EXIF metadata (requires `pip install piexif`; silently skipped if not installed):

| EXIF field | Content |
|---|---|
| `ImageDescription` | Full Sentinel-1 scene name |
| `Make` | `Sentinel-1A` or `Sentinel-1C` (parsed from scene name) |
| `Software` | `OrmuzOsint` |
| `DateTimeOriginal` | Scene acquisition time (UTC, `YYYY:MM:DD HH:MM:SS`) |
| `GPSLatitude` / `GPSLongitude` | UL corner of the tile in WGS-84 decimal degrees (DMS rational encoding) |
| `GPSLatitudeRef` / `GPSLongitudeRef` | `N`/`S` and `E`/`W` |
| `GPSMapDatum` | `WGS-84` |

For multi-tile scenes each tile has its own GPS coordinates, computed by bilinear interpolation over the scene's geolocation grid at the tile's UL pixel position.

Use `--list-jpgs` to read and display all EXIF fields for every JPG in `data/safe/`.

### Idempotency

The pipeline is safe to run multiple times — it will never overwrite completed work:

1. **Trimmed zip** (no `measurement/` entries): scene is skipped immediately with `"Already processed"`. This is the permanent done marker set by `--trim-safe`.
2. **Tiles already present** (`*_falsecolor_r*c*.jpg` found next to the zip): scene is skipped.
3. **No SAFE zip + sidecar exists**: `visualisation.py` uses `download_sar.download_safe()` to fetch the zip from CDSE before rendering. Requires `CDSE_USER` / `CDSE_PASSWORD` in `.env`.

To force a re-render, delete the existing tiles (and the trimmed zip if applicable), then re-run. Or download a fresh SAFE with `download_sar.py`.

### Usage

```bash
# List all scenes found in data/
python visualisation.py --list

# List all output JPGs with EXIF metadata
python visualisation.py --list-jpgs

# Render a single scene by ID (or unique prefix)
python visualisation.py 163390df
python visualisation.py 1633       # prefix is fine

# Render all scenes in data/ in one call
python visualisation.py --all

# Render all scenes, crop to Bandar Abbas, trim SAFEs afterwards
python visualisation.py --all \
    --crop-bbox 56.12176 26.94227 56.55762 27.24841 \
    --trim-safe

# Force the medium-res preview path (faster but ~130 m/px)
python visualisation.py 163390df --prefer preview

# Larger tiles (fewer files, more RAM)
python visualisation.py 163390df --max-dim 16000

# Crop only (no trimming)
python visualisation.py 163390df --crop-bbox 56.12176 26.94227 56.55762 27.24841

# Verbose (shows per-tile detail, LUT timings, file sizes)
python visualisation.py 163390df -v
```

### Full flag reference

| Flag | Type | Default | Description |
|---|---|---|---|
| `scene_id` | string | — | Scene ID or unique prefix to render (positional). Omit with `--list`, `--list-jpgs`, or `--all`. |
| `--list` | flag | off | Print all scenes in `data/` (ID, sources, name) and exit. |
| `--list-jpgs` | flag | off | List all JPGs in `data/safe/`, explain filename structure, display EXIF metadata for each, then exit. |
| `--all` | flag | off | Render every scene in `data/`. Continues past per-scene errors; prints a summary (N rendered, N skipped, N failed). |
| `--max-dim N` | int | `8000` | Max pixel dimension per tile. Larger = fewer files, more peak RAM (~1.5 GB/tile + full image). |
| `--prefer {safe,preview}` | choice | `safe` | Source preference when both a SAFE zip and a preview TIF are present. |
| `--bbox W S E N` | 4 floats | Hormuz | Reference bbox for `--full-coverage` filtering (not for cropping). |
| `--full-coverage` | flag | off | Skip scenes whose footprint does not fully contain `--bbox`. |
| `--crop-bbox W S E N` | 4 floats | — | Crop the output image to this geographic bbox (min_lon min_lat max_lon max_lat). Uses geolocation grid (SAFE) or GeoTIFF tags (preview) to find pixel bounds. |
| `--trim-safe` | flag | off | After rendering, delete `measurement/` entries from the SAFE zip (saves ~900 MB–1 GB). The trimmed zip becomes the permanent done marker. |
| `-v` / `--verbose` | flag | off | DEBUG-level logging. |

### Disk-space management (`--trim-safe`)

`--trim-safe` rewrites the SAFE zip in-place, keeping only annotation, calibration, manifest, and support files (~5–20 MB) and discarding the raw GRD TIFFs (~900 MB–1 GB). The operation is atomic — a `.trimming` temp file is written first, then renamed over the original. If writing fails the original is untouched.

**A trimmed zip is the permanent done marker.** On any subsequent run `visualisation.py` detects the absent `measurement/` folder and skips the scene. If you need to re-render (different crop, different tile size), re-download the full SAFE first with `download_sar.py`.

### Memory usage

Peak RAM ≈ 1.5 GB per tile during false-color computation, plus the full DN arrays (~1.6 GB for a typical IW GRDH scene). Total: **~3 GB** at default settings (`--max-dim 8000`). Bump `--max-dim 16000` for 4 tiles instead of 12 at ~6 GB peak.

### Why σ0 and not γ0?

γ0 = σ0 / cos(incidence angle). For Sentinel-1 IW (θ ≈ 30°–46°) that's 0.6–1.6 dB — visually identical after the dB stretch applied here. The incidence angle per pixel is available in `annotation/s1a-iw-grd-*.xml` inside the SAFE if you need true γ0 for quantitative work.

---

## `ais_listener.py` — multi-source AIS collector

Subscribes to one or more AIS sources concurrently, filters every message by the bounding box, and writes matches to hourly JSONL files. All sources auto-reconnect with exponential backoff (1 s → 60 s cap). SIGINT and SIGTERM trigger graceful shutdown — the queue is drained before exit.

### Sources

| Source ID | What it connects to | Dependency |
|---|---|---|
| `aisstream` | `wss://stream.aisstream.io/v0/stream` — global real-time WebSocket with server-side bbox filtering | `pip install websockets` |
| `nmea-tcp` | Any NMEA 0183 AIS receiver over TCP (RTL-SDR + AIS-catcher, dAISy dongle, a friend's gateway) | `pip install pyais` |
| `replay` | Replay an existing JSONL capture at configurable speed | (none) |

### Usage

```bash
# aisstream.io, Hormuz bbox
python ais_listener.py --bbox 54.5 25.0 57.5 27.5

# aisstream + NMEA-TCP receiver (e.g. AIS-catcher on a local Raspberry Pi)
python ais_listener.py --bbox 54.5 25.0 57.5 27.5 \
    --sources aisstream nmea-tcp \
    --nmea-tcp 192.168.1.42:4002

# Multiple NMEA endpoints
python ais_listener.py --bbox 54.5 25.0 57.5 27.5 \
    --sources nmea-tcp \
    --nmea-tcp host-a:4002 \
    --nmea-tcp host-b:10110

# Replay a previous capture (no network, useful for testing)
python ais_listener.py --bbox 54.5 25.0 57.5 27.5 \
    --sources replay \
    --replay-file ais_data/2026-05-23/14.jsonl

# Replay with deliberate delay between messages
python ais_listener.py --bbox 54.5 25.0 57.5 27.5 \
    --sources replay \
    --replay-file ais_data/2026-05-23/14.jsonl \
    --replay-delay 0.1

# Run detached in background
nohup python ais_listener.py --bbox 54.5 25.0 57.5 27.5 > ais.log 2>&1 &
tail -f ais.log
```

### Full flag reference

| Flag | Type | Default | Description |
|---|---|---|---|
| `--bbox W S E N` | 4 floats | **required** | Bounding box (min_lon min_lat max_lon max_lat). All messages outside it are dropped. |
| `--sources` | list | `aisstream` | Which sources to run concurrently: `aisstream`, `nmea-tcp`, `replay`. |
| `--nmea-tcp HOST:PORT` | string | — | NMEA-TCP endpoint. Repeat for multiple receivers. |
| `--replay-file PATH` | path | — | JSONL file to replay (canonical schema). |
| `--replay-delay SEC` | float | `0` | Seconds between replayed messages. `0` = as fast as possible. |
| `--output-dir PATH` | path | `./ais_data` | Where to write hourly JSONL files. |
| `--stats-interval SEC` | int | `60` | Seconds between throughput log lines. |
| `-v` / `--verbose` | flag | off | DEBUG-level logging. |

### Canonical record schema

Every message from every source is normalized to the same shape before writing:

```json
{
  "source":      "aisstream",
  "received_at": "2026-05-24T16:08:09.123+00:00",
  "type":        "PositionReport",
  "mmsi":        211223344,
  "lat":         26.012345,
  "lon":         55.678901,
  "ship_name":   "EXAMPLE TANKER",
  "time_utc":    "2026-05-24 16:08:08.412345 +0000 UTC",
  "raw":         { }
}
```

The `raw` field preserves the original source payload so no information is lost.

### Output layout

```
ais_data/
├── 2026-05-24/
│   ├── 14.jsonl     ← UTC hour 14:00–14:59
│   ├── 15.jsonl
│   └── 16.jsonl
└── 2026-05-25/
    ├── 00.jsonl
    └── …
```

Files are append-only and line-flushed — safe to `tail -f`, `wc -l`, or rsync while the listener is running.

### Architecture

One asyncio event loop. Each source is a `Source` subclass running as a concurrent task, pushing canonical records into a shared `asyncio.Queue` (max 10 000 items). A `writer_task` drains the queue into hourly JSONL files. A `stats_task` logs throughput. SIGINT/SIGTERM sets a stop event; the loop drains the queue (10 s timeout), then cancels all tasks.

### Extending with a new source

Subclass `Source`, implement `async def run(self, bbox, queue, stats)`, call `await self._emit(raw_dict, bbox, queue, stats)` for each message. Add a branch to `normalize()` for the canonical schema mapping, add `choices` to `--sources`, and add a constructor in `build_sources()`. The framework handles filtering, writing, stats, reconnect, and shutdown.

### Reconnect behaviour

Both `aisstream` and `nmea-tcp` sources wrap their main loop in a `while True` / `try/except` with exponential backoff: 1 s on the first failure, doubling each time, capped at 60 s. Reconnects are logged at WARNING level.

---

## `bandar_abbas_daily.sh` — Port of Bandar Abbas daily imagery

A convenience wrapper that pulls the last N days of Sentinel-1 scenes intersecting the Port of Bandar Abbas and renders each as a false-color crop of the port area.

**Bandar Abbas bbox:** `W=56.12176  S=26.94227  E=56.55762  N=27.24841`
Covers Shahid Rajaee Container Port (west), the old fishing/commercial port (east), and the anchorage zone south of the breakwater.

### Usage

```bash
# Default: last 7 days
bash bandar_abbas_daily.sh

# Last 14 days
bash bandar_abbas_daily.sh --days 14

# Also download full SAFE products (~1 GB/scene, native ~10 m resolution)
bash bandar_abbas_daily.sh --with-safe
```

### Arguments

| Argument | Default | Description |
|---|---|---|
| `--days N` | `7` | Number of days to look back. |
| `--with-safe` | off | Download full SAFE zips in addition to previews. Without this flag, only Sentinel Hub preview GeoTIFFs are downloaded. |

### What it does, step by step

1. Sources `.env` for credentials (`CDSE_USER`, `CDSE_PASSWORD`, `SH_CLIENT_ID`, `SH_CLIENT_SECRET`)
2. Runs `download_sar.py --days N --bbox 56.12176 26.94227 56.55762 27.24841` (optionally with `--no-safe`)
3. Runs `visualisation.py --all --crop-bbox 56.12176 26.94227 56.55762 27.24841 --trim-safe`
4. Lists all `*_falsecolor*.jpg` output files

### Idempotency of daily runs

`--trim-safe` is applied automatically. After a scene is rendered the first time, the SAFE zip's raw measurement TIFFs are removed and the trimmed zip becomes the permanent done marker. Re-running the script skips already-rendered scenes, only processing genuinely new arrivals. The cron equivalent:

```bash
# /etc/cron.d/bandar_abbas
0 6 * * * cd /opt/OrmuzOsint && bash bandar_abbas_daily.sh >> /var/log/bandar_abbas.log 2>&1
```

---

## Fusion workflow

```bash
# Terminal 1: collect AIS continuously over the AOI
python ais_listener.py --bbox 54.5 25.0 57.5 27.5

# Terminal 2: every few days, pull new SAR and render
python download_sar.py --days 7
python visualisation.py --all --crop-bbox 54.5 25.0 57.5 27.5 --trim-safe
python visualisation.py --list-jpgs
```

For each rendered scene:

1. Open `data/safe/<scene>.json`, read `ContentDate.Start` — that's the acquisition timestamp
2. Open `ais_data/YYYY-MM-DD/HH.jsonl` for the matching UTC hour (± a few minutes on either side)
3. Filter AIS records to the same bbox
4. Project each AIS lat/lon onto the JPG pixel grid using the tile manifest's `y_range` and `x_range`
5. Compare:
   - **SAR + matching AIS**: identified vessel (MMSI → name, type, flag)
   - **SAR without AIS**: dark vessel — investigate

---

## Dependencies

### Core (required for all scripts)

```
requests
python-dotenv
tqdm
numpy
tifffile
imagecodecs      # ZSTD/LZW decode for Sentinel Hub GeoTIFFs
Pillow
websockets
```

### Optional

```
pyais            # --sources nmea-tcp (NMEA 0183 AIS decoding)
piexif           # EXIF metadata in output JPGs (visualisation.py)
```

Install everything:

```bash
pip install -r requirements.txt
pip install pyais piexif   # optional extras
```

---

## Caveats

**SAR resolution.** Sentinel-1 IW GRDH is ~10 m (native) / ~20 m (effective ground resolution). You can detect a 30 m vessel reliably, estimate length to within ~20 m, but not classify ship type from pixel intensity alone. Sub-meter classification requires commercial SAR (Umbra, Capella, ICEYE).

**AIS coverage gaps.** aisstream.io is the only truly-free global real-time stream available without contributing your own receiver. Mid-strait coverage in Hormuz depends on satellite AIS uplink cadence. Small craft (wooden dhows, IRGC fast boats) often have weak radar cross-sections and no AIS transponder.

**Sentinel-1 revisit.** ~3-day average for Hormuz (A+C combined). Significant events can happen between passes.

**σ0 vs γ0.** The script uses σ0 ellipsoid (no DEM-based terrain correction). For open-water maritime AOIs this is fine. Coastal/terrain-affected scenes would need full radiometric terrain correction via SNAP.

**Not for navigation or operational use.** This is an OSINT / research tool. Data has gaps, calibration is approximate, the AIS feed is incomplete. Do not use it to make navigation decisions.

---

## License

[MIT](LICENSE) — do what you want, no warranty, attribution appreciated.

---

## Acknowledgments

- **ESA Copernicus & Sentinel-1** — free, open, high-quality SAR data
- **Sentinel Hub on CDSE** — Process API for clipped previews
- **aisstream.io** — global AIS WebSocket kept genuinely free
- **pyais** maintainers — clean NMEA decoder
- Bellingcat, TankerTrackers, Global Fishing Watch — showing what's possible with open data
