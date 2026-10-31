// Text-ish blocks: text, callout, formula, tabs, compare.

import { h, katexHtml, md, mdInline } from "../dom";
import type { Renderer } from "./types";

const CALLOUT_ICON = { info: "ℹ️", analogy: "💡", warning: "⚠️", key: "🔑" } as const;
const CALLOUT_LABEL = { info: "Note", analogy: "Analogy", warning: "Common mistake", key: "Key idea" } as const;

export const text: Renderer<"text"> = (b) => md(b.md, "md x-text");

export const callout: Renderer<"callout"> = (b) =>
  h("aside.x-callout", { "data-variant": b.variant },
    h("div.x-callout-icon", { "aria-hidden": "true" }, CALLOUT_ICON[b.variant]),
    h("div", null,
      h("div.x-callout-title", null, b.title || CALLOUT_LABEL[b.variant]),
      md(b.md)));

export const formula: Renderer<"formula"> = (b, ctx) => {
  const deeper = ctx.deeper
    ? h("button.x-btn.x-btn-soft.x-btn-sm.x-formula-deeper", { type: "button", "aria-haspopup": "dialog", title: "What each term means, where this formula comes from, and why it looks like this" }, "Go deeper →")
    : null;
  if (deeper) deeper.addEventListener("click", () => ctx.deeper!(deeper));
  return h("figure.x-formula", null,
    h("div.x-formula-math", { html: katexHtml(b.latex, true) }),
    b.caption ? h("figcaption", null, b.caption) : null,
    b.terms.length
      ? h("dl.x-formula-terms", null, ...b.terms.flatMap((t) => [
          h("dt", { html: katexHtml(t.symbol, false), "data-link-var": t.var }),
          h("dd", { "data-link-var": t.var }, t.meaning, t.var ? h("span.x-link-hint", { title: "Linked to a slider" }, " ↔") : null),
        ]))
      : null,
    deeper ? h("div.x-formula-foot", null, deeper) : null);
};

export const tabs: Renderer<"tabs"> = (b, ctx) => {
  const id = ctx.key.replace(/[^a-z0-9]/gi, "-");
  const panel = h("div.x-tab-panel", { role: "tabpanel", id: `${id}-panel` });
  const buttons = b.tabs.map((t, i) =>
    h("button.x-tab", { role: "tab", type: "button", onclick: () => select(i) }, t.label));
  function select(i: number) {
    buttons.forEach((btn, j) => btn.setAttribute("aria-selected", String(i === j)));
    panel.replaceChildren(md(b.tabs[i].md));
  }
  select(0);
  return h("div.x-tabs", null, h("div.x-tab-list", { role: "tablist" }, ...buttons), panel);
};

export const compare: Renderer<"compare"> = (b) => {
  const side = (s: typeof b.left, cls: string) =>
    h(`div.x-compare-side.${cls}` as "div", null,
      h("h4", null, s.title),
      h("ul", null, ...s.points.map((p) => h("li", null, mdInline(p)))));
  return h("div.x-compare", null, side(b.left, "is-a"), h("div.x-compare-vs", { "aria-hidden": "true" }, "vs"), side(b.right, "is-b"));
};
