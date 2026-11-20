# /// script
# requires-python = ">=3.11"
# dependencies = ["pyproj", "scipy"]
# ///
"""Time the Python steps that the browser test (index.html) repeats, on the same run folder.

    uv run spike/browser-parse/bench_python.py RUN_FOLDER [--sample 0.1]

Prints times, then the same one-line summary as the browser page. Prints counts only, no run data."""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import matsim_agent_viz as m  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("run_dir", type=Path)
ap.add_argument("--sample", type=float, default=0.1)
args = ap.parse_args()
m.progress = lambda *a, **k: None  # keep the console to the summary
files = m.find_inputs(args.run_dir)

t0 = time.time()
link_from, link_mid, _, _ = m.read_network(files["network"])
t_net = time.time() - t0
stops, kinds = {}, {}
t_sched = None
if files.get("schedule"):
    t0 = time.time()
    stops, kinds, _ = m.read_schedule(files["schedule"])
    t_sched = time.time() - t0
t0 = time.time()
legs, n_events = m.build_legs(files["events"], args.sample, link_from, link_mid, stops, None, kinds)
t_ev = time.time() - t0

print(f"network   {t_net:7.1f} s")
if t_sched is not None:
    print(f"schedule  {t_sched:7.1f} s")
print(f"events    {t_ev:7.1f} s  ({n_events:,} events, {n_events / t_ev / 1e6:.2f} M/s)")
modes, s = {}, 0.0
for leg in legs:
    modes[leg["mode"]] = modes.get(leg["mode"], 0) + 1
    for p in leg["pts"]:
        s += p[0] + p[1] + p[2]
print(json.dumps({"legs": len(legs), "points": sum(len(l["pts"]) for l in legs),
                  "hops": sum(len(l.get("hops", ())) for l in legs), "checksum": round(s), "modes": modes},
                 separators=(",", ":")))
