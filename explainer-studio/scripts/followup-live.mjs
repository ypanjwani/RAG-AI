// Live follow-up test: take a saved page (.variety/<file>.json, from variety.mjs), send a follow-up request about it the way the
// app does (shared/followup.ts buildFollowUp), and report what changed: the revision summary, warnings, cost and a diff of
// every stage against the page it was made from (an untouched stage must be identical).
// Makes real API calls (costs money). Needs the Flask API on :8787 (npm run dev:api).
//
//   node scripts/followup-live.mjs --base playground-low-1 --quality playground --effort low --name pg-chart "Let me change the number of keys in the d_k chart"
//
// Output: .variety/followup/<name>.json ({spec, usage, request, base}).

import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createServer } from "vite";

const API = process.env.API ?? "http://localhost:8787";
const args = process.argv.slice(2);
const opt = (k, d) => { const i = args.indexOf(`--${k}`); return i >= 0 ? args.splice(i, 2)[1] : d; };
const base = opt("base", "playground-low-1");
const quality = opt("quality", "quality-plus");
const effort = opt("effort", "low");
const name = opt("name", `${base}-${Date.now().toString(36)}`);
const text = args.join(" ");
if (!text) { console.error("Give the follow-up request as the last argument."); process.exit(1); }

const vite = await createServer({ configFile: false, root: process.cwd(), logLevel: "error", server: { middlewareMode: true, hmr: false }, appType: "custom", optimizeDeps: { noDiscovery: true } });
const { buildFollowUp } = await vite.ssrLoadModule("/shared/followup.ts");
await vite.close();

const prev = JSON.parse(readFileSync(`.variety/${base}.json`, "utf8")).spec;
const followUp = buildFollowUp("revise", prev, [prev]);
if (!followUp) throw new Error("The page is too large to revise.");

// One throwaway account, saved next to the results and reused (sign-ups are limited to 5 an hour per network).
const ACCOUNT = ".variety/followup/account.json";
mkdirSync(".variety/followup", { recursive: true });
const post = (path, body) => fetch(`${API}/api/auth/${path}`, { method: "POST", headers: { "Content-Type": "application/json", Origin: "http://localhost:5173" }, body: JSON.stringify(body) });
let account = existsSync(ACCOUNT) ? JSON.parse(readFileSync(ACCOUNT, "utf8")) : null;
let auth = account ? await post("login", account) : null;
if (!auth?.ok) {
  account = { email: `followup-${Date.now().toString(36)}@example.com`, password: `pw-${Math.random().toString(36).slice(2)}-xx` };
  auth = await post("register", account);
  if (!auth.ok) throw new Error(`Sign-in failed: HTTP ${auth.status}: ${await auth.text()}`);
  writeFileSync(ACCOUNT, JSON.stringify(account));
}
const token = (await auth.json()).accessToken;

const t0 = Date.now();
const res = await fetch(`${API}/api/generate`, {
  method: "POST",
  headers: { "Content-Type": "application/json", Origin: "http://localhost:5173", Authorization: `Bearer ${token}` },
  body: JSON.stringify({ mode: "concept", text, quality, effort, followUp, ...(quality.startsWith("code-lab") || quality === "playground" ? { debugRejected: true } : {}) }),
});
if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
const events = (await res.text()).split("\n").filter((l) => l.startsWith("data: ")).map((l) => JSON.parse(l.slice(6)));
const err = events.find((e) => e.type === "error");
if (err) { console.log(`ERROR: ${err.message}`); process.exit(1); }
const done = events.find((e) => e.type === "done");
if (!done) throw new Error("stream ended without a spec");
const { spec, usage } = done;

mkdirSync(".variety/followup", { recursive: true });
writeFileSync(`.variety/followup/${name}.json`, JSON.stringify({ request: text, quality, effort, base, spec, usage }, null, 1));

const plan = events.filter((e) => e.type === "step" && e.step === "plan").map((e) => e.detail).filter(Boolean);
console.log(`\n[${name}] ${quality}/${effort} on ${base}: "${text}"`);
console.log(`  plan steps: ${plan.join(" → ")}`);
console.log(`  revision: ${JSON.stringify(spec.revision ?? "none (written whole)")}`);
console.log(`  stages: ${prev.stages.length} → ${spec.stages.length}`);

// Every stage against the page it came from.
const canon = (v) => (Array.isArray(v) ? v.map(canon) : v && typeof v === "object" ? Object.fromEntries(Object.keys(v).sort().map((k) => [k, canon(v[k])])) : v); // the server orders keys differently
const key = (st) => JSON.stringify(canon({ ...st, icon: undefined }));
const before = new Map(prev.stages.map((s) => [s.id, s]));
const widgetsOf = (st) => st.blocks.filter((b) => b.type === "widget");
for (const st of spec.stages) {
  const b = before.get(st.id);
  if (!b) { console.log(`  + ${st.id}: new stage`); continue; }
  const same = key(b) === key(st);
  const w0 = widgetsOf(b), w1 = widgetsOf(st);
  const wchanged = w0.length === w1.length ? w1.filter((w, i) => w.html !== w0[i].html).map((w) => w.title) : ["(count changed)"];
  const textSame = JSON.stringify(canon(b.blocks.filter((x) => x.type !== "widget"))) === JSON.stringify(canon(st.blocks.filter((x) => x.type !== "widget")));
  console.log(`  ${same ? "=" : "~"} ${st.id}${same ? "" : ` · text ${textSame ? "same" : "CHANGED"} · widgets ${w1.length}${wchanged.length ? ` (changed: ${wchanged.join(", ")})` : " (same)"}`}`);
}
for (const id of before.keys()) if (!spec.stages.some((s) => s.id === id)) console.log(`  - ${id}: removed`);
console.log(`  widgets: ${prev.stages.flatMap(widgetsOf).length} → ${spec.stages.flatMap(widgetsOf).length}`);
console.log(`  warnings (${spec.warnings.length}): ${spec.warnings.join(" | ") || "none"}`);
if (usage) {
  const by = Object.entries(usage.steps.reduce((m, s) => ((m[s.step] = (m[s.step] ?? 0) + s.usd), m), {}));
  console.log(`  cost: $${usage.totalUsd.toFixed(4)} in ${((Date.now() - t0) / 1000).toFixed(0)}s · ${by.map(([k, v]) => `${k} $${v.toFixed(4)}`).join(", ")}`);
}
