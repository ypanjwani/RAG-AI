# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-page, client-side interactive explainer that walks complete beginners (learners, students) through the full RAG (Retrieval-Augmented Generation) pipeline step by step: document chunking → chunk embeddings → vector store → query input → query embedding → similarity search → top-k retrieval → chunk inspection → LLM generation. It accompanies a blog post, so every step pairs a plain-language explainer with something interactive and a visual.

No app server, no build step, no package manager (the only server code is the small Groq proxy Worker in `worker/`, below). Just `index.html` + `style.css` + `app.js`, plus CDN libraries: pdf.js (3.11.174, classic build) for reading uploaded PDFs, marked + DOMPurify for rendering LLM answers (all loaded via `<script>` tags in `index.html`), and Transformers.js (dynamically `import()`ed from jsDelivr in `app.js`) for running a real embedding model in the browser. The final generation step calls the real Groq API directly from the browser (see "Generation step calls a real LLM" below).

## Running it

There is no build/test/lint tooling. Serve the directory over HTTP (opening `index.html` via `file://` does not work reliably with the pdf.js worker and the dynamic import):

```
python -m http.server 8743
```

then open `http://localhost:8743/index.html`.

The only sanity check available is a syntax check on the single script file:

```
node --check app.js
```

## Layout

Three columns (`.layout` in `index.html`):

- **Left: document sidebar** (`#doc-sidebar`) — "Pick a document": use the built-in sample text or upload a PDF, plus a status line and a document card (name, counts, snippet) whose **View** button opens the full text in a centered modal (`openDocModal()`/`closeDocModal()`; closes via ✕, Escape, or backdrop click). It collapses to a slim rail via the panel-toggle icon (`setDocCollapsed()`), and starts collapsed on narrow screens (≤800px, where it stacks above the content).
- **Main: the pipeline** (`#app`) — filled entirely by the router (below).
- **Right: chat sidebar** (`#chat-sidebar`) — "Ask your document" (see "Chat sidebar" below). Collapses like the left one (`setChatCollapsed()`); stacks below the content on narrow screens.

### Chat sidebar (runs the whole pipeline per question)

`runChatPipeline(question)` runs the real Steps 1→9 for a chat question using the same functions as the step pages: it ensures chunks + embeddings exist (calling `generateEmbeddings()` on the first run, with model-download progress shown in the chat), tokenizes and embeds the question, ranks/retrieves with the user's *current* Step 6/7 settings, and calls `callGroq(buildRagMessages())`. Each step's chip goes pending → running → done (with a short `CHAT_STEP_DELAY` so beginners can watch), and `renderAll()` after each step marks it done in the overview/step dots. Each chat question is answered independently (no chat history is sent to the LLM). While a question runs (`state.chatBusy`), sending and step-opening are disabled.

**Per-answer snapshots ("Option B"):** each message in `state.chatMessages` stores a `snap` (`takeSnapshot()`: `chunkKey`, question, tokens, query vector, retrieval mode/weight/k, chunk size/overlap, answer). Clicking a done step chip (or a "Sources" chunk chip, which also highlights that chunk in Step 8 via `state.inspectFocus`) calls `loadSnapshot()`, which copies that answer's snapshot back into the shared state, so the step pages show *that* answer even if newer questions were asked since. `state.activeChatId` marks which answer the pages show (badge in the chat, banner on Steps 4–9); manual changes (asking in Step 4, changing mode/weight/k, re-chunking) clear it via `resetQueryResults()`/`detachChatView()`. A snapshot is **stale** once `state.chunkKey` changes (document or chunk settings changed): its chips are disabled with a "document changed, ask again" note. `renderAll()` also calls `renderChat()` so these states stay in sync.

**Indexing is reused, not re-run:** if the document is already prepared (embeddings complete and Steps 1/3 visited), a chat question marks Steps 1–3 as `"reused"` (faded ✓ chips, still clickable) and only animates Steps 4–9 — mirroring that real RAG indexes once. They run visibly only on the first question or after the document/chunk settings change.

