// Builds the data the Flask server reads from the TypeScript sources, so the
// two never drift: the built system prompts (prompts/prompts.ts), output
// formats and tool definitions (prompts/formats.ts), the block/outline schemas
// as a plain tree (shared/spec.ts, shared/lab.ts), the menus and the limits.
// Output: server/explainer/generated/*.json and server/tests/fixtures/sample.json (committed).
//
//   node scripts/export-server.mjs           write the files
//   node scripts/export-server.mjs --check   fail if they are out of date
//
// Run it after any change to prompts/, the zod schemas in shared/spec.ts or
// shared/lab.ts, shared/menus.ts, or LIMITS.

import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { createServer } from "vite";

const OUT = "server/explainer/generated";
const check = process.argv.includes("--check");

const vite = await createServer({ configFile: false, root: process.cwd(), logLevel: "error", server: { middlewareMode: true, hmr: false }, appType: "custom", optimizeDeps: { noDiscovery: true } });
const load = (p) => vite.ssrLoadModule(p);

try {
  const prompts = await load("/prompts/prompts.ts");
  const formats = await load("/prompts/formats.ts");
  const spec = await load("/shared/spec.ts");
  const lab = await load("/shared/lab.ts");
  const menus = await load("/shared/menus.ts");
  const validate = await load("/shared/validate.ts");
  const factcheck = await load("/shared/factcheck.ts");
  const sample = await load("/shared/sample.ts");

  // Every exported string of prompts.ts is a prompt.
  const promptStrings = Object.fromEntries(Object.entries(prompts).filter(([, v]) => typeof v === "string").sort(([a], [b]) => a.localeCompare(b)));

  // zod schema -> a plain tree the Python validator and compact reader walk.
  const tree = (s) => {
    const t = s?.def?.type;
    switch (t) {
      case "object": return { t, shape: Object.fromEntries(Object.entries(s.shape).map(([k, v]) => [k, tree(v)])) };
      case "optional": return { t, inner: tree(s.unwrap()) };
      case "array": return { t, element: tree(s.element) };
      case "union": return { t, options: s.options.map(tree) };
      case "number": return { t, int: Boolean(s.isInt) };
      case "enum": return { t, values: s.options };
      case "literal": return { t, values: [...s.def.values] };
      case "string": case "boolean": case "unknown": return { t };
      default: throw new Error(`export-server: unsupported zod type "${t}"`);
    }
  };
  const blocks = Object.fromEntries(Object.entries(spec.BlockSchemas).map(([k, v]) => [k, tree(v)]));
  const schemas = {
    // Block schemas in Block union order (zod tries union options in this order).
    blockOrder: spec.Block.options.map((o) => o.shape.type.def.values[0]),
    promptBlockOrder: spec.PromptBlock.options.map((o) => o.shape.type.def.values[0]),
    blocks,
    outline: tree(spec.Outline),
    promptOutline: tree(spec.PromptOutline),
    design: tree(spec.Design),
    brief: tree(lab.Brief),
    labWritten: lab.LAB_WRITTEN,
  };

  const plain = (o) => JSON.parse(JSON.stringify(o));
  const menuData = plain(Object.fromEntries(Object.entries(menus).filter(([, v]) => typeof v === "object" && v !== null)));
  const constants = {
    LIMITS: spec.LIMITS,
    LITE_DROPPED: [...spec.LITE_DROPPED],
    CONTROL_TYPES: [...spec.CONTROL_TYPES],
    MAX_STAGES: validate.MAX_STAGES,
    WIDGET_MAX_CHARS: validate.WIDGET_MAX_CHARS,
    MAX_CLAIMS: factcheck.MAX_CLAIMS,
  };
  const formatData = plain({ ...formats.FORMATS, computeTool: formats.COMPUTE_TOOL, editTools: formats.EDIT_TOOLS });

  const files = {
    "prompts.json": promptStrings,
    "formats.json": formatData,
    "schemas.json": schemas,
    "menus.json": menuData,
    "constants.json": constants,
  };
  // Test fixture for the server's own tests (server/tests): the sample explainer's outline and design.
  const fixtures = { "../../tests/fixtures/sample.json": { outline: sample.SAMPLE_OUTLINE, design: sample.SAMPLE_DESIGN } };
  Object.assign(files, fixtures);
  mkdirSync("server/tests/fixtures", { recursive: true });
  let stale = [];
  if (!check) mkdirSync(OUT, { recursive: true });
  for (const [name, data] of Object.entries(files)) {
    const text = JSON.stringify(data, null, 1) + "\n";
    const path = join(OUT, name);
    if (check) {
      if (!existsSync(path) || readFileSync(path, "utf8") !== text) stale.push(path);
    } else writeFileSync(path, text);
  }
  if (check && stale.length) {
    console.error(`Out of date: ${stale.join(", ")}. Run npm run export:server.`);
    process.exitCode = 1;
  } else console.log(check ? "Generated server files are up to date." : `Wrote ${Object.keys(files).length} files (${OUT}, server/tests/fixtures).`);
} finally {
  await vite.close();
}
