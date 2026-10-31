import { defineConfig } from "vite";

export default defineConfig({
  root: "app",
  server: {
    port: 5173,
    // Same-origin API calls in dev; the Flask server runs on 8787 (scripts/dev-api.mjs).
    proxy: { "/api": { target: "http://127.0.0.1:8787" } },
  },
  build: { outDir: "../dist", emptyOutDir: true },
});
