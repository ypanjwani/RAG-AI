# Playground mode: how it works + layout code-fix plan

Written Oct 31, 2026. Part 1 is how Playground mode worked before this change; part 2 is what we found; part 3 was the first plan; **part 4 is what was built (Oct 31)**. Read `.claude/rules/playground.md` too before changing the mode.

---

## Part 1: how Playground mode works today

All code is in `server/explainer/codelab.py` unless noted.

### The order of steps for one section

1. **Section choice (before any spec):** the page writer marks sections (`PLANNER_NOTE_PG`), Jev's gate checks each (`gate_one(playground=True)`, `PG_GATE_QUESTIONS`), the cap is 4 per page (`MAX_MARKED`). A failed playground lets `_refill` try the next gate-opened section.
2. **Haiku writes the spec** (`_pg_ask`, prompt `PLAYGROUND_SYSTEM` in `prompts/prompts.ts`). Haiku sees only this one section (`_section(outline, si)`: label, summary, numbered blocks) plus the writer's hint. Sections run in parallel.
3. **Checks + repairs** (`_pg_until_clean`): check, then code fixes, then up to 2 Haiku repairs, then the layout step (below).
4. **Plan summary** (`playground_plan`): code only, no AI.
5. **Jev's review** (`review(..., playground=True)`, `PG_REVIEW_QUESTIONS`): one fix on a clear no, re-checked and re-reviewed.
6. **Screenshot check** (`_pg_look`): off unless `PG_LOOK=1`.
7. **Saved on the page** (`_pg_accept` → `playground_html`).

Any failed step → the section stays text, with a note.

### The spec (what Haiku writes)

One JSON object:

| Field | Holds |
|---|---|
| `title` | 1-5 word name, in the section's terms (shown as the playground's heading) |
| `afterBlock` | optional: which section block it goes after (placement only, not stored) |
| `plan` | Haiku writes it first: `insight`, `mechanism`, `user_changes`, `reacts`, `views` |
| `state` | all shared values |
| `init` | optional JS snippet run at start and on Reset |
| `components` | 2-12 of: slider, select, toggle, button, play, chart, readout |

Controls write to `state` (sliders directly; `onChange`/`onClick`/`onTick` return a patch). Charts' `draw` snippets return marks (line, points, bars, heatmap, node, link, text, graph, sequence, matrix...). Snippets are JS function bodies as strings, using only `state`, args, `Math` and the helper list. Reset is added automatically.

### The functions

