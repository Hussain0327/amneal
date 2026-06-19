import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The frontend's first test harness. CI already gates eslint + tsc + next build +
// OpenAPI contract-drift; vitest adds component/unit behaviour coverage (e.g. the
// INV-2 "declined turns carry no citation affordance" invariant) that the static
// gates can't express. Kept intentionally small: jsdom + Testing Library, no
// coverage thresholds or browser mode until a concrete need appears.
const rootDir = dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  plugins: [react()],
  resolve: {
    // Mirror tsconfig's "@/*" -> "./*". A regex anchor is required: a bare "@"
    // string alias would also swallow "@testing-library/*" and break the harness.
    alias: [{ find: /^@\//, replacement: `${rootDir}/` }],
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
    include: ["{components,lib}/**/*.test.{ts,tsx}"],
  },
});
