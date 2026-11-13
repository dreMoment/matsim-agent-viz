# /// script
# requires-python = ">=3.11"
# dependencies = ["pyproj", "scipy"]
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
  output_transitSchedule  stop coordinates and modes of public transport (optional)
  output_config.xml       coordinate system and iteration (optional)
  sharing service file    bike-sharing stations and bikes (optional), found
                          through the config's sharing module
It never reads the population, facilities or households files.

How positions are built:
  * car and pt: from the vehicle's link entries and stop arrivals/departures,
    so dots follow the network and wait at stops;
  * trains and trams: between two stops, along the railway tracks of
    OpenStreetMap (downloaded from Overpass once, cached in
    ~/.cache/matsim-agent-viz/rail), because their network links are often
    straight lines; --no-rail turns this off;
  * walks: along OpenStreetMap footpaths and streets (cached in
    ~/.cache/matsim-agent-viz/walk), keeping MATSim's start and end times;
    --no-walk turns this off;
  * other teleported modes (bike, PubliBike, car_passenger, ...): a straight
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
  --no-rail       trains and trams use the run's own links, no download
  --no-walk       walkers move in straight lines, no download
  --no-open       only write the page
"""

import argparse
import gzip
import heapq
import json
import math
import re
import sys
import time
import xml.etree.ElementTree as ET
import zlib
from collections import Counter
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
    files["sharing"] = sharing_for(run_dir, files["config"])
    return files


def sharing_for(run_dir: Path, config: Path | None) -> Path | None:
    """The station file a config points to, or None (also when the config is not readable XML)."""
    try:
        return locate_sharing(run_dir, config) if config is None or config.is_file() else None
    except SystemExit:  # the build will say what is wrong with the config
        return None


def read_sharing_services(config: Path | None) -> list[dict]:
    """The services in the config's sharing module (shared_mobility contrib), as {param: value}."""
    services = []
    if config is not None:
        for el in iter_elements(config, {"parameterset"}):
            params = {p.get("name"): (p.get("value") or "").strip() for p in el.findall("param")}
            if "serviceInputFile" in params:
                services.append(params)
    return services


def station_services(config: Path | None) -> list[dict]:
    """The station-based services in the config (free-floating ones have no stations to draw)."""
    return [s for s in read_sharing_services(config) if s.get("serviceScheme", "").lower() != "freefloating"]


def service_file(run_dir: Path, config: Path, svc: dict) -> Path | None:
    """Where a service's file is. The path is relative to the original config, which is not in the
    output folder, so it is looked up in the run folder, next to the config and in the folder above."""
    p = Path(svc.get("serviceInputFile") or "")
    if not p.name:
        return None
    cands = [p] if p.is_absolute() else [run_dir / p, config.parent / p, run_dir.parent / p]
    cands += [run_dir / p.name, run_dir.parent / p.name]
    return next((c for c in cands if c.is_file()), None)


def locate_sharing(run_dir: Path, config: Path | None) -> Path | None:
    """The file of the first station-based service in the config that can be found, or None."""
    for svc in station_services(config):
        if (path := service_file(run_dir, config, svc)) is not None:
            return path
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


def read_sharing_stations(path: Path, run_dir: Path, config: Path | None, link_mid):
    """Stations, their start fleet and the service from a shared_mobility service file, or None."""
    ids, xy, by_id, by_link = [], [], {}, {}
    fleet = Counter()  # bikes per start station; vehicles may be listed before the stations
    skipped = 0
    for el in iter_elements(path, {"station", "vehicle"}, "Reading sharing stations"):
        if el.tag == "station":
            pos = link_mid.get(el.get("link"))
            if pos is None:
                skipped += 1
                continue
            by_id[el.get("id")] = by_link[el.get("link")] = len(ids)
            ids.append(el.get("id")); xy.append(pos)
        elif el.get("startStation"):
            fleet[el.get("startStation")] += 1
    if not ids:
        say(f"note: {path.name} has no stations on this network (a free-floating service?), no station layer")
        return None
    if skipped:
        say(f"note: {skipped} sharing stations are on links missing from the network and are left out")
    # Which service this file belongs to. Events name it in their "service" attribute.
    services = read_sharing_services(config)
    stations = [s for s in services if s.get("serviceScheme", "").lower() != "freefloating"]
    match = [s for s in stations if (f := service_file(run_dir, config, s)) and f.resolve() == path.resolve()]
    if not match:
        same_name = [s for s in stations if Path(s.get("serviceInputFile") or "").name == path.name]
        match = same_name if len(same_name) == 1 else stations if len(stations) == 1 else []
    svc = match[0] if match else {}
    if svc and len(stations) > 1:
        say(f"note: {len(stations)} station-based sharing services in the config, showing '{svc.get('id')}'")
    if not svc and services:
        say(f"note: cannot tell which sharing service {path.name} belongs to; "
            "events of the services named in the config are left out")
    return {"ids": ids, "xy": xy, "n0": [fleet[i] for i in ids], "by_id": by_id, "by_link": by_link,
            "service": svc.get("id"), "mode": svc.get("mode"),
            # with no service identified, skip the events of every service the config names
            "skip": set() if svc else {s.get("id") for s in services}}


