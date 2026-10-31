// A panel that slides in from the right over the explainer, styled like the worked-examples panel
// (.x-ex-*): section notes ("Deeper explanation", "In other words") and, in the app, web search.

import { h } from "./dom";

export interface SidePanel {
  /** The scrolling area under the header. */
  list: HTMLElement;
  close(restoreFocus?: boolean): void;
}

export function openSidePanel(o: {
  host: HTMLElement;
  trigger: HTMLElement;
  title: string;
  sub?: string;
  content?: Node[];
  /** A footer row (status, buttons), shown under the list. */
  foot?: HTMLElement;
  onClose?(): void;
}): SidePanel {
  const list = h("div.x-ex-list", null, ...(o.content ?? []));
  const closeBtn = h("button.x-icon-btn", { type: "button", "aria-label": `Close ${o.title}` }, "✕");
  const scrim = h("div.x-ex-scrim");
  const panel = h("aside.x-ex-panel.x-sec-panel", { role: "dialog", "aria-modal": "true", "aria-label": o.sub ? `${o.title}: ${o.sub}` : o.title },
    h("div.x-ex-head", null, h("div", null, h("strong", null, o.title), o.sub ? h("div.x-sec-sub", null, o.sub) : null), closeBtn),
    list,
    o.foot ?? null);
  const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") close(); };
  function close(restoreFocus = true) {
    if (!panel.isConnected) return;
    panel.remove(); scrim.remove();
    document.removeEventListener("keydown", onKey);
    o.onClose?.();
    if (restoreFocus && o.trigger.isConnected) o.trigger.focus();
  }
  scrim.addEventListener("click", () => close());
  closeBtn.addEventListener("click", () => close());
  document.addEventListener("keydown", onKey);
  o.host.append(scrim, panel);
  closeBtn.focus();
  return { list, close };
}
