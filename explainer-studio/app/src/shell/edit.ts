// Edit chat: a chat panel (the drawer's ✏️ Edit tab) for changing one part of a
// finished explainer. The reader clicks ✏️ Edit on a part and types what they
// want. Each message goes to Haiku (POST /api/edit), which answers and calls
// tools; the tools run here, in code (shared/edit.ts), and every change goes
// through checkEdit, the checker generation uses, so a change can never break
// the page. When a request is unclear Haiku asks back with clickable answers;
// it can also show graph previews or a settings form for the reader to use.

import { derivePalette } from "../../../shared/color";
import { addPreset, applyChanges, blockName, conversions, editContext, fieldLabel, getAt, humanize, itemName, leaves, listEditable, listOp, presetVars, setAt, suggestions, textChanges, type Leaf, type ListOp, type Path } from "../../../shared/edit";
import type { Block, EditContent, EditMessage, ExplainerSpec } from "../../../shared/spec";
import { checkEdit } from "../../../shared/validate";
import { editChat } from "../api";
import { renderBlock } from "../render/components";
import { h, md } from "../render/dom";
import { Store } from "../render/engine";

export interface EditTarget { stage: number; block: number }

export interface EditHost {
  spec(): ExplainerSpec;
  /** Persist and re-render. structural: parts were added, removed or moved (saved quiz answers in that stage are reset). */
  apply(next: ExplainerSpec, focus: EditTarget | null, structural: boolean): void;
  undo(): boolean;
  canUndo(): boolean;
  restoreOriginal(): boolean;
  hasOriginal(): boolean;
  /** Current value of a page variable (slider positions). */
  value(name: string): number;
  /** Highlight a part on the page (null clears). */
  highlight(t: EditTarget | null): void;
}

/** Tool-call rounds per message before the chat stops and says so. */
const MAX_ROUNDS = 6;
const isControl = (b: Block) => b.type === "slider" || b.type === "choice" || b.type === "toggle";
type Result = { content: string; is_error?: boolean };

