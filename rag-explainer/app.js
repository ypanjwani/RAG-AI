"use strict";

// ============================================================
// State
// ============================================================

const state = {
  // Set by the gitignored config.local.js (generated from .env).
  groqApiKey: window.RAG_DEV_GROQ_KEY || "",

  docSource: "sample", // "sample" | "upload"
  documentText: "",    // extracted text of the uploaded PDF (page 1 only)
  documentName: "",
  pdfPageCount: 0,
  docLoading: false,
  docError: "",

  // Step 1: chunking
  chunkSize: 250,
  chunkOverlap: 40,
  chunks: [],     // {text, start, end, tokens, tokenVecs, vec}
  chunkKey: null, // doc text + settings the chunks were built from
  chunkingVisited: false,

  // Step 2: chunk embeddings
  embed: { status: "idle", loaded: 0, total: 0, done: 0, error: "" },
  embedderReady: false,
  selectedChunk: 0,
  embedView: { dimStart: 0, dim: 0, allTokens: false }, // token × dimension table (Steps 2 and 5)
  lab: { a: "dog", b: "puppy", busy: false, result: null, error: "" },

  // Step 3: vector store
  storeVisited: false,
  storeSelected: null,

  // Steps 4-9: querying
  query: "",
  queryTokens: null, // [{id, text}]
  queryEmbed: null,  // {tokens, tokenVecs, vec}
  queryBusy: false,
  queryError: "",
  retrievalMode: "cosine", // "cosine" | "bm25" | "hybrid"
  hybridWeight: 0.5,
  topK: 3,
  simSelected: null,
  bm25Idf: null, // word -> IDF, built with the chunk embeddings
  bm25AvgLen: 0,
  bm25N: 0,
  similarityVisited: false,
  topkVisited: false,
  inspectVisited: false,
  gen: { status: "idle", answer: null, truncated: false, error: "" },
  noRag: { status: "idle", answer: null, truncated: false, error: "" },
  inspectFocus: null, // chunk to highlight in Step 8 (from a chat "Sources" click)

  // Chat sidebar
  chatMessages: [],   // see runChatPipeline()
  chatBusy: false,
  activeChatId: null, // chat answer whose snapshot the step pages are showing
};

// ============================================================
// Groq API
// ============================================================

const GROQ_URL = "https://api.groq.com/openai/v1/chat/completions";
// gpt-oss is a reasoning model: hidden reasoning tokens count against max_tokens,
// so the limit stays generous. Swap if Groq retires or renames it.
const GROQ_MODEL = "openai/gpt-oss-120b";

// Public deploys go through the Cloudflare Worker in worker/, which holds the key.
// Set to the deployed Worker URL (not a secret). config.local.js may override it.
const GROQ_PROXY_URL = window.RAG_PROXY_URL || "https://rag-explainer-proxy.ypanjwani1110.workers.dev";

// Calls Groq's OpenAI-compatible chat endpoint, via the proxy if one is set,
// otherwise directly with the local dev key. Throws an Error with a
// beginner-readable message on failure.
async function callGroq(messages, { model = GROQ_MODEL, maxTokens = 4096 } = {}) {
  const viaProxy = Boolean(GROQ_PROXY_URL);
  if (!viaProxy && !state.groqApiKey) {
    throw new Error("The AI step isn't set up on this copy of the explainer yet.");
  }

  let res;
  try {
    res = viaProxy
      ? await fetch(GROQ_PROXY_URL, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ messages }), // the proxy fixes model and max_tokens
        })
      : await fetch(GROQ_URL, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${state.groqApiKey}`,
          },
          body: JSON.stringify({ model, messages, max_tokens: maxTokens }),
        });
  } catch {
    throw new Error(viaProxy
      ? "Couldn't reach the AI service. Check your internet connection and try again."
      : "Couldn't reach Groq. Check your internet connection and try again.");
  }

  if (!res.ok) {
    if (res.status === 429) throw new Error("Too many questions in a short time. Wait a minute and try again.");
    if (viaProxy && res.status === 403) throw new Error("This site isn't allowed to use the AI service.");
    if (res.status === 401) throw new Error(viaProxy
      ? "The AI service's key was rejected. The site owner needs to update it."
      : "Groq rejected the API key. Check the key in config.local.js.");
    let detail = "";
    try { detail = (await res.json()).error?.message || ""; } catch { /* non-JSON body */ }
    throw new Error(`The AI service returned an error (${res.status}). ${detail}`.trim());
  }

  const data = await res.json();
  const choice = data.choices?.[0];
  return {
    text: choice?.message?.content || "",
    truncated: choice?.finish_reason === "length",
  };
}

// Model output goes into innerHTML, so always sanitize.
function renderMarkdown(md) {
  return DOMPurify.sanitize(marked.parse(md));
}

// ============================================================
// Document (sample text or PDF upload, page 1 only)
// ============================================================

const SAMPLE_DOCUMENT_NAME = "sample text";
const SAMPLE_DOCUMENT = `Rome and Vatican City
Rome is the capital of Italy and one of the oldest cities in Europe. Inside Rome sits Vatican City, the smallest country in the world. The Pope lives in the Vatican, in the Apostolic Palace next to St. Peter's Basilica. Millions of visitors come every year to see the Sistine Chapel, whose ceiling was painted by Michelangelo.

How Bees Make Honey
Honeybees fly from flower to flower collecting nectar, a sugary liquid. Back at the hive, worker bees pass the nectar between them and add enzymes that break down the sugar. They store it in wax cells and fan it with their wings until most of the water evaporates. What is left is thick, sweet honey, which the colony eats during winter.

How Volcanoes Form
Deep under the ground, rock gets so hot that it melts into magma. Magma is lighter than the solid rock around it, so it slowly rises. When pressure builds up, the magma pushes through cracks in the Earth's crust and erupts as lava, ash and gas. Over many eruptions, the cooled lava piles up into a cone-shaped mountain called a volcano.

Why the Sky Is Blue
Sunlight looks white but is made of all the colors of the rainbow. When sunlight enters the air, it bumps into tiny gas molecules. Blue light has short waves, so it gets scattered in every direction much more than red light. That scattered blue light reaches our eyes from all over the sky, which is why the sky looks blue on a clear day.`;

const PDFJS_WORKER_URL = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js";

// The document every later step works on.
function activeDocument() {
  if (state.docSource === "upload" && state.documentText) {
    return { name: state.documentName, text: state.documentText };
  }
  if (state.docSource === "sample") {
    return { name: SAMPLE_DOCUMENT_NAME, text: SAMPLE_DOCUMENT };
  }
  return null; // "upload" chosen but no usable PDF yet
}

// Reads only page 1 of the PDF; the rest of the file is ignored.
async function extractPdfText(file) {
  pdfjsLib.GlobalWorkerOptions.workerSrc = PDFJS_WORKER_URL;
  const data = await file.arrayBuffer();
  const pdf = await pdfjsLib.getDocument({ data }).promise;
  const page = await pdf.getPage(1);
  const content = await page.getTextContent();

  let text = "";
  for (const item of content.items) {
    text += item.str;
    if (item.hasEOL) text += "\n";
    else if (item.str && !item.str.endsWith(" ")) text += " ";
  }
  text = text
    .replace(/[ \t]+/g, " ")
    .replace(/ *\n */g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();

  return { text, pageCount: pdf.numPages };
}

function wordCount(text) {
  return text.split(/\s+/).filter(Boolean).length;
}

const docEls = {
  toggle: document.getElementById("doc-toggle"),
  options: document.querySelectorAll('input[name="doc-source"]'),
  upload: document.getElementById("doc-upload"),
  file: document.getElementById("doc-file"),
  fileName: document.getElementById("doc-file-name"),
  status: document.getElementById("doc-status"),
  card: document.getElementById("doc-card"),
  cardName: document.getElementById("doc-card-name"),
  cardMeta: document.getElementById("doc-card-meta"),
  cardSnippet: document.getElementById("doc-card-snippet"),
  view: document.getElementById("doc-view"),
  modal: document.getElementById("doc-modal"),
  modalTitle: document.getElementById("doc-modal-title"),
  modalMeta: document.getElementById("doc-modal-meta"),
  modalBody: document.getElementById("doc-modal-body"),
  modalClose: document.getElementById("doc-modal-close"),
};

function docMeta(doc) {
  const parts = [`${wordCount(doc.text).toLocaleString()} words`, `${doc.text.length.toLocaleString()} characters`];
  if (state.docSource === "upload") parts.unshift(`page 1 of ${state.pdfPageCount}`);
  return parts.join(" · ");
}

function renderDocSidebar() {
  docEls.upload.hidden = state.docSource !== "upload";
  docEls.status.classList.remove("error");
  docEls.status.textContent = "";

  const doc = activeDocument();
  if (state.docSource === "upload" && state.docLoading) {
    docEls.status.textContent = "⏳ Reading page 1…";
  } else if (state.docSource === "upload" && state.docError) {
    docEls.status.textContent = state.docError;
    docEls.status.classList.add("error");
  } else if (!doc) {
    docEls.status.textContent = "Choose a PDF file above to continue.";
  }

  docEls.card.hidden = !doc;
  if (doc) {
    docEls.cardName.textContent = state.docSource === "sample" ? "Sample article" : doc.name;
    docEls.cardMeta.textContent = docMeta(doc);
    docEls.cardSnippet.textContent = snippet(doc.text, 180);
  }
}

// Paragraphs separated by blank lines; a short first line acts as a heading.
function openDocModal() {
  const doc = activeDocument();
  if (!doc) return;
  docEls.modalTitle.textContent = state.docSource === "sample" ? "Sample article" : doc.name;
  docEls.modalMeta.textContent = state.docSource === "upload"
    ? `${docMeta(doc)} · this is all the text the demo will use`
    : docMeta(doc);
  docEls.modalBody.replaceChildren(...doc.text.split(/\n\s*\n/).map((para) => {
    const lines = para.trim().split("\n");
    const frag = document.createDocumentFragment();
    if (lines.length > 1 && lines[0].length < 60 && !/[.!?:]$/.test(lines[0])) {
      const h = document.createElement("h3");
      h.textContent = lines.shift();
      frag.appendChild(h);
    }
    const p = document.createElement("p");
    p.textContent = lines.join(" ");
    frag.appendChild(p);
    return frag;
  }));
  docEls.modal.hidden = false;
  docEls.modalBody.scrollTop = 0;
  docEls.modalClose.focus();
}

function closeDocModal() {
  if (docEls.modal.hidden) return;
  docEls.modal.hidden = true;
  docEls.view.focus();
}

async function handlePdfUpload(file) {
  state.docError = "";
  state.documentText = ""; // a failed new upload shouldn't leave the old PDF in use
  const isPdf = file.type === "application/pdf" || /\.pdf$/i.test(file.name);
  if (!isPdf) {
    state.docError = "That file isn't a PDF. Please choose a .pdf file.";
    renderDocSidebar();
    return;
  }

  state.docLoading = true;
  renderDocSidebar();
  try {
    const { text, pageCount } = await extractPdfText(file);
    if (!text) {
      state.docError =
        "Page 1 of this PDF has no selectable text. It's probably a scan (a photo of a page). Try a different PDF.";
    } else {
      state.documentText = text;
      state.documentName = file.name;
      state.pdfPageCount = pageCount;
    }
  } catch {
    state.docError = "Couldn't read this PDF. It may be damaged or password-protected. Try a different file.";
  } finally {
    state.docLoading = false;
    renderDocSidebar();
    renderAll();
  }
}

function setDocCollapsed(collapsed) {
  document.querySelector(".layout").classList.toggle("doc-collapsed", collapsed);
  const label = collapsed ? "Show document panel" : "Hide document panel";
  docEls.toggle.title = label;
  docEls.toggle.setAttribute("aria-label", label);
}

function initDocSidebar() {
  docEls.toggle.addEventListener("click", () => {
    setDocCollapsed(!document.querySelector(".layout").classList.contains("doc-collapsed"));
  });
  if (window.matchMedia("(max-width: 800px)").matches) setDocCollapsed(true);

  docEls.options.forEach((radio) => {
    radio.addEventListener("change", () => {
      state.docSource = radio.value;
      renderDocSidebar();
      renderAll();
    });
  });
  docEls.file.addEventListener("change", () => {
    const file = docEls.file.files[0];
    docEls.fileName.textContent = file ? file.name : "No file chosen";
    if (file) handlePdfUpload(file);
  });

  docEls.view.addEventListener("click", openDocModal);
  docEls.modalClose.addEventListener("click", closeDocModal);
  docEls.modal.addEventListener("click", (e) => { if (e.target === docEls.modal) closeDocModal(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDocModal(); });

  // "How to use" guide (static content lives in index.html).
  const helpModal = document.getElementById("help-modal");
  const helpOpen = document.getElementById("help-open");
  const closeHelp = () => {
    if (helpModal.hidden) return;
    helpModal.hidden = true;
    helpOpen.focus();
  };
  helpOpen.addEventListener("click", () => {
    helpModal.hidden = false;
    helpModal.querySelector(".modal-body").scrollTop = 0;
    document.getElementById("help-close").focus();
  });
  document.getElementById("help-close").addEventListener("click", closeHelp);
  helpModal.addEventListener("click", (e) => { if (e.target === helpModal) closeHelp(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeHelp(); });
  renderDocSidebar();
}

// ============================================================
// Pipeline: stages, routing, rendering
// ============================================================

const STAGES = [
  { id: "chunking", label: "Chunking", phase: "index", icon: "✂️",
    blurb: "Cut the document into small, overlapping pieces." },
  { id: "embed", label: "Chunk Embeddings", phase: "index", icon: "🔢",
    blurb: "Turn each piece into a list of numbers that captures its meaning." },
  { id: "store", label: "Vector Store", phase: "index", icon: "🗄️",
    blurb: "Save every piece with its numbers so it can be searched." },
  { id: "query", label: "Ask a Question", phase: "query", icon: "💬",
    blurb: "Type a question and see how the model reads it." },
  { id: "query-embed", label: "Query Embedding", phase: "query", icon: "🧭",
    blurb: "Turn the question into numbers, the same way as the chunks." },
  { id: "similarity", label: "Similarity Search", phase: "query", icon: "📏",
    blurb: "Score every chunk by how well it matches the question." },
  { id: "topk", label: "Top-k Retrieval", phase: "query", icon: "🏆",
    blurb: "Keep only the best few chunks." },
  { id: "inspect", label: "Inspect Chunks", phase: "query", icon: "🔍",
    blurb: "Look at what was found and why." },
  { id: "generate", label: "LLM Generation", phase: "query", icon: "✨",
    blurb: "Hand the chunks to an AI and get an answer." },
];

const PHASES = [
  { id: "index", label: "Phase A · Indexing", short: "Indexing", sub: "Done once, before any question is asked" },
  { id: "query", label: "Phase B · Querying", short: "Querying", sub: "Runs every time someone asks a question" },
];

const STAGE_RENDERERS = {
  chunking: renderChunkingStage,
  embed: renderEmbedStage,
  store: renderStoreStage,
  query: renderQueryStage,
  "query-embed": renderQueryEmbedStage,
  similarity: renderSimilarityStage,
  topk: renderTopkStage,
  inspect: renderInspectStage,
  generate: renderGenerateStage,
};

const STATUS_LABEL = { done: "✓ Done", ready: "Ready", locked: "🔒 Locked", soon: "Coming soon" };

const appEl = document.getElementById("app");

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function html(str) {
  const t = document.createElement("template");
  t.innerHTML = str.trim();
  return t.content.firstElementChild;
}

function snippet(text, n) {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length > n ? flat.slice(0, n).trimEnd() + "…" : flat;
}

function stageIndex(id) {
  return STAGES.findIndex((s) => s.id === id);
}

function isStageComplete(id) {
  switch (id) {
    case "chunking": return state.chunkingVisited && state.chunks.length > 0;
    case "embed": return state.chunks.length > 0 && state.chunks.every((c) => c.vec);
    case "store": return state.storeVisited && isStageComplete("embed");
    case "query": return !!state.queryTokens;
    case "query-embed": return !!state.queryEmbed;
    case "similarity": return state.similarityVisited && !!state.queryEmbed;
    case "topk": return state.topkVisited && isStageComplete("similarity");
    case "inspect": return state.inspectVisited && isStageComplete("topk");
    case "generate": return !!state.gen.answer;
    default: return false;
  }
}

function isStageUnlocked(id) {
  const i = stageIndex(id);
  if (i === 0) return !!activeDocument();
  const prev = STAGES[i - 1].id;
  return isStageUnlocked(prev) && isStageComplete(prev); // a whole chain, not just the previous step
}

function stageStatus(id) {
  if (!STAGE_RENDERERS[id]) return "soon";
  if (!isStageUnlocked(id)) return "locked";
  return isStageComplete(id) ? "done" : "ready";
}

function currentRoute() {
  const id = location.hash.replace(/^#\/?/, "");
  return stageIndex(id) >= 0 ? id : null;
}

function goTo(id) {
  location.hash = id ? `#/${id}` : "#/";
}

