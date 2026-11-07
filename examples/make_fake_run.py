"""Write a tiny fake MATSim output folder (events, network, schedule, config)."""
import gzip, random, sys
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

(out / "output_config.xml").write_text('<?xml version="1.0"?>\n<config><module name="global">'
    '<param name="coordinateSystem" value="EPSG:2056"/></module></config>\n')

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

TELE = {"walk": 1.3, "bike": 4.2, "publibike": 4.0, "car_passenger": 8.0, "truck": 9.0, "outside": 12.0, "remote_walk": 1.3}
for p in range(3000):
    pid = f"p{p}"; mode = rnd.choices(["car", "pt", "walk", "bike", "publibike", "car_passenger", "truck", "outside", "remote_walk"], [30, 25, 20, 8, 4, 6, 3, 3, 1])[0]
    home = (rnd.randrange(N), rnd.randrange(N)); work = (rnd.randrange(N), rnd.randrange(N))
    if home == work: continue
    for leg_no, (a, b, t0) in enumerate(((home, work, rnd.gauss(7.6*3600, 1800)), (work, home, rnd.gauss(17.3*3600, 2400)))):
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

ev.sort()
with gzip.open(out / "output_events.xml.gz", "wt") as f:
    f.write('<?xml version="1.0" encoding="utf-8"?>\n<events version="1.0">\n')
    for t, _, typ, kw in ev:
        attrs = " ".join(f'{k}="{v}"' for k, v in kw.items())
        f.write(f'\t<event time="{t:.1f}" type="{typ}" {attrs}  />\n')
    f.write('</events>\n')
print(f"fake run with {len(ev)} events in {out}")
