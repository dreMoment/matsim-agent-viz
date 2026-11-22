// Reads a MATSim run and builds the page data, in the browser (js/worker.js) or in Node (tools/build.mjs).
//
// What it reads (plain or .gz): output_events.xml (movements of the final iteration), output_network.xml
// (node coordinates for link events), output_transitSchedule.xml (stop coordinates and modes of public
// transport, optional), output_config.xml (coordinate system and iteration, optional) and a bike-sharing
// service file (optional, found through the config's sharing module). It never reads the population,
// facilities or households files.
//
// How positions are built:
//   * car and pt: from the vehicle's link entries and stop arrivals/departures, so dots follow the
//     network and wait at stops;
//   * trains and trams: between two stops, along the railway tracks of OpenStreetMap (downloaded from
//     Overpass by tile, then cached), because their network links are often straight lines;
//   * walks: along OpenStreetMap footpaths and streets, keeping MATSim's start and end times;
//   * car passengers: the fastest path on the run's car links, like the cars;
//   * other teleported modes (bike, PubliBike, ...): a straight line from start to end at constant speed;
//   * agents are sampled with a stable hash of the person id.
//
// Only tile bounds go out (to Overpass), never run data. Messages are counts only: no ids, coordinates or records.
//
// The caller passes `env`:
//   proj4                                  the proj4 library
//   say(text)                              a log line
//   progress({step, fraction, reading})    how far the build is
//   cache: {get(kind, key), put(kind, key, text)}   OpenStreetMap tiles (public data), may be missing
//   shared: {get(kind, key)}               ready-made tiles published with the site (tools/osm-tiles.mjs), may be missing
//   fetch                                  for Overpass and epsg.io

export const MIN_POINT_GAP_S = 10;  // thin out link events closer together than this ...
export const MERGE_TOL_M = 8.0;     // ... but only where the dropped point lies this close to the path, so turns stay

export class BuildError extends Error {}
/** The run's coordinate system is missing or unusable: the start page then asks for one. */
export class CrsError extends BuildError {}
const fail = msg => { throw new BuildError(msg); };
const failCrs = msg => { throw new CrsError(msg); };

// role -> file name in the run folder, required, what it is used for
export const INPUTS = {
  events: { name: "output_events.xml", required: true, use: "movements of the final iteration" },
  network: { name: "output_network.xml", required: true, use: "link coordinates" },
  schedule: { name: "output_transitSchedule.xml", required: false, use: "stop coordinates for public transport" },
  config: { name: "output_config.xml", required: false, use: "coordinate system and iteration number" },
  sharing: { name: "sharing service file", required: false, use: "bike-sharing stations, from the config's sharing module" },
};

// ---------------------------------------------------------------- finding the inputs
// A run folder is a list of {path, file}: path relative to the chosen folder, e.g. "run/output/output_events.xml.gz"
// (the folder's own name first, as the browser gives it), file a Blob with a name. The outputs may sit in a
// subfolder (a run folder with its output/ inside): see outputFolder.

/** How deep below the chosen folder the readers look for the outputs (ITERS and hidden folders are skipped). */
export const MAX_DEPTH = 3;
export const skipFolder = name => name === "ITERS" || name.startsWith(".");

const dirOf = p => { const i = p.lastIndexOf("/"); return i < 0 ? "" : p.slice(0, i); };

/** The entries grouped by folder: Map(folder path -> entries), built once per entries list. */
const folderCache = new WeakMap();
function byFolder(entries) {
  let m = folderCache.get(entries);
  if (!m) {
    m = new Map();
    for (const e of entries) {
      const d = dirOf(e.path);
      if (!m.has(d)) m.set(d, []);
      m.get(d).push(e);
    }
    folderCache.set(entries, m);
  }
  return m;
}
const inDir = (entries, dir) => byFolder(entries).get(dir) ?? [];

/** <name>.gz or <name> in the folder dir (also with a run-id prefix), or null. */
function locateIn(entries, name, dir) {
  const here = inDir(entries, dir);
  for (const cand of [`${name}.gz`, name]) {
    const hit = here.find(e => e.file.name === cand);
    if (hit) return hit.file;
  }
  const byName = suffix => here.filter(e => e.file.name.endsWith(suffix) && e.file.name.length > suffix.length)
    .map(e => e.file).sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
  return byName(`.${name}.gz`)[0] ?? byName(`.${name}`)[0] ?? null;
}

/** The folder (path as in entries) that holds the run's outputs: the shallowest folder with an events file (the
 * chosen folder itself if it has one), else the shallowest with a network file. Several at that depth: a
 * BuildError naming them. */
export function outputFolder(entries) {
  const root = entries[0] ? entries[0].path.split("/")[0] : "";
  const dirs = [...byFolder(entries).keys()];
  for (const role of ["events", "network"]) {
    const has = dirs.filter(d => locateIn(entries, INPUTS[role].name, d));
    if (!has.length) continue;
    const depth = d => d.split("/").length;
    const min = Math.min(...has.map(depth));
    const top = has.filter(d => depth(d) === min).sort();
    if (top.length > 1) {
      const names = top.map(d => d.split("/").slice(1).join("/"));
      fail(`This folder holds ${top.length} runs (${names.slice(0, 4).join(", ")}${top.length > 4 ? ", …" : ""}). Choose one of them.`);
    }
    return top[0];
  }
  return root;
}

/** Which inputs the folder has: {role: File or null}. */
export async function findInputs(entries) {
  const dir = outputFolder(entries), files = {};
  for (const [role, { name }] of Object.entries(INPUTS)) if (role !== "sharing") files[role] = locateIn(entries, name, dir);
  files.sharing = await sharingFor(entries, files.config);
  return files;
}

/** The station file a config points to, or null (also when the config is not readable). */
export async function sharingFor(entries, config) {
  if (!config) return null;
  try {
    for (const svc of stationServices(await readConfig(config))) {
      const f = serviceFile(entries, svc);
      if (f) return f;
    }
  } catch { /* the build will say what is wrong with the config */ }
  return null;
}

const stationServices = cfg => cfg.services.filter(s => (s.serviceScheme ?? "").toLowerCase() !== "freefloating");

/** base + "/" + rel with "." and ".." resolved, or null if rel climbs above base's first folder. */
function joinPath(base, rel) {
  const out = base.split("/");
  for (const part of rel.split("/")) {
    if (part === "..") { if (out.length <= 1) return null; out.pop(); }
    else if (part && part !== ".") out.push(part);
  }
  return out.join("/");
}

/** Where a service's file is. The path is relative to the original config, which is not in the output
 * folder (usually the folder above it), so it is looked up by its path from the output folder, the folder above
 * and the chosen folder, then by its name in the output folder, then anywhere. */
function serviceFile(entries, svc) {
  const p = (svc.serviceInputFile ?? "").replace(/\\/g, "/");
  const name = p.split("/").pop();
  if (!name) return null;
  const out = outputFolder(entries), root = out.split("/")[0];
  const bases = [...new Set([out, dirOf(out), root].filter(Boolean))];
  for (const b of bases) {
    const want = joinPath(b, p);
    const hit = want && entries.find(e => e.path === want);
    if (hit) return hit.file;
  }
  const here = inDir(entries, out).find(e => e.file.name === name);
  if (here) return here.file;
  return entries.find(e => e.file.name === name)?.file ?? null;
}

/** 'Zurich_10pct' -> 'Zurich 10%'. Folders called output or simulation_output give no name. */
export function scenarioName(folder) {
  const f = (folder ?? "").trim();
  if (!f || ["simulation_output", "output"].includes(f.toLowerCase())) return "MATSim run";
  return f.replace(/_/g, " ").replace(/(\d+)\s*pct\b/gi, "$1%");
}

// ---------------------------------------------------------------- reading XML
const GZIP = [0x1f, 0x8b];

async function isGzip(file) {
  const b = new Uint8Array(await file.slice(0, 2).arrayBuffer());
  return b[0] === GZIP[0] && b[1] === GZIP[1];
}

async function textStream(file, onBytes) {
  let stream = file.stream();
  if (onBytes) {
    let read = 0;
    stream = stream.pipeThrough(new TransformStream({
      transform(chunk, ctl) { read += chunk.byteLength; onBytes(read); ctl.enqueue(chunk); },
    }));
  }
  if (await isGzip(file)) stream = stream.pipeThrough(new DecompressionStream("gzip"));
  else if (file.name.endsWith(".gz")) fail(`${file.name} ends in .gz but is not gzip. Rename it or copy the file again.`);
  return stream.pipeThrough(new TextDecoderStream());
}

async function readText(file) {
  const reader = (await textStream(file)).getReader();
  let out = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return out;
    out += value;
  }
}

