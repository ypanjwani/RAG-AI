# Explainer Studio: the built frontend and the Flask API in one image (Render, Fly, Railway...).
# Build from explainer-studio/:  docker build -t explainer-studio .
# Run:  docker run -p 8000:8000 -e ANTHROPIC_API_KEY=... -e JEV_API_KEY=... -e DATABASE_URL=postgres://... -e JWT_SECRET=... explainer-studio

FROM node:22-slim AS web
WORKDIR /src
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

# Code Lab (server/explainer/codelab.py) runs each AI-written widget in jsdom before showing it: Node, jsdom, and
# Vega + Vega-Lite for Code Lab 2.0's charts (same versions as package.json). Playwright takes Code Lab 7.0's
# screenshots (scripts/snap-widget.mjs); its browser is installed in the final image below.
FROM node:22-slim AS checker
WORKDIR /chk
RUN npm init -y >/dev/null && npm install --omit=dev jsdom@26.1.0 vega@6.4.0 vega-lite@6.5.0 playwright@1.64.1

FROM python:3.12-slim
WORKDIR /app
COPY server/ ./server/
RUN pip install --no-cache-dir ./server
COPY --from=web /src/dist ./dist
COPY --from=checker /usr/local/bin/node /usr/local/bin/node
COPY --from=checker /chk/node_modules ./node_modules
COPY scripts/check-widget.mjs scripts/snap-widget.mjs scripts/check-playground.mjs ./scripts/
# Headless Chromium for Code Lab 7.0's look check, with the system libraries it needs (~+300 MB). Without it the look
# check is skipped (it fails open) and everything else works.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN node node_modules/playwright/cli.js install --with-deps --only-shell chromium && rm -rf /var/lib/apt/lists/*
# The helper kit (window.XW) every widget is checked with, as in the real frame, and Vega's eval-free interpreter.
COPY shared/widget-kit.js shared/vega-interpreter.js ./shared/
# Playground mode's checker (scripts/check-playground.mjs) runs the playground validator and the runtime files it loads.
COPY shared/playground/ ./shared/playground/
ENV EXPLAINER_DIST=/app/dist PORT=8000 ENV=production
WORKDIR /app/server
EXPOSE 8000
# Migrate the database once, before the workers start, then serve.
CMD ["sh", "-c", "alembic upgrade head && exec gunicorn -c gunicorn.conf.py"]
