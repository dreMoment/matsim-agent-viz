"""Write a tiny fake MATSim output folder (events, network, schedule, config, bike-sharing stations)."""
import gzip, heapq, random, sys
from pathlib import Path

out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
rnd = random.Random(3)
X0, Y0, N, D = 2680000, 1245000, 25, 300  # 25x25 grid, 300 m spacing, around Zurich HB

def nid(i, j): return f"n{i}_{j}"
links = {}
with gzip.open(out / "output_network.xml.gz", "wt") as f:
    f.write('<?xml version="1.0"?>\n<network>\n<nodes>\n')
    for i in range(N):
        for j in range(N):
            f.write(f'<node id="{nid(i,j)}" x="{X0+i*D}" y="{Y0+j*D}"/>\n')
    f.write('</nodes>\n<links>\n')
    for i in range(N):
        for j in range(N):
            for di, dj in ((1,0),(0,1),(-1,0),(0,-1)):
                a, b = i+di, j+dj
                if 0 <= a < N and 0 <= b < N:
                    lid = f"{nid(i,j)}-{nid(a,b)}"; links[(i,j,a,b)] = lid
                    f.write(f'<link id="{lid}" from="{nid(i,j)}" to="{nid(a,b)}" length="{D}" freespeed="13.9" capacity="1000" permlanes="1" modes="car,pt"/>\n')
    f.write('</links>\n</network>\n')

# a bus line along row j=12, stops every 3 nodes
STOPS = [(i, 12) for i in range(0, N, 3)]
with gzip.open(out / "output_transitSchedule.xml.gz", "wt") as f:
    f.write('<?xml version="1.0"?>\n<transitSchedule>\n<transitStops>\n')
    for k, (i, j) in enumerate(STOPS):
        f.write(f'<stopFacility id="s{k}" x="{X0+i*D}" y="{Y0+j*D}" linkRefId="{links[(i,j,i+1,j)] if i+1<N else links[(i,j,i-1,j)]}"/>\n')
    f.write('</transitStops>\n</transitSchedule>\n')

# station-based bike sharing (shared_mobility contrib): a few stations with few bikes, so some run empty
BIKE_STATIONS = [(3, 3), (3, 12), (3, 21), (12, 3), (12, 21), (21, 3), (21, 12), (21, 21), (8, 8), (16, 16)]
BIKES_PER_STATION = 3
(out / "publibike_service.xml").write_text('<?xml version="1.0" encoding="utf-8"?>\n<service>\n<stations>\n'
    + "".join(f'<station id="st{k}" link="{links[(i,j,i+1,j)]}" capacity="100"/>\n' for k, (i, j) in enumerate(BIKE_STATIONS))
    + '</stations>\n<vehicles>\n'
    + "".join(f'<vehicle id="b{k}_{n}" startStation="st{k}" startLink="{links[(i,j,i+1,j)]}"/>\n'
              for k, (i, j) in enumerate(BIKE_STATIONS) for n in range(BIKES_PER_STATION))
    + '</vehicles>\n</service>\n')

(out / "output_config.xml").write_text('<?xml version="1.0"?>\n<config><module name="global">'
    '<param name="coordinateSystem" value="EPSG:2056"/></module>'
    '<module name="sharing"><parameterset type="mode"><param name="id" value="publibike"/>'
    '<param name="mode" value="publibike"/><param name="serviceScheme" value="StationBased"/>'
    '<param name="serviceInputFile" value="publibike_service.xml"/></parameterset></module></config>\n')

ev = []
def E(t, typ, **kw): ev.append((t, len(ev), typ, kw))
def path(a, b):
    (i, j), (ti, tj) = a, b; out = []
    while i != ti: ni = i + (1 if ti > i else -1); out.append(links[(i,j,ni,j)]); i = ni
    while j != tj: nj = j + (1 if tj > j else -1); out.append(links[(i,j,i,nj)]); j = nj
    return out
def link_at(i, j): return links[(i,j,i+1,j)] if i+1 < N else links[(i,j,i-1,j)]
def xy(i, j): return dict(x=str(X0+i*D+20), y=str(Y0+j*D+20))

