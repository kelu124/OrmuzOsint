#!/usr/bin/env python3
"""
ais2.py — minimal aisstream.io text streamer.

Adapted from the DB-backed collector pattern. Same principles:
  • Subscribes to PositionReport AND ShipStaticData
  • Keeps an in-memory static cache keyed by MMSI so each position line gets
    enriched with ship name / type / destination / dimensions as soon as the
    static data has been seen at least once
  • Per-vessel throttle (one line per MMSI per --throttle seconds)
  • Normalizes the aisstream nanosecond timestamps to ISO-8601
  • Reconnects with backoff

No database. No batching. No external helper modules. One line per kept
message goes to stdout — pipe it, tee it, grep it.

Usage:
    python ais2.py                                    # default Hormuz bbox
    python ais2.py --bbox 22.0 48.0 30.5 60.0          # full Persian Gulf
    python ais2.py --bbox 54.5 25.0 57.5 27.5 --throttle 60
    python ais2.py --jsonl > stream.jsonl              # machine-readable
    python ais2.py --include-static                    # also log static-data updates

Credentials (.env or env):
    AISSTREAM_API_KEY=...

Requirements:
    pip install websockets python-dotenv
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import websockets

log = logging.getLogger("ais2")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def normalize_timestamp(raw: str) -> str:
    """aisstream's "2026-03-14 06:57:51.594510977 +0000 UTC" → ISO-8601.

    Empty string in → empty string out.
    """
    if not raw:
        return ""
    try:
        parts = raw.split(" +")
        base = parts[0] if len(parts) >= 2 else raw.rstrip(" UTC")
        base = base.replace(" ", "T", 1)
        if "." in base:
            main, frac = base.split(".", 1)
            base = main + "." + frac[:6]  # ns → µs
        return base
    except Exception:
        return ""


def _dim_sum(dim: dict, *keys: str) -> float | None:
    """Sum the dimension components, treating missing as 0. Returns None if all are missing."""
    if not dim:
        return None
    vals = [dim.get(k) for k in keys]
    if all(v is None for v in vals):
        return None
    return sum(v or 0 for v in vals)


def format_human(record: dict) -> str:
    """One-line human-readable rendering of a position record."""
    ts = record.get("time_utc") or record.get("received_at") or ""
    # Compact time: just HH:MM:SS UTC if possible
    short_t = ts[11:19] if len(ts) >= 19 else ts
    mmsi = record["mmsi"]
    name = (record.get("ship_name") or "").strip()[:20]
    lat = record["lat"]
    lon = record["lon"]
    sog = record.get("sog")
    cog = record.get("cog")
    dest = (record.get("destination") or "").strip()[:18]

    sog_s = f"{sog:5.1f}kn" if sog is not None else "  ?  kn"
    cog_s = f"{cog:5.1f}°" if cog is not None else "  ?  °"

    parts = [
        f"{short_t:>8s}",
        f"MMSI {mmsi:>9}",
        f"{name:<20s}",
        f"({lat:8.4f}, {lon:9.4f})",
        f"sog={sog_s}",
        f"cog={cog_s}",
    ]
    if dest:
        parts.append(f"→ {dest}")
    return "  ".join(parts)


# ---------------------------------------------------------------------------
# Core stream
# ---------------------------------------------------------------------------


async def stream(
    api_key: str,
    bbox: tuple[float, float, float, float],
    throttle_sec: float,
    jsonl: bool,
    include_static: bool,
) -> None:
    """Connect, subscribe, drain forever (reconnecting on error)."""

    # aisstream wants [[[SW_lat, SW_lon], [NE_lat, NE_lon]]]
    min_lon, min_lat, max_lon, max_lat = bbox
    subscribe_msg = {
        "APIKey": api_key,
        "BoundingBoxes": [[[min_lat, min_lon], [max_lat, max_lon]]],
        "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
    }

    static_cache: dict[int, dict] = {}
    last_stored: dict[int, float] = {}

    backoff = 1
    while True:
        try:
            async with websockets.connect(
                "wss://stream.aisstream.io/v0/stream",
                ping_interval=30,
                ping_timeout=20,
            ) as ws:
                await ws.send(json.dumps(subscribe_msg))
                log.info(
                    "Connected. bbox=(W=%.3f S=%.3f E=%.3f N=%.3f) throttle=%gs",
                    *bbox, throttle_sec,
                )
                backoff = 1

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    if "error" in msg:
                        log.error("server error: %s", msg["error"])
                        break

                    msg_type = msg.get("MessageType")
                    meta_data = msg.get("MetaData") or {}
                    mmsi = meta_data.get("MMSI")
                    if not mmsi:
                        continue

                    # -- ShipStaticData: enrich the cache, optionally log --
                    if msg_type == "ShipStaticData":
                        body = (msg.get("Message") or {}).get("ShipStaticData") or {}
                        dim = body.get("Dimension") or {}
                        static_cache[mmsi] = {
                            "ship_name": (body.get("Name") or "").strip(),
                            "ship_type": body.get("Type"),
                            "destination": (body.get("Destination") or "").strip(),
                            "draught": body.get("MaximumStaticDraught"),
                            "length": _dim_sum(dim, "A", "B"),
                            "width": _dim_sum(dim, "C", "D"),
                        }
                        if include_static:
                            cached = static_cache[mmsi]
                            line = (
                                f"  STATIC  MMSI {mmsi:>9}  "
                                f"{cached['ship_name']:<20s}  "
                                f"type={cached['ship_type']}  "
                                f"L={cached['length']}m W={cached['width']}m  "
                                f"→ {cached['destination']}"
                            )
                            print(line, flush=True)
                        continue

                    # -- PositionReport: throttle, enrich, emit --
                    if msg_type != "PositionReport":
                        continue

                    pos = (msg.get("Message") or {}).get("PositionReport") or {}
                    lat = pos.get("Latitude")
                    lon = pos.get("Longitude")
                    if lat is None or lon is None:
                        continue

                    now_mono = time.monotonic()
                    if now_mono - last_stored.get(mmsi, 0) < throttle_sec:
                        continue
                    last_stored[mmsi] = now_mono

                    static = static_cache.get(mmsi, {})
                    ship_name = (
                        (meta_data.get("ShipName") or "").strip()
                        or static.get("ship_name", "")
                    )

                    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    ts = normalize_timestamp(meta_data.get("time_utc", "")) or now_iso

                    record = {
                        "mmsi": mmsi,
                        "time_utc": ts,
                        "received_at": now_iso,
                        "lat": lat,
                        "lon": lon,
                        "sog": pos.get("Sog"),
                        "cog": pos.get("Cog"),
                        "heading": pos.get("TrueHeading"),
                        "ship_name": ship_name,
                        "ship_type": static.get("ship_type"),
                        "destination": static.get("destination"),
                        "draught": static.get("draught"),
                        "length": static.get("length"),
                        "width": static.get("width"),
                    }

                    line = (
                        json.dumps(record, default=str, ensure_ascii=False)
                        if jsonl
                        else format_human(record)
                    )
                    print(line, flush=True)

        except asyncio.CancelledError:
            log.info("cancelled")
            raise
        except (
            websockets.exceptions.ConnectionClosed,
            OSError,
            ConnectionError,
        ) as e:
            log.warning("Connection lost: %s — reconnecting in %ds", e, backoff)
        except Exception as e:
            log.error("Unexpected: %s — reconnecting in %ds", e, backoff)
        await asyncio.sleep(backoff)
        backoff = min(60, backoff * 2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--bbox", nargs=4, type=float,
        metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"),
        default=[54.5, 25.0, 57.5, 27.5],
        help="Bounding box (lon/lat). Default: Strait of Hormuz.",
    )
    p.add_argument(
        "--throttle", type=float, default=120.0,
        help="Per-MMSI minimum interval between emitted positions (seconds). "
             "Default 120. Use 0 to emit everything.",
    )
    p.add_argument(
        "--jsonl", action="store_true",
        help="Emit one JSON record per line instead of human-readable text.",
    )
    p.add_argument(
        "--include-static", action="store_true",
        help="Also print a line whenever ShipStaticData arrives (not just positions).",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true",
        help="DEBUG-level logging on stderr.",
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,  # keep stdout clean for the data stream
    )

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    api_key = os.environ.get("AISSTREAM_API_KEY")
    if not api_key:
        log.error("AISSTREAM_API_KEY missing (set in .env or environment).")
        return 2

    min_lon, min_lat, max_lon, max_lat = args.bbox
    if not (-180 <= min_lon < max_lon <= 180 and -90 <= min_lat < max_lat <= 90):
        log.error("Invalid bbox %s", args.bbox)
        return 2

    try:
        asyncio.run(stream(
            api_key=api_key,
            bbox=tuple(args.bbox),  # type: ignore
            throttle_sec=args.throttle,
            jsonl=args.jsonl,
            include_static=args.include_static,
        ))
    except KeyboardInterrupt:
        log.info("interrupted")
    return 0


if __name__ == "__main__":
    sys.exit(main())