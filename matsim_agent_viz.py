# /// script
# requires-python = ">=3.11"
# dependencies = ["pyproj"]
# ///
"""3D map animation of the last iteration: agents as dots, by mode.

Reads the final outputs of a MATSim run and writes one HTML page. Open it in
a browser to watch a sample of agents move over a 3D map of the area, with
terrain and extruded buildings. Dots are black, or coloured by mode with a
toggle. The page loads the map, terrain and building tiles from OpenFreeMap
and Mapterhorn, so it needs internet. Your data stays in the file.

What it reads (plain or .gz):
  output_events.xml       movements of the final iteration
  output_network.xml      node coordinates for link events
  output_transitSchedule  stop coordinates for public transport (optional)
  output_config.xml       coordinate system and iteration (optional)
  sharing service file    bike-sharing stations and bikes (optional), found
                          through the config's sharing module
It never reads the population, facilities or households files.

How positions are built:
  * car and pt: from the vehicle's link entries and stop arrivals/departures,
    so dots follow the network and wait at stops;
  * teleported modes (walk, bike, PubliBike, car_passenger, ...): a straight
    line from start to end, at constant speed, because MATSim does not
    simulate their movement;
  * agents are sampled with a stable hash of the person id (--sample).

Privacy: the HTML contains sampled agents' trajectories. By default it is
written into the run folder, next to the data it came from. The console
shows only counts.

Usage:
  uv run matsim_agent_viz.py
      opens a launcher in your browser: choose the run folder, check the
      files it found, pick a sample and build. The page opens when done.
  uv run matsim_agent_viz.py path/to/simulation_output
      builds the page from the command line and opens it.
  uv run matsim_agent_viz.py path/to/simulation_output --view
      opens the existing page again, no rebuild.
Pages are served on 127.0.0.1 only. Opening the HTML file directly may not
work in some viewers (T3, Quick Look), because they block WebGL.
Options:
  --sample 0.1    share of agents to show (0-1)
  --title TEXT    scenario name on the page
  --crs EPSG:xxxx coordinate system, if output_config does not set one
  --out FILE      HTML to write (default: agents_3d.html in the run folder)
  --events/--network/--schedule/--config/--sharing FILE
                  use this file instead of the one in the run folder
  --no-open       only write the page
"""

import argparse
import gzip
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
import zlib
from html import escape
from pathlib import Path

from pyproj import CRS, Transformer

MIN_POINT_GAP_S = 10  # thin out link events closer together than this


# ---------------------------------------------------------------- input checks
def fail(msg):
    sys.exit(f"ERROR: {msg}")


_log = print  # the launcher swaps this to collect progress lines
_progress = None  # the launcher sets this: called as (step, file, bytes of that file read so far)


def say(msg):
    _log(msg)


def progress(step: str, path: Path | None = None, pos: int | None = None):
    if _progress is not None:
        _progress(step, path, pos)


# role -> (file name in the run folder, required, what it is used for)
INPUTS = {
    "events": ("output_events.xml", True, "movements of the final iteration"),
    "network": ("output_network.xml", True, "link coordinates"),
    "schedule": ("output_transitSchedule.xml", False, "stop coordinates for public transport"),
    "config": ("output_config.xml", False, "coordinate system and iteration number"),
    "sharing": ("sharing service file", False, "bike-sharing stations, from the config's sharing module"),
}


def locate(run_dir: Path, name: str) -> Path | None:
    """Return <name>.gz or <name> in run_dir (also accepts a run-id prefix), or None."""
    for cand in (run_dir / f"{name}.gz", run_dir / name):
        if cand.is_file():
            return cand
    prefixed = sorted(run_dir.glob(f"*.{name}.gz")) + sorted(run_dir.glob(f"*.{name}"))
    return prefixed[0] if prefixed else None


def find_inputs(run_dir: Path) -> dict[str, Path | None]:
    files = {role: locate(run_dir, name) for role, (name, _, _) in INPUTS.items() if role != "sharing"}
    try:
        files["sharing"] = locate_sharing(run_dir, files["config"])
    except SystemExit:  # config is not readable XML: the build will say so
        files["sharing"] = None
    return files


