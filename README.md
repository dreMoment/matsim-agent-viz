# matsim-agent-viz

A minimal 3D animation of the last iteration of a MATSim run. Each agent on a leg is a dot moving over a tilted map with terrain and 3D buildings. Dots are black by default, or coloured by mode.

One Python script reads the run's final outputs and writes a single HTML page. The map, terrain and building tiles come from OpenFreeMap and Mapterhorn (no API key), so the page needs internet. The simulation data stays inside the file.

## Usage

Needs [uv](https://docs.astral.sh/uv/). It installs the only dependency, `pyproj`.

```sh
# build the page and open it in the browser
uv run matsim_agent_viz.py --run-dir path/to/simulation_output --open

# open an existing page again without rebuilding
uv run matsim_agent_viz.py --run-dir path/to/simulation_output --view
```

The page is written to `agents_3d.html` in the run folder (change this with `--out`). `--open` and `--view` serve it on `127.0.0.1` only. Opening the file directly fails in some viewers (Quick Look, editor previews) because they block WebGL.

| Option | Default | Meaning |
|---|---|---|
| `--run-dir` | required | MATSim output folder |
| `--out` | `<run-dir>/agents_3d.html` | HTML file to write |
| `--sample` | `0.1` | share of agents to show (stable hash of the person id) |
| `--title` | from the folder name | scenario name on the page |
| `--crs` | from `output_config.xml` | e.g. `EPSG:2056`, if the config doesn't set one (an LV95/LV03 guess is the fallback) |

A full Zurich 10% run (49 M events) takes about 2.5 minutes. With `--sample 0.1` the page is about 34 MB.

## What it reads

- `output_events.xml.gz`: movements of the final iteration
- `output_network.xml.gz`: link coordinates
- `output_transitSchedule.xml.gz`: stop coordinates
- `output_config.xml`: coordinate system and last iteration number

It never reads the population, facilities or households files.

- **Car and PT**: dots follow the vehicle's link entries and stop events. This includes the SBB deterministic transit engine.
- **Teleported modes** (walk, bike, car passenger, …): dots move in a straight line at constant speed, because MATSim doesn't simulate the path.

## In the page

- **Panel**: play/pause, a 24 h timeline, speed (1/4/15/30 simulated min per second) and the dot style (Black / By mode). Click a mode to hide it.
- **Keys**:
  - `Space` play/pause
  - `←` `→` ±5 min
  - `1`–`4` speed
  - `H` presentation mode (`Esc` also leaves it)
  - `L` street names
  - `?` help
- **Camera**: drag to pan; right-drag or Ctrl-drag to tilt and rotate.

## Test data

`examples/make_fake_run.py` writes a small synthetic run: a street grid around Zurich HB, a bus line and 3000 agents across nine modes. Use it to try the tool without real data:

```sh
uv run examples/make_fake_run.py /tmp/fake_run
uv run matsim_agent_viz.py --run-dir /tmp/fake_run --sample 1 --open
```

## Files

- `matsim_agent_viz.py`: reads the run and writes the page
- `page.html`: the page template. `__DATA__`, `__TITLE__`, `__EYEBROW__` and `__NOTE__` are filled in by the script.
