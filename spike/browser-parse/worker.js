// Spike: read a MATSim run in the browser and build the agents' legs, as build_legs() in
// matsim_agent_viz.py does (without bike sharing). Runs in a Web Worker; files never leave the machine.
"use strict";

const MIN_POINT_GAP_S = 10, MERGE_TOL_M = 8.0;

// ---------------------------------------------------------------- streaming XML scan
// Calls onTag(name, attrs) for every start tag whose name is in `names`. Reads the file as a
// stream (gunzipped on the fly for .gz), so memory stays flat however big the file is.
async function scan(file, names, onTag, step, onText) {
  let stream = file.stream();
  let read = 0;
  const counted = new TransformStream({
    transform(chunk, ctl) { read += chunk.byteLength; ctl.enqueue(chunk); },
  });
  stream = stream.pipeThrough(counted);
  if (file.name.endsWith(".gz")) stream = stream.pipeThrough(new DecompressionStream("gzip"));
  const reader = stream.pipeThrough(new TextDecoderStream()).getReader();
  let rest = "", lastReport = 0;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    const text = rest + value;
    let pos = 0;
    for (;;) {
      const lt = text.indexOf("<", pos);
      if (lt < 0) { pos = text.length; break; }
      const gt = text.indexOf(">", lt);
      if (gt < 0) { pos = lt; break; }
      let e = lt + 1;
      while (e < gt) {
        const c = text.charCodeAt(e);
        if (c === 32 || c === 9 || c === 10 || c === 13 || c === 47 || c === 62) break;
        e++;
      }
      const name = text.slice(lt + 1, e);
      if (names.has(name)) {
        if (onText) {  // also hand over the text up to the next tag, so wait until it is all here
          const nx = text.indexOf("<", gt);
          if (nx < 0) { pos = lt; break; }
          onTag(name, attrs(text, e, gt));
          onText(text.slice(gt + 1, nx));
        } else onTag(name, attrs(text, e, gt));
      }
      pos = gt + 1;
    }
    rest = text.slice(pos);
    if (read - lastReport > 8e6) { lastReport = read; postMessage({ progress: step, read, size: file.size }); }
  }
  postMessage({ progress: step, read: file.size, size: file.size });
}