// The only render entrypoint: rebuilds the current route's view from scratch.
function renderAll() {
  ensureChunks();
  heatmapRegistry.clear();
  hideTip();
  const id = currentRoute();
  appEl.replaceChildren(id ? renderStagePage(id) : renderFlowchart());
  renderChat();
}

function explainer(title, bodyHtml) {
  return html(`
    <div class="explainer">
      <div class="explainer-icon">💡</div>
      <div class="explainer-body"><h3>${title}</h3>${bodyHtml}</div>
    </div>`);
}

// ---------- Overview flowchart ----------

function renderFlowchart() {
  const root = html(`
    <div class="overview">
      <section class="hero">
        <div class="hero-kicker">Interactive guide</div>
        <h2>How does RAG work?</h2>
        <p>
          <strong>RAG</strong> (Retrieval-Augmented Generation) lets an AI answer questions using
          <strong>your</strong> document. Instead of guessing, it first <em>finds</em> the most relevant
          pieces of the document, then <em>writes</em> an answer from them.
          Follow the steps below to watch every part happen for real, right in your browser.
        </p>
      </section>
    </div>`);

  const next = STAGES.find((s) => stageStatus(s.id) === "ready");
  if (next) {
    const btn = html(`<button type="button" class="btn primary hero-btn">
      ${isStageComplete("chunking") ? "Continue" : "Start"} with Step ${stageIndex(next.id) + 1}: ${next.label} →</button>`);
    btn.addEventListener("click", () => goTo(next.id));
    root.querySelector(".hero").appendChild(btn);
  } else if (!activeDocument()) {
    root.querySelector(".hero").appendChild(html(
      `<p class="hero-note">👈 First, pick a document in the left panel.</p>`));
  }

  PHASES.forEach((phase, pi) => {
    const sec = html(`
      <section class="phase">
        <div class="phase-head">
          <span class="phase-pill ${phase.id}">${phase.label}</span>
          <span class="phase-sub">${phase.sub}</span>
        </div>
        <div class="flow-grid"></div>
      </section>`);
    const grid = sec.querySelector(".flow-grid");
    STAGES.filter((s) => s.phase === phase.id).forEach((s) => grid.appendChild(stepCard(s)));
    root.appendChild(sec);
    if (pi === 0) {
      root.appendChild(html(`<div class="phase-link"><span>↓</span> then, for every question <span>↓</span></div>`));
    }
  });
  return root;
}

function stepCard(s) {
  const st = stageStatus(s.id);
  const card = html(`
    <button type="button" class="step-card ${st}">
      <span class="step-card-top">
        <span class="step-icon">${s.icon}</span>
        <span class="badge ${st}">${STATUS_LABEL[st]}</span>
      </span>
      <span class="step-num">Step ${stageIndex(s.id) + 1}</span>
      <span class="step-label">${s.label}</span>
      <span class="step-blurb">${s.blurb}</span>
    </button>`);
  card.addEventListener("click", () => goTo(s.id));
  return card;
}

// ---------- Stage page ----------

function renderStagePage(id) {
  const i = stageIndex(id);
  const s = STAGES[i];
  const phase = PHASES.find((p) => p.id === s.phase);
  const page = html(`
    <div class="stage-page">
      <div class="stage-topbar">
        <a href="#/" class="back-link">← All steps</a>
        <div class="step-dots"></div>
      </div>
      <header class="stage-header ${s.phase}">
        <div class="stage-meta">
          <span class="phase-pill ${s.phase} light">${phase.short}</span>
          <span>Step ${i + 1} of ${STAGES.length}</span>
        </div>
        <h2><span class="stage-icon">${s.icon}</span>${s.label}</h2>
        <p>${s.blurb}</p>
      </header>
      <div class="stage-body"></div>
      <nav class="stage-nav"></nav>
    </div>`);

  const chatMsg = s.phase === "query" && state.chatMessages.find((m) => m.id === state.activeChatId);
  if (chatMsg) {
    const banner = html(`<div class="chat-banner">💬 Showing the chat question: <b></b></div>`);
    banner.querySelector("b").textContent = `“${chatMsg.question}”`;
    page.querySelector(".stage-header").after(banner);
  }

  const body = page.querySelector(".stage-body");
  if (!STAGE_RENDERERS[id]) body.appendChild(renderComingSoon());
  else if (!isStageUnlocked(id)) body.appendChild(renderLockedNotice(id));
  else body.appendChild(STAGE_RENDERERS[id]());

  // Dots and nav are built after the body, since rendering can complete a step.
  const dots = page.querySelector(".step-dots");
  STAGES.forEach((st, k) => {
    const dot = html(`<a class="step-dot ${stageStatus(st.id)}${st.id === id ? " current" : ""}"
      href="#/${st.id}" title="Step ${k + 1}: ${st.label}">${k + 1}</a>`);
    dots.appendChild(dot);
  });

  const nav = page.querySelector(".stage-nav");
  const prev = STAGES[i - 1];
  const next = STAGES[i + 1];
  if (prev) {
    const b = html(`<button type="button" class="btn secondary">← Step ${i}: ${prev.label}</button>`);
    b.addEventListener("click", () => goTo(prev.id));
    nav.appendChild(b);
  } else {
    nav.appendChild(html(`<span></span>`));
  }
  if (next) {
    const b = html(`<button type="button" class="btn primary">Step ${i + 2}: ${next.label} →</button>`);
    if (!STAGE_RENDERERS[next.id]) {
      b.disabled = true;
      b.title = "Coming soon";
    } else if (!isStageComplete(id)) {
      b.disabled = true;
      b.title = "Finish this step first";
    }
    b.addEventListener("click", () => goTo(next.id));
    nav.appendChild(b);
  }
  return page;
}

function renderLockedNotice(id) {
  const i = stageIndex(id);
  if (!activeDocument()) {
    const n = html(`
      <div class="notice">
        <div class="notice-icon">📄</div>
        <h3>Pick a document first</h3>
        <p>Choose the sample text or upload a PDF in the left panel, then come back here.</p>
        <button type="button" class="btn primary">Open the document panel</button>
      </div>`);
    n.querySelector("button").addEventListener("click", () => setDocCollapsed(false));
    return n;
  }
  const prev = STAGES[i - 1];
  const open = STAGES.find((s) => stageStatus(s.id) === "ready") || prev;
  const n = html(`
    <div class="notice">
      <div class="notice-icon">🔒</div>
      <h3>This step is locked</h3>
      <p>It unlocks once you finish <strong>Step ${i}: ${prev.label}</strong>.</p>
      <button type="button" class="btn primary">Go to Step ${stageIndex(open.id) + 1}: ${open.label} →</button>
    </div>`);
  n.querySelector("button").addEventListener("click", () => goTo(open.id));
  return n;
}

function renderComingSoon() {
  return html(`
    <div class="notice soon">
      <div class="notice-icon">🚧</div>
      <h3>Coming soon</h3>
      <p>This step is still being built. For now, explore Steps 1–3 (Indexing).</p>
    </div>`);
}

// ---------- Tooltip (heatmaps + scatter) ----------

const tipEl = document.createElement("div");
tipEl.className = "tip";
document.body.appendChild(tipEl);

function showTip(content, x, y) {
  tipEl.innerHTML = content;
  tipEl.style.display = "block";
  const r = tipEl.getBoundingClientRect();
  const left = Math.min(x + 14, window.innerWidth - r.width - 8);
  const top = y + 18 + r.height > window.innerHeight ? y - r.height - 10 : y + 18;
  tipEl.style.left = `${Math.max(8, left)}px`;
  tipEl.style.top = `${top}px`;
}

function hideTip() {
  tipEl.style.display = "none";
}

function onPipelineMouseMove(e) {
  const t = e.target;
  if (!(t instanceof Element)) return hideTip();

  const img = t.closest("img.heatmap");
  if (img) {
    const vec = heatmapRegistry.get(img.dataset.hm);
    if (vec) {
      const r = img.getBoundingClientRect();
      const dim = Math.min(vec.length - 1, Math.max(0, Math.floor(((e.clientX - r.left) / r.width) * vec.length)));
      const v = vec[dim];
      showTip(`Number <b>#${dim + 1}</b> of ${vec.length}: <span class="${v < 0 ? "neg" : "pos"}">${v >= 0 ? "+" : ""}${v.toFixed(4)}</span>`,
        e.clientX, e.clientY);
      return;
    }
  }

  const pt = t.closest(".pt[data-chunk]");
  if (pt) {
    const i = +pt.dataset.chunk;
    const c = state.chunks[i];
    if (c) {
      showTip(`<b>Chunk ${i + 1}</b><br>${escapeHtml(snippet(c.text, 140))}`, e.clientX, e.clientY);
      return;
    }
  }
  hideTip();
}

// ---------- Heatmap strips ----------

const heatmapRegistry = new Map(); // data-hm id -> vector, for the hover tooltip
let heatmapSeq = 0;

// 98th-percentile magnitude, so a few outlier numbers don't wash out the colors.
function heatmapScale(vecs) {
  const abs = [];
  for (const v of vecs) for (const x of v) abs.push(Math.abs(x));
  abs.sort((a, b) => a - b);
  return Math.max(abs[Math.floor(0.98 * (abs.length - 1))] || 0, 1e-6);
}

function heatmapColor(v, scale) {
  const t = Math.max(-1, Math.min(1, v / scale));
  const base = [241, 245, 249];
  const target = t < 0 ? [37, 99, 235] : [234, 88, 12];
  const a = Math.abs(t);
  return base.map((b, k) => Math.round(b + (target[k] - b) * a));
}

function heatmapStrip(vec, scale = heatmapScale([vec]), cls = "") {
  const canvas = document.createElement("canvas");
  canvas.width = vec.length;
  canvas.height = 1;
  const ctx = canvas.getContext("2d");
  const img = ctx.createImageData(vec.length, 1);
  for (let i = 0; i < vec.length; i++) {
    const [r, g, b] = heatmapColor(vec[i], scale);
    img.data.set([r, g, b, 255], i * 4);
  }
  ctx.putImageData(img, 0, 0);

  const el = new Image();
  el.src = canvas.toDataURL();
  el.className = `heatmap ${cls}`.trim();
  el.alt = `${vec.length} numbers shown as colors`;
  el.draggable = false;
  const id = String(++heatmapSeq);
  el.dataset.hm = id;
  heatmapRegistry.set(id, vec);
  return el;
}

function heatmapLegend() {
  return html(`
    <div class="hm-legend">
      <span class="hm-grad"></span>
      <span><b class="neg">blue</b> = negative · white ≈ 0 · <b class="pos">orange</b> = positive</span>
      <span class="muted">Hover any strip to read its numbers.</span>
    </div>`);
}

// ============================================================
// Step 1: Chunking
// ============================================================

const CHUNK_PALETTE = [
  { bg: "#dbeafe", solid: "#3b82f6" },
  { bg: "#fde68a", solid: "#d97706" },
  { bg: "#bbf7d0", solid: "#16a34a" },
  { bg: "#fbcfe8", solid: "#db2777" },
  { bg: "#ddd6fe", solid: "#7c3aed" },
  { bg: "#fed7aa", solid: "#ea580c" },
  { bg: "#a5f3fc", solid: "#0891b2" },
  { bg: "#fecaca", solid: "#dc2626" },
];

function chunkColor(i) {
  return CHUNK_PALETTE[i % CHUNK_PALETTE.length];
}

function effectiveOverlap() {
  return Math.min(state.chunkOverlap, Math.floor(state.chunkSize / 2));
}

const isSpace = (ch) => /\s/.test(ch);

// Fixed-size character chunks with overlap, snapped to word boundaries:
// a chunk ends at the last whitespace before the size limit, and the next
// chunk's overlap starts at a word start.
function chunkDocument(text, size, overlap) {
  const chunks = [];
  const n = text.length;
  let start = 0;
  while (start < n && isSpace(text[start])) start++;

  while (start < n) {
    let end = Math.min(start + size, n);
    if (end < n) {
      let cut = end;
      while (cut > start && !isSpace(text[cut])) cut--;
      if (cut > start) end = cut; // otherwise one huge "word": hard cut
    }
    let e = end;
    while (e > start && isSpace(text[e - 1])) e--;
    chunks.push({ text: text.slice(start, e), start, end: e });
    if (end >= n) break;

    let next = Math.max(end - overlap, start + 1);
    if (!isSpace(text[next - 1])) {
      while (next < n && !isSpace(text[next])) next++; // mid-word: skip to its end
    }
    while (next < n && isSpace(text[next])) next++;
    start = next;
  }
  return chunks;
}

// Re-chunks (and drops vectors) only when the document or settings change.
function ensureChunks() {
  const doc = activeDocument();
  const key = doc ? `${state.chunkSize}|${effectiveOverlap()}|${doc.text}` : "";
  if (key === state.chunkKey) return;

  state.chunkKey = key;
  state.chunks = doc
    ? chunkDocument(doc.text, state.chunkSize, effectiveOverlap())
        .map((c) => ({ ...c, tokens: null, tokenVecs: null, vec: null }))
    : [];
  state.selectedChunk = 0;
  state.storeVisited = false;
  state.storeSelected = null;
  state.bm25Idf = null;
  resetQueryResults();
  if (state.embed.status !== "loading-model") {
    state.embed = { status: "idle", loaded: 0, total: 0, done: 0, error: "" };
  }
}