const ENT = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'" };
const unescapeXml = s => s.replace(/&(#x[0-9a-fA-F]+|#\d+|\w+);/g, (m, e) =>
  e[0] === "#" ? String.fromCodePoint(e[1] === "x" ? parseInt(e.slice(2), 16) : +e.slice(1)) : (ENT[e] ?? m));

/** The attributes of the tag whose name ends at i, and the index of the tag's closing ">":
 * [attrs, end], or null if the tag is not complete in s yet. Quoted values may hold ">". */
function attrs(s, i) {
  const out = {}, n = s.length;
  for (;;) {
    while (i < n && s.charCodeAt(i) <= 32) i++;
    if (i >= n) return null;
    const c = s.charCodeAt(i);
    if (c === 62) return [out, i];             // ">"
    if (c === 47) { i++; continue; }          // "/" of "/>"
    const eq = s.indexOf("=", i);
    if (eq < 0) return null;
    const name = s.slice(i, eq), bad = name.indexOf(">");
    if (bad >= 0) return [out, i + bad];     // an attribute without a value: skip it
    let q = eq + 1;
    while (q < n && s.charCodeAt(q) <= 32) q++;
    if (q >= n) return null;
    const quote = s[q];
    if (quote !== '"' && quote !== "'") {    // not well-formed: give up on this tag
      const gt = s.indexOf(">", q);
      return gt < 0 ? null : [out, gt];
    }
    const close = s.indexOf(quote, q + 1);
    if (close < 0) return null;
    let v = s.slice(q + 1, close);
    if (v.indexOf("&") >= 0) v = unescapeXml(v);
    out[name.trimEnd()] = v;
    i = close + 1;
  }
}

/**
 * Streams a file and calls onTag(name, attrs, text) for each start tag whose name is in `names`.
 * `text` is the text right after the tag (up to the next tag), only when `withText` is set.
 * Reports the bytes read (on disk, so compressed bytes for .gz) to onBytes.
 */
async function scan(file, names, onTag, { withText = false, onBytes } = {}) {
  const reader = (await textStream(file, onBytes)).getReader();
  let rest = "", root = null, closed = false;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      const text = rest + value, n = text.length;
      let pos = 0;
      for (;;) {
        const lt = text.indexOf("<", pos);
        if (lt < 0) { pos = n; break; }
        if (text.startsWith("<!--", lt)) {  // a comment may hold anything
          const end = text.indexOf("-->", lt + 4);
          if (end < 0) { pos = lt; break; }
          pos = end + 3;
          continue;
        }
        let e = lt + 1;
        if (text.charCodeAt(e) === 47) e++;  // an end tag: its name starts after "/"
        for (; e < n; e++) {
          const c = text.charCodeAt(e);
          if (c === 32 || c === 9 || c === 10 || c === 13 || c === 47 || c === 62) break;
        }
        if (e >= n) { pos = lt; break; }
        const name = text.slice(lt + 1, e);
        if (names.has(name)) {
          const r = attrs(text, e);
          if (!r) { pos = lt; break; }  // the tag goes on in the next chunk
          const gt = r[1];
          if (withText) {  // wait until the text after the tag is all here
            const nx = text.indexOf("<", gt);
            if (nx < 0) { pos = lt; break; }
            onTag(name, r[0], unescapeXml(text.slice(gt + 1, nx)));
          } else onTag(name, r[0]);
          pos = gt + 1;
          continue;
        }
        // Track the root element, to tell a complete file from a cut-off one. (No caller asks for the
        // root's own tag in `names`, so it always comes through here.)
        if (root === null && name[0] !== "?" && name[0] !== "!") {
          const r = attrs(text, e);  // its real end, even with ">" or "/>" inside a value
          if (!r) { pos = lt; break; }
          root = name;
          if (text.charCodeAt(r[1] - 1) === 47) closed = true;  // <root/>
          pos = r[1] + 1;
          continue;
        }
        // Another tag: skip it. A ">" inside one of its values ends it early, but values hold no "<",
        // so the next "<" is still the next tag.
        const gt = text.indexOf(">", e);
        if (gt < 0) { pos = lt; break; }
        if (root !== null && name.length === root.length + 1 && name === "/" + root) closed = true;
        pos = gt + 1;
      }
      rest = text.slice(pos);
    }
  } catch (err) {
    if (err instanceof BuildError) throw err;
    fail(`${file.name} could not be read${err.message ? ` (${err.message})` : ""}. Is the file complete, or was the copy cut off?`);
  }
  if (!closed) fail(`${file.name} ends too early${root ? ` (no closing </${root}>)` : ""}. Is the file complete, or was the copy cut off?`);
}

/** A small XML tree (tags, attributes, children; no text), for config files. */
function parseTree(text) {
  const root = { tag: "", attrs: {}, children: [] };
  const stack = [root];
  let pos = 0;
  for (;;) {
    const lt = text.indexOf("<", pos);
    if (lt < 0) break;
    if (text.startsWith("<!--", lt)) { pos = text.indexOf("-->", lt + 4) + 3; if (pos < 3) break; continue; }
    if (text.startsWith("<?", lt)) { pos = text.indexOf("?>", lt + 2) + 2; if (pos < 2) break; continue; }  // may hold ">"
    const gt = text.indexOf(">", lt);
    if (gt < 0) break;
    pos = gt + 1;
    const c = text[lt + 1];
    if (c === "?" || c === "!") continue;
    if (c === "/") {  // an end tag must close the open element
      if (stack.length < 2 || text.slice(lt + 2, gt).trim() !== stack[stack.length - 1].tag) { root.bad = true; break; }
      stack.pop();
      continue;
    }
    let e = lt + 1;
    while (e < text.length && !/[\s/>]/.test(text[e])) e++;
    const r = attrs(text, e);
    if (!r) break;
    pos = r[1] + 1;
    const el = { tag: text.slice(lt + 1, e), attrs: r[0], children: [] };
    stack[stack.length - 1].children.push(el);
    if (text[r[1] - 1] !== "/") stack.push(el);
  }
  root.complete = !root.bad && stack.length === 1 && root.children.length > 0;  // every element closed, in order
  return root;
}

function* walkTree(el) {
  for (const c of el.children) { yield c; yield* walkTree(c); }
}

/** What the build needs from output_config: params by name (first one wins) and the sharing services. */
export async function readConfig(file) {
  let tree;
  try { tree = parseTree(await readText(file)); } catch (e) {
    if (e instanceof BuildError) throw e;
    fail(`${file.name} could not be read (${e.message || e}).`);
  }
  if (!tree.complete) fail(`${file.name} ends too early or is not XML. Is the file complete?`);
  const params = {}, services = [];
  for (const el of walkTree(tree)) {
    if (el.tag === "param" && el.attrs.name && !(el.attrs.name in params)) params[el.attrs.name] = el.attrs.value ?? "";
    if (el.tag === "parameterset") {
      const p = {};
      for (const c of el.children) if (c.tag === "param") p[c.attrs.name] = (c.attrs.value ?? "").trim();
      if ("serviceInputFile" in p) services.push(p);
    }
  }
  return { params, services };
}

// ---------------------------------------------------------------- coordinate systems
const UNSET_CRS = new Set(["", "atlantis", "none"]);  // "Atlantis" is MATSim's default for "not set"

// MATSim's own short names (MGC in matsim core). WGS84_UTMnnN/S is handled below.
const MATSIM_CRS = {
  gk4: "EPSG:31468", dhdn_gk4: "EPSG:31468",
  ch1903_lv03: "EPSG:21781", ch1903_lv03_gt: "EPSG:21781",
  ch1903_lv03_plus: "EPSG:2056", ch1903_lv03_plus_gt: "EPSG:2056",
  wgs84: "EPSG:4326", pseudo_mercator: "EPSG:3857",
  wgs84_svy21: "EPSG:3414", nad83_utm17n: "EPSG:26917",
};

// Common grids, so most runs need no lookup (from epsg.io; the German and British grid shifts as Helmert parameters).
const DHDN = "+ellps=bessel +towgs84=598.1,73.7,418.2,0.202,0.045,-2.455,6.7 +units=m +no_defs";
const KNOWN_CRS = {
  "EPSG:2056": "+proj=somerc +lat_0=46.9524055555556 +lon_0=7.43958333333333 +k_0=1 +x_0=2600000 +y_0=1200000 +ellps=bessel +towgs84=674.374,15.056,405.346,0,0,0,0 +units=m +no_defs",
  "EPSG:21781": "+proj=somerc +lat_0=46.9524055555556 +lon_0=7.43958333333333 +k_0=1 +x_0=600000 +y_0=200000 +ellps=bessel +towgs84=674.374,15.056,405.346,0,0,0,0 +units=m +no_defs",
  "EPSG:31466": `+proj=tmerc +lat_0=0 +lon_0=6 +k=1 +x_0=2500000 +y_0=0 ${DHDN}`,
  "EPSG:31467": `+proj=tmerc +lat_0=0 +lon_0=9 +k=1 +x_0=3500000 +y_0=0 ${DHDN}`,
  "EPSG:31468": `+proj=tmerc +lat_0=0 +lon_0=12 +k=1 +x_0=4500000 +y_0=0 ${DHDN}`,
  "EPSG:31469": `+proj=tmerc +lat_0=0 +lon_0=15 +k=1 +x_0=5500000 +y_0=0 ${DHDN}`,
  "EPSG:25830": "+proj=utm +zone=30 +ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs",
  "EPSG:25831": "+proj=utm +zone=31 +ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs",
  "EPSG:25832": "+proj=utm +zone=32 +ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs",
  "EPSG:25833": "+proj=utm +zone=33 +ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs",
  "EPSG:3414": "+proj=tmerc +lat_0=1.36666666666667 +lon_0=103.833333333333 +k=1 +x_0=28001.642 +y_0=38744.572 +ellps=WGS84 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs",
  "EPSG:26917": "+proj=utm +zone=17 +ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs",
  "EPSG:2154": "+proj=lcc +lat_0=46.5 +lon_0=3 +lat_1=49 +lat_2=44 +x_0=700000 +y_0=6600000 +ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs",
  "EPSG:27700": "+proj=tmerc +lat_0=49 +lon_0=-2 +k=0.9996012717 +x_0=400000 +y_0=-100000 +ellps=airy +towgs84=446.448,-125.157,542.06,0.15,0.247,0.842,-20.489 +units=m +no_defs",
  "EPSG:28992": "+proj=sterea +lat_0=52.1561605555556 +lon_0=5.38763888888889 +k=0.9999079 +x_0=155000 +y_0=463000 +ellps=bessel +towgs84=565.4171,50.3319,465.5524,-0.398957,0.343988,-1.8774,4.0725 +units=m +no_defs",
  "EPSG:3035": "+proj=laea +lat_0=52 +lon_0=10 +x_0=4321000 +y_0=3210000 +ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs",
  "EPSG:2180": "+proj=tmerc +lat_0=0 +lon_0=19 +k=0.9993 +x_0=500000 +y_0=-5300000 +ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs",
  "EPSG:3006": "+proj=utm +zone=33 +ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs",
  "EPSG:5514": "+proj=krovak +lat_0=49.5 +lon_0=24.8333333333333 +alpha=30.2881397527778 +k=0.9999 +x_0=0 +y_0=0 +ellps=bessel +towgs84=570.8,85.7,462.8,4.998,1.587,5.261,3.56 +units=m +no_defs",
  "EPSG:4326": "+proj=longlat +datum=WGS84 +no_defs",
  "EPSG:3857": "+proj=merc +a=6378137 +b=6378137 +lat_ts=0 +lon_0=0 +x_0=0 +y_0=0 +k=1 +units=m +nadgrids=@null +wktext +no_defs",
};

