#!/usr/bin/env python3
"""
ais_listener.py — Background AIS data collector with bbox filtering.

Subscribes to one or more AIS sources concurrently, filters every message by
a bounding box, and persists what matches to hourly JSONL files for later
fusion with SAR (download_sar.py / visualisation.py).

Sources:
  aisstream    — wss://stream.aisstream.io  (real-time, free with API key)
  nmea-tcp     — Any NMEA 0183 AIS feed over TCP (AIS Catcher, dAISy, etc.)
  replay       — Replay an existing JSONL file (testing / historical)

Output layout (UTC):
  <output-dir>/
      YYYY-MM-DD/
          HH.jsonl       # one record per line, append-only, line-flushed

Each record (canonical schema across all sources):
  { "source": "...",
    "received_at": "...",          # ISO timestamp when *we* got it
    "type": "...",                 # AIS message type / pyais msg_type
    "mmsi": ...,
    "lat": ...,
    "lon": ...,
    "ship_name": "...",            # if available
    "time_utc": "...",             # AIS broadcast time if available
    "raw": { ... }                 # original payload, kept intact
  }

Usage:
  # Default (aisstream.io alone)
  python ais_listener.py --bbox 54.5 25.0 57.5 27.5

  # Multiple sources, multiple NMEA endpoints
  python ais_listener.py --bbox 54.5 25.0 57.5 27.5 \\
      --sources aisstream nmea-tcp \\
      --nmea-tcp 192.168.1.10:4002 --nmea-tcp my-rpi.local:10110

  # Replay a previous capture (no live deps)
  python ais_listener.py --bbox 54.5 25.0 57.5 27.5 \\
      --sources replay --replay-file ais_data/2026-05-24/14.jsonl

  # Run detached
  nohup python ais_listener.py --bbox 54.5 25.0 57.5 27.5 > ais.log 2>&1 &

Credentials (.env):
  AISSTREAM_API_KEY=...

Requirements:
  pip install websockets python-dotenv
  pip install pyais         # only needed if you use --sources nmea-tcp
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("ais")


# ---------------------------------------------------------------------------
# Bbox + normalization
# ---------------------------------------------------------------------------

Bbox = tuple[float, float, float, float]  # (min_lon, min_lat, max_lon, max_lat)


def in_bbox(lon: float, lat: float, bbox: Bbox) -> bool:
    return bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def normalize(source: str, raw: dict) -> Optional[dict]:
    """Source-specific payload → canonical record. None if no usable position."""
    if source == "aisstream":
        meta = raw.get("MetaData") or {}
        lat = meta.get("latitude")
        lon = meta.get("longitude")
        if lat is None or lon is None:
            return None
        return {
            "source": source,
            "received_at": _now_iso(),
            "type": raw.get("MessageType"),
            "mmsi": meta.get("MMSI"),
            "lat": float(lat),
            "lon": float(lon),
            "ship_name": (meta.get("ShipName") or "").strip() or None,
            "time_utc": meta.get("time_utc"),
            "raw": raw,
        }

    if source == "nmea-tcp":
        # `raw` is the dict produced by pyais decode().asdict()
        lat = raw.get("lat")
        lon = raw.get("lon")
        if lat is None or lon is None:
            return None
        return {
            "source": source,
            "received_at": _now_iso(),
            "type": str(raw.get("msg_type")) if raw.get("msg_type") is not None else None,
            "mmsi": raw.get("mmsi"),
            "lat": float(lat),
            "lon": float(lon),
            "ship_name": (raw.get("shipname") or "").strip() or None,
            "time_utc": None,
            "raw": raw,
        }

    if source == "replay":
        # Already in canonical shape
        if "lat" in raw and "lon" in raw:
            return raw
        return None

    return None


# ---------------------------------------------------------------------------
# Source base + implementations
# ---------------------------------------------------------------------------


class Source:
    name: str = "?"

    async def run(self, bbox: Bbox, queue: asyncio.Queue, stats: dict) -> None:
        raise NotImplementedError

    async def _emit(self, raw: dict, bbox: Bbox, queue: asyncio.Queue, stats: dict) -> None:
        """Normalize → bbox-filter → enqueue. Updates stats."""
        msg = normalize(self.name, raw)
        if msg is None:
            return
        stats[self.name]["received"] += 1
        if in_bbox(msg["lon"], msg["lat"], bbox):
            stats[self.name]["kept"] += 1
            if msg.get("mmsi") is not None:
                stats[self.name]["mmsis"].add(msg["mmsi"])
            await queue.put(msg)


class AISStreamSource(Source):
    name = "aisstream"
    URL = "wss://stream.aisstream.io/v0/stream"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def run(self, bbox, queue, stats):
        try:
            import websockets
        except ImportError:
            log.error("[%s] `websockets` not installed (pip install websockets).", self.name)
            return

        # aisstream uses [[lat, lon], [lat, lon]] in SW → NE order
        minx, miny, maxx, maxy = bbox
        subscription = json.dumps({
            "APIKey": self.api_key,
            "BoundingBoxes": [[[miny, minx], [maxy, maxx]]],
        })

        backoff = 1
        while True:
            try:
                async with websockets.connect(
                    self.URL, ping_interval=30, ping_timeout=20, close_timeout=5
                ) as ws:
                    await ws.send(subscription)
                    log.info("[%s] connected", self.name)
                    backoff = 1
                    async for raw_text in ws:
                        try:
                            raw = json.loads(raw_text)
                        except json.JSONDecodeError:
                            continue
                        # Server errors arrive as plain dicts with "error" key
                        if "error" in raw:
                            log.error("[%s] server error: %s", self.name, raw["error"])
                            break
                        await self._emit(raw, bbox, queue, stats)
            except asyncio.CancelledError:
                log.info("[%s] cancelled", self.name)
                raise
            except Exception as e:
                log.warning("[%s] %s — reconnecting in %ds", self.name, e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(60, backoff * 2)


class NMEATCPSource(Source):
    name = "nmea-tcp"

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port

    async def run(self, bbox, queue, stats):
        try:
            from pyais import decode as ais_decode  # type: ignore
        except ImportError:
            log.error(
                "[%s] `pyais` not installed (pip install pyais). "
                "This source needs it to decode NMEA 0183 AIVDM/AIVDO sentences.",
                self.name,
            )
            return

        endpoint = f"{self.host}:{self.port}"
        backoff = 1
        while True:
            try:
                log.info("[%s] connecting to %s", self.name, endpoint)
                reader, writer = await asyncio.open_connection(self.host, self.port)
                log.info("[%s] connected to %s", self.name, endpoint)
                backoff = 1

                # Fragment buffer: messages with n_total > 1 arrive on consecutive
                # !AIVDM lines and have to be decoded together.
                fragments: dict[str, list[Optional[bytes]]] = {}

                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    line = line.strip()
                    if not (line.startswith(b"!AIVDM") or line.startswith(b"!AIVDO")):
                        continue

                    parts = line.split(b",")
                    if len(parts) < 7:
                        continue
                    try:
                        n_total = int(parts[1])
                        n = int(parts[2])
                        seq = parts[3].decode("ascii", errors="ignore") or "_"
                    except (ValueError, UnicodeDecodeError):
                        continue

                    if n_total == 1:
                        sentences = [line]
                    else:
                        buf = fragments.setdefault(seq, [None] * n_total)
                        if 1 <= n <= n_total:
                            buf[n - 1] = line
                        if all(s is not None for s in buf):
                            sentences = fragments.pop(seq)  # type: ignore
                        else:
                            continue

                    try:
                        decoded = ais_decode(*sentences).asdict()  # type: ignore
                    except Exception:
                        # Bad checksum, unsupported type, etc. — keep streaming.
                        continue

                    await self._emit(decoded, bbox, queue, stats)

                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                log.info("[%s] connection closed by peer", self.name)
            except asyncio.CancelledError:
                log.info("[%s] cancelled", self.name)
                raise
            except (OSError, ConnectionError) as e:
                log.warning("[%s] %s — reconnecting in %ds", self.name, e, backoff)
            except Exception as e:
                log.warning("[%s] unexpected %s — reconnecting in %ds",
                            self.name, e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(60, backoff * 2)


class ReplaySource(Source):
    name = "replay"

    def __init__(self, path: Path, delay: float = 0.0):
        self.path = path
        self.delay = delay  # seconds between messages, 0 = as fast as possible

    async def run(self, bbox, queue, stats):
        if not self.path.exists():
            log.error("[%s] file not found: %s", self.name, self.path)
            return
        log.info("[%s] replaying %s", self.name, self.path)
        n = 0
        with open(self.path, "r") as f:
            for raw_line in f:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    raw = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                await self._emit(raw, bbox, queue, stats)
                n += 1
                if self.delay:
                    await asyncio.sleep(self.delay)
                elif n % 1000 == 0:
                    await asyncio.sleep(0)  # cooperative yield
        log.info("[%s] replay complete (%d lines)", self.name, n)


# ---------------------------------------------------------------------------
# Writer + stats
# ---------------------------------------------------------------------------


async def writer_task(queue: asyncio.Queue, output_dir: Path, stats: dict) -> None:
    """Drain the queue into hourly JSONL files."""
    current_path: Optional[Path] = None
    current_fh = None
    try:
        while True:
            msg = await queue.get()
            try:
                ts = datetime.now(timezone.utc)
                date_dir = output_dir / ts.strftime("%Y-%m-%d")
                hour_path = date_dir / f"{ts.strftime('%H')}.jsonl"

                if current_path != hour_path:
                    if current_fh:
                        current_fh.close()
                    date_dir.mkdir(parents=True, exist_ok=True)
                    current_fh = open(hour_path, "a", buffering=1, encoding="utf-8")
                    current_path = hour_path
                    log.info("[writer] writing to %s", hour_path)

                current_fh.write(json.dumps(msg, default=str, ensure_ascii=False) + "\n")
                stats["_writer"]["written"] += 1
            finally:
                queue.task_done()
    except asyncio.CancelledError:
        log.info("[writer] cancelled, flushing")
        raise
    finally:
        if current_fh:
            current_fh.close()


async def stats_task(stats: dict, interval: int) -> None:
    """Log throughput every `interval` seconds."""
    try:
        while True:
            await asyncio.sleep(interval)
            parts = []
            for name in sorted(k for k in stats if not k.startswith("_")):
                s = stats[name]
                parts.append(
                    f"{name}: kept {s['kept']}/{s['received']} "
                    f"({len(s['mmsis'])} MMSIs)"
                )
            written = stats.get("_writer", {}).get("written", 0)
            log.info("[stats] %s | written %d", " | ".join(parts) or "(no data)", written)
    except asyncio.CancelledError:
        raise


# ---------------------------------------------------------------------------
# CLI / main
# ---------------------------------------------------------------------------


def build_sources(args, env: dict) -> list[Source]:
    sources: list[Source] = []

    if "aisstream" in args.sources:
        key = env.get("AISSTREAM_API_KEY") or os.getenv("AISSTREAM_API_KEY")
        if not key:
            log.error("AISSTREAM_API_KEY missing (set in .env or environment).")
            sys.exit(2)
        sources.append(AISStreamSource(key))

    if "nmea-tcp" in args.sources:
        if not args.nmea_tcp:
            log.error("--sources includes nmea-tcp but no --nmea-tcp HOST:PORT given.")
            sys.exit(2)
        for endpoint in args.nmea_tcp:
            if ":" not in endpoint:
                log.error("Bad --nmea-tcp '%s' (need HOST:PORT)", endpoint)
                sys.exit(2)
            host, port_s = endpoint.rsplit(":", 1)
            try:
                port = int(port_s)
            except ValueError:
                log.error("Bad port in --nmea-tcp '%s'", endpoint)
                sys.exit(2)
            sources.append(NMEATCPSource(host, port))

    if "replay" in args.sources:
        if not args.replay_file:
            log.error("--sources includes replay but no --replay-file given.")
            sys.exit(2)
        sources.append(ReplaySource(args.replay_file, delay=args.replay_delay))

    return sources


async def main_async(args) -> int:
    # bbox validation
    bbox: Bbox = tuple(args.bbox)  # type: ignore
    minx, miny, maxx, maxy = bbox
    if not (-180 <= minx < maxx <= 180 and -90 <= miny < maxy <= 90):
        log.error("Invalid bbox %s — need MIN_LON<MAX_LON in [-180,180], "
                  "MIN_LAT<MAX_LAT in [-90,90].", bbox)
        return 2

    # Credentials
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    env = {k: v for k, v in os.environ.items()}

    sources = build_sources(args, env)
    if not sources:
        log.error("No sources configured.")
        return 2

    args.output_dir.mkdir(parents=True, exist_ok=True)

    queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)
    stats: dict = defaultdict(lambda: {"received": 0, "kept": 0, "mmsis": set()})
    stats["_writer"] = {"written": 0}

    log.info("Bbox (lon/lat): %s", bbox)
    log.info("Output: %s", args.output_dir)
    log.info("Sources: %s", ", ".join(s.name for s in sources))

    # Build tasks
    tasks: list[asyncio.Task] = []
    for src in sources:
        tasks.append(asyncio.create_task(src.run(bbox, queue, stats), name=src.name))
    tasks.append(asyncio.create_task(writer_task(queue, args.output_dir, stats),
                                     name="writer"))
    tasks.append(asyncio.create_task(stats_task(stats, args.stats_interval),
                                     name="stats"))

    # Shutdown handling
    stop = asyncio.Event()

    def _shutdown(sig_name: str) -> None:
        if not stop.is_set():
            log.info("Received %s — shutting down", sig_name)
            stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown, sig.name)
        except (NotImplementedError, RuntimeError):
            pass  # e.g. Windows

    # Wait until either a signal arrives or all sources finish naturally
    # (replay-only runs end on EOF).
    async def _watch_sources():
        source_tasks = [t for t in tasks if t.get_name() not in ("writer", "stats")]
        await asyncio.gather(*source_tasks, return_exceptions=True)
        log.info("All sources finished")

    watcher = asyncio.create_task(_watch_sources())
    stopper = asyncio.create_task(stop.wait())
    await asyncio.wait([watcher, stopper], return_when=asyncio.FIRST_COMPLETED)

    # Drain the queue, then cancel writer + stats
    log.info("Draining queue (%d pending)…", queue.qsize())
    try:
        await asyncio.wait_for(queue.join(), timeout=10)
    except asyncio.TimeoutError:
        log.warning("Drain timed out; %d msgs may be lost", queue.qsize())

    for t in tasks:
        if not t.done():
            t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    # Final summary
    log.info("─" * 60)
    log.info("Final stats:")
    for name in sorted(k for k in stats if not k.startswith("_")):
        s = stats[name]
        log.info("  %-12s  received=%d  kept=%d  unique MMSI=%d",
                 name, s["received"], s["kept"], len(s["mmsis"]))
    log.info("  written: %d records", stats["_writer"]["written"])
    return 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--bbox", nargs=4, type=float, required=True,
        metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"),
        help="Bounding box to filter all incoming messages.",
    )
    p.add_argument(
        "--sources", nargs="+", default=["aisstream"],
        choices=["aisstream", "nmea-tcp", "replay"],
        help="Which sources to run concurrently (default: aisstream).",
    )
    p.add_argument(
        "--nmea-tcp", action="append", metavar="HOST:PORT",
        help="NMEA-TCP endpoint(s). Repeat for multiple receivers.",
    )
    p.add_argument(
        "--replay-file", type=Path,
        help="JSONL file to replay (canonical schema).",
    )
    p.add_argument(
        "--replay-delay", type=float, default=0.0,
        help="Sleep this many seconds between replayed messages (default 0).",
    )
    p.add_argument(
        "--output-dir", type=Path, default=Path("ais_data"),
        help="Where to write hourly JSONL files (default ./ais_data).",
    )
    p.add_argument(
        "--stats-interval", type=int, default=60,
        help="Seconds between stats log lines (default 60).",
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
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        log.info("Interrupted")
        return 0


if __name__ == "__main__":
    sys.exit(main())