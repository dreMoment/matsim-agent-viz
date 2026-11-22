"""Write a tiny fake MATSim output folder (events, network, schedule, config, bike-sharing stations).

    uv run examples/make_fake_run.py OUT_DIR          # street grid around Zurich HB, works offline
    uv run examples/make_fake_run.py OUT_DIR --osm    # real Zurich streets from OpenStreetMap (for demos)

Public transport: a bus line on the roads, and an S-Bahn and a tram line at real Zurich stops that
run on straight links between their stops, as pt2matsim builds them for rail."""
import gzip, heapq, json, math, random, sys, urllib.parse, urllib.request
from pathlib import Path

args = [a for a in sys.argv[1:] if not a.startswith("--")]
OSM = "--osm" in sys.argv
if len(args) != 1: sys.exit(__doc__)
out = Path(args[0]); out.mkdir(parents=True, exist_ok=True)
rnd = random.Random(3)

# lines on tracks: (line, transportMode, stops (id, x, y), seconds between stops, headway); real stop positions in LV95
TRACK_LINES = [
    ("S5", "rail", [("hardbruecke", 2681456, 1248818), ("hb", 2683126, 1248057), ("stadelhofen", 2683825, 1246784),
                    ("tiefenbrunnen", 2684830, 1244969)], 180, 900),
    ("S9", "rail", [("hb", 2683126, 1248057), ("oerlikon", 2683435, 1251775)], 360, 900),
    ("T6", "tram", [("paradeplatz", 2683110, 1247130), ("rennweg", 2683051, 1247503), ("bahnhofstrasse", 2683135, 1247833),
                    ("central", 2683460, 1247900), ("haldenegg", 2683525, 1248225)], 90, 450),
]
TRACK_LINES = [(l, m, [(f"{l}_{sid}", x, y) for sid, x, y in st], hop, hw) for l, m, st, hop, hw in TRACK_LINES]

# ---------- road graph: node id -> (x, y) in LV95, and undirected edges (both directions become links)
def grid_roads():
    X0, Y0, N, D = 2680000, 1245000, 25, 300  # 25x25 grid, 300 m spacing, around Zurich HB
    nodes = {f"n{i}_{j}": (X0 + i*D, Y0 + j*D) for i in range(N) for j in range(N)}
    edges = [(f"n{i}_{j}", f"n{i+di}_{j+dj}") for i in range(N) for j in range(N) for di, dj in ((1, 0), (0, 1))
             if i + di < N and j + dj < N]
    return nodes, edges

def lv95(lat, lon):
    """WGS84 to LV95, swisstopo's approximate formulas (about 1 m)."""
    p, l = (lat * 3600 - 169028.66) / 10000, (lon * 3600 - 26782.5) / 10000
    e = 2600072.37 + 211455.93*l - 10938.51*l*p - 0.36*l*p*p - 44.54*l**3
    n = 1200147.07 + 308807.95*p + 3745.25*l*l + 76.63*p*p - 194.56*l*l*p + 119.79*p**3
    return round(e, 1), round(n, 1)

def osm_roads():
    """Drivable streets of central Zurich from Overpass, cached (public OSM data)."""
    cache = Path.home() / ".cache" / "matsim-agent-viz" / "demo_roads.json"
    if cache.is_file():
        data = json.loads(cache.read_text())
    else:
        print("downloading Zurich streets from OpenStreetMap ...")
        q = ('[out:json][timeout:120];way["highway"~"^(trunk|primary|secondary|tertiary|unclassified|residential)$"]'
             '(47.355,8.505,47.400,8.570);(._;>;);out;')
        req = urllib.request.Request("https://overpass-api.de/api/interpreter", headers={"User-Agent": "matsim-agent-viz"},
                                     data=urllib.parse.urlencode({"data": q}).encode())
        with urllib.request.urlopen(req, timeout=300) as r: data = json.load(r)
        cache.parent.mkdir(parents=True, exist_ok=True); cache.write_text(json.dumps(data))
    pts = {e["id"]: lv95(e["lat"], e["lon"]) for e in data["elements"] if e["type"] == "node"}
    edges = []
    for w in (e for e in data["elements"] if e["type"] == "way"):
        if w.get("tags", {}).get("tunnel", "no") != "no": continue  # tunnels would cut across the map
        ns = [n for n in w["nodes"] if n in pts]
        edges += [(f"o{a}", f"o{b}") for a, b in zip(ns, ns[1:]) if a != b]
    nodes = {f"o{k}": v for k, v in pts.items()}
    # keep the largest connected part, so every trip finds a route
    adj = {}
    for a, b in edges: adj.setdefault(a, []).append(b); adj.setdefault(b, []).append(a)
    seen, best = set(), set()
    for s in adj:
        if s in seen: continue
        comp, stack = {s}, [s]
        while stack:
            for v in adj[stack.pop()]:
                if v not in comp: comp.add(v); stack.append(v)
        seen |= comp
        if len(comp) > len(best): best = comp
    return {k: nodes[k] for k in best}, [(a, b) for a, b in edges if a in best]

