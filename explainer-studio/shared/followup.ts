// Follow-ups: a message typed under an explainer continues the conversation.
// "revise" plans a new version from the previous explainer (its current spec,
// Edit chat edits included); "new" makes a related explainer that knows what
// the conversation already covered. Pure helpers shared by the app (which
// builds the FollowUp) and the server (server/explainer/core/followup.py turns it into the planner request).

import { ICONS, type IconName } from "./menus";
import { LIMITS, type Block, type Design, type ExplainerSpec, type FollowUp, type KeptParts, type Outline } from "./spec";

type FollowUpWidgets = NonNullable<Extract<FollowUp, { kind: "revise" }>["widgets"]>;

/** How the pipeline joins the chatbot context (server/explainer/pipeline.py): outline text, then this, then the source. */
const SOURCE_MARK = "\n\nSource material:\n";

/** The spec as the planner's outline: no design, chatbot, warnings or icons; widgets and computed example steps left out. */
export function compactOutline(spec: ExplainerSpec): Outline {
  const block = (b: Block): Block => {
    if (b.type === "widget") return { type: "text", md: `(interactive widget: ${b.title})` };
    if (b.type === "examples") return { ...b, items: b.items.map(({ steps: _steps, ...it }) => it) };
    return b;
  };
  return {
    title: spec.title,
    subtitle: spec.subtitle,
    level: spec.level,
    layout: spec.layout,
    keyIntuition: spec.keyIntuition,
    phases: spec.phases,
    stages: spec.stages.map((s) => ({ id: s.id, label: s.label, phase: s.phase, blurb: s.blurb, blocks: s.blocks.map(block) })),
    glossary: spec.glossary,
  };
}

/** The source excerpt an explainer was built from (paper summaries, or the typed request). */
export function sourceOf(spec: ExplainerSpec): string {
  const i = spec.chatbot.context.indexOf(SOURCE_MARK);
  return i < 0 ? "" : spec.chatbot.context.slice(i + SOURCE_MARK.length).trim();
}

const ICON_NAME = new Map(Object.entries(ICONS).map(([name, emoji]) => [emoji as string, name as IconName]));

/** The previous explainer's design, so a revision keeps its look (and skips the design call). */
export function designOf(spec: ExplainerSpec): { design: Design; accent?: string; colorMode?: NonNullable<ExplainerSpec["design"]["mode"]> } {
  return {
    design: {
      // A custom palette is stamped on after assembly; "ocean" is only a valid placeholder.
      theme: spec.design.theme === "custom" ? "ocean" : spec.design.theme,
      fontPairing: spec.design.fontPairing,
      headingStyle: spec.design.headingStyle,
      stageIcons: spec.stages.flatMap((s) => {
        const icon = ICON_NAME.get(s.icon);
        return icon ? [{ stageId: s.id, icon }] : [];
      }),
      chatbot: { systemPrompt: spec.chatbot.systemPrompt, starters: spec.chatbot.starters },
    },
    ...(spec.design.theme === "custom" && spec.design.accent ? { accent: spec.design.accent } : {}),
    ...(spec.design.mode ? { colorMode: spec.design.mode } : {}),
  };
}

/**
 * What the previous version already wrote and a revision can reuse for the stages it keeps: worked-example
 * steps ("<stage id>:<block>:<item>", the key applyExampleSteps uses) and section extras. Section extras are
 * dropped first, then the steps, when they don't fit LIMITS.keptChars (the server then writes them again).
 */
export function keptOf(spec: ExplainerSpec): KeptParts | null {
  const steps: Record<string, string[]> = {};
  spec.stages.forEach((st) => st.blocks.forEach((b, bi) => {
    if (b.type === "examples") b.items.forEach((it, ii) => { if (it.steps?.length) steps[`${st.id}:${bi}:${ii}`] = it.steps; });
  }));
  const notes = spec.sectionNotes && Object.keys(spec.sectionNotes).length ? spec.sectionNotes : undefined;
  for (const kept of [{ steps, notes }, { steps }]) {
    const k: KeptParts = { ...(Object.keys(kept.steps).length ? { steps: kept.steps } : {}), ...(kept.notes ? { notes: kept.notes } : {}) };
    if (!k.steps && !k.notes) return null;
    if (JSON.stringify(k).length <= LIMITS.keptChars) return k;
  }
  return null;
}

/**
 * The previous version's widgets by stage id, in page order, so a revision can put back each one whose
 * placeholder it kept. Widgets past LIMITS.widgetChars are left out (they then stay a placeholder).
 */
export function widgetsOf(spec: ExplainerSpec): FollowUpWidgets | null {
  const out: FollowUpWidgets = {};
  let chars = 0;
  for (const st of spec.stages) {
    for (const b of st.blocks) {
      if (b.type !== "widget" || chars + b.html.length > LIMITS.widgetChars) continue;
      chars += b.html.length;
      (out[st.id] ??= []).push(b);
    }
  }
  return Object.keys(out).length ? out : null;
}

