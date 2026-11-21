// Take the still of the start page background (demo/zurich.webp) again: its first frame, at 1920×1080 CSS pixels
// and twice that in device pixels. Serve the repo on 127.0.0.1:8000 first, then:
//   node tools/capture-still.mjs still.png
//   cwebp -q 70 -sharp_yuv still.png -o demo/zurich@2x.webp && cwebp -q 72 -sharp_yuv -resize 1920 0 still.png -o demo/zurich.webp
// Needs Google Chrome (set CHROME to its path elsewhere than macOS).
import { spawn } from "node:child_process";
import { writeFileSync, mkdtempSync } from "node:fs";
import os from "node:os"; import path from "node:path";
const [W, H, DPR, OUT] = [1920, 1080, 2, process.argv[2] ?? "still.png"];
const chrome = spawn(process.env.CHROME ?? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", ["--headless=new", "--remote-debugging-port=9333",
  "--user-data-dir=" + mkdtempSync(path.join(os.tmpdir(), "cap-")), "--hide-scrollbars", "--enable-gpu", "--ignore-gpu-blocklist", "about:blank"], { stdio: "ignore" });
const sleep = ms => new Promise(r => setTimeout(r, ms));
let ws; for (let i = 0; i < 50; i++) { try { const l = await (await fetch("http://127.0.0.1:9333/json")).json(); const p = l.find(t => t.type === "page"); if (p) { ws = new WebSocket(p.webSocketDebuggerUrl); break; } } catch {} await sleep(200); }
await new Promise(r => ws.onopen = r);
let id = 0; const wait = new Map();
ws.onmessage = e => { const m = JSON.parse(e.data); if (m.id && wait.has(m.id)) { wait.get(m.id)(m); wait.delete(m.id); } };
const send = (method, params = {}) => new Promise(r => { const i = ++id; wait.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
await send("Emulation.setDeviceMetricsOverride", { width: W, height: H, deviceScaleFactor: DPR, mobile: false });
await send("Page.enable"); await send("Page.navigate", { url: "http://127.0.0.1:8000/" }); await sleep(3000);
const r = await send("Runtime.evaluate", { awaitPromise: true, returnByValue: true, expression: `(async () => {
  const { renderParts } = await import('./js/engine.js');
  const [d, tpl] = await Promise.all([fetch('demo/zurich.json').then(r=>r.json()), fetch('page.html').then(r=>r.text())]);
  d.demo = true;
  const still = tpl.replace("STILL = DEMO && matchMedia", "STILL = DEMO || matchMedia");
  const u = URL.createObjectURL(new Blob(renderParts(still, d, {title:'',eyebrow:'',note:''}), {type:'text/html'}));
  for (const el of [document.getElementById('app'), document.getElementById('veil'), document.getElementById('poster'), document.querySelector('footer')]) el.style.visibility = 'hidden';
  const fr = document.getElementById('backdrop'); fr.onload = null; fr.style.transition = 'none';
  const ready = new Promise(r => addEventListener('message', e => e.source === fr.contentWindow && e.data?.demoReady && r()));
  fr.src = u; await ready; await new Promise(r => setTimeout(r, 3000));
  fr.style.opacity = 1; const a = fr.contentDocument.querySelector('.maplibregl-ctrl-attrib'); if (a) a.style.display = 'none';
  await new Promise(r => setTimeout(r, 500));
  return fr.contentDocument.getElementById('clock').textContent; })()` });
console.log("clock", r.result?.result?.value ?? JSON.stringify(r.result).slice(0, 300));
const shot = await send("Page.captureScreenshot", { format: "png" });
writeFileSync(OUT, Buffer.from(shot.result.data, "base64")); console.log("wrote", OUT);
ws.close(); chrome.kill();
