// "Ask about this explainer" widget. All UI is fixed; the spec only supplies
// the system prompt, starter questions and context.

import type { ChatRequest, ExplainerSpec } from "../../../shared/spec";
import { h, md } from "./dom";

export interface ChatMsg { role: "user" | "assistant"; content: string; error?: boolean }

export interface ChatOptions {
  spec: ExplainerSpec;
  messages: ChatMsg[];
  onMessages(msgs: ChatMsg[]): void;
  send(req: ChatRequest, onText: (t: string) => void): Promise<void>;
}

const HISTORY = 12; // recent turns sent with each question

export function createChat(o: ChatOptions): HTMLElement {
  const msgs = [...o.messages];
  let busy = false;
  const list = h("div.x-chat-list", { "aria-live": "polite" });
  const input = h("textarea.x-chat-input", { rows: 1, placeholder: "Ask a question…", "aria-label": "Your question" });
  const sendBtn = h("button.x-chat-send", { type: "button", "aria-label": "Send" }, "↑");

  function bubble(m: ChatMsg) {
    return h("div.x-chat-msg", { class: `is-${m.role}${m.error ? " is-error" : ""}` }, m.role === "assistant" ? md(m.content || "…") : m.content);
  }
  function draw() {
    list.replaceChildren(...msgs.map(bubble));
    if (!msgs.length) {
      list.append(h("div.x-chat-empty", null,
        h("div.x-chat-empty-icon", null, "💬"),
        h("p", null, "Ask anything about this explainer."),
        h("div.x-chat-starters", null, ...o.spec.chatbot.starters.map((q) => h("button.x-chip", { type: "button", onclick: () => ask(q) }, q)))));
    }
    list.scrollTop = list.scrollHeight;
    sendBtn.disabled = busy;
  }

  async function ask(q: string) {
    q = q.trim();
    if (!q || busy) return;
    busy = true;
    input.value = "";
    resize();
    msgs.push({ role: "user", content: q });
    const reply: ChatMsg = { role: "assistant", content: "" };
    msgs.push(reply);
    draw();
    const live = list.lastElementChild!;
    const history = msgs.slice(0, -1).filter((m) => !m.error).slice(-HISTORY).map(({ role, content }) => ({ role, content }));
    while (history.length && history[0].role !== "user") history.shift();
    try {
      await o.send({ context: o.spec.chatbot.context, messages: history }, (t) => {
        reply.content += t;
        live.replaceChildren(md(reply.content));
        list.scrollTop = list.scrollHeight;
      });
    } catch (e) {
      reply.content = e instanceof Error ? e.message : "Something went wrong.";
      reply.error = true;
    }
    busy = false;
    o.onMessages(msgs);
    draw();
  }

  const resize = () => { input.style.height = "auto"; input.style.height = `${Math.min(input.scrollHeight, 140)}px`; };
  input.addEventListener("input", resize);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(input.value); }
  });
  sendBtn.addEventListener("click", () => ask(input.value));

  const close = h("button.x-icon-btn", { type: "button", "aria-label": "Close chat" }, "✕");
  const root = h("div.x-chat", null,
    h("div.x-chat-head", null, h("strong", null, "Ask about this explainer"),
      h("button.x-icon-btn", { type: "button", title: "Clear conversation", "aria-label": "Clear conversation", onclick: () => { if (!busy) { msgs.length = 0; o.onMessages(msgs); draw(); } } }, "⟲"),
      close),
    list,
    h("div.x-chat-compose", null, input, sendBtn),
    h("div.x-chat-note", null, "Answers are AI-generated and can be wrong."));
  close.addEventListener("click", () => root.dispatchEvent(new CustomEvent("x-close", { bubbles: true })));
  draw();
  return root;
}
