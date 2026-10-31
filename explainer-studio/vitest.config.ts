import { defineConfig } from "vitest/config";

// Separate from vite.config.ts, whose root is app/.
export default defineConfig({ test: { include: ["tests/**/*.test.ts"] } });
