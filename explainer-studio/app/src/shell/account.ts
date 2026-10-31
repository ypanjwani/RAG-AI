// Accounts in the app shell: the sign-in / create-account screen, the account panel (today's free
// allowance, password, sign out, delete account) and the offer to import pre-account history.

import { ApiError, allowance, changePassword, currentUser, deleteAccount, login, logout, logoutEverywhere, refreshAllowance, register, type Allowance } from "../auth";
import { deletePreference, listPreferences, setPreferenceStatus, type Preference } from "../api";
import { h } from "../render/dom";
import { temperatureDemo } from "./authDemo";
import { modeButton } from "./mode";

const PASSWORD_MIN = 8;
const PASSWORD_MAX_BYTES = 72;

/** Problems the server would refuse anyway, caught before sending. */
function checkPassword(pw: string): string | null {
  if (pw.length < PASSWORD_MIN) return `Use at least ${PASSWORD_MIN} characters.`;
  if (new TextEncoder().encode(pw).length > PASSWORD_MAX_BYTES) return `Use at most ${PASSWORD_MAX_BYTES} bytes (about ${PASSWORD_MAX_BYTES} plain letters).`;
  if (!pw.trim()) return "The password can't be only spaces.";
  return null;
}

function field(label: string, input: HTMLInputElement, control: HTMLElement = input): { el: HTMLElement; error: (msg: string | null) => void } {
  const id = `f-${Math.random().toString(36).slice(2, 8)}`;
  input.id = id;
  const err = h("div.auth-field-error", { id: `${id}-err`, role: "alert" });
  input.setAttribute("aria-describedby", `${id}-err`);
  return {
    el: h("div.auth-field", null, h("label", { for: id }, label), control, err),
    error: (msg) => { err.textContent = msg ?? ""; input.setAttribute("aria-invalid", String(!!msg)); },
  };
}

function passwordInput(autocomplete: string): HTMLElement & { input: HTMLInputElement } {
  const input = h("input.auth-input", { type: "password", autocomplete, required: true, maxlength: 200 }) as HTMLInputElement;
  const toggle = h("button.auth-show", { type: "button", "aria-label": "Show password", "aria-pressed": "false" }, "Show");
  toggle.addEventListener("click", () => {
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    toggle.textContent = show ? "Hide" : "Show";
    toggle.setAttribute("aria-pressed", String(show));
    toggle.setAttribute("aria-label", show ? "Hide password" : "Show password");
  });
  return Object.assign(h("div.auth-pw", null, input, toggle), { input });
}

const ARROW = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 6l6 6-6 6"/></svg>';
const CHECK = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';

/** Four bars and a word under a new password: length first, then a mix of character kinds. */
function strengthMeter(input: HTMLInputElement) {
  const bars = [0, 1, 2, 3].map(() => h("i"));
  const text = h("div.auth-strength-text");
  const el = h("div.auth-strength", { "aria-live": "polite" }, h("div.auth-strength-bars", { "aria-hidden": "true" }, ...bars), text);
  const draw = () => {
    const v = input.value;
    let s = 0;
    if (v.length >= PASSWORD_MIN) {
      s = 1;
      if (v.length >= 12) s++;
      if (/[a-z]/.test(v) && /[A-Z]/.test(v)) s++;
      if (/\d/.test(v) && /[^A-Za-z0-9]/.test(v)) s++;
    }
    const level = v ? Math.max(s, 1) : 0;
    el.dataset.level = String(v.length < PASSWORD_MIN && v ? 0 : level);
    bars.forEach((b, i) => b.classList.toggle("on", i < level));
    const left = PASSWORD_MIN - v.length;
    text.textContent = !v ? `At least ${PASSWORD_MIN} characters.`
      : left > 0 ? `${left} more character${left === 1 ? "" : "s"} to go.`
      : ["Okay.", "Okay.", "Good.", "Strong.", "Very strong."][s];
  };
  input.addEventListener("input", draw);
  draw();
  return { el, draw };
}

