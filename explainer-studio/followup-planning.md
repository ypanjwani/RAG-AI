# Follow-ups that edit, not rewrite

Status: plan only, nothing built yet (Oct 25, 2026).

## The problem

When a reader asks a follow-up ("make this chart interactive", "change this graph"), the page gets rewritten
and the interactive parts disappear, including the one the reader asked about.

### Why it happens (current code)

1. **Haiku can't see the widgets.** `compactOutline` (`shared/followup.ts:14`) turns every widget into a single
   text line `"(interactive widget: <title>)"`. Its code, Playground spec and plan are not sent.
2. **The page's mode is dropped.** `pipeline.py:510` runs every Code Lab 7.0 / Playground revision as a plain
   Interactive (Quality+) revision. The widget builder, Jev gate and widget checks never run.
3. **Haiku can't reply with a widget.** The revision reply is the outline format (text, cards, prebuilt blocks).
   It has no place for Code Lab widget code or a Playground spec.
4. **Nothing puts the widgets back.** `restore_widgets` (`deepen.py:210`) exists, but only Review Rounds
   (Quality 2.0) calls it. The follow-up path doesn't, so even stages Haiku didn't touch come back with a text
   line where the widget was.
5. **Rewrites happen too easily.**
   - Quick read (Lite+) always uses `REWRITE_RULES`: the whole outline, every time.
   - Interactive uses `CHANGE_RULES` (changes only), but the prompt lets Haiku send the complete outline "if the
     request changes most of the explainer", and it often does that for visual requests. Its changes are also
     whole stages, so one chart edit rewrites the section's text too.
   - After a full outline comes back, the clarity and fact checks run on every stage again.

Haiku is capable of doing what the reader asks. The pipeline limits what it sees, what it may reply with and
what runs afterwards.

## Approach: find the target, edit only that, check only that

Haiku sees everything relevant to the change but sends back only what changed. Everything else is copied over
unchanged, never regenerated.

### Step 1: find what to change (one cheap Haiku call, Low effort)

Input: the page outline. Each widget appears as its title **plus a one-line summary of its plan** (the plan is
already stored on the widget block, `codelab.py:397`), not just a placeholder. Also: the glossary and the
reader's request.

Output (structured JSON):
```json
{"targets": [
  {"stage": "<stage id>", "part": "widget" | "text" | "section", "what": "<what to change, in one sentence>"},
  {"after": "<stage id>", "part": "new-section", "what": "..."}
], "pageWide": false}
```
- `pageWide: true` is only for requests that clearly change the whole page (level, audience, length, style
  throughout). Only then is the current full-revision path used.
- Unclear → ask the reader (the existing "unclear" route already does this).
- **UI shortcut (later):** a "✏️ Edit" button on each widget and section fills in the target directly, so this
  step is skipped (cheaper, and the target is never guessed wrong).

### Step 2: edit each target (one call per target, the page's Effort)

The editor gets the target in full plus the context it needs to stay consistent:

| Context | Why |
|---|---|
| The section's full text (all its blocks) | The chart has to match what this section teaches: formulas, numbers, terms |
| The widget's stored plan | What it was meant to teach, its ranges and maths |
| The widget's current code / Playground spec | So it edits the widget instead of starting over |
| A short outline of the rest of the page (titles, key intuition, glossary) | Same terms, no repeat of another section's chart |
| The source material (paper excerpt) | New numbers and formulas stay accurate |
| The reader's request + the `what` from Step 1 | What to change |

Allowed output:
1. **The edited target.** For a widget: the new code or spec, plus an updated plan. For long code, small
   find-and-replace edits are allowed instead of the whole file, which keeps output small and stops drift.
2. **Optional: edits to this section's own text**, only where the change makes it wrong or incomplete
   (e.g. the text says "fixed scores" but the chart now has a temperature slider).
3. Other sections are locked. If they need changing, they should be their own targets from Step 1.

Same rule the other way: a text edit gets the section's widget plan too, and may return
`"widgetMismatch": true`. That widget then gets edited as well.

Prompts: reuse the mode's own prompts so the edited widget follows the same rules as a new one.
- Code Lab 7.0: `CODE_LAB_FREE_SYSTEM` + a new short "edit" user turn (current code + plan + request).
- Playground: `PLAYGROUND_SYSTEM` / `PLAYGROUND_REPAIR` style, spec in, spec out.
- Interactive / Quick read: a changes-only reply for that one stage (existing `CHANGE_RULES` "replace", scoped
  to the target).
- Creative (Freeform+): `WIDGET_SYSTEM` with the current widget.

### Step 3: check only what changed

- **Widget:** the mode's normal checks on the edited widget only: `check-widget.mjs` (Code Lab 7.0),
  `check-playground.mjs` + `_pg_autofix` (Playground), the number/claim checks, Jev's review with the mode's
  questions.
- **New: text and chart agree.** One Jev (or Haiku) question: "does this section's text still describe this
  widget?" (section text + the widget's plan). On a clear no, fix the text once.
- **Text:** clarity + fact check with `only=` set to the changed stages (this already works).
- **Failure never loses anything:** one repair; if that fails, keep the **old** widget and text together and
  tell the reader "couldn't change this chart: <reason>". A section is never left half changed.

### Step 4: merge

- Untouched stages are copied byte for byte from the previous spec (not from the compact outline).
- Code check: every stage not in the target list must be identical before and after; if not, keep the old one
  and log a warning.
- Saved as a new version as today (`version + 1`), with a revision summary
  ("1 chart changed, 1 section's text updated, 6 kept as they were").

## Build order

1. **Stop losing widgets (small, do first).** In the revise path, put back widgets from the previous spec for
   Code Lab 7.0, Playground, Creative and Lab, the way `deepen.refit` / `restore_widgets` already do. Stages
   Haiku didn't change keep their widgets. Add a test: a revision of a page with widgets keeps every widget whose
   stage wasn't changed.
2. **Widget edit call (Step 2 + 3 for widgets)** for Code Lab 7.0 and Playground, reusing their prompts,
   checks and repair. Includes the "text still describes the chart" check.
3. **Target finder (Step 1)** in front of it, replacing "rewrite the outline" for follow-ups. Full revision
   only when `pageWide` is true.
4. **Tighten the Interactive prompt:** no full outline for one-visual or one-section requests.
5. **"✏️ Edit" buttons** on widgets and sections in the viewer, sending a pre-filled target.
6. **Measure:** the same follow-ups on 4-5 pages per mode. Track cost, time, widgets kept, unwanted changes
   (diff of untouched stages should be empty) and whether the request was actually done.

## Cost notes

- Step 1 is a short Low call (outline in, a few lines out).
- Step 2 input is one section + one widget + a page summary; output is one widget (or small edits). Much less
  than regenerating a page.
- Prompt caching: put the page block first and mark it cached, as `revise.page_block` does, so a second
  follow-up on the same page reads it cheaply.

## Open questions

- Find-and-replace edits vs a full widget rewrite: start with a full rewrite of the one widget (simpler, checks
  catch problems), and add edits if output cost is high.
- Should a widget edit go through Jev's gate again? Probably not: the section already earned its widget. Run the
  review only.
- The 4-widget cap: an edit that turns a text section into an interactive one ("add a chart here") must respect
  `MAX_MARKED`; if the page is full, say so instead of dropping another widget.
- Where the target finder should live for modes without widgets (Quick read): the same call works; its targets
  are just text.
