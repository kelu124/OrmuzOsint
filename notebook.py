import asyncio
import websockets
import json
from datetime import datetime, timezone
# ── Load .env from project folder ──
from dotenv import load_dotenv
from pathlib import Path
import os

_PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(_PROJECT_DIR / ".env")
HORMUZ_BBOX = [[25.0, 54.5], [27.5, 57.5]]

api_key = os.getenv("AISSTREAM_API_KEY", "")

async def connect_ais_stream():

    async with websockets.connect("wss://stream.aisstream.io/v0/stream") as websocket:
        url = "wss://stream.aisstream.io/v0/stream"
        subscribe_msg = {
            "APIKey": api_key,
            "BoundingBoxes": [HORMUZ_BBOX],
            "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
        }

        subscribe_message_json = json.dumps(subscribe_msg)
        await websocket.send(subscribe_message_json)

        async for message_json in websocket:
            message = json.loads(message_json)
            message_type = message["MessageType"]

            if message_type == "PositionReport":
                # the message parameter contains a key of the message type which contains the message itself
                ais_message = message['Message']['PositionReport']
                print(f"[{datetime.now(timezone.utc)}] ShipId: {ais_message['UserID']} Latitude: {ais_message['Latitude']} Latitude: {ais_message['Longitude']}")

async def connect_with_retry():
    retries = 0
    while True:
        try:
            await connect_ais_stream()
        except websockets.exceptions.InvalidStatus as e:
            if e.response.status_code == 429:
                wait = min(30, 5 * (retries + 1))
                print(f"Rate limited (429) — waiting {wait}s before retry…")
                await asyncio.sleep(wait)
                retries += 1
            else:
                raise
        except websockets.exceptions.ConnectionClosed:
            print("Connection closed — reconnecting in 5s…")
            await asyncio.sleep(5)
            retries = 0

asyncio.run(connect_with_retry())

