// Small DOM helpers shared by the shell and the renderer.

import DOMPurify from "dompurify";
import katex from "katex";
import { marked } from "marked";

type Child = Node | string | number | null | undefined | false;
type Attrs = Record<string, string | number | boolean | null | undefined | EventListener>;

/** h("div.card", {onclick: fn, "aria-label": "x"}, child, ...) */
export function h<K extends keyof HTMLElementTagNameMap>(tag: K | `${K}.${string}`, attrs?: Attrs | null, ...children: Child[]): HTMLElementTagNameMap[K] {
  const [name, ...classes] = tag.split(".");
  const el = document.createElement(name) as HTMLElementTagNameMap[K];
  if (classes.length) el.className = classes.join(" ");
  for (const [k, v] of Object.entries(attrs ?? {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v as EventListener);
    else if (k === "class") el.className = [el.className, v].filter(Boolean).join(" ");
    else if (k === "html") el.innerHTML = String(v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  append(el, ...children);
  return el;
}

export function append(el: Node, ...children: Child[]) {
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    el.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
  }
}

const SVG_NS = "http://www.w3.org/2000/svg";
export function svg(tag: string, attrs: Record<string, string | number> = {}, ...children: (SVGElement | string)[]): SVGElement {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
  for (const c of children) el.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  return el;
}

export function katexHtml(tex: string, display: boolean): string {
  try {
    return katex.renderToString(tex, { displayMode: display, throwOnError: false, trust: false, strict: "ignore" });
  } catch {
    return `<code>${escapeHtml(tex)}</code>`;
  }
}

export function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}

// Two marks beyond Markdown (taught in WRITING_RULES, prompts/prompts.ts):
// ==text== highlights a key sentence, [[term]] colours a key term. Styled per
// page in explainer.css (the brief's emphasis style may turn either into bold).
marked.use({
  extensions: [
    {
      name: "highlight", level: "inline",
      start: (src: string) => src.indexOf("=="),
      tokenizer(src: string) {
        const m = /^==(?=\S)([^\n]*?\S)==(?!=)/.exec(src);
        if (m) return { type: "highlight", raw: m[0], tokens: this.lexer.inlineTokens(m[1]) };
      },
      renderer(t) { return `<mark class="x-hl">${this.parser.parseInline(t.tokens ?? [])}</mark>`; },
    },
    {
      name: "keyterm", level: "inline",
      start: (src: string) => src.indexOf("[["),
      tokenizer(src: string) {
        const m = /^\[\[([^[\]\n]+)\]\]/.exec(src);
        if (m) return { type: "keyterm", raw: m[0], tokens: this.lexer.inlineTokens(m[1]) };
      },
      renderer(t) { return `<span class="x-key">${this.parser.parseInline(t.tokens ?? [])}</span>`; },
    },
  ],
});

/**
 * AI/model Markdown -> safe HTML. Math is cut out first ($$..$$, $..$) and
 * rendered by KaTeX after sanitizing, so DOMPurify never sees KaTeX markup and
 * Markdown never mangles TeX. Placeholders use a private-use character: NUL
 * was dropped by DOMPurify's HTML parse whenever the output contained tags.
 */
export function mdToHtml(src: string, inline = false): string {
  const math: string[] = [];
  const stash = (tex: string, display: boolean) => {
    math.push(katexHtml(tex, display));
    return `M${math.length - 1}`;
  };
  let text = src.replace(/\$\$([\s\S]+?)\$\$/g, (_, t) => stash(t, true));
  text = text.replace(/(^|[^\\$])\$([^\s$](?:[^$\n]*?[^\s$])?)\$(?!\d)/g, (_, pre, t) => pre + stash(t, false));
  const raw = inline ? (marked.parseInline(text, { async: false }) as string) : (marked.parse(text, { async: false, breaks: true }) as string);
  const clean = DOMPurify.sanitize(raw, { FORBID_TAGS: ["style", "img", "iframe", "form", "input"], FORBID_ATTR: ["style"] });
  return clean.replace(/M(\d+)/g, (_, i) => math[+i] ?? "");
}

export function md(src: string, cls = "md"): HTMLElement {
  return h("div", { class: cls, html: mdToHtml(src) });
}

export function mdInline(src: string): HTMLSpanElement {
  return h("span", { html: mdToHtml(src, true) });
}

export function fmt(n: number, digits = 2): string {
  if (!Number.isFinite(n)) return "—";
  const abs = Math.abs(n);
  if (abs >= 1e6 || (abs > 0 && abs < 10 ** -digits)) return n.toExponential(Math.max(1, digits));
  const s = n.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: 0 });
  // A tiny negative (or -0) rounds to "-0": show plain 0.
  return /^-0(?:[.,]0*)?$/.test(s) ? s.slice(1) : s;
}

/** Short axis labels: 15M, 2.5k, 0.001 (exponents only for tiny values). */
export function fmtTick(n: number): string {
  if (!Number.isFinite(n)) return "";
  const abs = Math.abs(n);
  if (abs >= 1e4) return new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(n);
  if (abs > 0 && abs < 1e-3) return n.toExponential(0);
  return fmt(n, 3);
}

export function uid(): string {
  return Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
}

/** Stable small hash, used for illustrative token IDs and colors. */
export function hash(s: string): number {
  let x = 2166136261;
  for (let i = 0; i < s.length; i++) x = Math.imul(x ^ s.charCodeAt(i), 16777619);
  return x >>> 0;
}
