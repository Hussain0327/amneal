// Flat ESLint config (ESLint 9). Next 16 removed the `next lint` command, so
// linting now runs ESLint directly (`npm run lint` -> `eslint .`). In v16
// eslint-config-next ships a native flat config, so we spread its
// `core-web-vitals` preset directly — the same ruleset we extended via
// `next/core-web-vitals` before.
import next from "eslint-config-next/core-web-vitals";

const eslintConfig = [
  { ignores: [".next/**", "node_modules/**", "next-env.d.ts"] },
  ...next,
];

export default eslintConfig;
