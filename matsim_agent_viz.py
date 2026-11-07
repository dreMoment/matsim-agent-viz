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
  output_transitSchedule  stop coordinates for public transport
  output_config.xml       coordinate system (EPSG:2056 if not found)
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
  uv run matsim_agent_viz.py --run-dir path/to/simulation_output --open
Watch an existing page again without rebuilding:
  uv run matsim_agent_viz.py --run-dir path/to/simulation_output --view
The page is served on 127.0.0.1 only. Opening the HTML file directly may not
work in some viewers (T3, Quick Look), so use --open or --view.
Options:
  --open          open the page in your browser after building it
  --view          open the existing page, no rebuild
  --crs EPSG:xxxx coordinate system, if output_config does not set one
  --run-dir DIR   simulation output folder (required)
  --out FILE      HTML to write (default: agents_3d.html in the run folder)
  --sample 0.1    share of agents to show (0-1)
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


def find(run_dir: Path, name: str) -> Path:
    """Return <name>.gz or <name> in run_dir (also accepts a run-id prefix)."""
    for cand in (run_dir / f"{name}.gz", run_dir / name):
        if cand.is_file():
            return cand
    prefixed = sorted(run_dir.glob(f"*.{name}.gz")) + sorted(run_dir.glob(f"*.{name}"))
    if prefixed:
        return prefixed[0]
    fail(f"missing {name}(.gz) in {run_dir}. Pass --run-dir pointing to the MATSim output folder.")


def open_xml(path: Path):
    if path.suffix == ".gz":
        with path.open("rb") as f:
            if f.read(2) != b"\x1f\x8b":
                fail(f"{path} ends in .gz but is not gzip. Rename it or re-copy the file.")
        return gzip.open(path, "rb")
    return path.open("rb")


def iter_elements(path: Path, tags: set[str]):
    """Stream elements with the given tags, freeing memory as it goes."""
    with open_xml(path) as f:
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
        except ET.ParseError as e:
            fail(f"{path} is not valid XML ({e}). Is the file complete?")


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
        print("note: coordinate system not set in output_config, network matches EPSG:2056 (LV95)")
        return CRS.from_epsg(2056)
    if share(480_000, 840_000, 70_000, 300_000) > 0.9:
        print("note: coordinate system not set in output_config, network matches EPSG:21781 (LV03)")
        return CRS.from_epsg(21781)
    fail("coordinate system not set in output_config and the network is not in a Swiss grid. "
         "Set global.coordinateSystem in the config, or add --crs EPSG:xxxx.")


def read_network(path: Path):
    nodes, links = {}, {}
    for el in iter_elements(path, {"node", "link"}):
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


def read_stops(path: Path):
    stops = {}
    for el in iter_elements(path, {"stopFacility"}):
        stops[el.get("id")] = (float(el.get("x")), float(el.get("y")))
    return stops


# ---------------------------------------------------------------- events
def build_legs(events: Path, sample: float, link_from, link_mid, stops):
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

    for el in iter_elements(events, {"event"}):
        n_events += 1
        if n_events % 5_000_000 == 0:
            print(f"  {n_events / 1e6:.0f} M events read ({time.time() - started:.0f} s)")
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

    for person in list(pending):
        finish(person)
    if n_events == 0:
        fail(f"{events.name} contains no events.")
    return legs, n_events


# ---------------------------------------------------------------- output
def to_payload(legs, crs: CRS):
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
    return {"modes": modes, "legs": out, "centre": centre}


PAGE = Path(__file__).with_name("page.html")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", type=Path, required=True, help="MATSim output folder (holds output_events.xml.gz)")
    ap.add_argument("--out", type=Path, help="HTML file to write (default: agents_3d.html in the run folder)")
    ap.add_argument("--sample", type=float, default=0.1, help="share of agents to show, 0-1 (default 0.1)")
    ap.add_argument("--title", help="scenario name shown on the page (default: from the run folder, e.g. 'Zurich 10pct')")
    ap.add_argument("--crs", help="coordinate system of the run, e.g. EPSG:2056 (default: from output_config)")
    ap.add_argument("--view", action="store_true", help="open the existing page in your browser, no rebuild")
    ap.add_argument("--open", action="store_true", help="open the page in your browser after building it")
    args = ap.parse_args()

    args.out = args.out or args.run_dir / "agents_3d.html"
    if args.view:
        page = args.out
        if not page.is_file():
            fail(f"{args.out} not found. Build it first by running the script without --view.")
        serve(page)
        return

    run_dir = args.run_dir
    if not run_dir.is_dir():
        fail(f"run folder {run_dir} not found.")
    if not 0 < args.sample <= 1:
        fail("--sample must be between 0 and 1.")

    events = find(run_dir, "output_events.xml")
    network = find(run_dir, "output_network.xml")
    schedule = find(run_dir, "output_transitSchedule.xml")
    try:
        config = find(run_dir, "output_config.xml")
    except SystemExit:
        config = None

    crs = CRS.from_user_input(args.crs) if args.crs else read_crs(config)
    print(f"reading network ...")
    link_from, link_mid = read_network(network)
    if crs is None:
        crs = guess_swiss_crs(link_mid.values())
    print(f"reading transit stops ...")
    stops = read_stops(schedule)
    print(f"reading events (this can take a few minutes) ...")
    legs, n_events = build_legs(events, args.sample, link_from, link_mid, stops)
    if not legs:
        fail("no legs found for the sampled agents. Try a larger --sample.")

    payload = to_payload(legs, crs)
    title = args.title or scenario_name(run_dir)
    last_it = read_param(config, "lastIteration")
    eyebrow = f"{title} · " + (f"iteration {last_it}" if last_it else "last iteration")
    note = "all agents" if args.sample >= 1 else f"{args.sample:.0%} sample of agents"
    html = (PAGE.read_text(encoding="utf-8").replace("__DATA__", json.dumps(payload, separators=(",", ":")))
            .replace("__NOTE__", escape(note)).replace("__EYEBROW__", escape(eyebrow))
            .replace("__TITLE__", escape(title)))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html, encoding="utf-8")

    # Counts only. No ids, coordinates or records.
    size_mb = args.out.stat().st_size / 1e6
    print(f"done: {n_events:,} events read, {len(legs):,} legs drawn across {len(payload['modes'])} modes")
    print(f"wrote {args.out} ({size_mb:.1f} MB).")
    if size_mb > 60:
        print("note: the page is large and may load slowly. Try --sample 0.05.")
    print("To watch it later without rebuilding, add --view.")
    if args.open:
        serve(args.out)


def serve(page: Path):
    """Serve the page on this Mac only (127.0.0.1) and open it in the default browser."""
    import functools, http.server, webbrowser
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(page.parent.resolve()))
    handler.log_message = lambda *a, **k: None  # keep the console quiet
    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler) as httpd:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/{page.name}"
        print(f"serving {url} (only reachable from this Mac). Press Ctrl-C to stop.", flush=True)
        webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("stopped.")


if __name__ == "__main__":
    main()
