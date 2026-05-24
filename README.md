# 🚢 Strait of Hormuz Ship Tracker + Satellite Monitor

Real-time AIS ship tracking **and** Sentinel satellite imagery over the Strait of Hormuz.

## Setup

### 1. Install

```bash
pip install -r requirements.txt
```

### 2. Configure credentials

```bash
cp .env.example .env
# Edit .env with your keys
```

All credentials are loaded from `.env` automatically. The sidebar fields are pre-filled from env vars — you can still override them in the UI.

### 3. Run

```bash
streamlit run app.py
```

### API keys (all free)

| Service | Env var | Get it |
|---------|---------|--------|
| **AISStream** | `AISSTREAM_API_KEY` | [aisstream.io](https://aisstream.io) (GitHub login) |
| **Copernicus (CDSE)** | `CDSE_USER` / `CDSE_PASSWORD` | [dataspace.copernicus.eu](https://dataspace.copernicus.eu) |
| **Sentinel Hub** | `SH_CLIENT_ID` / `SH_CLIENT_SECRET` | CDSE Dashboard → OAuth Clients |

Each feature works independently — AIS works without CDSE, search works without Sentinel Hub, etc.

## Features

### 📡 Live AIS Tracker (Tab 1)
- WebSocket connection to aisstream.io with Hormuz bounding box
- Ships classified as **Waiting/Anchored** (red) or **Transiting** (green)
- Interactive pydeck map with satellite basemap, shipping lane overlays, heading arrows
- Filterable data tables + CSV export

### 🛰️ Satellite Imagery (Tab 2)
- **Catalog search** — find Sentinel-1 (SAR) and Sentinel-2 (optical) scenes
- **Quicklook browser** — cached thumbnail previews
- **Rendered imagery** — Sentinel Hub Process API with ship-optimized evalscripts
- **Map overlay** — drape rendered imagery on the interactive map

## Caching

All caches live in `./cache/` inside the project folder (git-ignored).

| Layer | What | TTL | Path |
|-------|------|-----|------|
| `@st.cache_data` | Catalog search results | 30 min | Streamlit memory |
| Disk | Search JSON responses | 6 hours | `./cache/catalog/` |
| Disk | Quicklook thumbnails | Permanent | `./cache/quicklooks/` |
| Disk | Rendered PNGs | 24 hours | `./cache/processed/` |
| Session | OAuth tokens | ~5 min (auto-refresh) | `st.session_state` |

Cache stats and clear buttons are in the Satellite Imagery tab footer.

## File structure

```
hormuz_tracker/
├── app.py            # Main Streamlit app (AIS + UI)
├── sentinel.py       # CDSE API module (search, auth, Process API, caching)
├── .env.example      # Credential template — copy to .env
├── .env              # Your credentials (git-ignored)
├── .gitignore
├── requirements.txt
├── cache/            # Auto-created, git-ignored
│   ├── catalog/
│   ├── quicklooks/
│   └── processed/
└── README.md
```
