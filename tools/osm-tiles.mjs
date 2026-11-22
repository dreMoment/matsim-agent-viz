// Downloads the OpenStreetMap tiles of tools/osm-regions.json from Overpass, with the engine's own queries,
// and writes them as gzipped JSON to osm/{rail,walk}/KEY.json.gz. The site publishes that folder, so builds
// in these regions take their tracks and footpaths from the site instead of waiting for Overpass
// (js/worker.js, `shared`). Public OSM data only: no run is read.
//
//   node tools/osm-tiles.mjs [--out osm] [--regions tools/osm-regions.json] [--max-age-days 7]
//
// Tiles younger than --max-age-days are kept. If a download fails, the old tile stays.
import fs from "node:fs/promises";
import path from "node:path";
import { gzipSync, gunzipSync } from "node:zlib";
import { railTile, walkTile, WALK_TILE } from "../js/engine.js";

const args = process.argv.slice(2);
const arg = (name, def) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : def; };
const OUT = arg("--out", "osm");
const REGIONS = arg("--regions", new URL("./osm-regions.json", import.meta.url).pathname);
const MAX_AGE_MS = Number(arg("--max-age-days", "7")) * 86400_000;

const file = (kind, key) => path.join(OUT, kind, `${key}.json.gz`);
const cache = {
  async get(kind, key) {
    try {
      const f = file(kind, key);
      if (Date.now() - (await fs.stat(f)).mtimeMs > MAX_AGE_MS) return null;
      return gunzipSync(await fs.readFile(f)).toString("utf8");
    } catch { return null; }
  },
  async put(kind, key, text) {
    const f = file(kind, key);
    await fs.mkdir(path.dirname(f), { recursive: true });
    await fs.writeFile(`${f}.tmp`, gzipSync(text, { level: 9 }));
    await fs.rename(`${f}.tmp`, f);
  },
};
// Overpass refuses Node's default User-Agent.
const fetchWithAgent = (url, init = {}) => fetch(url, { ...init, headers: { ...init.headers, "User-Agent": "matsim-agent-viz" } });
const env = { cache, fetch: fetchWithAgent, say: text => console.log(text) };

const range = (a, b) => Array.from({ length: b - a + 1 }, (_, k) => a + k);
const tiles = { rail: new Set(), walk: new Set() };
const regions = JSON.parse(await fs.readFile(REGIONS, "utf8"));
for (const [s, w, n, e] of Object.values(regions.rail ?? {}))
  for (const lat of range(Math.floor(s), Math.floor(n))) for (const lon of range(Math.floor(w), Math.floor(e))) tiles.rail.add(`${lat}_${lon}`);
for (const [s, w, n, e] of Object.values(regions.walk ?? {}))
  for (const i of range(Math.floor(s / WALK_TILE), Math.floor(n / WALK_TILE)))
    for (const j of range(Math.floor(w / WALK_TILE), Math.floor(e / WALK_TILE))) tiles.walk.add(`${i}_${j}`);

const load = { rail: k => railTile(...k.split("_").map(Number), env), walk: k => walkTile(...k.split("_").map(Number), env) };
let fresh = 0, fetched = 0, failed = 0;
for (const kind of ["rail", "walk"]) {
  for (const key of tiles[kind]) {
    if (await cache.get(kind, key)) { fresh++; continue; }
    const t0 = Date.now();
    const data = await load[kind](key);
    if (data) fetched++; else failed++;
    console.log(`${kind} ${key}: ${data ? "downloaded" : "failed"} in ${Math.round((Date.now() - t0) / 1000)} s`);
    await new Promise(r => setTimeout(r, 2000));  // be gentle with the public Overpass server
  }
}
console.log(`${tiles.rail.size} rail and ${tiles.walk.size} footpath tiles: ${fresh} still fresh, ${fetched} downloaded, ${failed} failed`);
