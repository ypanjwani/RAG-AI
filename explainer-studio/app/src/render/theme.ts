// Light/dark changes: the reader's mode toggle (html[data-mode]) or the OS setting.
// Parts that copy theme colours (AI widgets, Plotly charts) re-read them on change.

/** Calls `fn` after the page's light/dark mode changes; returns a function that stops watching. */
export function watchTheme(fn: () => void): () => void {
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  // The new CSS applies after the attribute change, so read colours a tick later.
  const mo = new MutationObserver(() => setTimeout(fn, 0));
  mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-mode"] });
  mq.addEventListener("change", fn);
  return () => {
    mo.disconnect();
    mq.removeEventListener("change", fn);
  };
}
