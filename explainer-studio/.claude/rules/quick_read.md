---
description: Rules for Quick Read mode (lite-plus), the cheapest text-first mode
paths:
  - "server/explainer/targets.py"
  - "server/explainer/revise.py"
  - "server/explainer/pricing.py"
---

# Quick Read mode (`lite-plus`)

- Fast, cheapest of all modes: good write-up, little interaction.
- Pages have no widgets, so a follow-up "widget" request is treated as a section rewrite.
- Follow-up text changes are scoped to the targeted sections only (`server/explainer/targets.py`, `revise.py`).
- Option "How Hands on = Mostly reading" is the cheapest setting here.
