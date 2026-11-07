# matsim-agent-viz

A minimal 3D animation of the last iteration of a MATSim run. Each agent on a leg is a dot moving over a tilted map with terrain and 3D buildings. Dots are black by default, or coloured by mode.

One Python script reads the run's final outputs and writes a single HTML page. The map, terrain and building tiles come from OpenFreeMap and Mapterhorn (no API key), so the page needs internet. The simulation data stays inside the file.

## Usage

Needs [uv](https://docs.astral.sh/uv/). It installs the only dependency, `pyproj`.

```sh
uv run matsim_agent_viz.py
```

This opens a launcher in your browser. Click **Choose…** to pick the run's output folder in the Finder dialog, or paste its path. The launcher shows which input files it found, and you can swap any of them. Pick the share of agents, then **Build and open**. When the page is ready it opens in the same tab. **Open last page** shows a page you built before, with no rebuild. The launcher remembers your last folder, file swaps, sample, title and coordinate system in `~/.config/matsim-agent-viz/launcher.json` (paths and settings only, no run data).

From the command line:

```sh
# build the page and open it
uv run matsim_agent_viz.py path/to/simulation_output

# open the existing page again, no rebuild
uv run matsim_agent_viz.py path/to/simulation_output --view
```

The page is written to `agents_3d.html` in the run folder (change this with `--out`). Everything is served on `127.0.0.1` only (port 8765 if it is free). Opening the HTML file directly fails in some viewers (Quick Look, editor previews) because they block WebGL. Stop the server with Ctrl-C or the launcher's **Quit**.

| Option | Default | Meaning |
|---|---|---|
| `run_dir` | none: opens the launcher | MATSim output folder |
| `--out` | `<run-dir>/agents_3d.html` | HTML file to write |
| `--sample` | `0.1` | share of agents to show (stable hash of the person id) |
| `--title` | from the folder name | scenario name on the page |
| `--crs` | from `output_config.xml` | e.g. `EPSG:2056`, if the config doesn't set one (an LV95/LV03 guess is the fallback) |
| `--events`, `--network`, `--schedule`, `--config` | found in the run folder | use a file from somewhere else |
| `--view` | | open the existing page, no rebuild |
| `--no-open` | | only write the page |

A full Zurich 10% run (49 M events) takes about 2.5 minutes. With `--sample 0.1` the page is about 34 MB.

## What it reads

Plain or `.gz`, with or without a run-id prefix (`myrun.output_events.xml.gz`):

- `output_events.xml.gz` (required): movements of the final iteration
- `output_network.xml.gz` (required): link coordinates
- `output_transitSchedule.xml.gz` (optional): stop coordinates. Without it, PT riders follow the network links only.
- `output_config.xml` (optional): coordinate system and last iteration number. Without it, the coordinate system is guessed from the network (Swiss grids) or taken from `--crs`.

It never reads the population, facilities or households files.

- **Car and PT**: dots follow the vehicle's link entries and stop events. This includes the SBB deterministic transit engine.
- **Teleported modes** (walk, bike, car passenger, …): dots move in a straight line at constant speed, because MATSim doesn't simulate the path.

## In the page

- **Panel**: play/pause, a 24 h timeline, speed (1/4/15/30 simulated min per second), the dot style (Black / By mode) and trip lines (Lines / Off). Click a mode to hide it.
- **Trip lines**: each leg in progress that is routed through the network (car, PT and other network modes) shows its whole path from start to end as a faint line, with the part already travelled drawn stronger. Teleported legs (walk, bike, …) get no line.
- **Keys**:
  - `Space` play/pause
  - `←` `→` ±5 min
  - `1`–`4` speed
  - `H` presentation mode (`Esc` also leaves it)
  - `L` street names
  - `T` trip lines
  - `?` help
- **Camera**: drag to pan; right-drag or Ctrl-drag to tilt and rotate.

## Test data

`examples/make_fake_run.py` writes a small synthetic run: a street grid around Zurich HB, a bus line and 3000 agents across nine modes. Use it to try the tool without real data:

```sh
uv run examples/make_fake_run.py /tmp/fake_run
uv run matsim_agent_viz.py /tmp/fake_run --sample 1
```

## Files

- `matsim_agent_viz.py`: reads the run and writes the page
- `launcher.html`: the launcher page (folder and file picker, build progress)
- `page.html`: the page template. `__DATA__`, `__TITLE__`, `__EYEBROW__` and `__NOTE__` are filled in by the script.
