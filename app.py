"""
Strait of Hormuz Ship Tracker
==============================
A Streamlit app that connects to aisstream.io's free WebSocket API
to capture AIS data from ships near the Strait of Hormuz, classifying
them as "Waiting/Anchored" or "Transiting" based on speed and nav status.

Requirements:
  - A free API key from https://aisstream.io (sign in with GitHub)
  - pip install streamlit websockets pydeck pandas

Run:
  streamlit run app.py
"""

import streamlit as st
import asyncio
import websockets
import json
import os
import threading
import time
import math
import pandas as pd
import pydeck as pdk
from datetime import datetime, timezone, timedelta
from collections import OrderedDict
from pathlib import Path

# ── Load .env from project folder ──
from dotenv import load_dotenv

_PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(_PROJECT_DIR / ".env")

# ──────────────────────────── CONFIG ────────────────────────────

# Strait of Hormuz bounding box [SW corner, NE corner]
HORMUZ_BBOX = [[25.0, 54.5], [27.5, 57.5]]

# Center of the strait (for map default view)
MAP_CENTER_LAT = 26.25
MAP_CENTER_LON = 56.0

# Speed threshold (knots) below which a ship is considered "waiting"
SPEED_THRESHOLD = 1.5

# Navigation statuses that indicate a ship is not transiting
ANCHORED_NAV_STATUSES = {0: "Under way using engine", 1: "At anchor", 5: "Moored"}

# How long to keep a ship in the tracker before considering it stale (seconds)
STALE_TIMEOUT = 600  # 10 minutes

# Max ships to hold in memory
MAX_SHIPS = 500

# ──────────────────── BASEMAP TILE PROVIDERS ────────────────────

TILE_PROVIDERS = {
    "🛰️ Satellite (ESRI)": {
        "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "attribution": "Esri World Imagery",
    },
    "🛰️ Satellite + Labels": {
        "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "labels_url": "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
        "attribution": "Esri World Imagery + Reference",
    },
    "🌍 Sentinel-2 Cloudless": {
        "url": "https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2021_3857/default/GoogleMapsCompatible/{z}/{y}/{x}.jpg",
        "attribution": "Sentinel-2 cloudless 2021 by EOX",
    },
    "🗺️ OpenStreetMap": {
        "url": "https://tile.openstreetmap.org/{z}/{y}/{x}.png",
        "attribution": "OpenStreetMap",
    },
    "🌑 Dark (CartoDB)": {
        "url": "https://basemaps.cartocdn.com/dark_all/{z}/{y}/{x}.png",
        "attribution": "CartoDB Dark Matter",
    },
}

# Shipping lane reference polygon (approximate TSS through Hormuz)
# Inbound lane (westbound toward Gulf) and outbound lane (eastbound toward Arabian Sea)
SHIPPING_LANES = {
    "Inbound (to Gulf)": [
        [26.08, 56.08], [26.23, 56.30], [26.42, 56.42],
        [26.55, 56.35], [26.40, 56.15], [26.20, 55.95], [26.08, 56.08],
    ],
    "Outbound (to Arabian Sea)": [
        [26.00, 56.20], [26.15, 56.42], [26.35, 56.55],
        [26.48, 56.48], [26.30, 56.28], [26.12, 56.08], [26.00, 56.20],
    ],
}


# ──────────────────────── SHIP STORE ────────────────────────────

class ShipStore:
    """Thread-safe store for ship position data."""

    def __init__(self, max_size=MAX_SHIPS):
        self._data: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()
        self._max_size = max_size
        self._msg_count = 0
        self._connected = False
        self._error = None

    def update(self, mmsi: str, info: dict):
        with self._lock:
            self._data[mmsi] = info
            self._data.move_to_end(mmsi)
            if len(self._data) > self._max_size:
                self._data.popitem(last=False)
            self._msg_count += 1

    def get_all(self) -> list[dict]:
        with self._lock:
            now = datetime.now(timezone.utc)
            return [
                v for v in self._data.values()
                if (now - v.get("last_seen", now)).total_seconds() < STALE_TIMEOUT
            ]

    @property
    def msg_count(self):
        with self._lock:
            return self._msg_count

    @property
    def connected(self):
        with self._lock:
            return self._connected

    @connected.setter
    def connected(self, val):
        with self._lock:
            self._connected = val

    @property
    def error(self):
        with self._lock:
            return self._error

    @error.setter
    def error(self, val):
        with self._lock:
            self._error = val


