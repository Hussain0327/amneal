import type { Config } from "tailwindcss";

// Amneal brand palette — mirrors src/regwatch/ui/branding.py so the Next.js UI
// is visually identical to the Streamlit POC it replaces.
const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        gold: { DEFAULT: "#F5B400", deep: "#D99400", soft: "#FFF8E6" },
        ink: { DEFAULT: "#16213A", soft: "#5A6478" },
      },
      fontFamily: {
        sans: ["var(--font-inter)", "ui-sans-serif", "system-ui", "sans-serif"],
        script: ["var(--font-yellowtail)", "cursive"],
      },
    },
  },
  plugins: [],
};

export default config;
