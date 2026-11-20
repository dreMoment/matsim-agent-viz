"""Copy a (fake) run with its events file blown up N times, for speed tests at real-run size.

    python3 spike/browser-parse/scale_events.py FAKE_RUN OUT_DIR N

Each event is written N times; copy k gets person and vehicle ids with the suffix _k, so the copies
are N independent populations moving at the same times. Other files are copied as they are."""
import gzip
import re
import shutil
import sys
from pathlib import Path

src, out, n = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
out.mkdir(parents=True, exist_ok=True)
for f in src.iterdir():
    if f.is_file() and "output_events" not in f.name:
        shutil.copy(f, out / f.name)
ids = re.compile(r'( (?:person|vehicle|vehicleId|driverId)=")([^"]*)(")')
with gzip.open(src / "output_events.xml.gz", "rt") as fi, \
        gzip.open(out / "output_events.xml.gz", "wt", compresslevel=6) as fo:
    for line in fi:
        if "<event " not in line:
            fo.write(line)
            continue
        fo.write("".join(ids.sub(lambda m: f"{m[1]}{m[2]}_{k}{m[3]}", line) for k in range(n)))
print(f"wrote {out / 'output_events.xml.gz'}")
