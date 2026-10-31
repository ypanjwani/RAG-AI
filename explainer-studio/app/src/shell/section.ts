// "Search the web" on a section: the side panel its button opens (app only, never in exports). Nothing is
// requested until the reader clicks; answers are saved on the record, so reopening is free, and "Search again"
// sends the earlier answers along so the next one covers something new.

import { LIMITS, type ExplainerSpec, type SectionResponse } from "../../../shared/spec";
import { h, md } from "../render/dom";
import { openSidePanel } from "../render/sidepanel";

/** A saved answer (newest last). */
export interface SectionNote extends SectionResponse { at: number }

/** A section's prose (blurb, text, callouts, formulas), as sent to the server. */
export function sectionText(spec: ExplainerSpec, i: number): string {
  const st = spec.stages[i];
  const parts: string[] = [st.blurb];
  for (const b of st.blocks) {
    if (b.type === "text") parts.push(b.md);
    else if (b.type === "callout") parts.push(b.title ? `${b.title}: ${b.md}` : b.md);
    else if (b.type === "formula") parts.push(`$$${b.latex}$$${b.caption ? ` ${b.caption}` : ""}`);
  }
  return parts.filter(Boolean).join("\n\n").slice(0, LIMITS.sectionChars);
}

/** The latest earlier answers that fit the server's limit, oldest first. */
function previousOf(notes: SectionNote[]): string[] {
  const out: string[] = [];
  let total = 0;
  for (const n of [...notes].reverse()) {
    if (total + n.md.length > LIMITS.sectionPrevious) break;
    out.unshift(n.md);
    total += n.md.length;
  }
  return out;
}

export function openSearchPanel(o: {
  host: HTMLElement;
  trigger: HTMLElement;
  stageLabel: string;
  notes(): SectionNote[];
  run(previous: string[], signal: AbortSignal): Promise<SectionResponse>;
  save(n: SectionNote): void;
}): () => void {
  let ctrl: AbortController | null = null;
  const again = h("button.x-btn.x-btn-primary.x-btn-sm", { type: "button" }, "🔎 Search again");
  const status = h("div.x-sec-status", { role: "status", "aria-live": "polite" });
  const panel = openSidePanel({
    host: o.host, trigger: o.trigger, title: "From the web", sub: o.stageLabel,
    foot: h("div.x-sec-foot", null, status, again),
    onClose: () => ctrl?.abort(),
  });

  const item = (n: SectionNote, i: number) => {
    const body = md(n.md, "md x-sec-body");
    body.querySelectorAll("a").forEach((a) => { a.target = "_blank"; a.rel = "noopener noreferrer"; });
    return h("section.x-ex-item", null,
      h("div.x-ex-num", null, `Search ${i + 1}`),
      body,
      n.sources.length
        ? h("ol.x-sec-sources", null, ...n.sources.map((s) =>
            h("li", null, h("a", { href: s.url, target: "_blank", rel: "noopener noreferrer" }, s.title), h("span.x-sec-host", null, ` ${hostOf(s.url)}`))))
        : null);
  };
  const draw = () => panel.list.replaceChildren(...o.notes().map(item).reverse()); // newest on top

  async function ask() {
    ctrl?.abort();
    ctrl = new AbortController();
    const signal = ctrl.signal;
    again.disabled = true;
    status.classList.remove("is-error");
    status.replaceChildren(h("span.x-sec-spinner", { "aria-hidden": "true" }), "Searching the web…");
    try {
      const r = await o.run(previousOf(o.notes()), signal);
      if (signal.aborted) return;
      o.save({ ...r, at: Date.now() });
      draw();
      panel.list.scrollTop = 0;
      status.replaceChildren();
    } catch (e) {
      if (signal.aborted) return;
      status.classList.add("is-error");
      status.textContent = e instanceof Error ? e.message : "Something went wrong. Please try again.";
    }
    again.disabled = false;
  }

  again.addEventListener("click", () => ask());
  draw();
  if (!o.notes().length) ask();
  return () => panel.close(false);
}

function hostOf(url: string): string {
  try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return ""; }
}