**Follow-up bubble after each answer** (`renderChatFollowup()`): a short note plus two buttons (`msg.panel` toggles between them):
- **🧩 Chunks** (`renderChunksPanel()`) — every chunk ranked with that answer's snapshot settings (`rankChunks(mode, ctx)` with `ctx = {queryEmbed, query, hybridWeight}` from the snapshot, so it never touches shared state), the top-k badged "✓ sent to the LLM" above a "not sent" divider; rows expand to full text.
- **⚙️ Change parameters** (`renderParamsPanel()`) — a draft (`msg.draft`) of retrieval mode, hybrid weight, k, chunk size and overlap (sliders update labels in place). **Regenerate** writes the draft into the shared settings and calls `runChatPipeline(question, {variant})`, appending a *new* turn labeled "🔄 Regenerated with: …" so old and new answers can be compared. Changing chunk size/overlap re-chunks, so Steps 1–3 run again and older answers go stale.

## Architecture

Everything lives in `app.js` as one big script (no modules, no framework). The mental model:

- **`state`** — a single global object holding the entire app's state (document, chunks + their vectors, BM25 index, query, retrieval settings, generation results, per-step "visited" flags, etc.).
- **`STAGES`** — an ordered array of `{id, label, phase, icon, blurb}` defining the pipeline's fixed order: `chunking`, `embed`, `store` (phase "index") then `query`, `query-embed`, `similarity`, `topk`, `inspect`, `generate` (phase "query"). The order matters: document processing comes *before* the query pipeline, mirroring how real RAG systems index a corpus before any query arrives. Query tokenization/embedding reuse the model loaded during chunk embedding, and BM25's index is built there too, so they must stay after it. `PHASES` groups them for the overview.
- **`STAGE_RENDERERS`** — maps each stage id to a `render<Stage>Stage()` function that returns a DOM node for that page, wiring up its own event listeners internally. Pages are built with the small `html()` template helper; always `escapeHtml()` document/user text that goes into HTML strings.
- **Hash routing** — `#/` (or no hash) shows the overview (`renderFlowchart()`: hero intro, then one card per stage grouped into the two phases, joined by arrows, each marked done/ready/locked). `#/<stage id>` shows that stage's page (`renderStagePage()`): back link, 1–9 progress dots, a gradient header, the renderer's content, and Prev/Next buttons. `hashchange` triggers `renderAll()`; use `goTo(id)` to navigate from code. It's one page load, so the global `state` carries across steps.
- **`renderAll()`** — the only full render entrypoint. It calls `ensureChunks()`, rebuilds the *current route's* view from scratch (no diffing/patching), and re-renders the chat message list. **Exception: sliders** (chunk size/overlap, hybrid weight, top-k) update only their results container in place (`renderChunkResults`, `renderSimResults`, `renderTopkResults`), because a full re-render would replace the slider mid-drag.
- **Locking** — `isStageUnlocked()` is a chain: a stage is open only if *every* earlier stage is unlocked and complete (step 1 just needs a document). `stageStatus()` checks locked before done. A locked page shows a notice linking to the first open step; its real renderer is never called, so renderers can assume their prerequisites exist. Next is enabled only once the current stage is complete.
- **Completion rules** (`isStageComplete()`): chunking = visited + chunks exist; embed = every chunk has a vector; store/similarity/topk/inspect = visited (they display/compute from existing data); query = question tokenized; query-embed = query vector exists; generate = an answer was received.
- **Invalidation** — `ensureChunks()` re-chunks only when the document text or chunk settings change (`state.chunkKey`); doing so drops all vectors, the BM25 index, and query-side results (`resetQueryResults()`), but keeps the question text. Asking a new question calls `resetQuery()`, which clears its tokens/vector and everything downstream. Document changes in the sidebar just call `renderAll()`.

### Document ingestion

`activeDocument()` returns `{name, text}` for either the built-in `SAMPLE_DOCUMENT` (four short sections on clearly distinct topics — Rome/Vatican, bees/honey, volcanoes, why the sky is blue — so retrieval has something to tell apart) or the uploaded PDF's text, or `null` if "Upload a PDF" is chosen without a usable file. PDFs of any length are accepted but **only page 1 is used** (`extractPdfText()` reads `getPage(1)` only); the sidebar says so explicitly. Non-PDFs, scanned/image-only pages (no text) and unreadable files get beginner-readable errors. Word/.docx is not supported.

### Chunking and highlighting