/** The coordinate system named by `value`, as {name, toWgs([x, y]) -> [lon, lat], fromWgs, geographic}.
 * null when it is not set ("Atlantis"). Takes EPSG codes, MATSim's names, proj strings and WKT.
 * Codes not listed above are looked up on epsg.io (only the code goes out). Throws BuildError if unknown. */
export async function parseCrs(value, env) {
  const raw = (value ?? "").trim();
  if (UNSET_CRS.has(raw.toLowerCase())) return null;
  let v = raw;
  const utm = /^wgs84_utm(\d{1,2})([ns])$/i.exec(v);
  if (utm) v = `EPSG:${(utm[2].toLowerCase() === "n" ? 32600 : 32700) + +utm[1]}`;
  v = MATSIM_CRS[v.toLowerCase()] ?? v;
  if (/^\d+$/.test(v)) v = `EPSG:${v}`;
  let def = null;
  const code = /^epsg:(\d+)$/i.exec(v);
  if (code) {
    const n = +code[1];
    v = `EPSG:${n}`;
    if (KNOWN_CRS[v]) def = KNOWN_CRS[v];
    else if ((n > 32600 && n <= 32660) || (n > 32700 && n <= 32760))
      def = `+proj=utm +zone=${n % 100}${n > 32700 ? " +south" : ""} +datum=WGS84 +units=m +no_defs`;
    else {
      try {
        const r = await env.fetch(`https://epsg.io/${n}.proj4`);
        const text = r.ok ? (await r.text()).trim() : "";
        if (text.startsWith("+proj")) def = text.replace(/\s*\+nadgrids=\S+/g, "").replace(/\s*\+type=crs/, "");
      } catch { /* offline */ }
      if (!def) failCrs(`cannot find coordinate system ${v}. Paste its proj string (from epsg.io) instead.`);
    }
  } else if (/^\+proj=|^(PROJCS|GEOGCS|PROJCRS|GEOGCRS)\[/i.test(v)) def = v;
  else failCrs(`cannot interpret coordinate system '${raw}'. Use e.g. EPSG:2056.`);
  let conv;
  try { conv = env.proj4(def, "EPSG:4326"); } catch {
    failCrs(`cannot use coordinate system '${raw}'. Use e.g. EPSG:2056.`);
  }
  const geographic = env.proj4.Proj(def).projName === "longlat";
  return { name: v, geographic, toWgs: p => conv.forward(p), fromWgs: p => conv.inverse(p) };
}

function inBox(pts, xmin, xmax, ymin, ymax) {
  let n = 0, inside = 0;
  for (const [x, y] of pts) { n++; if (x >= xmin && x <= xmax && y >= ymin && y <= ymax) inside++; }
  return inside / n;
}

/** Pick LV95 or LV03 from the coordinate range. */
async function guessSwissCrs(points, env) {
  if (inBox(points, 2_480_000, 2_840_000, 1_070_000, 1_300_000) > 0.9) {
    env.say("note: coordinate system not set in output_config, network matches EPSG:2056 (LV95)");
    return parseCrs("EPSG:2056", env);
  }
  if (inBox(points, 480_000, 840_000, 70_000, 300_000) > 0.9) {
    env.say("note: coordinate system not set in output_config, network matches EPSG:21781 (LV03)");
    return parseCrs("EPSG:21781", env);
  }
  failCrs("coordinate system not set in output_config and the network is not in a Swiss grid. " +
       "Set global.coordinateSystem in the config, or enter one, e.g. EPSG:25832.");
}

// ---------------------------------------------------------------- static inputs
async function readNetwork(file, onBytes) {
  const nodes = new Map(), links = [], car = [];
  let crs = null;
  await scan(file, new Set(["node", "link", "attribute"]), (tag, a, text) => {
    if (tag === "node") nodes.set(a.id, [+a.x, +a.y]);
    else if (tag === "link") {
      links.push([a.id, a.from, a.to]);
      if ((a.modes ?? "car").split(",").includes("car")) {
        const length = parseFloat(a.length), speed = parseFloat(a.freespeed);
        if (!Number.isNaN(length) && !Number.isNaN(speed)) car.push([a.from, a.to, length, speed]);
      }
    } else if (!nodes.size && a.name === "coordinateReferenceSystem") crs = text.trim() || null;  // network attributes come first
  }, { withText: true, onBytes });
  if (!nodes.size || !links.length) fail(`${file.name} has no nodes or links. Is it a MATSim network?`);
  const linkFrom = new Map(), linkMid = new Map();
  for (const [id, f, t] of links) {
    const A = nodes.get(f), B = nodes.get(t);
    if (A) linkFrom.set(id, A);
    if (A && B) linkMid.set(id, [(A[0] + B[0]) / 2, (A[1] + B[1]) / 2]);
  }
  return { linkFrom, linkMid, roads: { nodes, links: car }, crs };
}

export function trackKind(mode) {
  const m = (mode ?? "").toLowerCase();
  if (m.includes("tram")) return "tram";
  if (/rail|train|subway|metro|s-?bahn/.test(m)) return "rail";
  return null;
}

/** What a transit vehicle looks like in the 3D world: 'rail', 'tram', 'ferry' or 'bus' (anything else). */
export function vehicleKind(mode) {
  if (/ferry|ship|boat/.test((mode ?? "").toLowerCase())) return "ferry";
  return trackKind(mode) ?? "bus";
}

const lineRoute = (line, route) => `${line}\u0000${route}`;

/** Stop coordinates, the track kind of each (line, route) that runs on tracks, and the vehicle kind of every one. */
async function readSchedule(file, onBytes) {
  const stops = new Map(), kinds = new Map(), vkinds = new Map();
  let line = null, route = null;
  await scan(file, new Set(["stopFacility", "transitLine", "transitRoute", "transportMode"]), (tag, a, text) => {
    if (tag === "stopFacility") stops.set(a.id, [+a.x, +a.y]);
    else if (tag === "transitLine") line = a.id;
    else if (tag === "transitRoute") route = a.id;
    else if (route !== null) {
      const mode = text.trim(), k = trackKind(mode), key = lineRoute(line, route);
      if (k) kinds.set(key, k);
      vkinds.set(key, vehicleKind(mode));
      route = null;
    }
  }, { withText: true, onBytes });
  return { stops, kinds, vkinds };
}

/** Stations, their start fleet and the service from a shared_mobility service file, or null. */
async function readSharingStations(file, config, entries, linkMid, env, onBytes) {
  const ids = [], xy = [], byId = new Map(), byLink = new Map(), fleet = new Map();
  let skipped = 0;
  await scan(file, new Set(["station", "vehicle"]), (tag, a) => {
    if (tag === "station") {
      const pos = linkMid.get(a.link);
      if (!pos) { skipped++; return; }
      byId.set(a.id, ids.length); byLink.set(a.link, ids.length);
      ids.push(a.id); xy.push(pos);
    } else if (a.startStation) fleet.set(a.startStation, (fleet.get(a.startStation) ?? 0) + 1);
  }, { onBytes });
  if (!ids.length) {
    env.say(`note: ${file.name} has no stations on this network (a free-floating service?), no station layer`);
    return null;
  }
  if (skipped) env.say(`note: ${skipped} sharing stations are on links missing from the network and are left out`);
  // Which service this file belongs to. Events name it in their "service" attribute.
  const services = config ? config.services : [];
  const stations = services.filter(s => (s.serviceScheme ?? "").toLowerCase() !== "freefloating");
  let match = stations.filter(s => serviceFile(entries, s) === file);
  if (!match.length) {
    const sameName = stations.filter(s => (s.serviceInputFile ?? "").replace(/\\/g, "/").split("/").pop() === file.name);
    match = sameName.length === 1 ? sameName : stations.length === 1 ? stations : [];
  }
  const svc = match[0] ?? null;
  if (svc && stations.length > 1) env.say(`note: ${stations.length} station-based sharing services in the config, showing '${svc.id}'`);
  if (!svc && services.length) env.say(`note: cannot tell which sharing service ${file.name} belongs to; ` +
    "events of the services named in the config are left out");
  return {
    ids, xy, n0: ids.map(i => fleet.get(i) ?? 0), byId, byLink, service: svc?.id ?? null, mode: svc?.mode ?? null,
    // with no service identified, skip the events of every service the config names
    skip: new Set(svc ? [] : services.map(s => s.id)), ev: [],
  };
}

// ---------------------------------------------------------------- geometry helpers
const hypot = Math.hypot;
const dist = (a, b) => hypot(a[0] - b[0], a[1] - b[1]);

/** Douglas-Peucker: drop points closer than `tol` metres to the line through their neighbours. */
export function simplify(pts, tol) {
  if (pts.length < 3) return pts;
  const keep = new Uint8Array(pts.length);
  keep[0] = keep[pts.length - 1] = 1;
  const stack = [[0, pts.length - 1]];
  while (stack.length) {
    const [i, j] = stack.pop();
    const [ax, ay] = pts[i], [bx, by] = pts[j];
    const dx = bx - ax, dy = by - ay, ln = hypot(dx, dy);
    let far = 0, k = -1;
    for (let m = i + 1; m < j; m++) {
      const [x, y] = pts[m];
      const d = ln ? Math.abs(dx * (ay - y) - dy * (ax - x)) / ln : hypot(x - ax, y - ay);
      if (d > far) { far = d; k = m; }
    }
    if (k >= 0 && far > tol) { keep[k] = 1; stack.push([i, k], [k, j]); }
  }
  return pts.filter((_, i) => keep[i]);
}

/** A binary min-heap of [priority, value] pairs. */
class Heap {
  constructor() { this.p = []; this.v = []; }
  get size() { return this.p.length; }
  push(pri, val) {
    const p = this.p, v = this.v;
    let i = p.length;
    p.push(pri); v.push(val);
    while (i > 0) {
      const up = (i - 1) >> 1;
      if (p[up] <= pri) break;
      p[i] = p[up]; v[i] = v[up]; i = up;
    }
    p[i] = pri; v[i] = val;
  }
  pop() {  // -> [priority, value]
    const p = this.p, v = this.v, top = [p[0], v[0]];
    const lp = p.pop(), lv = v.pop(), n = p.length;
    if (n) {
      let i = 0;
      for (;;) {
        let c = 2 * i + 1;
        if (c >= n) break;
        if (c + 1 < n && p[c + 1] < p[c]) c++;
        if (p[c] >= lp) break;
        p[i] = p[c]; v[i] = v[c]; i = c;
      }
      p[i] = lp; v[i] = lv;
    }
    return top;
  }
}

/** Nearest-point lookups over many points (instead of a k-d tree): a grid of buckets. */
class PointIndex {
  constructor(xs, ys, ids, cell) {
    this.xs = xs; this.ys = ys; this.cell = cell;
    this.grid = new Map();
    for (const n of ids) {
      const key = `${Math.floor(xs[n] / cell)},${Math.floor(ys[n] / cell)}`;
      let b = this.grid.get(key);
      if (!b) this.grid.set(key, b = []);
      b.push(n);
    }
  }
  /** The nearest point within maxDist of (x, y), or -1. */
  nearest(x, y, maxDist) {
    const c = this.cell, ci = Math.floor(x / c), cj = Math.floor(y / c);
    const R = Math.ceil(maxDist / c) + 1;
    let best = -1, bestD = maxDist;
    for (let r = 0; r <= R; r++) {
      if (best >= 0 && (r - 1) * c > bestD) break;  // every point in ring r is at least (r - 1) cells away
      for (let i = ci - r; i <= ci + r; i++) {
        for (let j = cj - r; j <= cj + r; j++) {
          if (Math.max(Math.abs(i - ci), Math.abs(j - cj)) !== r) continue;
          const b = this.grid.get(`${i},${j}`);
          if (!b) continue;
          for (const n of b) {
            const d = hypot(this.xs[n] - x, this.ys[n] - y);
            if (d < bestD || (d === bestD && best < 0)) { bestD = d; best = n; }
          }
        }
      }
    }
    return best;
  }
}

function countTop(counter, n) {  // like Counter.most_common(n): highest counts first, ties in insertion order
  return [...counter.entries()].sort((a, b) => b[1] - a[1]).slice(0, n).map(e => e[0]);
}

// ---------------------------------------------------------------- OpenStreetMap tiles
export const OVERPASS = "https://overpass-api.de/api/interpreter";
const sleep = ms => new Promise(r => setTimeout(r, ms));

/** Overpass result for `query`, cached as kind/key (public OSM data, no run data). null if offline. */
async function osmTile(kind, key, query, what, env) {
  const cached = await env.cache?.get(kind, key).catch(() => null);
  if (cached) {
    try { return JSON.parse(cached); } catch { /* broken cache entry: download again */ }
  }
  const shared = await env.shared?.get(kind, key).catch(() => null);
  if (shared) {
    try { return JSON.parse(shared); } catch { /* broken file: ask Overpass */ }
  }
  let raw = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const r = await env.fetch(OVERPASS, {
        method: "POST", body: new URLSearchParams({ data: query }),
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
      });
      if (!r.ok) {
        const err = new Error(`HTTP ${r.status}`);
        err.final = r.status >= 400 && r.status < 500 && r.status !== 429;  // refused, not busy: retrying won't help
        throw err;
      }
      raw = await r.text();
      const data = JSON.parse(raw);
      if ((data.remark ?? "").includes("error")) throw new Error(data.remark.slice(0, 200));  // incomplete data
      await env.cache?.put(kind, key, raw).catch(() => {});
      return data;
    } catch (e) {
      if (attempt === 2 || e.final) {
        env.say(`note: could not download ${what} (${e.message || e})`);
        return null;
      }
      await sleep(10_000 * (attempt + 1));  // Overpass is busy now and then
    }
  }
  return null;
}

