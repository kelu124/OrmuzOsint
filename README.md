# Hormuz Ship Tracker

Open-source toolkit for **fusing Sentinel-1 SAR imagery with live AIS data** over the Strait of Hormuz (or any other maritime AOI). Built for OSINT-style analysis: detect every ship the radar sees, cross-reference with what's broadcasting AIS, and surface the gap — the "dark" vessels.

All data sources are free. No paid satellite imagery, no commercial AIS subscriptions, no GIS suite required.

---

## Why this exists

Synthetic Aperture Radar (SAR) sees ships day or night, through clouds and haze. AIS tells you who's *claiming* to be where. The interesting boats are the ones SAR sees but AIS doesn't — IRGC fast craft running dark, sanctioned tankers spoofing positions, fishing boats with disabled transponders.

This project gives you the three pieces you need:

1. **Download** the latest Sentinel-1 SAR scenes over a bounding box
2. **Visualize** them as high-resolution false-color images where ships pop visually
3. **Collect** AIS broadcasts in the same area in parallel, ready to overlay

The default AOI is the Strait of Hormuz, but every script accepts `--bbox` so you can point it anywhere — Singapore Strait, Bab-el-Mandeb, the Bosphorus, the English Channel, your local marina.

---

## The pipeline

```
                         ┌──────────────────────────┐
                         │  Copernicus Data Space   │  Sentinel-1 IW GRDH
                         │  + Sentinel Hub          │  (free, ~3 days revisit)
                         └────────────┬─────────────┘
                                      │
                         ┌────────────▼─────────────┐
                         │     download_sar.py      │
                         │  • OData catalog query   │
                         │  • Full SAFE .zip + clip │
                         │  • Bbox-coverage filter  │
                         └────────────┬─────────────┘
                                      │
                                      ▼
                              data/safe/*.SAFE.zip
                              data/previews/*.SAFE.tif
                                      │
                         ┌────────────▼─────────────┐
                         │    visualisation.py      │
                         │  • σ⁰ calibration        │
                         │  • False-color RGB       │
                         │  • Native-res tiling     │
                         └────────────┬─────────────┘
                                      │
                                      ▼
                              data/safe/*_falsecolor*.jpg
                              data/safe/*_tiles.json

                         ┌──────────────────────────┐
                         │   aisstream.io WebSocket │  Live AIS
                         │   NMEA receivers         │  (free, real-time)
                         │   Replay files           │
                         └────────────┬─────────────┘
                                      │
                         ┌────────────▼─────────────┐
                         │     ais_listener.py      │
                         │  • Multi-source async    │
                         │  • Bbox filter           │
                         │  • Hourly JSONL output   │
                         └────────────┬─────────────┘
                                      │
                                      ▼
                              ais_data/YYYY-MM-DD/HH.jsonl
```

The two halves run independently and produce timestamped artifacts that can be cross-referenced offline: for every SAR scene's acquisition window, query the corresponding AIS hour file and compare positions.

---

## Repository layout

```
.
├── download_sar.py        # Sentinel-1 downloader (SAFE + clipped preview)
├── visualisation.py       # False-color renderer with native-res tiling + bbox crop
├── ais_listener.py        # Multi-source AIS collector (Python)
├── ais_listener.js        # AIS collector for Node.js 22+ (no npm deps)
├── push_ais_to_github.js  # Push new ais_data/ files to GitHub via REST API
├── bandar_abbas_daily.sh  # Daily Sentinel-1 + render workflow for Bandar Abbas
├── .env.example           # Credentials template
├── requirements.txt       # pip dependencies
├── README.md              # this file
├── LICENSE                # MIT
├── data/                  # SAR outputs (created on first run)
│   ├── safe/              # Full SAFE products + rendered JPGs
│   └── previews/          # Clipped 3-band GeoTIFF previews
└── ais_data/              # AIS captures (created on first run)
    └── YYYY-MM-DD/
        └── HH.jsonl       # One line per AIS message
```

---

## Quick start