/** What the conversation already covered, for a "new" follow-up (a few hundred tokens). */
export function threadSummary(specs: ExplainerSpec[]): string {
  return specs.map((s, i) => [
    `${i + 1}. "${s.title}" (${s.level}): ${s.keyIntuition}`,
    `   Steps: ${s.stages.map((st) => st.label).join("; ")}`,
    s.glossary.length ? `   Terms: ${s.glossary.map((g) => g.term).join(", ")}` : "",
  ].filter(Boolean).join("\n")).join("\n");
}

/**
 * The FollowUp sent with the request. base: the explainer to revise or build on
 * (the conversation's latest); thread: every finished explainer so far, oldest
 * first. The source excerpt is trimmed to fit LIMITS.followUpChars; returns
 * null if even the bare outline is too large.
 */
export function buildFollowUp(kind: FollowUp["kind"], base: ExplainerSpec, thread: ExplainerSpec[]): FollowUp | null {
  const source = sourceOf(base);
  if (kind === "revise") {
    const outline = JSON.stringify(compactOutline(base));
    if (outline.length > LIMITS.followUpChars) return null;
    const room = LIMITS.followUpChars - outline.length;
    const kept = keptOf(base);
    const widgets = widgetsOf(base);
    return { kind, outline, ...designOf(base), ...(source && room > 500 ? { source: source.slice(0, room) } : {}), ...(kept ? { kept } : {}), ...(widgets ? { widgets } : {}) };
  }
  const previous = threadSummary(thread.slice(-6)).slice(0, 12_000);
  const room = LIMITS.followUpChars - previous.length;
  return { kind, previous, ...(source ? { source: source.slice(0, room) } : {}) };
}

/** How a revision that only changes part of the page replies (Quality+; server/explainer/revise.py merges it). */
const CHANGE_RULES = [
  "Rules for the revision:",
  '- Reply with one JSON object holding only what changes: {"changes": {...}}. Every stage you don\'t mention is kept exactly as it is, so never repeat an unchanged stage. Its fields, each optional:',
  '  - "add": [{"after": "<id of the stage it follows, or \\"start\\">", "stage": {a complete new stage in the usual format, with a new id}}]',
  '  - "replace": [{a complete stage in the usual format, with the id of the stage it replaces}]',
  '  - "remove": ["<stage id>", ...]',
  '  - "title", "subtitle", "keyIntuition", "level": new values, only when they should change',
  '  - "glossary": [{"term": "...", "definition": "..."}]: terms to add or update',
  "- Change what the request asks for, and anything the change makes wrong. A new or replaced stage may use the controls of the stages before it and must use one of the outline's phases.",
  "- If the request changes most of the explainer (a different level, audience, style or length throughout), reply instead with the complete revised outline in the usual format, keeping the ids of the stages you keep.",
  "- Use the reader level the request implies, otherwise keep the current one.",
].join("\n");

const REWRITE_RULES = [
  "Rules for the revision:",
  "- Reply with the complete revised outline as one JSON object, in the usual format.",
  "- Change what the request asks for. Keep everything else (stages, blocks, numbers, formulas, wording) as it is unless the change makes it wrong or the request asks for a broader rewrite.",
  "- Keep the ids of the stages you keep. Use the reader level the request implies, otherwise keep the current one.",
].join("\n");

/** Added when the outline holds widget placeholders: the finished widget is put back where its placeholder is. */
const WIDGET_RULE = 'A text block that reads "(interactive widget: ...)" stands for a finished interactive widget. Keep every such block exactly as it is and where it is; widgets can\'t be rewritten here, so fix the text around them instead.';

/**
 * The planner's user turn for a follow-up (the cached system prompts stay byte-stable). changes: the reply
 * may hold only the changes (Quality+); otherwise it is the complete revised outline (Lite+, Freeform+).
 */
export function revisionRequest(text: string, f: Extract<FollowUp, { kind: "revise" }>, changes = false): string {
  return [
    "Revise an existing interactive explainer for the reader's follow-up request.",
    `Reader's request: ${text}`,
    changes ? CHANGE_RULES : REWRITE_RULES,
    f.outline.includes("(interactive widget: ") ? WIDGET_RULE : "",
    `Current explainer (outline JSON):\n${f.outline}`,
    f.source ? `Source material it was built from (stay accurate to it):\n${f.source}` : "",
  ].filter(Boolean).join("\n\n");
}

/** Appended to a normal request when the explainer continues the conversation. */
export function continuationNote(f: Extract<FollowUp, { kind: "new" }>): string {
  return [
    `This continues a conversation that already produced these explainers:\n${f.previous}`,
    "Build on them: reuse their terms, refer back where it helps, and don't re-teach what they covered at length unless the request asks for it.",
    f.source ? `Source material from the conversation (use it when the request is about the same material):\n${f.source}` : "",
  ].filter(Boolean).join("\n\n");
}
