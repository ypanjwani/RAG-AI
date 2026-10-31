// Engagement blocks: flipcards, reveal, quiz, predict.

import { h, md, mdInline } from "../dom";
import type { Renderer } from "./types";

export const flipcards: Renderer<"flipcards"> = (b) =>
  h("div.x-flipgrid", null, ...b.cards.map((c) => {
    const card = h("button.x-flip", { type: "button", "aria-pressed": "false" },
      h("span.x-flip-inner", null,
        h("span.x-flip-front", null, h("span.x-flip-term", null, c.front), h("span.x-flip-hint", null, "Click to flip")),
        h("span.x-flip-back", null, mdInline(c.back))));
    card.addEventListener("click", () => card.setAttribute("aria-pressed", String(card.getAttribute("aria-pressed") !== "true")));
    return card;
  }));

export const reveal: Renderer<"reveal"> = (b) => {
  const answer = h("div.x-reveal-answer", { hidden: true }, md(b.answer));
  const btn = h("button.x-btn.x-btn-soft", { type: "button" }, "Reveal answer");
  btn.addEventListener("click", () => {
    answer.hidden = !answer.hidden;
    btn.textContent = answer.hidden ? "Reveal answer" : "Hide answer";
  });
  return h("div.x-reveal", null, h("div.x-reveal-prompt", null, h("span.x-badge", null, "Think first"), mdInline(b.prompt)), btn, answer);
};

/** One multiple-choice question with feedback; shared by quiz and predict. */
function mcq(opts: {
  question: string; choices: string[]; answer: number; explain: string;
  saved: number | undefined; onAnswer: (i: number) => void; revealOnly?: boolean;
}): HTMLElement {
  const feedback = h("div.x-mcq-feedback", { "aria-live": "polite" });
  const buttons = opts.choices.map((c, i) =>
    h("button.x-mcq-choice", { type: "button", onclick: () => choose(i, true) }, h("span.x-mcq-letter", null, String.fromCharCode(65 + i)), mdInline(c)));
  function choose(i: number, fresh: boolean) {
    buttons.forEach((btn, j) => {
      btn.disabled = true;
      btn.classList.toggle("is-correct", j === opts.answer);
      btn.classList.toggle("is-wrong", j === i && i !== opts.answer);
    });
    const right = i === opts.answer;
    const verdict = opts.revealOnly ? (right ? "Good prediction!" : "Interesting guess, here's what happens:") : right ? "Correct!" : "Not quite.";
    feedback.replaceChildren(h("strong", { class: right ? "is-right" : "is-off" }, verdict), md(opts.explain));
    if (!right && !opts.revealOnly) {
      feedback.append(h("button.x-btn.x-btn-ghost", { type: "button", onclick: reset }, "Try again"));
    }
    if (fresh) opts.onAnswer(i);
  }
  function reset() {
    buttons.forEach((btn) => { btn.disabled = false; btn.classList.remove("is-correct", "is-wrong"); });
    feedback.replaceChildren();
  }
  const el = h("div.x-mcq", null, h("div.x-mcq-q", null, mdInline(opts.question)), h("div.x-mcq-choices", null, ...buttons), feedback);
  if (opts.saved !== undefined) choose(opts.saved, false);
  return el;
}

export const quiz: Renderer<"quiz"> = (b, ctx) =>
  h("section.x-quiz", null,
    h("div.x-quiz-head", null, h("span.x-badge", null, "Check yourself"), b.questions.length > 1 ? h("span.x-muted", null, `${b.questions.length} questions`) : null),
    ...b.questions.map((q, i) => {
      const qkey = `${ctx.key}:${i}`;
      return mcq({ question: q.q, choices: q.choices, answer: q.answer, explain: q.explain, saved: ctx.answer(qkey), onAnswer: (c) => ctx.setAnswer(qkey, c) });
    }));

export const predict: Renderer<"predict"> = (b, ctx) => {
  const qkey = `${ctx.key}:0`;
  return h("section.x-predict", null,
    h("div.x-quiz-head", null, h("span.x-badge", null, "Predict first")),
    mcq({ question: b.question, choices: b.choices, answer: b.answer, explain: b.explain, saved: ctx.answer(qkey), onAnswer: (c) => ctx.setAnswer(qkey, c), revealOnly: true }));
};
