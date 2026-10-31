---
description: Rules for Code Lab 7.0 (code-lab-7), Haiku-written widget code with checks (beta)
paths:
  - "server/explainer/codelab.py"
  - "server/explainer/codelab_edit.py"
  - "scripts/check-widget.mjs"
  - "server/tests/test_codelab*.py"
---

# Code Lab 7.0 (`code-lab-7`, beta)

- The writer marks at most 4 sections that need an interactive part (never intros or summaries). Each marked section's part is Haiku's own code: plan, build, checks, repairs, backup, at the page's Effort.
- The builder uses `CODE_LAB_FREE_SYSTEM`, not `CODE_LAB_SYSTEM`: the plan fixes what each part shows (ids, ranges, maths); Haiku designs how it looks (any visual form, kit optional, ~300 lines, no fixed-axis rule, data clearly visible at the starting setting).
- Applies: writer marks, plan, checks, repairs, Jev review, backup, cap, repeat check, Thinking switch.
- `scripts/check-widget.mjs` runs each widget in jsdom and **fails closed** (a check that can't run is a failure). Every control must change what is drawn; Vega-Lite dropped encodings fail; widgets may not show notes about the plan/brief.
- Unmarked sections go through Jev's gate. Page cap is 4 interactive parts and counts **kept** parts: a failed part triggers `_refill` on the next gate-opened section.
- Maths checks (claims, sanity rules) are hard checks; Jev's maths review is dormant (`math_review` not called).
- Code: `server/explainer/codelab.py` (`CodeLabSettings.for_effort(free=True)`); edits in `codelab_edit.py` (`CODE_LAB_EDIT`: update plan, rebuild from old code, rerun checks). A failed edit keeps the old widget byte for byte.
- A widget added to a section without one counts against `MAX_MARKED`.
