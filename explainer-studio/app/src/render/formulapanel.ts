// "Go deeper" on a formula: a side panel that explains every term, where the formula comes from, why it looks like
// this, a tiny worked example and a common misreading (shared/spec.ts FormulaNote). A saved note opens at once (also in
// exports); otherwise the app's `load` writes it on the first click (POST /api/formula) and saves it on the page.

import type { FormulaNote } from "../../../shared/spec";
import { h, katexHtml, md, mdInline } from "./dom";
import { openSidePanel } from "./sidepanel";

export function openFormulaPanel(o: {
  host: HTMLElement;
  trigger: HTMLElement;
  latex: string;
  caption: string;
  note?: FormulaNote;
  load?(signal: AbortSignal): Promise<FormulaNote>;
}): () => void {
  let ctrl: AbortController | null = null;
  const status = h("div.x-sec-status", { role: "status", "aria-live": "polite" });
  const retry = h("button.x-btn.x-btn-primary.x-btn-sm", { type: "button", hidden: true }, "Try again");
  const panel = openSidePanel({
    host: o.host, trigger: o.trigger, title: "Go deeper", sub: o.caption || "This formula",
    foot: o.note ? undefined : h("div.x-sec-foot", null, status, retry),
    onClose: () => ctrl?.abort(),
  });
  panel.list.parentElement?.classList.add("x-fd-panel");
  const head = h("div.x-fd-formula", { html: katexHtml(o.latex, true) });

  const draw = (n: FormulaNote) => {
    panel.list.replaceChildren(head, ...noteParts(n));
    status.parentElement?.remove();  // the footer only holds the loading state
  };

  async function load() {
    if (!o.load) return;
    ctrl?.abort();
    ctrl = new AbortController();
    const signal = ctrl.signal;
    retry.hidden = true;
    status.classList.remove("is-error");
    status.replaceChildren(h("span.x-sec-spinner", { "aria-hidden": "true" }), "Working through this formula… (about 10–20 seconds)");
    panel.list.replaceChildren(head);
    try {
      const n = await o.load(signal);
      if (!signal.aborted) draw(n);
    } catch (e) {
      if (signal.aborted) return;
      status.classList.add("is-error");
      status.textContent = e instanceof Error ? e.message : "Something went wrong. Please try again.";
      retry.hidden = false;
    }
  }

  retry.addEventListener("click", () => load());
  if (o.note) draw(o.note);
  else load();
  return () => panel.close(false);
}

/** The note's sections, in reading order: the idea, each term, where it comes from, why this shape, an example, a pitfall. */
function noteParts(n: FormulaNote): HTMLElement[] {
  const part = (label: string, ...body: (Node | null)[]) =>
    h("section.x-ex-item.x-fd-part", null, h("div.x-ex-num", null, label), ...body);
  const steps = (items: string[]) => h("ol.x-fd-steps", null, ...items.map((s) => h("li", null, md(s, "md x-sec-body"))));
  return [
    h("div.x-fd-idea", null, md(n.idea, "md")),
    part("Every term",
      h("dl.x-fd-terms", null, ...n.terms.flatMap((t) => [
        h("dt", null, h("span.x-fd-sym", { html: katexHtml(t.symbol, false) }), t.name ? h("span.x-fd-name", null, t.name) : null),
        h("dd", null, md(t.meaning, "md x-sec-body"), t.role ? h("div.x-fd-role", null, h("strong", null, "Why it's there: "), mdInline(t.role)) : null),
      ]))),
    part("Where it comes from", steps(n.origin)),
    n.shape ? part("Why it looks like this", md(n.shape, "md x-sec-body")) : null,
    n.example.length ? part("Try it with numbers", steps(n.example)) : null,
    n.pitfall ? h("div.x-fd-pitfall", null, h("strong", null, "⚠️ Common mix-up: "), mdInline(n.pitfall)) : null,
  ].filter((e): e is HTMLElement => !!e);
}