// ---------------------------------------------------------------- rail tracks
// Trains and trams often run on straight "artificial" links between stops (pt2matsim builds them so).
// To draw them on the real tracks, the tool downloads the railway lines of OpenStreetMap around the
// stops that sampled riders used and routes each hop between two stops along them.
const RAIL_TYPES = "rail|light_rail|narrow_gauge|subway|tram";
const MAX_RAIL_TILES = 60;
const SNAP_M = 300;           // a stop looks for tracks this close
const WRONG_TRACK = 4.0;      // cost factor for a train on tram tracks, or a tram on rail tracks
const SIDING = 1.3;           // cost factor for sidings: through tracks first
const MAX_TURN_COS = -0.17;   // no turns sharper than ~100 degrees: a train cannot reverse at a switch
const SIMPLIFY_M = 3.0;

export function railTile(lat, lon, env) {
  const query = `[out:json][timeout:240];way["railway"~"^(${RAIL_TYPES})$"]["service"!~"^(yard|spur)$"]` +
    `(${lat},${lon},${lat + 1},${lon + 1})->.w;.w out body qt;node(w.w);out skel qt;`;
  return osmTile("rail", `${lat}_${lon}`, query, "rail tracks", env);
}

const compatible = (track, kind) => track === 2 || (track === 1) === (kind === "tram");

/** Tracks as a graph of OSM nodes in the run's coordinates, with a grid index of track segments. */
class RailGraph {
  static CELL = 250.0;

  constructor(tiles, crs) {
    const ids = new Map(), ways = new Map();
    this.x = []; this.y = [];
    for (const data of tiles) {
      for (const el of data.elements ?? []) {
        if (el.type === "node" && !ids.has(el.id)) {
          ids.set(el.id, this.x.length);
          const [x, y] = crs.fromWgs([el.lon, el.lat]);
          this.x.push(x); this.y.push(y);
        } else if (el.type === "way") ways.set(el.id, el);
      }
    }
    // adjacency: node -> [neighbour, length, track kind 0 rail / 1 tram / 2 both, cost factor] flattened
    this.adj = this.x.map(() => []);
    this.grid = new Map();
    for (const w of ways.values()) {
      const tags = w.tags ?? {};
      const kind = { tram: 1, light_rail: 2 }[tags.railway] ?? 0;
      const factor = tags.service === "siding" ? SIDING : 1.0;
      const refs = (w.nodes ?? []).filter(n => ids.has(n)).map(n => ids.get(n));
      for (let i = 0; i + 1 < refs.length; i++) {
        const a = refs[i], b = refs[i + 1];
        if (a === b) continue;
        const d = hypot(this.x[a] - this.x[b], this.y[a] - this.y[b]);
        this.adj[a].push([b, d, kind, factor]); this.adj[b].push([a, d, kind, factor]);
        this.index(a, b, kind);
      }
    }
  }

  index(a, b, kind) {
    const c = RailGraph.CELL;
    const [x0, x1] = [this.x[a], this.x[b]].sort((p, q) => p - q), [y0, y1] = [this.y[a], this.y[b]].sort((p, q) => p - q);
    for (let i = Math.floor(x0 / c); i <= Math.floor(x1 / c); i++) {
      for (let j = Math.floor(y0 / c); j <= Math.floor(y1 / c); j++) {
        const key = `${i},${j}`;
        let cell = this.grid.get(key);
        if (!cell) this.grid.set(key, cell = []);
        cell.push([a, b, kind]);
      }
    }
  }

