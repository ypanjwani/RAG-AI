// Variety test: generate explainers for several different AI topics through
// the local dev server and report which tools each one used and what it cost.
// Makes real API calls (costs money). Needs `npm run dev` running.
//
//   node scripts/variety.mjs                       all topics, Quality+ mode (prints the clarity and fact-check stats)
//   node scripts/variety.mjs --mode freeform-plus 2 5   topics #2 and #5 (1-based), Freeform+ mode
//   node scripts/variety.mjs --mode quality-deep --effort low   Quality 2.0 at Low effort (default: the server's PLAN_EFFORT)
//   node scripts/variety.mjs --mode code-lab-2 --effort low --no-think 1 4   Code Lab 2.0 with chart planning thinking off
//   node scripts/variety.mjs --mode playground --effort low 1   Playground mode on topic #1
//
// Specs are saved to .variety/<mode>-<n>.json (<mode>-<effort>-<n>.json with --effort, plus -nothink with --no-think)
// for inspection.

import { mkdirSync, writeFileSync } from "node:fs";

const API = process.env.API ?? "http://localhost:5173";
const TOPICS = [
  "Self-attention in transformers: queries, keys, values and why scaling by sqrt(d_k) matters.",
  "Diffusion models: how adding noise and learning to remove it generates images.",
  "PPO and RLHF: how language models are aligned with a reward model and a KL penalty.",
  "LoRA: low-rank adaptation for cheap fine-tuning of large models.",
  "Retrieval-augmented generation: embeddings, vector search and grounding answers.",
  "ReAct-style agents that interleave reasoning steps with tool calls.",
  "ResNet: why residual connections let very deep CNNs train.",
  "Evaluating LLMs on benchmarks: accuracy, confidence intervals and test-set contamination.",
];

