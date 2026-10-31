// Code Lab widget kit: small helpers every AI-written widget can use as window.XW, so it doesn't redraw axes, sliders and
// labels from scratch (which is where small models get layout wrong). Plain ES5-style JS, no dependencies.
// The same file is loaded into the sandboxed frame (app/src/render/components/widget.ts, `?raw`) and into the jsdom
// check (scripts/check-widget.mjs). The builder's prompt (CODE_LAB_SYSTEM in prompts/prompts.ts) documents this API:
// keep the two in step.
(function () {
  var SVGNS = "http://www.w3.org/2000/svg";

  function el(tag, attrs, parent, text) {
    var svg = /^(svg|g|path|line|rect|circle|text|polyline|polygon|defs|marker|title)$/.test(tag);
    var e = svg ? document.createElementNS(SVGNS, tag) : document.createElement(tag);
    for (var k in attrs || {}) e.setAttribute(k, attrs[k]);
    if (text != null) e.textContent = text;
    if (parent) parent.appendChild(e);
    return e;
  }

  function fmt(v, d) {
    if (v === Infinity) return "∞";
    if (v === -Infinity) return "-∞";
    if (!isFinite(v)) return "?";
    // Keeps the digits asked for ("0.500"): the widget checker reads a readout's precision from them.
    var s = Number(v).toFixed(d == null ? 2 : d);
    return /^-0(\.0*)?$/.test(s) ? s.slice(1) : s;
  }

  // Decimals in a slider's step (1 -> 0, 0.05 -> 2), so its value shows "16", not "16.00".
  function stepDigits(step) {
    var s = +step, n = 0;
    if (!(s > 0) || !isFinite(s)) return 2;
    while (n < 6 && Math.abs(s * Math.pow(10, n) - Math.round(s * Math.pow(10, n))) > 1e-9) n++;
    return n;
  }

  // The gap between round ticks (1, 2 or 5 times a power of ten) that gives about n steps over [lo, hi].
  function tickStep(lo, hi, n) {
    var raw = Math.abs(hi - lo) / Math.max(1, n || 4);
    if (!(raw > 0) || !isFinite(raw)) return 0;
    var p = Math.pow(10, Math.floor(Math.log(raw) / Math.LN10)), f = raw / p;
    return (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * p;
  }

  // Round tick values inside [lo, hi], about n steps apart (like 0, 0.25, 0.5 ... rather than 0.3, 0.5, 0.8).
  function ticks(lo, hi, n) {
    if (hi < lo) { var t = lo; lo = hi; hi = t; }
    var step = tickStep(lo, hi, n), out = [];
    if (!step) return isFinite(lo) ? [lo] : [];
    var first = Math.ceil(lo / step - 1e-9), last = Math.floor(hi / step + 1e-9);
    for (var i = first; i <= last; i++) out.push(+(i * step).toPrecision(12));
    return out.length >= 2 ? out : [lo, hi];
  }

  // Decimals a tick label needs for this step (0.25 -> 2, 5 -> 0). A widget's xdigits/ydigits is ignored for ticks:
  // fewer decimals than the step needs would print the same label twice ("2, 2"), more only adds zeros.
  function tickDigits(lo, hi, n) {
    var step = tickStep(lo, hi, n), need = 0;
    while (step && need < 6 && Math.abs(step * Math.pow(10, need) - Math.round(step * Math.pow(10, need))) > 1e-6) need++;
    return need;
  }

  // Labelled slider with its live value. opts: id, label, min, max, step, value, unit, digits, onInput(value). Returns {el, input, get, set}.
  function slider(parent, opts) {
    var id = opts.id || "xw" + Math.random().toString(36).slice(2, 8);
    var row = el("div", { "class": "xw-ctl" }, parent);
    el("label", { "for": id }, row, opts.label);
    var input = el("input", { type: "range", id: id, min: opts.min, max: opts.max, step: opts.step == null ? "any" : opts.step, value: opts.value }, row);
    var out = el("span", { "class": "xw-val" }, row);
    var digits = opts.digits != null ? opts.digits : opts.step != null && opts.step !== "any" ? stepDigits(opts.step) : 2;
    function show() { out.textContent = fmt(+input.value, digits) + (opts.unit ? " " + opts.unit : ""); }
    input.addEventListener("input", function () { show(); if (opts.onInput) opts.onInput(+input.value); });
    show();
    return { el: row, input: input, get: function () { return +input.value; }, set: function (v) { input.value = v; show(); } };
  }

  // Button. Returns the element.
  function button(parent, label, onClick) {
    var b = el("button", { type: "button", "class": "xw-btn" }, parent, label);
    b.addEventListener("click", onClick);
    return b;
  }

  // A chart frame: an <svg> (viewBox 0 0 w h, default 600 x 300) with axes, ticks and axis titles drawn at a readable size.
  // opts: xmin, xmax, ymin, ymax, xlabel, ylabel, xticks, yticks, xdigits, ydigits, w, h, id.
  // Returns {svg, g, x(v), y(v), w, h, left, right, top, bottom}: draw into `g` using x() and y().
  function chart(parent, opts) {
    var w = opts.w || 600, h = opts.h || 300;
    var m = { left: 64, right: 16, top: 16, bottom: 52 };
    var svg = el("svg", { viewBox: "0 0 " + w + " " + h, "class": "xw-chart", role: "img", "aria-label": opts.title || opts.ylabel || "chart" }, parent);
    if (opts.id) svg.setAttribute("id", opts.id);
    var x0 = m.left, x1 = w - m.right, y0 = h - m.bottom, y1 = m.top;
    var xmin = opts.xmin, xmax = opts.xmax, ymin = opts.ymin, ymax = opts.ymax;
    function x(v) { return x0 + (v - xmin) / (xmax - xmin || 1) * (x1 - x0); }
    function y(v) { return y0 - (v - ymin) / (ymax - ymin || 1) * (y0 - y1); }
    var axes = el("g", { "class": "xw-axes" }, svg);
    var yd = tickDigits(ymin, ymax, opts.yticks || 4), xd = tickDigits(xmin, xmax, opts.xticks || 5);
    ticks(ymin, ymax, opts.yticks || 4).forEach(function (t) {
      el("line", { x1: x0, x2: x1, y1: y(t), y2: y(t), "class": "xw-grid" }, axes);
      el("text", { x: x0 - 8, y: y(t) + 4, "text-anchor": "end", "class": "xw-tick" }, axes, fmt(t, yd));
    });
    ticks(xmin, xmax, opts.xticks || 5).forEach(function (t) {
      el("text", { x: x(t), y: y0 + 18, "text-anchor": "middle", "class": "xw-tick" }, axes, fmt(t, xd));
    });
    el("line", { x1: x0, x2: x1, y1: y0, y2: y0, "class": "xw-axis" }, axes);
    el("line", { x1: x0, x2: x0, y1: y0, y2: y1, "class": "xw-axis" }, axes);
    if (opts.xlabel) el("text", { x: (x0 + x1) / 2, y: h - 8, "text-anchor": "middle", "class": "xw-axis-title" }, axes, opts.xlabel);
    if (opts.ylabel) el("text", { x: 16, y: (y0 + y1) / 2, "text-anchor": "middle", transform: "rotate(-90 16 " + (y0 + y1) / 2 + ")", "class": "xw-axis-title" }, axes, opts.ylabel);
    var g = el("g", { "class": "xw-plot" }, svg);
    return { svg: svg, g: g, x: x, y: y, w: w, h: h, left: x0, right: x1, top: y1, bottom: y0 };
  }

  // Clears what a redraw put in a chart's `g`.
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  // Colour legend: items [{color, label}], colour as a CSS value such as "var(--cat-0)".
  function legend(parent, items) {
    var box = el("div", { "class": "xw-legend" }, parent);
    items.forEach(function (it) {
      var s = el("span", { "class": "xw-key" }, box);
      el("i", { style: "background:" + it.color }, s);
      s.appendChild(document.createTextNode(it.label));
    });
    return box;
  }

  // A row of live numbers: items [{label}] -> returns {set(i, text)}.
  function readouts(parent, labels) {
    var row = el("div", { "class": "xw-readouts" }, parent), vals = [];
    labels.forEach(function (l) {
      var c = el("div", { "class": "xw-read" }, row);
      el("span", { "class": "xw-read-label" }, c, l);
      vals.push(el("b", { "class": "xw-read-val" }, c, ""));
    });
    return { el: row, set: function (i, text) { vals[i].textContent = text; } };
  }

  // The one-line "what to notice" under a visual. Returns the element so it can change with the state.
  function note(parent, text) { return el("p", { "class": "xw-note" }, parent, text); }

  // ---------- Vega charts (Code Lab 2.0/4.0: Vega-Lite; Code Lab 3.0: full Vega) ----------
  // The server writes XW.vega("vl", spec) around Haiku's spec; the frame loads window.vega and window.vegaLite
  // first. The chart takes the page's colours, can't load anything from outside, and each bound control (a Vega-Lite
  // param or a Vega signal with "bind") gets id = its name, like any planned control.
  function cssVar(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function vegaConfig() {
    var text = cssVar("--text", "#1b1f24"), text2 = cssVar("--text-2", "#4a525c"), muted = cssVar("--muted", "#6b7480");
    var border = cssVar("--border", "#d8dde3"), font = cssVar("--font-body", "system-ui, sans-serif");
    var fallback = ["#2f6fb0", "#e07b39", "#3a9a5b", "#9b59b6", "#c0392b", "#7f8c8d"];
    var cats = fallback.map(function (c, i) { return cssVar("--cat-" + i, c); });
    return {
      background: null, font: font,
      axis: { labelColor: muted, titleColor: text2, gridColor: border, domainColor: text2, tickColor: text2, labelFontSize: 12, titleFontSize: 13, titleFontWeight: "normal" },
      legend: { labelColor: text2, titleColor: text2, labelFontSize: 12, titleFontSize: 12 },
      header: { labelColor: text2, titleColor: text2 },
      title: { color: text, fontSize: 14 },
      view: { stroke: border },
      range: { category: cats },
      mark: { color: cats[0] },
      text: { color: text },
    };
  }

  var views = (window.XW_VIEWS = window.XW_VIEWS || {});
  // Vega's own warnings per chart (e.g. "Log scale domain includes zero"): the widget check quotes them when a chart
  // draws nothing, so a repair knows why. Some warnings are harmless, so they are never a failure on their own.
  var warnings = (window.XW_WARNINGS = window.XW_WARNINGS || {});

  function warnLogger(id) {
    var list = (warnings[id] = warnings[id] || []);
    var keep = function () {
      var text = Array.prototype.slice.call(arguments).map(String).join(" ");
      if (list.length < 10 && list.indexOf(text) < 0) list.push(text);
    };
    var quiet = function () {};
    var logger = { warn: keep, error: keep, info: quiet, debug: quiet };
    logger.level = function () { return arguments.length ? logger : 2; };  // 2 = warn, Vega's own scale
    return logger;
  }

  // Code Lab 3.0 writes full Vega specs (signals, scales, axes, marks) instead of Vega-Lite. Told apart by $schema, or
  // by the shape (top-level "marks" and no "mark") when the schema is missing.
  function isFullVega(spec) {
    var s = String(spec.$schema || "");
    if (/\/schema\/vega-lite\//.test(s)) return false;
    if (/\/schema\/vega\//.test(s)) return true;
    return Array.isArray(spec.marks) && !spec.mark && !spec.layer && !spec.hconcat && !spec.vconcat && !spec.concat;
  }

  // The page's colours for a full Vega spec: Vega's own config (axes, legends, default mark colours, the "category"
  // range) plus signals a mark can use by name, so a spec never hard-codes a colour: xw_c0 ... xw_c5 (series),
  // xw_text, xw_text2, xw_muted, xw_border, xw_accent, and xw_box / xw_box_on (light fills for boxes that hold text).
  function fullVegaTheme() {
    var c = vegaConfig(), cats = c.range.category, text = c.text.color;
    var text2 = cssVar("--text-2", "#4a525c"), muted = cssVar("--muted", "#6b7480"), border = cssVar("--border", "#d8dde3");
    var config = {
      background: null,
      axis: c.axis, legend: c.legend, title: c.title, range: c.range,
      text: { fill: text, font: c.font },
      line: { stroke: cats[0], strokeWidth: 2 }, trail: { fill: cats[0] },
      rect: { fill: cats[0] }, area: { fill: cats[0] }, arc: { fill: cats[0] }, symbol: { fill: cats[0] }, path: { stroke: cats[0] },
      rule: { stroke: muted }, group: {},
    };
    var signals = [["xw_text", text], ["xw_text2", text2], ["xw_muted", muted], ["xw_border", border], ["xw_accent", cssVar("--accent", cats[0])],
      ["xw_box", cssVar("--surface-2", "#f1f3f5")], ["xw_box_on", cssVar("--accent-soft", "#fde7d9")]];
    cats.forEach(function (v, i) { signals.push(["xw_c" + i, v]); });
    return { config: config, signals: signals };
  }

  function withTheme(spec, theme) {
    var have = {};
    (spec.signals || []).forEach(function (s) { if (s && s.name) have[s.name] = true; });
    var add = theme.signals.filter(function (s) { return !have[s[0]]; }).map(function (s) { return { name: s[0], value: s[1] }; });
    var out = {};
    for (var k in spec) out[k] = spec[k];
    out.signals = add.concat(spec.signals || []);
    delete out.config;  // the page's theme, not the spec's own
    return out;
  }

  function vegaChart(id, spec) {
    var root = document.getElementById(id);
    if (!root) throw new Error('XW.vega: there is no element with id "' + id + '"');
    var full = isFullVega(spec);
    if (!window.vega || (!full && !window.vegaLite) || !window.vegaInterpreter) throw new Error("XW.vega: the Vega libraries are not loaded");
    var params = ((full ? spec.signals : spec.params) || []).map(function (p) { return p && p.name; }).filter(Boolean);
    var current = null;

    function render() {
      var keep = {};
      if (current) {
        params.forEach(function (n) { try { keep[n] = current.signal(n); } catch (e) { /* not a signal */ } });
        current.finalize();
      }
      root.innerHTML = "";
      var log = warnLogger(id);
      var compiled, config = null;
      if (full) {
        var theme = fullVegaTheme();
        compiled = withTheme(spec, theme);
        config = theme.config;
      } else {
        compiled = window.vegaLite.compile(spec, { config: vegaConfig(), logger: log }).spec;
      }
      var loader = window.vega.loader();
      loader.load = loader.sanitize = function () { return Promise.reject(new Error("a chart can't load data from outside the page")); };
      // The frame's Content-Security-Policy blocks eval, so formulas run in Vega's interpreter instead of compiled code.
      var view = new window.vega.View(window.vega.parse(compiled, config, { ast: true }),
        { renderer: "svg", container: root, hover: true, loader: loader, expr: window.vegaInterpreter });
      view.logger(log);
      for (var n in keep) { try { view.signal(n, keep[n]); } catch (e) { /* gone */ } }
      current = views[id] = view;
      return view.runAsync().then(function () {
        // Name each bound control after its param, so the plan's control ids exist on the page.
        var inputs = root.querySelectorAll(".vega-bind input, .vega-bind select");
        for (var i = 0; i < inputs.length; i++) {
          var name = inputs[i].getAttribute("name");
          if (name && params.indexOf(name) >= 0 && !document.getElementById(name)) inputs[i].id = name;
        }
        return view;
      });
    }

    // Light/dark changes: the frame updates its CSS variables on the same message, so redraw just after.
    addEventListener("message", function (e) {
      if (e.data && e.data.type === "widget-theme") setTimeout(render, 0);
    });
    return render();
  }

  window.XW = { el: el, fmt: fmt, ticks: ticks, slider: slider, button: button, chart: chart, clear: clear, legend: legend, readouts: readouts, note: note, vega: vegaChart };
})();

// Styles for the helpers (kept next to them so the two stay in step). widget.ts adds this CSS to the frame.
window.XW_CSS = [
  ".xw-ctl{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:6px 0;font-size:14px;color:var(--text-2)}",
  ".xw-ctl label{flex:0 1 auto}",
  ".xw-ctl input[type=range]{flex:1 1 180px;min-width:120px}",
  ".xw-val{min-width:4.5em;font-family:var(--font-mono);font-weight:700;color:var(--accent)}",
  ".xw-btn{padding:6px 14px;border-radius:8px;border:1px solid var(--border);background:var(--surface);color:var(--text)}",
  ".xw-btn:hover{border-color:var(--accent)}",
  ".xw-chart{display:block;width:100%;height:auto;max-width:600px}",
  ".xw-grid{stroke:var(--border);stroke-width:1}",
  ".xw-axis{stroke:var(--text-2);stroke-width:1.2}",
  ".xw-tick{font-size:12px;fill:var(--muted);font-family:var(--font-mono)}",
  ".xw-axis-title{font-size:13px;fill:var(--text-2)}",
  ".xw-legend{display:flex;flex-wrap:wrap;gap:6px 16px;margin:6px 0;font-size:13px;color:var(--text-2)}",
  ".xw-key i{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:6px;vertical-align:-1px}",
  ".xw-readouts{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin:8px 0}",
  ".xw-read{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:6px 10px;font-size:12px;color:var(--text-2)}",
  ".xw-read-val{display:block;font-family:var(--font-mono);font-size:16px;color:var(--text);margin-top:2px}",
  ".xw-note{font-size:13px;color:var(--text-2);margin:8px 0 0;line-height:1.45}",
].join("");
