// Cloudflare Worker: forwards the RAG Explainer's LLM requests to Groq so the API key
// never reaches the browser. The key is a Worker secret (wrangler secret put GROQ_API_KEY).

const GROQ_URL = "https://api.groq.com/openai/v1/chat/completions";
const MODEL = "openai/gpt-oss-120b"; // fixed here so callers can't pick a pricier model
const MAX_TOKENS = 4096;             // gpt-oss reasons first; lower limits cut answers off
const MAX_MESSAGES = 4;
const MAX_CHARS = 12000;             // app's real max is ~5k (6 chunks × 600 chars + prompt)

function allowedOrigins(env) {
  return (env.ALLOWED_ORIGINS || "").split(",").map((s) => s.trim()).filter(Boolean);
}

function json(body, status, origin) {
  const headers = { "Content-Type": "application/json" };
  if (origin) {
    headers["Access-Control-Allow-Origin"] = origin;
    headers["Vary"] = "Origin";
  }
  return new Response(JSON.stringify(body), { status, headers });
}

function validMessages(messages) {
  if (!Array.isArray(messages) || messages.length < 1 || messages.length > MAX_MESSAGES) return false;
  let chars = 0;
  for (const m of messages) {
    if (!m || (m.role !== "system" && m.role !== "user") || typeof m.content !== "string") return false;
    chars += m.content.length;
  }
  return chars <= MAX_CHARS;
}

export default {
  async fetch(request, env) {
    const origin = request.headers.get("Origin") || "";
    // Light filter only: non-browser clients can fake Origin. The rate limit is the real guard.
    if (!allowedOrigins(env).includes(origin)) {
      return json({ error: { message: "Origin not allowed." } }, 403);
    }

    if (request.method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: {
          "Access-Control-Allow-Origin": origin,
          "Access-Control-Allow-Methods": "POST, OPTIONS",
          "Access-Control-Allow-Headers": "Content-Type",
          "Access-Control-Max-Age": "86400",
          "Vary": "Origin",
        },
      });
    }
    if (request.method !== "POST") {
      return json({ error: { message: "Use POST." } }, 405, origin);
    }

    if (env.LIMITER) {
      const ip = request.headers.get("CF-Connecting-IP") || "unknown";
      const { success } = await env.LIMITER.limit({ key: ip });
      if (!success) return json({ error: { message: "Too many requests." } }, 429, origin);
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return json({ error: { message: "Body must be JSON." } }, 400, origin);
    }
    if (!validMessages(body?.messages)) {
      return json({ error: { message: "Invalid messages." } }, 400, origin);
    }
    const messages = body.messages.map(({ role, content }) => ({ role, content }));

    let upstream;
    try {
      upstream = await fetch(GROQ_URL, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${env.GROQ_API_KEY}`,
        },
        body: JSON.stringify({ model: MODEL, messages, max_tokens: MAX_TOKENS }),
      });
    } catch {
      return json({ error: { message: "Couldn't reach Groq." } }, 502, origin);
    }

    return new Response(upstream.body, {
      status: upstream.status,
      headers: {
        "Content-Type": upstream.headers.get("Content-Type") || "application/json",
        "Access-Control-Allow-Origin": origin,
        "Vary": "Origin",
      },
    });
  },
};