def read_sharing_services(config: Path | None) -> list[dict]:
    """The services in the config's sharing module (shared_mobility contrib), as {param: value}."""
    services = []
    if config is not None:
        for el in iter_elements(config, {"parameterset"}):
            params = {p.get("name"): (p.get("value") or "").strip() for p in el.findall("param")}
            if "serviceInputFile" in params:
                services.append(params)
    return services


def locate_sharing(run_dir: Path, config: Path | None) -> Path | None:
    """The station-based service file a config points to. The path is relative to the original
    config, so it is looked up next to the run folder, the config and the folder above."""
    for svc in read_sharing_services(config):
        value = svc.get("serviceInputFile")
        if not value or svc.get("serviceScheme", "").lower() == "freefloating":
            continue
        p = Path(value)
        cands = [p] if p.is_absolute() else [run_dir / p, config.parent / p, run_dir.parent / p]
        cands += [run_dir / p.name, run_dir.parent / p.name]
        for c in cands:
            if c.is_file():
                return c
    return None


def iter_elements(path: Path, tags: set[str], step: str | None = None):
    """Stream elements with the given tags, freeing memory as it goes.

    With a step name, reports how far into the file (on disk, so compressed bytes for .gz) it has read.
    """
    with path.open("rb") as raw:
        f = raw
        if path.suffix == ".gz":
            if raw.read(2) != b"\x1f\x8b":
                fail(f"{path} ends in .gz but is not gzip. Rename it or re-copy the file.")
            raw.seek(0)
            f = gzip.GzipFile(fileobj=raw, mode="rb")
        if step:
            progress(step, path, 0)
        try:
            ctx = ET.iterparse(f, events=("start", "end"))
            _, root = next(ctx)
            n = 0
            for ev, el in ctx:
                if ev == "end" and el.tag in tags:
                    yield el
                    n += 1
                    if n % 50000 == 0:
                        root.clear()
                        if step:
                            progress(step, path, raw.tell())
        except ET.ParseError as e:
            fail(f"{path} is not valid XML ({e}). Is the file complete?")
        if step:
            progress(step, path, path.stat().st_size)


# ---------------------------------------------------------------- static inputs
UNSET_CRS = {"", "atlantis", "none"}  # "Atlantis" is MATSim's default for "not set"


def read_crs(config: Path | None) -> CRS | None:
    """CRS from output_config, or None if it is not set."""
    if config is not None:
        for el in iter_elements(config, {"param"}):
            if el.get("name") == "coordinateSystem":
                value = (el.get("value") or "").strip()
                if value.lower() in UNSET_CRS:
                    return None
                try:
                    return CRS.from_user_input(value)
                except Exception:
                    fail(f"cannot interpret coordinateSystem '{value}' from {config.name}.")
    return None


def read_param(config: Path | None, name: str) -> str | None:
    """A single <param name=...> value from output_config, or None."""
    if config is not None:
        for el in iter_elements(config, {"param"}):
            if el.get("name") == name:
                return (el.get("value") or "").strip() or None
    return None


def scenario_name(run_dir: Path) -> str:
    """'Zurich_10pct/simulation_output' -> 'Zurich 10%'."""
    parts = [p for p in run_dir.resolve().parts if p.lower() not in ("simulation_output", "output", "")]
    if not parts:
        return "MATSim run"
    return re.sub(r"(\d+)\s*pct\b", r"\1%", parts[-1].replace("_", " "), flags=re.I)


def guess_swiss_crs(points) -> CRS:
    """Pick LV95 or LV03 from the coordinate range. Prints only the choice."""
    pts = list(points)
    def share(xmin, xmax, ymin, ymax):
        return sum(xmin <= x <= xmax and ymin <= y <= ymax for x, y in pts) / len(pts)
    if share(2_480_000, 2_840_000, 1_070_000, 1_300_000) > 0.9:
        say("note: coordinate system not set in output_config, network matches EPSG:2056 (LV95)")
        return CRS.from_epsg(2056)
    if share(480_000, 840_000, 70_000, 300_000) > 0.9:
        say("note: coordinate system not set in output_config, network matches EPSG:21781 (LV03)")
        return CRS.from_epsg(21781)
    fail("coordinate system not set in output_config and the network is not in a Swiss grid. "
         "Set global.coordinateSystem in the config, or add --crs EPSG:xxxx.")


