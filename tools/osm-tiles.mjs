// Downloads the OpenStreetMap tiles of tools/osm-regions.json from Overpass, with the engine's own queries,
// and writes them as gzipped JSON to osm/{rail,walk}/KEY.json.gz. The site publishes that folder, so builds
// in these regions take their tracks and footpaths from the site instead of waiting for Overpass
// (js/worker.js, `shared`). Public OSM data only: no run is read.
//
//   node tools/osm-tiles.mjs [--out osm] [--regions tools/osm-regions.json] [--max-age-days 6] [--budget-min 0]
//
// A tile is downloaded again when it is older than --max-age-days or when the engine's query for it changed
// (osm/queries.json keeps a hash of each tile's query). If a download fails, the old tile stays. With
// --budget-min, downloads stop after that many minutes, so a slow Overpass can't hold up a deploy.
// Tiles outside the regions are removed. In GitHub Actions, it sets the step output changed=true when
// it changed osm/.
import fs from "node:fs/promises";
import path from "node:path";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";
import { railTile, walkTile, railQuery, walkQuery, WALK_TILE } from "../js/engine.js";

const args = process.argv.slice(2);
const arg = (name, def) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : def; };
const number = (name, def) => {
  const v = Number(arg(name, def));
  if (!Number.isFinite(v) || v < 0) { console.error(`ERROR: ${name} needs a number`); process.exit(1); }
  return v;
};
const OUT = arg("--out", "osm");
const REGIONS = arg("--regions", fileURLToPath(new URL("./osm-regions.json", import.meta.url)));
const MAX_AGE_MS = number("--max-age-days", "6") * 86400_000;
const BUDGET_MS = number("--budget-min", "0") * 60_000;

const file = (kind, key) => path.join(OUT, kind, `${key}.json.gz`);
const MANIFEST = path.join(OUT, "queries.json");
const manifest = JSON.parse(await fs.readFile(MANIFEST, "utf8").catch(() => "{}"));
const hash = text => createHash("sha256").update(text).digest("hex").slice(0, 16);

let changed = false;
const markChanged = async () => {
  if (changed) return;
  changed = true;
  if (process.env.GITHUB_OUTPUT) await fs.appendFile(process.env.GITHUB_OUTPUT, "changed=true\n");
};

// Which tiles the regions need, and the engine's query for each.
const range = (a, b) => Array.from({ length: b - a + 1 }, (_, k) => a + k);
const tiles = { rail: new Map(), walk: new Map() };
const regions = JSON.parse(await fs.readFile(REGIONS, "utf8"));
for (const [s, w, n, e] of Object.values(regions.rail ?? {}))
  for (const lat of range(Math.floor(s), Math.floor(n)))
    for (const lon of range(Math.floor(w), Math.floor(e))) tiles.rail.set(`${lat}_${lon}`, railQuery(lat, lon));
for (const [s, w, n, e] of Object.values(regions.walk ?? {}))
  for (const i of range(Math.floor(s / WALK_TILE), Math.floor(n / WALK_TILE)))
    for (const j of range(Math.floor(w / WALK_TILE), Math.floor(e / WALK_TILE))) tiles.walk.set(`${i}_${j}`, walkQuery(i, j));

// Remove tiles outside the regions and files left by an interrupted run.
for (const kind of ["rail", "walk"]) {
  for (const name of await fs.readdir(path.join(OUT, kind)).catch(() => [])) {
    const key = name.endsWith(".json.gz") ? name.slice(0, -".json.gz".length) : null;
    if (key && tiles[kind].has(key)) continue;
    await fs.rm(path.join(OUT, kind, name), { force: true });
    if (key) delete manifest[`${kind}/${key}`];
    await markChanged();
  }
}

const fresh = async (kind, key, query) => {
  if (manifest[`${kind}/${key}`] !== hash(query)) return false;
  const st = await fs.stat(file(kind, key)).catch(() => null);
  return st !== null && Date.now() - st.mtimeMs <= MAX_AGE_MS;
};
// The engine asks this cache first: a miss makes it download, a put stores the new tile.
const store = {
  async get() { return null; },
  async put(kind, key, text) {
    const f = file(kind, key);
    await fs.mkdir(path.dirname(f), { recursive: true });
    await fs.writeFile(`${f}.tmp`, gzipSync(text, { level: 9 }));
    await fs.rename(`${f}.tmp`, f);
    manifest[`${kind}/${key}`] = hash(tiles[kind].get(key));
    await fs.writeFile(MANIFEST, JSON.stringify(manifest, null, 1));
    await markChanged();
  },
};
// Overpass refuses Node's default User-Agent.
const fetchWithAgent = (url, init = {}) => fetch(url, { ...init, headers: { ...init.headers, "User-Agent": "matsim-agent-viz" } });
const env = { cache: store, fetch: fetchWithAgent, say: text => console.log(text) };
const load = { rail: (lat, lon) => railTile(lat, lon, env), walk: (i, j) => walkTile(i, j, env) };

const start = Date.now();
let kept = 0, fetched = 0, failed = 0, skipped = 0;
for (const kind of ["rail", "walk"]) {
  for (const [key, query] of tiles[kind]) {
    if (await fresh(kind, key, query)) { kept++; continue; }
    if (BUDGET_MS && Date.now() - start > BUDGET_MS) { skipped++; continue; }
    const t0 = Date.now();
    const data = await load[kind](...key.split("_").map(Number));
    if (data) fetched++; else failed++;
    console.log(`${kind} ${key}: ${data ? "downloaded" : "failed"} in ${Math.round((Date.now() - t0) / 1000)} s`);
    await new Promise(r => setTimeout(r, 2000));  // be gentle with the public Overpass server
  }
}
console.log(`${tiles.rail.size} rail and ${tiles.walk.size} footpath tiles: ${kept} kept, ${fetched} downloaded, ` +
  `${failed} failed, ${skipped} left for the next run (time budget)`);
if ((failed || skipped) && process.env.GITHUB_ACTIONS)
  console.log(`::warning::${failed} OpenStreetMap tiles failed to download and ${skipped} were left for the next run; the site falls back to Overpass for them`);
