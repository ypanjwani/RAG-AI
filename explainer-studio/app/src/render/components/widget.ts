// Freeform mode: an interactive widget whose HTML/CSS/JS was written by the
// AI. It is the one place AI-written code runs, so it is fenced in:
// - sandbox="allow-scripts" without allow-same-origin: an opaque origin, so no
//   access to the app's DOM, storage, cookies or history;
// - a CSP with default-src 'none': no network, no external scripts or fonts;
// - only two messages cross the boundary: the widget's height (out) and the
//   theme colours (in), and incoming messages are accepted only from its frame.

import type { Renderer } from "./types";
import { h } from "../dom";
import { watchTheme } from "../theme";
// Helpers (window.XW: chart axes, labelled sliders, legends, readouts) that Code Lab widgets may use; shared with the jsdom check.
import kitJs from "../../../../shared/widget-kit.js?raw";

// Code Lab 2.0's charts: Vega and Vega-Lite go into a frame only when its widget calls XW.vega(...), loaded on first use
// (vega-src.ts).
const USES_VEGA = /\bXW\.vega\s*\(/;
let vegaScripts: Promise<string> | null = null;
const loadVega = () => (vegaScripts ??= import("./vega-src").then((m) => m.VEGA_SOURCES.map(inlineScript).join("")));
// Playground mode's playgrounds (XW.playground(spec)): the playground runtime goes into a frame only when its widget
// calls it, loaded on first use (playground-src.ts). It goes after the widget kit, which replaces window.XW.
const USES_PLAYGROUND = /\bXW\.playground\s*\(/;
let playgroundScripts: Promise<string> | null = null;
const loadPlayground = () => (playgroundScripts ??= import("./playground-src").then((m) =>
  `<style>${m.PLAYGROUND_CSS}</style>` + m.PLAYGROUND_SOURCES.map(inlineScript).join("")));
/** Inline script text can't contain "</script" (it would end the tag early). */
const inlineScript = (js: string) => `<script>${js.replace(/<\/script/gi, "<\\/script")}</script>`;

/** Theme variables handed to the widget so it matches the page (and its light/dark mode). */
const THEME_VARS = [
  "--bg", "--surface", "--surface-2", "--text", "--text-2", "--muted", "--border",
  "--accent", "--accent-2", "--accent-soft", "--accent-text", "--on-accent",
  "--cat-0", "--cat-1", "--cat-2", "--cat-3", "--cat-4", "--cat-5", "--neg", "--pos",
  "--font-body", "--font-mono", "--radius-sm",
];
const MIN_H = 80, MAX_H = 1200;

function themeOf(el: Element): Record<string, string> {
  const cs = getComputedStyle(el);
  return Object.fromEntries(THEME_VARS.map((v) => [v, cs.getPropertyValue(v).trim()]).filter(([, val]) => val));
}

/** late: scripts that go after the widget kit (the playground runtime). evalOk: the playground runtime compiles its
 * spec's snippets with new Function, so its frames allow eval. That adds no power: the frame already runs AI-written
 * inline scripts, with an opaque origin and no network (default-src 'none'). */
function srcdoc(html: string, theme: Record<string, string>, scheme: string, libs = "", late = "", evalOk = false): string {
  const vars = Object.entries(theme).map(([k, v]) => `${k}:${v};`).join("");
  return `<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'${evalOk ? " 'unsafe-eval'" : ""}; style-src 'unsafe-inline'; img-src data: blob:">
<style>
:root{${vars}color-scheme:${scheme}}
html,body{margin:0;padding:0;background:transparent;color:var(--text);font-family:var(--font-body),system-ui,sans-serif;font-size:15px;line-height:1.5}
body{padding:2px}
*,*::before,*::after{box-sizing:border-box}
input,button,select{font:inherit;color:inherit;accent-color:var(--accent)}
button{cursor:pointer}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
svg,canvas{max-width:100%}
</style>
${libs}
<script>${kitJs}</script>
<script>(function(){var s=document.createElement("style");s.textContent=window.XW_CSS||"";document.head.appendChild(s)})();</script>
${late}
<script>
(function(){
  // Content height (not documentElement.scrollHeight, which never drops below the frame's own height).
  var post=function(){if(document.body)parent.postMessage({type:"widget-height",h:Math.ceil(document.body.getBoundingClientRect().bottom)+4},"*")};
  var soon=function(){setTimeout(post,0)};
  // ResizeObserver can pause when the page isn't rendering, so also measure on load, after interactions and a few times early on.
  addEventListener("DOMContentLoaded",function(){new ResizeObserver(post).observe(document.body);post()});
  addEventListener("load",post);
  ["input","change","click","keyup"].forEach(function(t){addEventListener(t,soon,true)});
  [100,500,1500].forEach(function(ms){setTimeout(post,ms)});
  addEventListener("message",function(e){
    var d=e.data;if(!d||d.type!=="widget-theme")return;
    for(var k in d.vars)document.documentElement.style.setProperty(k,d.vars[k]);
    document.documentElement.style.colorScheme=d.scheme;
  });
})();
</script>
</head><body>
${html}
</body></html>`;
}

export const widget: Renderer<"widget"> = (b, ctx) => {
  const frame = h("iframe.x-widget-frame", {
    sandbox: "allow-scripts",
    title: b.title || "Interactive widget",
    referrerpolicy: "no-referrer",
  });
  frame.style.height = "320px";
  const fig = h("figure.x-widget", null,
    h("div.x-chart-head", null,
      h("figcaption.x-chart-title", null, b.title),
      h("span.x-badge", { title: "This interactive part was written by AI and runs in a sandbox" }, "AI-built")),
    frame);

  const scheme = () => (getComputedStyle(document.documentElement).colorScheme.includes("dark") ? "dark" : "light");

  // Height reports from this widget only.
  const onMessage = (e: MessageEvent) => {
    if (e.source !== frame.contentWindow || e.data?.type !== "widget-height") return;
    const hgt = Number(e.data.h);
    if (Number.isFinite(hgt)) frame.style.height = `${Math.min(MAX_H, Math.max(MIN_H, hgt))}px`;
  };
  window.addEventListener("message", onMessage);

  // Keep colours in step with the page's light/dark mode.
  const pushTheme = () => frame.contentWindow?.postMessage({ type: "widget-theme", vars: themeOf(fig), scheme: scheme() }, "*");
  const stopTheme = watchTheme(pushTheme);
  ctx.onDispose(() => {
    window.removeEventListener("message", onMessage);
    stopTheme();
  });

  // The theme is only readable once the figure is in the page.
  setTimeout(() => {
    const show = (libs = "") => { frame.srcdoc = srcdoc(b.html, themeOf(fig), scheme(), libs); };
    if (USES_PLAYGROUND.test(b.html)) {
      const late = (rt = "") => { frame.srcdoc = srcdoc(b.html, themeOf(fig), scheme(), "", rt, true); };
      loadPlayground().then(late, () => late());  // without the runtime, XW.playground is missing and the frame stays empty
    } else if (USES_VEGA.test(b.html)) loadVega().then(show, () => show());  // without the libraries the widget says so itself
    else show();
  }, 0);
  return fig;
};