# buses: one every 10 min from 06:00 to 20:00, 40 s per link, 30 s dwell
bus_times = {}
for k, dep in enumerate(range(6*3600, 20*3600, 600)):
    v = f"bus_{k}"; t = dep
    for s, (i, j) in enumerate(STOPS):
        E(t, "VehicleArrivesAtFacility", vehicle=v, facility=f"s{s}")
        bus_times.setdefault((k, s), t)
        E(t+30, "VehicleDepartsAtFacility", vehicle=v, facility=f"s{s}")
        t += 30
        if s + 1 < len(STOPS):
            for lid in path((i, j), STOPS[s+1]):
                E(t, "entered link", vehicle=v, link=lid); t += 40

bike_trips = []  # PubliBike trips are played out afterwards, in time order, so station stocks stay right
TELE = {"walk": 1.3, "bike": 4.2, "publibike": 4.0, "car_passenger": 8.0, "truck": 9.0, "outside": 12.0, "remote_walk": 1.3}
for p in range(3000):
    pid = f"p{p}"; mode = rnd.choices(["car", "pt", "walk", "bike", "publibike", "car_passenger", "truck", "outside", "remote_walk"], [30, 25, 20, 8, 4, 6, 3, 3, 1])[0]
    home = (rnd.randrange(N), rnd.randrange(N)); work = (rnd.randrange(N), rnd.randrange(N))
    if home == work: continue
    for leg_no, (a, b, t0) in enumerate(((home, work, rnd.gauss(7.6*3600, 1800)), (work, home, rnd.gauss(17.3*3600, 2400)))):
        if mode == "publibike":
            bike_trips.append((pid, leg_no, a, b, t0)); continue
        E(t0, "actend", person=pid, link=link_at(*a), actType="home" if leg_no == 0 else "work", **xy(*a))
        if mode == "car":
            E(t0, "departure", person=pid, link=link_at(*a), legMode="car")
            E(t0+1, "PersonEntersVehicle", person=pid, vehicle=pid)
            t = t0 + 1
            for lid in path(a, b): t += rnd.uniform(25, 60); E(t, "entered link", vehicle=pid, link=lid)
            E(t+20, "PersonLeavesVehicle", person=pid, vehicle=pid); t += 20
            E(t, "arrival", person=pid, link=link_at(*b), legMode="car")
        elif mode == "pt":
            s_a = min(range(len(STOPS)), key=lambda s: abs(STOPS[s][0]-a[0]))
            s_b = min(range(len(STOPS)), key=lambda s: abs(STOPS[s][0]-b[0]))
            if s_a >= s_b: s_a, s_b = 0, len(STOPS)-1  # buses run one way only
            k = next((k for k in range(84) if bus_times.get((k, s_a), 0) > t0 + 300), None)
            if k is None: continue
            si, sj = STOPS[s_a]
            E(t0, "departure", person=pid, link=link_at(*a), legMode="walk")
            E(t0+300, "arrival", person=pid, link=link_at(si, sj), legMode="walk")
            E(t0+300, "actstart", person=pid, link=link_at(si, sj), actType="pt interaction", **xy(si, sj))
            E(t0+300, "actend", person=pid, link=link_at(si, sj), actType="pt interaction", **xy(si, sj))
            E(t0+300, "departure", person=pid, link=link_at(si, sj), legMode="pt")
            E(bus_times[(k, s_a)]+5, "PersonEntersVehicle", person=pid, vehicle=f"bus_{k}")
            ta = bus_times[(k, s_b)] + 5; ei, ej = STOPS[s_b]
            E(ta, "PersonLeavesVehicle", person=pid, vehicle=f"bus_{k}")
            E(ta, "arrival", person=pid, link=link_at(ei, ej), legMode="pt")
            E(ta, "actstart", person=pid, link=link_at(ei, ej), actType="pt interaction", **xy(ei, ej))
            E(ta, "actend", person=pid, link=link_at(ei, ej), actType="pt interaction", **xy(ei, ej))
            E(ta, "departure", person=pid, link=link_at(ei, ej), legMode="walk")
            t = ta + 300; E(t, "arrival", person=pid, link=link_at(*b), legMode="walk")
        else:
            dist = (abs(a[0]-b[0]) + abs(a[1]-b[1])) * D * 1.3
            E(t0, "departure", person=pid, link=link_at(*a), legMode=mode)
            t = t0 + dist / TELE[mode]
            E(t, "travelled", person=pid, distance=f"{dist:.1f}", mode=mode)
            E(t, "arrival", person=pid, link=link_at(*b), legMode=mode)
        E(t, "actstart", person=pid, link=link_at(*b), actType="work" if leg_no == 0 else "home", **xy(*b))