/** The signed-out screen: a brand panel with a live example, and the sign-in / sign-up form. onDone runs after success. */
export function authView(opts: { onDone: () => void; mode?: "login" | "register" }): HTMLElement {
  let mode = opts.mode ?? "login";
  const copy = {
    login: { title: "Welcome back", sub: "Sign in to make explainers and pick up where you left off.", btn: "Sign in", busy: "Signing in…", ph: "Your password" },
    register: { title: "Create your free account", sub: "Turn any AI concept or research paper into a page you can play with.", btn: "Create account", busy: "Creating account…", ph: `At least ${PASSWORD_MIN} characters` },
  };
  const email = h("input.auth-input", { type: "email", autocomplete: "email", required: true, maxlength: 320, inputmode: "email", placeholder: "you@example.com" }) as HTMLInputElement;
  const pw = passwordInput("current-password");
  const emailF = field("Email", email);
  const pwF = field("Password", pw.input, pw);
  const meter = strengthMeter(pw.input);
  pwF.el.insertBefore(meter.el, pwF.el.lastChild);
  const formError = h("div.auth-error", { role: "alert" });
  const submit = h("button.auth-submit", { type: "submit" }) as HTMLButtonElement;
  const idle = () => submit.replaceChildren(h("span", null, copy[mode].btn), h("span.icon", { "aria-hidden": "true", html: ARROW }));
  const title = h("h1.auth-title");
  const sub = h("p.auth-sub");
  const head = h("div.auth-head", null, title, sub);
  const tabs = (["login", "register"] as const).map((m) =>
    h("button", { type: "button", role: "tab", onclick: () => { mode = m; draw(); email.focus(); } }, m === "login" ? "Sign in" : "Create account"));
  const seg = h("div.auth-seg", { role: "tablist", "aria-label": "Sign in or create an account" }, h("span.auth-seg-pill", { "aria-hidden": "true" }), ...tabs);

  const draw = () => {
    const c = copy[mode];
    seg.dataset.mode = mode;
    tabs.forEach((t, i) => t.setAttribute("aria-selected", String((i === 0) === (mode === "login"))));
    title.textContent = c.title;
    sub.textContent = c.sub;
    pw.input.placeholder = c.ph;
    pw.input.autocomplete = mode === "login" ? "current-password" : "new-password";
    meter.el.hidden = mode !== "register";
    meter.draw();
    idle();
    head.classList.remove("is-in"); void head.offsetWidth; head.classList.add("is-in");
    emailF.error(null); pwF.error(null); formError.textContent = "";
  };

  const form = h("form.auth-form", { novalidate: true }, formError, emailF.el, pwF.el, submit) as HTMLFormElement;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    emailF.error(null); pwF.error(null); formError.textContent = "";
    const addr = email.value.trim();
    let bad = false;
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(addr)) { emailF.error("Enter a valid email address."); bad = true; }
    const pwProblem = mode === "register" ? checkPassword(pw.input.value) : pw.input.value ? null : "Enter your password.";
    if (pwProblem) { pwF.error(pwProblem); bad = true; }
    if (bad) { (form.querySelector("[aria-invalid=true]") as HTMLElement | null)?.focus(); return; }
    submit.disabled = true;
    submit.replaceChildren(h("span.auth-spinner", { "aria-hidden": "true" }), h("span", null, copy[mode].busy));
    try {
      await (mode === "login" ? login(addr, pw.input.value) : register(addr, pw.input.value));
      opts.onDone();
    } catch (err) {
      if (err instanceof ApiError && err.fields) {
        if (err.fields.email) emailF.error(err.fields.email);
        if (err.fields.password) pwF.error(err.fields.password);
      }
      formError.textContent = err instanceof Error ? err.message : String(err);
      if (err instanceof ApiError && err.code === "email_taken") {
        formError.append(" ", h("button.auth-link", { type: "button", onclick: () => { mode = "login"; draw(); pw.input.focus(); } }, "Sign in instead"));
      }
    } finally {
      submit.disabled = false;
      idle();
    }
  });
  draw();
  queueMicrotask(() => email.focus());

  const logo = () => h("div.brand-logo", null, h("span.brand-mark", { "aria-hidden": "true" }, "✦"), "Explainer Studio");
  const perk = (t: string) => h("span", null, h("span.icon", { "aria-hidden": "true", html: CHECK }), t);
  return h("div.auth-page.shell", null,
    h("aside.auth-brand", null,
      logo(),
      h("div.auth-pitch", null,
        h("h2", null, "Don't just read about AI.", h("br"), h("em", null, "Play"), " with it."),
        h("p", null, "Type a concept or drop in a research paper. Get an interactive page with sliders, live charts and quizzes that make the idea click.")),
      temperatureDemo(),
      h("div.auth-perks", null, perk("Free: 10 explainers a day"), perk("No card needed"), perk("History saved to your account"))),
    h("main.auth-side", null,
      h("div.auth-top", null, modeButton()),
      h("div.auth-center", null,
        h("div.auth-box", null,
          h("div.auth-mobile-logo", null, logo()),
          seg, head, form,
          h("div.auth-or", null, "or"),
          h("a.auth-demo", { href: "#/demo" },
            h("span", null, h("strong", null, "See a sample explainer first"), h("small", null, "No account needed. Takes 30 seconds.")),
            h("span.auth-demo-arrow", { "aria-hidden": "true", html: ARROW })))),
      h("p.auth-legal", null, "AI topics only · Pages are made by AI and checked by code, but can still be wrong.")));
}