def read_network(path: Path):
    nodes, links = {}, {}
    for el in iter_elements(path, {"node", "link"}, "Reading the network"):
        if el.tag == "node":
            nodes[el.get("id")] = (float(el.get("x")), float(el.get("y")))
        else:
            links[el.get("id")] = (el.get("from"), el.get("to"))
    if not nodes or not links:
        fail(f"{path.name} has no nodes or links. Is it a MATSim network?")
    link_from = {l: nodes[a] for l, (a, b) in links.items() if a in nodes}
    link_mid = {l: ((nodes[a][0] + nodes[b][0]) / 2, (nodes[a][1] + nodes[b][1]) / 2)
                for l, (a, b) in links.items() if a in nodes and b in nodes}
    return link_from, link_mid


def read_sharing_stations(path: Path, config: Path | None, link_mid):
    """Stations, their start fleet and the service id from a shared_mobility service file, or None."""
    ids, xy, n0, by_id, by_link = [], [], [], {}, {}
    skipped = 0
    for el in iter_elements(path, {"station", "vehicle"}, "Reading sharing stations"):
        if el.tag == "station":
            pos = link_mid.get(el.get("link"))
            if pos is None:
                skipped += 1
                continue
            by_id[el.get("id")] = by_link[el.get("link")] = len(ids)
            ids.append(el.get("id")); xy.append(pos); n0.append(0)
        elif (s := by_id.get(el.get("startStation"))) is not None:
            n0[s] += 1
    if not ids:
        say(f"note: {path.name} has no stations on this network, no station layer")
        return None
    if skipped:
        say(f"note: {skipped} sharing stations are on links missing from the network and are left out")
    # Which service in the config this file belongs to: events name it in their "service" attribute.
    services = read_sharing_services(config)
    match = [s for s in services if Path(s["serviceInputFile"]).name == path.name] or \
            (services if len(services) == 1 else [])
    svc = match[0] if match else {}
    return {"ids": ids, "xy": xy, "n0": n0, "by_id": by_id, "by_link": by_link,
            "service": svc.get("id"), "mode": svc.get("mode") or svc.get("id")}


def read_stops(path: Path):
    stops = {}
    for el in iter_elements(path, {"stopFacility"}, "Reading transit stops"):
        stops[el.get("id")] = (float(el.get("x")), float(el.get("y")))
    return stops


# ---------------------------------------------------------------- events
# shared_mobility contrib events -> kind in the page (0 pickup, 1 dropoff, 2 failed pickup, 3 failed dropoff)
SHARING_EVENTS = {"sharing vehicle pickup": 0, "sharing vehicle dropoff": 1,
                  "sharing vehicle pickup failed": 2, "sharing vehicle dropoff failed": 3}