  /** Track segments near (x, y): [[a, b, px, py, offset]], the point on the segment closest to it. */
  snaps(x, y, kind) {
    const c = RailGraph.CELL, r = Math.floor(SNAP_M / c) + 1;
    const ci = Math.floor(x / c), cj = Math.floor(y / c);
    const out = [], seen = new Set();
    for (let i = ci - r; i <= ci + r; i++) {
      for (let j = cj - r; j <= cj + r; j++) {
        for (const [a, b, k] of this.grid.get(`${i},${j}`) ?? []) {
          const key = a * this.x.length + b;
          if (seen.has(key) || !compatible(k, kind)) continue;
          seen.add(key);
          const ax = this.x[a], ay = this.y[a], bx = this.x[b], by = this.y[b];
          const dx = bx - ax, dy = by - ay;
          const f = Math.max(0, Math.min(1, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)));
          const px = ax + f * dx, py = ay + f * dy, d = hypot(x - px, y - py);
          if (d <= SNAP_M) out.push([a, b, px, py, d]);
        }
      }
    }
    return out;
  }

  /** Track polyline from stop `start` to stop `end` (both [x, y]), or null if there is no sensible one.
   * A* over (node, previous node), so the path never turns back at a switch. Leaving a stop costs
   * twice the distance to the track, so stops take the nearest suitable track. */
  route(start, end, kind) {
    const src = this.snaps(start[0], start[1], kind), dst = this.snaps(end[0], end[1], kind);
    if (!src.length || !dst.length) return null;
    const limit = 3 * dist(start, end) + 3000;
    const [ex, ey] = end, X = this.x, Y = this.y;
    const h = n => hypot(X[n] - ex, Y[n] - ey);
    const N = X.length;
    const sk = (n, prev) => n * N + prev;  // state: at n, came from prev
    // where the path can leave the track to reach the end stop: node -> [[towards node, px, py, extra cost]]
    const goal = new Map();
    const addGoal = (n, other, px, py, extra) => { let g = goal.get(n); if (!g) goal.set(n, g = []); g.push([other, px, py, extra]); };
    for (const [a, b, px, py, d] of dst) {
      addGoal(a, b, px, py, hypot(X[a] - px, Y[a] - py) + 2 * d);
      addGoal(b, a, px, py, hypot(X[b] - px, Y[b] - py) + 2 * d);
    }
    const best = new Map(), parent = new Map(), heap = new Heap();
    const finals = [];  // [cost, last state or null, start x, start y, end x, end y]
    let minFinal = Infinity;
    const addFinal = f => { finals.push(f); if (f[0] < minFinal) minFinal = f[0]; };
    for (const [a, b, px, py, d] of src) {
      for (const [n, other] of [[a, b], [b, a]]) {
        const g = 2 * d + hypot(X[n] - px, Y[n] - py);
        const s = sk(n, other);  // moving away from `other`, so the next step must not turn back to it
        if (g < (best.get(s) ?? Infinity)) {
          best.set(s, g); parent.set(s, ["start", px, py]);
          heap.push(g + h(n), [g, n, other]);
        }
      }
    }
    for (const [a, b, px, py, d] of src) {  // both stops on the same segment: straight along it
      for (const [a2, b2, qx, qy, d2] of dst) {
        if ((a === a2 && b === b2) || (a === b2 && b === a2)) addFinal([2 * d + hypot(px - qx, py - qy) + 2 * d2, null, px, py, qx, qy]);
      }
    }
    let expanded = 0;
    while (heap.size) {
      const [f, [g, n, prev]] = heap.pop();
      if (finals.length && f >= minFinal) break;
      if (g > (best.get(sk(n, prev)) ?? Infinity) || f > limit) continue;
      if (++expanded > 300_000) break;
      const vx = X[n] - X[prev], vy = Y[n] - Y[prev], vl = hypot(vx, vy) || 1.0;
      for (const [towards, qx, qy, extra] of goal.get(n) ?? []) {
        const tx = X[towards] - X[n], ty = Y[towards] - Y[n];
        if (towards !== prev && (vx * tx + vy * ty) / (vl * (hypot(tx, ty) || 1.0)) >= MAX_TURN_COS)
          addFinal([g + extra, [n, prev], null, null, qx, qy]);
      }
      for (const [m, d, k, factor] of this.adj[n]) {
        if (m === prev) continue;
        const wx = X[m] - X[n], wy = Y[m] - Y[n], wl = hypot(wx, wy) || 1.0;
        if ((vx * wx + vy * wy) / (vl * wl) < MAX_TURN_COS) continue;
        const g2 = g + d * factor * (compatible(k, kind) ? 1.0 : WRONG_TRACK);
        const s2 = sk(m, n);
        if (g2 < (best.get(s2) ?? Infinity)) {
          best.set(s2, g2); parent.set(s2, [n, prev]);
          heap.push(g2 + h(m), [g2, m, n]);
        }
      }
    }
    if (!finals.length) return null;
    let [cost, state, px, py, qx, qy] = finals.reduce((a, b) => (b[0] < a[0] ? b : a));
    if (cost > limit) return null;
    const pts = [[qx, qy]];
    while (state !== null && pts.length < 1_000_000) {
      pts.push([X[state[0]], Y[state[0]]]);
      const p = parent.get(sk(state[0], state[1]));
      if (p[0] === "start") { px = p[1]; py = p[2]; state = null; } else state = p;
    }
    pts.push([px, py]);
    pts.reverse();
    return simplify(pts, SIMPLIFY_M);
  }
}

/** Track polylines for the hops (Map key -> [from stop, to stop, kind]): Map key -> [[x, y], ...]. */
async function railTracks(hops, stops, crs, env) {
  if (crs.geographic) {
    env.say("note: the run's coordinates are in degrees, not metres; trains move in straight lines");
    return new Map();
  }
  const tiles = new Map();  // "lat_lon" -> [lat, lon]
  const tileOf = p => { const [lon, lat] = crs.toWgs(p); return [Math.floor(lat), Math.floor(lon)]; };
  for (const [a, b] of hops.values()) {
    const [ax, ay] = stops.get(a), [bx, by] = stops.get(b);
    const steps = Math.max(1, Math.floor(hypot(bx - ax, by - ay) / 20000));  // tiles along the hop, every 20 km
    for (let s = 0; s <= steps; s++) {
      const t = tileOf([ax + (bx - ax) * s / steps, ay + (by - ay) * s / steps]);
      tiles.set(t.join("_"), t);
    }
  }
  let chosen = [...tiles.values()];
  if (chosen.length > MAX_RAIL_TILES) {
    env.say(`note: the trains cover ${chosen.length} map tiles of 1 degree, only the ${MAX_RAIL_TILES} with most stops get tracks`);
    const count = new Map(), at = new Map();
    for (const [a, b] of hops.values()) {
      for (const s of [a, b]) {
        const t = tileOf(stops.get(s)), k = t.join("_");
        count.set(k, (count.get(k) ?? 0) + 1); at.set(k, t);
      }
    }
    chosen = countTop(count, MAX_RAIL_TILES).map(k => at.get(k));
  }
  chosen.sort((p, q) => p[0] - q[0] || p[1] - q[1]);
  const data = [];
  for (let i = 0; i < chosen.length; i++) {
    env.progress({ step: `Rail tracks: tile ${i + 1} of ${chosen.length}` });
    const tile = await railTile(chosen[i][0], chosen[i][1], env);
    if (!tile) {
      env.say("note: trains and trams move in straight lines between stops where tracks are missing");
      break;
    }
    data.push(tile);
  }
  if (!data.length) return new Map();
  env.progress({ step: "Routing trains on the tracks" });
  const graph = new RailGraph(data, crs);
  const out = new Map();
  for (const [key, [a, b, kind]] of hops) {
    const line = graph.route(stops.get(a), stops.get(b), kind);
    if (line) out.set(key, line);
  }
  return out;
}

// ---------------------------------------------------------------- footpaths
// MATSim teleports walkers: it knows only where and when a walk starts and ends. To draw walkers on the
// street, the tool downloads the walkable ways of OpenStreetMap (footways, pedestrian zones, steps,
// paths and streets) in small tiles where sampled agents walk, and gives each walk its shortest path.
// The start and end times stay MATSim's; only the shape changes.
const WALK_WAYS = "footway|pedestrian|path|steps|living_street|residential|service|unclassified|track|cycleway|" +
  "corridor|platform|tertiary|tertiary_link|secondary|secondary_link|primary|primary_link";
export const WALK_TILE = 0.05;     // degrees: about 5.5 x 3.8 km in Switzerland
const MAX_WALK_TILES = 40;  // the tiles with most walks get paths, the other walks stay straight
const WALK_SNAP_M = 250;    // a walk's start or end looks for a path this close
const WALK_STEP_M = 25;     // long path segments get extra points this far apart, so start and end snap close
const MAX_WALK_M = 5000;    // longer walks stay straight
const WALK_SIMPLIFY_M = 5.0;

export const isWalk = mode => mode.toLowerCase().includes("walk");

export function walkTile(i, j, env) {
  const r = v => Math.round(v * 1e4) / 1e4;
  const s = r(i * WALK_TILE), w = r(j * WALK_TILE), n = r(s + WALK_TILE), e = r(w + WALK_TILE);
  const query = `[out:json][timeout:240];way["highway"~"^(${WALK_WAYS})$"]["foot"!~"^(no|private)$"]` +
    `["access"!~"^(no|private)$"](${s},${w},${n},${e})->.w;.w out skel qt;node(w.w);out skel qt;`;
  return osmTile("walk", `${i}_${j}`, query, "footpaths", env);
}

/** Walkable ways as an undirected graph in the run's coordinates. */
function walkGraph(tiles, crs) {
  const ids = new Map(), ways = new Map(), x = [], y = [];
  for (const data of tiles) {
    for (const el of data.elements ?? []) {
      if (el.type === "node") {
        if (!ids.has(el.id)) {
          ids.set(el.id, x.length);
          const p = crs.fromWgs([el.lon, el.lat]);
          x.push(p[0]); y.push(p[1]);
        }
      } else if (el.type === "way") ways.set(el.id, el.nodes);
    }
  }
  const edges = [];  // a, b pairs, flat
  for (const refs of ways.values()) {
    const r = refs.filter(n => ids.has(n)).map(n => ids.get(n));
    for (let k = 0; k + 1 < r.length; k++) {
      let u = r[k];
      const v = r[k + 1];
      const d = hypot(x[v] - x[u], y[v] - y[u]);
      const steps = Math.floor(d / WALK_STEP_M);
      const x0 = x[u], y0 = y[u], dx = x[v] - x[u], dy = y[v] - y[u];
      for (let s = 1; s <= steps; s++) {  // extra points on long segments, evenly spaced
        const f = s / (steps + 1);
        x.push(x0 + dx * f); y.push(y0 + dy * f);
        edges.push(u, x.length - 1); u = x.length - 1;
      }
      edges.push(u, v);
    }
  }
  const g = new Graph(x.length);
  for (let k = 0; k < edges.length; k += 2) {
    const a = edges[k], b = edges[k + 1], w = hypot(x[a] - x[b], y[a] - y[b]) + 1e-3;
    g.add(a, b, w); g.add(b, a, w);
  }
  g.finish();
  const all = Array.from(x.keys());
  return { graph: g, X: x, Y: y, index: new PointIndex(x, y, all, 50) };
}

/** Give the sampled walks a path on OSM footpaths: adds a hop to each walk leg that gets one and
 * returns Map ("walk\0n") -> [[x, y], ...], in run coordinates. */