// Colors each character by the chunk that owns it. Overlaps show the later
// chunk's color, with a dashed underline in the earlier chunk's color.
function buildHighlightedHtml(text, chunks) {
  const coverKey = (p) => {
    const ids = [];
    for (let i = 0; i < chunks.length; i++) {
      if (p >= chunks[i].start && p < chunks[i].end) ids.push(i);
    }
    return ids.join(" ");
  };
  const segment = (a, b, key) => {
    const body = escapeHtml(text.slice(a, b));
    if (!key) return body;
    const ids = key.split(" ").map(Number);
    const owner = ids[ids.length - 1];
    const overlap = ids.length > 1;
    const style = `background:${chunkColor(owner).bg};` +
      (overlap ? `text-decoration-color:${chunkColor(ids[0]).solid};` : "");
    return `<span class="hl${overlap ? " ov" : ""}" data-chunks="${key}" data-owner="${owner}" style="${style}">${body}</span>`;
  };

  let out = "";
  let segStart = 0;
  let prevKey = null;
  for (let p = 0; p <= text.length; p++) {
    const key = p < text.length ? coverKey(p) : null;
    if (key !== prevKey) {
      if (prevKey !== null) out += segment(segStart, p, prevKey);
      segStart = p;
      prevKey = key;
    }
  }
  return out;
}

function renderChunkingStage() {
  state.chunkingVisited = true;
  const root = html(`<div class="stage-content"></div>`);
  root.appendChild(explainer("Why cut the document into chunks?", `
    <p>An AI can't re-read a whole document every time you ask something. So we cut it into small pieces,
    like <strong>index cards</strong>. Later, we'll hand the AI only the few cards that matter for your question.</p>
    <p><strong>Overlap</strong> means neighbouring cards share a few words, so an idea that gets cut in half
    still appears whole on at least one card.</p>`));

  const controls = html(`
    <div class="card controls">
      <div class="control">
        <div class="control-top"><label for="chunk-size">Chunk size</label><output id="chunk-size-val"></output></div>
        <input type="range" id="chunk-size" min="100" max="600" step="10">
        <div class="control-hint">How many characters fit on one card. Bigger = fewer, longer chunks.</div>
      </div>
      <div class="control">
        <div class="control-top"><label for="chunk-overlap">Overlap</label><output id="chunk-overlap-val"></output></div>
        <input type="range" id="chunk-overlap" min="0" max="120" step="5">
        <div class="control-hint">Characters shared with the previous card (at most half the chunk size).</div>
      </div>
    </div>`);
  const results = html(`<div class="chunk-results"></div>`);
  root.append(controls, results);

  const sizeIn = controls.querySelector("#chunk-size");
  const ovIn = controls.querySelector("#chunk-overlap");
  sizeIn.value = state.chunkSize;
  ovIn.value = state.chunkOverlap;

  // Sliders update the results in place (a full re-render would break the drag).
  const sync = () => {
    controls.querySelector("#chunk-size-val").textContent = `${state.chunkSize} chars`;
    const eff = effectiveOverlap();
    controls.querySelector("#chunk-overlap-val").textContent =
      eff < state.chunkOverlap ? `${eff} chars (capped)` : `${eff} chars`;
    renderChunkResults(results);
  };
  sizeIn.addEventListener("input", () => { state.chunkSize = +sizeIn.value; ensureChunks(); sync(); });
  ovIn.addEventListener("input", () => { state.chunkOverlap = +ovIn.value; ensureChunks(); sync(); });
  sync();
  return root;
}

function renderChunkResults(container) {
  const doc = activeDocument();
  const chunks = state.chunks;
  const avgWords = Math.round(chunks.reduce((a, c) => a + wordCount(c.text), 0) / chunks.length);

  const cards = chunks.map((c, i) => {
    const col = chunkColor(i);
    const prev = chunks[i - 1];
    const ovLen = prev && prev.end > c.start ? Math.min(prev.end - c.start, c.text.length) : 0;
    const ov = ovLen
      ? `<mark class="ov" title="Overlap: also at the end of Chunk ${i}">${escapeHtml(c.text.slice(0, ovLen))}</mark>`
      : "";
    return `
      <div class="chunk-card" data-chunk="${i}" style="--c-bg:${col.bg};--c-solid:${col.solid}">
        <div class="chunk-card-head">
          <span class="dot"></span><b>Chunk ${i + 1}</b>
          <span class="muted">${wordCount(c.text)} words · ${c.text.length} chars</span>
        </div>
        <div class="chunk-text">${ov}${escapeHtml(c.text.slice(ovLen))}</div>
      </div>`;
  }).join("");

  container.innerHTML = `
    <div class="stats-bar">
      <div class="stat"><b>${chunks.length}</b><span>chunks</span></div>
      <div class="stat"><b>${avgWords}</b><span>words per chunk (avg)</span></div>
      <div class="stat"><b>${doc.text.length.toLocaleString()}</b><span>characters in document</span></div>
    </div>
    <div class="two-col">
      <div class="card">
        <div class="card-title">Your document, colored by chunk</div>
        <div class="doc-hl">${buildHighlightedHtml(doc.text, chunks)}</div>
        <div class="legend-row"><span class="ov-swatch"></span>dashed underline = overlap (text shared by two chunks)</div>
      </div>
      <div class="card">
        <div class="card-title">The chunks <span class="muted">· hover one to find it in the document</span></div>
        <div class="chunk-list">${cards}</div>
      </div>
    </div>`;

  const docHl = container.querySelector(".doc-hl");
  const list = container.querySelector(".chunk-list");

  const focus = (i, scrollCard) => {
    docHl.classList.toggle("focusing", i != null);
    container.querySelectorAll(".active").forEach((el) => el.classList.remove("active"));
    if (i == null) return;
    docHl.querySelectorAll(`.hl[data-chunks~="${i}"]`).forEach((el) => el.classList.add("active"));
    const card = list.querySelector(`.chunk-card[data-chunk="${i}"]`);
    card.classList.add("active");
    if (scrollCard) {
      const top = card.offsetTop - list.offsetTop;
      if (top < list.scrollTop || top + card.offsetHeight > list.scrollTop + list.clientHeight) {
        list.scrollTo({ top: top - 8, behavior: "smooth" });
      }
    }
  };

  list.addEventListener("mouseover", (e) => {
    const card = e.target.closest(".chunk-card");
    if (card) focus(+card.dataset.chunk, false);
  });
  list.addEventListener("mouseleave", () => focus(null));
  docHl.addEventListener("mouseover", (e) => {
    const span = e.target.closest(".hl");
    if (span) focus(+span.dataset.owner, true);
  });
  docHl.addEventListener("mouseleave", () => focus(null));
}

// ============================================================
// Step 2: Chunk embeddings (real model: all-MiniLM-L6-v2)
// ============================================================

const TRANSFORMERS_URL = "https://cdn.jsdelivr.net/npm/@huggingface/transformers@3.8.1";
const EMBED_MODEL = "Xenova/all-MiniLM-L6-v2";

let embedderPromise = null;
const modelFiles = {}; // file -> {loaded, total}, for the download progress bar

function onModelProgress(p) {
  if (p.status !== "progress" || !p.total) return;
  modelFiles[p.file] = { loaded: p.loaded, total: p.total };
  let loaded = 0;
  let total = 0;
  for (const f of Object.values(modelFiles)) { loaded += f.loaded; total += f.total; }
  state.embed.loaded = loaded;
  state.embed.total = total;

  // Update in place: progress fires many times per second.
  const fill = document.getElementById("embed-progress-fill");
  const label = document.getElementById("embed-progress-label");
  if (fill) fill.style.width = `${((loaded / total) * 100).toFixed(1)}%`;
  if (label) label.textContent = `${(loaded / 1e6).toFixed(1)} / ${(total / 1e6).toFixed(1)} MB`;
  const chatLabel = document.getElementById("chat-dl-label");
  if (chatLabel) chatLabel.textContent = `(${Math.round((loaded / total) * 100)}%)`;
}

function loadEmbedder() {
  if (!embedderPromise) {
    embedderPromise = (async () => {
      const { pipeline } = await import(TRANSFORMERS_URL);
      return pipeline("feature-extraction", EMBED_MODEL, { dtype: "q8", progress_callback: onModelProgress });
    })();
    embedderPromise.catch(() => { embedderPromise = null; }); // allow retry
  }
  return embedderPromise;
}

// Mean over token vectors, then scale to length 1.
// Mean over tokens, then L2-normalize. Intermediate values are returned for the Step 2/5 math view.
function poolSteps(tokenVecs) {
  const d = tokenVecs[0].length;
  const mean = new Float32Array(d);
  for (const v of tokenVecs) for (let j = 0; j < d; j++) mean[j] += v[j] / tokenVecs.length;
  let norm = 0;
  for (let j = 0; j < d; j++) norm += mean[j] * mean[j];
  norm = Math.sqrt(norm) || 1;
  const vec = new Float32Array(d);
  for (let j = 0; j < d; j++) vec[j] = mean[j] / norm;
  return { mean, norm, vec };
}

function meanPoolNormalize(tokenVecs) {
  return poolSteps(tokenVecs).vec;
}

async function tokenizeText(text) {
  const ex = await loadEmbedder();
  const { input_ids } = ex.tokenizer(text, { truncation: true });
  const ids = Array.from(input_ids.data, Number);
  const pieces = ex.tokenizer.model.convert_ids_to_tokens(ids);
  return ids.map((id, i) => ({ id, text: pieces[i] }));
}

// One model run: real WordPiece tokens, each token's contextual vector, and the pooled vector.
async function embedText(text) {
  const ex = await loadEmbedder();
  const out = await ex(text, { pooling: "none" });
  const [, T, D] = out.dims;
  const tokenVecs = [];
  for (let t = 0; t < T; t++) tokenVecs.push(out.data.slice(t * D, (t + 1) * D));
  const tokens = await tokenizeText(text);
  return { tokens, tokenVecs, vec: meanPoolNormalize(tokenVecs) };
}

// Vectors are unit-length, so cosine similarity is a plain dot product.
function cosineSim(a, b) {
  let s = 0;
  for (let i = 0; i < a.length; i++) s += a[i] * b[i];
  return s;
}

async function generateEmbeddings() {
  const es = state.embed;
  try {
    if (!state.embedderReady) {
      es.status = "loading-model";
      renderAll();
      await loadEmbedder();
      state.embedderReady = true;
    }
    const key = state.chunkKey;
    state.embed.status = "embedding";
    state.embed.done = state.chunks.filter((c) => c.vec).length;
    renderAll();
    for (const [i, c] of state.chunks.entries()) {
      if (c.vec) continue;
      const r = await embedText(c.text);
      if (state.chunkKey !== key) return; // chunks changed mid-run; ensureChunks reset the status
      Object.assign(c, r);
      state.embed.done++;
      state.embed.justDone = i; // animate only the newest strip
      renderAll();
    }
    buildBm25Index();
    state.embed.status = "idle";
    state.embed.justDone = null;
  } catch (err) {
    console.error(err);
    state.embed.status = "error";
    state.embed.error = "Couldn't load the embedding model. Check your internet connection and try again.";
  }
  renderAll();
}

function renderEmbedStage() {
  const root = html(`<div class="stage-content"></div>`);
  root.appendChild(explainer("What is an embedding?", `
    <p>Computers can't compare meanings directly, but they're great with numbers. An <strong>embedding model</strong>
    reads a piece of text and turns it into a list of <strong>384 numbers</strong>, like coordinates for its meaning
    on a giant map.</p>
    <p>Texts that mean similar things get similar numbers, even when they use different words.
    Here we run a real model (<code>all-MiniLM-L6-v2</code>) right inside your browser.</p>`));

  root.appendChild(renderEmbedAction());

  const chunks = state.chunks;
  const vecs = chunks.filter((c) => c.vec).map((c) => c.vec);
  const scale = vecs.length ? heatmapScale(vecs) : 1;

  const listCard = html(`
    <div class="card">
      <div class="card-title">Each chunk → one vector <span class="muted">· click a chunk to see how it's made</span></div>
      <div class="vec-list"></div>
    </div>`);
  const list = listCard.querySelector(".vec-list");
  chunks.forEach((c, i) => {
    const col = chunkColor(i);
    const row = html(`
      <button type="button" class="vec-row${c.vec ? "" : " pending"}${i === state.selectedChunk && c.vec ? " selected" : ""}"
        style="--c-solid:${col.solid}">
        <span class="vec-row-label"><span class="dot"></span>Chunk ${i + 1}</span>
        <span class="vec-row-text">${escapeHtml(snippet(c.text, 60))}</span>
        <span class="vec-row-strip"></span>
      </button>`);
    const strip = row.querySelector(".vec-row-strip");
    strip.appendChild(
      c.vec ? heatmapStrip(c.vec, scale, i === state.embed.justDone ? "fresh" : "") : html(`<span class="shimmer"></span>`));
    if (c.vec) {
      strip.appendChild(html(`<span class="vec-row-nums">[${Array.from(c.vec.slice(0, 4), fmtNum).join(", ")}, … ${c.vec.length} numbers]</span>`));
    }
    row.disabled = !c.vec;
    row.addEventListener("click", () => {
      if (state.selectedChunk !== i) state.embedView = { dimStart: 0, dim: 0, allTokens: false };
      state.selectedChunk = i;
      renderAll();
    });
    list.appendChild(row);
  });
  if (vecs.length) listCard.appendChild(heatmapLegend());
  root.appendChild(listCard);

  const sel = chunks[state.selectedChunk];
  if (sel && sel.vec) root.appendChild(renderTokenFlow(sel, `How Chunk ${state.selectedChunk + 1} becomes one vector`));
  if (state.embedderReady) root.appendChild(renderLab());
  return root;
}

function renderEmbedAction() {
  const es = state.embed;
  const n = state.chunks.length;
  const card = html(`<div class="card action-card"></div>`);

  if (es.status === "loading-model") {
    const pct = es.total ? (es.loaded / es.total) * 100 : 0;
    card.innerHTML = `
      <div class="action-title">⬇️ Downloading the embedding model…</div>
      <div class="progress"><div id="embed-progress-fill" class="progress-fill" style="width:${pct}%"></div></div>
      <div class="progress-meta"><span id="embed-progress-label">Starting…</span>
        <span class="muted">Only the first time. Your browser keeps a copy afterwards.</span></div>`;
  } else if (es.status === "embedding") {
    card.innerHTML = `
      <div class="action-title">🔢 Embedding chunk ${Math.min(es.done + 1, n)} of ${n}…</div>
      <div class="progress"><div class="progress-fill" style="width:${(es.done / n) * 100}%"></div></div>
      <div class="progress-meta muted">Watch each chunk's numbers appear below.</div>`;
  } else if (es.status === "error") {
    card.innerHTML = `<div class="action-error">⚠️ ${escapeHtml(es.error)}</div>
      <button type="button" class="btn primary">Try again</button>`;
    card.querySelector("button").addEventListener("click", generateEmbeddings);
  } else if (!isStageComplete("embed")) {
    card.innerHTML = `
      <button type="button" class="btn primary big">✨ Generate Chunk Embeddings</button>
      <div class="muted small">The first run downloads a ~23 MB model into your browser. After that, it's instant.</div>`;
    card.querySelector("button").addEventListener("click", generateEmbeddings);
  } else {
    card.classList.add("success");
    card.innerHTML = `<div class="action-title">✅ All ${n} chunks embedded.
      Each one is now a list of 384 numbers.</div>`;
  }
  return card;
}