# ──────────────────── CLASSIFICATION LOGIC ──────────────────────

def classify_ship(speed_knots: float, nav_status: int) -> str:
    """Classify a ship as Waiting or Transiting."""
    if nav_status in (1, 5):  # At anchor or Moored
        return "Waiting / Anchored"
    if speed_knots < SPEED_THRESHOLD:
        return "Waiting / Anchored"
    return "Transiting"


def nav_status_label(code: int) -> str:
    labels = {
        0: "Under way (engine)",
        1: "At anchor",
        2: "Not under command",
        3: "Restricted maneuverability",
        4: "Constrained by draught",
        5: "Moored",
        6: "Aground",
        7: "Engaged in fishing",
        8: "Under way (sailing)",
        14: "AIS-SART",
        15: "Not defined",
    }
    return labels.get(code, f"Unknown ({code})")


# ────────────────── WEBSOCKET LISTENER ──────────────────────────

async def _ais_listener(api_key: str, store: ShipStore):
    """Async loop that connects to aisstream.io and populates the store."""
    url = "wss://stream.aisstream.io/v0/stream"
    subscribe_msg = {
        "APIKey": api_key,
        "BoundingBoxes": [HORMUZ_BBOX],
        "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
    }

    while True:
        try:
            store.error = None
            async with websockets.connect(url, ping_interval=20) as ws:
                await ws.send(json.dumps(subscribe_msg))
                store.connected = True

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    msg_type = msg.get("MessageType", "")
                    meta = msg.get("MetaData", {})
                    mmsi = str(meta.get("MMSI", ""))
                    if not mmsi:
                        continue

                    ship_name = meta.get("ShipName", "").strip()
                    lat = meta.get("latitude", meta.get("Latitude"))
                    lon = meta.get("longitude", meta.get("Longitude"))

                    if lat is None or lon is None:
                        continue

                    now = datetime.now(timezone.utc)

                    if msg_type == "PositionReport":
                        report = msg.get("Message", {}).get("PositionReport", {})
                        sog = report.get("Sog", 0)  # speed over ground in knots
                        cog = report.get("Cog", 0)  # course over ground
                        heading = report.get("TrueHeading", 0)
                        nav_status = report.get("NavigationalStatus", 15)

                        status = classify_ship(sog, nav_status)

                        store.update(mmsi, {
                            "mmsi": mmsi,
                            "name": ship_name if ship_name else mmsi,
                            "lat": lat,
                            "lon": lon,
                            "speed_kn": round(sog, 1),
                            "course": round(cog, 1),
                            "heading": heading,
                            "nav_status": nav_status_label(nav_status),
                            "status": status,
                            "last_seen": now,
                        })

                    elif msg_type == "ShipStaticData":
                        # Update name if we get static data
                        static = msg.get("Message", {}).get("ShipStaticData", {})
                        imo = static.get("ImoNumber", "")
                        ship_type = static.get("Type", 0)
                        # Only update name if we already have this ship
                        existing = store._data.get(mmsi)
                        if existing and ship_name:
                            existing["name"] = ship_name

        except websockets.exceptions.ConnectionClosed:
            store.connected = False
            store.error = "Connection closed — reconnecting in 5s…"
        except Exception as e:
            store.connected = False
            store.error = f"Error: {e} — reconnecting in 5s…"

        await asyncio.sleep(5)


