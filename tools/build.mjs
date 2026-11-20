#!/usr/bin/env node
// Build a page from the command line, with the same engine as the website (js/engine.js). Needs Node 22+.
//
//   node tools/build.mjs RUN_FOLDER [--world] [--out FILE] [--sample 0.1] [--title TEXT] [--crs EPSG:2056]
//                        [--events FILE] [--network FILE] [--schedule FILE] [--config FILE] [--sharing FILE]
//                        [--no-rail] [--no-walk] [--no-passenger]
//
// Writes agents_3d.html (agents_world.html with --world) into the run folder unless --out is given.
// OpenStreetMap tiles are cached in ~/.cache/matsim-agent-viz. Prints counts only.
import { promises as fs, openAsBlob } from "node:fs";
import { gunzipSync, gzipSync } from "node:zlib";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import proj4 from "../vendor/proj4.mjs";
import { build, renderPage, findInputs, sharingFor, BuildError, INPUTS } from "../js/engine.js";

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const USAGE = "usage: node tools/build.mjs RUN_FOLDER [--world] [--out FILE] [--sample 0.1] [--title TEXT] [--crs CRS]\n" +
  "       [--events/--network/--schedule/--config/--sharing FILE] [--no-rail] [--no-walk] [--no-passenger]";

function parseArgs(argv) {
  const opt = { sample: 0.1, files: {} };
  const rest = [];
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i], next = () => { if (i + 1 >= argv.length) die(`${a} needs a value`); return argv[++i]; };
    if (a === "--world") opt.world = true;
    else if (a === "--no-rail") opt.rail = false;
    else if (a === "--no-walk") opt.walk = false;
    else if (a === "--no-passenger") opt.passengers = false;
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

/** The folder as the browser sees it: [{path: "folder/name", file}], the top level and one level below. */
async function listFolder(dir) {
  const base = path.basename(path.resolve(dir)), out = [];
  for (const e of await fs.readdir(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isFile()) out.push({ path: `${base}/${e.name}`, file: await asFile(p) });
    else if (e.isDirectory() && e.name !== "ITERS") {
      for (const f of await fs.readdir(p, { withFileTypes: true }))
        if (f.isFile()) out.push({ path: `${base}/${e.name}/${f.name}`, file: await asFile(path.join(p, f.name)) });
    }
  }
  return out;
}

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
  if (opt.files.config && !opt.files.sharing) files.sharing = await sharingFor(entries, files.config);
  const env = {
    proj4, fetch, cache,
    say: text => console.log(text),
    progress: () => {},
  };
  const { payload, meta } = await build(files, { ...opt, folder: path.basename(path.resolve(opt.dir)) }, env, entries);
  const template = await fs.readFile(path.join(ROOT, opt.world ? "world.html" : "page.html"), "utf8");
  const out = opt.out ?? path.join(opt.dir, opt.world ? "agents_world.html" : "agents_3d.html");
  await fs.mkdir(path.dirname(path.resolve(out)), { recursive: true });
  const html = renderPage(template, payload, meta);
  await fs.writeFile(out, html);
  const mb = Buffer.byteLength(html) / 1e6;
  console.log(`wrote ${out} (${mb.toFixed(1)} MB).`);
  if (mb > 60) console.log("note: the page is large and may load slowly. Try a sample of 5%.");
} catch (e) {
  if (e instanceof BuildError) die(`ERROR: ${e.message}`);
  throw e;
}
