#!/usr/bin/env node
// Build a page from the command line, with the same engine as the website (js/engine.js). Needs Node 22+.
//
//   node tools/build.mjs RUN_FOLDER [--world] [--out FILE] [--sample 0.1] [--title TEXT] [--crs EPSG:2056]
//                        [--events FILE] [--network FILE] [--schedule FILE] [--config FILE] [--sharing FILE]
//                        [--no-rail] [--no-walk] [--no-passenger] [--json]
//
// Writes agents_3d.html (agents_world.html with --world) into the run folder unless --out is given.
// --json writes only the page data, without bike-sharing stations (how demo/zurich.json, the start page's background, is made).
// OpenStreetMap tiles are cached in ~/.cache/matsim-agent-viz. Prints counts only.
import { promises as fs, openAsBlob } from "node:fs";
import { gunzipSync, gzipSync } from "node:zlib";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import proj4 from "../vendor/proj4.mjs";
import { build, renderParts, findInputs, sharingFor, readConfig, BuildError, INPUTS } from "../js/engine.js";

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const USAGE = "usage: node tools/build.mjs RUN_FOLDER [--world] [--out FILE] [--sample 0.1] [--title TEXT] [--crs CRS]\n" +
  "       [--events/--network/--schedule/--config/--sharing FILE] [--no-rail] [--no-walk] [--no-passenger] [--json]";

function parseArgs(argv) {
  const opt = { sample: 0.1, files: {} };
  const rest = [];
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i], next = () => { if (i + 1 >= argv.length) die(`${a} needs a value`); return argv[++i]; };
    if (a === "--world") opt.world = true;
    else if (a === "--no-rail") opt.rail = false;
    else if (a === "--no-walk") opt.walk = false;
    else if (a === "--no-passenger") opt.passengers = false;
    else if (a === "--json") opt.json = true;
    else if (a === "--out") opt.out = next();
    else if (a === "--sample") opt.sample = parseFloat(next());
    else if (a === "--title") opt.title = next();
    else if (a === "--crs") opt.crs = next();
    else if (a.startsWith("--") && a.slice(2) in INPUTS) opt.files[a.slice(2)] = next();
    else if (a === "-h" || a === "--help") { console.log(USAGE); process.exit(0); }
    else if (a.startsWith("-")) die(`unknown option ${a}\n${USAGE}`);
    else rest.push(a);
  }
  if (rest.length !== 1) die(USAGE);
  opt.dir = rest[0];
  return opt;
}

function die(msg) { console.error(msg); process.exit(1); }

const asFile = async p => new File([await openAsBlob(p)], path.basename(p));

const kindOf = async p => { const st = await fs.stat(p).catch(() => null); return st?.isFile() ? "file" : st?.isDirectory() ? "dir" : null; };

/** The folder as the browser sees it: [{path: "folder/name", file}], the top level and one level below.
 * Follows symbolic links. */
async function listFolder(dir) {
  const base = path.basename(path.resolve(dir)), out = [];
  for (const name of await fs.readdir(dir)) {
    const p = path.join(dir, name), kind = await kindOf(p);
    if (kind === "file") out.push({ path: `${base}/${name}`, file: await asFile(p) });
    else if (kind === "dir" && name !== "ITERS") {
      for (const sub of await fs.readdir(p).catch(() => []))
        if (await kindOf(path.join(p, sub)) === "file") out.push({ path: `${base}/${name}/${sub}`, file: await asFile(path.join(p, sub)) });
    }
  }
  return out;
}

/** The station file the config names, looked up outside the run folder too: its path is relative to the original
 * config, which usually sits in the folder above the output. Same places as the Python tool: the run folder, the
 * config's folder and the folder above, by path and then by name. */