`chunkDocument()` does fixed-size character chunking with overlap, snapped to word boundaries (a chunk ends at the last whitespace before the size limit and the overlap starts at a word start), so no chunk or BM25 keyword holds a half-word. Defaults: size 250, overlap 40; `effectiveOverlap()` caps overlap at half the chunk size. `buildHighlightedHtml()` colors the source text by which chunk "owns" each character range (using `CHUNK_PALETTE`, cycled by chunk index) — in overlapping regions it shows the later chunk's color with a dashed underline in the earlier chunk's color, which is a known simplification. Hovering a chunk card highlights its text and vice versa.

### Embeddings: a real sentence-embedding model (all-MiniLM-L6-v2)

Chunk and query vectors come from `Xenova/all-MiniLM-L6-v2` (`EMBED_MODEL`), a production sentence-transformers model (6-layer transformer, WordPiece tokenizer, 384-dim output), run client-side by Transformers.js (`TRANSFORMERS_URL`, pinned to 3.8.1). `loadEmbedder()` lazily imports the library and builds a `feature-extraction` pipeline once (`dtype: "q8"`; the ~23 MB weights are browser-cached after the first download); `onModelProgress()` drives Step 2's download progress bar in place.

`embedText(text)` runs the model once with `pooling: "none"` and returns `{tokens, tokenVecs, vec}`: the real WordPiece tokens (with `[CLS]`/`[SEP]` and IDs), each token's contextual 384-dim vector (last hidden layer), and the pooled vector — `meanPoolNormalize()` (mean over tokens, then L2-normalize), which matches the library's own `pooling: "mean", normalize: true` output to ~1e-8 (verified). The pooled `vec` is the only thing used for retrieval; token vectors exist for the "tokens → one vector per token → pooled vector" visual (`renderTokenFlow()`, shared by Step 2 and Step 5). Its middle part is `renderEmbTable()`: a token × dimension table of real numbers (6 dims at a time, paged; heatmap-tinted cells; first 12 tokens, expandable) with **average** and **÷ length = final** footer rows, plus a worked-math panel for the clicked dimension. It uses `poolSteps()` (returns `{mean, norm, vec}`; `meanPoolNormalize()` is just its `.vec`) so the shown math is exactly what retrieval uses. View state is `state.embedView`. Step 2's chunk rows also show the first 4 numbers of each vector. `tokenizeText()` is the tokenizer alone (Step 4). Because every vector is unit-length, `cosineSim()` is a plain dot product.

History: an earlier version used TF-IDF vectors, and before that hashed character-bigram "embeddings" (which failed to discriminate topics). Both were removed in favor of the real model — **don't reintroduce fake/hashed embeddings.**