# PubliBike: walk to the nearest station, take a bike if one is there, ride to the station nearest the
# destination and walk on. With no bike, the pickup fails and the person walks the whole way.
md = lambda a, b: abs(a[0]-b[0]) + abs(a[1]-b[1])
near = lambda p, skip=None: min((k for k in range(len(BIKE_STATIONS)) if k != skip), key=lambda k: md(p, BIKE_STATIONS[k]))
stock = {k: [f"b{k}_{n}" for n in range(BIKES_PER_STATION)] for k in range(len(BIKE_STATIONS))}
returns = []  # (time, station, bike) dropoffs not yet counted
plans = []
for pid, leg_no, a, b, t0 in bike_trips:
    sa = near(a); sb = near(b, skip=sa)
    plans.append((t0 + max(60, md(a, BIKE_STATIONS[sa]) * D), pid, leg_no, a, b, t0, sa, sb))
for t1, pid, leg_no, a, b, t0, sa, sb in sorted(plans):
    while returns and returns[0][0] <= t1:
        _, k, bike = heapq.heappop(returns); stock[k].append(bike)
    A, B = BIKE_STATIONS[sa], BIKE_STATIONS[sb]
    la, lb = links[(A[0], A[1], A[0]+1, A[1])], links[(B[0], B[1], B[0]+1, B[1])]
    E(t0, "actend", person=pid, link=link_at(*a), actType="home" if leg_no == 0 else "work", **xy(*a))
    E(t0, "departure", person=pid, link=link_at(*a), legMode="walk")
    E(t1, "arrival", person=pid, link=la, legMode="walk")
    E(t1, "actstart", person=pid, link=la, actType="sharing pickup interaction", **xy(*A))
    if stock[sa]:
        bike = stock[sa].pop()
        E(t1, "sharing vehicle pickup", person=pid, service="publibike", link=la, vehicle=bike, station=f"st{sa}")
        E(t1+30, "actend", person=pid, link=la, actType="sharing pickup interaction", **xy(*A))
        E(t1+30, "departure", person=pid, link=la, legMode="publibike")
        dist = md(A, B) * D * 1.4; t2 = t1 + 30 + dist / TELE["publibike"]
        E(t2, "travelled", person=pid, distance=f"{dist:.1f}", mode="publibike")
        E(t2, "arrival", person=pid, link=lb, legMode="publibike")
        E(t2, "actstart", person=pid, link=lb, actType="sharing dropoff interaction", **xy(*B))
        E(t2, "sharing vehicle dropoff", person=pid, service="publibike", link=lb, vehicle=bike, station=f"st{sb}")
        heapq.heappush(returns, (t2, sb, bike))
        E(t2+30, "actend", person=pid, link=lb, actType="sharing dropoff interaction", **xy(*B))
        E(t2+30, "departure", person=pid, link=lb, legMode="walk")
        t = t2 + 30 + max(60, md(B, b) * D)
    else:
        # like the contrib: the failed event names the nearest station that still has a bike (where the
        # agent would try next), not the empty one; with no bike anywhere there is no event
        spare = [k for k in range(len(BIKE_STATIONS)) if stock[k]]
        if spare:
            k = min(spare, key=lambda k: md(A, BIKE_STATIONS[k])); K = BIKE_STATIONS[k]
            E(t1, "sharing vehicle pickup failed", person=pid, service="publibike",
              link=links[(K[0], K[1], K[0]+1, K[1])], station=f"st{k}")
        E(t1+30, "actend", person=pid, link=la, actType="sharing pickup interaction", **xy(*A))
        E(t1+30, "departure", person=pid, link=la, legMode="walk")
        t = t1 + 30 + max(60, md(A, b) * D)
    E(t, "arrival", person=pid, link=link_at(*b), legMode="walk")
    E(t, "actstart", person=pid, link=link_at(*b), actType="work" if leg_no == 0 else "home", **xy(*b))

ev.sort()
with gzip.open(out / "output_events.xml.gz", "wt") as f:
    f.write('<?xml version="1.0" encoding="utf-8"?>\n<events version="1.0">\n')
    for t, _, typ, kw in ev:
        attrs = " ".join(f'{k}="{v}"' for k, v in kw.items())
        f.write(f'\t<event time="{t:.1f}" type="{typ}" {attrs}  />\n')
    f.write('</events>\n')
print(f"fake run with {len(ev)} events in {out}")