async function walkTracks(legs, crs, env) {
  if (crs.geographic) {
    env.say("note: the run's coordinates are in degrees, not metres; walkers move in straight lines");
    return new Map();
  }
  let walks = legs.filter(l => isWalk(l.mode) && !l.routed && l.pts.length === 2 &&
    30 < dist(l.pts[0].slice(1), l.pts[1].slice(1)) && dist(l.pts[0].slice(1), l.pts[1].slice(1)) < MAX_WALK_M);
  if (!walks.length) return new Map();
  const need = [], count = new Map();  // tiles each walk needs: those of its bounding box
  for (const leg of walks) {
    const [lon0, lat0] = crs.toWgs(leg.pts[0].slice(1)), [lon1, lat1] = crs.toWgs(leg.pts[1].slice(1));
    const [i0, i1] = [Math.floor(lat0 / WALK_TILE), Math.floor(lat1 / WALK_TILE)].sort((a, b) => a - b);
    const [j0, j1] = [Math.floor(lon0 / WALK_TILE), Math.floor(lon1 / WALK_TILE)].sort((a, b) => a - b);
    const t = [];
    for (let i = i0; i <= i1; i++) for (let j = j0; j <= j1; j++) t.push(`${i}_${j}`);
    need.push(t);
    for (const k of t) count.set(k, (count.get(k) ?? 0) + 1);
  }
  const chosen = countTop(count, MAX_WALK_TILES).map(k => k.split("_").map(Number))
    .sort((p, q) => p[0] - q[0] || p[1] - q[1]);
  if (count.size > MAX_WALK_TILES) env.say(`note: the walks cover ${count.size} map tiles; the ${MAX_WALK_TILES} ` +
    "with most walks get footpaths, walks elsewhere stay straight");
  const tiles = [], loaded = new Set();
  for (let n = 0; n < chosen.length; n++) {
    const [i, j] = chosen[n];
    env.progress({ step: `Footpaths: tile ${n + 1} of ${chosen.length}` });
    const tile = await walkTile(i, j, env);
    if (!tile) { env.say("note: walks where footpaths are missing stay straight"); break; }
    tiles.push(tile); loaded.add(`${i}_${j}`);
  }
  walks = walks.filter((_, k) => need[k].every(t => loaded.has(t)));
  if (!tiles.length || !walks.length) return new Map();
  env.progress({ step: "Routing walks on the footpaths" });
  const { graph, X, Y, index } = walkGraph(tiles, crs);
  // cost in metres; give up on long detours (e.g. a start snapped to the wrong side of a river)
  return shortestPaths(walks, graph, X, Y, index, WALK_SNAP_M, beeline => 2.5 * beeline + 500, WALK_SIMPLIFY_M, "walk");
}

/** A weighted graph in compressed rows (like scipy's csr_matrix). Parallel edges keep the lowest weight. */
class Graph {
  constructor(n) { this.n = n; this.edges = []; }
  add(a, b, w) { this.edges.push(a, b, w); }
  finish() {
    const n = this.n, e = this.edges, m = e.length / 3;
    const best = new Map();  // a * n + b -> lowest weight
    for (let k = 0; k < m; k++) {
      const key = e[3 * k] * n + e[3 * k + 1], w = e[3 * k + 2];
      const old = best.get(key);
      if (old === undefined || w < old) best.set(key, w);
    }
    this.start = new Int32Array(n + 1);
    for (const key of best.keys()) this.start[Math.floor(key / n) + 1]++;
    for (let i = 0; i < n; i++) this.start[i + 1] += this.start[i];
    this.to = new Int32Array(best.size); this.w = new Float64Array(best.size);
    const fill = this.start.slice(0, n);
    for (const [key, w] of best) {
      const a = Math.floor(key / n), b = key - a * n, k = fill[a]++;
      this.to[k] = b; this.w[k] = w;
    }
    this.edges = null;
  }
  /** Dijkstra from src, exploring no further than `limit`, until every node in `targets` is settled.
   * Returns dist(node) (Infinity if not reached) and pred(node). Valid until the next search. */
  dijkstra(src, limit, targets) {
    if (!this.d) {  // reused between searches: a stamp marks the entries of the current one
      this.d = new Float64Array(this.n); this.pred = new Int32Array(this.n);
      this.seen = new Int32Array(this.n); this.done = new Int32Array(this.n); this.stamp = 0;
    }
    const { d, pred, seen, done } = this, id = ++this.stamp, heap = new Heap();
    const want = new Set(targets);
    let left = want.size;
    d[src] = 0; pred[src] = -1; seen[src] = id; heap.push(0, src);
    while (heap.size && left > 0) {
      const [du, u] = heap.pop();
      if (done[u] === id || du > d[u]) continue;
      done[u] = id;
      if (want.has(u)) left--;
      for (let k = this.start[u]; k < this.start[u + 1]; k++) {
        const v = this.to[k], dv = du + this.w[k];
        if (dv <= limit && (seen[v] !== id || dv < d[v])) { d[v] = dv; pred[v] = u; seen[v] = id; heap.push(dv, v); }
      }
    }
    return { dist: n => (seen[n] === id ? d[n] : Infinity), pred: n => pred[n] };
  }
}

/** Route two-point legs on a graph (nodes at X, Y; `index` finds the nodes a leg may start or end at).
 * `limit(beeline)` is the highest cost a path may have. Adds a hop to each leg that gets a path and
 * returns Map (name\0n) -> [[x, y], ...]. On an undirected graph, each search starts from the end
 * that more legs share (many walks start or end at one stop). */
function shortestPaths(legs, graph, X, Y, index, snapM, limit, tol, name, directed = false) {
  const snapped = legs.map(l => [index.nearest(l.pts[0][1], l.pts[0][2], snapM), index.nearest(l.pts[1][1], l.pts[1][2], snapM)]);
  const ok = snapped.map(([u, v]) => u >= 0 && v >= 0);
  const uses = new Map();
  snapped.forEach((pair, k) => { if (ok[k]) for (const v of pair) uses.set(v, (uses.get(v) ?? 0) + 1); });
  const groups = new Map();
  snapped.forEach(([u, v], k) => {
    if (!ok[k] || u === v) return;
    const [src, dst, rev] = !directed && uses.get(v) > uses.get(u) ? [v, u, true] : [u, v, false];
    let g = groups.get(src);
    if (!g) groups.set(src, g = []);
    g.push([k, dst, rev]);
  });
  const tracks = new Map();
  for (const [src, group] of groups) {
    const caps = group.map(([k]) => limit(dist(legs[k].pts[0].slice(1), legs[k].pts[1].slice(1))));
    let cap = 0;
    for (const c of caps) if (c > cap) cap = c;
    const { dist: d, pred } = graph.dijkstra(src, cap, group.map(g => g[1]));
    group.forEach(([k, dst, rev], gi) => {
      if (!(d(dst) <= caps[gi])) return;
      const path = [dst];
      while (path[path.length - 1] !== src && path.length < 1_000_000) path.push(pred(path[path.length - 1]));
      if (!rev) path.reverse();
      const key = `${name}\u0000${tracks.size}`;
      tracks.set(key, simplify(path.map(n => [X[n], Y[n]]), tol));
      legs[k].hops = [[0, 1, key]];
    });
  }
  return tracks;
}

// ---------------------------------------------------------------- car passengers
// MATSim teleports car passengers too. They follow the fastest path on the run's own car links
// (free-flow travel times), so they drive where the cars drive. No download.
const PASSENGER_SNAP_M = 1000;
const LOCAL_SPEED = 60 / 3.6;  // passengers get on and off at nodes of roads this slow or slower, not on motorways

export const isPassenger = mode => { const m = mode.toLowerCase(); return m.includes("passenger") || m === "ride"; };

/** Give the sampled car-passenger legs a path on the run's car links (see shortestPaths). */
function passengerTracks(legs, roads, env) {
  const rides = legs.filter(l => isPassenger(l.mode) && !l.routed && l.pts.length === 2);
  if (!rides.length || !roads.links.length) return new Map();
  env.progress({ step: "Routing car passengers on the roads" });
  const idx = new Map(), X = [], Y = [];
  for (const [n, [x, y]] of roads.nodes) { idx.set(n, X.length); X.push(x); Y.push(y); }
  const graph = new Graph(X.length), local = new Set();
  for (const [u, v, length, speed] of roads.links) {
    if (idx.has(u) && idx.has(v) && speed > 0) {
      graph.add(idx.get(u), idx.get(v), Math.max(length, 0.1) / speed);  // parallel links: the fastest stays
      if (speed <= LOCAL_SPEED) { local.add(idx.get(u)); local.add(idx.get(v)); }
    }
  }
  if (!local.size) return new Map();
  graph.finish();
  const index = new PointIndex(X, Y, [...local].sort((a, b) => a - b), 200);
  // cost in seconds at free flow: allow a path up to about 3x slower than a straight line at 30 km/h
  const tracks = shortestPaths(rides, graph, X, Y, index, PASSENGER_SNAP_M, beeline => beeline / 3 + 300, SIMPLIFY_M, "ride", true);
  for (const leg of rides) if (leg.hops) leg.routed = true;  // a path like a car's: it gets a trip line
  return tracks;
}

// ---------------------------------------------------------------- events
// shared_mobility contrib events -> kind in the page (0 pickup, 1 dropoff, 2 failed pickup, 3 failed dropoff)
const SHARING_EVENTS = { "sharing vehicle pickup": 0, "sharing vehicle dropoff": 1,
  "sharing vehicle pickup failed": 2, "sharing vehicle dropoff failed": 3 };
// The contrib's pickup and dropoff activities. A failed event names the station the agent is sent to
// next, so the station that failed is the one of the activity the agent is at.
const SHARING_ACTS = new Set(["sharing pickup interaction", "sharing dropoff interaction"]);