async function sharingNearby(dir, config, configPath) {
  let cfg;
  try { cfg = await readConfig(config); } catch { return null; }
  for (const svc of cfg.services) {
    if ((svc.serviceScheme ?? "").toLowerCase() === "freefloating") continue;
    const p = (svc.serviceInputFile ?? "").replace(/\\/g, "/");
    if (!p) continue;
    const cands = [  // in Python's order: by path, then by name next to the run
      ...(path.isAbsolute(p) ? [p] : [path.resolve(dir, p), path.resolve(path.dirname(configPath ?? path.join(dir, "x")), p), path.resolve(dir, "..", p)]),
      path.resolve(dir, path.basename(p)), path.resolve(dir, "..", path.basename(p))];
    for (const c of cands) if (await kindOf(c) === "file") return asFile(c);
  }
  return null;
}

/** 'Zurich_10pct/simulation_output' -> the folder name to title the page by: 'Zurich_10pct'. */
const titleFolder = dir => path.resolve(dir).split(path.sep)
  .filter(p => p && !["simulation_output", "output"].includes(p.toLowerCase())).pop() ?? "";

// OpenStreetMap tiles, gzipped JSON in ~/.cache/matsim-agent-viz/{rail,walk}/LAT_LON.json.gz
const CACHE_DIR = path.join(os.homedir(), ".cache", "matsim-agent-viz");
const cache = {
  async get(kind, key) {
    try { return gunzipSync(await fs.readFile(path.join(CACHE_DIR, kind, `${key}.json.gz`))).toString("utf8"); } catch { return null; }
  },
  async put(kind, key, text) {
    const file = path.join(CACHE_DIR, kind, `${key}.json.gz`);
    await fs.mkdir(path.dirname(file), { recursive: true });
    await fs.writeFile(`${file}.tmp`, gzipSync(text));
    await fs.rename(`${file}.tmp`, file);
  },
};

const opt = parseArgs(process.argv.slice(2));
try {
  if (!(await fs.stat(opt.dir).catch(() => null))?.isDirectory()) die(`ERROR: run folder ${opt.dir} not found.`);
  const entries = await listFolder(opt.dir);
  const files = await findInputs(entries);
  for (const [role, p] of Object.entries(opt.files)) {
    if (!(await fs.stat(p).catch(() => null))?.isFile()) die(`ERROR: ${p} not found.`);
    files[role] = await asFile(p);
  }
  if (files.config && !opt.files.sharing) {  // the places the Python tool looked first, then anywhere in the run folder
    files.sharing = await sharingNearby(opt.dir, files.config, opt.files.config)
      ?? (opt.files.config ? await sharingFor(entries, files.config) : files.sharing);
  }
  // Overpass refuses Node's default User-Agent. (Browsers send their own and must not set one: that needs a preflight.)
  const withAgent = (url, init = {}) => fetch(url, { ...init, headers: { ...init.headers, "User-Agent": "matsim-agent-viz" } });
  const env = {
    proj4, fetch: withAgent, cache,
    say: text => console.log(text),
    progress: () => {},
  };
  const { payload, meta } = await build(files, { ...opt, folder: titleFolder(opt.dir) }, env, entries);
  if (opt.json) {
    if (!opt.out) die("--json needs --out FILE");
    delete payload.sharing;
    await fs.writeFile(opt.out, JSON.stringify(payload));
    console.log(`wrote ${opt.out} (${((await fs.stat(opt.out)).size / 1e6).toFixed(1)} MB).`);
    process.exit(0);
  }
  const template = await fs.readFile(path.join(ROOT, opt.world ? "world.html" : "page.html"), "utf8");
  const out = opt.out ?? path.join(opt.dir, opt.world ? "agents_world.html" : "agents_3d.html");
  await fs.mkdir(path.dirname(path.resolve(out)), { recursive: true });
  const parts = renderParts(template, payload, meta);
  await fs.writeFile(out, parts);  // written part by part: the data can be very long
  const mb = parts.reduce((s, p) => s + Buffer.byteLength(p), 0) / 1e6;
  console.log(`wrote ${out} (${mb.toFixed(1)} MB).`);
  if (mb > 60) console.log("note: the page is large and may load slowly. Try a sample of 5%.");
} catch (e) {
  if (e instanceof BuildError) die(`ERROR: ${e.message}`);
  throw e;
}