export function createEditPanel(host: EditHost) {
  let target: EditTarget | null = null;
  let messages: EditMessage[] = [];
  /**
   * A question waiting for the reader: ask_user (Haiku's question) or edit_text
   * (code asks: write it yourself, or AI?). Holds the tool id and the other results of that round.
   */
  let pending: { id: string; results: EditContent[]; kind: "ask" | "text"; paths: Path[] } | null = null;
  /** Tool results to send with the reader's next message (after they chose to write text themselves). */
  let deferred: EditContent[] | null = null;
  /** The reader chose "Rewrite with AI": Haiku may now propose text (once). */
  let aiText = false;
  let busy = false;
  let abort: AbortController | null = null;
  let spent = 0;
  /** Bumped when the reader opens another part or closes the chat, so a reply in flight is dropped. */
  let session = 0;
  /** Live parts of the transcript (settings forms) that redraw after every change. */
  let live: (() => void)[] = [];

  const log = h("div.ed-log", { "aria-live": "polite" });
  const subtitle = h("div.ed-sub");
  const cost = h("div.ed-cost");
  const undoBtn = h("button.ed-hbtn", { type: "button", title: "Undo the last change", onclick: () => { if (host.undo()) { note("↶ Undone."); sync(); } } }, "↶ Undo");
  let confirmRestore = false;
  const restoreBtn = h("button.ed-hbtn", { type: "button", title: "Throw away every edit and go back to the generated page" }, "Restore original");
  restoreBtn.addEventListener("click", () => {
    if (!confirmRestore) { confirmRestore = true; restoreBtn.textContent = "Click again to restore"; return; }
    confirmRestore = false;
    restoreBtn.textContent = "Restore original";
    if (host.restoreOriginal()) { note("Restored the page as it was generated."); sync(); }
  });
  const input = h("textarea.ed-input", { rows: 1, placeholder: "Click ✏️ Edit on a part first", "aria-label": "Tell Edit chat what to change", maxlength: 1000, disabled: true });
  const sendBtn = h("button.ed-send", { type: "button", "aria-label": "Send", disabled: true }, "↑");
  const el = h("div.ed-panel", { "aria-label": "Edit chat" },
    h("header.ed-head", null,
      h("div", null, h("div.ed-title", null, "✏️ Edit chat"), subtitle),
      h("div.ed-hacts", null, undoBtn, restoreBtn,
        h("button.ed-close", { type: "button", "aria-label": "Close", onclick: () => close() }, "✕"))),
    log,
    h("div.ed-compose", null, input, sendBtn),
    cost);

  // ---------- transcript ----------

  const scroll = () => requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; });
  /** The "…" shown while Haiku works; messages from tools go above it, in order. */
  let thinking: HTMLElement | null = null;
  function bot(...children: (Node | string | null)[]) {
    const m = h("div.ed-msg.is-bot", null, ...children);
    if (thinking?.isConnected) log.insertBefore(m, thinking);
    else log.append(m);
    scroll();
    return m;
  }
  const note = (text: string, cls = "ed-act") => bot(h("p", { class: cls }, text));
  const you = (text: string) => { log.append(h("div.ed-msg.is-you", null, text)); scroll(); };
  const chips = (items: [string, () => void][]) => h("div.ed-chips", null, ...items.map(([label, fn]) => h("button.ed-chip", { type: "button", onclick: fn }, label)));

  const block = (): Block | undefined => (target ? host.spec().stages[target.stage]?.blocks[target.block] : undefined);
  const stageBlocks = () => host.spec().stages[target!.stage].blocks;
  const replaceBlock = (b: Block) => stageBlocks().map((x, i) => (i === target!.block ? b : x));

  function hint() {
    log.replaceChildren();
    bot(h("p", null, "Click ✏️ Edit on any part of the page (a chart, a slider, cards, a paragraph…), then tell me what to change in your own words."),
      h("p.ed-note", null, "For example: “make the x axis go up to 10”, “show this as a table”, “add a button for a very high temperature”, “explain this more simply”."));
  }

  function redraw() {
    undoBtn.disabled = !host.canUndo();
    restoreBtn.hidden = !host.hasOriginal();
    const b = block();
    subtitle.textContent = b ? `${blockName(b)} · step ${target!.stage + 1}` : "No part selected";
    input.disabled = !b;
    input.placeholder = b ? "What should change? (Enter to send)" : "Click ✏️ Edit on a part first";
    sendBtn.disabled = !b || busy;
    cost.textContent = spent ? `This chat so far ≈ $${spent.toFixed(4)}` : "";
    live.forEach((f) => f());
  }

  /** After undo/restore the part may be gone: keep the panel honest. */
  function sync() {
    if (target && !block()) { target = null; messages = []; pending = null; live = []; hint(); host.highlight(null); }
    redraw();
  }

  /** Check an edit to the target's stage; apply it or return why not. */
  function tryApply(blocks: Block[], focus: EditTarget | null, done: string, structural = false): Result {
    const r = checkEdit(host.spec(), target!.stage, blocks);
    if (!r.ok) { note(`Couldn't do that: ${r.reason}`, "ed-err"); redraw(); return { content: `Refused by the checker: ${r.reason}`, is_error: true }; }
    host.apply(r.spec, focus, structural);
    target = focus;
    host.highlight(target);
    note(`✓ ${done}`);
    for (const n of r.notes) note(n, "ed-note");
    if (!target) { messages = []; pending = null; }
    redraw();
    return { content: `Done: ${done}${r.notes.length ? ` (${r.notes.join(" ")})` : ""}` };
  }

  // ---------- entry ----------

  function open(t: EditTarget) {
    abort?.abort();
    session++;
    target = t;
    messages = [];
    pending = null;
    deferred = null;
    aiText = false;
    busy = false;
    live = [];
    log.replaceChildren();
    host.highlight(t);
    const b = block();
    if (!b) return;
    redraw();
    const starters: [string, () => void][] = suggestions(host.spec(), t).map((s) => [s, () => send(s)]);
    if (textLeaves(b).length) starters.splice(Math.max(0, starters.length - 1), 0, ["✏️ Edit the text myself", () => { you("Edit the text myself"); textEditor([]); }]);
    bot(h("p", null, `Editing ${blockName(b)}. What would you like to change?`), chips(starters));
    input.focus();
  }

  function close() {
    abort?.abort();
    session++;
    target = null;
    messages = [];
    pending = null;
    deferred = null;
    aiText = false;
    busy = false;
    live = [];
    host.highlight(null);
    hint();
    redraw();
    el.dispatchEvent(new CustomEvent("x-close", { bubbles: true })); // closes the drawer
  }

  // ---------- the conversation ----------

  function send(text: string) {
    text = text.trim();
    if (!text || busy || !block()) return;
    you(text);
    if (pending) {
      // Typing instead of clicking still answers the open question. For a text
      // choice, typed wording instructions mean "rewrite it with AI, like this".
      const answer = pending.kind === "text" ? `The reader chose AI and asked: ${text}. Call set_values now with the new text.` : text;
      if (pending.kind === "text") aiText = true;
      messages.push({ role: "user", content: [...pending.results, { type: "tool_result", tool_use_id: pending.id, content: answer }] });
      pending = null;
      log.querySelectorAll(".ed-ask-opts").forEach((e) => e.remove());
    } else if (deferred) {
      messages.push({ role: "user", content: [...deferred, { type: "text", text }] });
      deferred = null;
    } else messages.push({ role: "user", content: text });
    run();
  }

  async function run() {
    busy = true;
    redraw();
    thinking = h("div.ed-msg.is-bot", null, h("p.ed-wait", null, "…"));
    log.append(thinking);
    abort = new AbortController();
    const mine = session;
    try {
      for (let round = 0; round < MAX_ROUNDS; round++) {
        const r = await editChat({ context: editContext(host.spec(), target!, host.value), messages: trimmed() }, abort.signal);
        if (session !== mine) return; // the reader moved to another part
        spent += r.usd;
        messages.push({ role: "assistant", content: r.content });
        for (const c of r.content) if (c.type === "text" && c.text.trim()) bot(md(c.text));
        const uses = r.content.filter((c): c is Extract<EditContent, { type: "tool_use" }> => c.type === "tool_use");
        if (!uses.length) return;
        const results: EditContent[] = [];
        for (const u of uses) {
          if ((u.name === "ask_user" || u.name === "edit_text") && !pending) {
            if (u.name === "ask_user") { pending = { id: u.id, results, kind: "ask", paths: [] }; showQuestion(u.input); continue; }
            const paths = textPaths(u.input.paths);
            if (paths) { pending = { id: u.id, results, kind: "text", paths }; showTextChoice(paths); continue; }
          }
          const res = u.name === "ask_user" || u.name === "edit_text"
            ? (pending ? { content: "Only one question at a time; ask again after their answer.", is_error: true } : { content: "Those paths aren't text of this part; use Values marked text or markdown.", is_error: true })
            : execute(u.name, u.input);
          results.push({ type: "tool_result", tool_use_id: u.id, content: res.content, ...(res.is_error ? { is_error: true } : {}) });
          if (!target) return; // the part was removed
        }
        if (pending) return; // wait for the reader's answer
        messages.push({ role: "user", content: results });
      }
      note("I stopped there to avoid going in circles. Tell me what to do next.", "ed-note");
    } catch (e) {
      if ((e as Error).name !== "AbortError") note((e as Error).message, "ed-err");
      // Drop the unanswered turn so the conversation stays valid.
      while (messages.length && messages[messages.length - 1].role === "user") messages.pop();
    } finally {
      thinking?.remove();
      thinking = null;
      busy = false;
      redraw();
      scroll();
    }
  }

  /** Keep the conversation short: the part's description is resent every turn anyway. */
  function trimmed(): EditMessage[] {
    if (messages.length <= 24) return messages;
    // Start at a plain user message so no tool result is cut from its call.
    const from = messages.findIndex((m, i) => i >= messages.length - 24 && m.role === "user" && typeof m.content === "string");
    return from > 0 ? messages.slice(from) : messages.slice(-24);
  }

  function showQuestion(input: Record<string, unknown>) {
    const question = String(input.question ?? "Which do you mean?");
    const options = Array.isArray(input.options) ? input.options.map(String).slice(0, 5) : [];
    bot(md(question), options.length ? h("div.ed-chips.ed-ask-opts", null, ...options.map((o) => h("button.ed-chip", { type: "button", onclick: () => send(o) }, o))) : null);
  }

  // ---------- tools (run here, in code) ----------

  function execute(name: string, input: Record<string, unknown>): Result {
    const b = block();
    if (!b) return { content: "No part is selected.", is_error: true };
    switch (name) {
      case "set_values": {
        const changes = Array.isArray(input.changes) ? input.changes as { path: Path; value: unknown }[] : [];
        if (!changes.length) return { content: "No changes given.", is_error: true };
        const r = applyChanges(b, changes);
        if ("error" in r) return { content: r.error, is_error: true };
        if (r.textChanged) {
          if (!aiText) return { content: "Text changes need the reader's choice first: call edit_text with these paths (no new text).", is_error: true };
          aiText = false;
          previewText(b, r.block);
          return { content: "Shown to the reader as a before/after preview; it is applied if they accept or edit it. Don't repeat it." };
        }
        return tryApply(replaceBlock(r.block), target, `${changes.map((c) => fieldLabel(c.path)).join(", ")} updated.`, b.type === "quiz" || b.type === "predict");
      }
      case "list_edit": {
        const path = input.path as Path;
        const op = input.op as ListOp;
        if (!Array.isArray(path) || typeof path.at(-1) !== "number" || !Array.isArray(getAt(b, path.slice(0, -1))) || getAt(b, path) === undefined)
          return { content: `${JSON.stringify(path)} is not an item of a list in this part.`, is_error: true };
        if (!listEditable(b, path.slice(0, -1))) return { content: "That list can't be changed.", is_error: true };
        if (!["remove", "duplicate", "up", "down"].includes(op)) return { content: "Unknown list operation.", is_error: true };
        const what = fieldLabel(path);
        return tryApply(replaceBlock(listOp(b, path, op)), target, op === "remove" ? `Removed ${what}.` : op === "duplicate" ? `Duplicated ${what}.` : `Moved ${what}.`, b.type === "quiz");
      }
      case "change_type": {
        const c = conversions(b)[Number(input.option)];
        if (!c) return { content: "That form isn't available for this part.", is_error: true };
        return tryApply(replaceBlock(c.block), target, `Changed to ${c.label.toLowerCase()}.`);
      }
      case "show_graph_options": return graphOptions();
      case "show_settings": settings(); return { content: "Shown the reader a form with every setting." };
      case "add_button": {
        const all = host.spec().stages.flatMap((s) => s.blocks);
        const allowed = new Set(presetVars(stageBlocks(), all, target!.block));
        const set = (Array.isArray(input.settings) ? input.settings as { var: string; value: number }[] : [])
          .filter((s) => allowed.has(s.var) && Number.isFinite(Number(s.value))).map((s) => ({ var: s.var, value: Number(s.value) }));
        if (!set.length) return { content: `No usable settings. Controls you can set: ${[...allowed].join(", ") || "none"}.`, is_error: true };
        const label = String(input.name ?? "").trim().slice(0, 40) || "Try this";
        return tryApply(addPreset(stageBlocks(), target!.block, label, set), target, `Added the button “${label}”.`, true);
      }
      case "arrange": return arrange(String(input.action));
      case "undo": {
        if (!host.undo()) return { content: "Nothing to undo.", is_error: true };
        note("↶ Undone.");
        sync();
        return { content: "Undid the last change." };
      }
      default: return { content: `Unknown tool ${name}.`, is_error: true };
    }
  }

  function arrange(action: string): Result {
    const b = block()!;
    const i = target!.block;
    const blocks = stageBlocks();
    if (action === "up" || action === "down") {
      const j = action === "up" ? i - 1 : i + 1;
      if (j < 0 || j >= blocks.length) return { content: `It's already ${action === "up" ? "first" : "last"} in this step.`, is_error: true };
      const next = [...blocks];
      [next[i], next[j]] = [next[j], next[i]];
      return tryApply(next, { stage: target!.stage, block: j }, action === "up" ? "Moved up." : "Moved down.", true);
    }
    if (action === "duplicate") {
      if (isControl(b)) return { content: "A slider or switch can't be duplicated (it would set the same variable twice).", is_error: true };
      const next = [...blocks];
      next.splice(i + 1, 0, structuredClone(b));
      return tryApply(next, { stage: target!.stage, block: i + 1 }, "Duplicated (the copy is now selected).", true);
    }
    if (action === "remove") {
      if (blocks.length < 2) return { content: "A step needs at least one part, so this one can't be removed.", is_error: true };
      return tryApply(blocks.filter((_, k) => k !== i), null, "Removed. ↶ Undo brings it back; click ✏️ Edit on another part to keep editing.", true);
    }
    return { content: "Unknown action.", is_error: true };
  }

  /** Text changes wait for the reader: before/after, Accept or Discard. */
  function previewText(before: Block, after: Block) {
    const at = target!;
    const changes = textChanges(before, after);
    const diff = h("div.ed-diff", null, ...changes.map((c) => h("div.ed-change", null,
      h("div.ed-where", null, c.where),
      c.from ? h("div.ed-from", null, md(c.from)) : null,
      c.to ? h("div.ed-to", null, md(c.to)) : h("div.ed-to.is-gone", null, "(removed)"))));
    const msg = bot(h("p", null, `Proposed text change${changes.length > 1 ? "s" : ""}:`), diff);
    const actions = chips([
      ["✓ Accept", () => {
        actions.remove();
        if (target?.stage !== at.stage || target.block !== at.block) { note("That part is no longer selected; open it again to apply.", "ed-note"); return; }
        tryApply(replaceBlock(after), at, "Text updated.", before.type === "quiz");
      }],
      ["✏️ Edit it myself", () => {
        actions.remove();
        if (target?.stage !== at.stage || target.block !== at.block) { note("That part is no longer selected; open it again to edit.", "ed-note"); return; }
        const changed = new Set(changes.map((c) => c.where));
        textEditor(textLeaves(after).filter((l) => changed.has(fieldLabel(l.path))).map((l) => l.path), after);
      }],
      ["✕ Discard", () => { actions.remove(); note("Discarded; nothing changed.", "ed-note"); }],
    ]);
    msg.append(actions);
  }

  // ---------- writing: by hand (no AI) or with AI ----------

  /** The part's text values (titles, labels, Markdown), which the reader may write by hand. */
  function textLeaves(b: Block): Leaf[] {
    return leaves(b).filter((l) => l.kind === "text" || l.kind === "prose");
  }

  /** edit_text paths, checked against the part's text (empty = all of it); null if any path isn't text. */
  function textPaths(raw: unknown): Path[] | null {
    const b = block();
    if (!b) return null;
    const paths = (Array.isArray(raw) ? raw : []).filter(Array.isArray) as Path[];
    const all = textLeaves(b).map((l) => JSON.stringify(l.path));
    return paths.every((p) => all.includes(JSON.stringify(p))) ? paths : null;
  }

  /** Code, not AI, asks how the text should change. */
  function showTextChoice(paths: Path[]) {
    const which = paths.length ? `the ${paths.map((p) => fieldLabel(p).toLowerCase()).join(", ")}` : "the text";
    bot(h("p", null, `How would you like to change ${which}?`),
      h("div.ed-chips.ed-ask-opts", null,
        h("button.ed-chip", { type: "button", onclick: () => chooseManual() }, "✏️ Write it myself"),
        h("button.ed-chip", { type: "button", onclick: () => chooseAI() }, "✨ Rewrite with AI")),
      h("p.ed-note", null, "Writing it yourself uses no AI. Or type how it should change and AI will rewrite it."));
  }

  function chooseManual() {
    if (!pending || pending.kind !== "text") return;
    you("✏️ Write it myself");
    log.querySelectorAll(".ed-ask-opts").forEach((e) => e.remove());
    // Sent with the reader's next message, so Haiku knows; no AI call now.
    deferred = [...pending.results, { type: "tool_result", tool_use_id: pending.id, content: "The reader is writing it themselves in an editor (no AI). Don't change the text." }];
    const paths = pending.paths;
    pending = null;
    textEditor(paths);
  }

  function chooseAI() {
    if (!pending || pending.kind !== "text" || busy) return;
    you("✨ Rewrite with AI");
    log.querySelectorAll(".ed-ask-opts").forEach((e) => e.remove());
    aiText = true;
    messages.push({ role: "user", content: [...pending.results, { type: "tool_result", tool_use_id: pending.id, content: "The reader chose AI: call set_values now with the new text for those paths." }] });
    pending = null;
    run();
  }

  /**
   * Hand-written text, no AI: one box per text value (only `paths`, or all of
   * the part's text), starting from the page or from an AI proposal (`start`).
   */
  function textEditor(paths: Path[], start?: Block) {
    const base = start ?? block();
    if (!base) return;
    const keep = new Set(paths.map((p) => JSON.stringify(p)));
    const fields = textLeaves(base).filter((l) => !keep.size || keep.has(JSON.stringify(l.path)));
    if (!fields.length) { note("This part has no text to edit.", "ed-note"); return; }
    const at = target!;
    const boxes = fields.map((l) => {
      const id = `ed-${Math.random().toString(36).slice(2, 9)}`;
      const long = l.kind === "prose" || String(l.value).length > 50;
      const t = long ? h("textarea", { id, rows: Math.min(8, Math.ceil(String(l.value).length / 38) + 1) }) : h("input", { id, type: "text" });
      t.value = String(l.value);
      return { l, t, row: h("div.ed-field.is-text", null, h("label", { for: id }, fieldLabel(l.path)), t) };
    });
    const form = h("div.ed-form", null, ...boxes.map((x) => x.row));
    const save = h("button.ed-chip.is-primary", { type: "button" }, "Save");
    const cancel = h("button.ed-chip", { type: "button" }, "Cancel");
    const buttons = h("div.ed-chips", null, save, cancel);
    const msg = bot(h("p", null, start ? "Change the proposal as you like, then save:" : "Write it your way, then save (no AI):"), form, buttons,
      h("p.ed-note", null, "Markdown works: **bold**, *italics*, lists and $math$."));
    save.addEventListener("click", () => {
      if (target?.stage !== at.stage || target.block !== at.block) { note("That part is no longer selected; open it again to edit.", "ed-note"); return; }
      const now = block()!;
      // From a proposal, every box is written over the proposal (which only differs in text).
      const r = applyChanges(start ?? now, boxes.map((x) => ({ path: x.l.path, value: x.t.value })));
      if ("error" in r) { note(r.error, "ed-err"); return; }
      if (JSON.stringify(r.block) === JSON.stringify(now)) { note("Nothing changed.", "ed-note"); return; }
      if (tryApply(replaceBlock(r.block), at, start ? "Text updated (AI's proposal, edited by you)." : "Text updated (written by you, no AI).", now.type === "quiz").is_error) return;
      buttons.remove();
      boxes.forEach((x) => { x.t.disabled = true; });
    });
    cancel.addEventListener("click", () => { msg.remove(); note("Cancelled; nothing changed.", "ed-note"); });
    boxes[0].t.focus();
  }

  function graphOptions(): Result {
    const b = block()!;
    const ok = conversions(b).filter((c) => checkEdit(host.spec(), target!.stage, replaceBlock(c.block)).ok);
    if (!ok.length) return { content: "This part has no other form that fits its data.", is_error: true };
    const spec = host.spec();
    const tiles = ok.map((c) => h("button.ed-tile", { type: "button", onclick: () => tryApply(replaceBlock(c.block), target, `Changed to ${c.label.toLowerCase()}.`) },
      h("div.ed-preview", { "aria-hidden": "true", inert: "" }, previewOf(c.block, spec)),
      h("span.ed-tile-name", null, c.label)));
    bot(h("div.ed-tiles", null, ...tiles));
    return { content: `Shown ${ok.length} live preview(s): ${ok.map((c) => c.label).join("; ")}. The reader clicks one to switch.` };
  }

  // ---------- the settings form (shown on request) ----------

  function settings() {
    const box = h("div.ed-form");
    const draw = () => {
      const b = block();
      if (!b) { box.replaceChildren(h("p.ed-note", null, "This part was removed.")); return; }
      box.replaceChildren(...paramForm(b));
    };
    draw();
    live.push(draw);
    bot(h("p.ed-note", null, "Change any value; it applies when you leave the field (or press Enter)."), box);
  }

  function paramForm(b: Block): HTMLElement[] {
    const fields = leaves(b).filter((l) => l.kind !== "id" && l.kind !== "data");
    const plain = fields.filter((l) => l.kind !== "expr");
    const formulas = fields.filter((l) => l.kind === "expr");
    const groups = new Map<string, Leaf[]>();
    for (const l of plain) {
      const k = l.path.length === 1 ? "" : typeof l.path[1] === "number" && l.path.length > 2 ? `${l.path[0]}:${l.path[1]}` : String(l.path[0]);
      (groups.get(k) ?? groups.set(k, []).get(k)!).push(l);
    }
    const out: HTMLElement[] = [];
    for (const [k, ls] of groups) {
      const [key, idx] = k.split(":");
      const i = idx === undefined ? undefined : Number(idx);
      const title = k === "" ? "Settings" : i === undefined ? humanize(key) : itemName(key, i);
      const ops = i !== undefined && listEditable(b, [key]) ? listButtons(b, [key, i]) : null;
      out.push(h("fieldset.ed-group", null, h("legend", null, title, ops), ...ls.map((l) => fieldRow(b, l))));
    }
    if (formulas.length) out.push(h("details.ed-group.ed-formulas", null,
      h("summary", null, `Formulas (advanced, ${formulas.length})`),
      h("p.ed-note", null, "Each change is checked at the sliders' minimum, default and maximum."),
      ...formulas.map((l) => fieldRow(b, l))));
    return out;
  }

  function listButtons(b: Block, path: Path): HTMLElement {
    const btn = (op: ListOp, label: string, title: string) => h("button.ed-ibtn", { type: "button", title, "aria-label": title, onclick: (e: Event) => { e.preventDefault(); manualList(b, path, op); } }, label);
    return h("span.ed-ops", null, btn("up", "↑", "Move up"), btn("down", "↓", "Move down"), btn("duplicate", "⧉", "Duplicate"), btn("remove", "🗑", "Remove"));
  }
  const manualList = (b: Block, path: Path, op: ListOp) =>
    tryApply(replaceBlock(listOp(b, path, op)), target, op === "remove" ? `Removed ${fieldLabel(path)}.` : op === "duplicate" ? `Duplicated ${fieldLabel(path)}.` : `Moved ${fieldLabel(path)}.`, b.type === "quiz");

  function fieldRow(b: Block, l: Leaf): HTMLElement {
    const id = `ed-${Math.random().toString(36).slice(2, 9)}`;
    const commit = (v: unknown) => {
      if (v === l.value) return;
      tryApply(replaceBlock(setAt(b, l.path, v)), target, `${fieldLabel(l.path)} updated.`, b.type === "quiz" || b.type === "predict");
    };
    let field: HTMLElement;
    switch (l.kind) {
      case "bool": {
        const c = h("input", { id, type: "checkbox", checked: !!l.value });
        c.addEventListener("change", () => commit(c.checked));
        field = c;
        break;
      }
      case "enum": {
        const s = h("select", { id }, ...(l.options ?? []).map((o) => h("option", { value: o, selected: o === l.value }, humanize(o))));
        s.addEventListener("change", () => commit(s.value));
        field = s;
        break;
      }
      case "answer": {
        const choices = (getAt(b, [...l.path.slice(0, -1), "choices"]) as string[] | undefined) ?? [];
        const s = h("select", { id }, ...choices.map((c, i) => h("option", { value: String(i), selected: i === l.value }, `${String.fromCharCode(65 + i)}. ${c.slice(0, 60)}`)));
        s.addEventListener("change", () => commit(Number(s.value)));
        field = s;
        break;
      }
      case "number": {
        const n = h("input", { id, type: "number", step: "any", value: String(l.value) });
        n.addEventListener("change", () => { const v = Number(n.value); if (n.value.trim() !== "" && Number.isFinite(v)) commit(v); else n.value = String(l.value); });
        field = n;
        break;
      }
      default: {
        const long = l.kind === "prose" || String(l.value).length > 50;
        const t = long ? h("textarea", { id, rows: Math.min(6, Math.ceil(String(l.value).length / 40) + 1) }) : h("input", { id, type: "text" });
        t.value = String(l.value);
        if (l.kind === "expr" || l.kind === "latex") t.classList.add("is-code");
        t.addEventListener("change", () => commit(t.value));
        if (!long) t.addEventListener("keydown", (e) => { if ((e as KeyboardEvent).key === "Enter") t.blur(); });
        field = t;
      }
    }
    const last = l.path.at(-1);
    const inline = typeof last === "number" && l.path.length > 1 && listEditable(b, l.path.slice(0, -1))
      ? h("span.ed-ops", null,
        h("button.ed-ibtn", { type: "button", title: "Duplicate", "aria-label": "Duplicate", onclick: () => manualList(b, l.path, "duplicate") }, "⧉"),
        h("button.ed-ibtn", { type: "button", title: "Remove", "aria-label": "Remove", onclick: () => manualList(b, l.path, "remove") }, "🗑"))
      : null;
    const name = typeof last === "number" ? itemName(String(l.path.at(-2) ?? ""), last) : humanize(String(last ?? ""));
    return h("div.ed-field", { class: l.kind === "bool" ? "is-check" : "" }, h("label", { for: id }, name), field, inline);
  }

  // ---------- compose box ----------

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); const t = input.value; input.value = ""; send(t); }
  });
  input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = `${Math.min(input.scrollHeight, 140)}px`; });
  sendBtn.addEventListener("click", () => { const t = input.value; input.value = ""; send(t); });

  hint();
  redraw();
  return { el, open, close, sync, target: () => target };
}

/** A live, scaled-down render of a candidate part, in the page's palette. */
function previewOf(b: Block, spec: ExplainerSpec): HTMLElement {
  const frame = h("div.explainer.ed-frame", { "data-theme": spec.design.theme, "data-fonts": spec.design.fontPairing, "data-emphasis": spec.design.emphasis });
  if (spec.design.theme === "custom" && spec.design.accent) {
    const pal = derivePalette(spec.design.accent);
    for (const [k, v] of Object.entries(pal.light)) frame.style.setProperty(`--c-l-${k}`, v);
    for (const [k, v] of Object.entries(pal.dark)) frame.style.setProperty(`--c-d-${k}`, v);
  }
  frame.append(renderBlock(b, { store: new Store(spec), key: "preview", answer: () => undefined, setAnswer: () => {}, onDispose: () => {}, href: () => "#" }));
  return frame;
}
