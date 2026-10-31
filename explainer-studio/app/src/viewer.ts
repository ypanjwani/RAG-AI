// Entry for exported explainers: renders window.__SPEC__ with hash routing
// (#/<stageId>), keeps reading progress in localStorage when available.

import type { ExplainerSpec } from "../../shared/spec";
import { renderExplainer, type Progress } from "./render/explainer";
import { injectExplainerStyles } from "./styles";

declare global { interface Window { __SPEC__?: ExplainerSpec } }

const spec = window.__SPEC__!;
const KEY = `explainer-progress:${spec.title}`;

function load(): Progress {
  try {
    const p = JSON.parse(localStorage.getItem(KEY) ?? "");
    if (Array.isArray(p.visited) && p.answers) return p;
  } catch { /* storage blocked or empty */ }
  return { visited: [], answers: {} };
}

let progress = load();
let dispose: (() => void) | null = null;
injectExplainerStyles();
document.documentElement.classList.add("standalone");
// Light/dark chosen when the explainer was created (custom mode); otherwise follow the OS.
if (spec.design.mode === "light" || spec.design.mode === "dark") document.documentElement.dataset.mode = spec.design.mode;

function render() {
  dispose?.();
  const stageId = location.hash.replace(/^#\/?/, "") || undefined;
  dispose = renderExplainer(document.getElementById("root")!, {
    spec,
    progress,
    stageId,
    href: (id) => (id ? `#/${id}` : "#/"),
    onProgress: (p) => {
      progress = p;
      try { localStorage.setItem(KEY, JSON.stringify(p)); } catch { /* ignore */ }
    },
  });
}
window.addEventListener("hashchange", render);
render();