def build_legs(events: Path, sample: float, link_from, link_mid, stops, sharing=None):
    """Legs of the sampled agents. With `sharing` (stations), also collects every station event
    of that service into sharing["ev"], for all agents: the page shows only station totals."""
    threshold = int(sample * 10000)

    def sampled(person):
        return person is not None and not person.startswith("pt_") and \
            zlib.crc32(person.encode()) % 10000 < threshold

    pos = {}           # person -> last known (x, y)
    open_leg = {}      # person -> {"mode", "pts": [(t, x, y)]}
    pending = {}       # person -> leg that arrived; end point may be refined by actstart
    riders = {}        # vehicle -> set of sampled persons on board
    legs = []
    n_events = 0
    started = time.time()
    if sharing is not None:
        sharing["ev"] = []

    def add(person, t, xy, routed=False):
        leg = open_leg.get(person)
        if leg is None or xy is None:
            return
        if routed:
            leg["routed"] = True
        pts = leg["pts"]
        if pts and t - pts[-1][0] < MIN_POINT_GAP_S and len(pts) > 1:
            pts[-1] = (t, *xy)
        else:
            pts.append((t, *xy))

    def finish(person):
        leg = pending.pop(person, None)
        if leg and len(leg["pts"]) >= 2 and leg["pts"][-1][0] > leg["pts"][0][0]:
            legs.append(leg)

    def act_xy(el):
        x, y = el.get("x"), el.get("y")
        if x is not None and y is not None:
            return (float(x), float(y))
        return link_mid.get(el.get("link"))

    for el in iter_elements(events, {"event"}, "Reading events"):
        n_events += 1
        if n_events % 5_000_000 == 0:
            say(f"  {n_events / 1e6:.0f} M events read ({time.time() - started:.0f} s)")
        typ = el.get("type")
        t = float(el.get("time"))
        person = el.get("person")

        if typ == "actend":
            if sampled(person):
                finish(person)
                pos[person] = act_xy(el)
        elif typ == "departure":
            if sampled(person):
                finish(person)
                start = pos.get(person) or link_mid.get(el.get("link"))
                if start is not None:
                    open_leg[person] = {"mode": el.get("legMode") or "unknown", "pts": [(t, *start)]}
        elif typ == "PersonEntersVehicle":
            if sampled(person) and person in open_leg:
                riders.setdefault(el.get("vehicle"), set()).add(person)
                last = open_leg[person]["pts"][-1]
                add(person, t, (last[1], last[2]))  # waited here until boarding
        elif typ == "PersonLeavesVehicle":
            veh = el.get("vehicle")
            if veh in riders:
                riders[veh].discard(person)
                if not riders[veh]:
                    del riders[veh]
        elif typ == "entered link":
            veh = el.get("vehicle")
            if veh in riders:
                xy = link_from.get(el.get("link"))
                for p in riders[veh]:
                    add(p, t, xy, routed=True)
        elif typ in ("VehicleArrivesAtFacility", "VehicleDepartsAtFacility"):
            veh = el.get("vehicle")
            if veh in riders:
                xy = stops.get(el.get("facility"))
                for p in riders[veh]:
                    add(p, t, xy, routed=True)
        elif typ == "arrival":
            if sampled(person) and person in open_leg:
                leg = open_leg.pop(person)
                end = link_mid.get(el.get("link"))
                if end is not None:
                    leg["pts"].append((t, *end))
                pending[person] = leg
                if end is not None:
                    pos[person] = end
        elif typ == "actstart":
            if sampled(person):
                xy = act_xy(el)
                leg = pending.get(person)
                if leg and xy is not None:
                    tl, _, _ = leg["pts"][-1]
                    leg["pts"][-1] = (tl, *xy)
                finish(person)
                if xy is not None:
                    pos[person] = xy
        elif typ in SHARING_EVENTS:
            if sharing is not None and (sharing["service"] is None or el.get("service") == sharing["service"]):
                station = el.get("station")
                s = sharing["by_id"].get(station) if station else sharing["by_link"].get(el.get("link"))
                if s is not None:
                    sharing["ev"].append((round(t), s, SHARING_EVENTS[typ]))

    for person in list(pending):
        finish(person)
    if n_events == 0:
        fail(f"{events.name} contains no events.")
    return legs, n_events


