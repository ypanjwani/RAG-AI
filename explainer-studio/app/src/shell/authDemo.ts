// The small live example on the sign-in page: how temperature reshapes a language model's
// next-word probabilities (softmax of the scores divided by the temperature).

import { h } from "../render/dom";

const WORDS: [string, number][] = [["mat", 3.2], ["floor", 2.4], ["sofa", 2.0], ["roof", 1.1], ["moon", 0.2]];

export function temperatureDemo(): HTMLElement {
  const rows = WORDS.map(([w]) => {
    const fill = h("span.ad-fill");
    const pct = h("span.ad-pct");
    return { el: h("div.ad-row", null, h("span.ad-word", null, w), h("span.ad-track", null, fill), pct), fill, pct };
  });
  const slider = h("input.ad-slider", { type: "range", id: "ad-temp", min: 0.1, max: 2, step: 0.05, value: 0.8 }) as HTMLInputElement;
  const out = h("output.ad-out", { for: "ad-temp" });
  const note = h("p.ad-note", { "aria-live": "polite" });

  const update = () => {
    const t = Number(slider.value);
    const ex = WORDS.map(([, s]) => Math.exp(s / t));
    const sum = ex.reduce((a, b) => a + b, 0);
    const p = ex.map((e) => e / sum);
    p.forEach((v, i) => { rows[i].fill.style.width = `${(v * 100).toFixed(1)}%`; rows[i].pct.textContent = `${Math.round(v * 100)}%`; });
    out.textContent = t.toFixed(2);
    note.textContent = t < 0.5 ? `Cold: the model almost always says “mat” (${Math.round(p[0] * 100)}%). Safe, but repetitive.`
      : t < 1.2 ? "Balanced: “mat” is still the favourite, but other words get a fair chance."
      : `Hot: even “moon” now has ${Math.round(p[4] * 100)}%. More creative, and more likely to be odd.`;
  };
  slider.addEventListener("input", update);
  update();

  return h("div.ad", { role: "group", "aria-label": "A small live example of an explainer" },
    h("div.ad-head", null,
      h("span.ad-tag", null, h("span.ad-live", { "aria-hidden": "true" }), "Live example"),
      h("span.ad-tag", null, "Sampling")),
    h("div.ad-title", null, "How temperature changes the next word"),
    h("div.ad-prompt", null, "The cat sat on the ", h("b", null, " ? ")),
    h("div.ad-bars", null, ...rows.map((r) => r.el)),
    h("div.ad-control", null, h("label", { for: "ad-temp" }, "Temperature"), slider, out),
    note);
}
