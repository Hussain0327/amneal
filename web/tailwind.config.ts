import type { Config } from "tailwindcss";

// Tailwind here drives layout utilities only — color and type live in CSS
// variables (see app/globals.css). These tokens mirror that system so any
// utility usage stays on-brand.
const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        paper: { DEFAULT: "#faf6ec", deep: "#f4eede", edge: "#e4d9bf" },
        ink: { DEFAULT: "#16213a", soft: "#6c7286", faint: "#9a9784" },
        gold: { DEFAULT: "#f5b400", deep: "#c98a0c", ink: "#8a5b00" },
        oxblood: "#7a2e2e",
      },
      fontFamily: {
        sans: ["var(--font-body)", "ui-sans-serif", "system-ui", "sans-serif"],
        display: ["var(--font-display)", "Georgia", "serif"],
        mono: ["var(--font-mono)", "ui-monospace", "monospace"],
        script: ["var(--font-script)", "cursive"],
      },
    },
  },
  plugins: [],
};

export default config;
