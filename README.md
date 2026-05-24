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
                                      ▼
                ┌─────────────────────┴───────────────────────┐
                │                                             │
   ┌────────────▼─────────────┐                ┌──────────────▼───────────┐
   │        ais2.py           │                │     ais_listener.py      │
   │  • aisstream only        │                │  • Multi-source async    │
   │  • Streams text/JSONL    │                │  • Bbox filter           │
   │  • Static-data enriched  │                │  • Hourly JSONL output   │
   └────────────┬─────────────┘                └──────────────┬───────────┘
                │                                             │
                ▼                                             ▼
       stdout / your-file.jsonl                    ais_data/YYYY-MM-DD/HH.jsonl
```

The two halves run independently and produce timestamped artifacts that can be cross-referenced offline: for every SAR scene's acquisition window, query the corresponding AIS data and compare positions.

---

## Repository layout

```
.
├── download_sar.py        # Sentinel-1 downloader (SAFE + clipped preview)
├── visualisation.py       # False-color renderer with native-res tiling
├── ais2.py                # Minimal AIS streamer (text/JSONL to stdout)
├── ais_listener.py        # Multi-source AIS collector (background daemon)
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

# 5a. Quick look: stream AIS to your terminal in real time
python ais2.py --bbox 54.5 25.0 57.5 27.5

# 5b. Or run a long-running collector that writes hourly JSONL files
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

# Force the medium-res preview path (faster, lower res)
python visualisation.py 163390df --prefer preview

# Larger tiles = fewer files, more RAM
python visualisation.py 163390df --max-dim 16000
```

### Full flag reference

| Flag | Description |
|---|---|
| `--list` | List scenes in `data/` and exit. |
| `--max-dim N` | Max pixel dimension per tile (default 8000). |
| `--prefer {safe,preview}` | Source preference when both exist (default `safe`). |
| `-v` / `--verbose` | DEBUG-level logging. |

### Memory usage

Peak RAM is roughly 1.5 GB per tile during processing, plus the full DN arrays held in memory (~1.6 GB for a typical IW GRDH). About 3 GB total at default settings. Bump `--max-dim 16000` for higher-res tiles (4 instead of 12 for the same scene) at ~6 GB peak.

### Why σ⁰ and not γ⁰?

You can compute γ⁰ from σ⁰ by dividing by `cos(incidence_angle)`. For Sentinel-1 IW (θ ≈ 30°–46°), this is a 0.6–1.6 dB difference — visually identical after the dB stretch the script applies. The incidence angle is in the SAFE under `annotation/s1a-iw-grd-*.xml` if you ever need true γ⁰ for quantitative work.

---

## AIS collection: two tools, pick what fits

There are two ways to bring AIS into the pipeline, and which one you reach for depends on how much ceremony you want.

| | `ais2.py` | `ais_listener.py` |
|---|---|---|
| Mental model | Watch the firehose | Run a daemon |
| Output | stdout (text or JSONL) — pipe it, tee it, redirect it | Hourly JSONL files in `ais_data/YYYY-MM-DD/HH.jsonl` |
| Sources | aisstream.io only | aisstream.io + NMEA-TCP + replay (extensible) |
| Static-data enrichment | ✅ name, type, destination, dimensions inlined into every position | ❌ raw payloads only |
| Per-vessel throttle | ✅ configurable (`--throttle SEC`) | ❌ writes every message in-bbox |
| Reconnect on failure | ✅ | ✅ |
| Best for | Interactive monitoring, quick captures, debugging, ad-hoc filtering with `jq` | Multi-day unattended collection, fusion with SAR scenes, multi-receiver setups |
| Dependencies | `websockets`, `python-dotenv` | + `pyais` (if using NMEA-TCP) |

If you don't know which to use, start with `ais2.py`. It's faster to understand, leaves no files behind unless you redirect, and gives you nicely enriched per-vessel records straight to your terminal. Move to `ais_listener.py` when you need durable archiving, multiple receivers, or you want the pipeline to survive your laptop closing.

---

## `ais2.py` — minimal text streamer

One source (aisstream.io), one output (stdout). Streams enriched AIS position records as they arrive, throttled to one line per vessel per N seconds so the terminal stays readable.

**The trick that makes it useful:** it subscribes to both `PositionReport` and `ShipStaticData` and keeps a per-MMSI cache of static info in memory. The first time a ship's static data arrives, the cache is populated; every position from that ship afterward is emitted with name, type, destination, length, width, and draught attached. You get the enrichment of a tracking platform without running a database.

### Output formats

Human-readable (default):

```
16:08:09  MMSI 211223344  EXAMPLE TANKER        ( 26.0421,   55.5023)  sog= 12.3kn  cog= 87.0°  → DUBAI
16:08:14  MMSI 538001122  ALSHAMS               ( 26.1108,   55.4901)  sog=  9.8kn  cog=265.0°  → JEBEL ALI
```

JSONL (`--jsonl`) — one record per line, ready for `jq` / pandas / DuckDB:

```json
{"mmsi": 211223344, "time_utc": "2026-05-24T16:08:09", "received_at": "2026-05-24T16:08:09+00:00", "lat": 26.0421, "lon": 55.5023, "sog": 12.3, "cog": 87.0, "heading": 88, "ship_name": "EXAMPLE TANKER", "ship_type": 80, "destination": "DUBAI", "draught": 11.5, "length": 200, "width": 32}
```

Logs (connection status, errors, reconnects) go to **stderr**, so stdout stays a clean data stream that survives pipes and redirects.

### Usage

```bash
# Default Hormuz bbox, human-readable to terminal
python ais2.py

