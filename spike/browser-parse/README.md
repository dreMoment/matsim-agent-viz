# Spike: can the browser read a MATSim run?

A test for turning the tool into a website that reads the run in the browser, with no upload. `worker.js` ports
the network and schedule readers and `build_legs()` (without bike sharing) to JavaScript. It reads the files as
a stream, unzipping on the fly. `index.html` runs it in a Web Worker. The page's Content-Security-Policy
forbids all network connections, so the run can't leave the machine.

## Run it

```sh
python3 -m http.server 8799 --bind 127.0.0.1 --directory spike/browser-parse
```

Open http://127.0.0.1:8799, choose the run's output folder, press **Run**. Then run the Python steps on the same folder:

```sh
uv run spike/browser-parse/bench_python.py RUN_FOLDER --sample 0.1
```

Both print the same one-line summary (legs, points, hops, a checksum of all points, legs per mode).
If they match, the browser built the same legs.

## Results on the fake run

`examples/make_fake_run.py`, blown up 120 times with `scale_events.py` to 10.3 M events, sample 0.1, M4 Pro Mac:

| | Events per second | Time |
|---|---|---|
| Python (`build_legs`) | 0.37 M | 28.1 s |
| Node 26 | 1.35 M | 7.6 s |
| Chrome (this page) | 1.74 M | 5.9 s |

All three build the same 113,936 legs (same checksum). Memory peaks at about 300 MB for both Python and Node.
At this rate, a 49 M-event run would take about 30 s in Chrome, against about 2.5 minutes in Python.