/** "3 of 10 explainers today" with a small bar. */
export function allowanceMeter(a: Allowance | null): HTMLElement | null {
  if (!a) return null;
  const pct = a.generationsLimit ? Math.min(100, (100 * a.generationsUsed) / a.generationsLimit) : 100;
  const out = a.generationsUsed >= a.generationsLimit || a.usdUsed >= a.usdLimit;
  return h("div.allow", { class: out ? "is-out" : "" },
    h("div.allow-label", null, out ? "Today's free allowance is used up" : `${a.generationsUsed} of ${a.generationsLimit} free explainers today`),
    h("div.allow-bar", { role: "progressbar", "aria-valuemin": "0", "aria-valuemax": String(a.generationsLimit), "aria-valuenow": String(a.generationsUsed), "aria-label": "Explainers made today" },
      h("span", { style: `width: ${pct}%` })));
}

/**
 * "My preferences": what the reader saved through Feedback. Active ones shape their future pages; each can be
 * turned off (kept for later) or deleted. Loaded when the section is opened.
 */
function preferencesSection(): HTMLElement {
  const list = h("div.pref-list", null, h("p.account-warn", null, "Loading…"));
  const draw = (prefs: Preference[]) => {
    if (!prefs.length) {
      list.replaceChildren(h("p.account-warn", null, "No saved preferences yet. Use Feedback on a page and tick “Remember this”."));
      return;
    }
    list.replaceChildren(...prefs.map((p) => {
      const row = h("div.pref-row", { class: p.status === "off" ? "is-off" : "" },
        h("span.pref-text", null, p.text),
        h("button.btn-pill", { type: "button", onclick: () => act(() => setPreferenceStatus(p.id, p.status === "active" ? "off" : "active")) }, p.status === "active" ? "Turn off" : "Turn on"),
        h("button.btn-pill.is-danger", { type: "button", "aria-label": `Delete preference: ${p.text}`, onclick: () => act(() => deletePreference(p.id)) }, "Delete"));
      return row;
    }));
  };
  const load = () => listPreferences().then(draw).catch((err) => {
    list.replaceChildren(h("p.account-warn", null, err instanceof Error ? err.message : String(err)));
  });
  const act = async (run: () => Promise<unknown>) => {
    try { await run(); } catch (err) { alertIn(list, err); return; }
    await load();
  };
  const section = h("details.account-section", null,
    h("summary", null, "My preferences"),
    h("p.account-warn", null, "Saved from your feedback. The ones that are on are used for your future pages only."),
    list) as HTMLDetailsElement;
  section.addEventListener("toggle", () => { if (section.open) load(); });
  return section;
}

function alertIn(list: HTMLElement, err: unknown) {
  list.prepend(h("p.account-warn.pref-error", { role: "alert" }, err instanceof Error ? err.message : String(err)));
}

