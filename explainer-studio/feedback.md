# Feedback button (v3) and saved preferences

v1 was built 2026-10-30. v2 was built the same day, after v1 sometimes changed the wrong part of the page or made it worse. v3 (also 2026-10-30) changes how the plan step finds the sections, after a real self-attention thread (v1→v4) went wrong; see "v3" below. Tested with unit, route and pipeline tests, and the plan step was replayed live on that real thread. **The full flow (dialog → new version) still needs a browser run.**

## The rule

The app never guesses which part to change. It **proposes** a plan, **asks** when it can't tell, changes **only what you confirmed**, and **checks** that each changed part is better before keeping it.

## v4 (current): Haiku picks the sections instead of Jev

v3 asked Jev "is this feedback about section N?" per section. That works when the reader *names or points at* a part. It fails when they *describe a kind of part*. "Currently there are reset buttons only in some interactions…" scored ≤0.28 on every section. So the card offered 12 single-choice chips carrying the raw feedback as the change. Only one widget was edited, weakly ("click to place q").

Now:
- **Pick: a small Haiku call** (`feedback_pick.pick_sections`, `FEEDBACK_PICK_SYSTEM`, format `feedbackPick`). It reads all the cards and the feedback, and returns `{sections, unsure, kind, why}`. It can match a description against the cards ("widgets whose only button is Reset" → sections 2 and 4), and handles several sections at once. A complaint is never "none": if it can't place it, it lists the possible sections as `unsure`.
- **Plan: the second Haiku call**, as before (full JSON of the picked sections only). When the pick is unsure, the plan step turns each unsure section into a **concrete choice** ("add a New keys button…"). The code-made "which part?" question is only the last resort.
- **Choices are multi-select**, and "The whole page" is always one of them.
- **Never a silent whole-page change:** more than 6 sections picked, or "whole page" with candidate sections → ask.
- **Richer cards:**
  - every playground lists the **Reset its library adds automatically**
  - a widget whose only button is Reset is flagged ("the reader can only drag on its chart")
  - each playground chart says what it draws, read from its draw code ("Per-head dimension d_k (bar chart)")
- **Jev** is now used only after the edit (`feedback_check.py`).

Live replay (real Haiku, 8 cases, $0.0103 in all; each feedback costs about $0.0007–0.002):

| Feedback | Result |
|---|---|
| "reset buttons only in some interactions…" | changes to **sections 2 and 4** (the two Reset-only widgets): a resample button, a slider ✅ |
| "…control that bar chart below the line plot" (no number) | **section 7** ✅ (needed the bar-chart card line) |
| "In section 7… bar chart" / "play button in section 7" / "section 5" | the right section ✅ |
| "Nice page, I liked it." | nothing ✅ |
| "This part is confusing" | asks, with 5 concrete choices + the whole page ✅ |
| "graphs… look same for every section" | 3 changes over 3 sections (2 whole-section rewrites): concrete, but broad. The card lets you untick. |

### The "is it better?" check now sees widgets (2026-10-30)

A real run: "In 'why it works', that matrix looks weird, without any values, weird colour, small matrix on a white background". The pick and the plan were right. But Jev's check only saw each widget's title, lesson and readerDoes, so a change to how it *looks* was invisible to Jev. It said "didn't deal with what you asked for" twice, the section was put back, and the run stopped with "Nothing on the page was changed".