- **`playground_html(spec)`**: keeps `title, plan, state, init, components` (`_PG_STORED`), escapes `</` and U+2028/2029, returns `<div id="pg"></div><script>XW.playground({...})</script>`. Used for the saved block, the size check and screenshots.
- **`check_playground(spec)`**: free. Size check (`WIDGET_MAX_CHARS`), then `node scripts/check-playground.mjs` → `shared/playground/validator.cjs` in a worker with a 5 s limit. Runs every snippet, no NaN, every control reaches a chart/readout, no chart always empty, 60 Play ticks. Returns `{"problems", "facts", "layout"}`; `facts` and `layout` only on a pass. Fails closed (`CHECK_FAILED`).
- **`_widen(rng, v)`**: grows `[lo, hi]` to hold `v` with a 10% margin; only the side that was too small moves. `None` if `rng` isn't a numeric range.
- **`_pg_autofix(spec, problems)`**: code only, one pass, no retries. Matches 3 hard-error messages by regex: draggable point outside chart (`_PG_DRAG_OUT` → widen x/y), bar outside y range (`_PG_BAR_OUT` → widen y), control that changes nothing (`_PG_DEAD` → remove it if another real control stays). Returns a fixed copy or `None`.
- **`_pg_until_clean`**: `by_code` = check → `_pg_autofix` → check again (kept unless the checker can't run). Then up to `MAX_REPAIRS` = 2 Haiku repairs (`PLAYGROUND_REPAIR`, `yourPreviousSpec` + `problems`), each followed by `by_code`. Stops on clean, `CHECK_FAILED`, a failed call or `no_playground`. Still failing → `(None, report)`. Clean → `_pg_fix_layout`.
- **`_pg_fix_layout`**: `LAYOUT_REPAIRS` = 1 Haiku try with `LAYOUT_LEAD` + the layout list. **No code-fix step today.** Kept only if it passes every check and has strictly fewer layout problems; otherwise the original stays.
- **`playground_plan(spec)`**: code only. Renames Haiku's plan into Code Lab's words so shared code works unchanged: `insight→lesson`, `user_changes→readerDoes`, `mechanism`, `reacts`; adds `title`, `afterBlock`, `controls` and `visuals` built from `components` (Reset skipped; drag/click charts count as controls), `backup: ""`. Length-capped.
  - Read by: Jev's `review_state` (`lesson`, `readerDoes`, `backup`; Jev's control list comes from the checker's `facts`, not `plan.controls`), `look_review` (`lesson`), `plan_text` saved on the block (`lesson, readerDoes, controls, visuals, backup`).
  - `mechanism` and `reacts` are built but read nowhere; `views` isn't copied.

### Decisions already made (don't reopen without new evidence)

- **Don't merge** `lesson/readerDoes/mechanism/reacts` into one field: saves only ~10-20 tokens, and Jev's questions check against `plan.lesson` alone.
- **Keep `title`**: Haiku writes it per section. Follow-up edits keep the old title on purpose. The section summary (`blurb`) and full text already go to Haiku and Jev.
- **Keep `afterBlock`**: it only places the playground inside its section. Which sections get one is decided earlier (writer marks + gate + cap).
- **No Jev for sorting layout problems**: the validator's messages are fixed sentences, so regex matches them for free.
- Open small idea: send `mechanism` + `reacts` to Jev in `review_state` (test first).

---

## Part 2: what the saved playgrounds show (Oct 31)

Script: re-checked every playground in `.variety/` and `server/instance/dev.db` with the free checker (167 playgrounds).

- 133 had no layout problems. **34 (20%) had at least one, 61 problems in total.**

| Layout message | Problems (playgrounds) | Where the fix lives |
|---|---|---|
| text runs past the chart edge | 36 (21) | renderer (text position is inside the `draw` snippet) |
| `yLabel` drawn over a heatmap | 7 (7) | spec field: code |
| data fills < 40% of the x range | 6 (6) | spec field: code |
| line outside the y range | 2 (2) | spec field: code |
| link label sits on a shape | 4 (2) | Haiku |
| shapes overlap | 3 (1) | Haiku |
| link through a shape / label cut in a shape / shape sticks out | 3 (3) | Haiku |

Caveats: these are **final** saved specs (after any kept layout repair), and some predate the layout check. First-draft specs likely have more.

---

## Part 3: the plan (not built)

Goal: fix layout problems with code first; Haiku's one layout repair stays as the fallback.

### Step 0: measure first drafts
Log the layout messages `_pg_fix_layout` receives (before any repair), e.g. into `res["layout_seen"]` for test-run stats, so the counts in part 2 can be redone on first drafts.

### Step 1: code fixes in the spec (new `_pg_layout_autofix`, same style as `_pg_autofix`)
Match the validator's exact messages (from `layoutProblems` in `shared/playground/validator.cjs`) with regex, by chart id:

| Message starts like | Fix |
|---|---|
| `<id>: the y-axis name '...' is drawn over the heatmap's top-left cells` | delete that chart's `yLabel` |
| `<id>: the data never fills more than N% of the x range [a,b] ... narrow x to about [c, d]` | set chart `x` to `[c, d]` |
| `<id>: a line mark lies outside the y range [a,b] ... (values lo to hi)` | `_widen` y with lo and hi |

Not code-fixable: `<id>: a <mark> sits on the top edge of the chart` (a capped line). The message gives no real values, and they are usually clamped inside the `draw` snippet, so widening y wouldn't help. Leave it to Haiku. (It never appeared in part 2.)

Run it at the **start of `_pg_fix_layout`**. Keep the fixed copy only if `check_playground` still passes (no hard problems; draggable points still inside after narrowing x) **and** layout problems are strictly fewer. Then, if layout problems remain, do the existing single Haiku repair on the code-fixed spec.

### Step 2: renderer fix for text past the edge (biggest group)
In `shared/playground/components.cjs` (draws text marks), keep text inside the plot: flip `align` or shift it inward when it would cross an edge. Update the matching check in `validator.cjs` (`layoutProblems`) so it measures the same geometry (they share `structures.cjs` helpers). Note: `shared/playground/` was copied from the ai-playground experiment (`Bloging-substack/ECommerce`), so this makes our copy differ; record that in `.claude/rules/playground.md`.

### Step 3: Haiku fallback
Unchanged: `LAYOUT_REPAIRS` = 1, for what's left (mostly diagram overlap/link problems).

### Tests and docs
- Unit tests in `server/tests/test_playground.py`: one per new message pattern (fixed and not-fixed cases), the "keep only if fewer and still passes" rule, and that a fix that breaks a draggable point is dropped.
- Renderer: a vitest case in `tests/playground.test.ts` for edge text.
- Update `.claude/rules/playground.md` (layout check section).
- Re-run the counting script after building to compare with part 2.

Expected effect from part 2's numbers: code clears about 51 of 61 problems (84%); Haiku is needed for about 10 diagram problems.

---

## Part 4: what was built (Oct 31)

Re-checking the 167 playgrounds in **every state** the dry run reaches (not only the first) changed part 3:
48 playgrounds had a problem, not 34. Text past the edge was nearly always a centred label crossing the left or top
edge by 20 px or less, sometimes one following a moving point. Capped lines appeared only after a control moved
(7 charts, mostly probabilities that really reach 1). Diagram problems hit 3 of 13 diagrams, all placed by loops.

Built:
1. **Validator checks every state** (`validator.cjs`: `stateMarks`, up to 48 states, `chartLayout` per state). A
   later-only problem ends with " (after the controls move, not at the start)". "Line outside y" is first-state only.
2. **Renderer keeps text inside** (`structures.placeText`, used by `components.cjs` and the validator's text check).
3. **Code fixes before Haiku** (`codelab._pg_layout_autofix`, run first in `_pg_fix_layout`): heatmap `yLabel` moved
   into the title "(rows: …)", empty x narrowed (not below an old start ≥ 0), 5% headroom for a line along a top of 1
   or 100, y widened up to 3×, range fitted to a heatmap. Kept only if every check passes with fewer problems.
4. **Haiku** keeps its single layout repair for what's left. Stats: `layout_seen`, `layout_code_fixed`, `layout_fixed`.

Result on the 167: **48 → 26** playgrounds with a problem after `placeText`, **→ 6** after the code fixes (every
code fix kept, none broke a check). Left for Haiku: 4 diagrams, 1 line clamped in its code (`Math.min(…, 10)`), 1
line far outside y.

Deferred: a diagram tidy pass in the renderer with Jev picking between candidates. Revisit once `layout_seen` from a
13-topic live run shows how often diagrams need it.

### Oct 31 (later): labels on lines + empty y range

After checking a live diffusion page and a self-attention page (8 playgrounds, all clean under the checks above):
- **Labels drawn across a line** (e.g. 'mode -2' on its dashed rule, 'fidelity' on its curve, 'ε(∅)' on an arrow):
  the renderer's `structures.placeLabels` nudges each label up or down off lines, areas, hlines, arrows and earlier
  labels. The checker reports one only if no free spot exists in the starting view.
- **Empty y range** ('Final samples' peaking at 0.27 on [0, 1]): new check like the x one; `_pg_layout_autofix` narrows y.
- On 175 playgrounds: 50 with a problem → **8** after renderer + code fixes (4 diagrams, 2 lines, 2 crowded labels).
- Not done (user chose not to): a label placed next to the wrong curve ('√ᾱₜ' beside the orange line).