Vectors are shown as heatmap strips (`heatmapStrip()`: a 384×1 canvas turned into a data-URL `<img>`, blue/orange diverging, color scale from the 98th-percentile magnitude via `heatmapScale()` so outlier dims don't wash things out). A single delegated `mousemove` listener (`onPipelineMouseMove()`) shows per-dimension values from `heatmapRegistry` (cleared each render) and chunk snippets for scatter dots. Step 2 also has a "Try it" lab comparing two phrases (dog/puppy ≈ 0.81, dog/volcano ≈ 0.28).

The 2D "map of meaning" (`renderScatter()`) uses PCA (`chunkPca()`, power iteration, fitted on chunk vectors only and returning a `project()` function, so the query ★ is projected onto the same map without moving the chunks). Overlapping dots are nudged apart for visibility. Options: `queryVec` (draws the ★), `links` (dashed lines from ★ to retrieved chunks), `faded`, `selected`.

### Step 6 retrieval mode: cosine / BM25 / hybrid

`state.retrievalMode` ("cosine" | "bm25" | "hybrid") is a real, retrieval-affecting choice — switching it actually changes which chunks get retrieved in Steps 7–9, not just what's displayed. `rankChunks(mode, ctx = state)` scores and sorts all chunks for the question in `ctx` (`queryEmbed`, `query`, `hybridWeight`); `queryTerms()`/`bm25Breakdown()`/`bm25Score()` likewise take an optional query text.

- **`"cosine"`** (default) — `cosineSim(queryVec, chunk.vec)`, dense/semantic similarity. Can rank a chunk first with few or no shared words (e.g. "Where does the Pope live?" → the Rome/Vatican chunk).
- **`"bm25"`** — real Okapi BM25. `buildBm25Index()` runs in the Step 2 "Generate Chunk Embeddings" handler (and lazily if missing), storing per-chunk term frequencies/lengths plus `state.bm25Idf` (a word → IDF function, `ln(1 + (N − n + 0.5)/(n + 0.5))`), `state.bm25AvgLen`, `state.bm25N`. BM25 is purely lexical: whole words via `simpleTokenize()`/`contentWordsOf()` (lowercase, a small `STOPWORDS` list removed, no stemming — so "live" ≠ "lives", which the UI uses as a teaching moment), not the model's WordPiece tokens. It has term-frequency saturation (`BM25_K1` = 1.2) and length normalization (`BM25_B` = 0.75), and its scores are **unbounded** (not capped at 1) — called out explicitly in the UI. `bm25Breakdown()` returns per-word rows for the "How was that score calculated?" panel.
- **`"hybrid"`** — `state.hybridWeight * cosineScore + (1 - state.hybridWeight) * normalizedBm25Score` (slider, default 0.5). Because BM25 is unbounded, it's normalized per query by dividing by the top raw BM25 score among the chunks before blending — shown explicitly in the breakdown. This is a deliberately simplified stand-in for production dense + sparse fusion — real systems more often fuse *rankings* (e.g. Reciprocal Rank Fusion), as the UI notes.

Step 7 keeps the top `state.topK` (1 to min(6, #chunks), default 3) via `retrieved()`; Step 8 shows those chunks with question keywords highlighted and their position in the document.

### Generation step calls a real LLM (Groq)

`callGroq()` sends messages to Groq's OpenAI-compatible chat completions endpoint (`GROQ_URL`) using `GROQ_MODEL` ("openai/gpt-oss-120b" — one of Groq's hosted open-weight models; swap this constant if Groq retires or renames it). There is no backend: the request is made directly from the browser with `fetch()`. `max_tokens` defaults to 4096 because gpt-oss is a reasoning model whose hidden reasoning tokens count against it — a low limit cuts the visible answer off; `callGroq()` reports `finish_reason === "length"` as `truncated` and the UI warns. Errors (no key, 401, 429, network) come back as beginner-readable messages.

Step 9 shows the full prompt in color-coded sections (`RAG_SYSTEM_PROMPT` instructions, the retrieved chunks labeled `[Chunk N]`, the question; built by `buildRagMessages()`), sends it, and offers a "Compare: ask without RAG" button (bare question, `NO_RAG_SYSTEM_PROMPT`) for a side-by-side. Answers are Markdown rendered by `renderMarkdown()` (marked + DOMPurify; always sanitize, since it's model output going into `innerHTML`), styled by `.answer-md`.

**API key / proxy:** public deploys call a Cloudflare Worker (`worker/worker.js`, config in `worker/wrangler.toml`) that holds the key as a secret (`wrangler secret put GROQ_API_KEY`) and forwards to Groq. The Worker checks `Origin` against `ALLOWED_ORIGINS` (a light filter), rate-limits per IP via the `LIMITER` binding (10 req / 60 s), validates messages (1–4, system/user only, ≤12k chars), and fixes `model`/`max_tokens` server-side; it passes Groq's JSON through unchanged. In `app.js`, `callGroq()` uses `GROQ_PROXY_URL` (set to the deployed Worker URL; `window.RAG_PROXY_URL` overrides it) when set, otherwise falls back to calling Groq directly with `window.RAG_DEV_GROQ_KEY`. Both globals come from `config.local.js`, a **gitignored** file made by hand from the gitignored `.env` (`GROQ_API_KEY=...`). `index.html` loads it optionally, and a 404 on deploys is harmless. Deploy the Worker with `cd worker && npx wrangler login && npx wrangler secret put GROQ_API_KEY && npx wrangler deploy`, and add the site's URL to `ALLOWED_ORIGINS` before going live. For local proxy testing, put the key in `worker/.dev.vars` (gitignored) and run `npx wrangler dev`. **Never hardcode a real API key into `app.js`, `index.html`, or any committed file** — this app may be deployed publicly (it accompanies a blog post), and anything in the source is visible to every visitor.