Fixed in `feedback_check.py`:
1. **Jev sees the real widget.** `stage_view` adds each widget's `settings`: the playground spec (state + components with their draw code) or the start of a code widget's html, up to 5,000 characters. The `fixes` question tells Jev to compare them.
2. **A widget change is put back only when it got worse.** If, after the retry, Jev only doubts that it *fixes* the complaint, the change is kept with "Changed "…" as you asked, but it may not fully fix it… Send feedback again if it still looks wrong". The previous widget comes back only when `not_worse` fails (on the retry, and on the first try when the retry can't be used). Text changes still revert when they fail twice.

The same feedback rerun live passed first time ($0.0022). It got values in the cells, a stronger colour, a 0–1 colour legend and axes. **Not fixed: the matrix is still small**, because the heatmap fills 0–n of a fixed 0–46 chart. The editor ignored "enlarge it", and Jev didn't spot it in the settings. Idea: a code check that flags a heatmap/matrix filling only a small part of its chart.

## v3: summaries by code, Jev picked the sections, Haiku saw only their JSON (replaced by v4)

What went wrong on the self-attention thread (v2 to v4):
- **v2:** the "make the widgets more varied" chip ran as a whole-page change, which can't touch widgets, so 3 new sections were added instead.
- **v3:** the Play button was added in the right section.
- **v4:** "make the play button control that bar chart" was aimed at section 4 (a ready-made chart + bars), not section 7 (the widget with Play and head bars). It tried to add a widget past the page's limit of 4, changed nothing, and still made a new version.

The causes:
- The page summary showed only each widget's title and lesson.
- The "ready-made charts aren't widgets" rule was missing from the prompt.
- The plan step didn't know what the previous feedback changed.
- An empty run still saved a version.

The new plan step (`POST /api/feedback`):
1. **Summaries by code** (`feedback_pick.section_cards`, $0). One card per section, built from the page's JSON:
   - its number, name and description
   - a line per block: chart and bar-chart titles, slider labels, readouts, the start of each text
   - for each widget, its **controls** (sliders, Play) and **what it shows**, read from the plan stored on the widget
   - what the **previous feedback** changed there. The app sends `lastChange` from the version's own `followUp.feedback`.
   The cards always match the page, because they come from the same JSON.
2. **Jev picks the sections** (`feedback_pick.pick_sections`, one call, about $0.0004):
   - a yes/no per section: "does the feedback ask to change this section?"
   - a choice for the kind of feedback: parts / whole page / new section / no change
   `feedback.decide_scope` turns the answers into what the writer may change:

   | Jev's answer | What happens |
   |---|---|
   | p ≥ 0.6 on a section | it's worked on |
   | 0.3 to 0.6, nothing above | the card asks about those sections |
   | "none" | no change |
   | "page", or more than 6 sections | whole page |
   | round 2 (the reader answered) | the unsure sections are worked on |
   | no Jev key, or Jev fails | the writer picks among all sections, as in v2 |
3. **Haiku writes the plan** (`FEEDBACK_SYSTEM`). It gets every card, plus the **full JSON of only the chosen sections**, what it may change, and the widget count against the limit. The restored rules: ready-made charts, sliders and bars are "text" changes; a widget change on a section without one *adds* a widget; use the section that actually contains the control or chart the reader points at.
4. **Code checks** (`clean_plan` with the scope):
   - changes outside the scope are dropped
   - an add past the 4-widget limit becomes a change to the section's own blocks (`fit_widget_limit`)
   - if nothing usable is left, the code asks "Which part did you mean?" with chips for Jev's unsure sections first
5. **No empty versions.** A feedback run where nothing changed (the edit failed, or Jev's check put every part back) ends with "Nothing on the page was changed: <why>". A failed run doesn't count as one of the day's explainers.

**Live replay on the real thread** (Jev + Haiku, 7 cases, $0.0047):

| Feedback | Jev | Plan |
|---|---|---|
| "Add one play button interaction in section 5" | section 5, p=0.98 | change in "Scores as a matrix" (as text: the page is at 4 widgets) |
| "Add one play button in section 7" | section 7, p=0.97 | widget change: Play grows n |
| "In section 7… play button… handle that bar chart" | section 7, p=0.94 | **Play steps the head count so the per-head bars change** (v4 got this wrong) |
| "…control that bar chart below the line plot" (no number) | section 7, p=0.67; section 4 maybe (0.39) | the same right change in section 7 |
| "Nice page, I liked it." | none | nothing |
| "This part is confusing" | nothing clear | asks which part |
| "The graphs and interactions… looks same for every section" | nothing clear | asks which part (all sections) |

Open points:
- "Looks the same everywhere" asks for one part, but the reader likely means *every* widget. A "change every interactive part" choice (one widget change per section, each checked) would fit better.
- An "add a play button" request on a section whose interactive parts are ready-made blocks becomes a text change, which can't add a Play control. The card could say so.

## Why v1 went wrong (and what v2 does instead)

| v1 problem | v2 |
|---|---|
| The sorter only saw section *labels* and had to guess where "the chart" was | The understand call sees a digest of the real page (`targets.outline_digest`: every section, block and widget) |
| The build log pulled in sections the reader never mentioned | Every change must quote the reader's words (`quoted()`); a change without a quote is dropped |
| Your words were rewritten twice (sorter → finder), so details got lost | One call; its targets go straight to `apply_targets` (the finder is skipped) |
| Vague feedback became a silent whole-page rewrite | Vague feedback gets a question; a change that can't be matched is an error, never a rewrite |
| Praised sections could be rewritten | They go on the `keep` list and are locked |
| Saved preferences leaked into the edit | Not added for a feedback change (only for new pages and ordinary revisions) |
| Nothing checked whether the new version was better | Jev checks each changed section; one retry, then the old section is put back |

## Flow

```
Feedback button → dialog step 1: textarea → Next
   │
   ▼
POST /api/feedback  {text, recordId, title, warnings[], followUp (the page as a revise follow-up), quality, answer?, round?}
  limits: CHAT rate limit + daily $ caps (routes/ai.py _chat_turn), cost logged as UsageEvent "feedback"
  feedback.understand: one Haiku call (FEEDBACK_SYSTEM, json_schema "feedback", effort low, ~$0.0003-0.0006)
     sees: page digest, build log, saved prefs, <feedback>…</feedback> and <answer>…</answer> as data
     reply: {changes[{part, stage, after, what, quote}], keep[], pageWide, question{text, options[]}, preference, topic}
  feedback.clean_plan (code checks):
     - changes: quote must be from the reader's words; unknown sections dropped (targets.clean_targets);
       a widget named by its title moves to the section that has it (place_widget_changes); kept sections dropped
     - question options: each must be a real target, or part "page"
     - round 2: never a question
     - the model saw something to change but nothing survived → the code asks "Which part did you mean?"
       (a chip per section and per interactive part, plus "The whole page")
     - Lab (scene-plus) page → whole page only
  a `feedback` row is saved with the plan; no preference yet
  ← {feedbackId, plan, preference | null, usd}
   │
   ▼
Dialog step 2: the card
  "I'll change:"    ☑ <section> · <part>  [editable what]
  "This asks for a change to the whole page:"  ☑ Change the whole page
  <question>        [chip] [chip] [The whole page] [Something else…] → one more call with the answer (round 2)
  "I'll keep as it is:"  🔒 <section>
  ☑ Remember for my future pages: "<preference>"
  [Run | Save | Close] [Back] [Cancel]
   │
   ▼
POST /api/feedback/<id>/confirm  {targets, keep, pageWide, remember}   (no AI call; once per feedback, else 409)
  targets checked against the page's stage ids; preference saved if remember
  ← {feedback: {id, targets, keep, pageWide} | null, preference | null}
   │
   ▼  (if feedback is not null)
app.ts feedbackFollowUp → new record v+1 in the thread, request.followUp = {…revise follow-up, feedback}
   │
   ▼
pipeline.generate
  - no prefs_hint for a feedback change
  - not pageWide: targets = clean_targets(feedback.targets minus keep) → apply_targets (finder skipped)
      nothing usable, or a page type that can't be changed part by part → ReaderError (nothing changes)
  - pageWide: the old whole-page revise path
  - feedback_check.check: for each changed section, Jev (noul questions):
      fixes      the change deals with the reader's complaint
      not_worse  nothing else got less clear or less correct
    a clear no (p ≤ 0.2) → one more try from the previous version with the reason → still no → previous section put back
    + warning. A new section is never removed (note only). No Jev / Jev error → kept + "couldn't be checked".
  - one log line: "feedback <id>: changed=[…] verify={stage: pass | pass-after-retry | reverted | kept-new | unchecked}"
```

## Live check of the understand step (sample page, 2026-10-30)

| Feedback | Plan |
|---|---|
| "The temperature explorer is confusing, I can't tell what the lines mean." | widget change in "Picking the next token" (the right one) |
| "This was confusing." | question: which part? (4 chips + whole page) |
| "Loved the tokens section! But the descent path chart is too busy. Also … fewer sliders." | widget change in the section that has "Descent path"; tokens locked; preference saved |
| "Great page, thanks!" | nothing to change |
| "Explain everything to a ten year old." | whole page; preference |
| "This was confusing." + answer "the part about picking the next token" | section change in "Picking the next token" |

The first run found three problems, now fixed:
- Haiku put the "descent path" change on a section with no widget, which would have added one. Fixed in the prompt, and in code by `place_widget_changes`.
- Haiku locked every unmentioned section. Fixed in the prompt: only praised sections go in `keep`.
- Haiku asked again in round 2. Fixed in the prompt: it never asks again after an answer, and the code drops any question in round 2.

## Data model

**`feedback`** (migration `0006_feedback_plan.py` added `plan` and `confirmed`)
| Column | Notes |
|---|---|
| `id` | String(32) |
| `user_id` | FK `users.id`, ON DELETE CASCADE, indexed |
| `record_id` | the explainer the feedback was about (nullable) |
| `text` | the reader's words (plus "Answer: …" in round 2) |
| `page_request` | the planned changes' `what`, joined (for a quick read of the table) |
| `plan` | JSON: the cleaned plan + `stageIds`, `preference`, `topic`, `round` |
| `confirmed` | JSON: what the reader ran (`{id, targets, keep, pageWide}`), or `{targets: [], …}` when nothing; null until confirmed |
| `preference_id` | the preference it created (nullable) |
| `usd`, `created_at` | |

**`user_preferences`**: unchanged from v1 (`id`, `user_id`, `text`, `topic`, `status` active/off/replaced, `feedback_id`, timestamps). A new preference replaces the active one on the same topic; at most 8 are active.

## API

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/api/feedback` | `{text, recordId?, title, warnings[], followUp, quality?, answer?, round?}` | `{feedbackId, plan, preference: {text, topic} \| null, usd}` |
| POST | `/api/feedback/<id>/confirm` | `{targets[], keep[], pageWide, remember}` | `{feedback: FeedbackRun \| null, preference: Preference \| null}`; 404 not yours, 409 already used |
| GET / PUT / DELETE | `/api/preferences…` | unchanged | unchanged |

`plan = {changes[{part, stage, after, what, stageLabel}], keep[{stage, label}], pageWide, question: {text, options[{label, part, stage, after, what}]} | null, wholeOnly, unmatched}`.
`FollowUp.feedback = FeedbackRun = {id, targets[{part, stage, after, what}], keep[], pageWide}` (`shared/spec.ts`; validated by `followup.parse_feedback_plan`).

## Files

New:
- `server/explainer/feedback_check.py`
- `server/migrations/versions/0006_feedback_plan.py`
- `server/tests/test_feedback_check.py`

Changed:
- `server/explainer/feedback.py`: understand, clean_plan, confirm (replaces sort_feedback)
- `server/explainer/followup.py`: `parse_feedback_plan`, `FollowUp.feedback`
- `server/explainer/pipeline.py`: feedback targets instead of the finder, no prefs, errors instead of the fallback, the check
- `server/explainer/routes/ai.py`: the confirm route
- `server/explainer/db.py`: `plan`, `confirmed`
- `prompts/prompts.ts` (`FEEDBACK_SYSTEM`), `prompts/formats.ts` (`feedback`), regenerated `server/explainer/generated/`
- `shared/spec.ts`: `FollowUpTarget`, `FeedbackRun`
- `app/src/api.ts`, `app/src/shell/feedback.ts` (two-step dialog), `app/src/shell/app.ts`, `app/src/styles/app.css`
- `server/tests/test_feedback.py` (rewritten)

## Tests

- `server/tests/test_feedback.py`, 22 tests:
  - parsing
  - FollowUp.feedback validation
  - plan checks: labels, keep lock, unknown or unquoted changes leading to the code's question, trimmed quotes, widget placement, the question cleaned, round 2, page-wide, praise only, whole-only pages
  - the route saves nothing for the reader before confirm
  - round-2 answer
  - unusable reply
  - confirm: run + preference, nothing to run, owner only, used once
  - preferences, account deletion, later generations
- `server/tests/test_feedback_check.py`, 9 tests:
  - finder skipped
  - kept / unmatched errors
  - Lab pages
  - page-wide without prefs
  - prefs still reach ordinary revisions
  - retry passes
  - reverted after two fails
  - only the failing part reverted
  - no Jev

On 2026-10-30: `npm run test:server` 485 passed, `npm test` 181 passed, typecheck and build clean, migration upgrade → downgrade → upgrade checked on a fresh SQLite database.

## Configuration

- `PREFERENCES=0` stops preferences from being added to new pages.
- No new settings.

## Not done yet

- **A live run in the browser.** Open a page, then try Feedback with a clear complaint, a vague one, praise + complaint, and "Something else…". Watch the `feedback <id>:` log line and the cost of the edit + Jev checks.
- **Not built in this round:** highlighting the changed parts in v+1, "Undo this part", and a 💬 button on each section.
- **Owner export** of the `feedback` table (now with plan and confirmed) to see what readers complain about.
