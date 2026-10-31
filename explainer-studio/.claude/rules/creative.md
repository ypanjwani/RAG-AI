---
description: Rules for Creative mode (freeform-plus), where Haiku writes widgets from scratch
paths:
  - "app/src/render/components/widget.ts"
  - "shared/widget-kit.js"
  - "server/explainer/deepen.py"
  - "server/explainer/js.py"
---

# Creative mode (`freeform-plus`)

- Haiku plans and writes everything, including interactive widgets, from scratch (no prebuilt components). Costs more and is slower, but best for visuals and interaction.
- Clarity check and fact check apply, same as Interactive.
- Widgets are `widget` blocks run in a sandboxed iframe (`widget.ts`, `shared/widget-kit.js`). Model output is untrusted: sanitize and escape.
- Improve passes cannot rewrite built widgets (`BUILT_BLOCKS` in `deepen.py`).
- Follow-ups: Creative widgets are kept but cannot be edited yet.
- Won the Oct 24 comparison against Quick read, Playground and CL7 (see memory notes).
