// Builds the standalone viewer (renderer only, no chat/API) that "Export HTML"
// inlines into the downloaded file. Output: app/public/viewer.js.
import { defineConfig } from "vite";

export default defineConfig({
  publicDir: false,
  build: {
    outDir: "app/public",
    emptyOutDir: false,
    lib: { entry: "app/src/viewer.ts", formats: ["iife"], name: "ExplainerViewer", fileName: () => "viewer.js" },
  },
});