const ENT = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'" };
const unescape = s => s.replace(/&(#x?[0-9a-fA-F]+|\w+);/g, (m, e) =>
  e[0] === "#" ? String.fromCodePoint(e[1] === "x" ? parseInt(e.slice(2), 16) : +e.slice(1)) : (ENT[e] ?? m));

function attrs(s, i, end) {
  const out = {};
  for (;;) {
    const eq = s.indexOf("=", i);
    if (eq < 0 || eq > end) return out;
    let a = i;
    while (s.charCodeAt(a) <= 32) a++;
    const q = s[eq + 1];
    const close = s.indexOf(q, eq + 2);
    let v = s.slice(eq + 2, close);
    if (v.indexOf("&") >= 0) v = unescape(v);
    out[s.slice(a, eq).trimEnd()] = v;
    i = close + 1;
  }
}

// ---------------------------------------------------------------- sampling: crc32 as in zlib
const CRC = new Int32Array(256);
for (let n = 0; n < 256; n++) {
  let c = n;
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  CRC[n] = c;
}
const enc = new TextEncoder();
function crc32(str) {
  let c = -1;
  for (let i = 0; i < str.length; i++) {
    const ch = str.charCodeAt(i);
    if (ch >= 128) return crc32Bytes(enc.encode(str));
    c = CRC[(c ^ ch) & 0xff] ^ (c >>> 8);
  }
  return (c ^ -1) >>> 0;
}
function crc32Bytes(b) {
  let c = -1;
  for (const x of b) c = CRC[(c ^ x) & 0xff] ^ (c >>> 8);
  return (c ^ -1) >>> 0;
}

// ---------------------------------------------------------------- static inputs
async function readNetwork(file) {
  const nodes = new Map(), links = [];
  await scan(file, new Set(["node", "link"]), (tag, a) => {
    if (tag === "node") nodes.set(a.id, [+a.x, +a.y]);
    else links.push([a.id, a.from, a.to]);
  }, "network");
  const linkFrom = new Map(), linkMid = new Map();
  for (const [id, f, t] of links) {
    const A = nodes.get(f), B = nodes.get(t);
    if (A) linkFrom.set(id, A);
    if (A && B) linkMid.set(id, [(A[0] + B[0]) / 2, (A[1] + B[1]) / 2]);
  }
  return { linkFrom, linkMid, nNodes: nodes.size, nLinks: links.length };
}

function trackKind(m) {
  m = (m || "").toLowerCase();
  if (m.includes("tram")) return "tram";
  if (/rail|train|subway|metro|s-?bahn/.test(m)) return "rail";
  return null;
}

// The schedule needs the text inside <transportMode>, so this one scans tags and the text after them.
async function readSchedule(file) {
  const stops = new Map(), kinds = new Map();
  let line = null, route = null, wantMode = false;
  await scan(file, new Set(["stopFacility", "transitLine", "transitRoute", "transportMode"]), (tag, a) => {
    if (tag === "stopFacility") stops.set(a.id, [+a.x, +a.y]);
    else if (tag === "transitLine") line = a.id;
    else if (tag === "transitRoute") route = a.id;
    else wantMode = true;
  }, "schedule", (text) => {
    if (!wantMode) return;
    wantMode = false;
    const k = trackKind(text.trim());
    if (k) kinds.set(line + "\u0000" + route, k);
  });
  return { stops, kinds };
}

// ---------------------------------------------------------------- events -> legs
function simplify3(a, b, c, tol) {
  // Douglas-Peucker on three points: true when b is within tol of the line a-c (so it can go).
  const dx = c[0] - a[0], dy = c[1] - a[1], ln = Math.hypot(dx, dy);
  const d = ln ? Math.abs(dx * (a[1] - b[1]) - dy * (a[0] - b[0])) / ln : Math.hypot(b[0] - a[0], b[1] - a[1]);
  return !(d > tol);
}

async function buildLegs(file, sample, net, sched) {
  const threshold = Math.trunc(sample * 10000);
  const { linkFrom, linkMid } = net;
  const stops = sched?.stops ?? new Map(), routeKinds = sched?.kinds ?? new Map();
  const sampledCache = new Map();
  const sampled = p => {
    if (p === undefined || p.startsWith("pt_")) return false;
    let s = sampledCache.get(p);
    if (s === undefined) { s = crc32(p) % 10000 < threshold; sampledCache.set(p, s); }
    return s;
  };
  const pos = new Map(), openLeg = new Map(), pending = new Map(), riders = new Map();
  const onTracks = new Map(), departed = new Map();
  const legs = [];
  let nEvents = 0;

  const add = (person, t, xy, routed = false, keep = false) => {
    const leg = openLeg.get(person);
    if (!leg || !xy) return;
    if (routed) leg.routed = true;
    const pts = leg.pts, n = pts.length;
    if (n > 1 && t - pts[n - 1][0] < MIN_POINT_GAP_S && leg.kept !== n - 1 &&
        simplify3([pts[n - 2][1], pts[n - 2][2]], [pts[n - 1][1], pts[n - 1][2]], xy, MERGE_TOL_M)) {
      pts[n - 1] = [t, xy[0], xy[1]];
    } else pts.push([t, xy[0], xy[1]]);
    if (keep) leg.kept = pts.length - 1;
  };
  const finish = person => {
    const leg = pending.get(person);
    pending.delete(person);
    if (leg && leg.pts.length >= 2 && leg.pts[leg.pts.length - 1][0] > leg.pts[0][0]) legs.push(leg);
  };
  const actXY = a => (a.x !== undefined && a.y !== undefined) ? [+a.x, +a.y] : linkMid.get(a.link);

  await scan(file, new Set(["event"]), (_, a) => {
    nEvents++;
    const typ = a.type, t = +a.time, person = a.person;
    switch (typ) {
      case "actend":
        if (sampled(person)) { finish(person); pos.set(person, actXY(a)); }
        break;
      case "departure":
        if (sampled(person)) {
          finish(person);
          const start = pos.get(person) || linkMid.get(a.link);
          if (start) openLeg.set(person, { mode: a.legMode || "unknown", pts: [[t, start[0], start[1]]] });
        }
        break;
      case "PersonEntersVehicle":
        if (sampled(person) && openLeg.has(person)) {
          const veh = a.vehicle;
          let r = riders.get(veh);
          if (!r) riders.set(veh, r = new Set());
          r.add(person);
          const pts = openLeg.get(person).pts, last = pts[pts.length - 1];
          add(person, t, [last[1], last[2]]);
        }
        break;
      case "PersonLeavesVehicle": {
        const r = riders.get(a.vehicle);
        if (r) { r.delete(person); if (!r.size) riders.delete(a.vehicle); }
        break;
      }
      case "entered link": {
        const r = riders.get(a.vehicle);
        if (r) { const xy = linkFrom.get(a.link); for (const p of r) add(p, t, xy, true); }
        break;
      }
      case "VehicleArrivesAtFacility":
      case "VehicleDepartsAtFacility": {
        const veh = a.vehicle, stop = a.facility, kind = onTracks.get(veh);
        const r = riders.get(veh);
        if (r) {
          const xy = stops.get(stop);
          const prev = kind && typ === "VehicleArrivesAtFacility" ? departed.get(veh) : undefined;
          for (const p of r) {
            const leg = openLeg.get(p);
            let hop = null;
            if (prev && xy && leg && prev[0] !== stop && leg.kept !== undefined && leg.pts[leg.kept][0] === prev[1])
              hop = [leg.kept, prev[0], stop, kind];
            add(p, t, xy, true, !!kind);
            if (hop && leg.pts[leg.pts.length - 1][0] === t)
              (leg.hops ??= []).push([hop[0], leg.pts.length - 1, hop[1], hop[2], hop[3]]);
          }
        }
        if (kind && typ === "VehicleDepartsAtFacility") departed.set(veh, [stop, t]);
        break;
      }
      case "TransitDriverStarts": {
        const kind = routeKinds.get(a.transitLineId + "\u0000" + a.transitRouteId);
        if (kind) { onTracks.set(a.vehicleId, kind); departed.delete(a.vehicleId); }
        break;
      }
      case "arrival":
        if (sampled(person) && openLeg.has(person)) {
          const leg = openLeg.get(person);
          openLeg.delete(person);
          const end = linkMid.get(a.link);
          if (end) leg.pts.push([t, end[0], end[1]]);
          pending.set(person, leg);
          if (end) pos.set(person, end);
        }
        break;
      case "actstart":
        if (sampled(person)) {
          const xy = actXY(a), leg = pending.get(person);
          if (leg && xy) { const l = leg.pts[leg.pts.length - 1]; leg.pts[leg.pts.length - 1] = [l[0], xy[0], xy[1]]; }
          finish(person);
          if (xy) pos.set(person, xy);
        }
        break;
    }
  }, "events");
  for (const p of [...pending.keys()]) finish(p);
  return { legs, nEvents };
}

// Same summary as bench_python.py, to check both build the same legs.
function summary(legs) {
  let points = 0, hops = 0, sum = 0;
  const modes = {};
  for (const l of legs) {
    points += l.pts.length;
    hops += l.hops?.length ?? 0;
    modes[l.mode] = (modes[l.mode] ?? 0) + 1;
    for (const p of l.pts) sum += p[0] + p[1] + p[2];
  }
  return { legs: legs.length, points, hops, checksum: Math.round(sum), modes };
}

onmessage = async ({ data: { events, network, schedule, sample } }) => {
  try {
    const time = {};
    let t0 = performance.now();
    const net = await readNetwork(network);
    time.network = (performance.now() - t0) / 1000;
    let sched = null;
    if (schedule) {
      t0 = performance.now();
      sched = await readSchedule(schedule);
      time.schedule = (performance.now() - t0) / 1000;
    }
    t0 = performance.now();
    const { legs, nEvents } = await buildLegs(events, sample, net, sched);
    time.events = (performance.now() - t0) / 1000;
    postMessage({ done: { time, nEvents, nNodes: net.nNodes, nLinks: net.nLinks,
      nStops: sched?.stops.size ?? 0, ...summary(legs) } });
  } catch (e) {
    postMessage({ error: String(e?.stack || e) });
  }
};