def read_schedule(path: Path):
    """Stop coordinates, and the track kind ('rail', 'tram') of each (line, route) that runs on tracks."""
    stops, kinds = {}, {}
    for el in iter_elements(path, {"stopFacility", "transitLine"}, "Reading transit stops"):
        if el.tag == "stopFacility":
            stops[el.get("id")] = (float(el.get("x")), float(el.get("y")))
        else:
            for route in el.iter("transitRoute"):
                kind = track_kind(route.findtext("transportMode"))
                if kind:
                    kinds[(el.get("id"), route.get("id"))] = kind
            el.clear()  # departures make up most of a schedule
    return stops, kinds


# ---------------------------------------------------------------- rail tracks
# Trains and trams often run on straight "artificial" links between stops (pt2matsim builds them so).
# To draw them on the real tracks, the script downloads the railway lines of OpenStreetMap around the
# stops that sampled riders used and routes each hop between two stops along them.
OVERPASS = "https://overpass-api.de/api/interpreter"
RAIL_CACHE = Path.home() / ".cache" / "matsim-agent-viz" / "rail"  # OSM tracks per 1-degree tile, public data
RAIL_TYPES = "rail|light_rail|narrow_gauge|subway|tram"
MAX_RAIL_TILES = 60
SNAP_M = 300            # a stop looks for tracks this close
WRONG_TRACK = 4.0       # cost factor for a train on tram tracks, or a tram on rail tracks
SIDING = 1.3            # cost factor for sidings: through tracks first
MAX_TURN_COS = -0.17    # no turns sharper than ~100 degrees: a train cannot reverse at a switch
SIMPLIFY_M = 3.0


def track_kind(transport_mode: str | None) -> str | None:
    """'rail' or 'tram' for transit modes that run on tracks, else None (bus, ferry, funicular, ...)."""
    m = (transport_mode or "").lower()
    if "tram" in m:
        return "tram"
    if re.search(r"rail|train|subway|metro|s-?bahn", m):
        return "rail"
    return None


