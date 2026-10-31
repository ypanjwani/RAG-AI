// The ChatGPT-style composer: auto-growing textarea, PDF attach (button or
// drag-and-drop), send button. PDFs are parsed in the browser right away so
// errors show before anything is sent.

import { INTERACTIVITY, LEVEL_CHOICES, type Interactivity, type LevelChoice } from "../../../shared/menus";
import { LIMITS, type GenEffort, type Quality } from "../../../shared/spec";
import type { ExtractedPaper } from "../pdf";
import { h } from "../render/dom";
import { isDark } from "./mode";

export interface ComposerSubmit {
  text: string;
  paper?: ExtractedPaper & { fileName: string };
  quality: Quality;
  effort: GenEffort;
  interactivity: Interactivity;
  level: LevelChoice;
  /** Most review rounds after the page, Interactive, Creative and Lab (0 = none; "Until clean" = 10). */
  reviewRounds: number;
  /** Code Lab 2.0, 3.0 and 4.0 only: Haiku thinks before planning each chart (off = cheaper and faster). */
  planThinking: boolean;
  /** Page colours: the explainer opens and exports in this. */
  colorMode: "light" | "dark";
}

/** A browser-remembered choice from a fixed list (first option is the default). */
function stored<T extends string>(key: string, options: readonly T[], fallback: T) {
  return {
    load(): T {
      try { const v = localStorage.getItem(key) as T; return options.includes(v) ? v : fallback; } catch { return fallback; }
    },
    save(v: T) {
      try { localStorage.setItem(key, v); } catch { /* storage blocked */ }
    },
  };
}

/** A small segmented toggle ("Effort  Low | Medium"), remembered per browser. */
function segmented<T extends string>(o: {
  label: string; key: string; fallback: T;
  /** Show the label in front of the buttons (otherwise it's only for screen readers). */
  showLabel?: boolean;
  onChange?: () => void;
  options: readonly (readonly [T, string, string])[];
}) {
  const store = stored(o.key, o.options.map((x) => x[0]), o.fallback);
  let chosen = store.load();
  const btns = o.options.map(([v, label, title]) =>
    h("button.cmp-mode-btn", { type: "button", title, onclick: () => { chosen = v; store.save(v); draw(); o.onChange?.(); } }, label));
  const el = h("div.cmp-mode", { role: "group", "aria-label": o.label }, o.showLabel ? h("span.cmp-mode-label", { "aria-hidden": "true" }, o.label) : null, ...btns);
  function draw() {
    btns.forEach((b, i) => b.setAttribute("aria-pressed", String(o.options[i][0] === chosen)));
  }
  draw();
  return { el, value: () => chosen };
}

/** A dropdown menu ("Interactive ▾"), remembered per browser. Each option shows its name and a one-line description.
 * Keyboard: Enter, Space or the arrow keys open it; arrows move, Enter picks, Escape closes. */
