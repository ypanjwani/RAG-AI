// "What do you want to add to each section?": the question the conversation asks after a Quality request,
// before anything is planned. The reader ticks any of the options (remembered for next time) and confirms.
// "Let this mode decide" and "None" can't be combined with the others.

import { EXTRAS, type Extra } from "../../../shared/menus";
import { h } from "../render/dom";

const KEY = "extras";
const KEYS = Object.keys(EXTRAS) as Extra[];
const label = (k: Extra) => EXTRAS[k].split(":")[0];

/** The reader's last answer (first time: worked examples, as pages had before the question). */
export function lastExtras(): Extra[] {
  try {
    const v = JSON.parse(localStorage.getItem(KEY) ?? "null");
    if (Array.isArray(v)) return KEYS.filter((k) => v.includes(k));
  } catch { /* storage blocked or bad value */ }
  return ["examples"];
}

/** A short line for the conversation once answered. */
export function extrasSummary(extras: Extra[]): string {
  return extras.length ? `Added to each section: ${extras.map(label).join(", ")}` : "Nothing added to the sections";
}

export function extrasQuestion(onConfirm: (extras: Extra[]) => void): HTMLElement {
  let chosen = lastExtras();
  const boxes = new Map<Extra | "none", HTMLInputElement>();
  const option = (k: Extra | "none", text: string, tip: string) => {
    const input = h("input", { type: "checkbox" });
    input.addEventListener("change", () => toggle(k, input.checked));
    boxes.set(k, input);
    return h("label.msg-extras-opt", null, input, h("span", null, h("strong", null, text), h("span.msg-extras-tip", null, tip)));
  };
  function toggle(k: Extra | "none", on: boolean) {
    if (k === "none") chosen = [];
    else if (k === "auto") chosen = on ? ["auto"] : [];
    else chosen = on ? [...chosen.filter((x) => x !== "auto"), k] : chosen.filter((x) => x !== k);
    chosen = KEYS.filter((x) => chosen.includes(x));
    draw();
  }
  function draw() {
    for (const [k, b] of boxes) b.checked = k === "none" ? !chosen.length : chosen.includes(k);
  }
  const go = h("button.btn-pill.is-primary", { type: "button" }, "✨ Create explainer");
  go.addEventListener("click", () => {
    try { localStorage.setItem(KEY, JSON.stringify(chosen)); } catch { /* storage blocked */ }
    go.disabled = true;
    onConfirm(chosen);
  });
  const el = h("div.msg-extras", { role: "group", "aria-label": "What do you want to add to each section?" },
    ...KEYS.map((k) => option(k, label(k), EXTRAS[k].slice(label(k).length + 2))),
    option("none", "None", "Just the explainer, with no worked examples"),
    h("div.msg-extras-go", null, go));
  draw();
  return el;
}