NODES, EDGES = osm_roads() if OSM else grid_roads()
def dist(a, b): (ax, ay), (bx, by) = NODES[a], NODES[b]; return math.hypot(ax - bx, ay - by)
LINK, ADJ = {}, {}  # (from, to) -> link id; node -> [(neighbour, length)]
for a, b in EDGES:
    for u, v in ((a, b), (b, a)):
        if (u, v) not in LINK:
            LINK[(u, v)] = f"{u}-{v}"; ADJ.setdefault(u, []).append((v, max(1.0, dist(u, v))))
PLACES = sorted(ADJ)

def route(a, b):
    """Shortest road route from node a to node b (A*), as link ids."""
    prev, best, todo = {a: None}, {a: 0.0}, [(dist(a, b), 0.0, a)]
    while todo:
        _, g, u = heapq.heappop(todo)
        if u == b: break
        if g > best[u]: continue
        for v, w in ADJ[u]:
            if g + w < best.get(v, math.inf):
                best[v] = g + w; prev[v] = u; heapq.heappush(todo, (g + w + dist(v, b), g + w, v))
    nodes = [b]
    while prev[nodes[-1]] is not None: nodes.append(prev[nodes[-1]])
    nodes.reverse()
    return [LINK[(u, v)] for u, v in zip(nodes, nodes[1:])]
def link_len(lid): u, v = lid.split("-"); return dist(u, v)
def link_at(n): return LINK[(n, ADJ[n][0][0])]
def xy(n): x, y = NODES[n]; return dict(x=str(x), y=str(y))
def nearest(x, y, among=None): return min(among or PLACES, key=lambda n: math.hypot(NODES[n][0] - x, NODES[n][1] - y))

# bus line: west to east through the middle of the area, a stop about every 600 m
xs = [x for x, _ in NODES.values()]; ys = [y for _, y in NODES.values()]
CX, CY = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
W, H = max(xs) - min(xs), max(ys) - min(ys)
bus_route = route(nearest(CX - 0.4*W, CY), nearest(CX + 0.4*W, CY))
STOPS, run_len = [bus_route[0].split("-")[0]], 0.0
for lid in bus_route:
    run_len += link_len(lid)
    if run_len >= 600: STOPS.append(lid.split("-")[1]); run_len = 0.0
with gzip.open(out / "output_network.xml.gz", "wt") as f:
    f.write('<?xml version="1.0"?>\n<network>\n<nodes>\n')
    for n, (x, y) in NODES.items():
        f.write(f'<node id="{n}" x="{x}" y="{y}"/>\n')
    for _, _, stops, *_ in TRACK_LINES:
        for sid, x, y in stops:
            f.write(f'<node id="pt_{sid}" x="{x}" y="{y}"/>\n')
    f.write('</nodes>\n<links>\n')
    for (u, v), lid in LINK.items():
        f.write(f'<link id="{lid}" from="{u}" to="{v}" length="{dist(u, v):.1f}" freespeed="13.9" capacity="1000" permlanes="1" modes="car,pt"/>\n')
    # S-Bahn and tram: straight links from stop to stop, both ways (see TRACK_LINES)
    for line, mode, stops, *_ in TRACK_LINES:
        for (a, *_), (b, *_) in zip(stops, stops[1:]):
            for u, v in ((a, b), (b, a)):
                f.write(f'<link id="pt_{u}_{v}" from="pt_{u}" to="pt_{v}" length="1000" freespeed="20" capacity="9999" permlanes="1" modes="{mode}"/>\n')
    f.write('</links>\n</network>\n')

