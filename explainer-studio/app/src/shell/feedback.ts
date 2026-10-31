// The Feedback button's dialog (Feedback v2, server/explainer/feedback.py). Step 1: the reader writes what they liked or
// didn't. The server sends back a plan (nothing has changed yet). Step 2: a card with the parts it would change (ticked,
// each "what" editable), the parts it keeps, a question when it couldn't tell which part is meant (chips, one or more, or "Something
// else…" for one more try), and the preference with a Remember box. Run confirms it; the app then makes the new version.
// Nothing is changed or saved until Run. Reader and model text only goes in as text nodes through h().

import type { ExplainerSpec, FollowUp, FollowUpTarget } from "../../../shared/spec";
import { confirmFeedback, sendFeedback, type FeedbackConfirmed, type FeedbackOption, type FeedbackResponse } from "../api";
import { h } from "../render/dom";

const MAX = 1000;
const ANSWER_MAX = 300;

/** What the dialog needs about the page: its record, spec, mode and the page as a revise follow-up. */
export interface FeedbackPage { recordId: string; spec: ExplainerSpec; quality?: string; followUp: FollowUp; lastChange?: { stages: string[]; what: string } }

/** Open the dialog for a finished page. onDone gets what the reader confirmed (and their words) after Run. */
export function openFeedbackDialog(page: FeedbackPage, onDone: (r: FeedbackConfirmed, text: string) => void): void {
  const dialog = h("dialog.account.feedback.shell", { "aria-labelledby": "feedback-title" }) as HTMLDialogElement;
  const close = () => { dialog.close(); dialog.remove(); };
  const body = h("div.feedback-body");
  let text = "";

  const ask = async (status: HTMLElement, button: HTMLButtonElement, answer?: string) => {
    button.disabled = true;
    status.textContent = "Reading your feedback…";
    try {
      const r = await sendFeedback({
        text, recordId: page.recordId, title: page.spec.title, warnings: (page.spec.warnings ?? []).slice(0, 20),
        followUp: page.followUp, ...(page.quality ? { quality: page.quality } : {}), ...(page.lastChange ? { lastChange: page.lastChange } : {}),
        ...(answer ? { answer, round: 2 as const } : {}),
      });
      showPlan(r, !!answer);
    } catch (err) {
      status.textContent = err instanceof Error ? err.message : String(err);
      button.disabled = false;
    }
  };

  // ---------- step 1: the feedback ----------
  const showWrite = () => {
    const input = h("textarea.add-input", { rows: 4, maxlength: MAX, placeholder: "What did you like, or what should be different? e.g. “The chart in section 2 is too busy” or “I prefer fewer sliders”" }) as HTMLTextAreaElement;
    input.value = text;
    const status = h("p.account-warn", { role: "status" });
    const send = h("button.btn-pill.is-primary", { type: "submit" }, "Next") as HTMLButtonElement;
    const form = h("form.account-form", { novalidate: true },
      h("div.add-form.feedback-box", null, input),
      h("p.account-warn", null, "You'll see what would change before anything happens."),
      status,
      h("div.account-actions", null, send, h("button.btn-pill", { type: "button", onclick: close }, "Cancel"))) as HTMLFormElement;
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      text = input.value.trim();
      if (!text) { input.focus(); return; }
      ask(status, send);
    });
    body.replaceChildren(form);
    input.focus();
  };

  // ---------- step 2: the plan ----------
  const showPlan = (r: FeedbackResponse, answered: boolean) => {
    const plan = r.plan;
    const rows: { target: FollowUpTarget; box: HTMLInputElement; what: HTMLTextAreaElement }[] = [];
    const picked = new Set<FeedbackOption>();  // the question's chips the reader chose (one or more; "page" alone)
    let whole = plan.pageWide;
    const status = h("p.account-warn", { role: "status" });
    const run = h("button.btn-pill.is-primary", { type: "button" }, "Run") as HTMLButtonElement;
    const parts: HTMLElement[] = [];

    const changeRow = (t: FollowUpTarget, label: string, checked: boolean) => {
      const box = h("input", { type: "checkbox", checked }) as HTMLInputElement;
      const what = h("textarea.add-input.feedback-what", { rows: 2, maxlength: ANSWER_MAX }) as HTMLTextAreaElement;
      what.value = t.what;
      box.addEventListener("change", () => { what.disabled = !box.checked; update(); });
      rows.push({ target: t, box, what });
      return h("div.feedback-change", null,
        h("label.feedback-change-head", null, box, h("strong", null, label), h("span.feedback-part", null, partName(t.part))),
        what);
    };

    if (plan.changes.length) {
      parts.push(h("p.feedback-label", null, "I'll change:"), ...plan.changes.map((c) => changeRow(c, c.stageLabel, true)));
    }
    const wholeBox = h("input", { type: "checkbox", checked: whole }) as HTMLInputElement;
    wholeBox.addEventListener("change", () => { whole = wholeBox.checked; update(); });
    const wholeRow = h("label.feedback-change-head.feedback-whole", null, wholeBox, h("strong", null, "Change the whole page"),
      h("span.feedback-part", null, "rewrites every section"));
    if (plan.pageWide) {
      parts.push(h("p.feedback-label", null, plan.wholeOnly ? "This kind of page can only be changed as a whole:" : "This asks for a change to the whole page:"),
        h("div.feedback-change", null, wholeRow, h("p.feedback-quote", null, `“${text}”`)));
    }

    if (plan.question) {
      const chips = h("div.feedback-chips");
      const other = h("div.feedback-other", { hidden: true });
      const chosen = h("ul.feedback-picked");
      const els = new Map<FeedbackOption, HTMLElement>();
      const pick = (o: FeedbackOption) => {
        if (picked.has(o)) picked.delete(o);
        else {
          // "The whole page" and particular parts don't mix.
          for (const x of [...picked]) if ((x.part === "page") !== (o.part === "page")) picked.delete(x);
          picked.add(o);
        }
        for (const [x, el] of els) el.classList.toggle("is-primary", picked.has(x));
        chosen.replaceChildren(...[...picked].filter((x) => x.what).map((x) => h("li", null, h("strong", null, x.label), ": ", x.what)));
        update();
      };
      for (const o of plan.question.options) {
        const el = h("button.btn-pill.feedback-chip", { type: "button", title: o.what || o.label, "aria-pressed": "false" }, o.label) as HTMLButtonElement;
        el.addEventListener("click", () => { pick(o); el.setAttribute("aria-pressed", String(picked.has(o))); });
        els.set(o, el);
        chips.append(el);
      }
      const answer = h("input.add-input", { type: "text", maxlength: ANSWER_MAX, placeholder: "Say which part you mean, and what should change" }) as HTMLInputElement;
      const again = h("button.btn-pill", { type: "button" }, "Ask again") as HTMLButtonElement;
      const againStatus = h("p.account-warn", { role: "status" });
      again.addEventListener("click", () => {
        const a = answer.value.trim();
        if (!a) { answer.focus(); return; }
        ask(againStatus, again, a);
      });
      other.append(answer, h("div.account-actions", null, again), againStatus);
      const otherBtn = h("button.btn-pill.feedback-chip", { type: "button", onclick: () => { other.hidden = false; otherBtn.hidden = true; answer.focus(); } }, "Something else…") as HTMLButtonElement;
      chips.append(otherBtn);
      parts.push(h("p.feedback-label", null, plan.question.text, h("span.feedback-part", null, " (pick one or more)")), chips, chosen, other);
    } else if (plan.unmatched) {
      parts.push(h("p.feedback-label", null, "I still couldn't tell which part you mean, so nothing will change unless you pick this:"),
        h("div.feedback-change", null, wholeRow));
    }

    if (plan.keep.length) {
      parts.push(h("p.feedback-label", null, "I'll keep as it is:"),
        h("ul.feedback-keep", null, ...plan.keep.map((k) => h("li", null, "🔒 ", k.label))));
    }

    const remember = h("input", { type: "checkbox", checked: true }) as HTMLInputElement;
    if (r.preference) {
      parts.push(h("label.feedback-remember", null, remember, "Remember for my future pages: ", h("em", null, `“${r.preference.text}”`)));
    }
    if (!plan.changes.length && !plan.pageWide && !plan.question && !plan.unmatched) {
      parts.push(h("p.feedback-label", null, r.preference ? "Nothing to change on this page." : "Thanks for the feedback. There's nothing to change on this page."));
    }
    if (answered && !plan.question) parts.unshift(h("p.account-warn", null, "Here's what I understood from your answer."));

    const targets = (): FollowUpTarget[] => {
      const ts = rows.filter((x) => x.box.checked).map((x) => ({ ...x.target, what: x.what.value.trim() || x.target.what }));
      for (const o of picked) if (o.part !== "page") ts.push({ part: o.part, stage: o.stage, after: o.after, what: o.what });
      return ts;
    };
    const pageWide = () => whole || [...picked].some((o) => o.part === "page");
    const update = () => {
      const changing = pageWide() || targets().length > 0;
      run.textContent = changing ? "Run" : r.preference ? "Save" : "Close";
      run.disabled = false;
      status.textContent = changing ? "A new version of the page will be made (it counts as one of today's explainers)." : "";
    };

    run.addEventListener("click", async () => {
      const ts = targets();
      const wide = pageWide();
      if (!wide && !ts.length && !(r.preference && remember.checked)) { close(); return; }
      run.disabled = true;
      status.textContent = "Saving…";
      try {
        const done = await confirmFeedback(r.feedbackId, { targets: wide ? [] : ts, keep: plan.keep.map((k) => k.stage), pageWide: wide, remember: !!r.preference && remember.checked });
        close();
        onDone(done, text);
      } catch (err) {
        status.textContent = err instanceof Error ? err.message : String(err);
        run.disabled = false;
      }
    });

    body.replaceChildren(h("div.account-form.feedback-plan", null,
      ...parts, status,
      h("div.account-actions", null, run, h("button.btn-pill", { type: "button", onclick: showWrite }, "Back"),
        h("button.btn-pill", { type: "button", onclick: close }, "Cancel"))));
    update();
  };

  dialog.append(
    h("div.account-head", null,
      h("h2", { id: "feedback-title" }, "Feedback"),
      h("button.icon-btn", { type: "button", "aria-label": "Close", onclick: close }, "✕")),
    body);
  dialog.addEventListener("close", () => dialog.remove());
  dialog.addEventListener("click", (e) => { if (e.target === dialog) close(); });
  document.body.append(dialog);
  dialog.showModal();
  showWrite();
}

function partName(part: FollowUpTarget["part"]): string {
  return part === "widget" ? "interactive part" : part === "text" ? "wording" : part === "section" ? "whole section" : "new section";
}