```bash
# 1. Clone and set up
git clone https://github.com/YOUR-USERNAME/hormuz-ship-tracker.git
cd hormuz-ship-tracker
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Fill in your free API keys
cp .env.example .env
# edit .env — see "Credentials" section below

# 3. Pull last week's SAR data over Hormuz
python download_sar.py --days 7

# 4. List what you've got, render the most recent scene
python visualisation.py --list
python visualisation.py <ID>          # use the ID printed by --list

# 5. In another terminal, start collecting AIS in parallel
python ais_listener.py --bbox 54.5 25.0 57.5 27.5
```

You now have rendered SAR JPGs in `data/safe/` and a growing archive of AIS positions in `ais_data/`. The next section explains each tool in detail.

---

## Credentials

All three services are free. Sign up, copy the keys into `.env`:

```dotenv
# AIS (https://aisstream.io — sign in with GitHub)
AISSTREAM_API_KEY=

# Copernicus Data Space (https://dataspace.copernicus.eu — free account)
CDSE_USER=
CDSE_PASSWORD=

# Sentinel Hub OAuth client
# CDSE Dashboard → User Settings → OAuth Clients → Create
SH_CLIENT_ID=
SH_CLIENT_SECRET=
```

| Service | Used for | Tier | Notes |
|---|---|---|---|
| aisstream.io | Live AIS WebSocket | Free, no quota | GitHub sign-in, instant API key |
| CDSE (OData) | Full Sentinel-1 SAFE products | Free, generous | Account approval is automatic |
| Sentinel Hub (CDSE) | Clipped GeoTIFF previews | Free tier: 30k processing units/month | More than enough for one bbox |

