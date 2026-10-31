// "Export HTML": a single self-contained file = the spec + the prebuilt viewer
// bundle (app/public/viewer.js, built by `npm run build:viewer`). Works offline
// except for web fonts and KaTeX CSS from CDNs. The chatbot isn't included
// because it needs our server.

import type { ExplainerSpec } from "../../../shared/spec";
import { escapeHtml } from "./dom";

export const FONTS_URL =
  "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&family=Fraunces:opsz,wght@9..144,500;9..144,700&family=Space+Grotesk:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;600&family=Nunito:wght@400;600;700;800&family=Fira+Code:wght@400;600&display=swap";
export const KATEX_CSS = "https://cdn.jsdelivr.net/npm/katex@0.19.0/dist/katex.min.css";

export async function exportHtml(spec: ExplainerSpec): Promise<void> {
  const res = await fetch(`${import.meta.env.BASE_URL}viewer.js`);
  if (!res.ok) throw new Error("The viewer bundle is missing. Run `npm run build:viewer`.");
  const viewer = (await res.text()).replace(/<\/script/gi, "<\\/script");
  const json = JSON.stringify(spec).replace(/</g, "\\u003c");
  const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(spec.title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="${FONTS_URL}">
<link rel="stylesheet" href="${KATEX_CSS}">
</head>
<body>
<div id="root" class="scroll-host"></div>
<script>window.__SPEC__ = ${json};</script>
<script>${viewer}</script>
</body>
</html>
`;
  const url = URL.createObjectURL(new Blob([html], { type: "text/html" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = `${spec.title.replace(/[^\w\- ]+/g, "").trim().replace(/\s+/g, "-").toLowerCase() || "explainer"}.html`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}