# Wider Persian Gulf + Gulf of Oman — much better coverage in practice
python ais2.py --bbox 48.0 22.0 60.0 30.5

# Capture to a JSONL file while still watching it live in the terminal
python ais2.py --bbox 48.0 22.0 60.0 30.5 --jsonl 2> stream.err | tee stream.jsonl

# Same thing detached from the terminal (survives logout)
nohup python ais2.py --bbox 48.0 22.0 60.0 30.5 --jsonl 2> stream.err | tee stream.jsonl &
disown

# No throttle — every position aisstream sends (very chatty)
python ais2.py --throttle 0

# 5-minute throttle — quieter, still useful for slow-moving vessels
python ais2.py --bbox 48.0 22.0 60.0 30.5 --throttle 300

# Live filter to just Hormuz transit with jq while watching the whole Gulf
python ais2.py --bbox 48.0 22.0 60.0 30.5 --jsonl \
    | jq -c 'select(.lon >= 54.5 and .lon <= 57.5 and .lat >= 25.0 and .lat <= 27.5)'

# Also surface static-data updates (useful for catching new ships entering the AOI)
python ais2.py --include-static
```

### Full flag reference

| Flag | Description |
|---|---|
| `--bbox W S E N` | Four floats (lon/lat). Default: Hormuz `54.5 25.0 57.5 27.5`. |
| `--throttle SEC` | Per-MMSI minimum interval between emitted positions (default 120). `0` = no throttle. |
| `--jsonl` | Emit one JSON record per line instead of human-readable text. |
| `--include-static` | Also print a line whenever ShipStaticData arrives. |
| `-v` / `--verbose` | DEBUG-level logging on stderr. |

### When traffic looks sparse

The terrestrial network behind aisstream.io has uneven coverage. The **mid-strait Hormuz bbox** specifically tends to be quiet because there are few volunteer receivers with line-of-sight to the middle of the strait. If you see no records after a minute or two:

```bash
# Sanity-check the connection works at all
python ais2.py --bbox -180 -90 180 90    # global firehose — instant traffic

# Use a wider Gulf bbox instead — gives you coverage from Dubai, Bandar Abbas,
# Muscat, Fujairah, Doha, Kuwait coastal receivers
python ais2.py --bbox 48.0 22.0 60.0 30.5
```

The wider Gulf bbox is what works in practice for OSINT-style monitoring of Hormuz traffic — you see tankers approaching the strait from either side hours before they enter it, which is often more analytically interesting than the strait itself.

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

## Fusion workflow

The whole point of the toolkit is correlating SAR detections with AIS positions.

```bash
# Terminal 1: collect AIS continuously over the AOI
# (Option A — long-running daemon writing hourly files for later analysis)
python ais_listener.py --bbox 54.5 25.0 57.5 27.5

# (Option B — lightweight, capture to one file while watching live)
python ais2.py --bbox 48.0 22.0 60.0 30.5 --jsonl 2> stream.err | tee stream.jsonl

# Terminal 2: every few days, pull new SAR scenes and render them
python download_sar.py --days 7
python visualisation.py --list
python visualisation.py <ID>
```

For each rendered scene, look up the acquisition timestamp in `data/safe/<scene>.json` (`ContentDate.Start`) and pull AIS records from the matching JSONL — either `ais_data/YYYY-MM-DD/HH.jsonl` (from `ais_listener.py`) or your captured `stream.jsonl` (from `ais2.py`) — within ±5 minutes. Project AIS lat/lon onto the JPG using the tile manifest's pixel ranges, and:

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
imagecodecs        # ZSTD decode for Sentinel Hub TIFFs (visualisation.py)
Pillow
websockets         # ais2.py and ais_listener.py
pyais              # only for ais_listener.py with --sources nmea-tcp
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