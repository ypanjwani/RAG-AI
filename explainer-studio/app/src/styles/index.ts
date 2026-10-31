// Explainer styles as strings so the same CSS is injected by the app and by
// the standalone viewer bundle used for "Export HTML".
import components from "./components.css?inline";
import explainer from "./explainer.css?inline";
import themes from "./themes.css?inline";
import tokens from "./tokens.css?inline";

export function injectExplainerStyles() {
  if (document.getElementById("explainer-styles")) return;
  const style = document.createElement("style");
  style.id = "explainer-styles";
  style.textContent = [tokens, themes, explainer, components].join("\n");
  document.head.appendChild(style);
}
