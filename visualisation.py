#!/usr/bin/env python3
"""
visualisation.py — Render Sentinel-1 SAR data into false-color JPGs.

Reads what download_sar.py left in ./data/:
  • data/safe/*.SAFE.zip     — full GRD products (preferred: high resolution)
  • data/previews/*.SAFE.tif — clipped σ0 previews (fallback: medium res)

False-color recipe (linear power → dB → 0..255):
    R = VV    G = VH    B = VH / VV

For SAFE: σ0 is computed properly from the calibration LUT
(σ0 = DN² / sigmaNought²). σ0 ≈ γ0 within ~1 dB across the IW incidence
range — visually identical after a dB stretch.

For the preview: bands 1 & 2 are already σ0 from Sentinel Hub Process API;
band 3 (VV/VH) from the TIFF is recomputed as VH/VV to match the recipe above.

SAFE rendering is at native ~10 m resolution. When an image exceeds
--max-dim (default 8000 px) on any axis, it's split into a grid of tiles,
each tile saved as its own JPG. Tile filenames encode position so they can
be re-stitched or located back on the original scene:

    <scene>_falsecolor_r{row}c{col}_y{y0}-{y1}_x{x0}-{x1}.jpg

A companion `<scene>_falsecolor_tiles.json` manifest lists the full grid.
When the image fits in one tile, output is just `<scene>_falsecolor.jpg`
with no manifest.

Usage:
    python visualisation.py --list
    python visualisation.py <ID>
    python visualisation.py <ID> --prefer preview   # force lo-res preview
    python visualisation.py <ID> --max-dim 12000    # bigger tiles, fewer files

Outputs are written next to the source file (data/safe/ or data/previews/).

Requirements:
    pip install numpy tifffile imagecodecs Pillow
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import math
import os
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import numpy as np

log = logging.getLogger("visualisation")

# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

DATA_DIR = Path("data")
SAFE_DIR = DATA_DIR / "safe"
PREVIEW_DIR = DATA_DIR / "previews"

DEFAULT_MAX_DIM = 8000  # max pixel dim per tile when rendering from SAFE

# Default bbox: Strait of Hormuz [minLon, minLat, maxLon, maxLat].
# Override with --bbox W S E N on the command line.
DEFAULT_BBOX = (54.5, 25.0, 57.5, 27.5)

# σ0 dB stretch ranges, tuned for ocean / coastal scenes (Hormuz-friendly)
VV_RANGE_DB = (-25.0, 0.0)
VH_RANGE_DB = (-30.0, -5.0)
RATIO_RANGE_DB = (-10.0, 5.0)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def scene_id(name: str) -> str:
    """Stable 8-char hex ID from a scene name."""
    return hashlib.sha1(name.encode()).hexdigest()[:8]


def discover_scenes() -> dict[str, dict]:
    """Catalog every scene found under data/.  {id: {name, preview?, safe?}}"""
    catalog: dict[str, dict] = {}
    if PREVIEW_DIR.is_dir():
        for tif in sorted(PREVIEW_DIR.glob("*.SAFE.tif")):
            name = tif.stem  # → "..._SAFE"
            entry = catalog.setdefault(scene_id(name), {"name": name})
            entry["preview"] = tif
    if SAFE_DIR.is_dir():
        for zf in sorted(SAFE_DIR.glob("*.SAFE.zip")):
            name = zf.stem
            entry = catalog.setdefault(scene_id(name), {"name": name})
            entry["safe"] = zf
    return catalog


def print_listing(catalog: dict[str, dict]) -> None:
    if not catalog:
        print("No scenes found in ./data/.")
        print("Run download_sar.py first.")
        return
    print(f"{'ID':10}  {'Sources':14}  Scene")
    print("─" * 100)
    for sid, info in sorted(catalog.items(), key=lambda x: x[1]["name"]):
        sources = []
        if "safe" in info:
            sources.append("SAFE")
        if "preview" in info:
            sources.append("preview")
        print(f"{sid:10}  {'+'.join(sources):14}  {info['name']}")
    print()
    print(f"{len(catalog)} scene(s). Pass an ID (or unique prefix) to render.")


# ---------------------------------------------------------------------------
# Bbox / coverage filtering
# ---------------------------------------------------------------------------


def _load_sidecar(scene: dict) -> dict | None:
    """Read the JSON sidecar that download_sar.py wrote next to the source.

    Returns the parsed metadata dict (which contains GeoFootprint), or None
    if no readable sidecar is present.
    """
    for key in ("safe", "preview"):
        if key not in scene:
            continue
        src: Path = scene[key]
        sidecar = src.with_suffix(".json")
        if sidecar.exists():
            try:
                return json.loads(sidecar.read_text())
            except (json.JSONDecodeError, OSError):
                continue
    return None


def _ring_from_geofootprint(geofootprint: dict) -> list[tuple[float, float]] | None:
    """Outer ring of a GeoJSON Polygon / MultiPolygon → list of (lon, lat)."""
    if not geofootprint or "type" not in geofootprint:
        return None
    coords = geofootprint.get("coordinates")
    if not coords:
        return None
    t = geofootprint["type"]
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
    """All 4 bbox corners inside the polygon ring."""
    minx, miny, maxx, maxy = bbox
    corners = [(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy)]
    return all(_point_in_polygon(x, y, ring) for x, y in corners)


# ---------------------------------------------------------------------------
# Geolocation grid — used to map crop_bbox to pixel offsets in a SAFE scene
# ---------------------------------------------------------------------------


def _parse_geolocation_grid(
    xml_bytes: bytes,
) -> list[tuple[int, int, float, float]]:
    """Return (line, pixel, lat, lon) tuples from a Sentinel-1 annotation XML.

    Sentinel-1 GRDH annotation XMLs contain a sparse geolocation grid sampled
    at roughly 10 km spacing.  We use this to find the pixel bounding box that
    corresponds to a geographic crop region.
    """
    root = ET.fromstring(xml_bytes)
    points: list[tuple[int, int, float, float]] = []
    for pt in root.findall(".//geolocationGridPoint"):
        points.append((
            int(pt.find("line").text),
            int(pt.find("pixel").text),
            float(pt.find("latitude").text),
            float(pt.find("longitude").text),
        ))
    return points


def _pixel_bounds_from_geo(
    geo_points: list[tuple[int, int, float, float]],
    crop_bbox: tuple[float, float, float, float],
    full_h: int,
    full_w: int,
) -> tuple[int, int, int, int]:
    """Conservative pixel/line bounds for a geographic crop_bbox.

    Returns (line_min, line_max, pix_min, pix_max).  Adds one GCP grid step of
    margin on each side so edge pixels aren't clipped.  Clamps to the full
    image extent.
    """
    minlon, minlat, maxlon, maxlat = crop_bbox

    # Include GCPs a little outside the target bbox to bracket the boundary
    margin_lat = max((maxlat - minlat) * 0.15, 0.05)
    margin_lon = max((maxlon - minlon) * 0.15, 0.05)

    near = [
        (l, p)
        for l, p, lat, lon in geo_points
        if (minlat - margin_lat) <= lat <= (maxlat + margin_lat)
        and (minlon - margin_lon) <= lon <= (maxlon + margin_lon)
    ]
    if not near:
        raise ValueError(
            f"No geolocation grid points near bbox {crop_bbox}. "
            "The bbox may lie outside the scene footprint."
        )

    near_lines = sorted({l for l, _ in near})
    near_pix   = sorted({p for _, p in near})

    # One grid step as margin
    all_lines = sorted({l for l, _, _, _ in geo_points})
    all_pix   = sorted({p for _, p, _, _ in geo_points})
    line_step = (all_lines[-1] - all_lines[0]) // max(len(all_lines) - 1, 1)
    pix_step  = (all_pix[-1]   - all_pix[0])   // max(len(all_pix)   - 1, 1)

    lmin = max(0,      near_lines[0]  - line_step)
    lmax = min(full_h, near_lines[-1] + line_step)
    pmin = max(0,      near_pix[0]    - pix_step)
    pmax = min(full_w, near_pix[-1]   + pix_step)

    if lmin >= lmax or pmin >= pmax:
        raise ValueError(
            f"Crop bbox produced an empty pixel region: "
            f"lines {lmin}:{lmax}  pixels {pmin}:{pmax}."
        )
    return lmin, lmax, pmin, pmax


# ---------------------------------------------------------------------------
# GeoTIFF extent helpers — used to crop preview images by geographic bbox
# ---------------------------------------------------------------------------


def _geotiff_extent(tif_path: Path) -> tuple[float, float, float, float] | None:
    """Geographic extent of a GeoTIFF as (minlon, minlat, maxlon, maxlat).

    Reads ModelPixelScaleTag (33550) and ModelTiepointTag (33922) from the
    TIFF tags — present in any Sentinel Hub output.  Returns None if absent.
    """
    import tifffile
    with tifffile.TiffFile(str(tif_path)) as tf:
        tags = tf.pages[0].tags
        scale_tag = tags.get(33550)
        tie_tag   = tags.get(33922)
        h, w = tf.pages[0].shape[:2]

    if scale_tag is None or tie_tag is None:
        return None

    sx = scale_tag.value[0]   # lon  per pixel (east)
    sy = scale_tag.value[1]   # lat  per pixel (south, positive)
    ox = tie_tag.value[3]     # longitude of upper-left corner
    oy = tie_tag.value[4]     # latitude  of upper-left corner

    return (ox, oy - sy * h, ox + sx * w, oy)  # (minlon, minlat, maxlon, maxlat)


def _preview_crop_slice(
    extent: tuple[float, float, float, float],
    crop_bbox: tuple[float, float, float, float],
    h: int,
    w: int,
) -> tuple[int, int, int, int]:
    """Pixel slice (row0, row1, col0, col1) for crop_bbox inside a GeoTIFF.

    extent and crop_bbox are both (minlon, minlat, maxlon, maxlat).
    Row 0 corresponds to the northern (max lat) edge of the image.
    """
    minlon_e, minlat_e, maxlon_e, maxlat_e = extent
    minlon_c, minlat_c, maxlon_c, maxlat_c = crop_bbox

    sx = (maxlon_e - minlon_e) / w   # lon per pixel
    sy = (maxlat_e - minlat_e) / h   # lat per pixel (row 0 = maxlat)

    col0 = max(0, int((minlon_c - minlon_e) / sx))
    col1 = min(w, math.ceil((maxlon_c - minlon_e) / sx))
    row0 = max(0, int((maxlat_e - maxlat_c) / sy))
    row1 = min(h, math.ceil((maxlat_e - minlat_c) / sy))

    if col0 >= col1 or row0 >= row1:
        raise ValueError(
            f"Crop bbox {crop_bbox} does not overlap the GeoTIFF extent {extent}."
        )
    return row0, row1, col0, col1


def filter_catalog(
    catalog: dict[str, dict],
    bbox: tuple[float, float, float, float],
    full_coverage: bool,
) -> dict[str, dict]:
    """When full_coverage is set, keep only scenes whose footprint contains the bbox.

    Footprints come from sidecar JSON files written by download_sar.py
    (the `GeoFootprint` field). Scenes lacking a usable sidecar are dropped
    with a warning, since the filter can't be evaluated for them.
    """
    if not full_coverage:
        return catalog

    kept: dict[str, dict] = {}
    no_sidecar: list[str] = []
    no_ring: list[str] = []
    for sid, info in catalog.items():
        meta = _load_sidecar(info)
        if not meta:
            no_sidecar.append(info["name"])
            continue
        ring = _ring_from_geofootprint(meta.get("GeoFootprint") or {})
        if not ring:
            no_ring.append(info["name"])
            continue
        if _bbox_fully_within(bbox, ring):
            kept[sid] = info

    if no_sidecar:
        log.warning("Excluded %d scene(s) with no sidecar JSON "
                    "(re-run download_sar.py to regenerate metadata).",
                    len(no_sidecar))
    if no_ring:
        log.warning("Excluded %d scene(s) with no usable GeoFootprint.",
                    len(no_ring))
    return kept


# ---------------------------------------------------------------------------
# False-color rendering
# ---------------------------------------------------------------------------


def stretch_db(power: np.ndarray, lo_db: float, hi_db: float) -> np.ndarray:
    """Linear power → dB → clipped 0..1."""
    db = 10.0 * np.log10(np.maximum(power, 1e-7))
    return np.clip((db - lo_db) / (hi_db - lo_db), 0.0, 1.0)


def false_color(vv: np.ndarray, vh: np.ndarray) -> np.ndarray:
    """RGB = (VV, VH, VH/VV).  Returns uint8 (H, W, 3)."""
    ratio = vh / np.maximum(vv, 1e-7)
    r = stretch_db(vv, *VV_RANGE_DB)
    g = stretch_db(vh, *VH_RANGE_DB)
    b = stretch_db(ratio, *RATIO_RANGE_DB)
    rgb = np.stack([r, g, b], axis=-1)
    return (rgb * 255.0).astype(np.uint8)


def save_jpg(rgb: np.ndarray, path: Path) -> None:
    from PIL import Image
    Image.fromarray(rgb).save(path, "JPEG", quality=92, optimize=True)


# ---------------------------------------------------------------------------
# Preview renderer  (medium-res, σ0 already calibrated)
# ---------------------------------------------------------------------------


def render_preview(
    tif_path: Path,
    out_dir: Path,
    scene_name: str,
    crop_bbox: tuple[float, float, float, float] | None = None,
) -> list[Path]:
    import tifffile
    log.info("Reading preview GeoTIFF: %s", tif_path.name)
    arr = tifffile.imread(str(tif_path))
    log.debug("raw shape: %s  dtype: %s", arr.shape, arr.dtype)

    if arr.ndim != 3:
        raise ValueError(f"Unexpected preview shape: {arr.shape}")
    # tifffile may return (bands, H, W) for PlanarConfig=2; flip to (H, W, bands)
    if arr.shape[0] in (3, 4) and arr.shape[0] < min(arr.shape[1], arr.shape[2]):
        arr = np.moveaxis(arr, 0, -1)
    if arr.shape[-1] < 2:
        raise ValueError(f"Need ≥2 bands (VV, VH); got {arr.shape}")

    if crop_bbox is not None:
        extent = _geotiff_extent(tif_path)
        if extent is None:
            log.warning("Preview GeoTIFF has no geotransform tags; skipping bbox crop.")
        else:
            h, w = arr.shape[:2]
            try:
                r0, r1, c0, c1 = _preview_crop_slice(extent, crop_bbox, h, w)
                log.info("Cropping preview to bbox %s → rows %d:%d  cols %d:%d",
                         crop_bbox, r0, r1, c0, c1)
                arr = arr[r0:r1, c0:c1, :]
            except ValueError as e:
                log.warning("Preview crop failed: %s  Rendering full preview.", e)

    vv = arr[..., 0].astype(np.float32)
    vh = arr[..., 1].astype(np.float32)
    log.info("Working size: %d × %d", vv.shape[1], vv.shape[0])
    log.info("Building false-color RGB…")
    rgb = false_color(vv, vh)
    out_path = out_dir / f"{scene_name}_falsecolor.jpg"
    log.info("Writing JPG: %s", out_path)
    save_jpg(rgb, out_path)
    return [out_path]


# ---------------------------------------------------------------------------
# SAFE renderer  (high-res, calibration from LUT)
# ---------------------------------------------------------------------------


def _find_member(members: list[str], *must_contain: str, ext: str) -> str:
    for m in members:
        if m.endswith(ext) and all(p in m for p in must_contain):
            return m
    raise FileNotFoundError(
        f"No zip member matches {must_contain!r} with extension {ext!r}"
    )


def parse_calibration_lut(xml_bytes: bytes):
    """Read the sigmaNought LUT from a Sentinel-1 calibration XML.

    Returns (lines, pixels, sigmas):
        lines  shape (N,)         ascending azimuth line indices
        pixels shape (M,)         range pixel positions (same for every vector)
        sigmas shape (N, M)       LUT values
    """
    root = ET.fromstring(xml_bytes)
    vectors = root.findall(".//calibrationVector")
    if not vectors:
        raise ValueError("No calibrationVector elements in XML")

    lines: list[int] = []
    sigmas: list[np.ndarray] = []
    pixels_arr: np.ndarray | None = None

    for v in vectors:
        lines.append(int(v.find("line").text))
        sigma = np.fromstring(v.find("sigmaNought").text, sep=" ", dtype=np.float64)
        sigmas.append(sigma)
        if pixels_arr is None:
            pixels_arr = np.fromstring(
                v.find("pixel").text, sep=" ", dtype=np.int64
            )

    return np.asarray(lines), pixels_arr, np.asarray(sigmas)


def _bilinear_lut(
    cal_lines: np.ndarray,
    cal_pixels: np.ndarray,
    cal_sigmas: np.ndarray,
    out_lines: np.ndarray,
    out_pixels: np.ndarray,
) -> np.ndarray:
    """Bilinear interpolation of cal_sigmas onto (out_lines × out_pixels)."""
    # Locate enclosing cell for each output coord
    i = np.clip(np.searchsorted(cal_lines, out_lines) - 1, 0, len(cal_lines) - 2)
    j = np.clip(np.searchsorted(cal_pixels, out_pixels) - 1, 0, len(cal_pixels) - 2)

    line_lo = cal_lines[i].astype(np.float32)
    line_hi = cal_lines[i + 1].astype(np.float32)
    pix_lo = cal_pixels[j].astype(np.float32)
    pix_hi = cal_pixels[j + 1].astype(np.float32)

    line_w = ((out_lines - line_lo) / np.maximum(line_hi - line_lo, 1.0)).astype(np.float32)
    pix_w = ((out_pixels - pix_lo) / np.maximum(pix_hi - pix_lo, 1.0)).astype(np.float32)

    # 4 corner samples — broadcast to (H, W)
    s00 = cal_sigmas[i[:, None], j[None, :]].astype(np.float32)
    s01 = cal_sigmas[i[:, None], (j + 1)[None, :]].astype(np.float32)
    s10 = cal_sigmas[(i + 1)[:, None], j[None, :]].astype(np.float32)
    s11 = cal_sigmas[(i + 1)[:, None], (j + 1)[None, :]].astype(np.float32)

    lw = line_w[:, None]
    pw = pix_w[None, :]
    return (
        s00 * (1 - lw) * (1 - pw)
        + s01 * (1 - lw) * pw
        + s10 * lw * (1 - pw)
        + s11 * lw * pw
    )


def compute_sigma0(
    dn: np.ndarray,
    cal_lines: np.ndarray,
    cal_pixels: np.ndarray,
    cal_sigmas: np.ndarray,
    y_offset: int = 0,
    x_offset: int = 0,
) -> np.ndarray:
    """σ0 = DN² / sigmaNought².  LUT sampled at the tile's coords in the
    full-image reference frame (y_offset, x_offset = tile's top-left corner)."""
    h, w = dn.shape
    out_lines = np.arange(y_offset, y_offset + h, dtype=np.float32)
    out_pixels = np.arange(x_offset, x_offset + w, dtype=np.float32)

    sigma_lut = _bilinear_lut(cal_lines, cal_pixels, cal_sigmas, out_lines, out_pixels)

    dn_f = dn.astype(np.float32)
    sigma_sq = sigma_lut * sigma_lut + 1e-7
    return (dn_f * dn_f) / sigma_sq


def tile_layout(h: int, w: int, max_dim: int) -> list[tuple[int, int, int, int, int, int]]:
    """Split an (h, w) image into a grid of tiles each ≤ max_dim per axis.

    Tiles are sized as evenly as possible. Returns a list of
        (row, col, y0, y1, x0, x1)
    where the slice [y0:y1, x0:x1] gives the tile in the full image."""
    n_rows = max(1, math.ceil(h / max_dim))
    n_cols = max(1, math.ceil(w / max_dim))
    row_edges = np.linspace(0, h, n_rows + 1).astype(int)
    col_edges = np.linspace(0, w, n_cols + 1).astype(int)
    tiles: list[tuple[int, int, int, int, int, int]] = []
    for r in range(n_rows):
        for c in range(n_cols):
            tiles.append(
                (r, c,
                 int(row_edges[r]),   int(row_edges[r + 1]),
                 int(col_edges[c]),   int(col_edges[c + 1]))
            )
    return tiles


def write_tile_manifest(
    path: Path,
    scene_name: str,
    h_full: int,
    w_full: int,
    n_rows: int,
    n_cols: int,
    max_dim: int,
    tiles: list[tuple[int, int, int, int, int, int]],
    files: list[Path],
) -> None:
    manifest = {
        "scene": scene_name,
        "full_image": {"width": w_full, "height": h_full},
        "grid": {"rows": n_rows, "cols": n_cols, "max_dim": max_dim},
        "tiles": [
            {
                "row": r, "col": c,
                "y_range": [y0, y1], "x_range": [x0, x1],
                "width": x1 - x0, "height": y1 - y0,
                "file": files[i].name,
            }
            for i, (r, c, y0, y1, x0, x1) in enumerate(tiles)
        ],
    }
    path.write_text(json.dumps(manifest, indent=2))


def render_safe(
    safe_zip: Path,
    out_dir: Path,
    scene_name: str,
    max_dim: int = DEFAULT_MAX_DIM,
    crop_bbox: tuple[float, float, float, float] | None = None,
) -> list[Path]:
    """Render a SAFE at native resolution, tiling so every tile ≤ max_dim px.

    When crop_bbox (minlon, minlat, maxlon, maxlat) is supplied the geolocation
    grid in the annotation XML is used to find the pixel/line range that covers
    the geographic area.  Only that crop is rendered.  The calibration LUT is
    still sampled at the original full-image coordinates so σ0 is correct.
    """
    import tifffile

    log.info("Opening SAFE archive: %s", safe_zip.name)
    crop_y_off = crop_x_off = 0  # pixel offsets into the full image for LUT calibration

    with zipfile.ZipFile(safe_zip) as z:
        members = z.namelist()

        vv_tif = _find_member(members, "/measurement/", "-vv-", ext=".tiff")
        vh_tif = _find_member(members, "/measurement/", "-vh-", ext=".tiff")
        vv_cal = _find_member(
            members, "/annotation/calibration/calibration-", "-vv-", ext=".xml"
        )
        vh_cal = _find_member(
            members, "/annotation/calibration/calibration-", "-vh-", ext=".xml"
        )

        log.info("  VV: %s", vv_tif.split("/")[-1])
        log.info("  VH: %s", vh_tif.split("/")[-1])

        log.info("Reading VV measurement TIFF…")
        with z.open(vv_tif) as f:
            vv_dn_full = tifffile.imread(io.BytesIO(f.read()))
        log.info("Reading VH measurement TIFF…")
        with z.open(vh_tif) as f:
            vh_dn_full = tifffile.imread(io.BytesIO(f.read()))

        log.info("Parsing calibration LUTs…")
        cal_vv = parse_calibration_lut(z.read(vv_cal))
        cal_vh = parse_calibration_lut(z.read(vh_cal))

        # Geographic crop — locate the annotation XML with the geolocation grid
        if crop_bbox is not None:
            ann_vv = next(
                (m for m in members
                 if m.endswith(".xml")
                 and "/annotation/s1" in m
                 and "/calibration/" not in m
                 and "-vv-" in m),
                None,
            )
            if ann_vv is None:
                log.warning("No annotation XML found in SAFE; skipping bbox crop.")
            else:
                try:
                    geo_points = _parse_geolocation_grid(z.read(ann_vv))
                    full_h, full_w = vv_dn_full.shape
                    lmin, lmax, pmin, pmax = _pixel_bounds_from_geo(
                        geo_points, crop_bbox, full_h, full_w
                    )
                    log.info(
                        "Cropping SAFE to bbox %s → lines %d:%d  pixels %d:%d  (%d×%d px)",
                        crop_bbox, lmin, lmax, pmin, pmax,
                        pmax - pmin, lmax - lmin,
                    )
                    vv_dn_full = vv_dn_full[lmin:lmax, pmin:pmax]
                    vh_dn_full = vh_dn_full[lmin:lmax, pmin:pmax]
                    crop_y_off, crop_x_off = lmin, pmin
                except ValueError as e:
                    log.warning("SAFE bbox crop failed: %s  Rendering full scene.", e)

    h, w = vv_dn_full.shape
    log.info(
        "Working image: %d × %d  (%.0f MP)%s",
        w, h, h * w / 1e6,
        "  (cropped)" if (crop_y_off or crop_x_off) else "  — native resolution",
    )

    tiles = tile_layout(h, w, max_dim)
    n_rows = tiles[-1][0] + 1
    n_cols = tiles[-1][1] + 1

    single_tile = len(tiles) == 1
    if single_tile:
        log.info("Image fits in one tile (≤ %d px on both axes).", max_dim)
    else:
        log.info("Tiling into %d × %d grid → %d JPG file(s), tile ≤ %d px",
                 n_rows, n_cols, len(tiles), max_dim)

    width_digits = max(len(str(w)), 6)  # zero-pad for sortable filenames

    written: list[Path] = []
    for i, (r, c, y0, y1, x0, x1) in enumerate(tiles, start=1):
        tile_h, tile_w = y1 - y0, x1 - x0
        log.info(
            "  tile [%d/%d]  r=%d c=%d  y=%d:%d  x=%d:%d  (%d × %d)",
            i, len(tiles), r, c, y0, y1, x0, x1, tile_w, tile_h,
        )

        vv_dn_tile = vv_dn_full[y0:y1, x0:x1]
        vh_dn_tile = vh_dn_full[y0:y1, x0:x1]

        # LUT coords must be in the full-image reference frame
        vv_sigma0 = compute_sigma0(
            vv_dn_tile, *cal_vv, y_offset=y0 + crop_y_off, x_offset=x0 + crop_x_off
        )
        vh_sigma0 = compute_sigma0(
            vh_dn_tile, *cal_vh, y_offset=y0 + crop_y_off, x_offset=x0 + crop_x_off
        )

        rgb = false_color(vv_sigma0, vh_sigma0)

        if single_tile:
            name = f"{scene_name}_falsecolor.jpg"
        else:
            name = (
                f"{scene_name}_falsecolor"
                f"_r{r:02d}c{c:02d}"
                f"_y{y0:0{width_digits}d}-{y1:0{width_digits}d}"
                f"_x{x0:0{width_digits}d}-{x1:0{width_digits}d}.jpg"
            )

        path = out_dir / name
        save_jpg(rgb, path)
        log.debug("    → %s  (%.1f MB)", path.name, path.stat().st_size / 1e6)
        written.append(path)

        # Free per-tile working memory
        del vv_dn_tile, vh_dn_tile, vv_sigma0, vh_sigma0, rgb

    if not single_tile:
        manifest_path = out_dir / f"{scene_name}_falsecolor_tiles.json"
        write_tile_manifest(
            manifest_path, scene_name, h, w, n_rows, n_cols, max_dim, tiles, written
        )
        log.info("Wrote tile manifest: %s", manifest_path.name)
        written.append(manifest_path)

    return written


# ---------------------------------------------------------------------------
# SAFE zip trimming — remove raw measurement TIFFs after rendering
# ---------------------------------------------------------------------------


def trim_measurement_folder(safe_zip: Path) -> None:
    """Remove measurement/ entries from a SAFE zip in-place.

    A Sentinel-1 SAFE zip's measurement/ subdirectory holds the raw GRD
    binary TIFFs (the bulk of the ~1 GB file).  After all tiles have been
    rendered, those can be dropped to reclaim disk space while preserving
    everything else (annotation XMLs, calibration LUTs, manifest, support).

    The operation is atomic: a temp file is written alongside the original,
    then renamed over it.  If writing fails the original is left untouched.
    Already-trimmed zips (no measurement/ entries) are silently skipped.
    """
    with zipfile.ZipFile(safe_zip) as z:
        all_members = z.namelist()
        keep = [m for m in all_members if "/measurement/" not in m]
        drop = [m for m in all_members if "/measurement/" in m]

    if not drop:
        log.info("Zip already trimmed (no measurement/ entries): %s", safe_zip.name)
        return

    size_before_mb = safe_zip.stat().st_size / 1e6
    log.info(
        "Trimming %d measurement/ entr%s from %s (%.0f MB)…",
        len(drop), "y" if len(drop) == 1 else "ies", safe_zip.name, size_before_mb,
    )

    tmp = safe_zip.with_suffix(".trimming")
    try:
        with zipfile.ZipFile(safe_zip) as src, \
             zipfile.ZipFile(tmp, "w") as dst:
            for member in keep:
                info = src.getinfo(member)
                dst.writestr(info, src.read(member))
        tmp.replace(safe_zip)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    size_after_mb = safe_zip.stat().st_size / 1e6
    log.info(
        "Trimmed %s: %.0f MB → %.0f MB  (freed %.0f MB)",
        safe_zip.name, size_before_mb, size_after_mb,
        size_before_mb - size_after_mb,
    )


def _is_safe_trimmed(safe_zip: Path) -> bool:
    """Return True if the zip contains no measurement/ entries."""
    with zipfile.ZipFile(safe_zip) as z:
        return not any("/measurement/" in m for m in z.namelist())


def _redownload_safe(safe_zip: Path) -> None:
    """Re-download a full SAFE product, overwriting the existing (trimmed) zip.

    Reads the product UUID from the companion sidecar JSON written by
    download_sar.py, authenticates with the CDSE OAuth endpoint using
    CDSE_USER / CDSE_PASSWORD, then streams the ~1 GB product to disk.

    Credentials are read from environment variables.  If they are not set,
    the .env file in the script directory (or CWD) is parsed first.
    """
    import requests

    # Load .env if credentials not already in environment
    def _load_dotenv() -> None:
        for candidate in (Path(__file__).parent / ".env", Path(".env")):
            if candidate.exists():
                for line in candidate.read_text().splitlines():
                    m = re.match(r"^\s*([A-Z_][A-Z0-9_]*)=(.*)", line)
                    if m and m.group(1) not in os.environ:
                        os.environ[m.group(1)] = m.group(2).strip()
                break

    _load_dotenv()

    user = os.environ.get("CDSE_USER", "")
    pw   = os.environ.get("CDSE_PASSWORD", "")
    if not user or not pw:
        raise RuntimeError(
            "CDSE_USER / CDSE_PASSWORD not set. "
            "Add them to .env or export them before running."
        )

    # Product UUID from sidecar JSON (written by download_sar.py)
    sidecar = safe_zip.with_suffix(".json")   # S1A_...SAFE.json
    if not sidecar.exists():
        raise FileNotFoundError(
            f"Sidecar {sidecar.name} not found — cannot determine the CDSE "
            "product ID needed for re-download.  Run download_sar.py to "
            "regenerate it."
        )
    meta = json.loads(sidecar.read_text())
    pid  = meta.get("Id")
    if not pid:
        raise ValueError(f"No 'Id' field in sidecar {sidecar.name}.")

    # Authenticate
    token_url = (
        "https://identity.dataspace.copernicus.eu/auth/realms/CDSE"
        "/protocol/openid-connect/token"
    )
    log.info("Authenticating with CDSE…")
    r = requests.post(
        token_url,
        data={"client_id": "cdse-public", "grant_type": "password",
              "username": user, "password": pw},
        timeout=30,
    )
    r.raise_for_status()
    token = r.json()["access_token"]

    # Stream download to a .partial temp file, then rename
    download_url = (
        f"https://download.dataspace.copernicus.eu"
        f"/odata/v1/Products({pid})/$value"
    )
    log.info("Re-downloading %s (product %s)…", safe_zip.name, pid)
    tmp = safe_zip.with_suffix(".zip.partial")
    try:
        with requests.get(
            download_url,
            headers={"Authorization": f"Bearer {token}"},
            stream=True,
            timeout=(30, 600),
        ) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            downloaded = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total and downloaded % (50 * 1024 * 1024) < 256 * 1024:
                            log.info(
                                "  %.0f / %.0f MB  (%.0f%%)",
                                downloaded / 1e6, total / 1e6,
                                100 * downloaded / total,
                            )
        tmp.rename(safe_zip)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise

    log.info(
        "Re-download complete: %s (%.0f MB)",
        safe_zip.name, safe_zip.stat().st_size / 1e6,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("scene_id", nargs="?", help="Scene ID (or unique prefix).")
    p.add_argument("--list", action="store_true",
                   help="List scenes in data/ and exit.")
    p.add_argument("--max-dim", type=int, default=DEFAULT_MAX_DIM,
                   help=f"Max pixel dim per tile (default {DEFAULT_MAX_DIM}). "
                        f"Larger = fewer tiles + more RAM; smaller = more "
                        f"tiles + less RAM.")
    p.add_argument("--prefer", choices=["safe", "preview"], default="safe",
                   help="Which source to use when both exist (default safe).")
    p.add_argument(
        "--bbox", nargs=4, type=float,
        metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"),
        default=list(DEFAULT_BBOX),
        help=f"Reference area for --full-coverage (lon/lat floats). "
             f"Default: {DEFAULT_BBOX} (Strait of Hormuz).",
    )
    p.add_argument(
        "--full-coverage", action="store_true",
        help="Only consider scenes whose footprint fully contains the bbox "
             "(uses GeoFootprint from sidecar JSON written by download_sar.py).",
    )
    p.add_argument(
        "--crop-bbox", nargs=4, type=float,
        metavar=("W", "S", "E", "N"),
        help="Crop the rendered image to this geographic bbox (min_lon min_lat max_lon max_lat). "
             "Uses the geolocation grid (SAFE) or GeoTIFF tags (preview) to compute pixel bounds.",
    )
    p.add_argument(
        "--trim-safe", action="store_true",
        help="After rendering, remove measurement/ entries (raw GRD TIFFs) "
             "from the SAFE zip to reclaim ~1 GB of disk space. "
             "Annotation, calibration, and manifest files are preserved. "
             "Irreversible — re-download via download_sar.py if needed.",
    )
    p.add_argument("-v", "--verbose", action="store_true",
                   help="DEBUG-level logging.")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    catalog = discover_scenes()

    if args.full_coverage:
        bbox = tuple(args.bbox)
        log.info("Filtering catalog: only scenes fully covering %s", bbox)
        before = len(catalog)
        catalog = filter_catalog(catalog, bbox, full_coverage=True)
        log.info("  %d → %d scene(s) after coverage filter", before, len(catalog))

    if args.list:
        print_listing(catalog)
        return 0

    if not args.scene_id:
        print("Provide a scene ID, or use --list to see options.", file=sys.stderr)
        return 2

    matches = [sid for sid in catalog if sid.startswith(args.scene_id)]
    if not matches:
        print(f"No scene matching '{args.scene_id}'.  Try --list.", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"Ambiguous prefix '{args.scene_id}' — matches: {', '.join(matches)}",
              file=sys.stderr)
        return 1

    sid = matches[0]
    info = catalog[sid]
    name = info["name"]
    log.info("Scene: %s   id=%s", name, sid)

    # Pick source
    if args.prefer == "safe" and "safe" in info:
        source = "safe"
    elif args.prefer == "preview" and "preview" in info:
        source = "preview"
    elif "safe" in info:
        source = "safe"
    elif "preview" in info:
        source = "preview"
    else:
        log.error("No usable file for scene %s", sid)
        return 1

    crop_bbox = tuple(args.crop_bbox) if args.crop_bbox else None
    if crop_bbox:
        log.info("Crop bbox: %s", crop_bbox)

    if source == "safe":
        src: Path = info["safe"]
        out_dir = src.parent
        log.info("Source: SAFE (native resolution, tiles ≤ %d px)", args.max_dim)

        if _is_safe_trimmed(src):
            log.warning(
                "SAFE zip has been trimmed (measurement/ removed). "
                "Tiles cannot be (re-)generated without the raw data. "
                "Attempting automatic re-download…"
            )
            try:
                _redownload_safe(src)
            except Exception as exc:
                log.error("Re-download failed: %s", exc)
                log.error(
                    "Re-run download_sar.py to fetch a fresh copy, "
                    "then retry visualisation.py."
                )
                return 1

        written = render_safe(src, out_dir, name, max_dim=args.max_dim, crop_bbox=crop_bbox)
        if args.trim_safe:
            trim_measurement_folder(src)
    else:
        src = info["preview"]
        out_dir = src.parent
        log.info("Source: preview GeoTIFF")
        written = render_preview(src, out_dir, name, crop_bbox=crop_bbox)

    log.info("─" * 60)
    log.info("✓ Wrote %d file(s):", len(written))
    total = 0.0
    for p in written:
        size_mb = p.stat().st_size / 1e6
        total += size_mb
        log.info("  %s  (%.1f MB)", p.name, size_mb)
    log.info("Total: %.1f MB", total)
    return 0


if __name__ == "__main__":
    sys.exit(main())