with gzip.open(out / "output_transitSchedule.xml.gz", "wt") as f:
    f.write('<?xml version="1.0"?>\n<transitSchedule>\n<transitStops>\n')
    for k, n in enumerate(STOPS):
        x, y = NODES[n]
        f.write(f'<stopFacility id="s{k}" x="{x}" y="{y}" linkRefId="{link_at(n)}"/>\n')
    for line, mode, stops, *_ in TRACK_LINES:
        for k, (sid, x, y) in enumerate(stops):
            f.write(f'<stopFacility id="{sid}" x="{x}" y="{y}" linkRefId="pt_{sid}_{stops[k+1 if k+1 < len(stops) else k-1][0]}"/>\n')
    f.write('</transitStops>\n')
    f.write('<transitLine id="bus"><transitRoute id="bus_e"><transportMode>bus</transportMode></transitRoute></transitLine>\n')
    for line, mode, *_ in TRACK_LINES:
        f.write(f'<transitLine id="{line}">' + "".join(f'<transitRoute id="{line}_{d}"><transportMode>{mode}</transportMode></transitRoute>' for d in (0, 1)) + '</transitLine>\n')
    f.write('</transitSchedule>\n')

# station-based bike sharing (shared_mobility contrib): a few stations with few bikes, so some run empty
BIKE_STATIONS = [nearest(CX + fx*W, CY + fy*H) for fx, fy in
                 ((-.36, -.36), (-.36, 0), (-.36, .36), (0, -.36), (0, .36), (.36, -.36), (.36, 0), (.36, .36), (-.16, -.16), (.16, .16))]
BIKES_PER_STATION = 3
(out / "publibike_service.xml").write_text('<?xml version="1.0" encoding="utf-8"?>\n<service>\n<stations>\n'
    + "".join(f'<station id="st{k}" link="{link_at(n)}" capacity="100"/>\n' for k, n in enumerate(BIKE_STATIONS))
    + '</stations>\n<vehicles>\n'
    + "".join(f'<vehicle id="b{k}_{b}" startStation="st{k}" startLink="{link_at(n)}"/>\n'
              for k, n in enumerate(BIKE_STATIONS) for b in range(BIKES_PER_STATION))
    + '</vehicles>\n</service>\n')

(out / "output_config.xml").write_text('<?xml version="1.0"?>\n<config><module name="global">'
    '<param name="coordinateSystem" value="EPSG:2056"/></module>'
    '<module name="sharing"><parameterset type="mode"><param name="id" value="publibike"/>'
    '<param name="mode" value="publibike"/><param name="serviceScheme" value="StationBased"/>'
    '<param name="serviceInputFile" value="publibike_service.xml"/></parameterset></module></config>\n')

ev = []
def E(t, typ, **kw): ev.append((t, len(ev), typ, kw))

# buses: one every 10 min from 06:00 to 20:00, 40 s per link, 30 s dwell
bus_times = {}
for k, dep in enumerate(range(6*3600, 20*3600, 600)):
    v = f"bus_{k}"; t = dep
    E(t - 60, "TransitDriverStarts", driverId=f"pt_{v}", vehicleId=v, transitLineId="bus", transitRouteId="bus_e", departureId=v)
    for s, n in enumerate(STOPS):
        E(t, "VehicleArrivesAtFacility", vehicle=v, facility=f"s{s}")
        bus_times.setdefault((k, s), t)
        E(t+30, "VehicleDepartsAtFacility", vehicle=v, facility=f"s{s}")
        t += 30
        if s + 1 < len(STOPS):
            for lid in route(n, STOPS[s+1]):
                E(t, "entered link", vehicle=v, link=lid); t += link_len(lid) / 8

