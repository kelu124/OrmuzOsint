"""
sentinel.py — Copernicus Data Space Ecosystem (CDSE) integration
=================================================================
Handles catalog search, authentication, quicklook downloads, and
Sentinel Hub Process API requests for Sentinel-1 / Sentinel-2 imagery.

All results are cached:
  • API tokens  → st.session_state (short TTL, auto-refreshed)
  • Catalog searches → @st.cache_data (TTL 30 min)
  • Quicklook PNGs  → disk cache  (./cache/quicklooks/)
  • Processed images → disk cache  (./cache/processed/)

Free CDSE account: https://dataspace.copernicus.eu
Sentinel Hub dashboard (for OAuth client): https://shapps.dataspace.copernicus.eu/dashboard/
"""

import hashlib
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
import streamlit as st

# ─────────────────────────── PATHS ──────────────────────────────

# Cache lives inside the project folder (next to app.py)
_PROJECT_DIR = Path(__file__).resolve().parent
CACHE_ROOT = _PROJECT_DIR / "cache"
QUICKLOOK_DIR = CACHE_ROOT / "quicklooks"
PROCESSED_DIR = CACHE_ROOT / "processed"
CATALOG_DIR = CACHE_ROOT / "catalog"

for _d in (QUICKLOOK_DIR, PROCESSED_DIR, CATALOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# ─────────────────────────── AUTH ───────────────────────────────

KEYCLOAK_URL = (
    "https://identity.dataspace.copernicus.eu"
    "/auth/realms/CDSE/protocol/openid-connect/token"
)

# Sentinel Hub token endpoint (for Process API)
SH_TOKEN_URL = (
    "https://identity.dataspace.copernicus.eu"
    "/auth/realms/CDSE/protocol/openid-connect/token"
)


def _get_cdse_token(username: str, password: str) -> dict:
    """Get a fresh Keycloak token for catalog / download APIs."""
    resp = requests.post(
        KEYCLOAK_URL,
        data={
            "client_id": "cdse-public",
            "username": username,
            "password": password,
            "grant_type": "password",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    data["_obtained_at"] = time.time()
    return data


def get_cdse_token(username: str, password: str) -> str:
    """Return a valid access token, refreshing if needed.  Cached in session_state."""
    key = "cdse_token_data"
    tok = st.session_state.get(key)
    if tok:
        age = time.time() - tok.get("_obtained_at", 0)
        if age < tok.get("expires_in", 300) - 30:
            return tok["access_token"]
    tok = _get_cdse_token(username, password)
    st.session_state[key] = tok
    return tok["access_token"]


def _get_sh_token(client_id: str, client_secret: str) -> dict:
    """Get a Sentinel Hub OAuth token (for Process API)."""
    resp = requests.post(
        SH_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    data["_obtained_at"] = time.time()
    return data


def get_sh_token(client_id: str, client_secret: str) -> str:
    """Return a valid Sentinel Hub access token, refreshing if needed."""
    key = "sh_token_data"
    tok = st.session_state.get(key)
    if tok:
        age = time.time() - tok.get("_obtained_at", 0)
        if age < tok.get("expires_in", 300) - 30:
            return tok["access_token"]
    tok = _get_sh_token(client_id, client_secret)
    st.session_state[key] = tok
    return tok["access_token"]


# ──────────────────── CATALOG SEARCH (OData) ────────────────────

CATALOG_BASE = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"


def _cache_key_catalog(collection, bbox, start, end, cloud_max, limit):
    raw = f"{collection}|{bbox}|{start}|{end}|{cloud_max}|{limit}"
    return hashlib.md5(raw.encode()).hexdigest()


@st.cache_data(ttl=1800, show_spinner="Searching CDSE catalog…")
def search_catalog(
    collection: str,
    bbox: tuple,
    start_date: str,
    end_date: str,
    cloud_cover_max: int = 30,
    limit: int = 20,
) -> list[dict]:
    """
    Search the CDSE OData catalog.
    Returns a list of product dicts with keys:
      Id, Name, ContentDate, CloudCover, GeoFootprint, etc.

    Results are cached by @st.cache_data for 30 min AND written to disk.
    """
    cache_file = CATALOG_DIR / f"{_cache_key_catalog(collection, bbox, start_date, end_date, cloud_cover_max, limit)}.json"
    if cache_file.exists():
        age_hours = (time.time() - cache_file.stat().st_mtime) / 3600
        if age_hours < 6:  # disk cache valid for 6 hours
            return json.loads(cache_file.read_text())

    sw_lat, sw_lon, ne_lat, ne_lon = bbox
    wkt = (
        f"POLYGON(({sw_lon} {sw_lat},{ne_lon} {sw_lat},"
        f"{ne_lon} {ne_lat},{sw_lon} {ne_lat},{sw_lon} {sw_lat}))"
    )

    filters = [
        f"Collection/Name eq '{collection}'",
        f"OData.CSC.Intersects(area=geography'SRID=4326;{wkt}')",
        f"ContentDate/Start gt {start_date}T00:00:00.000Z",
        f"ContentDate/Start lt {end_date}T23:59:59.999Z",
    ]
    if "SENTINEL-2" in collection.upper() and cloud_cover_max < 100:
        filters.append(
            f"Attributes/OData.CSC.DoubleAttribute/any(att:att/Name eq 'cloudCover' "
            f"and att/OData.CSC.DoubleAttribute/Value lt {cloud_cover_max})"
        )

    url = (
        f"{CATALOG_BASE}?$filter={' and '.join(filters)}"
        f"&$top={limit}&$orderby=ContentDate/Start desc"
    )

    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    products = resp.json().get("value", [])

    results = []
    for p in products:
        content_date = p.get("ContentDate", {})
        # Extract cloud cover from Attributes if present
        cloud_cover = None
        for attr in p.get("Attributes", []):
            if attr.get("Name") == "cloudCover":
                cloud_cover = attr.get("Value")
                break

        results.append({
            "id": p["Id"],
            "name": p["Name"],
            "start": content_date.get("Start", ""),
            "end": content_date.get("End", ""),
            "cloud_cover": cloud_cover,
            "size_mb": round(p.get("ContentLength", 0) / 1e6, 1),
            "online": p.get("Online", True),
            "footprint": p.get("GeoFootprint", {}),
        })

    cache_file.write_text(json.dumps(results, default=str))
    return results


# ─────────────────── QUICKLOOK DOWNLOAD ─────────────────────────

def _quicklook_path(product_id: str) -> Path:
    return QUICKLOOK_DIR / f"{product_id}.jpg"


def download_quicklook(product_id: str, token: str) -> Optional[Path]:
    """
    Download the quicklook thumbnail for a product.
    Cached on disk — never re-downloads if the file already exists.
    """
    path = _quicklook_path(product_id)
    if path.exists() and path.stat().st_size > 0:
        return path

    url = f"{CATALOG_BASE}({product_id})/Nodes"
    try:
        # Try the quicklook endpoint
        ql_url = (
            f"https://zipper.dataspace.copernicus.eu/odata/v1"
            f"/Products({product_id})/Quicklook"
        )
        resp = requests.get(
            ql_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
            stream=True,
        )
        if resp.status_code == 200 and len(resp.content) > 1000:
            path.write_bytes(resp.content)
            return path
    except Exception:
        pass

    return None


# ─────────── SENTINEL HUB PROCESS API (rendered imagery) ───────

SH_PROCESS_URL = "https://sh.dataspace.copernicus.eu/api/v1/process"

# Evalscripts for rendering
EVALSCRIPTS = {
    "sentinel-2-true-color": """
//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B04", "B03", "B02"], units: "DN" }],
    output: { bands: 3, sampleType: "AUTO" }
  };
}
function evaluatePixel(sample) {
  return [3.5 * sample.B04 / 10000,
          3.5 * sample.B03 / 10000,
          3.5 * sample.B02 / 10000];
}
""",
    "sentinel-2-enhanced": """
//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B04", "B03", "B02", "B08"], units: "DN" }],
    output: { bands: 3, sampleType: "AUTO" }
  };
}
function evaluatePixel(s) {
  // Enhance water/land contrast for ship visibility
  let r = s.B04 / 10000, g = s.B03 / 10000, b = s.B02 / 10000;
  let ndwi = (g - s.B08 / 10000) / (g + s.B08 / 10000 + 0.001);
  let gain = ndwi > 0.1 ? 2.0 : 4.0;  // boost land, keep water darker
  return [gain * r, gain * g, gain * b];
}
""",
    "sentinel-1-vv": """
//VERSION=3
function setup() {
  return {
    input: [{ bands: ["VV"], units: "LINEAR_POWER" }],
    output: { bands: 1, sampleType: "UINT8" }
  };
}
function evaluatePixel(sample) {
  // Log-scale for better ship vs. water contrast
  var val = Math.log10(sample.VV + 0.0001);
  val = (val + 4) / 4;  // normalize roughly to 0-1
  return [255 * Math.max(0, Math.min(1, val))];
}
""",
    "sentinel-1-ship-enhance": """
//VERSION=3
function setup() {
  return {
    input: [{ bands: ["VV", "VH"], units: "LINEAR_POWER" }],
    output: { bands: 3, sampleType: "UINT8" }
  };
}
function evaluatePixel(s) {
  // Ships appear as bright VV spots; dual-pol composite
  let vv = Math.log10(s.VV + 0.0001);
  let vh = Math.log10(s.VH + 0.0001);
  vv = Math.max(0, Math.min(1, (vv + 3.5) / 3));
  vh = Math.max(0, Math.min(1, (vh + 4) / 3.5));
  // R=VV, G=VH, B=VV/VH ratio → ships pop in red/yellow
  let ratio = Math.max(0, Math.min(1, (vv - vh + 0.5)));
  return [255 * vv, 255 * vh, 255 * ratio];
}
""",
}


def _processed_cache_key(
    collection, bbox, date_from, date_to, evalscript_name, width, height, mosaicking
):
    raw = f"{collection}|{bbox}|{date_from}|{date_to}|{evalscript_name}|{width}|{height}|{mosaicking}"
    return hashlib.md5(raw.encode()).hexdigest()


def fetch_processed_image(
    sh_token: str,
    collection: str,
    bbox: tuple,
    date_from: str,
    date_to: str,
    evalscript_name: str = "sentinel-2-true-color",
    width: int = 1024,
    height: int = 1024,
    mosaicking: str = "leastCC",
    max_cache_age_hours: float = 24,
) -> Optional[Path]:
    """
    Fetch a rendered image from Sentinel Hub Process API.
    Cached on disk — skips the API if a recent-enough file exists.

    Args:
        sh_token:        Sentinel Hub OAuth token
        collection:      e.g. "sentinel-2-l2a", "sentinel-1-grd"
        bbox:            (sw_lat, sw_lon, ne_lat, ne_lon)
        date_from/to:    ISO date strings  "2026-01-01"
        evalscript_name: key into EVALSCRIPTS dict
        width/height:    output image size in pixels
        mosaicking:      "leastCC" | "mostRecent" | "leastRecent"
        max_cache_age_hours: skip API if cached file is newer than this
    """
    cache_hash = _processed_cache_key(
        collection, bbox, date_from, date_to, evalscript_name, width, height, mosaicking
    )
    cache_path = PROCESSED_DIR / f"{cache_hash}.png"

    # Disk cache check
    if cache_path.exists() and cache_path.stat().st_size > 0:
        age_hours = (time.time() - cache_path.stat().st_mtime) / 3600
        if age_hours < max_cache_age_hours:
            return cache_path

    evalscript = EVALSCRIPTS.get(evalscript_name)
    if not evalscript:
        return None

    sw_lat, sw_lon, ne_lat, ne_lon = bbox

    # Map collection names to Sentinel Hub data collection identifiers
    sh_collection_map = {
        "SENTINEL-2": "sentinel-2-l2a",
        "sentinel-2-l2a": "sentinel-2-l2a",
        "sentinel-2-l1c": "sentinel-2-l1c",
        "SENTINEL-1": "sentinel-1-grd",
        "sentinel-1-grd": "sentinel-1-grd",
    }
    sh_type = sh_collection_map.get(collection, collection)

    payload = {
        "input": {
            "bounds": {
                "bbox": [sw_lon, sw_lat, ne_lon, ne_lat],
                "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/4326"},
            },
            "data": [
                {
                    "type": sh_type,
                    "dataFilter": {
                        "timeRange": {
                            "from": f"{date_from}T00:00:00Z",
                            "to": f"{date_to}T23:59:59Z",
                        },
                        "mosaickingOrder": mosaicking,
                    },
                }
            ],
        },
        "output": {
            "width": width,
            "height": height,
            "responses": [{"identifier": "default", "format": {"type": "image/png"}}],
        },
        "evalscript": evalscript,
    }

    # Add cloud cover filter for S2
    if "sentinel-2" in sh_type:
        payload["input"]["data"][0]["dataFilter"]["maxCloudCoverage"] = 40

    try:
        resp = requests.post(
            SH_PROCESS_URL,
            headers={
                "Authorization": f"Bearer {sh_token}",
                "Content-Type": "application/json",
                "Accept": "image/png",
            },
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()

        if resp.headers.get("Content-Type", "").startswith("image"):
            cache_path.write_bytes(resp.content)
            return cache_path
        else:
            return None

    except requests.exceptions.HTTPError as e:
        st.warning(f"Sentinel Hub API error: {e.response.status_code} — {e.response.text[:300]}")
        return None
    except Exception as e:
        st.warning(f"Sentinel Hub request failed: {e}")
        return None


# ──────────────────── CACHE MANAGEMENT ──────────────────────────

def get_cache_stats() -> dict:
    """Return cache size and file counts for the UI."""
    stats = {}
    for name, path in [("quicklooks", QUICKLOOK_DIR), ("processed", PROCESSED_DIR), ("catalog", CATALOG_DIR)]:
        files = list(path.glob("*"))
        total_mb = sum(f.stat().st_size for f in files) / 1e6
        stats[name] = {"files": len(files), "size_mb": round(total_mb, 1)}
    return stats


def clear_cache(which: str = "all"):
    """Clear disk cache.  which = 'all' | 'quicklooks' | 'processed' | 'catalog'."""
    dirs = {
        "quicklooks": QUICKLOOK_DIR,
        "processed": PROCESSED_DIR,
        "catalog": CATALOG_DIR,
    }
    targets = dirs.values() if which == "all" else [dirs[which]]
    for d in targets:
        for f in d.glob("*"):
            f.unlink(missing_ok=True)
