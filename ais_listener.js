#!/usr/bin/env node
/**
 * ais_listener.js — Node.js AIS collector (aisstream.io WebSocket)
 *
 * Drop-in equivalent of ais_listener.py for environments without Python.
 * Requires Node.js 22+ (built-in WebSocket API, no npm deps).
 *
 * Usage:
 *   node ais_listener.js --bbox 48.0 22.0 60.0 30.5
 *   nohup node ais_listener.js --bbox 48.0 22.0 60.0 30.5 > ais.log 2>&1 &
 *
 * Credentials (.env):
 *   AISSTREAM_API_KEY=...
 */

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dir = path.dirname(fileURLToPath(import.meta.url));

// --- Load .env ---
function loadEnv() {
  const envPath = path.join(__dir, '.env');
  if (!fs.existsSync(envPath)) return;
  for (const line of fs.readFileSync(envPath, 'utf8').split('\n')) {
    const m = line.match(/^\s*([A-Z_][A-Z0-9_]*)=(.*)$/);
    if (m && !process.env[m[1]]) process.env[m[1]] = m[2].trim();
  }
}

// --- Args ---
function parseArgs() {
  const args = process.argv.slice(2);
  const bbox = { w: 48.0, s: 22.0, e: 60.0, n: 30.5 };
  const out = { outputDir: path.join(__dir, 'ais_data'), statsInterval: 60 };
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--bbox') {
      bbox.w = parseFloat(args[++i]);
      bbox.s = parseFloat(args[++i]);
      bbox.e = parseFloat(args[++i]);
      bbox.n = parseFloat(args[++i]);
    } else if (args[i] === '--output-dir') {
      out.outputDir = args[++i];
    } else if (args[i] === '--stats-interval') {
      out.statsInterval = parseInt(args[++i]);
    }
  }
  return { bbox, ...out };
}

// --- Helpers ---
function nowIso() {
  return new Date().toISOString().replace('Z', '+00:00');
}

function inBbox(lon, lat, { w, s, e, n }) {
  return lon >= w && lon <= e && lat >= s && lat <= n;
}

function normalize(raw) {
  const meta = raw.MetaData || {};
  const lat = meta.latitude;
  const lon = meta.longitude;
  if (lat == null || lon == null) return null;
  return {
    source: 'aisstream',
    received_at: nowIso(),
    type: raw.MessageType || null,
    mmsi: meta.MMSI ?? null,
    lat: parseFloat(lat),
    lon: parseFloat(lon),
    ship_name: (meta.ShipName || '').trim() || null,
    time_utc: meta.time_utc || null,
    raw,
  };
}

// --- File writer ---
function getOutputFile(outputDir) {
  const now = new Date();
  const date = now.toISOString().slice(0, 10);
  const hour = String(now.getUTCHours()).padStart(2, '0');
  const dir = path.join(outputDir, date);
  fs.mkdirSync(dir, { recursive: true });
  return path.join(dir, `${hour}.jsonl`);
}

function writeRecord(outputDir, record) {
  const file = getOutputFile(outputDir);
  fs.appendFileSync(file, JSON.stringify(record) + '\n');
}

// --- Main ---
loadEnv();
const { bbox, outputDir, statsInterval } = parseArgs();
const apiKey = process.env.AISSTREAM_API_KEY;

if (!apiKey) {
  console.error('ERROR: AISSTREAM_API_KEY not set. Add it to .env');
  process.exit(1);
}

const WS_URL = 'wss://stream.aisstream.io/v0/stream';
const subscription = JSON.stringify({
  APIKey: apiKey,
  BoundingBoxes: [[[bbox.s, bbox.w], [bbox.n, bbox.e]]],
});

const stats = { received: 0, kept: 0, mmsis: new Set(), errors: 0 };

function logStats() {
  console.log(
    `[stats] received=${stats.received} kept=${stats.kept} mmsis=${stats.mmsis.size} errors=${stats.errors}`
  );
}

let backoff = 1000;
let running = true;

async function connect() {
  while (running) {
    try {
      console.log(`[aisstream] connecting to ${WS_URL} bbox=${JSON.stringify(bbox)}`);
      const ws = new WebSocket(WS_URL);

      await new Promise((resolve, reject) => {
        ws.addEventListener('open', () => {
          console.log('[aisstream] connected');
          backoff = 1000;
          ws.send(subscription);
        });

        ws.addEventListener('message', async (event) => {
          // Node.js 22 built-in WebSocket delivers data as Blob, not string
          const text = typeof event.data === 'string' ? event.data : await event.data.text();
          let raw;
          try { raw = JSON.parse(text); } catch { return; }
          if (raw.error) {
            console.error('[aisstream] server error:', raw.error);
            ws.close();
            return;
          }
          stats.received++;
          const rec = normalize(raw);
          if (!rec) return;
          if (!inBbox(rec.lon, rec.lat, bbox)) return;
          stats.kept++;
          if (rec.mmsi != null) stats.mmsis.add(rec.mmsi);
          try { writeRecord(outputDir, rec); } catch (e) {
            stats.errors++;
            console.error('[writer] error:', e.message);
          }
        });

        ws.addEventListener('close', (e) => {
          console.warn(`[aisstream] disconnected (code=${e.code})`);
          resolve();
        });

        ws.addEventListener('error', (e) => {
          console.error('[aisstream] error:', e.message || e);
          reject(e);
        });
      });
    } catch (e) {
      console.warn(`[aisstream] ${e.message} — reconnecting in ${backoff / 1000}s`);
    }

    if (!running) break;
    await new Promise(r => setTimeout(r, backoff));
    backoff = Math.min(60000, backoff * 2);
  }
}

const statsTimer = setInterval(logStats, statsInterval * 1000);

process.on('SIGINT', () => { running = false; clearInterval(statsTimer); logStats(); console.log('[shutdown] done'); process.exit(0); });
process.on('SIGTERM', () => { running = false; clearInterval(statsTimer); logStats(); console.log('[shutdown] done'); process.exit(0); });

console.log(`[aisstream] starting — bbox W=${bbox.w} S=${bbox.s} E=${bbox.e} N=${bbox.n}`);
connect().catch(e => { console.error('fatal:', e); process.exit(1); });