# trains and trams, both ways, 06:00 to 20:00, 30 s dwell
track_times = {}  # (line, direction) -> [(vehicle, {stop: arrival time})]
for line, mode, stops, hop, headway in TRACK_LINES:
    for d in (0, 1):
        seq = stops if d == 0 else stops[::-1]
        for k, dep in enumerate(range(6*3600 + d*120, 20*3600, headway)):
            v = f"{line}_{d}_{k}"; t = dep; times = {}
            E(t - 60, "TransitDriverStarts", driverId=f"pt_{v}", vehicleId=v, transitLineId=line, transitRouteId=f"{line}_{d}", departureId=v)
            for s, (sid, *_ ) in enumerate(seq):
                E(t, "VehicleArrivesAtFacility", vehicle=v, facility=sid); times[sid] = t
                E(t+30, "VehicleDepartsAtFacility", vehicle=v, facility=sid); t += 30
                if s + 1 < len(seq):
                    E(t + 1, "entered link", vehicle=v, link=f"pt_{sid}_{seq[s+1][0]}"); t += hop
            track_times.setdefault((line, d), []).append((v, times))

bike_trips = []  # PubliBike trips are played out afterwards, in time order, so station stocks stay right
TELE = {"walk": 1.3, "bike": 4.2, "publibike": 4.0, "car_passenger": 8.0, "truck": 9.0, "outside": 12.0, "remote_walk": 1.3}
def gap(p, q):  # metres between two nodes or (x, y) points
    (px, py), (qx, qy) = (p if isinstance(p, tuple) else NODES[p]), (q if isinstance(q, tuple) else NODES[q])
    return math.hypot(px - qx, py - qy)
