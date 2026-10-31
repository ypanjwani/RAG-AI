// Playground mode's check: runs shared/playground/validator.cjs (from the ai-playground experiment) on one spec.
// Input: the spec as JSON on stdin. Output: one JSON line {"ok", "errors", "facts"} (facts only when ok).
// The validation runs in a worker thread with a time limit, so a snippet that loops forever is stopped.
// Called by server/explainer/codelab.py (check_playground), which treats a crash or no output as a failure.
import { Worker, isMainThread, parentPort, workerData } from "node:worker_threads";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const TIMEOUT_MS = 5000;

if (isMainThread) {
  let input = "";
  process.stdin.setEncoding("utf8");
  for await (const chunk of process.stdin) input += chunk;
  const print = (r) => process.stdout.write(JSON.stringify(r) + "\n");
  let spec;
  try {
    spec = JSON.parse(input);
  } catch (e) {
    print({ ok: false, errors: [`the playground was not valid JSON: ${e.message}`] });
    process.exit(0);
  }
  const w = new Worker(fileURLToPath(import.meta.url), { workerData: { spec } });
  const timer = setTimeout(() => {
    w.terminate();
    print({ ok: false, errors: ["a snippet ran too long (possible infinite loop); keep loops bounded"] });
  }, TIMEOUT_MS);
  w.once("message", (r) => { clearTimeout(timer); w.terminate(); print(r); });
  w.once("error", (e) => { clearTimeout(timer); print({ ok: false, errors: [`validator error: ${e.message}`] }); });
} else {
  const require = createRequire(import.meta.url);
  const { validate } = require("../shared/playground/validator.cjs");
  let r;
  try {
    r = validate(workerData.spec);
  } catch (e) {
    r = { ok: false, errors: [`validator error: ${e.message}`] };
  }
  parentPort.postMessage(r);
}
