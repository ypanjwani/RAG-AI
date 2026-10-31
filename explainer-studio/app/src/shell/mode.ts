// Light/dark mode for the app shell: "auto" follows the OS; the choice is remembered per browser.

import { h } from "../render/dom";

export type Mode = "auto" | "light" | "dark";

export function loadMode(): Mode {
  try { return (localStorage.getItem("mode") as Mode) || "auto"; } catch { return "auto"; }
}

/** Show a mode without remembering it (an explainer opened in its own light/dark). */
export function setRootMode(m: Mode) {
  if (m === "auto") delete document.documentElement.dataset.mode;
  else document.documentElement.dataset.mode = m;
}

export function applyMode(m: Mode) {
  setRootMode(m);
  try { localStorage.setItem("mode", m); } catch { /* storage blocked */ }
}

/** Whether the page is dark right now (a forced mode, or the OS setting). */
export function isDark(): boolean {
  const m = loadMode();
  return m === "auto" ? window.matchMedia("(prefers-color-scheme: dark)").matches : m === "dark";
}

/** Auto | Light | Dark, for the sidebar. */
export function modeToggle(): HTMLElement {
  const modes: [Mode, string][] = [["auto", "Auto"], ["light", "Light"], ["dark", "Dark"]];
  const wrap = h("div.mode-toggle", { role: "group", "aria-label": "Colour mode" });
  const draw = () => wrap.replaceChildren(...modes.map(([m, label]) =>
    h("button", { type: "button", "aria-pressed": String(loadMode() === m), onclick: () => { applyMode(m); draw(); } }, label)));
  draw();
  return wrap;
}

const SUN = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
const MOON = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>';

/** One button that flips between light and dark (the sign-in page). */
export function modeButton(): HTMLElement {
  const btn = h("button.theme-btn", { type: "button" });
  const draw = () => {
    btn.innerHTML = isDark() ? SUN : MOON;
    btn.setAttribute("aria-label", isDark() ? "Switch to light mode" : "Switch to dark mode");
  };
  btn.addEventListener("click", () => { applyMode(isDark() ? "light" : "dark"); draw(); });
  draw();
  return btn;
}
