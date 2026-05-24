#!/usr/bin/env python3
"""
Download Sentinel-1 IW GRD (VV+VH) SAR data over the Strait of Hormuz.

Pulls two things per scene:
  1. The full SAFE product zip (~1 GB) via the CDSE OData API — for SNAP /
     real ship-detection pipelines.
  2. A clipped GeoTIFF preview over the bbox via the Sentinel Hub Process API
     — 3 bands (VV, VH, VV/VH), sigma0 linear power, FLOAT32 — for quick look.

Both are cached on disk: re-runs over overlapping date ranges skip files
already present.

Requirements:
    pip install requests python-dotenv tqdm

Usage:
    python download_sar.py --start 2026-05-01 --end 2026-05-24
    python download_sar.py --days 14
    python download_sar.py --start 2026-05-01 --end 2026-05-24 --no-safe
    python download_sar.py --start 2026-05-01 --end 2026-05-24 --list-only
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from tqdm import tqdm

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Default bbox: Strait of Hormuz [minLon, minLat, maxLon, maxLat].
# Override with --bbox W S E N on the command line.
DEFAULT_BBOX = (54.5, 25.0, 57.5, 27.5)

CDSE_TOKEN_URL = (
    "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/"
    "protocol/openid-connect/token"
)
CDSE_CATALOG_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
CDSE_DOWNLOAD_URL = (
    "https://download.dataspace.copernicus.eu/odata/v1/Products({pid})/$value"
)
SH_PROCESS_URL = "https://sh.dataspace.copernicus.eu/api/v1/process"

# Preview output size (Sentinel Hub free tier caps at 2500x2500 per request).
# 2500x2000 over the Hormuz bbox ≈ 130 m / pixel — fine for overview;
# use the full SAFE for proper detection.
PREVIEW_WIDTH = 2500
PREVIEW_HEIGHT = 2000

# Sigma0 linear power, VV / VH / ratio.
EVALSCRIPT = """//VERSION=3
function setup() {
  return {
    input: [{ bands: ["VV", "VH"], units: "LINEAR_POWER" }],
    output: { bands: 3, sampleType: "FLOAT32" },
    mosaicking: "ORBIT"
  };
}
function evaluatePixel(sample) {
  const vh = Math.max(sample.VH, 1e-7);
  return [sample.VV, sample.VH, sample.VV / vh];
}
"""

log = logging.getLogger("hormuz-sar")

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def cdse_password_token(user: str, password: str) -> str:
    """Access token via password grant — used to download SAFE products."""
    r = requests.post(
        CDSE_TOKEN_URL,
        data={
            "client_id": "cdse-public",
            "grant_type": "password",
            "username": user,
            "password": password,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def sh_client_credentials_token(client_id: str, client_secret: str) -> str:
    """Access token via client_credentials — used for Sentinel Hub Process API."""
    r = requests.post(
        CDSE_TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


# ---------------------------------------------------------------------------
# Catalog search
# ---------------------------------------------------------------------------


def _bbox_to_polygon_wkt(bbox: tuple[float, float, float, float]) -> str:
    minx, miny, maxx, maxy = bbox
    return (
        f"POLYGON(({minx} {miny},{maxx} {miny},"
        f"{maxx} {maxy},{minx} {maxy},{minx} {miny}))"
    )


def _ring_from_geofootprint(geofootprint: dict) -> list[tuple[float, float]] | None:
    """Outer ring of a GeoJSON Polygon / MultiPolygon → list of (lon, lat)."""
    if not geofootprint or "type" not in geofootprint:
        return None
    t = geofootprint["type"]
    coords = geofootprint.get("coordinates")
    if not coords:
        return None
    if t == "Polygon":
        ring = coords[0]
    elif t == "MultiPolygon":
        ring = coords[0][0]
    else:
        return None
    return [(float(p[0]), float(p[1])) for p in ring]


def _point_in_polygon(x: float, y: float, ring: list[tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon test."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-30) + xi
        ):
            inside = not inside
        j = i
    return inside


def _bbox_fully_within(
    bbox: tuple[float, float, float, float], ring: list[tuple[float, float]]
) -> bool:
    """All 4 bbox corners inside the polygon ring.

    For convex-ish footprints (which Sentinel-1 IW scenes always are — they're
    quasi-trapezoidal), this implies the whole bbox is covered.
    """
    minx, miny, maxx, maxy = bbox
    corners = [(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy)]
    return all(_point_in_polygon(x, y, ring) for x, y in corners)


def search_products(
    start: datetime, end: datetime, bbox: tuple[float, float, float, float]
) -> list[dict]:
    """Query CDSE OData for Sentinel-1 IW GRDH dual-pol (VV+VH) scenes."""
    polygon = _bbox_to_polygon_wkt(bbox)
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"

    # 1SDV = IW + dual-polarization VV+VH. GRDH = high-res ground range detected.
    filt = (
        "Collection/Name eq 'SENTINEL-1'"
        " and contains(Name,'_IW_GRDH_1SDV_')"
        f" and ContentDate/Start gt {start.strftime(fmt)}"
        f" and ContentDate/Start lt {end.strftime(fmt)}"
        f" and OData.CSC.Intersects(area=geography'SRID=4326;{polygon}')"
    )

    log.info("Querying CDSE catalog (%s → %s)…", start.date(), end.date())
    log.debug("OData filter: %s", filt)

    results: list[dict] = []
    params = {"$filter": filt, "$orderby": "ContentDate/Start desc", "$top": 100}
    url: str | None = CDSE_CATALOG_URL

    while url:
        r = requests.get(url, params=params if url == CDSE_CATALOG_URL else None,
                         timeout=60)
        r.raise_for_status()
        payload = r.json()
        batch = payload.get("value", [])
        results.extend(batch)
        url = payload.get("@odata.nextLink")
        params = None  # nextLink already encodes everything
        if len(results) >= 500:  # paranoia cap
            break

    return results


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------


def download_safe(
    product_id: str,
    product_name: str,
    user: str,
    password: str,
    dest_dir: Path,
) -> tuple[Path, bool]:
    """Download full SAFE zip. Returns (path, was_cached)."""
    # product_name already ends in .SAFE — append .zip
    dest = dest_dir / f"{product_name}.zip"
    if dest.exists() and dest.stat().st_size > 0:
        return dest, True

    # Fresh token: SAFE downloads can take several minutes and tokens expire.
    token = cdse_password_token(user, password)
    headers = {"Authorization": f"Bearer {token}"}
    url = CDSE_DOWNLOAD_URL.format(pid=product_id)

    tmp = dest.with_suffix(".zip.partial")
    try:
        with requests.get(url, headers=headers, stream=True, timeout=(30, 600)) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            with open(tmp, "wb") as f, tqdm(
                total=total or None,
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
                desc="  SAFE",
                leave=False,
            ) as bar:
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        f.write(chunk)
                        bar.update(len(chunk))
        tmp.rename(dest)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise
    return dest, False


def download_preview(
    product_name: str,
    acq_start: datetime,
    acq_end: datetime,
    bbox: tuple[float, float, float, float],
    sh_client_id: str,
    sh_client_secret: str,
    dest_dir: Path,
) -> tuple[Path, bool]:
    """Render a clipped VV/VH/ratio GeoTIFF via Sentinel Hub Process API."""
    dest = dest_dir / f"{product_name}.tif"
    if dest.exists() and dest.stat().st_size > 0:
        return dest, True

    token = sh_client_credentials_token(sh_client_id, sh_client_secret)

    # Pad the acquisition time slightly so the matching scene is captured.
    pad = timedelta(minutes=1)
    t_from = (acq_start - pad).strftime("%Y-%m-%dT%H:%M:%SZ")
    t_to = (acq_end + pad).strftime("%Y-%m-%dT%H:%M:%SZ")

    minx, miny, maxx, maxy = bbox
    body = {
        "input": {
            "bounds": {
                "bbox": [minx, miny, maxx, maxy],
                "properties": {
                    "crs": "http://www.opengis.net/def/crs/EPSG/0/4326"
                },
            },
            "data": [
                {
                    "type": "sentinel-1-grd",
                    "dataFilter": {
                        "timeRange": {"from": t_from, "to": t_to},
                        "polarization": "DV",
                        "acquisitionMode": "IW",
                        "resolution": "HIGH",
                    },
                    "processing": {
                        "orthorectify": True,
                        "backCoeff": "SIGMA0_ELLIPSOID",
                    },
                }
            ],
        },
        "output": {
            "width": PREVIEW_WIDTH,
            "height": PREVIEW_HEIGHT,
            "responses": [
                {"identifier": "default", "format": {"type": "image/tiff"}}
            ],
        },
        "evalscript": EVALSCRIPT,
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "image/tiff",
    }

    r = requests.post(SH_PROCESS_URL, json=body, headers=headers, timeout=300)
    if r.status_code >= 400:
        log.error("Sentinel Hub Process API error %s: %s",
                  r.status_code, r.text[:500])
        r.raise_for_status()

    tmp = dest.with_suffix(".tif.partial")
    with open(tmp, "wb") as f:
        f.write(r.content)
    tmp.rename(dest)
    return dest, False


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def write_sidecar(product: dict, path: Path) -> None:
    """Save scene metadata as a sidecar JSON for downstream tooling."""
    if path.exists():
        return
    meta = {
        "Id": product.get("Id"),
        "Name": product.get("Name"),
        "ContentDate": product.get("ContentDate"),
        "OriginDate": product.get("OriginDate"),
        "ContentLength": product.get("ContentLength"),
        "Footprint": product.get("Footprint"),
        "GeoFootprint": product.get("GeoFootprint"),
        "S3Path": product.get("S3Path"),
    }
    path.write_text(json.dumps(meta, indent=2, default=str))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    date_group = p.add_mutually_exclusive_group(required=True)
    date_group.add_argument(
        "--start",
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc),
        help="Start date (YYYY-MM-DD, inclusive, UTC).",
    )
    date_group.add_argument(
        "--days",
        type=int,
        help="Convenience: pull the last N days (mutually exclusive with --start).",
    )
    p.add_argument(
        "--end",
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc),
        default=None,
        help="End date (YYYY-MM-DD, exclusive, UTC). Defaults to now.",
    )
    p.add_argument(
        "--bbox", nargs=4, type=float,
        metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"),
        default=list(DEFAULT_BBOX),
        help=f"Area of interest as 4 floats (lon/lat). "
             f"Default: {DEFAULT_BBOX} (Strait of Hormuz).",
    )
    p.add_argument(
        "--full-coverage", action="store_true",
        help="Only keep scenes whose footprint FULLY contains the bbox "
             "(default: any scene intersecting the bbox).",
    )
    p.add_argument(
        "--data-dir", type=Path, default=Path("data"),
        help="Output directory (default: ./data).",
    )
    p.add_argument(
        "--no-safe", action="store_true",
        help="Skip full SAFE product downloads.",
    )
    p.add_argument(
        "--no-preview", action="store_true",
        help="Skip Sentinel Hub clipped previews.",
    )
    p.add_argument(
        "--list-only", action="store_true",
        help="Just list matching scenes; download nothing.",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true",
        help="Verbose logging (DEBUG).",
    )
    return p.parse_args(argv)


def human_size(n: int | None) -> str:
    if not n:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    load_dotenv()
    cdse_user = os.getenv("CDSE_USER")
    cdse_pass = os.getenv("CDSE_PASSWORD")
    sh_id = os.getenv("SH_CLIENT_ID")
    sh_secret = os.getenv("SH_CLIENT_SECRET")

    need_safe = not args.no_safe and not args.list_only
    need_preview = not args.no_preview and not args.list_only

    if need_safe and not (cdse_user and cdse_pass):
        log.error("CDSE_USER / CDSE_PASSWORD missing from .env "
                  "(required for SAFE downloads).")
        return 2
    if need_preview and not (sh_id and sh_secret):
        log.error("SH_CLIENT_ID / SH_CLIENT_SECRET missing from .env "
                  "(required for Sentinel Hub previews).")
        return 2

    # Resolve date window
    end = args.end or datetime.now(tz=timezone.utc)
    if args.days is not None:
        start = end - timedelta(days=args.days)
    else:
        start = args.start
    if start >= end:
        log.error("Start (%s) must be before end (%s).", start, end)
        return 2

    # Prepare output dirs
    safe_dir = args.data_dir / "safe"
    preview_dir = args.data_dir / "previews"
    args.data_dir.mkdir(parents=True, exist_ok=True)
    if need_safe:
        safe_dir.mkdir(parents=True, exist_ok=True)
    if need_preview:
        preview_dir.mkdir(parents=True, exist_ok=True)

    # Validate bbox
    bbox = tuple(args.bbox)
    minx, miny, maxx, maxy = bbox
    if not (-180 <= minx < maxx <= 180 and -90 <= miny < maxy <= 90):
        log.error("Invalid bbox %s — need MIN_LON<MAX_LON in [-180,180], "
                  "MIN_LAT<MAX_LAT in [-90,90].", bbox)
        return 2

    log.info("Bounding box (lon/lat): %s", bbox)
    log.info("Time window: %s → %s (UTC)", start.isoformat(), end.isoformat())
    log.info("Coverage filter: %s",
             "full containment" if args.full_coverage else "any intersection")

    # Search
    try:
        products = search_products(start, end, bbox)
    except requests.HTTPError as e:
        log.error("Catalog query failed: %s — %s", e, e.response.text[:300])
        return 1

    if not products:
        log.warning("No Sentinel-1 IW GRDH (VV+VH) scenes intersect that bbox/window.")
        return 0

    log.info("Found %d intersecting scene(s) from CDSE.", len(products))

    # Optional client-side full-coverage filter
    if args.full_coverage:
        kept: list[dict] = []
        for p in products:
            ring = _ring_from_geofootprint(p.get("GeoFootprint") or {})
            if ring and _bbox_fully_within(bbox, ring):
                kept.append(p)
        dropped = len(products) - len(kept)
        log.info("Full-coverage filter: kept %d, dropped %d "
                 "(scene footprint did not contain the whole bbox).",
                 len(kept), dropped)
        if not kept:
            log.warning(
                "No scenes fully cover this bbox. A Sentinel-1 IW swath is "
                "~250 km wide and ~150-250 km along-track — bboxes larger "
                "than that can't be covered by a single scene. "
                "Try a smaller bbox, or drop --full-coverage."
            )
            return 0
        products = kept

    log.info("Will process %d scene(s):", len(products))
    for p in products:
        cd = p.get("ContentDate", {}) or {}
        log.info(
            "  • %s  [%s, %s]",
            p["Name"],
            cd.get("Start", "?"),
            human_size(p.get("ContentLength")),
        )

    if args.list_only:
        return 0

    # Download
    n_safe_cached = n_safe_new = 0
    n_prev_cached = n_prev_new = 0
    failures: list[str] = []

    for idx, p in enumerate(products, 1):
        name = p["Name"]
        pid = p["Id"]
        cd = p.get("ContentDate", {}) or {}
        # Strip 'Z' / 'fractional seconds' robustly
        acq_start = datetime.fromisoformat(
            cd["Start"].replace("Z", "+00:00")
        )
        acq_end = datetime.fromisoformat(
            cd["End"].replace("Z", "+00:00")
        )

        log.info("[%d/%d] %s", idx, len(products), name)

        # SAFE
        if need_safe:
            try:
                path, cached = download_safe(
                    pid, name, cdse_user, cdse_pass, safe_dir
                )
                if cached:
                    log.info("  SAFE: cached at %s", path)
                    n_safe_cached += 1
                else:
                    log.info("  SAFE: downloaded → %s (%s)",
                             path, human_size(path.stat().st_size))
                    n_safe_new += 1
                write_sidecar(p, path.with_suffix(".json"))
            except Exception as e:
                log.error("  SAFE: FAILED — %s", e)
                failures.append(f"SAFE {name}: {e}")

        # Preview
        if need_preview:
            try:
                path, cached = download_preview(
                    name, acq_start, acq_end, bbox,
                    sh_id, sh_secret, preview_dir,
                )
                if cached:
                    log.info("  preview: cached at %s", path)
                    n_prev_cached += 1
                else:
                    log.info("  preview: downloaded → %s (%s)",
                             path, human_size(path.stat().st_size))
                    n_prev_new += 1
                write_sidecar(p, path.with_suffix(".json"))
            except Exception as e:
                log.error("  preview: FAILED — %s", e)
                failures.append(f"preview {name}: {e}")

    log.info("─" * 60)
    log.info("Summary:")
    if need_safe:
        log.info("  SAFE:    %d new, %d cached", n_safe_new, n_safe_cached)
    if need_preview:
        log.info("  preview: %d new, %d cached", n_prev_new, n_prev_cached)
    if failures:
        log.warning("  failures (%d):", len(failures))
        for f in failures:
            log.warning("    - %s", f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())