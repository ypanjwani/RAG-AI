// Playground mode: XW.playground(spec) draws one playground spec (shared/playground/*.cjs, from the ai-playground
// experiment) inside a widget frame. widget.ts loads this after the widget kit (which sets window.XW) and after the
// runtime files (state, helpers, sandbox, structures, components, renderer), so they are all on window here.
// The server writes the widget's html as <div id="pg"></div><script>XW.playground({...})</script>.
(function () {
  var XW = (window.XW = window.XW || {});
  var toastTimer = null;

  function toast(text) {
    var t = document.querySelector(".pg-toast");
    if (!t) { t = document.createElement("div"); t.className = "pg-toast"; document.body.appendChild(t); }
    t.textContent = text;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.remove(); }, 4000);
  }

  XW.playground = function (spec, id) {
    var box = document.getElementById(id || "pg");
    if (!box) { box = document.createElement("div"); box.id = id || "pg"; document.body.appendChild(box); }
    try {
      return window.renderPlayground(box, spec, {
        onError: function (where, msg) { console.warn("[playground] " + where + ": " + msg); toast("Something in this playground failed: " + msg); },
      });
    } catch (e) {
      console.error("[playground] could not draw:", e);
      box.textContent = "This playground could not be drawn.";
      return null;
    }
  };
})();
