# Agent instructions

A single-script tool. `matsim_agent_viz.py` reads a MATSim run and fills `page.html` (MapLibre, no build step), or with `--world` `world.html` (a three.js world to explore, no build step). See README.md for usage.

## Data

Real MATSim runs hold synthetic-population data that may be protected. Before touching a real run, check the rules of the project it belongs to. For example, abmt-project's AGENTS.md forbids cloud models from reading population, facility, household and Mikrozensus files.

- Develop and test against `examples/make_fake_run.py`, never against real runs.
- Keep the script away from population, facilities and households files. It needs only events, network, transit schedule and config.
- Console output stays at counts: no person ids, coordinates or records.
- Never commit run data or generated pages (`*.html` other than `page.html`; see .gitignore).

## Checking UI changes

`page.html` needs WebGL, so headless screenshots are unreliable. To check a change:

1. Build the fake run.
2. Serve the page on 127.0.0.1.
3. Look at it in a real browser at desktop size. Phone sizes are out of scope.

For `world.html`, build the fake run with `--osm` so the streets match the map. The page exposes `window.world` (`follow(i)`, `at(x, z)`, `setTime(s)`, `stats`) for checks from the console.
