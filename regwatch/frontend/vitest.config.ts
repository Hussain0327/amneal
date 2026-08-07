/// <reference types="vitest" />
import path from "node:path";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Vitest harness for the Ask grounding logic: the citation tokenizer (pure) and
// the Markdown stamp substitution (jsdom + Testing Library). Kept separate from
// the Next build — these run via `npm test`, not `next build`.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./test/setup.ts"],
    include: ["test/**/*.test.{ts,tsx}"],
    // Pin the suite's timezone: date assertions (sidebarHistory's "Jan 5,
    // 2026", time.test.ts) must not depend on the host's local offset.
    env: { TZ: "UTC" },
  },
  resolve: {
    // Mirror the tsconfig "@/*" path alias so component imports resolve in tests.
    alias: { "@": path.resolve(__dirname, ".") },
  },
});