// The API needs an account: EXPLAINER_EMAIL / EXPLAINER_PASSWORD sign in, otherwise a throwaway account
// is registered. Each account gets the free plan's daily allowance (FREE_DAILY_GENERATIONS on the server).
const email = process.env.EXPLAINER_EMAIL ?? `variety-${Date.now().toString(36)}@example.com`;
const password = process.env.EXPLAINER_PASSWORD ?? `variety-${Math.random().toString(36).slice(2)}-pw`;
let registered = !!process.env.EXPLAINER_EMAIL;
async function signIn() {
  const path = registered ? "login" : "register";
  registered = true;
  const res = await fetch(`${API}/api/auth/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Origin: "http://localhost:5173" },
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) throw new Error(`Sign-in failed: HTTP ${res.status}: ${await res.text()}`);
  return (await res.json()).accessToken;
}
let token;

async function generate(text, quality, effort) {
  token ??= await signIn(); // access tokens last 15 minutes; a long run signs in again below
  const res = await fetch(`${API}/api/generate`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Origin: "http://localhost:5173", Authorization: `Bearer ${token}` },
    // Quality 2.0: ask for every version and audit too (debugVersions; the app never sends it).
    body: JSON.stringify({ mode: "concept", text, quality, ...(effort ? { effort } : {}), ...(noThink ? { planThinking: false } : {}),
      ...(quality === "quality-deep" ? { debugVersions: true } : {}),
      // Code Lab: also the widgets Jev's review rejected (debugRejected; the app never sends it).
      ...(quality.startsWith("code-lab") || quality === "playground" ? { debugRejected: true } : {}) }),
  });
  if (res.status === 401) { token = undefined; return generate(text, quality, effort); }
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
  const body = await res.text();
  const events = body.split("\n").filter((l) => l.startsWith("data: ")).map((l) => JSON.parse(l.slice(6)));
  const err = events.find((e) => e.type === "error");
  if (err) throw new Error(err.message);
  const done = events.find((e) => e.type === "done");
  if (!done) throw new Error("stream ended without a spec");
  const checks = events.filter((e) => e.type === "step" && /Checking numbers/.test(e.detail ?? "")).length;
  return { spec: done.spec, usage: done.usage, checks, versions: done.versions, rejected: done.rejected, labMarks: done.labMarks, labMath: done.labMath };
}

const args = process.argv.slice(2);
const mi = args.indexOf("--mode");
const quality = mi >= 0 ? args.splice(mi, 2)[1] : "quality-plus";
const ei = args.indexOf("--effort");
const effort = ei >= 0 ? args.splice(ei, 2)[1] : undefined;
const ti = args.indexOf("--no-think");
const noThink = ti >= 0 && !!args.splice(ti, 1).length;
const name = (effort ? `${quality}-${effort}` : quality) + (noThink ? "-nothink" : "");
const pick = args.map(Number).filter(Boolean);
const list = TOPICS.map((t, i) => [i + 1, t]).filter(([n]) => !pick.length || pick.includes(n));
mkdirSync(".variety", { recursive: true });
let total = 0;

for (const [n, topic] of list) {
  process.stdout.write(`\n#${n} [${name}] ${topic}\n`);
  try {
    const { spec, usage, checks, versions, rejected, labMarks, labMath } = await generate(topic, quality, effort);
    writeFileSync(`.variety/${name}-${n}.json`, JSON.stringify({ spec, usage, ...(rejected ? { rejected } : {}), ...(labMarks ? { labMarks } : {}), ...(labMath ? { labMath } : {}) }, null, 1));
    if (labMath?.length) console.log(`  maths review: ${labMath.map((m) => `${m.section} ${m.outcome}${m.failed?.length ? ` (${m.failed.join(", ")})` : ""}${m.soft?.length ? ` soft: ${m.soft.join(", ")}` : ""}`).join("; ")}`);
    if (rejected?.length) console.log(`  rejected by the review (saved in the file): ${rejected.map((r) => `${r.section} [${r.kind}] "${r.title}"`).join("; ")}`);
    // Quality 2.0: each version with the findings and audit that led to it, as .variety/<name>-<n>-v<k>.json.
    (versions ?? []).forEach((v, k) => writeFileSync(`.variety/${name}-${n}-v${k + 1}.json`, JSON.stringify(v, null, 1)));
    const blocks = spec.stages.flatMap((s) => s.blocks);
    const types = blocks.map((b) => b.type);
    const hist = Object.entries(types.reduce((m, t) => ((m[t] = (m[t] ?? 0) + 1), m), {})).sort((a, b) => b[1] - a[1]);
    const exps = blocks.filter((b) => b.type === "experiment");
    const exSteps = blocks.filter((b) => b.type === "examples").flatMap((b) => b.items).filter((i) => i.steps?.length).length;
    const markers = (JSON.stringify(spec.stages).match(/"✎/g) ?? []).length;
    const words = blocks.filter((b) => b.type === "text").reduce((a, b) => a + b.md.split(/\s+/).length, 0);
    console.log(`  "${spec.title}" · ${spec.layout} · ${spec.stages.length} stages · ${spec.design.theme}/${spec.design.fontPairing} · compute calls: ${checks}`);
    console.log(`  stages: ${spec.stages.map((s) => s.label).join(" → ")}`);
    console.log(`  blocks: ${hist.map(([t, c]) => `${t}×${c}`).join(" ")}`);
    if (exps.length) console.log(`  experiments: ${exps.map((e) => `"${e.title}" repeats=${JSON.stringify(e.repeats)}`).join("; ")}`);
    console.log(`  worked examples with steps: ${exSteps} · words in text blocks: ${words} · leftover ✎ notes: ${markers}`);
    if (usage) {
      total += usage.totalUsd;
      const byStep = Object.entries(usage.steps.reduce((m, s) => ((m[s.step] = (m[s.step] ?? 0) + s.usd), m), {}));
      const out = usage.steps.reduce((a, s) => a + s.output, 0);
      console.log(`  cost: $${usage.totalUsd.toFixed(4)} in ${usage.seconds}s · output tokens ${out} · ${byStep.map(([k, v]) => `${k} $${v.toFixed(4)}`).join(", ")}`);
    }
    if (usage?.decisions?.length) {
      const d = usage.decisions;
      const built = d.filter((x) => x.kept);
      const firstOk = built.filter((x) => x.used === x.jevPick).length;
      const w = (x) => (x.worth === undefined ? "?" : x.worth.toFixed(2));
      console.log(`  filter: kept ${built.length}/${d.length} · kept worth ${built.map(w).join(" ")} · to text ${d.filter((x) => !x.kept).map(w).join(" ") || "none"}`);
      console.log(`  components: ${built.map((x) => `${x.jevPick}(${x.confidence.toFixed(2)})${x.used === x.jevPick ? "" : `→${x.used}`}`).join(" ")}`);
      console.log(`  Jev's first pick used: ${firstOk}/${built.length} · fallbacks: ${built.length - firstOk}`);
    }
    const sample = blocks.find((b, i) => b.type === "text" && i > 0) ?? blocks.find((b) => b.type === "text");
    if (sample) console.log(`  sample text: ${sample.md.replace(/\s+/g, " ").slice(0, 320)}${sample.md.length > 320 ? "…" : ""}`);
    if (spec.clarity) console.log(`  clarity: ${spec.clarity.flagged}/${spec.clarity.stages} stages flagged · ${spec.clarity.added} added · ${spec.clarity.edited} edited · ${spec.clarity.remaining} still flagged`);
    for (const v of (versions ?? []).filter((x) => x.findings)) {
      const byCheck = Object.entries(v.findings.reduce((m, f) => ((m[f.check] = (m[f.check] ?? 0) + 1), m), {})).map(([k, c]) => `${k}×${c}`).join(" ");
      const real = (v.audit ?? []).filter((i) => i.verdict === "real");
      console.log(`  ${v.label}: ${v.findings.length} findings (${byCheck || "none"}) · audit: ${real.length} real, ${(v.audit ?? []).length - real.length} false alarms · ${v.spec?.stages.length ?? "?"} stages after`);
      for (const i of real) console.log(`    [${i.action}] ${i.stage || "(new)"}: ${i.instruction.slice(0, 160)}`);
    }
    if (spec.factCheck) console.log(`  fact check: ${spec.factCheck.checked} checked · ${spec.factCheck.flagged} flagged · ${spec.factCheck.fixed} fixed`);
    console.log(`  warnings (${spec.warnings.length}): ${spec.warnings.join(" | ") || "none"}`);
  } catch (e) {
    console.log(`  FAILED: ${e.message}`);
  }
}
if (list.length > 1) console.log(`\nTotal cost (${name}): $${total.toFixed(4)}`);