def start_listener(api_key: str, store: ShipStore):
    """Run the async listener in a background thread."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_ais_listener(api_key, store))


# ──────────────────────── STREAMLIT UI ──────────────────────────

st.set_page_config(
    page_title="Hormuz Strait Ship Tracker",
    page_icon="🚢",
    layout="wide",
)

# Late import — sentinel module lives next to app.py
from sentinel import (
    search_catalog,
    get_cdse_token,
    get_sh_token,
    download_quicklook,
    fetch_processed_image,
    get_cache_stats,
    clear_cache,
    EVALSCRIPTS,
)
from PIL import Image

st.title("🚢 Strait of Hormuz — Ship Tracker & Satellite Monitor")

# ══════════════════════ SIDEBAR (shared) ════════════════════════

with st.sidebar:
    st.header("⚙️ AIS Settings")
    api_key = st.text_input(
        "AISStream API Key",
        value=os.getenv("AISSTREAM_API_KEY", ""),
        type="password",
        help="Get a free key at https://aisstream.io — sign in with GitHub.",
    )
    speed_thresh = st.slider(
        "Speed threshold (knots)",
        0.5, 5.0, SPEED_THRESHOLD, 0.5,
        help="Ships below this speed are classified as waiting.",
    )
    auto_refresh = st.checkbox("Auto-refresh every 10s", value=True)

    st.markdown("---")
    st.subheader("🗺️ Basemap")
    basemap_choice = st.selectbox(
        "Tile layer",
        list(TILE_PROVIDERS.keys()),
        index=0,
    )
    show_lanes = st.checkbox("Show shipping lane guides", value=True)
    show_bbox = st.checkbox("Show bounding box", value=True)

    st.markdown("---")
    st.subheader("🛰️ Copernicus (CDSE)")
    cdse_user = st.text_input(
        "CDSE Email",
        value=os.getenv("CDSE_USER", ""),
        help="Free account at https://dataspace.copernicus.eu",
    )
    cdse_pass = st.text_input(
        "CDSE Password",
        value=os.getenv("CDSE_PASSWORD", ""),
        type="password",
    )

    st.markdown("---")
    st.subheader("🔬 Sentinel Hub")
    st.caption("For rendered imagery via Process API. Create OAuth client at the [CDSE dashboard](https://shapps.dataspace.copernicus.eu/dashboard/).")
    sh_client_id = st.text_input(
        "SH Client ID",
        value=os.getenv("SH_CLIENT_ID", ""),
        type="password",
    )
    sh_client_secret = st.text_input(
        "SH Client Secret",
        value=os.getenv("SH_CLIENT_SECRET", ""),
        type="password",
    )

    st.markdown("---")
    # Show .env status
    env_path = _PROJECT_DIR / ".env"
    if env_path.exists():
        loaded_keys = [k for k in ("AISSTREAM_API_KEY", "CDSE_USER", "CDSE_PASSWORD",
                                    "SH_CLIENT_ID", "SH_CLIENT_SECRET")
                       if os.getenv(k)]
        st.success(f"`.env` loaded ({len(loaded_keys)}/5 keys set)")
    else:
        st.caption("💡 Create a `.env` file to avoid re-entering credentials. See `.env.example`.")

    st.markdown(
        "**Bounding box**\n\n"
        f"SW: `{HORMUZ_BBOX[0][0]}°N, {HORMUZ_BBOX[0][1]}°E`\n\n"
        f"NE: `{HORMUZ_BBOX[1][0]}°N, {HORMUZ_BBOX[1][1]}°E`"
    )

# ══════════════════════ TOP-LEVEL TABS ══════════════════════════

main_tab_ais, main_tab_sat = st.tabs(["📡 Live AIS Tracker", "🛰️ Satellite Imagery"])

# ┌─────────────────────────────────────────────────────────────┐
# │  TAB 1 — LIVE AIS TRACKER                                  │
# └─────────────────────────────────────────────────────────────┘

with main_tab_ais:

    # ── Initialize store & background thread ──
    if "ship_store" not in st.session_state:
        st.session_state.ship_store = ShipStore()
    if "listener_started" not in st.session_state:
        st.session_state.listener_started = False

    store: ShipStore = st.session_state.ship_store

    if api_key and not st.session_state.listener_started:
        thread = threading.Thread(
            target=start_listener, args=(api_key, store), daemon=True
        )
        thread.start()
        st.session_state.listener_started = True
        st.toast("🔌 Connecting to AIS stream…")

    if not api_key:
        st.info(
            "👈 Enter your **free** AISStream API key in the sidebar to start tracking.\n\n"
            "Sign up at [aisstream.io](https://aisstream.io) (GitHub login)."
        )
    else:
        # ── Connection status ──
        col_status, col_msgs, col_ships = st.columns(3)
        ships = store.get_all()

        with col_status:
            if store.connected:
                st.success("🟢 Connected")
            elif store.error:
                st.warning(store.error)
            else:
                st.info("🔄 Connecting…")
        with col_msgs:
            st.metric("Messages received", f"{store.msg_count:,}")
        with col_ships:
            st.metric("Ships tracked", len(ships))

        if not ships:
            st.info("Waiting for AIS data… Ships will appear as they broadcast.")
        else:
            # ── Build DataFrame ──
            df = pd.DataFrame(ships)
            df["status"] = df.apply(
                lambda r: "Waiting / Anchored"
                if r["speed_kn"] < speed_thresh or r["nav_status"] in ("At anchor", "Moored")
                else "Transiting",
                axis=1,
            )
            waiting = df[df["status"] == "Waiting / Anchored"]
            transiting = df[df["status"] == "Transiting"]

            c1, c2 = st.columns(2)
            c1.metric("🔴 Waiting / Anchored", len(waiting))
            c2.metric("🟢 Transiting", len(transiting))

            # ── Map ──
            st.subheader("Live Map")

            df["color_r"] = df["status"].apply(lambda s: 220 if "Waiting" in s else 40)
            df["color_g"] = df["status"].apply(lambda s: 50 if "Waiting" in s else 180)
            df["color_b"] = df["status"].apply(lambda s: 50 if "Waiting" in s else 60)

            layers = []

            # 1) Basemap tile layer
            tile_cfg = TILE_PROVIDERS[basemap_choice]
            layers.append(
                pdk.Layer(
                    "TileLayer",
                    data=None,
                    get_tile_data=tile_cfg["url"],
                    min_zoom=0, max_zoom=19, tile_size=256,
                )
            )
            if "labels_url" in tile_cfg:
                layers.append(
                    pdk.Layer(
                        "TileLayer",
                        data=None,
                        get_tile_data=tile_cfg["labels_url"],
                        min_zoom=0, max_zoom=19, tile_size=256,
                    )
                )

            # 2) Bounding box
            if show_bbox:
                sw_lat, sw_lon = HORMUZ_BBOX[0]
                ne_lat, ne_lon = HORMUZ_BBOX[1]
                bbox_path = [{"path": [
                    [sw_lon, sw_lat], [ne_lon, sw_lat],
                    [ne_lon, ne_lat], [sw_lon, ne_lat], [sw_lon, sw_lat],
                ]}]
                layers.append(pdk.Layer(
                    "PathLayer", data=bbox_path, get_path="path",
                    get_color=[255, 255, 0, 120], width_min_pixels=2, get_width=80,
                ))

            # 3) Shipping lanes
            if show_lanes:
                lane_data = []
                lane_colors = {
                    "Inbound (to Gulf)": [100, 180, 255, 100],
                    "Outbound (to Arabian Sea)": [255, 180, 100, 100],
                }
                for lane_name, coords in SHIPPING_LANES.items():
                    lane_data.append({
                        "path": [[lon, lat] for lat, lon in coords],
                        "color": lane_colors.get(lane_name, [200, 200, 200, 80]),
                    })
                layers.append(pdk.Layer(
                    "PathLayer", data=lane_data, get_path="path",
                    get_color="color", width_min_pixels=3, get_width=300,
                ))

            # 4) Ship markers
            layers.append(pdk.Layer(
                "ScatterplotLayer", data=df,
                get_position=["lon", "lat"],
                get_fill_color=["color_r", "color_g", "color_b", 200],
                get_radius=800, pickable=True, auto_highlight=True,
            ))

            # 5) Heading arrows
            transiting_wh = transiting.copy()
            if not transiting_wh.empty:
                transiting_wh["arrow_lat"] = transiting_wh.apply(
                    lambda r: r["lat"] + 0.015 * math.cos(math.radians(r["course"])), axis=1)
                transiting_wh["arrow_lon"] = transiting_wh.apply(
                    lambda r: r["lon"] + 0.015 * math.sin(math.radians(r["course"])), axis=1)
                layers.append(pdk.Layer(
                    "ScatterplotLayer", data=transiting_wh,
                    get_position=["arrow_lon", "arrow_lat"],
                    get_fill_color=[40, 220, 80, 160], get_radius=350, pickable=False,
                ))

            view = pdk.ViewState(latitude=MAP_CENTER_LAT, longitude=MAP_CENTER_LON, zoom=7, pitch=0)
            tooltip = {
                "html": "<b>{name}</b><br/>MMSI: {mmsi}<br/>Speed: {speed_kn} kn<br/>"
                        "Course: {course}°<br/>Nav: {nav_status}<br/>Status: <b>{status}</b>",
                "style": {"backgroundColor": "#1a1a2e", "color": "white"},
            }
            st.pydeck_chart(pdk.Deck(
                layers=layers, initial_view_state=view, tooltip=tooltip,
                map_provider=None, parameters={"cull": True},
            ))
            st.caption(f"Basemap: {tile_cfg['attribution']}")

            # ── Data tables ──
            st.subheader("Ship Details")
            tab_all, tab_waiting, tab_transit = st.tabs(
                ["All Ships", "🔴 Waiting / Anchored", "🟢 Transiting"]
            )
            display_cols = ["mmsi", "name", "speed_kn", "course", "nav_status", "status", "lat", "lon"]
            with tab_all:
                st.dataframe(df[display_cols].sort_values("speed_kn"), use_container_width=True, hide_index=True)
            with tab_waiting:
                st.dataframe(waiting[display_cols].sort_values("speed_kn"), use_container_width=True, hide_index=True)
            with tab_transit:
                st.dataframe(transiting[display_cols].sort_values("speed_kn", ascending=False), use_container_width=True, hide_index=True)

            st.download_button(
                "📥 Download CSV",
                df[display_cols].to_csv(index=False),
                file_name=f"hormuz_ships_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
                mime="text/csv",
            )


# ┌─────────────────────────────────────────────────────────────┐
# │  TAB 2 — SATELLITE IMAGERY                                 │
# └─────────────────────────────────────────────────────────────┘

with main_tab_sat:

    st.caption(
        "Search and render Sentinel-1 (SAR) & Sentinel-2 (optical) imagery "
        "over the Strait of Hormuz via the free **Copernicus Data Space Ecosystem**."
    )

    # ── Credentials gate ──
    has_cdse = bool(cdse_user and cdse_pass)
    has_sh = bool(sh_client_id and sh_client_secret)

    if not has_cdse:
        st.info(
            "👈 Enter your **Copernicus Data Space** credentials in the sidebar.\n\n"
            "Free account → [dataspace.copernicus.eu](https://dataspace.copernicus.eu)"
        )

    # ── Search controls ──
    st.subheader("🔍 Search Available Scenes")

    s_col1, s_col2, s_col3, s_col4 = st.columns([2, 2, 1, 1])
    with s_col1:
        sat_choice = st.selectbox("Satellite", ["SENTINEL-2", "SENTINEL-1"])
    with s_col2:
        today = datetime.now().date()
        date_range = st.date_input(
            "Date range",
            value=(today - timedelta(days=14), today),
            max_value=today,
        )
    with s_col3:
        cloud_max = st.slider("Max cloud %", 0, 100, 30, disabled=(sat_choice != "SENTINEL-2"))
    with s_col4:
        max_results = st.selectbox("Max results", [5, 10, 20, 50], index=1)

    # Handle single-date selection
    if isinstance(date_range, tuple) and len(date_range) == 2:
        d_from, d_to = date_range
    else:
        d_from = d_to = date_range

    bbox_tuple = (HORMUZ_BBOX[0][0], HORMUZ_BBOX[0][1], HORMUZ_BBOX[1][0], HORMUZ_BBOX[1][1])

    # ── Run search ──
    products = []
    if has_cdse and st.button("🔎 Search catalog", type="primary"):
        try:
            products = search_catalog(
                collection=sat_choice,
                bbox=bbox_tuple,
                start_date=d_from.isoformat(),
                end_date=d_to.isoformat(),
                cloud_cover_max=cloud_max,
                limit=max_results,
            )
            st.session_state["sat_products"] = products
        except Exception as e:
            st.error(f"Catalog search failed: {e}")

    # Persist results across reruns
    products = st.session_state.get("sat_products", [])

    if products:
        st.success(f"Found **{len(products)}** scenes")

        # ── Results table ──
        results_df = pd.DataFrame(products)
        results_df["date"] = pd.to_datetime(results_df["start"]).dt.strftime("%Y-%m-%d %H:%M")
        show_cols = ["date", "name", "cloud_cover", "size_mb", "online"]
        st.dataframe(results_df[show_cols], use_container_width=True, hide_index=True)

        # ── Quicklook browser ──
        st.subheader("🖼️ Quicklook Previews")
        st.caption("Thumbnails are cached on disk — downloading once, then served locally.")

        if has_cdse:
            try:
                token = get_cdse_token(cdse_user, cdse_pass)
            except Exception as e:
                st.error(f"Auth failed: {e}")
                token = None

            if token:
                ql_cols = st.columns(min(len(products), 4))
                for idx, prod in enumerate(products[:8]):
                    col = ql_cols[idx % len(ql_cols)]
                    with col:
                        ql_path = download_quicklook(prod["id"], token)
                        if ql_path and ql_path.exists():
                            st.image(str(ql_path), caption=prod["name"][:40], use_container_width=True)
                        else:
                            st.caption(f"No quicklook: {prod['name'][:30]}")

        # ── Sentinel Hub rendered imagery ──
        st.subheader("🛰️ Rendered Imagery (Sentinel Hub Process API)")

        if not has_sh:
            st.info(
                "To render full imagery over the Hormuz area, enter your **Sentinel Hub** "
                "OAuth credentials in the sidebar.\n\n"
                "Create a free OAuth client at the "
                "[CDSE Dashboard](https://shapps.dataspace.copernicus.eu/dashboard/) → "
                "User Settings → OAuth Clients."
            )
        else:
            # Pick evalscript based on satellite
            if sat_choice == "SENTINEL-2":
                script_options = {
                    "True Color (RGB)": "sentinel-2-true-color",
                    "Ship-Enhanced (NDWI contrast)": "sentinel-2-enhanced",
                }
            else:
                script_options = {
                    "VV Backscatter (grayscale)": "sentinel-1-vv",
                    "Dual-pol Ship Enhance (VV+VH)": "sentinel-1-ship-enhance",
                }

            r_col1, r_col2, r_col3 = st.columns(3)
            with r_col1:
                render_style = st.selectbox("Rendering style", list(script_options.keys()))
            with r_col2:
                render_res = st.selectbox("Resolution", [512, 1024, 2048], index=1)
            with r_col3:
                mosaic_order = st.selectbox(
                    "Mosaicking",
                    ["leastCC", "mostRecent", "leastRecent"],
                    help="leastCC = least cloudy pixel; mostRecent = latest acquisition",
                )

            # Scene picker — let user choose a date range from the catalog results
            if products:
                scene_dates = [p["start"][:10] for p in products]
                unique_dates = sorted(set(scene_dates), reverse=True)
                sel_date = st.selectbox(
                    "Scene date (from catalog results)",
                    unique_dates,
                    help="Imagery is fetched for a 1-day window around this date.",
                )
                render_from = sel_date
                render_to = sel_date
            else:
                render_from = d_from.isoformat()
                render_to = d_to.isoformat()

            if st.button("🖼️ Fetch rendered image", type="primary"):
                with st.spinner("Authenticating with Sentinel Hub…"):
                    try:
                        sh_token = get_sh_token(sh_client_id, sh_client_secret)
                    except Exception as e:
                        st.error(f"Sentinel Hub auth failed: {e}")
                        sh_token = None

                if sh_token:
                    collection_map = {"SENTINEL-2": "sentinel-2-l2a", "SENTINEL-1": "sentinel-1-grd"}
                    with st.spinner("Requesting image from Process API (cached 24h)…"):
                        img_path = fetch_processed_image(
                            sh_token=sh_token,
                            collection=collection_map[sat_choice],
                            bbox=bbox_tuple,
                            date_from=render_from,
                            date_to=render_to,
                            evalscript_name=script_options[render_style],
                            width=render_res,
                            height=render_res,
                            mosaicking=mosaic_order,
                            max_cache_age_hours=24,
                        )

                    if img_path and img_path.exists():
                        st.session_state["last_rendered_image"] = str(img_path)
                        st.session_state["last_render_meta"] = {
                            "satellite": sat_choice,
                            "date": render_from,
                            "style": render_style,
                            "bbox": bbox_tuple,
                        }
                    else:
                        st.warning("No image returned — the date range may have no data or too much cloud cover.")

            # ── Display rendered image ──
            rendered_path = st.session_state.get("last_rendered_image")
            render_meta = st.session_state.get("last_render_meta", {})

            if rendered_path and os.path.exists(rendered_path):
                st.markdown(
                    f"**{render_meta.get('satellite', '')}** · "
                    f"{render_meta.get('date', '')} · "
                    f"{render_meta.get('style', '')}"
                )

                # Show as image
                img = Image.open(rendered_path)
                st.image(img, use_container_width=True)

                # Also offer as map overlay using BitmapLayer
                with st.expander("🗺️ Overlay on map"):
                    sw_lat, sw_lon, ne_lat, ne_lon = bbox_tuple
                    overlay_layers = [
                        pdk.Layer(
                            "TileLayer",
                            data=None,
                            get_tile_data=TILE_PROVIDERS["🌑 Dark (CartoDB)"]["url"],
                            min_zoom=0, max_zoom=19, tile_size=256,
                        ),
                        pdk.Layer(
                            "BitmapLayer",
                            image=rendered_path,
                            bounds=[[sw_lon, sw_lat], [sw_lon, ne_lat],
                                    [ne_lon, ne_lat], [ne_lon, sw_lat]],
                            opacity=0.85,
                        ),
                    ]
                    st.pydeck_chart(pdk.Deck(
                        layers=overlay_layers,
                        initial_view_state=pdk.ViewState(
                            latitude=MAP_CENTER_LAT, longitude=MAP_CENTER_LON,
                            zoom=7, pitch=0,
                        ),
                        map_provider=None,
                    ))

                # Download button
                with open(rendered_path, "rb") as f:
                    st.download_button(
                        "📥 Download PNG",
                        f.read(),
                        file_name=f"hormuz_{render_meta.get('satellite', 'sat')}_{render_meta.get('date', 'img')}.png",
                        mime="image/png",
                    )

    elif has_cdse:
        st.caption("Use the search above to find available Sentinel scenes over the Strait of Hormuz.")

    # ── Cache management ──
    st.markdown("---")
    st.subheader("💾 Cache Management")
    st.caption("Downloaded quicklooks and rendered images are cached on disk to avoid re-downloading.")

    cache_stats = get_cache_stats()
    cc1, cc2, cc3 = st.columns(3)
    cc1.metric("Quicklooks", f"{cache_stats['quicklooks']['files']} files ({cache_stats['quicklooks']['size_mb']} MB)")
    cc2.metric("Rendered images", f"{cache_stats['processed']['files']} files ({cache_stats['processed']['size_mb']} MB)")
    cc3.metric("Catalog cache", f"{cache_stats['catalog']['files']} files ({cache_stats['catalog']['size_mb']} MB)")

    clear_col1, clear_col2 = st.columns(2)
    with clear_col1:
        if st.button("🗑️ Clear all caches"):
            clear_cache("all")
            st.cache_data.clear()
            st.toast("All caches cleared")
            st.rerun()
    with clear_col2:
        if st.button("🗑️ Clear processed images only"):
            clear_cache("processed")
            st.toast("Processed image cache cleared")


# ══════════════════════════ FOOTER ══════════════════════════════

st.markdown("---")
st.caption(
    "**AIS Data:** [aisstream.io](https://aisstream.io) free WebSocket API · "
    "**Satellite imagery:** [Copernicus Data Space Ecosystem](https://dataspace.copernicus.eu) "
    "(Sentinel-1/2, free) · "
    "**Basemaps:** [ESRI](https://www.arcgis.com/), "
    "[Sentinel-2 Cloudless](https://s2maps.eu/) by [EOX](https://eox.at/), "
    "[CartoDB](https://carto.com/)"
)

# Auto-rerun (only meaningful for AIS tab, but keeps the app alive)
if auto_refresh and api_key:
    if "last_refresh" not in st.session_state:
        st.session_state.last_refresh = time.time()
    if time.time() - st.session_state.last_refresh > 10:
        st.session_state.last_refresh = time.time()
        st.rerun()