def osm_tile(path: Path, query: str, what: str) -> dict | None:
    """Overpass result for `query`, cached gzipped at `path` (public OSM data, no run data). None if offline."""
    import urllib.parse, urllib.request
    if path.is_file():
        try:
            return json.loads(gzip.decompress(path.read_bytes()))
        except (OSError, ValueError):
            pass  # broken cache file: download again
    req = urllib.request.Request(OVERPASS, data=urllib.parse.urlencode({"data": query}).encode(),
                                 headers={"User-Agent": "matsim-agent-viz"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                raw = r.read()
            data = json.loads(raw)
            if "error" in data.get("remark", ""):  # timed out or out of memory: the data is incomplete
                raise RuntimeError(data["remark"][:200])
            break
        except Exception as e:
            if attempt == 2:
                say(f"note: could not download {what} ({type(e).__name__}: {e})")
                return None
            time.sleep(10 * (attempt + 1))  # Overpass is busy now and then
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(gzip.compress(raw))
    tmp.replace(path)
    return data


def rail_tile(lat: int, lon: int) -> dict | None:
    """OSM railway ways and their nodes in a 1x1 degree tile."""
    query = (f'[out:json][timeout:240];way["railway"~"^({RAIL_TYPES})$"]["service"!~"^(yard|spur)$"]'
             f'({lat},{lon},{lat + 1},{lon + 1})->.w;.w out body qt;node(w.w);out skel qt;')
    return osm_tile(RAIL_CACHE / f"{lat}_{lon}.json.gz", query, "rail tracks")


class RailGraph:
    """Tracks as a graph of OSM nodes in the run's coordinates, with a grid index of track segments."""
    CELL = 250.0

    def __init__(self, tiles: list[dict], crs: CRS):
        tf = Transformer.from_crs(4326, crs, always_xy=True)
        ids, lons, lats, ways = {}, [], [], {}
        for data in tiles:
            for el in data.get("elements", []):
                if el["type"] == "node" and el["id"] not in ids:
                    ids[el["id"]] = len(lons); lons.append(el["lon"]); lats.append(el["lat"])
                elif el["type"] == "way":
                    ways[el["id"]] = el
        self.x, self.y = (list(v) for v in tf.transform(lons, lats)) if lons else ([], [])
        # adjacency: node -> [(neighbour, length, track kind 0 rail / 1 tram / 2 both, cost factor)]
        self.adj = [[] for _ in lons]
        self.grid = {}
        for w in ways.values():
            tags = w.get("tags", {})
            kind = {"tram": 1, "light_rail": 2}.get(tags.get("railway"), 0)
            factor = SIDING if tags.get("service") == "siding" else 1.0
            refs = [ids[n] for n in w.get("nodes", []) if n in ids]
            for a, b in zip(refs, refs[1:]):
                if a == b:
                    continue
                d = math.dist((self.x[a], self.y[a]), (self.x[b], self.y[b]))
                self.adj[a].append((b, d, kind, factor)); self.adj[b].append((a, d, kind, factor))
                self._index(a, b, kind)

    def _index(self, a, b, kind):
        c = self.CELL
        x0, x1 = sorted((self.x[a], self.x[b])); y0, y1 = sorted((self.y[a], self.y[b]))
        for i in range(int(x0 // c), int(x1 // c) + 1):
            for j in range(int(y0 // c), int(y1 // c) + 1):
                self.grid.setdefault((i, j), []).append((a, b, kind))

    def snaps(self, x, y, kind):
        """Track segments near (x, y): [(a, b, px, py, offset)], the point on the segment closest to it."""
        c, r = self.CELL, int(SNAP_M // self.CELL) + 1
        ci, cj = int(x // c), int(y // c)
        out, seen = [], set()
        for i in range(ci - r, ci + r + 1):
            for j in range(cj - r, cj + r + 1):
                for a, b, k in self.grid.get((i, j), ()):
                    if (a, b) in seen or not compatible(k, kind):
                        continue
                    seen.add((a, b))
                    ax, ay, bx, by = self.x[a], self.y[a], self.x[b], self.y[b]
                    dx, dy = bx - ax, by - ay
                    f = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
                    px, py = ax + f * dx, ay + f * dy
                    d = math.hypot(x - px, y - py)
                    if d <= SNAP_M:
                        out.append((a, b, px, py, d))
        return out

    def route(self, start, end, kind):
        """Track polyline from stop `start` to stop `end` (both (x, y)), or None if there is no sensible one.

        A* over (node, previous node), so the path never turns back at a switch. Leaving a stop costs
        twice the distance to the track, so stops take the nearest suitable track."""
        src, dst = self.snaps(*start, kind), self.snaps(*end, kind)
        if not src or not dst:
            return None
        straight = math.dist(start, end)
        limit = 3 * straight + 3000
        ex, ey = end
        X, Y = self.x, self.y
        h = lambda n: math.hypot(X[n] - ex, Y[n] - ey)
        # where the path can leave the track to reach the end stop: node -> [(towards node, px, py, extra cost)]
        goal = {}
        for a, b, px, py, d in dst:
            goal.setdefault(a, []).append((b, px, py, math.hypot(X[a] - px, Y[a] - py) + 2 * d))
            goal.setdefault(b, []).append((a, px, py, math.hypot(X[b] - px, Y[b] - py) + 2 * d))
        best, parent, heap = {}, {}, []
        finals = []  # (cost, last state or None, start point if no state, end point)
        for a, b, px, py, d in src:
            for n, other in ((a, b), (b, a)):
                g = 2 * d + math.hypot(X[n] - px, Y[n] - py)
                state = (n, other)  # moving away from `other`, so the next step must not turn back to it
                if g < best.get(state, math.inf):
                    best[state] = g; parent[state] = ("start", px, py)
                    heapq.heappush(heap, (g + h(n), g, state))
        for a, b, px, py, d in src:  # both stops on the same segment: straight along it
            for a2, b2, qx, qy, d2 in dst:
                if {a, b} == {a2, b2}:
                    finals.append((2 * d + math.hypot(px - qx, py - qy) + 2 * d2, None, px, py, qx, qy))
        expanded = 0
        while heap:
            f, g, state = heapq.heappop(heap)
            if finals and f >= min(c[0] for c in finals):
                break
            if g > best.get(state, math.inf) or f > limit:
                continue
            expanded += 1
            if expanded > 300_000:
                break
            n, prev = state
            vx, vy = X[n] - X[prev], Y[n] - Y[prev]
            vl = math.hypot(vx, vy) or 1.0
            for towards, qx, qy, extra in goal.get(n, ()):
                tx, ty = X[towards] - X[n], Y[towards] - Y[n]
                if towards != prev and (vx * tx + vy * ty) / (vl * (math.hypot(tx, ty) or 1.0)) >= MAX_TURN_COS:
                    finals.append((g + extra, state, None, None, qx, qy))
            for m, d, k, factor in self.adj[n]:
                if m == prev:
                    continue
                wx, wy = X[m] - X[n], Y[m] - Y[n]
                wl = math.hypot(wx, wy) or 1.0
                if (vx * wx + vy * wy) / (vl * wl) < MAX_TURN_COS:
                    continue
                g2 = g + d * factor * (1.0 if compatible(k, kind) else WRONG_TRACK)
                s2 = (m, n)
                if g2 < best.get(s2, math.inf):
                    best[s2] = g2; parent[s2] = state
                    heapq.heappush(heap, (g2 + h(m), g2, s2))
        if not finals:
            return None
        cost, state, px, py, qx, qy = min(finals, key=lambda f: f[0])
        if cost > limit:
            return None
        pts = [(qx, qy)]
        while state is not None and len(pts) < 1_000_000:
            pts.append((X[state[0]], Y[state[0]]))
            p = parent[state]
            if p[0] == "start":
                px, py = p[1], p[2]
                state = None
            else:
                state = p
        pts.append((px, py))
        pts.reverse()
        return simplify(pts, SIMPLIFY_M)


def compatible(track: int, kind: str) -> bool:
    return track == 2 or (track == 1) == (kind == "tram")


def simplify(pts, tol):
    """Douglas-Peucker: drop points closer than `tol` metres to the line through their neighbours."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts); keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        (ax, ay), (bx, by) = pts[i], pts[j]
        dx, dy = bx - ax, by - ay
        ln = math.hypot(dx, dy)
        far, k = 0.0, None
        for m in range(i + 1, j):
            x, y = pts[m]
            d = abs(dx * (ay - y) - dy * (ax - x)) / ln if ln else math.hypot(x - ax, y - ay)
            if d > far:
                far, k = d, m
        if k is not None and far > tol:
            keep[k] = True
            stack += [(i, k), (k, j)]
    return [p for p, kp in zip(pts, keep) if kp]


def rail_tracks(hops: set, stops: dict, crs: CRS) -> dict:
    """Track polylines for the hops {(from stop, to stop, kind)}: {hop: [(x, y), ...]}, in run coordinates."""
    if crs.is_geographic:
        say("note: the run's coordinates are in degrees, not metres; trains move in straight lines")
        return {}
    to_wgs = Transformer.from_crs(crs, 4326, always_xy=True)
    tiles = set()
    for a, b, _ in hops:
        (ax, ay), (bx, by) = stops[a], stops[b]
        steps = max(1, int(math.dist((ax, ay), (bx, by)) // 20000))  # tiles along the hop, every 20 km
        for s in range(steps + 1):
            lon, lat = to_wgs.transform(ax + (bx - ax) * s / steps, ay + (by - ay) * s / steps)
            tiles.add((math.floor(lat), math.floor(lon)))
    if len(tiles) > MAX_RAIL_TILES:
        say(f"note: the trains cover {len(tiles)} map tiles of 1 degree, only the {MAX_RAIL_TILES} "
            "with most stops get tracks")
        count = Counter()
        for a, b, _ in hops:
            for s in (a, b):
                lon, lat = to_wgs.transform(*stops[s])
                count[(math.floor(lat), math.floor(lon))] += 1
        tiles = {t for t, _ in count.most_common(MAX_RAIL_TILES)}
    data, cached = [], 0
    for i, (lat, lon) in enumerate(sorted(tiles)):
        is_cached = (RAIL_CACHE / f"{lat}_{lon}.json.gz").is_file()
        cached += is_cached
        if not is_cached:
            say(f"  downloading rail tracks from OpenStreetMap, tile {i + 1} of {len(tiles)} ...")
        progress(f"Rail tracks: tile {i + 1} of {len(tiles)}")
        tile = rail_tile(lat, lon)
        if tile is None:
            say("note: trains and trams move in straight lines between stops where tracks are missing")
            break
        data.append(tile)
    if not data:
        return {}
    progress("Routing trains on the tracks")
    graph = RailGraph(data, crs)
    out = {}
    for hop in hops:
        a, b, kind = hop
        line = graph.route(stops[a], stops[b], kind)
        if line:
            out[hop] = line
    return out


# ---------------------------------------------------------------- footpaths
# MATSim teleports walkers: it knows only where and when a walk starts and ends. To draw walkers on the
# street, the script downloads the walkable ways of OpenStreetMap (footways, pedestrian zones, steps,
# paths and streets) in small tiles where sampled agents walk, and gives each walk its shortest path.
# The start and end times stay MATSim's; only the shape changes.
WALK_CACHE = Path.home() / ".cache" / "matsim-agent-viz" / "walk"  # OSM ways per WALK_TILE tile, public data
WALK_WAYS = ("footway|pedestrian|path|steps|living_street|residential|service|unclassified|track|cycleway|"
             "corridor|platform|tertiary|tertiary_link|secondary|secondary_link|primary|primary_link")
WALK_TILE = 0.05        # degrees: about 5.5 x 3.8 km in Switzerland
MAX_WALK_TILES = 40     # the tiles with most walks get paths, the other walks stay straight
WALK_SNAP_M = 250       # a walk's start or end looks for a path this close
WALK_STEP_M = 25        # long path segments get extra points this far apart, so start and end snap close
MAX_WALK_M = 5000       # longer walks stay straight
WALK_SIMPLIFY_M = 5.0


def is_walk(mode: str) -> bool:
    return "walk" in mode.lower()


def walk_tile(i: int, j: int) -> dict | None:
    """OSM ways a pedestrian may use, in the tile (i, j) of WALK_TILE degrees (south-west corner i, j)."""
    s, w = round(i * WALK_TILE, 4), round(j * WALK_TILE, 4)
    n, e = round(s + WALK_TILE, 4), round(w + WALK_TILE, 4)
    query = (f'[out:json][timeout:240];way["highway"~"^({WALK_WAYS})$"]["foot"!~"^(no|private)$"]'
             f'["access"!~"^(no|private)$"]({s},{w},{n},{e})->.w;.w out skel qt;node(w.w);out skel qt;')
    return osm_tile(WALK_CACHE / f"{i}_{j}.json.gz", query, "footpaths")


def walk_graph(tiles: list[dict], crs: CRS):
    """Walkable ways as a sparse graph in the run's coordinates. Returns (graph, x, y, k-d tree of nodes)."""
    import numpy as np
    from scipy.sparse import csr_matrix
    from scipy.spatial import cKDTree
    ids, lons, lats, ways = {}, [], [], {}
    for data in tiles:
        for el in data.get("elements", []):
            if el["type"] == "node":
                if el["id"] not in ids:
                    ids[el["id"]] = len(lons); lons.append(el["lon"]); lats.append(el["lat"])
            elif el["type"] == "way":
                ways[el["id"]] = el["nodes"]
    x, y = Transformer.from_crs(4326, crs, always_xy=True).transform(lons, lats)
    x, y = list(x), list(y)
    a, b = [], []
    for refs in ways.values():
        r = [ids[n] for n in refs if n in ids]
        for u, v in zip(r, r[1:]):
            d = math.hypot(x[v] - x[u], y[v] - y[u])
            steps = int(d // WALK_STEP_M)
            for s in range(1, steps + 1):  # extra points on long segments
                f = s / (steps + 1)
                x.append(x[u] + (x[v] - x[u]) * f); y.append(y[u] + (y[v] - y[u]) * f)
                a.append(u); b.append(len(x) - 1); u = len(x) - 1
            a.append(u); b.append(v)
    x, y, a, b = np.array(x), np.array(y), np.array(a, dtype=np.int64), np.array(b, dtype=np.int64)
    w = np.hypot(x[a] - x[b], y[a] - y[b]) + 1e-3  # zero weights would read as "no edge"
    n = len(x)
    graph = csr_matrix((np.r_[w, w], (np.r_[a, b], np.r_[b, a])), shape=(n, n))
    return graph, x, y, cKDTree(np.c_[x, y])


def walk_tracks(legs, crs: CRS) -> dict:
    """Give the sampled walks a path on OSM footpaths: adds a hop to each walk leg that gets one and
    returns {("walk", n): [(x, y), ...]}, in run coordinates."""
    import numpy as np
    from scipy.sparse.csgraph import dijkstra
    if crs.is_geographic:
        say("note: the run's coordinates are in degrees, not metres; walkers move in straight lines")
        return {}
    walks = [leg for leg in legs if is_walk(leg["mode"]) and not leg.get("routed") and len(leg["pts"]) == 2
             and 30 < math.dist(leg["pts"][0][1:], leg["pts"][1][1:]) < MAX_WALK_M]
    if not walks:
        return {}
    to_wgs = Transformer.from_crs(crs, 4326, always_xy=True)
    xs = [p[1] for leg in walks for p in leg["pts"]]
    ys = [p[2] for leg in walks for p in leg["pts"]]
    lons, lats = to_wgs.transform(xs, ys)
    need = []  # tiles each walk needs: those of its bounding box
    count = Counter()
    for k in range(len(walks)):
        i0, i1 = sorted((math.floor(lats[2 * k] / WALK_TILE), math.floor(lats[2 * k + 1] / WALK_TILE)))
        j0, j1 = sorted((math.floor(lons[2 * k] / WALK_TILE), math.floor(lons[2 * k + 1] / WALK_TILE)))
        t = {(i, j) for i in range(i0, i1 + 1) for j in range(j0, j1 + 1)}
        need.append(t)
        count.update(t)
    chosen = {t for t, _ in count.most_common(MAX_WALK_TILES)}
    if len(count) > MAX_WALK_TILES:
        say(f"note: the walks cover {len(count)} map tiles; the {MAX_WALK_TILES} with most walks get footpaths, "
            "walks elsewhere stay straight")
    tiles, loaded = [], set()
    for n, (i, j) in enumerate(sorted(chosen)):
        if not (WALK_CACHE / f"{i}_{j}.json.gz").is_file():
            say(f"  downloading footpaths from OpenStreetMap, tile {n + 1} of {len(chosen)} ...")
        progress(f"Footpaths: tile {n + 1} of {len(chosen)}")
        tile = walk_tile(i, j)
        if tile is None:
            say("note: walks where footpaths are missing stay straight")
            break
        tiles.append(tile); loaded.add((i, j))
    walks = [leg for leg, t in zip(walks, need) if t <= loaded]
    if not tiles or not walks:
        return {}
    progress("Routing walks on the footpaths")
    graph, X, Y, tree = walk_graph(tiles, crs)
    # cost in metres; give up on long detours (e.g. a start snapped to the wrong side of a river)
    return shortest_paths(walks, graph, X, Y, tree, WALK_SNAP_M, lambda beeline: 2.5 * beeline + 500,
                          WALK_SIMPLIFY_M, "walk", directed=False)


def shortest_paths(legs, graph, X, Y, tree, snap_m, limit, tol, name, directed):
    """Route two-point legs on a graph (scipy sparse, nodes at X, Y, k-d tree `tree` over the nodes it
    may start or end at). `limit(beeline)` is the highest cost a path may have. Adds a hop to each
    leg that gets a path and returns {(name, n): [(x, y), ...]}."""
    import numpy as np
    from scipy.sparse.csgraph import dijkstra
    ends = np.array([p[1:] for leg in legs for p in leg["pts"]])
    dist, node = tree.query(ends, distance_upper_bound=snap_m)
    ok = np.isfinite(dist).reshape(-1, 2).all(axis=1).tolist()
    ids = getattr(tree, "node_ids", None)  # tree over some of the nodes: map back to graph nodes
    if ids is not None:
        node = ids[np.minimum(node, len(ids) - 1)]
    snapped = node.reshape(-1, 2).tolist()
    # one search per start; on an undirected graph, per shared end (many walks start or end at one stop)
    uses = Counter(v for pair, good in zip(snapped, ok) if good for v in pair)
    groups = {}
    for k, ((u, v), good) in enumerate(zip(snapped, ok)):
        if good and u != v:
            src, dst, rev = (v, u, True) if not directed and uses[v] > uses[u] else (u, v, False)
            groups.setdefault(src, []).append((k, dst, rev))
    tracks = {}
    for src, group in groups.items():
        caps = [limit(math.dist(legs[k]["pts"][0][1:], legs[k]["pts"][1][1:])) for k, _, _ in group]
        d, pred = dijkstra(graph, directed=directed, indices=src, limit=max(caps), return_predecessors=True)
        for (k, dst, rev), cap in zip(group, caps):
            if not d[dst] <= cap:
                continue
            path = [dst]
            while path[-1] != src and len(path) < 1_000_000:
                path.append(int(pred[path[-1]]))
            if not rev:
                path.reverse()
            key = (name, len(tracks))
            tracks[key] = simplify([(float(X[n]), float(Y[n])) for n in path], tol)
            legs[k]["hops"] = [(0, 1, key)]
    return tracks


# ---------------------------------------------------------------- events
# shared_mobility contrib events -> kind in the page (0 pickup, 1 dropoff, 2 failed pickup, 3 failed dropoff)
SHARING_EVENTS = {"sharing vehicle pickup": 0, "sharing vehicle dropoff": 1,
                  "sharing vehicle pickup failed": 2, "sharing vehicle dropoff failed": 3}
# The contrib's pickup and dropoff activities. A failed event names the station the agent is sent to
# next, so the station that failed is the one of the activity the agent is at.
SHARING_ACTS = {"sharing pickup interaction", "sharing dropoff interaction"}


def build_legs(events: Path, sample: float, link_from, link_mid, stops, sharing=None, route_kinds=None):
    """Legs of the sampled agents. With `sharing` (stations), also collects every station event
    of that service into sharing["ev"], for all agents: the page shows only station totals.

    With `route_kinds` ((line, route) -> 'rail' / 'tram'), each ride of a train or tram between two
    stops is noted in leg["hops"] as (index of the departure point, index of the arrival point,
    (from stop, to stop, kind)), so it can be drawn along the tracks later."""
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
    at_station = {}    # person -> link of the sharing pickup/dropoff activity they are at (all persons)
    if sharing is not None:
        sharing["ev"] = []
    route_kinds = route_kinds or {}
    on_tracks = {}     # vehicle -> 'rail' / 'tram', for transit vehicles of routes that run on tracks
    departed = {}      # such vehicle -> (stop it last left, time)

    def add(person, t, xy, routed=False, keep=False):
        """Add a point to the person's open leg. Points closer in time than MIN_POINT_GAP_S replace
        the last one, unless that one is kept (a train's stop, which a track hop starts from)."""
        leg = open_leg.get(person)
        if leg is None or xy is None:
            return
        if routed:
            leg["routed"] = True
        pts = leg["pts"]
        if pts and t - pts[-1][0] < MIN_POINT_GAP_S and len(pts) > 1 and leg.get("kept") != len(pts) - 1:
            pts[-1] = (t, *xy)
        else:
            pts.append((t, *xy))
        if keep:
            leg["kept"] = len(pts) - 1

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
            stop = el.get("facility")
            kind = on_tracks.get(veh)
            if veh in riders:
                xy = stops.get(stop)
                prev = departed.get(veh) if kind and typ == "VehicleArrivesAtFacility" else None
                for p in riders[veh]:
                    leg = open_leg.get(p)
                    hop = None
                    if prev and xy is not None and leg and prev[0] != stop and leg.get("kept") is not None \
                            and leg["pts"][leg["kept"]][0] == prev[1]:  # on board since the last stop
                        hop = (leg["kept"], prev[0], stop, kind)
                    add(p, t, xy, routed=True, keep=kind is not None)
                    if hop and leg["pts"][-1][0] == t:
                        leg.setdefault("hops", []).append((hop[0], len(leg["pts"]) - 1, hop[1:]))
            if kind and typ == "VehicleDepartsAtFacility":
                departed[veh] = (stop, t)
        elif typ == "TransitDriverStarts":
            kind = route_kinds.get((el.get("transitLineId"), el.get("transitRouteId")))
            if kind:
                on_tracks[el.get("vehicleId")] = kind
                departed.pop(el.get("vehicleId"), None)
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
            if sharing is not None and el.get("actType") in SHARING_ACTS:
                at_station[person] = el.get("link")
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
            if sharing is None:
                continue
            svc = el.get("service")
            other = svc != sharing["service"] if sharing["service"] is not None else svc in sharing["skip"]
            if other:  # another service's event
                continue
            kind = SHARING_EVENTS[typ]
            here = at_station.pop(person, None)
            if kind >= 2:  # failed: the event names the next station to try, so use where the agent is
                s = sharing["by_link"].get(here)
            elif el.get("station"):
                s = sharing["by_id"].get(el.get("station"))
            else:
                s = sharing["by_link"].get(el.get("link"))
            if s is not None:
                sharing["ev"].append((round(t), s, kind))

    for person in list(pending):
        finish(person)
    if n_events == 0:
        fail(f"{events.name} contains no events.")
    return legs, n_events


# ---------------------------------------------------------------- output
def with_tracks(leg, track_idx):
    """The leg's points without those inside hops that have a track, and [point index, track, ...]:
    the page inserts each track after that point. Hops are (first point, last point, track key)."""
    pts, refs, i = [], [], 0
    for k_dep, k_arr, key in leg.get("hops", ()):
        t = track_idx.get(key)
        if t is None or k_dep < i:
            continue
        pts += leg["pts"][i:k_dep + 1]
        refs += [len(pts) - 1, t]
        i = k_arr
    return pts + leg["pts"][i:], refs


def deltas(v):
    """First value, then the steps between values: shorter numbers in the JSON."""
    return v[:1] + [b - a for a, b in zip(v, v[1:])]


def to_payload(legs, crs: CRS, sharing=None, tracks=None):
    tf = Transformer.from_crs(crs, 4326, always_xy=True)
    modes = sorted({leg["mode"] for leg in legs})
    mode_idx = {m: i for i, m in enumerate(modes)}
    tracks = tracks or {}
    track_idx = {hop: i for i, hop in enumerate(tracks)}
    out = []
    starts_lon, starts_lat = [], []
    for leg in legs:
        pts, refs = with_tracks(leg, track_idx)
        ts, xs, ys = zip(*pts)
        lons, lats = tf.transform(xs, ys)
        lon_i = [round(v * 1e5) for v in lons]
        lat_i = [round(v * 1e5) for v in lats]
        routed = 1 if leg.get("routed") else 0  # moved through network links or transit stops, not teleported
        out.append([mode_idx[leg["mode"]], [round(v) for v in ts], lon_i, lat_i, routed] + ([refs] if refs else []))
        starts_lon.append(lons[0]); starts_lat.append(lats[0])
    out.sort(key=lambda r: r[1][0])
    # initial view: close in on the median leg start, where 3D buildings show
    starts_lon.sort(); starts_lat.sort()
    centre = [starts_lon[len(starts_lon) // 2], starts_lat[len(starts_lat) // 2]]
    payload = {"modes": modes, "legs": out, "centre": centre}
    if tracks:  # train, tram and walk paths, each stored once: [[lon, Δlon, ...], [lat, Δlat, ...]]
        payload["tracks"] = []
        for line in tracks.values():
            lons, lats = tf.transform(*zip(*line))
            payload["tracks"].append([deltas([round(v * 1e5) for v in lons]), deltas([round(v * 1e5) for v in lats])])
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
          title: str | None = None, crs: str | None = None, rail: bool = True,
          walk: bool = True) -> Path:
    """Read the run and write the page. `files` maps INPUTS roles to paths (None = not there).
    With `rail`, trains and trams follow OpenStreetMap tracks between stops; with `walk`, walkers follow
    OpenStreetMap footpaths (both downloaded once, then cached)."""
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
        stops, route_kinds = read_schedule(schedule)
    else:
        say("no transit schedule: pt riders follow the network links only")
        stops, route_kinds = {}, {}
    sharing = None
    if sharing_file is not None:
        say("reading sharing stations ...")
        sharing = read_sharing_stations(sharing_file, run_dir, config, link_mid)
    say("reading events (this can take a few minutes) ...")
    legs, n_events = build_legs(events, sample, link_from, link_mid, stops, sharing,
                                route_kinds if rail else None)
    if not legs:
        fail("no legs found for the sampled agents. Try a larger sample.")
    hops = {h[2] for leg in legs for h in leg.get("hops", ())}
    tracks = {}
    if hops:
        say(f"routing {len(hops):,} train and tram hops on OpenStreetMap tracks ...")
        tracks = rail_tracks(hops, stops, crs_obj)
        say(f"  {len(tracks):,} of {len(hops):,} hops follow the tracks, the rest go straight between stops")
    if walk:
        n_walks = sum(is_walk(leg["mode"]) for leg in legs)
        if n_walks:
            say(f"routing {n_walks:,} walks on OpenStreetMap footpaths ...")
            paths = walk_tracks(legs, crs_obj)
            tracks.update(paths)
            say(f"  {len(paths):,} of {n_walks:,} walks follow footpaths, the rest go straight")

    progress("Writing the page")
    payload = to_payload(legs, crs_obj, sharing, tracks)
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
            f"{kinds.count(2):,} failed pickups, {kinds.count(3):,} failed dropoffs")
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
    ap.add_argument("--no-rail", action="store_true",
                    help="don't route trains and trams along OpenStreetMap tracks (no download)")
    ap.add_argument("--no-walk", action="store_true",
                    help="walkers move in straight lines, no footpath download")
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
    if args.config and not args.sharing:  # the sharing file comes from whichever config is used
        files["sharing"] = sharing_for(run_dir, args.config)
    build(run_dir, files, out, args.sample, args.title, args.crs, rail=not args.no_rail, walk=not args.no_walk)
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
            if url.path == "/api/crs":  # what a config sets: coordinate system and sharing station file
                q = parse_qs(url.query)
                cfg = Path(q.get("config", [""])[0]).expanduser()
                sharing = sharing_for(Path(q.get("dir", [""])[0]).expanduser(), cfg)
                return self.reply({"crs": config_crs(cfg), "sharing": str(sharing) if sharing else None})
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