// crc32 as in zlib, for the stable sample of persons
const CRC = new Int32Array(256);
for (let n = 0; n < 256; n++) {
  let c = n;
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  CRC[n] = c;
}
const utf8 = new TextEncoder();
export function crc32(str) {
  let c = -1;
  for (let i = 0; i < str.length; i++) {
    const ch = str.charCodeAt(i);
    if (ch >= 128) {  // not ASCII: hash the UTF-8 bytes
      c = -1;
      for (const b of utf8.encode(str)) c = CRC[(c ^ b) & 0xff] ^ (c >>> 8);
      return (c ^ -1) >>> 0;
    }
    c = CRC[(c ^ ch) & 0xff] ^ (c >>> 8);
  }
  return (c ^ -1) >>> 0;
}

/**
 * Legs of the sampled agents. With `sharing` (stations), also collects every station event of that
 * service into sharing.ev, for all agents: the page shows only station totals.
 *
 * With `routeKinds` (line\0route -> 'rail' / 'tram'), each ride of a train or tram between two stops is
 * noted in leg.hops as [index of the departure point, index of the arrival point, hop key], and the hop
 * in `hops` (key -> [from stop, to stop, kind]), so it can be drawn along the tracks later.
 *
 * With `vehicleKinds` (line\0route -> 'bus' / 'tram' / 'rail' / 'ferry'), a leg that rides a transit vehicle
 * gets leg.veh = [vehicle id, kind] and leg.board = boarding time, so the 3D world draws one vehicle for
 * all its sampled riders, and the rider standing at the stop until it comes.
 */
async function buildLegs(file, sample, { linkFrom, linkMid }, stops, sharing, routeKinds, vehicleKinds, onBytes, say) {
  const threshold = Math.trunc(sample * 10000);
  const sampled = p => p !== undefined && !p.startsWith("pt_") && crc32(p) % 10000 < threshold;
  const pos = new Map();       // person -> last known [x, y]
  const openLeg = new Map();   // person -> {mode, pts: [[t, x, y]]}
  const pending = new Map();   // person -> leg that arrived; end point may be refined by actstart
  const riders = new Map();    // vehicle -> Set of sampled persons on board
  const legs = [], hopDefs = new Map();
  const atStation = new Map(); // person -> link of the sharing pickup/dropoff activity they are at (all persons)
  routeKinds ??= new Map(); vehicleKinds ??= new Map();
  const onTracks = new Map();  // vehicle -> 'rail' / 'tram', for transit vehicles of routes that run on tracks
  const transit = new Map();   // transit vehicle -> 'bus' / 'tram' / 'rail' / 'ferry', from TransitDriverStarts
  const departed = new Map();  // such vehicle -> [stop it last left, time]
  let nEvents = 0;
  const started = Date.now();

  // Add a point to the person's open leg. Points closer in time than MIN_POINT_GAP_S replace the last one
  // if it lies on the way (within MERGE_TOL_M of the line from the one before to the new point), unless that
  // one is kept (a train's stop, which a track hop starts from).
  const add = (person, t, xy, routed = false, keep = false) => {
    const leg = openLeg.get(person);
    if (!leg || !xy) return;
    if (routed) leg.routed = true;
    const pts = leg.pts, n = pts.length;
    if (n > 1 && t - pts[n - 1][0] < MIN_POINT_GAP_S && leg.kept !== n - 1 &&
        simplify([[pts[n - 2][1], pts[n - 2][2]], [pts[n - 1][1], pts[n - 1][2]], xy], MERGE_TOL_M).length === 2) {
      pts[n - 1] = [t, xy[0], xy[1]];
    } else pts.push([t, xy[0], xy[1]]);
    if (keep) leg.kept = pts.length - 1;
  };
  const finish = person => {
    const leg = pending.get(person);
    pending.delete(person);
    if (leg && leg.pts.length >= 2 && leg.pts[leg.pts.length - 1][0] > leg.pts[0][0]) legs.push(leg);
  };
  const actXY = a => (a.x !== undefined && a.y !== undefined) ? [+a.x, +a.y] : linkMid.get(a.link);

  await scan(file, new Set(["event"]), (_, a) => {
    nEvents++;
    if (nEvents % 5_000_000 === 0) say(`  ${nEvents / 1e6} M events read (${Math.round((Date.now() - started) / 1000)} s)`);
    const typ = a.type, t = +a.time, person = a.person;
    switch (typ) {
      case "actend":
        if (sampled(person)) { finish(person); pos.set(person, actXY(a)); }
        break;
      case "departure":
        if (sampled(person)) {
          finish(person);
          const start = pos.get(person) || linkMid.get(a.link);
          if (start) openLeg.set(person, { mode: a.legMode || "unknown", pts: [[t, start[0], start[1]]] });
        }
        break;
      case "PersonEntersVehicle":
        if (sampled(person) && openLeg.has(person)) {
          const veh = a.vehicle, leg = openLeg.get(person);
          let r = riders.get(veh);
          if (!r) riders.set(veh, r = new Set());
          r.add(person);
          if (transit.has(veh) && !leg.veh) { leg.veh = [veh, transit.get(veh)]; leg.board = t; }
          const last = leg.pts[leg.pts.length - 1];
          add(person, t, [last[1], last[2]]);  // waited here until boarding
        }
        break;
      case "PersonLeavesVehicle": {
        const r = riders.get(a.vehicle);
        if (r) { r.delete(person); if (!r.size) riders.delete(a.vehicle); }
        break;
      }
      case "entered link": {
        const r = riders.get(a.vehicle);
        if (r) { const xy = linkFrom.get(a.link); for (const p of r) add(p, t, xy, true); }
        break;
      }
      case "VehicleArrivesAtFacility":
      case "VehicleDepartsAtFacility": {
        const veh = a.vehicle, stop = a.facility, kind = onTracks.get(veh);
        const r = riders.get(veh);
        if (r) {
          const xy = stops.get(stop);
          const prev = kind && typ === "VehicleArrivesAtFacility" ? departed.get(veh) : undefined;
          for (const p of r) {
            const leg = openLeg.get(p);
            let hop = null;
            if (prev && xy && leg && prev[0] !== stop && leg.kept !== undefined && leg.pts[leg.kept][0] === prev[1])
              hop = [leg.kept, prev[0], stop, kind];  // on board since the last stop
            add(p, t, xy, true, kind !== undefined);
            if (hop && leg.pts[leg.pts.length - 1][0] === t) {
              const key = `${hop[1]}\u0000${hop[2]}\u0000${hop[3]}`;
              hopDefs.set(key, [hop[1], hop[2], hop[3]]);
              (leg.hops ??= []).push([hop[0], leg.pts.length - 1, key]);
            }
          }
        }
        if (kind && typ === "VehicleDepartsAtFacility") departed.set(veh, [stop, t]);
        break;
      }
      case "TransitDriverStarts": {
        const key = lineRoute(a.transitLineId, a.transitRouteId);
        transit.set(a.vehicleId, vehicleKinds.get(key) ?? "bus");
        const kind = routeKinds.get(key);
        if (kind) { onTracks.set(a.vehicleId, kind); departed.delete(a.vehicleId); }
        break;
      }
      case "arrival":
        if (sampled(person) && openLeg.has(person)) {
          const leg = openLeg.get(person);
          openLeg.delete(person);
          const end = linkMid.get(a.link);
          if (end) leg.pts.push([t, end[0], end[1]]);
          pending.set(person, leg);
          if (end) pos.set(person, end);
        }
        break;
      case "actstart":
        if (sharing && SHARING_ACTS.has(a.actType)) atStation.set(person, a.link);
        if (sampled(person)) {
          const xy = actXY(a), leg = pending.get(person);
          if (leg && xy) { const l = leg.pts[leg.pts.length - 1]; leg.pts[leg.pts.length - 1] = [l[0], xy[0], xy[1]]; }
          finish(person);
          if (xy) pos.set(person, xy);
        }
        break;
      default: {
        const kind = SHARING_EVENTS[typ];
        if (kind === undefined || !sharing) break;
        const svc = a.service;
        const other = sharing.service !== null ? svc !== sharing.service : sharing.skip.has(svc);
        if (other) break;  // another service's event
        const here = atStation.get(person);
        atStation.delete(person);
        let s;
        if (kind >= 2) s = sharing.byLink.get(here);  // failed: the event names the next station to try, so use where the agent is
        else if (a.station) s = sharing.byId.get(a.station);
        else s = sharing.byLink.get(a.link);
        if (s !== undefined) sharing.ev.push([round(t), s, kind]);
      }
    }
  }, { onBytes });
  for (const p of [...pending.keys()]) finish(p);
  if (nEvents === 0) fail(`${file.name} contains no events.`);
  const hops = new Map();  // only those of kept legs
  for (const leg of legs) for (const h of leg.hops ?? []) hops.set(h[2], hopDefs.get(h[2]));
  return { legs, hops, nEvents };
}

// ---------------------------------------------------------------- output
/** The leg's points without those inside hops that have a track, and [point index, track, ...]:
 * the page inserts each track after that point. Hops are [first point, last point, track key]. */
function withTracks(leg, trackIdx) {
  let pts = [], i = 0;
  const refs = [];
  for (const [kDep, kArr, key] of leg.hops ?? []) {
    const t = trackIdx.get(key);
    if (t === undefined || kDep < i) continue;
    pts = pts.concat(leg.pts.slice(i, kDep + 1));
    refs.push(pts.length - 1, t);
    i = kArr;
  }
  return [pts.concat(leg.pts.slice(i)), refs];
}

/** First value, then the steps between values: shorter numbers in the JSON. */
const deltas = v => v.map((x, i) => (i ? x - v[i - 1] : x));

export const VEHICLE_KINDS = ["bus", "tram", "rail", "ferry"];
/** Round half to even, like Python's round(): 21852.5 -> 21852 (event times are often whole and a half). */
const round = v => { const r = Math.round(v); return Math.abs(v % 1) === 0.5 && r % 2 !== 0 ? r - 1 : r; };
const e5 = v => round(v * 1e5);
const cmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0);