# ---------------------------------------------------------------- output
def to_payload(legs, crs: CRS, sharing=None):
    tf = Transformer.from_crs(crs, 4326, always_xy=True)
    modes = sorted({leg["mode"] for leg in legs})
    mode_idx = {m: i for i, m in enumerate(modes)}
    out = []
    starts_lon, starts_lat = [], []
    for leg in legs:
        ts, xs, ys = zip(*leg["pts"])
        lons, lats = tf.transform(xs, ys)
        lon_i = [round(v * 1e5) for v in lons]
        lat_i = [round(v * 1e5) for v in lats]
        routed = 1 if leg.get("routed") else 0  # moved through network links or transit stops, not teleported
        out.append([mode_idx[leg["mode"]], [round(v) for v in ts], lon_i, lat_i, routed])
        starts_lon.append(lons[0]); starts_lat.append(lats[0])
    out.sort(key=lambda r: r[1][0])
    # initial view: close in on the median leg start, where 3D buildings show
    starts_lon.sort(); starts_lat.sort()
    centre = [starts_lon[len(starts_lon) // 2], starts_lat[len(starts_lat) // 2]]
    payload = {"modes": modes, "legs": out, "centre": centre}
    if sharing is not None:
        xs, ys = zip(*sharing["xy"])
        lons, lats = tf.transform(xs, ys)
        payload["sharing"] = {
            "service": sharing["service"] or "sharing", "mode": sharing["mode"], "ids": sharing["ids"],
            "x": [round(v * 1e5) for v in lons], "y": [round(v * 1e5) for v in lats], "n0": sharing["n0"],
            "ev": [v for e in sharing["ev"] for v in e],  # flat [t, station, kind, ...], in time order
        }
    return payload


PAGE = Path(__file__).with_name("page.html")
LAUNCHER = Path(__file__).with_name("launcher.html")
DEFAULT_OUT = "agents_3d.html"
LAUNCHER_PORT = 8765
# The launcher's last choices (folder, swapped files, sample, title, CRS). Paths and settings only, no run data.
SETTINGS = Path.home() / ".config" / "matsim-agent-viz" / "launcher.json"


def build(run_dir: Path, files: dict[str, Path | None], out: Path, sample: float = 0.1,
          title: str | None = None, crs: str | None = None) -> Path:
    """Read the run and write the page. `files` maps INPUTS roles to paths (None = not there)."""
    if not 0 < sample <= 1:
        fail("sample must be between 0 and 1.")
    for role, (name, required, _) in INPUTS.items():
        path = files.get(role)
        if path is None and required:
            fail(f"missing {name}(.gz) in {run_dir}. Point to the MATSim output folder, or choose the file.")
        if path is not None and not path.is_file():
            fail(f"{path} not found.")
    events, network, schedule, config, sharing_file = (files.get(role) for role in INPUTS)

    if crs:
        try:
            crs_obj = CRS.from_user_input(crs)
        except Exception:
            fail(f"cannot interpret coordinate system '{crs}'. Use e.g. EPSG:2056.")
    else:
        crs_obj = read_crs(config)
    say("reading network ...")
    link_from, link_mid = read_network(network)
    if crs_obj is None:
        crs_obj = guess_swiss_crs(link_mid.values())
    if schedule is not None:
        say("reading transit stops ...")
        stops = read_stops(schedule)
    else:
        say("no transit schedule: pt riders follow the network links only")
        stops = {}
    sharing = None
    if sharing_file is not None:
        say("reading sharing stations ...")
        sharing = read_sharing_stations(sharing_file, config, link_mid)
    say("reading events (this can take a few minutes) ...")
    legs, n_events = build_legs(events, sample, link_from, link_mid, stops, sharing)
    if not legs:
        fail("no legs found for the sampled agents. Try a larger sample.")

    progress("Writing the page")
    payload = to_payload(legs, crs_obj, sharing)
    title = title or scenario_name(run_dir)
    last_it = read_param(config, "lastIteration")
    eyebrow = f"{title} · " + (f"iteration {last_it}" if last_it else "last iteration")
    note = "all agents" if sample >= 1 else f"{sample:.0%} sample of agents"
    html = (PAGE.read_text(encoding="utf-8").replace("__DATA__", json.dumps(payload, separators=(",", ":")))
            .replace("__NOTE__", escape(note)).replace("__EYEBROW__", escape(eyebrow))
            .replace("__TITLE__", escape(title)))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    # Counts only. No ids, coordinates or records.
    size_mb = out.stat().st_size / 1e6
    say(f"done: {n_events:,} events read, {len(legs):,} legs drawn across {len(payload['modes'])} modes")
    if sharing is not None:
        kinds = [k for _, _, k in sharing["ev"]]
        say(f"sharing: {len(sharing['ids']):,} stations, {kinds.count(0):,} pickups, "
            f"{kinds.count(2):,} failed pickups")
    say(f"wrote {out} ({size_mb:.1f} MB).")
    if size_mb > 60:
        say("note: the page is large and may load slowly. Try a sample of 5%.")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", nargs="?", type=Path,
                    help="MATSim output folder. Leave it out to open the launcher in your browser.")
    ap.add_argument("--run-dir", dest="run_dir_opt", type=Path, help=argparse.SUPPRESS)
    ap.add_argument("--out", type=Path, help=f"HTML file to write (default: {DEFAULT_OUT} in the run folder)")
    ap.add_argument("--sample", type=float, default=0.1, help="share of agents to show, 0-1 (default 0.1)")
    ap.add_argument("--title", help="scenario name shown on the page (default: from the run folder, e.g. 'Zurich 10pct')")
    ap.add_argument("--crs", help="coordinate system of the run, e.g. EPSG:2056 (default: from output_config)")
    for role, (name, _, use) in INPUTS.items():
        ap.add_argument(f"--{role}", type=Path, help=f"use this file instead of the one found in the run folder ({use})")
    ap.add_argument("--view", action="store_true", help="open the existing page in your browser, no rebuild")
    ap.add_argument("--no-open", action="store_true", help="only write the page, don't open it")
    ap.add_argument("--open", action="store_true", help=argparse.SUPPRESS)  # old flag: opening is the default now
    args = ap.parse_args()

    run_dir = args.run_dir or args.run_dir_opt
    if run_dir is None:
        serve()
        return
    out = args.out or run_dir / DEFAULT_OUT
    if args.view:
        if not out.is_file():
            fail(f"{out} not found. Build it first by running the script without --view.")
        serve(out)
        return
    if not run_dir.is_dir():
        fail(f"run folder {run_dir} not found.")
    files = find_inputs(run_dir)
    files.update({role: getattr(args, role) for role in INPUTS if getattr(args, role)})
    build(run_dir, files, out, args.sample, args.title, args.crs)
    if args.no_open:
        print("To watch it later without rebuilding, add --view.")
    else:
        serve(out)


# ---------------------------------------------------------------- launcher
def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data: dict):
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(SETTINGS)


