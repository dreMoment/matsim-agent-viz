// Runs the build in the background, so the page stays responsive. The run's files are read here, on this
// computer; only OpenStreetMap tile bounds go out (to Overpass), and coordinate-system codes the tool does
// not know (to epsg.io).
import proj4 from "../vendor/proj4.mjs";
import { build, renderPage, BuildError } from "./engine.js";

// OpenStreetMap tiles (public data) stay in the browser's cache storage, so the next build needs no download.
const CACHE = "matsim-agent-viz-osm-v1";
const cacheUrl = (kind, key) => new URL(`osm-cache/${kind}/${key}.json`, self.location.href).href;
const cache = {
  async get(kind, key) {
    const r = await (await caches.open(CACHE)).match(cacheUrl(kind, key));
    return r ? r.text() : null;
  },
  async put(kind, key, text) {
    await (await caches.open(CACHE)).put(cacheUrl(kind, key), new Response(text, { headers: { "Content-Type": "application/json" } }));
  },
};

let lastProgress = 0;
const env = {
  proj4,
  fetch: (...a) => fetch(...a),
  cache: self.caches ? cache : null,
  say: text => postMessage({ log: text }),
  progress: p => {  // at most a few updates a second
    const now = performance.now();
    if (p.reading && now - lastProgress < 150) return;
    lastProgress = now;
    postMessage({ progress: p });
  },
};

onmessage = async ({ data: { files, entries, opt } }) => {
  try {
    const { payload, meta, stats } = await build(files, opt, env, entries);
    const template = await (await fetch(new URL(opt.world ? "../world.html" : "../page.html", import.meta.url))).text();
    postMessage({ done: { html: renderPage(template, payload, meta), title: meta.title, stats } });
  } catch (e) {
    postMessage({ error: e instanceof BuildError ? e.message : `${e.name || "Error"}: ${e.message || e}` });
    if (!(e instanceof BuildError)) console.error(e);
  }
};
