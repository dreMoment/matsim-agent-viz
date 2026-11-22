# Agent instructions

A static website with no build step. `index.html` lets the user choose a MATSim run folder; `js/engine.js` reads the run in the browser (in `js/worker.js`) and fills `page.html` (MapLibre) or `world.html` (a three.js world to explore). `tools/build.mjs` runs the same engine in Node. See README.md for usage.

## Data

Real MATSim runs hold synthetic-population data that may be protected. Before touching a real run, check the rules of the project it belongs to. For example, abmt-project's AGENTS.md forbids cloud models from reading population, facility, household and Mikrozensus files.

- Develop and test against `examples/make_fake_run.py`, never against real runs.
- Keep the engine away from population, facilities and households files. It needs only events, network, transit schedule, config and the bike-sharing station file.
- Log output stays at counts: no person ids, coordinates or records.
- The run must never leave the user's browser. Don't add a backend, analytics or any request that carries run data. Every host the pages may reach is listed in the Content-Security-Policy at the top of `index.html`, `page.html` and `world.html`; keep the three in step, and add a host only for public data (tiles, libraries, OpenStreetMap).
- Never commit run data or generated pages (`*.html` other than `index.html`, `page.html` and `world.html`; see .gitignore).
- `osm/` (ready-made OpenStreetMap tiles the site publishes) holds only what `tools/osm-tiles.mjs` downloads for the boxes in `tools/osm-regions.json`. Never derive its regions or tiles from a run. It is built in CI, not committed.
- `demo/zurich.json` (the start page's background) must stay fake: make it only from `examples/make_fake_run.py --osm` (see README.md, Files).

## Checking changes

To check the engine, build the fake run with `node tools/build.mjs FAKE_RUN --sample 1` (add `--world` for the 3D world).

The pages need WebGL, so headless screenshots are unreliable. To check a UI change:

1. Build the fake run.
2. Serve the repo on 127.0.0.1 (`python3 -m http.server 8000 --bind 127.0.0.1`) and open http://127.0.0.1:8000.
3. Drop the fake run's folder on the page, build, and look at it in a real browser at desktop size. Phone sizes are out of scope.

For `world.html`, build the fake run with `--osm` so the streets match the map. The page exposes `window.world` (`follow(i)`, `at(x, z)`, `setTime(s)`, `stats`) for checks from the console.