def config_crs(config: Path | None) -> str | None:
    """The coordinate system a config file sets, as written there, or None."""
    if config is None or not config.is_file():
        return None
    try:
        crs = read_param(config, "coordinateSystem")
    except SystemExit:  # not readable XML: the build will say so
        return None
    return None if (crs or "").lower() in UNSET_CRS else crs


def check_dir(d: Path) -> dict:
    """What the launcher shows for a folder: which inputs were found, and an existing page."""
    if not d.is_dir():
        return {"ok": False}
    files = find_inputs(d)
    out = d / DEFAULT_OUT
    return {
        "ok": True, "dir": str(d), "title": scenario_name(d), "out": str(out),
        "crs": config_crs(files["config"]),
        "files": {role: {"name": name, "required": req, "use": use,
                         "path": str(files[role]) if files[role] else None,
                         "size": files[role].stat().st_size if files[role] else None}
                  for role, (name, req, use) in INPUTS.items()},
        "existing": {"size": out.stat().st_size, "mtime": out.stat().st_mtime} if out.is_file() else None,
    }


def pick(kind: str, start: str | None) -> dict:
    """Open the native macOS folder/file dialog and return the chosen path."""
    import subprocess
    if sys.platform != "darwin":
        return {"error": "The file dialog only works on macOS. Type the path instead."}
    what = "folder" if kind == "folder" else "file"
    prompt = "Choose the MATSim output folder" if what == "folder" else "Choose the file"
    start = start if start and Path(start).is_dir() else None
    lines = ["on run argv", "activate",
             f'set p to choose {what} with prompt "{prompt}"'
             + (" default location (POSIX file (item 1 of argv) as alias)" if start else ""),
             "return POSIX path of p", "end run"]
    cmd = ["osascript"] + [a for line in lines for a in ("-e", line)] + ([start] if start else [])
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        return {"cancelled": True}
    path = res.stdout.strip()
    return {"path": path.rstrip("/") or "/"}


