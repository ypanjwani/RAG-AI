"""Design helpers applied around the design call (which lives in pipeline.py): Jev's page brief
overrides, and stamping a palette onto an assembled spec."""

from __future__ import annotations


def design_with_brief(d: dict, b: dict) -> dict:
    """Page brief: Jev's confident theme/font/heading picks override the design call's."""
    out = dict(d)
    if b.get("theme"):
        out["theme"] = b["theme"]
    if b.get("fontPairing"):
        out["fontPairing"] = b["fontPairing"]
    if b.get("headingStyle"):
        out["headingStyle"] = b["headingStyle"]
    return out


def apply_brief(spec: dict, b: dict) -> dict:
    """Page brief: stamp text size, spacing and emphasis style onto the assembled spec."""
    return {**spec, "design": {**spec["design"], "textSize": b["textSize"], "density": b["density"], "emphasis": b["emphasis"]}}


def apply_prefs(spec: dict, prefs: dict) -> dict:
    """Stamp a palette and light/dark onto the assembled spec (revisions keep the reader's custom accent)."""
    design = {**spec["design"], "theme": prefs["palette"]}
    if prefs["palette"] == "custom" and prefs.get("accent"):
        design["accent"] = prefs["accent"]
    design["mode"] = prefs["colorMode"]
    return {**spec, "design": design}