/** The page's data. With `vehicles` (the 3D world), also "pt": [[leg, vehicle, kind, boarded after s], ...]
 * for legs on a transit vehicle. Vehicles are numbered here; their ids stay out of the page. */
function toPayload(legs, crs, sharing, tracks, vehicles) {
  const modes = [...new Set(legs.map(l => l.mode))].sort(cmp);
  const modeIdx = new Map(modes.map((m, i) => [m, i]));
  const trackIdx = new Map([...tracks.keys()].map((k, i) => [k, i]));
  let out = [];
  const startsLon = [], startsLat = [];
  for (const leg of legs) {
    const [pts, refs] = withTracks(leg, trackIdx);
    const lonI = [], latI = [];
    let first = null;
    for (const p of pts) {
      const [lon, lat] = crs.toWgs([p[1], p[2]]);
      if (!first) first = [lon, lat];
      lonI.push(e5(lon)); latI.push(e5(lat));
    }
    const routed = leg.routed ? 1 : 0;  // moved through network links or transit stops, not teleported
    const row = [modeIdx.get(leg.mode), pts.map(p => round(p[0])), lonI, latI, routed];
    if (refs.length) row.push(refs);
    out.push(row);
    startsLon.push(first[0]); startsLat.push(first[1]);
  }
  const order = out.map((_, i) => i).sort((a, b) => out[a][1][0] - out[b][1][0]);
  out = order.map(i => out[i]);
  // initial view: close in on the median leg start, where 3D buildings show
  startsLon.sort((a, b) => a - b); startsLat.sort((a, b) => a - b);
  const centre = [startsLon[startsLon.length >> 1], startsLat[startsLat.length >> 1]];
  const payload = { modes, legs: out, centre };
  if (vehicles) {
    const vehIdx = new Map(), pt = [];
    order.forEach((i, k) => {
      const leg = legs[i];
      if (!leg.veh) return;
      const [veh, kind] = leg.veh;
      if (!vehIdx.has(veh)) vehIdx.set(veh, vehIdx.size);
      pt.push([k, vehIdx.get(veh), VEHICLE_KINDS.indexOf(kind), Math.max(0, round(leg.board - leg.pts[0][0]))]);
    });
    payload.vkinds = VEHICLE_KINDS; payload.pt = pt;
  }
  if (tracks.size) {  // train, tram and walk paths, each stored once: [[lon, Δlon, ...], [lat, Δlat, ...]]
    payload.tracks = [...tracks.values()].map(line => {
      const ll = line.map(p => crs.toWgs(p));
      return [deltas(ll.map(p => e5(p[0]))), deltas(ll.map(p => e5(p[1])))];
    });
  }
  if (sharing) {
    const ll = sharing.xy.map(p => crs.toWgs(p));
    payload.sharing = {
      service: sharing.service || "sharing", mode: sharing.mode, ids: sharing.ids,
      x: ll.map(p => e5(p[0])), y: ll.map(p => e5(p[1])), n0: sharing.n0,
      ev: sharing.ev.flat(),  // flat [t, station, kind, ...], in time order
    };
  }
  return payload;
}

const escapeHtml = s => String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#x27;" }[c]));

/** The page as a list of strings (the template with the data and texts filled in), for a Blob or a file. */
export function renderParts(template, payload, { title, eyebrow, note }) {
  let json;
  try {
    json = JSON.stringify(payload);
    if (json.includes("<")) json = json.replace(/</g, "\\u003c");  // no "</script>" inside the data
  } catch (e) {
    if (e instanceof RangeError) fail("the page would be too large for the browser. Choose a smaller share of agents.");
    throw e;
  }
  const fill = { __DATA__: json, __NOTE__: escapeHtml(note), __EYEBROW__: escapeHtml(eyebrow), __TITLE__: escapeHtml(title) };
  return template.split(/(__DATA__|__NOTE__|__EYEBROW__|__TITLE__)/).map(p => fill[p] ?? p);
}

// ---------------------------------------------------------------- the build
/**
 * Read the run and return the page data.
 * files: {events, network, schedule, config, sharing} (Blobs with names, or null).
 * entries: the whole folder ([{path, file}]), to tell which sharing service a station file belongs to.
 * opt: {sample, title, crs, folder, rail, walk, passengers, world}
 * Returns {payload, meta: {title, eyebrow, note}, stats}.
 */
export async function build(files, opt, env, entries = []) {
  const { sample = 0.1, rail = true, walk = true, passengers = true, world = false } = opt;
  if (!(sample > 0 && sample <= 1)) fail("sample must be between 0 and 1.");
  for (const [role, { name, required }] of Object.entries(INPUTS)) {
    if (!files[role] && required) fail(`missing ${name}(.gz). Choose the MATSim output folder, or choose the file.`);
  }
  const { events, network, schedule, config, sharing: sharingFile } = files;

  // Progress over all inputs read, weighted by file size on disk.
  const reading = [network, schedule, sharingFile, events].filter(Boolean);
  const total = reading.reduce((s, f) => s + f.size, 0) || 1;
  const done = new Map(reading.map(f => [f, 0]));
  const tracker = (step, file) => n => {
    done.set(file, n);
    let sum = 0;
    for (const v of done.values()) sum += v;
    env.progress({ step, fraction: Math.min(1, sum / total), reading: true });
  };
  const finished = file => done.set(file, file.size);

  const cfg = config ? await readConfig(config) : null;
  let crs = null;
  if (opt.crs) crs = await parseCrs(opt.crs, env);
  else if (cfg && "coordinateSystem" in cfg.params) {
    const value = cfg.params.coordinateSystem;
    try { crs = await parseCrs(value, env); } catch (e) {
      failCrs(`cannot interpret coordinateSystem '${value}' from ${config.name}. Enter a coordinate system, e.g. EPSG:2056.`);
    }
  }
  env.say("reading network ...");
  const net = await readNetwork(network, tracker("Reading the network", network));
  finished(network);
  if (!crs && net.crs) {
    try { crs = await parseCrs(net.crs, env); } catch {
      env.say(`note: cannot interpret the network's coordinate system '${net.crs}', ignoring it`);
    }
    if (crs) env.say(`note: coordinate system not set in output_config, using ${net.crs} from the network`);
  }
  crs ??= await guessSwissCrs([...net.linkMid.values()], env);

  let sched = { stops: new Map(), kinds: new Map(), vkinds: new Map() };
  if (schedule) {
    env.say("reading transit stops ...");
    sched = await readSchedule(schedule, tracker("Reading transit stops", schedule));
    finished(schedule);
  } else env.say("no transit schedule: pt riders follow the network links only");
  let sharing = null;
  if (sharingFile) {
    env.say("reading sharing stations ...");
    sharing = await readSharingStations(sharingFile, cfg, entries, net.linkMid, env, tracker("Reading sharing stations", sharingFile));
    finished(sharingFile);
  }
  env.say("reading events ...");
  const { legs, hops, nEvents } = await buildLegs(events, sample, net, sched.stops, sharing,
    rail ? sched.kinds : null, world ? sched.vkinds : null, tracker("Reading events", events), env.say);
  if (!legs.length) fail("no legs found for the sampled agents. Try a larger sample.");

  let tracks = new Map();
  if (hops.size) {
    env.say(`routing ${hops.size.toLocaleString("en")} train and tram hops on OpenStreetMap tracks ...`);
    tracks = await railTracks(hops, sched.stops, crs, env);
    env.say(`  ${tracks.size.toLocaleString("en")} of ${hops.size.toLocaleString("en")} hops follow the tracks, the rest go straight between stops`);
  }
  if (walk) {
    const nWalks = legs.filter(l => isWalk(l.mode)).length;
    if (nWalks) {
      env.say(`routing ${nWalks.toLocaleString("en")} walks on OpenStreetMap footpaths ...`);
      const paths = await walkTracks(legs, crs, env);
      for (const [k, v] of paths) tracks.set(k, v);
      env.say(`  ${paths.size.toLocaleString("en")} of ${nWalks.toLocaleString("en")} walks follow footpaths, the rest go straight`);
    }
  }
  if (passengers) {
    const nRides = legs.filter(l => isPassenger(l.mode)).length;
    if (nRides) {
      env.say(`routing ${nRides.toLocaleString("en")} car passenger legs on the road network ...`);
      const paths = passengerTracks(legs, net.roads, env);
      for (const [k, v] of paths) tracks.set(k, v);
      env.say(`  ${paths.size.toLocaleString("en")} of ${nRides.toLocaleString("en")} follow the roads, the rest go straight`);
    }
  }

  env.progress({ step: "Writing the page" });
  const payload = toPayload(legs, crs, sharing, tracks, world);
  const title = opt.title || scenarioName(opt.folder);
  const lastIt = (cfg?.params.lastIteration ?? "").trim();
  const eyebrow = `${title} · ` + (lastIt ? `iteration ${lastIt}` : "last iteration");
  const note = sample >= 1 ? "all agents" : `${Math.round(sample * 100)}% sample of agents`;

  // Counts only. No ids, coordinates or records.
  env.say(`done: ${nEvents.toLocaleString("en")} events read, ${legs.length.toLocaleString("en")} legs drawn across ${payload.modes.length} modes`);
  if (sharing) {
    const kinds = sharing.ev.map(e => e[2]), n = k => kinds.filter(x => x === k).length.toLocaleString("en");
    env.say(`sharing: ${sharing.ids.length.toLocaleString("en")} stations, ${n(0)} pickups, ${n(2)} failed pickups, ${n(3)} failed dropoffs`);
  }
  return { payload, meta: { title, eyebrow, note }, stats: { events: nEvents, legs: legs.length, modes: payload.modes.length } };
}