def walk_s(p, q): return gap(p, q) * 1.3 / TELE["walk"]  # a walk's duration, at walking speed on a detour of 1.3
MAX_ACCESS_S = 1500  # PT riders with more walking to and from the stops than this (about 1.5 km) stay home
for p in range(3000):
    pid = f"p{p}"; mode = rnd.choices(["car", "pt", "walk", "bike", "publibike", "car_passenger", "truck", "outside", "remote_walk"], [30, 25, 20, 8, 4, 6, 3, 3, 1])[0]
    home, work = rnd.choice(PLACES), rnd.choice(PLACES)
    by_rail = mode == "pt" and rnd.random() < 0.5
    if mode == "pt":  # PT riders live and work near a line: within a few hundred metres of two of its stops
        stops = [s[1:] for s in rnd.choice(TRACK_LINES)[2]] if by_rail else [NODES[s] for s in STOPS]
        i, j = sorted(rnd.sample(range(len(stops)), 2))
        if rnd.random() < 0.5 and by_rail: i, j = j, i  # buses run west to east only, so bus riders live in the west
        home, work = (nearest(x + rnd.uniform(-400, 400), y + rnd.uniform(-400, 400)) for x, y in (stops[i], stops[j]))
    if dist(home, work) < 300: continue
    for leg_no, (a, b, t0) in enumerate(((home, work, rnd.gauss(7.6*3600, 1800)), (work, home, rnd.gauss(17.3*3600, 2400)))):
        if mode == "publibike":
            bike_trips.append((pid, leg_no, a, b, t0)); continue
        E(t0, "actend", person=pid, link=link_at(a), actType="home" if leg_no == 0 else "work", **xy(a))
        if mode in ("car", "truck"):  # both drive on the network; a link is entered when the one before is done
            E(t0, "departure", person=pid, link=link_at(a), legMode=mode)
            E(t0+1, "PersonEntersVehicle", person=pid, vehicle=pid)
            t = t0 + 1
            speed = rnd.uniform(5, 12) if mode == "car" else rnd.uniform(5, 9)
            for lid in route(a, b): E(t, "entered link", vehicle=pid, link=lid); t += link_len(lid) / speed
            E(t+5, "PersonLeavesVehicle", person=pid, vehicle=pid); t += 5
            E(t, "arrival", person=pid, link=link_at(b), legMode=mode)
        elif by_rail:
            # the train or tram line with the shortest walks: board at the stop nearest home, leave at the one nearest work
            line, stops, ia, ib = min(((l, st, min(range(len(st)), key=lambda i: gap(tuple(st[i][1:]), a)),
                                        min(range(len(st)), key=lambda i: gap(tuple(st[i][1:]), b))) for l, _, st, *_ in TRACK_LINES),
                                      key=lambda c: gap(tuple(c[1][c[2]][1:]), a) + gap(tuple(c[1][c[3]][1:]), b))
            (sa, xa, ya), (sb, xb, yb) = stops[ia], stops[ib]
            wa, wb = walk_s(a, (xa, ya)), walk_s((xb, yb), b)
            if ia == ib or wa + wb > MAX_ACCESS_S: continue
            d = 0 if ia < ib else 1
            run = next(((v, tm) for v, tm in track_times[(line, d)] if tm[sa] > t0 + wa), None)
            if run is None: continue
            v, tm = run
            E(t0, "departure", person=pid, link=link_at(a), legMode="walk")
            E(t0+wa, "arrival", person=pid, link=f"pt_{sa}_{stops[ia+1 if ia+1 < len(stops) else ia-1][0]}", legMode="walk")
            E(t0+wa, "actstart", person=pid, link="", actType="pt interaction", x=str(xa), y=str(ya))
            E(t0+wa, "actend", person=pid, link="", actType="pt interaction", x=str(xa), y=str(ya))
            E(t0+wa, "departure", person=pid, link="", legMode="pt")
            E(tm[sa]+5, "PersonEntersVehicle", person=pid, vehicle=v)
            ta = tm[sb] + 5
            E(ta, "PersonLeavesVehicle", person=pid, vehicle=v)
            E(ta, "arrival", person=pid, link="", legMode="pt")
            E(ta, "actstart", person=pid, link="", actType="pt interaction", x=str(xb), y=str(yb))
            E(ta, "actend", person=pid, link="", actType="pt interaction", x=str(xb), y=str(yb))
            E(ta, "departure", person=pid, link="", legMode="walk")
            t = ta + wb; E(t, "arrival", person=pid, link=link_at(b), legMode="walk")
        elif mode == "pt":
            s_a = min(range(len(STOPS)), key=lambda s: dist(STOPS[s], a))
            s_b = min(range(len(STOPS)), key=lambda s: dist(STOPS[s], b))
            wa, wb = walk_s(a, STOPS[s_a]), walk_s(STOPS[s_b], b)
            if s_a >= s_b or wa + wb > MAX_ACCESS_S: continue  # buses run one way only
            k = next((k for k in range(84) if bus_times.get((k, s_a), 0) > t0 + wa), None)
            if k is None: continue
            sn = STOPS[s_a]
            E(t0, "departure", person=pid, link=link_at(a), legMode="walk")
            E(t0+wa, "arrival", person=pid, link=link_at(sn), legMode="walk")
            E(t0+wa, "actstart", person=pid, link=link_at(sn), actType="pt interaction", **xy(sn))
            E(t0+wa, "actend", person=pid, link=link_at(sn), actType="pt interaction", **xy(sn))
            E(t0+wa, "departure", person=pid, link=link_at(sn), legMode="pt")
            E(bus_times[(k, s_a)]+5, "PersonEntersVehicle", person=pid, vehicle=f"bus_{k}")
            ta = bus_times[(k, s_b)] + 5; en = STOPS[s_b]
            E(ta, "PersonLeavesVehicle", person=pid, vehicle=f"bus_{k}")
            E(ta, "arrival", person=pid, link=link_at(en), legMode="pt")
            E(ta, "actstart", person=pid, link=link_at(en), actType="pt interaction", **xy(en))
            E(ta, "actend", person=pid, link=link_at(en), actType="pt interaction", **xy(en))
            E(ta, "departure", person=pid, link=link_at(en), legMode="walk")
            t = ta + wb; E(t, "arrival", person=pid, link=link_at(b), legMode="walk")
        else:
            d = dist(a, b) * 1.3
            E(t0, "departure", person=pid, link=link_at(a), legMode=mode)
            t = t0 + d / TELE[mode]
            E(t, "travelled", person=pid, distance=f"{d:.1f}", mode=mode)
            E(t, "arrival", person=pid, link=link_at(b), legMode=mode)
        E(t, "actstart", person=pid, link=link_at(b), actType="work" if leg_no == 0 else "home", **xy(b))