function renderTokenFlow(chunk, title, whole = "chunk") {
  const card = html(`
    <div class="card token-flow">
      <div class="card-title">${title}</div>
      <div class="flow-step">
        <div class="flow-num">1</div>
        <div class="flow-main">
          <h4>Split into tokens</h4>
          <p class="muted">The model cuts text into word pieces it knows. Rare words get split into smaller pieces
          (marked <code>##</code>). <code>[CLS]</code> and <code>[SEP]</code> mark the start and end.
          Hover a token to see its ID number.</p>
          <div class="token-chips"></div>
        </div>
      </div>
      <div class="flow-arrow">↓</div>
      <div class="flow-step">
        <div class="flow-num">2</div>
        <div class="flow-main">
          <h4>One vector per token</h4>
          <p class="muted">The model reads all the tokens together, so each token's 384 numbers depend on its
          neighbours. ("bank" gets different numbers in "river bank" and "bank account".) Each row is one token,
          each column one of the 384 numbers. The bottom two rows turn all the rows into a single vector.
          Click a column to see the math.</p>
          <div class="emb-table-wrap"></div>
        </div>
      </div>
      <div class="flow-arrow">↓ average each column, then scale to length 1</div>
      <div class="flow-step">
        <div class="flow-num">3</div>
        <div class="flow-main">
          <h4>One vector for the whole ${whole}</h4>
          <p class="muted">This single list of 384 numbers is what gets ${whole === "chunk" ? "stored and searched" : "compared with every chunk"}.</p>
          <div class="pooled"></div>
        </div>
      </div>
    </div>`);

  card.querySelector(".token-chips").innerHTML = tokenChipsHtml(chunk.tokens, false);

  card.querySelector(".emb-table-wrap").append(...renderEmbTable(chunk, whole));

  const pooled = card.querySelector(".pooled");
  pooled.appendChild(heatmapStrip(chunk.vec, heatmapScale([chunk.vec]), "big"));
  pooled.appendChild(heatmapLegend());
  return card;
}

const fmtNum = (v) => v.toFixed(3);
const EMB_COLS = 6;      // dimension columns visible at once
const EMB_ROWS = 12;     // token rows before "show more"
const EMB_TERMS = 6;     // terms written out in the average formula