/** The account panel: allowance, change password, preferences, sign out (here or everywhere), delete the account. */
export async function openAccountPanel(): Promise<void> {
  const user = currentUser();
  if (!user) return;
  const dialog = h("dialog.account.shell", { "aria-labelledby": "account-title" }) as HTMLDialogElement;
  const close = () => { dialog.close(); dialog.remove(); };
  const status = h("p.account-status", { role: "status" });
  const meter = h("div", null, allowanceMeter(allowance()));
  refreshAllowance().then((a) => meter.replaceChildren(allowanceMeter(a) ?? ""));

  // Change password
  const cur = passwordInput("current-password");
  const next = passwordInput("new-password");
  const curF = field("Current password", cur.input, cur);
  const nextF = field("New password", next.input, next);
  const pwForm = h("form.account-form", { novalidate: true }, curF.el, nextF.el, h("button.btn-pill", { type: "submit" }, "Change password")) as HTMLFormElement;
  pwForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    curF.error(null); nextF.error(null); status.textContent = "";
    const problem = checkPassword(next.input.value);
    if (!cur.input.value) { curF.error("Enter your current password."); return; }
    if (problem) { nextF.error(problem); return; }
    try {
      await changePassword(cur.input.value, next.input.value);
      pwForm.reset();
      status.textContent = "Password changed. Other devices were signed out.";
    } catch (err) {
      const f = err instanceof ApiError ? err.fields : undefined;
      if (f?.currentPassword) curF.error(f.currentPassword);
      else if (f?.newPassword) nextF.error(f.newPassword);
      else status.textContent = err instanceof Error ? err.message : String(err);
    }
  });

  // Delete account (asks for the password; the server checks it)
  const delPw = passwordInput("current-password");
  const delF = field("Type your password to confirm", delPw.input, delPw);
  const delForm = h("form.account-form.is-danger", { novalidate: true }, delF.el,
    h("button.btn-pill.is-danger", { type: "submit" }, "Delete my account and history")) as HTMLFormElement;
  delForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    delF.error(null);
    if (!delPw.input.value) { delF.error("Enter your password."); return; }
    try {
      await deleteAccount(delPw.input.value);
      close();
    } catch (err) {
      delF.error(err instanceof Error ? err.message : String(err));
    }
  });

  dialog.append(
    h("div.account-head", null,
      h("h2", { id: "account-title" }, "Account"),
      h("button.icon-btn", { type: "button", "aria-label": "Close", onclick: close }, "✕")),
    h("p.account-email", null, user.email, h("span.account-plan", null, "Free plan")),
    meter,
    status,
    h("details.account-section", null, h("summary", null, "Change password"), pwForm),
    preferencesSection(),
    h("div.account-actions", null,
      h("button.btn-pill", { type: "button", onclick: async () => { await logout(); close(); } }, "Sign out"),
      h("button.btn-pill", { type: "button", onclick: async () => { await logoutEverywhere().catch(() => undefined); close(); } }, "Sign out on all devices")),
    h("details.account-section", null, h("summary", null, "Delete account"),
      h("p.account-warn", null, "This permanently deletes your account and every explainer in it. It can't be undone."), delForm));
  dialog.addEventListener("close", () => dialog.remove());
  dialog.addEventListener("click", (e) => { if (e.target === dialog) close(); });
  document.body.append(dialog);
  dialog.showModal();
}

/** Offer to move history saved in this browser (before accounts) into the account. */
export function legacyOffer(count: number, onImport: (progress: (n: number) => void) => Promise<number>, onDismiss: () => void): HTMLElement {
  const text = h("span", null, `Found ${count} explainer${count > 1 ? "s" : ""} saved in this browser before accounts. Add ${count > 1 ? "them" : "it"} to your account?`);
  const add = h("button.btn-pill", { type: "button" }, "Add to my account") as HTMLButtonElement;
  const no = h("button.btn-pill", { type: "button" }, "Not now") as HTMLButtonElement;
  const el = h("div.legacy-offer.shell", { role: "region", "aria-label": "Import earlier explainers" }, text, h("div.legacy-actions", null, add, no));
  add.addEventListener("click", async () => {
    add.disabled = no.disabled = true;
    const n = await onImport((k) => { text.textContent = `Adding… ${k} of ${count}`; });
    text.textContent = n === count ? `Added ${n} explainer${n > 1 ? "s" : ""} to your account.` : `Added ${n} of ${count}. The rest will be offered again next time.`;
    no.disabled = false;
    no.textContent = "Close";
    add.remove();
  });
  no.addEventListener("click", () => { el.remove(); onDismiss(); });
  return el;
}