# PubliBike: walk to the nearest station, take a bike if one is there, ride to the station nearest the
# destination and walk on. With no bike, the pickup fails and the person walks the whole way.
near = lambda p, skip=None: min((k for k in range(len(BIKE_STATIONS)) if k != skip), key=lambda k: dist(p, BIKE_STATIONS[k]))
stock = {k: [f"b{k}_{n}" for n in range(BIKES_PER_STATION)] for k in range(len(BIKE_STATIONS))}
returns = []  # (time, station, bike) dropoffs not yet counted
plans = []
for pid, leg_no, a, b, t0 in bike_trips:
    sa = near(a); sb = near(b, skip=sa)
    plans.append((t0 + max(60, dist(a, BIKE_STATIONS[sa])), pid, leg_no, a, b, t0, sa, sb))
for t1, pid, leg_no, a, b, t0, sa, sb in sorted(plans):
    while returns and returns[0][0] <= t1:
        _, k, bike = heapq.heappop(returns); stock[k].append(bike)
    A, B = BIKE_STATIONS[sa], BIKE_STATIONS[sb]
    la, lb = link_at(A), link_at(B)
    E(t0, "actend", person=pid, link=link_at(a), actType="home" if leg_no == 0 else "work", **xy(a))
    E(t0, "departure", person=pid, link=link_at(a), legMode="walk")
    E(t1, "arrival", person=pid, link=la, legMode="walk")
    E(t1, "actstart", person=pid, link=la, actType="sharing pickup interaction", **xy(A))
    if stock[sa]:
        bike = stock[sa].pop()
        E(t1, "sharing vehicle pickup", person=pid, service="publibike", link=la, vehicle=bike, station=f"st{sa}")
        E(t1+30, "actend", person=pid, link=la, actType="sharing pickup interaction", **xy(A))
        E(t1+30, "departure", person=pid, link=la, legMode="publibike")
        d = dist(A, B) * 1.4; t2 = t1 + 30 + d / TELE["publibike"]
        E(t2, "travelled", person=pid, distance=f"{d:.1f}", mode="publibike")
        E(t2, "arrival", person=pid, link=lb, legMode="publibike")
        E(t2, "actstart", person=pid, link=lb, actType="sharing dropoff interaction", **xy(B))
        E(t2, "sharing vehicle dropoff", person=pid, service="publibike", link=lb, vehicle=bike, station=f"st{sb}")
        heapq.heappush(returns, (t2, sb, bike))
        E(t2+30, "actend", person=pid, link=lb, actType="sharing dropoff interaction", **xy(B))
        E(t2+30, "departure", person=pid, link=lb, legMode="walk")
        t = t2 + 30 + max(60, dist(B, b))
    else:
        # like the contrib: the failed event names the nearest station that still has a bike (where the
        # agent would try next), not the empty one; with no bike anywhere there is no event
        spare = [k for k in range(len(BIKE_STATIONS)) if stock[k]]
        if spare:
            k = min(spare, key=lambda k: dist(A, BIKE_STATIONS[k]))
            E(t1, "sharing vehicle pickup failed", person=pid, service="publibike",
              link=link_at(BIKE_STATIONS[k]), station=f"st{k}")
        E(t1+30, "actend", person=pid, link=la, actType="sharing pickup interaction", **xy(A))
        E(t1+30, "departure", person=pid, link=la, legMode="walk")
        t = t1 + 30 + max(60, dist(A, b))
    E(t, "arrival", person=pid, link=link_at(b), legMode="walk")
    E(t, "actstart", person=pid, link=link_at(b), actType="work" if leg_no == 0 else "home", **xy(b))

ev.sort()
with gzip.open(out / "output_events.xml.gz", "wt") as f:
    f.write('<?xml version="1.0" encoding="utf-8"?>\n<events version="1.0">\n')
    for t, _, typ, kw in ev:
        attrs = " ".join(f'{k}="{v}"' for k, v in kw.items())
        f.write(f'\t<event time="{t:.1f}" type="{typ}" {attrs}  />\n')
    f.write('</events>\n')
print(f"fake run with {len(ev)} events in {out}")