def serve(page: Path | None = None):
    """Serve on this Mac only (127.0.0.1) and open the browser.

    With a page: show it. Without: show the launcher, which picks a run, builds and opens the page.
    """
    import http.server, shutil, threading, webbrowser
    from urllib.parse import parse_qs, unquote, urlparse

    state = {"page": page, "running": False, "log": [], "error": None, "progress": None}
    settings_lock = threading.Lock()

    def track(files: dict):
        """Progress over all inputs read in a build, weighted by file size on disk, with a time-left guess."""
        sizes = {p: p.stat().st_size for role, p in files.items() if p and role != "config" and p.is_file()}
        total = sum(sizes.values()) or 1
        done = dict.fromkeys(sizes, 0)
        started = time.time()

        def on_progress(step, path, pos):
            if path in done:
                done[path] = pos
            fraction = min(1.0, sum(done.values()) / total)
            elapsed = time.time() - started
            # Too early to guess: the first seconds include start-up and small files.
            left = elapsed * (1 - fraction) / fraction if fraction > 0.03 and elapsed > 3 and path else None
            state["progress"] = {"step": step, "fraction": fraction, "left": left,
                                 "reading": path is not None}
        return on_progress

    def run_build(req: dict):
        global _log, _progress
        _log = lambda m: (print(m, flush=True), state["log"].append(str(m)))
        try:
            run_dir = Path(req["dir"]).expanduser()
            files = {role: Path(p).expanduser() if p else None for role, p in req.get("files", {}).items()}
            _progress = track(files)
            out = run_dir / DEFAULT_OUT
            state["page"] = build(run_dir, files, out, float(req.get("sample", 0.1)),
                                  req.get("title") or None, req.get("crs") or None)
        except SystemExit as e:
            state["error"] = str(e.code).removeprefix("ERROR: ")
        except Exception as e:  # show it in the launcher instead of a dead thread
            state["error"] = f"{type(e).__name__}: {e}"
        finally:
            _log, _progress = print, None
            state["running"] = False

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep the console quiet
            pass

        def reply(self, body, code=200, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def trusted(self):
            # Only our own pages: blocks other websites (DNS rebinding, form posts) from driving this server.
            return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def do_GET(self):
            if not self.trusted():
                return self.reply({"error": "forbidden"}, 403)
            url = urlparse(self.path)
            if url.path == "/":
                return self.reply(LAUNCHER.read_bytes(), ctype="text/html; charset=utf-8")
            if url.path == "/api/check":
                return self.reply(check_dir(Path(parse_qs(url.query).get("dir", [""])[0]).expanduser()))
            if url.path == "/api/settings":
                return self.reply(load_settings())
            if url.path == "/api/crs":
                return self.reply({"crs": config_crs(Path(parse_qs(url.query).get("config", [""])[0]).expanduser())})
            if url.path == "/api/status":
                p = state["page"]
                done = p and not state["running"] and p.is_file()
                return self.reply({"running": state["running"], "log": state["log"], "error": state["error"],
                                   "progress": state["progress"],
                                   "view": f"/view/{p.name}" if done else None,
                                   "size": p.stat().st_size if done else None})
            p = state["page"]
            if p and unquote(url.path) == f"/view/{p.name}" and p.is_file():
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(p.stat().st_size))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with p.open("rb") as f:
                    shutil.copyfileobj(f, self.wfile)
                return
            self.reply({"error": "not found"}, 404)

        def do_POST(self):
            if not self.trusted() or self.headers.get("Content-Type") != "application/json":
                return self.reply({"error": "forbidden"}, 403)
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            path = urlparse(self.path).path
            if path == "/api/settings":
                if isinstance(req, dict):
                    with settings_lock:
                        save_settings(req)
                return self.reply({"ok": True})
            if path == "/api/pick":
                return self.reply(pick(req.get("kind", "folder"), req.get("start")))
            if path == "/api/build":
                if state["running"]:
                    return self.reply({"error": "a build is already running"}, 409)
                state.update(running=True, log=[], error=None, page=None, progress=None)
                threading.Thread(target=run_build, args=(req,), daemon=True).start()
                return self.reply({"ok": True})
            if path == "/api/open":
                p = Path(req.get("path", "")).expanduser()
                if p.suffix != ".html" or not p.is_file():
                    return self.reply({"error": f"{p} not found"}, 404)
                state["page"] = p
                return self.reply({"view": f"/view/{p.name}", "size": p.stat().st_size})
            if path == "/api/quit":
                self.reply({"ok": True})
                threading.Thread(target=httpd.shutdown, daemon=True).start()
                return
            self.reply({"error": "not found"}, 404)

    try:  # a fixed, familiar URL when it is free; any free port otherwise
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", LAUNCHER_PORT), Handler)
    except OSError:
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    with httpd:
        port = httpd.server_address[1]
        url = f"http://127.0.0.1:{port}/" + (f"view/{page.name}" if page else "")
        what = "page" if page else "launcher"
        print(f"{what} at {url} (only reachable from this Mac). Press Ctrl-C to stop.", flush=True)
        webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        print("stopped.")


if __name__ == "__main__":
    main()
