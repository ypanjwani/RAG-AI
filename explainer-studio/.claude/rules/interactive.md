---
description: Rules for Interactive mode (quality-plus), the default mode using prebuilt components
paths:
  - "server/explainer/pipeline.py"
  - "server/explainer/clarity.py"
  - "server/explainer/factcheck.py"
  - "server/explainer/deepen.py"
  - "server/explainer/core/**"
  - "shared/spec.ts"
  - "shared/validate.ts"
  - "prompts/**"
---

# Interactive mode (`quality-plus`, the default)

- Content (planning and write-up) is Haiku. Interaction is **not** written from scratch: Haiku chooses and configures prebuilt components (sliders, charts, calculators), so the mode is cheaper and faster than Creative.
- Clarity check and fact check run on the text: Jev detects, Haiku judges and rewrites.
- Pages render from a JSON spec validated twice (TypeScript `shared/` and the Python port in `server/explainer/core/`); never edit `server/explainer/generated/` by hand.
- Review Rounds apply here (verify, audit, improve loop); "until clean" repeats until the audit is empty.
- Follow-ups: text targets get one scoped planner call; prebuilt widgets are kept via `restore_kept_widgets`.