function dropdown<T extends string>(o: {
  label: string; key: string; fallback: T;
  onChange?: () => void;
  options: readonly (readonly [T, string, string])[];
}) {
  const store = stored(o.key, o.options.map((x) => x[0]), o.fallback);
  let chosen = store.load();
  const name = h("span.cmp-pick-name");
  const btn = h("button.cmp-pick-btn", { type: "button", "aria-haspopup": "listbox", "aria-expanded": "false", "aria-label": o.label, title: o.label },
    name, h("span.cmp-pick-chev", { "aria-hidden": "true", html: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>' }));
  const items = o.options.map(([v, label, desc]) => {
    const it = h("div.cmp-pick-opt", { role: "option", tabindex: "-1", "data-value": v },
      h("span.cmp-pick-opt-name", null, label), h("span.cmp-pick-opt-desc", null, desc));
    it.addEventListener("click", () => pick(v));
    return it;
  });
  const menu = h("div.cmp-pick-menu", { role: "listbox", "aria-label": o.label, hidden: true }, ...items);
  const el = h("div.cmp-pick", null, btn, menu);

  function draw() {
    name.textContent = o.options.find((x) => x[0] === chosen)?.[1] ?? "";
    items.forEach((it, i) => it.setAttribute("aria-selected", String(o.options[i][0] === chosen)));
  }
  function pick(v: T) {
    const changed = v !== chosen;
    chosen = v;
    store.save(v);
    draw();
    setOpen(false);
    btn.focus();
    if (changed) o.onChange?.();
  }
  const outside = (e: Event) => { if (!el.contains(e.target as Node)) setOpen(false); };
  const onKey = (e: KeyboardEvent) => {
    const at = (items as HTMLElement[]).indexOf(document.activeElement as HTMLElement);
    if (e.key === "Escape") { e.preventDefault(); setOpen(false); btn.focus(); }
    else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const next = at < 0 ? 0 : (at + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
      items[next].focus();
    } else if ((e.key === "Enter" || e.key === " ") && at >= 0) { e.preventDefault(); pick(o.options[at][0]); }
    else if (e.key === "Tab") setOpen(false);
  };
  function setOpen(open: boolean) {
    menu.hidden = !open;
    btn.setAttribute("aria-expanded", String(open));
    if (open) {
      // Open upward when there's room above inside the scrolling view (the home box sits mid-page), else downward.
      const top = btn.closest(".view")?.getBoundingClientRect().top ?? 0;
      menu.classList.toggle("is-below", btn.getBoundingClientRect().top - top < menu.offsetHeight + 16);
      items[Math.max(0, o.options.findIndex((x) => x[0] === chosen))].focus();
      document.addEventListener("pointerdown", outside);
      document.addEventListener("keydown", onKey);
    } else {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", onKey);
    }
  }
  const isOpen = () => !menu.hasAttribute("hidden");
  btn.addEventListener("click", () => setOpen(!isOpen()));
  btn.addEventListener("keydown", (e) => {
    if (!isOpen() && (e.key === "ArrowDown" || e.key === "ArrowUp")) { e.preventDefault(); setOpen(true); }
  });
  draw();
  return { el, value: () => chosen };
}

export function createComposer(opts: { placeholder: string; autofocus?: boolean; onSubmit(s: ComposerSubmit): void }): HTMLElement {
  let paper: ComposerSubmit["paper"];
  let reading = false;

  const textarea = h("textarea.cmp-input", { rows: 1, placeholder: opts.placeholder, "aria-label": "Describe a concept or add a note", maxlength: LIMITS.conceptChars });
  const file = h("input", { type: "file", accept: "application/pdf,.pdf", hidden: true });
  const attach = h("button.cmp-icon", { type: "button", title: "Attach a research paper (PDF)", "aria-label": "Attach PDF" },
    h("span", { "aria-hidden": "true", html: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>' }));
  const send = h("button.cmp-send", { type: "submit", "aria-label": "Create explainer", title: "Create explainer" },
    h("span", { "aria-hidden": "true", html: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>' }));
  const chips = h("div.cmp-files");
  const error = h("div.cmp-error", { role: "alert" });

  const sync = () => {
    send.disabled = reading || (!paper && textarea.value.trim().length < 3);
    textarea.placeholder = paper ? "Anything to focus on? (optional)" : opts.placeholder;
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 220)}px`;
    textarea.style.overflowY = textarea.scrollHeight > 220 ? "auto" : "hidden";
  };

  function drawChip(status: string, isError = false) {
    chips.replaceChildren();
    if (!paper && !reading) return;
    chips.append(h("div.cmp-file", { class: isError ? "is-error" : "" },
      h("div.cmp-file-icon", { "aria-hidden": "true" }, "PDF"),
      h("div.cmp-file-meta", null, h("div.cmp-file-name", null, paper?.fileName ?? status), h("div.cmp-file-sub", null, paper ? `${paper.pages} pages · ${paper.sections.length} sections · ${Math.round(paper.chars / 1000)}k characters${paper.referencesCut ? ` · references removed (${Math.round(paper.referencesCut / 1000)}k)` : ""}` : "Reading…")),
      h("button.cmp-file-x", { type: "button", "aria-label": "Remove file", onclick: () => { paper = undefined; reading = false; drawChip(""); sync(); } }, "✕")));
  }

  async function takeFile(f: File | undefined) {
    if (!f) return;
    error.textContent = "";
    reading = true;
    paper = undefined;
    drawChip(f.name);
    sync();
    try {
      const { extractPaper } = await import("../pdf"); // pdf.js is large; load on first use
      const p = await extractPaper(f);
      paper = { ...p, fileName: f.name };
    } catch (e) {
      error.textContent = e instanceof Error ? e.message : "Couldn't read that PDF.";
    }
    reading = false;
    drawChip("");
    sync();
    textarea.focus();
  }

  attach.addEventListener("click", () => file.click());
  file.addEventListener("change", () => { takeFile(file.files?.[0]); file.value = ""; });
  textarea.addEventListener("input", sync);
  textarea.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
  });

  // "Deep" was a mode until Oct 2026; it is now Interactive with 3 review rounds.
  try {
    if (localStorage.getItem("quality") === "quality-deep") { localStorage.setItem("quality", "quality-plus"); localStorage.setItem("reviewRounds", "3"); }
  } catch { /* storage blocked */ }
  const mode = dropdown<Quality>({
    // Lab and Code Lab 1.0-6.0 are off the menu (old explainers made with them still open and show their mode).
    // A saved choice of one of them falls back to Interactive.
    label: "Generation mode", key: "quality", fallback: "quality-plus", onChange: () => drawOptions(),
    options: [
      ["quality-plus", "Interactive", "Sliders, charts, quizzes and worked examples, with every section checked for clarity and facts."],
      ["lite-plus", "Quick read", "A short page that is mostly reading, with charts at fixed values. The fastest."],
      ["freeform-plus", "Creative", "Every interactive part is custom-built for the topic. More playful."],
      ["playground", "Playground (beta)", "A deep page where up to 4 sections get a hands-on playground: drag, click, play and watch it react. Tested and reviewed."],
      ["code-lab-7", "Code Lab 7.0 (beta)", "A deep page with interactive parts written from scratch for each section (at most 4), designed freely like Creative's, then tested and reviewed. Costs more."],
    ],
  });
  const effort = segmented<GenEffort>({
    label: "Effort", key: "effort", fallback: "medium", onChange: () => drawOptions(),
    options: [
      ["low", "Low", "Less thinking when planning and building parts: cheaper and faster, pages may be thinner"],
      ["medium", "Medium", "The default amount of thinking for planning and building parts"],
      ["high", "High", "Most thinking when planning the page (text, numbers, quizzes): fewer slips, but slower and costs more. Parts are built as on Medium"],
    ],
  });

  const hands = segmented<Interactivity>({
    label: "How hands-on", key: "interactivity", fallback: "balanced", onChange: () => drawOptions(),
    options: (Object.keys(INTERACTIVITY) as Interactivity[]).map((k) => [k, INTERACTIVITY[k].split(":")[0], INTERACTIVITY[k]] as const),
  });

  // Reader level. Auto lets the server pick it (Quality+/Lite+); the other modes' planner picks it.
  const level = segmented<LevelChoice>({
    label: "Level", key: "level", fallback: "beginner", onChange: () => drawOptions(),
    options: (Object.keys(LEVEL_CHOICES) as LevelChoice[]).map((k) => [k, LEVEL_CHOICES[k].split(":")[0], k === "auto" ? "Auto: chosen from the topic" : LEVEL_CHOICES[k]] as const),
  });

  // Review rounds (not Quick read): after the page, verify -> audit -> improve rounds, at most this many; they stop
  // early once a round finds nothing to fix (server/explainer/deepen.py).
  const rounds = segmented<"0" | "1" | "2" | "3" | "5" | "max">({
    label: "Review rounds", key: "reviewRounds", fallback: "0", onChange: () => drawOptions(),
    options: [
      ["0", "Off", "No extra rounds: the page's usual clarity and fact checks only"],
      ["1", "1", "Up to 1 review round"],
      ["2", "2", "Up to 2 review rounds"],
      ["3", "3", "Up to 3 review rounds"],
      ["5", "5", "Up to 5 review rounds"],
      ["max", "Until clean", "Keep reviewing until a round finds nothing to fix (at most 10 rounds)"],
    ],
  });
  const roundsHint = h("span.cmp-opt-hint", null, "Extra checking after the page");
  const roundsRow = h("div.cmp-opt", null,
    h("div.cmp-opt-head", null, h("span.cmp-opt-label", null, "Review rounds"), roundsHint), rounds.el,
    h("p.cmp-opt-note", null, "Each round checks the page and fixes what it finds, and stops early once it's clean. More rounds: slower, costs more."));
  // Not Quick read (a short static page), nor Code Lab 1-3. Code Lab 4.0, 5.0 and 6.0 can: their interactive parts are kept through each round.
  const isCodeLab = () => mode.value() === "code-lab" || mode.value() === "code-lab-2" || mode.value() === "code-lab-3" || mode.value() === "code-lab-7" || mode.value() === "playground";
  const noRounds = () => mode.value() === "lite-plus" || (isCodeLab() && mode.value() !== "code-lab-7" && mode.value() !== "playground");
  const roundsCount = () => (noRounds() ? 0 : rounds.value() === "max" ? 10 : Number(rounds.value()));

  // Thinking (Code Lab 2.0 to 6.0): whether Haiku thinks before planning each chart or interactive part (2.0-4.0: also repairs and backups;
  // 5.0/6.0: also building, repairing and backing up the widget code, where thinking was ~85-90% of the build cost).
  // Measured Oct 2026: off cost ~40% less and was ~25% faster, with a few more charts rejected by the reviewer.
  const thinking = segmented<"off" | "on">({
    label: "Thinking", key: "planThinking", fallback: "off", onChange: () => drawOptions(),
    options: [
      ["off", "Off", "Interactive parts are planned and built without thinking: cheaper and faster"],
      ["on", "On", "Haiku thinks before planning and building each interactive part: costs more and is slower"],
    ],
  });
  const thinkingHint = h("span.cmp-opt-hint", null, "Think before planning and building interactive parts");
  const thinkingRow = h("div.cmp-opt", null,
    h("div.cmp-opt-head", null, h("span.cmp-opt-label", null, "Thinking"), thinkingHint), thinking.el,
    h("p.cmp-opt-note", null, "Off costs about 40% less and is faster. On can keep a few more charts."));
  const isChartLab = () => mode.value() === "code-lab-2" || mode.value() === "code-lab-3" || mode.value() === "code-lab-7" || mode.value() === "playground";

  // Page colours: stamped on the page, so it opens and exports in it. First time = what the app shows now.
  const colors = segmented<"light" | "dark">({
    label: "Page colours", key: "pageMode", fallback: isDark() ? "dark" : "light",
    options: [
      ["light", "Light", "A light page, in the app and in the exported file"],
      ["dark", "Dark", "A dark page, in the app and in the exported file"],
    ],
  });

  // Level, how hands-on and effort live in a small popover behind "Options"; a dot shows when any is changed.
  const optRow = (label: string, hint: string, el: HTMLElement) => h("div.cmp-opt", null, h("div.cmp-opt-head", null, h("span.cmp-opt-label", null, label), h("span.cmp-opt-hint", null, hint)), el);
  const pop = h("div.cmp-pop", { role: "dialog", "aria-label": "Options", hidden: true },
    optRow("Level", "Who the page is written for", level.el),
    optRow("How hands-on", "Reading vs. things to play with", hands.el),
    optRow("Effort", "More thinking: slower, fewer slips", effort.el),
    thinkingRow,
    roundsRow,
    optRow("Page colours", "Kept when you export", colors.el));
  const optBtn = h("button.cmp-opts-btn", { type: "button", "aria-expanded": "false", "aria-haspopup": "dialog", title: "Level, how hands-on, effort" },
    h("span", { "aria-hidden": "true", html: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/></svg>' }),
    h("span", null, "Options"), h("span.cmp-opts-dot", { "aria-hidden": "true" }));
  function drawOptions() {
    optBtn.classList.toggle("is-changed", level.value() !== "beginner" || hands.value() !== "balanced" || effort.value() !== "medium" || roundsCount() > 0
      || (isChartLab() && thinking.value() === "on"));
    thinkingRow.classList.toggle("is-off", !isChartLab());
    thinkingHint.textContent = isChartLab() ? "Think before planning and building interactive parts" : "Only for Code Lab 2.0–7.0 and Playground";
    roundsRow.classList.toggle("is-off", noRounds());
    roundsHint.textContent = noRounds() ? `Not for ${isCodeLab() ? "Code Lab 1–3" : "Quick read"}` : "Extra checking after the page";
  }
  const outside = (e: Event) => { if (!pop.contains(e.target as Node) && !optBtn.contains(e.target as Node)) setPop(false); };
  const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") { setPop(false); optBtn.focus(); } };
  function setPop(open: boolean) {
    pop.hidden = !open;
    optBtn.setAttribute("aria-expanded", String(open));
    // Open upward when there's room above the button inside the scrolling view (the home box sits mid-page), else downward.
    if (open) {
      const top = optBtn.closest(".view")?.getBoundingClientRect().top ?? 0;
      pop.classList.toggle("is-below", optBtn.getBoundingClientRect().top - top < pop.offsetHeight + 16);
    }
    if (open) { document.addEventListener("pointerdown", outside); document.addEventListener("keydown", onKey); }
    else { document.removeEventListener("pointerdown", outside); document.removeEventListener("keydown", onKey); }
  }
  optBtn.addEventListener("click", () => setPop(optBtn.getAttribute("aria-expanded") !== "true"));
  drawOptions();

  const form = h("form.composer", { novalidate: true }, chips, textarea, h("div.cmp-row", null, attach, mode.el, h("div.cmp-opts", null, optBtn, pop), send), file);
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (send.disabled) return;
    opts.onSubmit({ text: textarea.value.trim(), paper, quality: mode.value(), effort: effort.value(), interactivity: hands.value(), level: level.value(), reviewRounds: roundsCount(),
      planThinking: isChartLab() && thinking.value() === "on", colorMode: colors.value() });
    paper = undefined;
    textarea.value = "";
    drawChip("");
    sync();
  });
  form.addEventListener("dragover", (e) => { e.preventDefault(); form.classList.add("is-drag"); });
  form.addEventListener("dragleave", () => form.classList.remove("is-drag"));
  form.addEventListener("drop", (e) => { e.preventDefault(); form.classList.remove("is-drag"); takeFile(e.dataTransfer?.files[0]); });

  const wrap = h("div.cmp-wrap", null, form, error);
  /** Let suggestion chips fill the box. */
  (wrap as HTMLElement & { fill?: (t: string) => void }).fill = (t: string) => { textarea.value = t; sync(); textarea.focus(); };
  queueMicrotask(() => { sync(); if (opts.autofocus) textarea.focus(); });
  return wrap;
}
