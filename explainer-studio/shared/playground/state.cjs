// Copied from the ai-playground experiment (Bloging-substack/ECommerce/ai-playground), adapted for Explainer Studio's Playground mode.
// Shared reactive state: the single wiring layer between all components.
// Components never reference each other; they only read/write the store.
(function (root) {
  function createStore(initial) {
    let data = Object.assign({}, initial);
    const watchers = new Set();
    let depth = 0;            // batch nesting
    let pending = new Set();  // keys changed during a batch

    function flush() {
      const changed = pending;
      pending = new Set();
      for (const w of [...watchers]) {
        if (w.keys === null || [...w.keys].some((k) => changed.has(k))) w.run();
      }
    }

    // Merge a patch (object) or compute one from current state (function).
    function set(patch) {
      const next = typeof patch === 'function' ? patch(data) : patch;
      for (const k in next) {
        if (!Object.is(data[k], next[k])) {
          data[k] = next[k];
          pending.add(k);
        }
      }
      if (depth === 0 && pending.size) flush();
    }

    // Group several set() calls into one redraw.
    function batch(fn) {
      depth++;
      try { fn(); } finally { depth--; }
      if (depth === 0 && pending.size) flush();
    }

    // Run fn(state) now and again whenever a key it READ changes.
    // Keys are tracked automatically, so a chart's draw() just reads state.
    function watch(fn) {
      const w = {
        keys: new Set(),
        run() {
          const seen = new Set();
          const view = new Proxy(data, {
            get: (t, k) => (seen.add(k), t[k]),
            set: (t, k) => { throw new Error(`state is read-only (tried to set "${String(k)}"); return a patch instead`); },
          });
          try { fn(view); } finally { w.keys = seen; }
        },
      };
      watchers.add(w);
      w.run();
      return () => watchers.delete(w);
    }

    // Run fn(state) on every change to any key (e.g. for debugging).
    function subscribe(fn) {
      const w = { keys: null, run: () => fn(data) };
      watchers.add(w);
      return () => watchers.delete(w);
    }

    return { get: () => data, set, batch, watch, subscribe };
  }

  if (typeof module !== 'undefined' && module.exports) module.exports = { createStore };
  else root.createStore = createStore;
})(typeof window !== 'undefined' ? window : globalThis);