// Token × dimension table with the pooling rows, plus the worked math for one dimension.
function renderEmbTable(item, whole) {
  const view = state.embedView;
  const { tokens, tokenVecs, vec } = item;
  const d = vec.length;
  const n = tokenVecs.length;
  const { mean, norm } = poolSteps(tokenVecs);
  const scale = heatmapScale(tokenVecs);
  const meanScale = heatmapScale([mean]);
  const pooledScale = heatmapScale([vec]);
  view.dimStart = Math.max(0, Math.min(view.dimStart, d - EMB_COLS));
  view.dim = Math.max(0, Math.min(view.dim, d - 1));
  const dims = Array.from({ length: EMB_COLS }, (_, k) => view.dimStart + k);

  const cell = (v, sc, j) => {
    const [r, g, b] = heatmapColor(v, sc);
    const dark = Math.abs(v / sc) > 0.6;
    return `<td data-dim="${j}" class="num${j === view.dim ? " emb-sel" : ""}${dark ? " dark" : ""}"
      style="background:rgb(${r},${g},${b})">${fmtNum(v)}</td>`;
  };

  const pager = html(`
    <div class="dim-pager">
      <button type="button" class="btn secondary" data-step="-1">◀</button>
      <span>Numbers <b>${view.dimStart + 1}–${view.dimStart + EMB_COLS}</b> of ${d}</span>
      <button type="button" class="btn secondary" data-step="1">▶</button>
      <span class="muted small">${n} tokens</span>
    </div>`);
  pager.querySelectorAll("[data-step]").forEach((b) => {
    const step = Number(b.dataset.step) * EMB_COLS;
    b.disabled = step < 0 ? view.dimStart === 0 : view.dimStart + EMB_COLS >= d;
    b.addEventListener("click", () => {
      view.dimStart = Math.max(0, Math.min(view.dimStart + step, d - EMB_COLS));
      view.dim = view.dimStart;
      renderAll();
    });
  });

  const shown = view.allTokens ? n : Math.min(n, EMB_ROWS);
  const rows = tokenVecs.slice(0, shown).map((v, k) =>
    `<tr><th class="tok-cell">${escapeHtml(tokens[k]?.text ?? "")}</th>${dims.map((j) => cell(v[j], scale, j)).join("")}<td class="strip-cell"></td></tr>`).join("");
  const scroll = html(`
    <div class="emb-scroll">
      <table class="emb-table">
        <thead><tr><th class="tok-cell">token</th>
          ${dims.map((j) => `<th data-dim="${j}" class="${j === view.dim ? "emb-sel" : ""}">#${j + 1}</th>`).join("")}
          <th class="strip-cell">all ${d}</th></tr></thead>
        <tbody>${rows}</tbody>
        <tfoot>
          <tr class="emb-avg"><th class="tok-cell">average</th>${dims.map((j) => cell(mean[j], meanScale, j)).join("")}<td class="strip-cell"></td></tr>
          <tr class="emb-final"><th class="tok-cell">÷ length = final</th>${dims.map((j) => cell(vec[j], pooledScale, j)).join("")}<td class="strip-cell"></td></tr>
        </tfoot>
      </table>
    </div>`);
  const strips = scroll.querySelectorAll(".strip-cell");
  tokenVecs.slice(0, shown).forEach((v, k) => strips[k + 1].appendChild(heatmapStrip(v, scale)));
  strips[shown + 1].appendChild(heatmapStrip(mean, meanScale));
  strips[shown + 2].appendChild(heatmapStrip(vec, pooledScale));
  scroll.querySelector("table").addEventListener("click", (e) => {
    const t = e.target.closest("[data-dim]");
    if (!t) return;
    view.dim = Number(t.dataset.dim);
    renderAll();
  });

  const out = [pager, scroll];
  if (n > EMB_ROWS) {
    const more = html(`<button type="button" class="link-btn emb-more"></button>`);
    more.textContent = view.allTokens ? "Show fewer tokens" : `+ ${n - EMB_ROWS} more tokens`;
    more.addEventListener("click", () => { view.allTokens = !view.allTokens; renderAll(); });
    out.push(more);
  }

  // Worked math for the selected dimension.
  const j = view.dim;
  const col = tokenVecs.map((v) => v[j]);
  const sum = col.reduce((a, b) => a + b, 0);
  const term = (v) => (v < 0 ? `(${fmtNum(v)})` : fmtNum(v));
  const terms = col.slice(0, EMB_TERMS).map(term).join(" + ") + (n > EMB_TERMS ? ` + … + ${term(col[n - 1])}` : "");
  const matches = Math.abs(mean[j] / norm - vec[j]) < 1e-5;
  out.push(html(`
    <div class="pool-math">
      <div class="pool-math-title">The math for number #${j + 1}</div>
      <div class="pool-line"><span class="pool-label">1. Average</span>
        <code>(${terms}) ÷ ${n} tokens = ${fmtNum(sum)} ÷ ${n} = <b>${fmtNum(mean[j])}</b></code>
        <span class="muted small">Every token votes, so the whole ${whole} ends up with one set of 384 numbers.</span></div>
      <div class="pool-line"><span class="pool-label">2. Length</span>
        <code>√(#1² + #2² + … + #${d}²) of the average row = <b>${fmtNum(norm)}</b></code>
        <span class="muted small">This measures how "long" the vector is. It's the same for all 384 numbers.</span></div>
      <div class="pool-line"><span class="pool-label">3. Final</span>
        <code>${fmtNum(mean[j])} ÷ ${fmtNum(norm)} = <b>${fmtNum(vec[j])}</b></code>
        ${matches ? `<span class="pool-ok">✓ matches number #${j + 1} of the ${whole}'s vector</span>` : ""}
        <span class="muted small">Dividing by the length makes every vector length 1, so long and short texts compare fairly
        and similarity becomes a simple multiply-and-add.</span></div>
    </div>`));
  return out;
}

const LAB_PRESETS = [
  ["dog", "puppy"],
  ["dog", "volcano"],
  ["Where does the Pope live?", "Vatican City"],
  ["I love this movie", "This film is great"],
];

function similarityVerdict(s) {
  if (s >= 0.7) return "Very similar meaning";
  if (s >= 0.5) return "Related";
  if (s >= 0.3) return "Loosely related";
  return "Not really related";
}

function renderLab() {
  const lab = state.lab;
  const card = html(`
    <div class="card lab">
      <div class="card-title">🧪 Try it: compare two phrases</div>
      <p class="muted">Type any two phrases. The model turns each into 384 numbers, and we measure how close they are.</p>
      <div class="lab-presets"></div>
      <div class="lab-inputs">
        <input type="text" class="lab-a" maxlength="200">
        <span class="lab-vs">vs</span>
        <input type="text" class="lab-b" maxlength="200">
        <button type="button" class="btn primary lab-go">Compare</button>
      </div>
      <div class="lab-result"></div>
    </div>`);

  const a = card.querySelector(".lab-a");
  const b = card.querySelector(".lab-b");
  a.value = lab.a;
  b.value = lab.b;
  a.addEventListener("input", () => { lab.a = a.value; });
  b.addEventListener("input", () => { lab.b = b.value; });

  const run = async () => {
    if (!lab.a.trim() || !lab.b.trim() || lab.busy) return;
    lab.busy = true;
    lab.error = "";
    renderAll();
    try {
      const ra = await embedText(lab.a);
      const rb = await embedText(lab.b);
      lab.result = { a: lab.a, b: lab.b, va: ra.vec, vb: rb.vec, sim: cosineSim(ra.vec, rb.vec) };
    } catch (err) {
      console.error(err);
      lab.error = "Something went wrong running the model. Try again.";
    }
    lab.busy = false;
    renderAll();
  };

  const go = card.querySelector(".lab-go");
  go.disabled = lab.busy;
  go.textContent = lab.busy ? "Comparing…" : "Compare";
  go.addEventListener("click", run);
  [a, b].forEach((inp) => inp.addEventListener("keydown", (e) => { if (e.key === "Enter") run(); }));

  const presets = card.querySelector(".lab-presets");
  LAB_PRESETS.forEach(([pa, pb]) => {
    const chip = html(`<button type="button" class="chip"></button>`);
    chip.textContent = `${pa} / ${pb}`;
    chip.addEventListener("click", () => { lab.a = pa; lab.b = pb; run(); });
    presets.appendChild(chip);
  });

  const out = card.querySelector(".lab-result");
  if (lab.error) out.appendChild(html(`<div class="action-error">⚠️ ${escapeHtml(lab.error)}</div>`));
  if (lab.result) {
    const r = lab.result;
    const scale = heatmapScale([r.va, r.vb]);
    const rowA = html(`<div class="lab-row"><span class="lab-label"></span></div>`);
    rowA.querySelector(".lab-label").textContent = `“${r.a}”`;
    rowA.appendChild(heatmapStrip(r.va, scale));
    const rowB = html(`<div class="lab-row"><span class="lab-label"></span></div>`);
    rowB.querySelector(".lab-label").textContent = `“${r.b}”`;
    rowB.appendChild(heatmapStrip(r.vb, scale));
    const pct = Math.max(0, Math.min(1, r.sim)) * 100;
    const meter = html(`
      <div class="meter">
        <div class="meter-top"><span>Similarity</span><b>${r.sim.toFixed(2)}</b></div>
        <div class="meter-track"><div class="meter-fill" style="width:${pct}%"></div></div>
        <div class="meter-scale"><span>0 · unrelated</span><span>1 · same meaning</span></div>
        <div class="meter-verdict">${similarityVerdict(r.sim)}</div>
      </div>`);
    out.append(rowA, rowB, meter);
  }
  return card;
}

// ============================================================
// Step 3: Vector store
// ============================================================

function dot(a, b) {
  let s = 0;
  for (let i = 0; i < a.length; i++) s += a[i] * b[i];
  return s;
}

// 2D PCA via power iteration, fitted on the chunk vectors only (so adding the
// question's star later doesn't move the chunks).
function chunkPca(vecs) {
  const n = vecs.length;
  const d = vecs[0].length;
  const mean = new Float64Array(d);
  for (const v of vecs) for (let j = 0; j < d; j++) mean[j] += v[j] / n;
  const X = vecs.map((v) => Float64Array.from(v, (x, j) => x - mean[j]));

  const comps = [];
  for (let k = 0; k < 2; k++) {
    let w = Float64Array.from({ length: d }, (_, j) => Math.sin(j * (k + 1) + 1));
    for (let it = 0; it < 200; it++) {
      const xw = X.map((x) => dot(x, w));
      const nw = new Float64Array(d);
      X.forEach((x, i) => { for (let j = 0; j < d; j++) nw[j] += x[j] * xw[i]; });
      for (const c of comps) {
        const p = dot(nw, c);
        for (let j = 0; j < d; j++) nw[j] -= p * c[j];
      }
      const norm = Math.sqrt(dot(nw, nw));
      if (norm < 1e-12) break;
      w = nw.map((x) => x / norm);
    }
    comps.push(w);
  }
  const project = (v) => {
    const x = Float64Array.from(v, (val, j) => val - mean[j]);
    return [dot(x, comps[0]), dot(x, comps[1])];
  };
  return { coords: vecs.map(project), project };
}

// opts: {queryVec, links: Set of chunk ids to connect to the star,
//        faded: Set of chunk ids to dim, selected: chunk id}
function renderScatter(onSelect, opts = {}) {
  const W = 640;
  const H = 380;
  const pad = 40;
  const pca = chunkPca(state.chunks.map((c) => c.vec));
  const pts = pca.coords.slice();
  const hasQuery = !!opts.queryVec;
  if (hasQuery) pts.push(pca.project(opts.queryVec));

  const xs = pts.map((p) => p[0]);
  const ys = pts.map((p) => p[1]);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  // One scale for both axes, so the map isn't stretched.
  const s = Math.min((W - 2 * pad) / (x1 - x0 || 1), (H - 2 * pad) / (y1 - y0 || 1));
  const cx = (W - (x1 - x0) * s) / 2;
  const cy = (H - (y1 - y0) * s) / 2;

  const pos = pts.map(([x, y]) => [cx + (x - x0) * s, H - (cy + (y - y0) * s)]);
  // Nudge apart dots that would overlap, so every chunk stays visible.
  const MIN_GAP = 34;
  for (let pass = 0; pass < 30; pass++) {
    let moved = false;
    for (let a = 0; a < pos.length; a++) {
      for (let b = a + 1; b < pos.length; b++) {
        const dx = pos[b][0] - pos[a][0];
        const dy = pos[b][1] - pos[a][1];
        const dist = Math.hypot(dx, dy);
        if (dist >= MIN_GAP) continue;
        const ang = dist > 1e-6 ? Math.atan2(dy, dx) : (a + b) * 2.4;
        const push = (MIN_GAP - dist) / 2;
        pos[a][0] -= Math.cos(ang) * push; pos[a][1] -= Math.sin(ang) * push;
        pos[b][0] += Math.cos(ang) * push; pos[b][1] += Math.sin(ang) * push;
        moved = true;
      }
    }
    for (const p of pos) {
      p[0] = Math.max(20, Math.min(W - 20, p[0]));
      p[1] = Math.max(20, Math.min(H - 20, p[1]));
    }
    if (!moved) break;
  }

  const n = state.chunks.length;
  const qPos = hasQuery ? pos[n] : null;
  const selected = "selected" in opts ? opts.selected : state.storeSelected;

  const lines = hasQuery && opts.links
    ? [...opts.links].map((i) => `<line class="q-link" x1="${qPos[0].toFixed(1)}" y1="${qPos[1].toFixed(1)}"
        x2="${pos[i][0].toFixed(1)}" y2="${pos[i][1].toFixed(1)}" stroke="${chunkColor(i).solid}"></line>`).join("")
    : "";

  const circles = pos.slice(0, n).map(([px, py], i) => {
    const col = chunkColor(i);
    const cls = `pt${selected === i ? " selected" : ""}${opts.faded?.has(i) ? " faded" : ""}`;
    return `<g class="${cls}" data-chunk="${i}" transform="translate(${px.toFixed(1)},${py.toFixed(1)})">
      <g class="pt-inner" style="animation-delay:${i * 60}ms">
        <circle r="15" fill="${col.solid}"></circle>
        <text dy="4.5">${i + 1}</text>
      </g>
    </g>`;
  }).join("");

  const star = hasQuery
    ? `<g class="q-star" transform="translate(${qPos[0].toFixed(1)},${qPos[1].toFixed(1)})">
        <g class="pt-inner" style="animation-delay:${n * 60 + 150}ms">
          <circle r="22" class="q-halo"></circle>
          <path d="M0,-17 L4.9,-6.2 L16.2,-5.3 L7.6,2.4 L10,13.8 L0,7.8 L-10,13.8 L-7.6,2.4 L-16.2,-5.3 L-4.9,-6.2 Z"></path>
        </g>
        <text class="q-label" y="34">your question</text>
      </g>`
    : "";

  const svg = html(`
    <svg class="scatter" viewBox="0 0 ${W} ${H}" role="img" aria-label="Map of chunks by meaning">
      <defs>
        <pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse">
          <path d="M 40 0 L 0 0 0 40" fill="none" stroke="#e5e7eb" stroke-width="1"></path>
        </pattern>
      </defs>
      <rect width="${W}" height="${H}" fill="url(#grid)" rx="10"></rect>
      ${lines}${circles}${star}
    </svg>`);
  if (onSelect) {
    svg.addEventListener("click", (e) => {
      const pt = e.target.closest(".pt");
      if (pt) onSelect(+pt.dataset.chunk);
    });
  }
  return svg;
}

function renderStoreStage() {
  state.storeVisited = true;
  const chunks = state.chunks;
  const root = html(`<div class="stage-content"></div>`);

  root.appendChild(explainer("What is a vector store?", `
    <p>A vector store is a special database. Each record holds a chunk's <strong>text</strong> and its
    <strong>vector</strong> (the 384 numbers from Step 2).</p>
    <p>Think of a library sorted by <em>meaning</em> instead of the alphabet: chunks about similar things sit on
    nearby shelves. When a question arrives, we'll look on the shelves closest to it.</p>`));

  const kb = (chunks.length * 384 * 4) / 1024;
  root.appendChild(html(`
    <div class="stats-bar">
      <div class="stat"><b>${chunks.length}</b><span>records stored</span></div>
      <div class="stat"><b>384</b><span>numbers per vector</span></div>
      <div class="stat"><b>${kb.toFixed(1)} KB</b><span>of vectors</span></div>
    </div>`));

  const select = (i) => {
    state.storeSelected = state.storeSelected === i ? null : i;
    root.querySelectorAll(".pt").forEach((el) =>
      el.classList.toggle("selected", +el.dataset.chunk === state.storeSelected));
    root.querySelectorAll(".vs-table tbody tr").forEach((tr) => {
      const on = +tr.dataset.chunk === state.storeSelected;
      tr.classList.toggle("selected", on);
      tr.classList.toggle("expanded", on);
    });
  };

  const mapCard = html(`
    <div class="card">
      <div class="card-title">🗺️ Map of meaning <span class="muted">· hover a dot to read it, click to find it in the table</span></div>
      <div class="scatter-wrap"></div>
      <p class="caption">Each dot is one chunk. Dots that sit close together have similar meaning.
      ${state.docSource === "sample" ? "See how chunks about the same topic (Rome, bees, volcanoes, the sky) huddle together? " : ""}
      <span class="muted">The real vectors have 384 dimensions; we squash them down to 2 so you can see them,
      so distances here are only approximate.</span></p>
    </div>`);
  mapCard.querySelector(".scatter-wrap").appendChild(renderScatter(select));
  root.appendChild(mapCard);

  const scale = heatmapScale(chunks.map((c) => c.vec));
  const tableCard = html(`
    <div class="card">
      <div class="card-title">🗄️ Inside the vector store <span class="muted">· click a row to see the full text</span></div>
      <div class="table-wrap">
        <table class="vs-table">
          <thead><tr><th>ID</th><th>Text</th><th>Vector (384 numbers)</th><th>Metadata</th></tr></thead>
          <tbody></tbody>
        </table>
      </div>
    </div>`);
  const tbody = tableCard.querySelector("tbody");
  chunks.forEach((c, i) => {
    const first = Array.from(c.vec.slice(0, 3), (v) => v.toFixed(3)).join(", ");
    const tr = html(`
      <table><tbody><tr data-chunk="${i}" style="--c-solid:${chunkColor(i).solid}">
        <td class="vs-id"><span class="dot"></span>chunk_${i + 1}</td>
        <td class="vs-text"><span class="short">${escapeHtml(snippet(c.text, 80))}</span><span class="full">${escapeHtml(c.text)}</span></td>
        <td class="vs-vec"><div class="vec-nums">[${first}, … ]</div></td>
        <td class="vs-meta">chars ${c.start}–${c.end}<br>${wordCount(c.text)} words</td>
      </tr></tbody></table>`).querySelector("tr");
    tr.querySelector(".vs-vec").prepend(heatmapStrip(c.vec, scale));
    if (state.storeSelected === i) tr.classList.add("selected", "expanded");
    tr.addEventListener("click", () => select(i));
    tbody.appendChild(tr);
  });
  root.appendChild(tableCard);

  root.appendChild(html(`
    <div class="callout success">
      <div class="callout-icon">🎉</div>
      <div><b>Indexing done!</b> Your document is chunked, embedded and stored.
      Next, we'll ask it a question.</div>
    </div>`));
  return root;
}

// ============================================================
// Querying phase: shared helpers
// ============================================================

const MODE_LABEL = { cosine: "Meaning (cosine)", bm25: "Keywords (BM25)", hybrid: "Hybrid" };
const MEDALS = ["🥇", "🥈", "🥉"];

function rankBadge(r) {
  return MEDALS[r] || `#${r + 1}`;
}

function freshGen() {
  return { status: "idle", answer: null, truncated: false, error: "" };
}

// Everything that depends on the question (or on the chunks it's compared to).
function resetQueryResults() {
  state.activeChatId = null;
  state.inspectFocus = null;
  state.simSelected = null;
  state.similarityVisited = false;
  state.topkVisited = false;
  state.inspectVisited = false;
  state.gen = freshGen();
  state.noRag = freshGen();
}

function resetQuery() {
  state.queryTokens = null;
  state.queryEmbed = null;
  state.queryError = "";
  resetQueryResults();
}

// ---------- BM25 (real Okapi BM25, purely lexical) ----------

const BM25_K1 = 1.2; // term-frequency saturation
const BM25_B = 0.75; // document-length normalization

const STOPWORDS = new Set((
  "a an the is are was were be been being am of in on at to for from by with and or but if then so as it its " +
  "this that these those what which who whom whose where when why how do does did can could should would will " +
  "shall may might must i you he she we they me him her us them my your his our their not no there here about " +
  "into over under than too very just also some any all each most more other such only own same s t"
).split(" "));

function simpleTokenize(text) {
  return text.toLowerCase().match(/[a-z0-9]+/g) || [];
}

function contentWordsOf(text) {
  return simpleTokenize(text).filter((w) => w.length > 1 && !STOPWORDS.has(w));
}

function buildBm25Index() {
  const N = state.chunks.length;
  const df = new Map();
  let totalLen = 0;
  for (const c of state.chunks) {
    const words = contentWordsOf(c.text);
    c.bm25Len = words.length;
    c.bm25Tf = new Map();
    for (const w of words) c.bm25Tf.set(w, (c.bm25Tf.get(w) || 0) + 1);
    for (const w of c.bm25Tf.keys()) df.set(w, (df.get(w) || 0) + 1);
    totalLen += words.length;
  }
  state.bm25N = N;
  state.bm25AvgLen = totalLen / N || 1;
  state.bm25Idf = (w) => {
    const n = df.get(w) || 0;
    return Math.log(1 + (N - n + 0.5) / (n + 0.5));
  };
}

function queryTerms(query = state.query) {
  return [...new Set(contentWordsOf(query))];
}

// Per-word breakdown, so the UI can show exactly how the score was built.
function bm25Breakdown(chunk, query = state.query) {
  if (!state.bm25Idf) buildBm25Index();
  const lenNorm = 1 - BM25_B + BM25_B * (chunk.bm25Len / state.bm25AvgLen);
  const rows = queryTerms(query).map((term) => {
    const tf = chunk.bm25Tf.get(term) || 0;
    const idf = state.bm25Idf(term);
    const score = tf ? (idf * (tf * (BM25_K1 + 1))) / (tf + BM25_K1 * lenNorm) : 0;
    return { term, tf, idf, score };
  });
  return { rows, lenNorm, total: rows.reduce((a, r) => a + r.score, 0) };
}

function bm25Score(chunk, query = state.query) {
  return bm25Breakdown(chunk, query).total;
}

// Scores every chunk under a retrieval mode, best first. `ctx` supplies the
// question ({queryEmbed, query, hybridWeight}); the chat passes a snapshot.
function rankChunks(mode = state.retrievalMode, ctx = state) {
  const q = ctx.queryEmbed.vec;
  const rows = state.chunks.map((c, i) => ({ i, cos: cosineSim(q, c.vec), bm25: bm25Score(c, ctx.query) }));
  const maxBm25 = Math.max(0, ...rows.map((r) => r.bm25));
  for (const r of rows) {
    // BM25 is unbounded, so hybrid first divides by this query's top BM25 score.
    r.bm25Norm = maxBm25 > 0 ? r.bm25 / maxBm25 : 0;
    r.score = mode === "cosine" ? r.cos
      : mode === "bm25" ? r.bm25
      : ctx.hybridWeight * r.cos + (1 - ctx.hybridWeight) * r.bm25Norm;
  }
  rows.sort((a, b) => b.score - a.score || a.i - b.i);
  rows.maxBm25 = maxBm25;
  return rows;
}

function retrieved() {
  return rankChunks().slice(0, Math.min(state.topK, state.chunks.length));
}

function chunkTag(i) {
  const col = chunkColor(i);
  return `<span class="chunk-tag" style="--c-solid:${col.solid};--c-bg:${col.bg}"><span class="dot"></span>Chunk ${i + 1}</span>`;
}

// Escapes text and wraps the question's keywords in <mark>.
function highlightKeywords(text, terms) {
  const set = new Set(terms);
  return text.split(/([A-Za-z0-9]+)/).map((part, k) =>
    k % 2 === 1 && set.has(part.toLowerCase()) ? `<mark class="kw">${escapeHtml(part)}</mark>` : escapeHtml(part)
  ).join("");
}

function tokenChipsHtml(tokens, animate) {
  return tokens.map((t, k) => {
    const special = t.text === "[CLS]" || t.text === "[SEP]";
    const delay = animate ? ` style="animation-delay:${k * 50}ms"` : "";
    return `<span class="tok${special ? " special" : ""}${animate ? " pop" : ""}"${delay} title="Token ID ${t.id}">${escapeHtml(t.text)}</span>`;
  }).join("");
}

// ============================================================
// Step 4: Ask a question
// ============================================================

const SAMPLE_QUESTIONS = [
  "Where does the Pope live?",
  "How do bees make honey?",
  "Why is the sky blue?",
  "What comes out of a volcano?",
];
const GENERIC_QUESTIONS = [
  "What is this document about?",
  "What are the main points?",
];

async function askQuestion(text) {
  const q = text.trim();
  if (!q || state.queryBusy) return;
  state.query = q;
  resetQuery();
  state.queryBusy = true;
  renderAll();
  try {
    state.queryTokens = await tokenizeText(q);
  } catch (err) {
    console.error(err);
    state.queryError = "Couldn't read the question with the model. Try again.";
  }
  state.queryBusy = false;
  renderAll();
}

function renderQueryStage() {
  const root = html(`<div class="stage-content"></div>`);
  root.appendChild(explainer("Now we ask a question", `
    <p>Steps 1–3 happened <strong>once</strong>, to prepare the document. From here on, everything happens
    <strong>every time someone asks a question</strong>.</p>
    <p>Type your own question, or pick one of the suggestions.</p>`));

  const ask = html(`
    <div class="card ask-card">
      <div class="ask-row">
        <input type="text" class="ask-input" maxlength="300" placeholder="Ask something about your document…">
        <button type="button" class="btn primary">Ask</button>
      </div>
      <div class="ask-suggest"><span class="muted small">Try:</span></div>
    </div>`);
  const input = ask.querySelector(".ask-input");
  input.value = state.query;
  const btn = ask.querySelector("button");
  btn.disabled = state.queryBusy;
  btn.textContent = state.queryBusy ? "Reading…" : "Ask";
  btn.addEventListener("click", () => askQuestion(input.value));
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") askQuestion(input.value); });

  const suggestions = state.docSource === "sample" ? SAMPLE_QUESTIONS : GENERIC_QUESTIONS;
  suggestions.forEach((q) => {
    const chip = html(`<button type="button" class="chip"></button>`);
    chip.textContent = q;
    chip.addEventListener("click", () => askQuestion(q));
    ask.querySelector(".ask-suggest").appendChild(chip);
  });
  root.appendChild(ask);

  if (state.queryError) root.appendChild(html(`<div class="action-error">⚠️ ${escapeHtml(state.queryError)}</div>`));

  if (state.queryTokens) {
    const words = simpleTokenize(state.query).length;
    const card = html(`
      <div class="card">
        <div class="card-title">How the model reads your question</div>
        <p class="question-quote"></p>
        <div class="token-chips big">${tokenChipsHtml(state.queryTokens, true)}</div>
        <div class="stats-bar compact">
          <div class="stat"><b>${words}</b><span>words (what you see)</span></div>
          <div class="stat"><b>${state.queryTokens.length}</b><span>tokens (what the model sees)</span></div>
        </div>
        <p class="muted small">The model doesn't see words, it sees <strong>tokens</strong>: word pieces, each with an ID
        number (hover one). <code>[CLS]</code> and <code>[SEP]</code> are added to mark the start and end. These are the
        same kind of tokens the chunks were split into in Step 2.</p>
      </div>`);
    card.querySelector(".question-quote").textContent = `“${state.query}”`;
    root.appendChild(card);
  }
  return root;
}

// ============================================================
// Step 5: Query embedding
// ============================================================

async function embedQuery() {
  if (state.queryBusy) return;
  state.queryBusy = true;
  state.queryError = "";
  renderAll();
  try {
    state.queryEmbed = await embedText(state.query);
  } catch (err) {
    console.error(err);
    state.queryError = "Couldn't run the model on your question. Try again.";
  }
  state.queryBusy = false;
  renderAll();
}

function renderQueryEmbedStage() {
  const root = html(`<div class="stage-content"></div>`);
  root.appendChild(explainer("Turn the question into numbers", `
    <p>To compare the question with the chunks, it has to be in the same "language": a list of 384 numbers.
    We use <strong>the exact same model</strong> as in Step 2. A different model would draw a different map,
    and the numbers wouldn't be comparable.</p>`));

  if (!state.queryEmbed) {
    const card = html(`
      <div class="card action-card">
        <p class="question-quote"></p>
        <button type="button" class="btn primary big"></button>
      </div>`);
    card.querySelector(".question-quote").textContent = `“${state.query}”`;
    const b = card.querySelector("button");
    b.textContent = state.queryBusy ? "Embedding…" : "🧭 Embed my question";
    b.disabled = state.queryBusy;
    b.addEventListener("click", embedQuery);
    root.appendChild(card);
    if (state.queryError) root.appendChild(html(`<div class="action-error">⚠️ ${escapeHtml(state.queryError)}</div>`));
    return root;
  }

  root.appendChild(renderTokenFlow(state.queryEmbed, "How your question becomes one vector", "question"));

  const near = rankChunks("cosine").slice(0, 2).map((r) => chunkTag(r.i)).join(" and ");
  const map = html(`
    <div class="card">
      <div class="card-title">🗺️ Where your question lands on the map of meaning</div>
      <div class="scatter-wrap"></div>
      <p class="caption">Your question (★) is closest in meaning to ${near}. That's already a hint about where the
      answer is! <span class="muted">The chunks haven't moved: it's the same map as Step 3, with the question dropped
      onto it. Distances are approximate, because 384 dimensions are squashed into 2.</span></p>
    </div>`);
  map.querySelector(".scatter-wrap").appendChild(renderScatter(null, { queryVec: state.queryEmbed.vec, selected: null }));
  root.appendChild(map);
  return root;
}

// ============================================================
// Step 6: Similarity search
// ============================================================

const MODE_HELP = {
  cosine: `Compares <strong>meaning</strong> using the 384 numbers. It can find the right chunk even when the chunk
    uses completely different words from your question. Scores go from about 0 (unrelated) to 1 (same meaning).`,
  bm25: `Classic <strong>keyword search</strong>, used by search engines for decades. It counts how often your
    question's words appear in a chunk, and gives rare words more weight than common ones. It needs exact word
    matches ("live" and "lives" count as different words). Its scores are <strong>not capped at 1</strong>.`,
  hybrid: `<strong>Mixes both</strong> meaning and keywords. Keyword scores aren't capped at 1, so we first divide
    them by the top keyword score to put both on a 0–1 scale, then blend them with the slider.
    <span class="muted">(Real systems often combine the two <em>rankings</em> instead, e.g. "Reciprocal Rank Fusion",
    but the idea is the same: each method catches what the other misses.)</span>`,
};

function renderSimilarityStage() {
  state.similarityVisited = true;
  const root = html(`<div class="stage-content"></div>`);
  root.appendChild(explainer("Score every chunk", `
    <p>Now we compare your question with <strong>every</strong> chunk and give each one a score: higher means a
    better match. There's more than one way to score, and the method really changes which chunks win.
    Try all three!</p>`));

  const modeCard = html(`
    <div class="card">
      <div class="seg" role="tablist"></div>
      <p class="mode-help"></p>
      <div class="hybrid-ctl" hidden>
        <div class="control-top"><label for="hy-w">Blend</label><output></output></div>
        <input type="range" id="hy-w" min="0" max="1" step="0.05">
        <div class="slider-ends"><span>← all keywords</span><span>all meaning →</span></div>
      </div>
    </div>`);
  const seg = modeCard.querySelector(".seg");
  ["cosine", "bm25", "hybrid"].forEach((m) => {
    const b = html(`<button type="button" class="seg-btn${state.retrievalMode === m ? " on" : ""}">${MODE_LABEL[m]}</button>`);
    b.addEventListener("click", () => { state.retrievalMode = m; state.activeChatId = null; renderAll(); });
    seg.appendChild(b);
  });
  modeCard.querySelector(".mode-help").innerHTML = MODE_HELP[state.retrievalMode];
  const results = html(`<div class="stage-content"></div>`);

  if (state.retrievalMode === "hybrid") {
    const ctl = modeCard.querySelector(".hybrid-ctl");
    ctl.hidden = false;
    const slider = ctl.querySelector("input");
    const out = ctl.querySelector("output");
    const label = () => {
      const w = state.hybridWeight;
      out.textContent = `${Math.round(w * 100)}% meaning · ${Math.round((1 - w) * 100)}% keywords`;
    };
    slider.value = state.hybridWeight;
    label();
    // Update in place: a full re-render would break the drag.
    slider.addEventListener("input", () => {
      state.hybridWeight = +slider.value;
      detachChatView();
      label();
      renderSimResults(results);
    });
  }

  root.append(modeCard, results);
  renderSimResults(results);
  return root;
}

function renderSimResults(container) {
  heatmapRegistry.clear();
  const ranked = rankChunks();
  const mode = state.retrievalMode;
  if (state.simSelected == null || !state.chunks[state.simSelected]) state.simSelected = ranked[0].i;

  const card = html(`
    <div class="card">
      <div class="card-title">Scores for every chunk <span class="muted">· click a bar to see how its score was calculated</span></div>
      <div class="kw-line"></div>
      <div class="score-list"></div>
      <div class="rank-note"></div>
    </div>`);

  if (mode !== "cosine") {
    const terms = queryTerms();
    card.querySelector(".kw-line").innerHTML = terms.length
      ? `<span class="muted small">Keywords BM25 looks for:</span> ${terms.map((t) => `<span class="kw-chip">${escapeHtml(t)}</span>`).join(" ")}
         <span class="muted small">(little words like "the" or "does" are ignored)</span>`
      : `<span class="action-error">Your question has no keywords left after removing little words like "the", so BM25 gives every chunk 0.</span>`;
  }

  const top = Math.max(...ranked.map((r) => r.score), 1e-9);
  const list = card.querySelector(".score-list");
  ranked.forEach((r, rank) => {
    const col = chunkColor(r.i);
    // BM25 has no fixed maximum, so its bars are relative to the top score.
    const width = mode === "bm25" ? (r.score / top) * 100 : r.score * 100;
    const row = html(`
      <button type="button" class="score-row${r.i === state.simSelected ? " selected" : ""}" style="--c-solid:${col.solid};--c-bg:${col.bg}">
        <span class="score-rank">${rankBadge(rank)}</span>
        <span class="score-label"><span class="dot"></span>Chunk ${r.i + 1}</span>
        <span class="score-track"><span class="score-fill" style="width:${Math.max(0, width).toFixed(1)}%"></span></span>
        <span class="score-val">${r.score.toFixed(3)}</span>
        <span class="score-text">${escapeHtml(snippet(state.chunks[r.i].text, 90))}</span>
      </button>`);
    row.addEventListener("click", () => { state.simSelected = r.i; renderSimResults(container); });
    list.appendChild(row);
  });

  // Point out what changed compared with plain meaning search.
  if (mode !== "cosine") {
    const cosRank = new Map(rankChunks("cosine").map((r, k) => [r.i, k]));
    let best = null;
    ranked.forEach((r, k) => {
      const delta = cosRank.get(r.i) - k;
      if (delta !== 0 && (!best || Math.abs(delta) > Math.abs(best.delta))) {
        best = { i: r.i, from: cosRank.get(r.i), to: k, delta };
      }
    });
    card.querySelector(".rank-note").innerHTML = best
      ? `🔀 Compared with <strong>Meaning</strong> mode, ${chunkTag(best.i)} moved from #${best.from + 1} to #${best.to + 1}.`
      : `Same order as <strong>Meaning</strong> mode, just different scores.`;
  }

  container.replaceChildren(card, renderScoreBreakdown(ranked));
}

function renderScoreBreakdown(ranked) {
  const i = state.simSelected;
  const r = ranked.find((x) => x.i === i);
  const chunk = state.chunks[i];
  const mode = state.retrievalMode;
  const card = html(`
    <div class="card breakdown">
      <div class="card-title">🧮 How was ${chunkTag(i)}'s score calculated?</div>
    </div>`);

  if (mode === "cosine" || mode === "hybrid") {
    const q = state.queryEmbed.vec;
    const prod = Float32Array.from(q, (v, j) => v * chunk.vec[j]);
    const scale = heatmapScale([q, chunk.vec]);
    const sec = html(`
      <div class="bd-section">
        <h4>${mode === "hybrid" ? "Part 1 · " : ""}Meaning score (cosine similarity)</h4>
        <p class="muted small">Multiply the two lists number by number, then add up all 384 results.
        (Both lists were scaled to length 1, so this simple sum <em>is</em> the cosine similarity.)</p>
        <div class="bd-strips">
          <span class="bd-label">Your question</span><span class="s1"></span>
          <span class="bd-label">× Chunk ${i + 1}</span><span class="s2"></span>
          <span class="bd-label">= products</span><span class="s3"></span>
        </div>
        <div class="bd-top"></div>
        <div class="bd-total">Add up all 384 products → <b>${r.cos.toFixed(3)}</b></div>
      </div>`);
    sec.querySelector(".s1").appendChild(heatmapStrip(q, scale));
    sec.querySelector(".s2").appendChild(heatmapStrip(chunk.vec, scale));
    sec.querySelector(".s3").appendChild(heatmapStrip(prod));
    const topDims = Array.from(prod, (v, j) => [v, j]).sort((a, b) => b[0] - a[0]).slice(0, 4);
    sec.querySelector(".bd-top").innerHTML = `<span class="muted small">Biggest contributions:</span> ` +
      topDims.map(([v, j]) => `<span class="mono-chip">#${j + 1}: ${q[j].toFixed(3)} × ${chunk.vec[j].toFixed(3)} = <b>${v.toFixed(4)}</b></span>`).join(" ");
    card.appendChild(sec);
  }

  if (mode === "bm25" || mode === "hybrid") {
    const bd = bm25Breakdown(chunk);
    const rows = bd.rows.map((x) => `
      <tr class="${x.tf ? "" : "zero"}">
        <td><span class="kw-chip">${escapeHtml(x.term)}</span></td>
        <td>${x.tf}</td>
        <td>${x.idf.toFixed(2)}</td>
        <td><b>${x.score.toFixed(3)}</b></td>
      </tr>`).join("");
    card.appendChild(html(`
      <div class="bd-section">
        <h4>${mode === "hybrid" ? "Part 2 · " : ""}Keyword score (BM25)</h4>
        <p class="muted small">For each keyword: how many times it appears in this chunk, combined with how <em>rare</em>
        it is across all chunks (rare words are better clues). Repeating a word helps less and less, and long chunks
        are scored down a little so they don't win just by being long.</p>
        <table class="bd-table">
          <thead><tr><th>Keyword</th><th>Times in chunk</th><th>Rarity (IDF)</th><th>Points</th></tr></thead>
          <tbody>${rows || `<tr><td colspan="4" class="muted">No keywords in the question.</td></tr>`}</tbody>
        </table>
        <div class="bd-total">Add up the points → <b>${r.bm25.toFixed(3)}</b>
          ${r.bm25 > 1 ? `<span class="muted small">(yes, above 1: BM25 scores aren't capped)</span>` : ""}</div>
        <details class="bd-formula"><summary>Show the exact formula</summary>
          <p>points = IDF × tf × (k1 + 1) ÷ (tf + k1 × L)</p>
          <p>L = 1 − b + b × (chunk length ÷ average length) = 1 − ${BM25_B} + ${BM25_B} × (${chunk.bm25Len} ÷ ${state.bm25AvgLen.toFixed(1)}) = <b>${bd.lenNorm.toFixed(3)}</b></p>
          <p>k1 = ${BM25_K1}, b = ${BM25_B}. IDF = ln(1 + (N − n + 0.5) ÷ (n + 0.5)), where N = ${state.bm25N} chunks and
          n = how many chunks contain the word. Lengths count keywords only.</p>
        </details>
      </div>`));
  }

  if (mode === "hybrid") {
    const w = state.hybridWeight;
    card.appendChild(html(`
      <div class="bd-section">
        <h4>Part 3 · Blend them</h4>
        <div class="bd-math">
          <div><span class="muted">Put BM25 on a 0–1 scale:</span> ${r.bm25.toFixed(3)} ÷ ${ranked.maxBm25.toFixed(3)}
            <span class="muted">(top BM25 score)</span> = <b>${r.bm25Norm.toFixed(3)}</b></div>
          <div><span class="muted">Blend:</span> ${w.toFixed(2)} × ${r.cos.toFixed(3)} + ${(1 - w).toFixed(2)} × ${r.bm25Norm.toFixed(3)}
            = <b class="big-num">${r.score.toFixed(3)}</b></div>
        </div>
      </div>`));
  }
  return card;
}

// ============================================================
// Step 7: Top-k retrieval
// ============================================================

function renderTopkStage() {
  state.topkVisited = true;
  const n = state.chunks.length;
  const maxK = Math.min(6, n);
  state.topK = Math.max(1, Math.min(state.topK, maxK));

  const root = html(`<div class="stage-content"></div>`);
  root.appendChild(explainer("Keep only the best few", `
    <p>We don't hand the AI every chunk, only the top <strong>k</strong>. Too few, and we might miss the answer.
    Too many, and we bury it in noise (and pay for more text). Drag the slider and watch the cutoff move.</p>`));

  const ctl = html(`
    <div class="card controls">
      <div class="control">
        <div class="control-top"><label for="topk">k = how many chunks to keep</label><output></output></div>
        <input type="range" id="topk" min="1" max="${maxK}" step="1">
        <div class="control-hint">Ranking by <b>${MODE_LABEL[state.retrievalMode]}</b> · <a href="#/similarity">change it in Step 6</a></div>
      </div>
    </div>`);
  const slider = ctl.querySelector("input");
  const out = ctl.querySelector("output");
  slider.value = state.topK;
  const results = html(`<div class="two-col"></div>`);
  const sync = () => {
    out.textContent = `${state.topK} of ${n}`;
    renderTopkResults(results);
  };
  slider.addEventListener("input", () => { state.topK = +slider.value; detachChatView(); sync(); });
  root.append(ctl, results);
  sync();
  return root;
}

function renderTopkResults(container) {
  heatmapRegistry.clear();
  const ranked = rankChunks();
  const k = state.topK;
  const list = html(`
    <div class="card">
      <div class="card-title">Ranked chunks</div>
      <div class="topk-list"></div>
    </div>`);
  const box = list.querySelector(".topk-list");
  ranked.forEach((r, rank) => {
    if (rank === k) box.appendChild(html(`<div class="cutoff"><span>✂ cutoff: everything below is left out</span></div>`));
    const kept = rank < k;
    const col = chunkColor(r.i);
    box.appendChild(html(`
      <div class="topk-row${kept ? " kept" : " dropped"}" style="--c-solid:${col.solid};--c-bg:${col.bg}">
        <span class="score-rank">${rankBadge(rank)}</span>
        <span class="score-label"><span class="dot"></span>Chunk ${r.i + 1}</span>
        <span class="topk-text">${escapeHtml(snippet(state.chunks[r.i].text, 70))}</span>
        <span class="topk-status">${kept ? "✓ kept" : "left out"}</span>
      </div>`));
  });

  const keep = new Set(ranked.slice(0, k).map((r) => r.i));
  const faded = new Set(ranked.slice(k).map((r) => r.i));
  const map = html(`
    <div class="card">
      <div class="card-title">🗺️ What gets retrieved</div>
      <div class="scatter-wrap"></div>
      <p class="caption muted small">Lines connect your question (★) to the ${k} chunk${k > 1 ? "s" : ""} we keep.</p>
    </div>`);
  map.querySelector(".scatter-wrap").appendChild(
    renderScatter(null, { queryVec: state.queryEmbed.vec, links: keep, faded, selected: null }));
  container.replaceChildren(list, map);
}

// ============================================================
// Step 8: Inspect chunks
// ============================================================

function renderInspectStage() {
  state.inspectVisited = true;
  const doc = activeDocument();
  const hits = retrieved();
  const terms = queryTerms();
  const root = html(`<div class="stage-content"></div>`);
  root.appendChild(explainer("Check what we found", `
    <p>Before the AI sees anything, let's look at what we're about to hand it. These ${hits.length} chunk${hits.length > 1 ? "s are" : " is"}
    <strong>all the AI will know</strong> about your document. Is the answer actually in here?</p>`));

  const posCard = html(`
    <div class="card">
      <div class="card-title">Where the retrieved chunks come from in your document</div>
      <div class="doc-bar"></div>
      <div class="doc-bar-ends"><span>start of document</span><span>end</span></div>
    </div>`);
  const bar = posCard.querySelector(".doc-bar");
  hits.forEach((r, rank) => {
    const c = state.chunks[r.i];
    const left = (c.start / doc.text.length) * 100;
    const width = Math.max(2, ((c.end - c.start) / doc.text.length) * 100);
    bar.appendChild(html(`<span class="doc-bar-seg" title="Chunk ${r.i + 1}"
      style="left:${left}%;width:${width}%;background:${chunkColor(r.i).solid}">${rankBadge(rank)}</span>`));
  });
  root.appendChild(posCard);

  hits.forEach((r, rank) => {
    const c = state.chunks[r.i];
    const col = chunkColor(r.i);
    const found = terms.filter((t) => c.bm25Tf?.has(t));
    let foot = terms.length
      ? `Contains <b>${found.length} of ${terms.length}</b> question keywords${found.length ? `: ${found.map((t) => `<mark class="kw">${escapeHtml(t)}</mark>`).join(" ")}` : ""}`
      : "No keywords in the question to look for.";
    if (!found.length && state.retrievalMode !== "bm25") foot += ` <span class="muted">· found by meaning, not by matching words</span>`;
    root.appendChild(html(`
      <div class="card inspect-card${state.inspectFocus === r.i ? " focused" : ""}" style="--c-solid:${col.solid};--c-bg:${col.bg}">
        <div class="inspect-head">
          <span class="inspect-medal">${rankBadge(rank)}</span>
          ${chunkTag(r.i)}
          <span class="muted small">score ${r.score.toFixed(3)} · ${MODE_LABEL[state.retrievalMode]}</span>
        </div>
        <div class="inspect-text">${highlightKeywords(c.text, terms)}</div>
        <div class="inspect-foot small">${foot}</div>
      </div>`));
  });

  root.appendChild(html(`
    <div class="callout tip-callout">
      <div class="callout-icon">🔧</div>
      <div>If the right chunk isn't here, the AI can't answer well, no matter how smart it is.
      Try a different method in <a href="#/similarity">Step 6</a> or a bigger k in <a href="#/topk">Step 7</a>.</div>
    </div>`));
  return root;
}

// ============================================================
// Step 9: LLM generation
// ============================================================

const RAG_SYSTEM_PROMPT =
  "You are a helpful assistant answering a beginner's question. Answer ONLY using the context below. " +
  "If the answer isn't in the context, say \"I don't know based on this document.\" " +
  "Keep the answer short and simple, and mention which chunk(s) you used, like [Chunk 2].";
const NO_RAG_SYSTEM_PROMPT = "You are a helpful assistant. Keep the answer short and simple.";

function buildRagMessages() {
  const context = retrieved().map((r) => `[Chunk ${r.i + 1}]\n${state.chunks[r.i].text}`).join("\n\n");
  return [
    { role: "system", content: RAG_SYSTEM_PROMPT },
    { role: "user", content: `Context:\n${context}\n\nQuestion: ${state.query}` },
  ];
}

async function runGeneration(which) {
  const slot = which === "rag" ? "gen" : "noRag";
  if (state[slot].status === "busy") return;
  state[slot] = { ...freshGen(), status: "busy" };
  renderAll();
  const messages = which === "rag"
    ? buildRagMessages()
    : [{ role: "system", content: NO_RAG_SYSTEM_PROMPT }, { role: "user", content: state.query }];
  try {
    // gpt-oss is a reasoning model: hidden reasoning tokens count against max_tokens.
    const { text, truncated } = await callGroq(messages, { model: GROQ_MODEL, maxTokens: 4096 });
    state[slot] = { status: "done", answer: text || "(The model returned an empty answer.)", truncated, error: "" };
  } catch (err) {
    state[slot] = { ...freshGen(), status: "error", error: err.message };
  }
  renderAll();
}

function renderAnswerCard(slot, title, cls) {
  const g = state[slot];
  const card = html(`<div class="card answer-card ${cls}"><div class="card-title">${title}</div><div class="answer-body"></div></div>`);
  const body = card.querySelector(".answer-body");
  if (g.status === "busy") {
    body.innerHTML = `<span class="typing"><span></span><span></span><span></span></span> <span class="muted small">Thinking…</span>`;
  } else if (g.status === "error") {
    body.innerHTML = `<div class="action-error">⚠️ ${escapeHtml(g.error)}</div>`;
  } else if (g.answer) {
    body.classList.add("answer-md");
    body.innerHTML = renderMarkdown(g.answer);
    if (g.truncated) body.appendChild(html(`<div class="msg-note">(The answer was cut off because it hit the length limit.)</div>`));
  }
  return card;
}

function renderGenerateStage() {
  const hits = retrieved();
  const root = html(`<div class="stage-content"></div>`);
  root.appendChild(explainer("Hand it to the AI", `
    <p>Finally, we paste the retrieved chunks into a <strong>prompt</strong> together with your question, and send it
    to a large language model (LLM). The AI only "knows" what's in this prompt. That's the whole trick of RAG:
    <em>find</em> the right text first, then let the AI <em>write</em> the answer from it.</p>`));

  const messages = buildRagMessages();
  const chars = messages.reduce((a, m) => a + m.content.length, 0);
  const prompt = html(`
    <div class="card">
      <div class="card-title">📨 The prompt we'll send <span class="muted">· about ${Math.round(chars / 4)} tokens</span></div>
      <div class="prompt">
        <div class="prompt-sec instr">
          <div class="prompt-tag">① Instructions</div>
          <div class="prompt-text"></div>
        </div>
        <div class="prompt-sec ctx">
          <div class="prompt-tag">② Context: the ${hits.length} retrieved chunk${hits.length > 1 ? "s" : ""}</div>
          <div class="prompt-chunks"></div>
        </div>
        <div class="prompt-sec qst">
          <div class="prompt-tag">③ Your question</div>
          <div class="prompt-text"></div>
        </div>
      </div>
    </div>`);
  prompt.querySelector(".instr .prompt-text").textContent = RAG_SYSTEM_PROMPT;
  prompt.querySelector(".qst .prompt-text").textContent = state.query;
  const chunksBox = prompt.querySelector(".prompt-chunks");
  hits.forEach((r) => {
    const col = chunkColor(r.i);
    const el = html(`<div class="prompt-chunk" style="--c-solid:${col.solid};--c-bg:${col.bg}"><b>[Chunk ${r.i + 1}]</b> <span></span></div>`);
    el.querySelector("span").textContent = state.chunks[r.i].text;
    chunksBox.appendChild(el);
  });
  root.appendChild(prompt);

  const actions = html(`
    <div class="card action-card">
      <div class="gen-buttons">
        <button type="button" class="btn primary big send">🚀 Send to the LLM</button>
        <button type="button" class="btn secondary big compare">🤔 Compare: ask without RAG</button>
      </div>
      <div class="muted small">Model: <code>${GROQ_MODEL}</code> on Groq</div>
    </div>`);
  const send = actions.querySelector(".send");
  send.disabled = state.gen.status === "busy";
  if (state.gen.answer) send.textContent = "🔁 Send again";
  send.addEventListener("click", () => runGeneration("rag"));
  const cmp = actions.querySelector(".compare");
  cmp.disabled = state.noRag.status === "busy";
  cmp.addEventListener("click", () => runGeneration("norag"));
  root.appendChild(actions);

  if (state.gen.status !== "idle" || state.noRag.status !== "idle") {
    const grid = html(`<div class="two-col answers"></div>`);
    if (state.gen.status !== "idle") grid.appendChild(renderAnswerCard("gen", "✅ Answer with RAG (using your document)", "rag"));
    if (state.noRag.status !== "idle") grid.appendChild(renderAnswerCard("noRag", "🤷 Answer without RAG (no document)", "norag"));
    root.appendChild(grid);
    if (state.gen.answer && state.noRag.answer) {
      root.appendChild(html(`<p class="caption">Compare the two. For famous facts (like where the Pope lives), the AI may
        already know the answer from its training. RAG really shines with text the AI has <strong>never seen</strong>: your
        own notes, company documents, or recent news. There, without the document it can only guess (or confidently make
        something up), while with RAG it answers from <em>your</em> text and points to the chunks it used.
        Try it: upload your own PDF in the left panel!</p>`));
    }
  }

  if (state.gen.answer) {
    const done = html(`
      <div class="finale">
        <div class="finale-icon">🎉</div>
        <h3>You've seen the whole RAG pipeline!</h3>
        <ol class="recap">
          ${STAGES.map((s) => `<li><span>${s.icon}</span> <b>${s.label}</b> <span class="muted">${s.blurb}</span></li>`).join("")}
        </ol>
        <button type="button" class="btn primary">Try another question →</button>
      </div>`);
    done.querySelector("button").addEventListener("click", () => goTo("query"));
    root.appendChild(done);
  }
  return root;
}

// ============================================================
// Chat sidebar: ask a question, run the whole pipeline, then open any step
// ============================================================
//
// Each chat answer keeps a snapshot of everything Steps 4-9 need (question,
// tokens, vector, retrieval settings, answer). Clicking a step on an older
// answer loads *that* snapshot, so the step pages show that answer's results.

const CHAT_STEP_DELAY = 250; // ms per step, so beginners can watch the pipeline move
let chatSeq = 0;
let chatStick = false; // scroll the message list to the bottom on next render

const chatEls = {
  layout: document.querySelector(".layout"),
  toggle: document.getElementById("chat-toggle"),
  messages: document.getElementById("chat-messages"),
  form: document.getElementById("chat-form"),
  input: document.getElementById("chat-input"),
  send: document.getElementById("chat-send"),
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Called by in-place slider updates (no full re-render): drop the banner and badge.
function detachChatView() {
  if (state.activeChatId == null) return;
  state.activeChatId = null;
  document.querySelector(".chat-banner")?.remove();
  renderChat();
}

function takeSnapshot() {
  return {
    chunkKey: state.chunkKey,
    query: state.query,
    queryTokens: state.queryTokens,
    queryEmbed: state.queryEmbed,
    retrievalMode: state.retrievalMode,
    hybridWeight: state.hybridWeight,
    topK: state.topK,
    chunkSize: state.chunkSize,
    overlap: effectiveOverlap(),
    gen: { ...state.gen },
  };
}

function snapshotUsable(msg) {
  return !!msg.snap && msg.snap.chunkKey === state.chunkKey;
}

// Puts an answer's snapshot back into the shared state the step pages read.
function loadSnapshot(msg) {
  if (!snapshotUsable(msg) || state.chatBusy) return false;
  const s = msg.snap;
  Object.assign(state, {
    query: s.query,
    queryTokens: s.queryTokens,
    queryEmbed: s.queryEmbed,
    queryError: "",
    retrievalMode: s.retrievalMode,
    hybridWeight: s.hybridWeight,
    topK: s.topK,
    gen: { ...s.gen },
    noRag: freshGen(),
    simSelected: null,
    similarityVisited: true,
    topkVisited: true,
    inspectVisited: true,
    activeChatId: msg.id,
  });
  return true;
}

function openChatStep(msg, stageId, focusChunk = null) {
  if (!loadSnapshot(msg)) return;
  state.inspectFocus = focusChunk;
  if (currentRoute() === stageId) {
    renderAll();
  } else {
    goTo(stageId); // hashchange -> renderAll
  }
  if (focusChunk != null) {
    setTimeout(() => {
      document.querySelector(".inspect-card.focused")?.scrollIntoView({ behavior: "smooth", block: "center" });
    }, 80);
  }
}

async function runChatPipeline(question, { variant = "" } = {}) {
  const q = question.trim();
  if (!q || state.chatBusy) return;

  const msg = {
    id: ++chatSeq,
    question: q,
    status: "running",
    steps: Object.fromEntries(STAGES.map((s) => [s.id, "pending"])),
    note: "",
    sources: [],
    snap: null,
    answer: null,
    truncated: false,
    error: "",
    variant,           // settings label when this is a "Regenerate" of an earlier answer
    panel: null,       // open follow-up panel: "chunks" | "params" | null
    draft: null,       // Change-parameters draft settings
    expanded: new Set(), // chunk rows opened in the Chunks panel
  };
  state.chatMessages.push(msg);
  chatStick = true;

  if (!activeDocument()) {
    msg.status = "error";
    msg.error = "Pick a document in the left panel first (the sample text, or a PDF).";
    renderChat();
    return;
  }

  state.chatBusy = true;
  renderChat();

  const step = async (id, fn) => {
    msg.steps[id] = "running";
    renderChat();
    await fn();
    await sleep(CHAT_STEP_DELAY);
    msg.steps[id] = "done";
    renderAll(); // overview, step dots and chat all update live
  };

  try {
    // Indexing happens once: if the document is already prepared, Steps 1-3 are reused, not re-run.
    ensureChunks();
    const prepared = isStageComplete("embed") && state.chunkingVisited && state.storeVisited;
    if (prepared) {
      for (const id of ["chunking", "embed", "store"]) msg.steps[id] = "reused";
      renderChat();
    } else {
      await step("chunking", async () => { ensureChunks(); state.chunkingVisited = true; });
      await step("embed", async () => {
        if (isStageComplete("embed")) return;
        msg.note = state.embedderReady
          ? "Embedding your document's chunks…"
          : "Preparing your document: downloading the embedding model (first time only) <span id=\"chat-dl-label\"></span>";
        renderChat();
        await generateEmbeddings();
        msg.note = "";
        if (!isStageComplete("embed")) throw new Error(state.embed.error || "Couldn't embed the document.");
      });
      await step("store", async () => { state.storeVisited = true; });
    }
    await step("query", async () => {
      state.query = q;
      resetQuery();
      state.queryTokens = await tokenizeText(q);
    });
    await step("query-embed", async () => { state.queryEmbed = await embedText(q); });
    await step("similarity", async () => { state.similarityVisited = true; });
    await step("topk", async () => { state.topkVisited = true; });
    await step("inspect", async () => {
      state.inspectVisited = true;
      msg.sources = retrieved().map((r) => r.i);
    });
    msg.snap = takeSnapshot(); // steps 1-8 stay explorable even if the LLM call fails
    await step("generate", async () => {
      const { text, truncated } = await callGroq(buildRagMessages());
      state.gen = { status: "done", answer: text || "(The model returned an empty answer.)", truncated, error: "" };
      msg.answer = state.gen.answer;
      msg.truncated = truncated;
    });
    msg.snap = takeSnapshot();
    msg.status = "done";
  } catch (err) {
    console.error(err);
    msg.status = "error";
    msg.error = err.message || "Something went wrong. Try again.";
    msg.note = "";
    for (const id in msg.steps) if (msg.steps[id] === "running") msg.steps[id] = "failed";
  }
  if (msg.snap) state.activeChatId = msg.id;
  state.chatBusy = false;
  chatStick = true;
  renderAll();
}

const isDoneChip = (st) => st === "done" || st === "reused";

function chipTitle(msg, s, k, st) {
  let name = `Step ${k + 1}: ${s.label}`;
  if (st === "failed") return `${name} (failed)`;
  if (!isDoneChip(st)) return name;
  if (st === "reused") name += " (already prepared earlier and reused: indexing happens once)";
  if (msg.snap && !snapshotUsable(msg)) return `${name}. The document changed since this answer, so ask again to see its steps.`;
  if (state.chatBusy) return `${name}. Wait for the current question to finish.`;
  return `Open ${name} for this question`;
}

function renderChatMessage(msg) {
  const active = state.activeChatId === msg.id;
  const canOpen = snapshotUsable(msg) && !state.chatBusy;
  const turn = html(`
    <div class="chat-turn">
      <div class="chat-q"></div>
      <div class="chat-a${active ? " active" : ""}">
        <div class="chat-a-head">
          <span class="chat-a-label">RAG pipeline</span>
          ${active ? `<span class="chat-active">● Showing in steps</span>` : ""}
        </div>
        <div class="chat-steps"></div>
        <div class="chat-body"></div>
      </div>
    </div>`);
  turn.querySelector(".chat-q").textContent = msg.question;
  if (msg.variant) {
    const v = html(`<div class="chat-variant">🔄 Regenerated with: <b></b></div>`);
    v.querySelector("b").textContent = msg.variant;
    turn.querySelector(".chat-q").before(v);
  }

  const steps = turn.querySelector(".chat-steps");
  STAGES.forEach((s, k) => {
    const st = msg.steps[s.id];
    const chip = html(`<button type="button" class="cstep ${st}"><span class="cstep-icon">${isDoneChip(st) ? "✓" : st === "failed" ? "!" : s.icon}</span><span class="cstep-num">${k + 1}</span></button>`);
    // Instant CSS tooltip with the step name (native title tooltips are slow and skip disabled buttons).
    chip.dataset.tip = s.label;
    chip.setAttribute("aria-label", chipTitle(msg, s, k, st));
    chip.disabled = !isDoneChip(st) || !canOpen;
    chip.addEventListener("click", () => openChatStep(msg, s.id));
    steps.appendChild(chip);
  });

  const body = turn.querySelector(".chat-body");
  if (msg.note) body.appendChild(html(`<div class="chat-note">${msg.note}</div>`));

  if (msg.answer) {
    const ans = html(`<div class="chat-answer answer-md"></div>`);
    ans.innerHTML = renderMarkdown(msg.answer);
    if (msg.truncated) ans.appendChild(html(`<div class="msg-note">(The answer was cut off because it hit the length limit.)</div>`));
    body.appendChild(ans);
  } else if (msg.status === "running") {
    const runningId = STAGES.find((s) => msg.steps[s.id] === "running");
    body.appendChild(html(`<div class="chat-working"><span class="typing"><span></span><span></span><span></span></span>
      <span>${runningId ? (runningId.id === "generate" ? "Writing the answer…" : `${runningId.label}…`) : "Starting…"}</span></div>`));
  }
  if (msg.error) body.appendChild(html(`<div class="action-error">⚠️ ${escapeHtml(msg.error)}</div>`));

  if (msg.sources.length) {
    const src = html(`<div class="chat-sources"><span class="muted small">Sources:</span></div>`);
    msg.sources.forEach((i) => {
      const b = html(`<button type="button" class="chat-src">${chunkTag(i)}</button>`);
      b.disabled = !canOpen;
      b.title = canOpen ? `See Chunk ${i + 1} in Step 8` : "";
      b.addEventListener("click", () => openChatStep(msg, "inspect", i));
      src.appendChild(b);
    });
    body.appendChild(src);
  }

  if (msg.snap) {
    const stale = !snapshotUsable(msg);
    body.appendChild(html(`<div class="chat-meta">${MODE_LABEL[msg.snap.retrievalMode]} · top ${msg.snap.topK}
      · chunks ${msg.snap.chunkSize}/${msg.snap.overlap}
      ${stale ? `<span class="chat-stale">· document changed, ask again to explore its steps</span>` : ""}</div>`));
  }
  if (msg.status === "done" && msg.answer) turn.appendChild(renderChatFollowup(msg));
  return turn;
}

// ---------- Follow-up bubble: "Chunks" and "Change parameters" ----------

const SCORE_EXPLAIN = {
  cosine: () => "Scores are meaning similarity (cosine): about 0 = unrelated, 1 = same meaning.",
  bm25: () => "Scores are keyword matches (BM25). They aren't capped at 1.",
  hybrid: (w) => `Scores = ${w.toFixed(2)} × meaning + ${(1 - w).toFixed(2)} × keywords (BM25 scaled to 0–1).`,
};

function renderChatFollowup(msg) {
  const usable = snapshotUsable(msg);
  const box = html(`
    <div class="chat-follow">
      <div class="chat-follow-note">💡 Curious how settings change this answer? See every chunk ranked for this
        question, or tweak the settings and regenerate.</div>
      <div class="chat-follow-btns">
        <button type="button" class="fbtn${msg.panel === "chunks" ? " on" : ""}" data-p="chunks">🧩 Chunks</button>
        <button type="button" class="fbtn${msg.panel === "params" ? " on" : ""}" data-p="params">⚙️ Change parameters</button>
      </div>
    </div>`);
  box.querySelectorAll(".fbtn").forEach((b) => {
    b.disabled = !usable;
    b.addEventListener("click", () => {
      const p = b.dataset.p;
      msg.panel = msg.panel === p ? null : p;
      if (p === "params" && !msg.draft) {
        const s = msg.snap;
        msg.draft = { mode: s.retrievalMode, weight: s.hybridWeight, topK: s.topK, chunkSize: s.chunkSize, overlap: s.overlap };
      }
      renderChat();
    });
  });
  if (!usable) {
    box.appendChild(html(`<div class="chat-stale small">The document changed since this answer, so these options are off. Ask again to explore.</div>`));
  } else if (msg.panel === "chunks") {
    box.appendChild(renderChunksPanel(msg));
  } else if (msg.panel === "params") {
    box.appendChild(renderParamsPanel(msg));
  }
  return box;
}

function renderChunksPanel(msg) {
  const s = msg.snap;
  const ranked = rankChunks(s.retrievalMode, { queryEmbed: s.queryEmbed, query: s.query, hybridWeight: s.hybridWeight });
  const k = Math.min(s.topK, ranked.length);
  const top = Math.max(...ranked.map((r) => r.score), 1e-9);
  const panel = html(`
    <div class="chat-panel">
      <div class="chat-panel-title">All ${ranked.length} chunks, most → least related</div>
      <div class="crows"></div>
      <div class="chat-panel-foot">${SCORE_EXPLAIN[s.retrievalMode](s.hybridWeight)} Only the top ${k} were sent to the LLM.
        Click a chunk to read it.</div>
    </div>`);
  const list = panel.querySelector(".crows");
  ranked.forEach((r, rank) => {
    if (rank === k && k < ranked.length) list.appendChild(html(`<div class="crow-cut">not sent to the LLM ↓</div>`));
    const used = rank < k;
    const open = msg.expanded.has(r.i);
    const col = chunkColor(r.i);
    const width = s.retrievalMode === "bm25" ? (r.score / top) * 100 : Math.max(0, r.score) * 100;
    const row = html(`
      <button type="button" class="crow${used ? " used" : ""}${open ? " open" : ""}" style="--c-solid:${col.solid};--c-bg:${col.bg}">
        <span class="crow-head">
          <span class="crow-rank">${rankBadge(rank)}</span>
          ${chunkTag(r.i)}
          ${used ? `<span class="crow-used">✓ sent to the LLM</span>` : ""}
          <span class="crow-score">${r.score.toFixed(3)}</span>
        </span>
        <span class="crow-bar"><span style="width:${Math.max(0, width).toFixed(1)}%"></span></span>
        <span class="crow-text"></span>
      </button>`);
    const text = state.chunks[r.i].text;
    row.querySelector(".crow-text").textContent = open ? text : snippet(text, 70);
    row.addEventListener("click", () => {
      if (msg.expanded.has(r.i)) msg.expanded.delete(r.i);
      else msg.expanded.add(r.i);
      const nowOpen = msg.expanded.has(r.i);
      row.classList.toggle("open", nowOpen);
      row.querySelector(".crow-text").textContent = nowOpen ? text : snippet(text, 70);
    });
    list.appendChild(row);
  });
  return panel;
}

const PARAM_MODE_HINT = {
  cosine: "Finds chunks with similar meaning, even with different words.",
  bm25: "Finds chunks that share your exact keywords.",
  hybrid: "Blends meaning and keywords.",
};

function renderParamsPanel(msg) {
  const d = msg.draft;
  const maxK = Math.max(1, Math.min(6, state.chunks.length));
  d.topK = Math.min(d.topK, maxK);
  const panel = html(`
    <div class="chat-panel pp">
      <div class="pp-group">
        <div class="pp-label">How to find chunks</div>
        <div class="pp-seg"></div>
        <div class="pp-hint">${PARAM_MODE_HINT[d.mode]}</div>
        ${d.mode === "hybrid" ? `
          <div class="pp-row"><span>Blend</span><output class="o-w"></output></div>
          <input type="range" class="s-w" min="0" max="1" step="0.05">
          <div class="slider-ends"><span>keywords</span><span>meaning</span></div>` : ""}
      </div>
      <div class="pp-group">
        <div class="pp-row"><span class="pp-label">Chunks sent to the LLM (k)</span><output class="o-k"></output></div>
        <input type="range" class="s-k" min="1" max="${maxK}" step="1">
        <div class="pp-hint">More chunks = more context, but also more noise.</div>
      </div>
      <div class="pp-group">
        <div class="pp-row"><span class="pp-label">Chunk size</span><output class="o-size"></output></div>
        <input type="range" class="s-size" min="100" max="600" step="10">
        <div class="pp-row"><span class="pp-label">Overlap</span><output class="o-ov"></output></div>
        <input type="range" class="s-ov" min="0" max="120" step="5">
        <div class="pp-hint warn">⚠️ Changing these re-prepares the whole document (Steps 1–3 run again), and earlier
          answers can't be reopened afterwards.</div>
      </div>
      <button type="button" class="btn primary pp-go">🔄 Regenerate answer</button>
    </div>`);

  const seg = panel.querySelector(".pp-seg");
  ["cosine", "bm25", "hybrid"].forEach((m) => {
    const b = html(`<button type="button" class="seg-btn${d.mode === m ? " on" : ""}">${m === "cosine" ? "Meaning" : m === "bm25" ? "Keywords" : "Hybrid"}</button>`);
    b.addEventListener("click", () => { d.mode = m; renderChat(); });
    seg.appendChild(b);
  });

  // Sliders update their labels in place (re-rendering would break the drag).
  const bind = (cls, out, key, fmt) => {
    const input = panel.querySelector(cls);
    if (!input) return;
    const o = panel.querySelector(out);
    input.value = d[key];
    o.textContent = fmt(d[key]);
    input.addEventListener("input", () => {
      d[key] = +input.value;
      o.textContent = fmt(d[key]);
      panel.querySelector(".o-ov").textContent = fmtOverlap(d.overlap);
    });
  };
  const fmtOverlap = (v) => {
    const eff = Math.min(v, Math.floor(d.chunkSize / 2));
    return eff < v ? `${eff} chars (capped)` : `${eff} chars`;
  };
  bind(".s-w", ".o-w", "weight", (v) => `${Math.round(v * 100)}% meaning`);
  bind(".s-k", ".o-k", "topK", (v) => `${v}`);
  bind(".s-size", ".o-size", "chunkSize", (v) => `${v} chars`);
  bind(".s-ov", ".o-ov", "overlap", fmtOverlap);

  const go = panel.querySelector(".pp-go");
  go.disabled = state.chatBusy;
  if (state.chatBusy) go.title = "Wait for the current question to finish";
  go.addEventListener("click", () => {
    if (state.chatBusy) return;
    state.retrievalMode = d.mode;
    state.hybridWeight = d.weight;
    state.topK = d.topK;
    state.chunkSize = d.chunkSize;
    state.chunkOverlap = d.overlap;
    msg.panel = null;
    const label = `${MODE_LABEL[d.mode]}${d.mode === "hybrid" ? ` (${Math.round(d.weight * 100)}% meaning)` : ""}` +
      ` · top ${d.topK} · chunks ${d.chunkSize}/${effectiveOverlap()}`;
    runChatPipeline(msg.question, { variant: label });
  });
  return panel;
}

function renderChat() {
  const box = chatEls.messages;
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  const prevTop = box.scrollTop;

  if (!state.chatMessages.length) {
    const empty = html(`
      <div class="chat-empty">
        <div class="chat-empty-icon">💬</div>
        <h3>Ask your document</h3>
        <p>Ask anything about your document. You'll watch all 9 RAG steps run, then you can open any step to see
        how the answer was found.</p>
        <div class="chat-suggest"></div>
      </div>`);
    const qs = state.docSource === "sample" ? SAMPLE_QUESTIONS : GENERIC_QUESTIONS;
    qs.forEach((q) => {
      const chip = html(`<button type="button" class="chip"></button>`);
      chip.textContent = q;
      chip.addEventListener("click", () => runChatPipeline(q));
      empty.querySelector(".chat-suggest").appendChild(chip);
    });
    box.replaceChildren(empty);
  } else {
    box.replaceChildren(...state.chatMessages.map(renderChatMessage));
  }

  box.scrollTop = chatStick || (state.chatBusy && nearBottom) ? box.scrollHeight : prevTop;
  chatStick = false;
  chatEls.send.disabled = state.chatBusy;
  chatEls.send.textContent = state.chatBusy ? "…" : "Send";
}

function setChatCollapsed(collapsed) {
  chatEls.layout.classList.toggle("chat-collapsed", collapsed);
  const label = collapsed ? "Show chat" : "Hide chat";
  chatEls.toggle.title = label;
  chatEls.toggle.setAttribute("aria-label", label);
}

function initChat() {
  chatEls.toggle.addEventListener("click", () => {
    setChatCollapsed(!chatEls.layout.classList.contains("chat-collapsed"));
  });
  if (window.matchMedia("(max-width: 800px)").matches) setChatCollapsed(true);

  const send = () => {
    const q = chatEls.input.value;
    if (!q.trim() || state.chatBusy) return;
    chatEls.input.value = "";
    runChatPipeline(q);
  };
  chatEls.form.addEventListener("submit", (e) => { e.preventDefault(); send(); });
  chatEls.input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
  renderChat();
}

function initPipeline() {
  window.addEventListener("hashchange", () => {
    renderAll();
    document.querySelector(".main-col").scrollTop = 0;
  });
  document.addEventListener("mousemove", onPipelineMouseMove);
  renderAll();
}

initDocSidebar();
initChat();
initPipeline();