The Sentinel Hub OAuth client is separate from your CDSE login. Go to the [CDSE Dashboard](https://shapps.dataspace.copernicus.eu/dashboard/), open **User Settings → OAuth Clients**, click **Create**, and paste the client ID and secret into `.env`.

---

## `download_sar.py` — Sentinel-1 downloader

Queries the CDSE OData catalog for `S1*_IW_GRDH_1SDV_*` scenes (IW mode, GRD high-res, dual-polarization VV+VH) that intersect a bounding box in a given time window. For each match, downloads two things:

- **Full SAFE product** (`data/safe/<scene>.zip`, ~1 GB) — the complete Sentinel-1 product with both polarizations, calibration LUTs, and metadata. This is what real ship-detection pipelines need.
- **Clipped GeoTIFF preview** (`data/previews/<scene>.tif`, ~30–100 MB) — Sentinel Hub Process API renders just your bbox as a 3-band FLOAT32 raster: σ⁰ VV, σ⁰ VH, VV/VH ratio. Orthorectified, ready to view.

Both are cached: re-running over overlapping date windows skips files that already exist. A `<scene>.json` sidecar next to each output holds the catalog metadata (footprint, acquisition times, orbit) for downstream tooling.

### Usage

```bash
# Default bbox (Hormuz), last 14 days
python download_sar.py --days 14

# Explicit window
python download_sar.py --start 2026-05-01 --end 2026-05-24

# Custom bbox: Singapore Strait
python download_sar.py --days 7 --bbox 103.5 1.0 104.5 1.5

# Only scenes whose footprint *fully contains* the bbox
python download_sar.py --days 30 --bbox 55.0 25.5 56.5 27.0 --full-coverage

# Dry-run: list what's available, download nothing
python download_sar.py --days 14 --list-only

# Skip the heavy SAFE products, just grab clipped previews
python download_sar.py --days 14 --no-safe
```

### Full flag reference

| Flag | Description |
|---|---|
| `--start YYYY-MM-DD` | Window start (UTC, inclusive). Mutually exclusive with `--days`. |
| `--end YYYY-MM-DD` | Window end (UTC, exclusive). Defaults to now. |
| `--days N` | Convenience: last N days ending now. |
| `--bbox W S E N` | Four floats (lon/lat). Default: Hormuz `54.5 25.0 57.5 27.5`. |
| `--full-coverage` | Keep only scenes whose footprint fully contains the bbox. |
| `--data-dir PATH` | Where to write (default `./data`). |
| `--no-safe` | Skip full SAFE downloads. |
| `--no-preview` | Skip Sentinel Hub previews. |
| `--list-only` | List matches; don't download. |
| `-v` / `--verbose` | DEBUG-level logging (shows the OData filter). |

### Notes on bbox coverage

A Sentinel-1 IW swath is ~250 km wide and each scene covers ~150–250 km along-track. The default Hormuz bbox (~300×280 km) is larger than a single scene along-track, so `--full-coverage` against the default bbox will frequently return zero results. The script warns when the filter empties the set. For a "fully covered" workflow, shrink the bbox to ~150 km on the longest axis or rely on multiple intersecting scenes mosaiced downstream.

### Expected scene count

With Sentinel-1A + Sentinel-1C operating, Hormuz sees a Sentinel-1 pass roughly every 3 days when averaging ascending and descending orbits. About half are dual-pol VV+VH. Expect 3–8 matching scenes per 14-day window.

---

## `visualisation.py` — false-color renderer

Reads what `download_sar.py` left in `data/`, applies σ⁰ calibration (from the SAFE's calibration LUT), and writes a false-color JPG per scene.

The recipe is the standard ocean-friendly composite:

| Channel | Source | Stretch |
|---|---|---|
| **R** | σ⁰ VV (dB) | −25 to 0 dB |
| **G** | σ⁰ VH (dB) | −30 to −5 dB |
| **B** | VH / VV ratio (dB) | −10 to +5 dB |

Open water comes out dark blue (low VV/VH, high VH/VV ratio). Land is olive-yellow. **Ships are bright yellow point targets** — their metal hulls and superstructure scatter strongly in both polarizations.

### Native-resolution tiling

A real Sentinel-1 IW GRDH is ~25,000 × 16,500 pixels. Rather than downsampling to fit one JPG, the script renders at **native ~10 m resolution** and splits into tiles where each tile's largest dimension is ≤ `--max-dim` (default 8000 px). With the default, that's typically a 4 × 3 grid = 12 tiles per scene.

Tiles are saved with position info baked into the filename:

```
<scene>_falsecolor_r{row}c{col}_y{y0}-{y1}_x{x0}-{x1}.jpg
```

Plus a `<scene>_falsecolor_tiles.json` manifest with the full grid layout:

```json
{
  "scene": "S1A_IW_GRDH_1SDV_20260520T024545_..._C5BD.SAFE",
  "full_image": { "width": 25789, "height": 16734 },
  "grid": { "rows": 3, "cols": 4, "max_dim": 8000 },
  "tiles": [
    { "row": 0, "col": 0, "y_range": [0, 5578], "x_range": [0, 6448],
      "width": 6448, "height": 5578,
      "file": "S1A_..._falsecolor_r00c00_y000000-005578_x000000-006448.jpg" }
  ]
}
```

If the image fits in a single tile (when `--max-dim` ≥ the largest dimension), output is just `<scene>_falsecolor.jpg` with no manifest. The calibration LUT is sampled at original-image coordinates regardless of tile boundary — tiles seamlessly reconstruct the full image, verified to floating-point precision.

### Usage

```bash
# List every scene found in data/
python visualisation.py --list

# Render a scene by ID (or unique prefix)
python visualisation.py 163390df
python visualisation.py 1633

# Render all scenes in data/ in one call
python visualisation.py --all

# Render all scenes with a geographic crop and disk cleanup
python visualisation.py --all --crop-bbox 56.12176 26.94227 56.55762 27.24841 --trim-safe

# Force the medium-res preview path (faster, lower res)
python visualisation.py 163390df --prefer preview

# Larger tiles = fewer files, more RAM
python visualisation.py 163390df --max-dim 16000

# Crop the rendered image to a geographic bounding box (lon/lat)
python visualisation.py 163390df --crop-bbox 56.12176 26.94227 56.55762 27.24841
```

The `--crop-bbox` flag uses the scene's geolocation grid (for SAFE products) or GeoTIFF spatial tags (for previews) to compute the exact pixel region that covers the requested area. For SAFE files the σ⁰ calibration LUT is still sampled at original-image coordinates so the result is radiometrically correct. If the crop bbox does not overlap the scene, the full image is rendered with a warning.

### Full flag reference

| Flag | Description |
|---|---|
| `--list` | List scenes in `data/` and exit. |
| `--max-dim N` | Max pixel dimension per tile (default 8000). |
| `--prefer {safe,preview}` | Source preference when both exist (default `safe`). |
| `--bbox W S E N` | Reference area for `--full-coverage` filtering (default Hormuz). |
| `--full-coverage` | Only render scenes whose footprint fully contains the bbox. |
| `--all` | Render every scene found in `data/` in one call. Continues past per-scene errors and prints a batch summary. Combines with all other flags. |
| `--crop-bbox W S E N` | Crop the output image to this geographic bbox (min_lon min_lat max_lon max_lat). |
| `--trim-safe` | After rendering, delete `measurement/` entries (raw GRD TIFFs) from the SAFE zip to reclaim ~1 GB. Annotation, calibration, and manifest files are kept. See recovery note below. |
| `-v` / `--verbose` | DEBUG-level logging. |

### Disk-space management and recovery

`--trim-safe` rewrites the SAFE zip in-place, keeping only annotation, calibration, manifest, and support files (~5–20 MB) and discarding the raw GRD measurement TIFFs (~900 MB–1 GB). The operation is atomic: a `.trimming` temp file is written first, then renamed over the original. If writing fails the original is untouched.

**Automatic recovery:** if you later ask `visualisation.py` to render (or re-render) a scene whose zip has already been trimmed, the script detects the missing `measurement/` folder, fetches a fresh full SAFE from CDSE using `CDSE_USER` / `CDSE_PASSWORD` from `.env`, and then renders normally. The re-download overwrites the trimmed zip. If credentials are absent or the download fails, a clear error is logged and the script exits with code 1.

```bash
# Render + free 1 GB afterwards
python visualisation.py <ID> --trim-safe

# Later, re-render (auto-downloads if the zip was trimmed)
python visualisation.py <ID> --crop-bbox 56.12176 26.94227 56.55762 27.24841
```

### Memory usage

Peak RAM is roughly 1.5 GB per tile during processing, plus the full DN arrays held in memory (~1.6 GB for a typical IW GRDH). About 3 GB total at default settings. Bump `--max-dim 16000` for higher-res tiles (4 instead of 12 for the same scene) at ~6 GB peak.

### Why σ⁰ and not γ⁰?

You can compute γ⁰ from σ⁰ by dividing by `cos(incidence_angle)`. For Sentinel-1 IW (θ ≈ 30°–46°), this is a 0.6–1.6 dB difference — visually identical after the dB stretch the script applies. The incidence angle is in the SAFE under `annotation/s1a-iw-grd-*.xml` if you ever need true γ⁰ for quantitative work.

---

## `ais_listener.py` — multi-source AIS collector

Subscribes to one or more AIS data sources concurrently, filters every message by the bounding box, and persists matches to hourly JSONL files for later fusion with SAR.

### Sources

| Source | Free? | What it is |
|---|---|---|
| `aisstream` | ✅ | aisstream.io's global WebSocket. Server-side bbox filtering; the script's local check is the safety net. |
| `nmea-tcp` | ✅ | Connect to any NMEA 0183 AIS receiver over TCP (your own RTL-SDR + AIS-Catcher, a dAISy hardware dongle, a friend's gateway in the Gulf, a public feed). Decodes AIVDM/AIVDO sentences including multi-fragment messages. Requires `pip install pyais`. |
| `replay` | ✅ | Replay an existing JSONL capture through the same pipeline. Useful for testing without burning aisstream quota, re-processing historical data with a different bbox, or sharing reproducible test cases. |

Honest note on alternatives: **aisstream.io is the only truly-free real-time global stream available out of the box.** AISHub.net requires you to contribute your own AIS receiver feed to access theirs. National sources (Norway's Kystverket, Denmark's DMA) only cover their own waters. Commercial satellite AIS (Spire, Kpler, exactEarth) is the gold standard for blue water — and isn't free. For Hormuz specifically, that means mid-strait coverage is the main known gap of any free workflow.

### Architecture

One asyncio event loop. Each source is a `Source` subclass running as a concurrent task and pushing canonical records into a shared queue. A writer task drains the queue into hourly JSONL files. A stats task logs throughput periodically. All sources auto-reconnect with exponential backoff (1s → 60s cap). SIGINT and SIGTERM trigger graceful shutdown: drain the queue, close the current file, log final stats.

### Canonical record schema

Every message ends up in the same shape regardless of source:

```json
{
  "source": "aisstream",
  "received_at": "2026-05-24T16:08:09.123+00:00",
  "type": "PositionReport",
  "mmsi": 211223344,
  "lat": 26.0,
  "lon": 55.5,
  "ship_name": "EXAMPLE TANKER",
  "time_utc": "2026-05-24 16:08:08.412345 +0000 UTC",
  "raw": { }
}
```

The `raw` field preserves the source payload so you don't lose anything if you later want a field the canonical schema didn't promote.

### Output layout

```
ais_data/
├── 2026-05-24/
│   ├── 14.jsonl
│   ├── 15.jsonl
│   └── 16.jsonl
└── 2026-05-25/
    ├── 00.jsonl
    └── ...
```

One file per UTC hour, append-only, line-flushed — safe to `tail -f` or rsync to another machine while running.

### Usage

```bash
# Default (aisstream alone) over Hormuz
python ais_listener.py --bbox 54.5 25.0 57.5 27.5

# Combine aisstream with a local NMEA receiver
python ais_listener.py --bbox 54.5 25.0 57.5 27.5 \
    --sources aisstream nmea-tcp \
    --nmea-tcp 192.168.1.42:4002

# Multiple NMEA endpoints
python ais_listener.py --bbox 54.5 25.0 57.5 27.5 \
    --sources nmea-tcp \
    --nmea-tcp host-a:4002 --nmea-tcp host-b:10110

# Replay a previous capture (no network)
python ais_listener.py --bbox 54.5 25.0 57.5 27.5 \
    --sources replay --replay-file ais_data/2026-05-23/14.jsonl

# Run detached
nohup python ais_listener.py --bbox 54.5 25.0 57.5 27.5 > ais.log 2>&1 &
tail -f ais.log
```

### Full flag reference

| Flag | Description |
|---|---|
| `--bbox W S E N` | **Required.** Four floats (lon/lat). |
| `--sources {aisstream,nmea-tcp,replay} ...` | Which sources to run (default `aisstream`). |
| `--nmea-tcp HOST:PORT` | NMEA-TCP endpoint(s). Repeat for multiple receivers. |
| `--replay-file PATH` | JSONL file to replay. |
| `--replay-delay SEC` | Sleep between replayed messages (default 0). |
| `--output-dir PATH` | Where to write hourly JSONL files (default `./ais_data`). |
| `--stats-interval SEC` | Seconds between stats log lines (default 60). |
| `-v` / `--verbose` | DEBUG-level logging. |

### Adding a fourth source

Subclass `Source`, implement `async def run(self, bbox, queue, stats)`, and call `await self._emit(raw_payload_dict, bbox, queue, stats)` for each message. Add a branch in `normalize()` to map your payload to the canonical schema, then add a `choices` entry on `--sources` and a constructor in `build_sources()`. The framework handles bbox filtering, queue plumbing, writing, stats, and shutdown.

---

## `bandar_abbas_daily.sh` — Port of Bandar Abbas daily imagery

A convenience wrapper that downloads the last N days of Sentinel-1 scenes that **fully cover the Port of Bandar Abbas** and renders each as a false-color crop of the port area.

**Port of Bandar Abbas bbox:** `W=56.12176  S=26.94227  E=56.55762  N=27.24841`
Covers Shahid Rajaee Container Port (west), the old fishing/commercial port (east), and the anchorage zone south of the breakwater.

```bash
# Default: last 7 days, preview-only (fast, no ~1 GB SAFE downloads)
bash bandar_abbas_daily.sh

# Last 14 days
bash bandar_abbas_daily.sh --days 14

# Also pull full SAFE products (~1 GB/scene, needed for native-res rendering)
bash bandar_abbas_daily.sh --with-safe
```

The script:
1. Sources `.env` for credentials
2. Runs `download_sar.py --full-coverage` with the Bandar Abbas bbox
3. Renders every scene found in `data/` with `visualisation.py --crop-bbox --trim-safe`
4. Prints a summary of output files

`--trim-safe` is applied by default: after tiling each SAFE product the raw measurement TIFFs (~1 GB) are removed, keeping the zip at ~10–20 MB. If a zip was trimmed by a previous run and tiles need to be regenerated, `visualisation.py` automatically re-downloads the full product before rendering.

Requires `CDSE_USER`, `CDSE_PASSWORD`, `SH_CLIENT_ID`, and `SH_CLIENT_SECRET` in `.env`. Sentinel Hub previews (~30–100 MB, ~130 m/px) are downloaded by default. Full SAFE products (~1 GB each, ~10 m/px) require `--with-safe`.

---

## Fusion workflow

The whole point of the toolkit is correlating SAR detections with AIS positions.

```bash
# Terminal 1: collect AIS continuously over the AOI
python ais_listener.py --bbox 54.5 25.0 57.5 27.5

# Terminal 2: every few days, pull new SAR scenes and render them
python download_sar.py --days 7
python visualisation.py --list
python visualisation.py <ID>
```

For each rendered scene, look up the acquisition timestamp in `data/safe/<scene>.json` (`ContentDate.Start`) and pull AIS records from the matching `ais_data/YYYY-MM-DD/HH.jsonl` files within ±5 minutes. Project AIS lat/lon onto the JPG using the tile manifest's pixel ranges, and:

- **SAR detection with matching AIS** → identified ship (you can pull MMSI, name, type)
- **SAR detection without matching AIS** → dark vessel, worth investigating

That second category is the operational signal. It's where this toolkit becomes useful versus just a ship-counting exercise.

---

## Caveats and honesty

**Coverage gaps.** Sentinel-1 IW revisits Hormuz every ~3 days when combining ascending and descending orbits. Terrestrial AIS coverage from aisstream.io is patchy in the middle of the strait. The operationally interesting small craft — wooden dhows, fiberglass IRGC fast boats — have weak radar cross-sections and routinely don't broadcast AIS at all. Free tooling has limits; this is a starting point, not a complete picture.

**SAR resolution.** Sentinel-1 IW GRDH is ~20 m resolution. You can detect a 30 m vessel reliably, estimate its length to within ~20 m, but you cannot classify ship type from pixels alone at this resolution. Sub-meter classification needs commercial SAR (Umbra, Capella, ICEYE) — those have free *sample* datasets but no systematic coverage.

**Sigma0 vs gamma0.** The script uses σ⁰ ellipsoid (no DEM-based terrain correction). For maritime AOIs this is fine. For coastal or terrain-affected scenes you'd want full radiometric terrain correction via SNAP.

**Not for navigation or operational use.** This is an OSINT / research tool. The data has gaps, the calibration is approximate, the AIS feed is incomplete. Do not use it to decide whether to sail through a strait.

---

## Dependencies

```text
requests
python-dotenv
tqdm
numpy
tifffile
imagecodecs        # ZSTD decode for Sentinel Hub TIFFs
Pillow
websockets
pyais              # only needed if you use --sources nmea-tcp
```

A `requirements.txt` is included.

---

## License

[MIT](LICENSE) — do what you want, no warranty, attribution appreciated.

---

## Acknowledgments

This wouldn't exist without:

- **ESA Copernicus & Sentinel-1** for free, open, high-quality SAR data
- **Sentinel Hub on CDSE** for the Process API that makes clipped previews trivial
- **aisstream.io** for keeping a global AIS WebSocket genuinely free
- The **pyais** maintainers for the cleanest NMEA decoder in Python
- The broader OSINT community — Bellingcat, TankerTrackers, Global Fishing Watch — for showing what's possible with public data and pointing the way

---

## Contributing

Issues and PRs welcome. Particularly interested in: a fusion script that does the SAR-vs-AIS comparison automatically, a Folium/QGIS overlay generator, additional AIS sources (regional public feeds, Global Fishing Watch API integration), and CFAR-based ship detection on the rendered tiles.