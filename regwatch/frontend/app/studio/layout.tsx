import type { Metadata } from "next";

import "./studio.css";

// The studio sits outside the (shell) group on purpose: it takes the whole
// viewport, so it must not inherit the sidebar or the canvas padding. It is set
// in the same parchment palette as the rest of the app -- every colour in
// studio.css is a token from globals.css, used verbatim -- and it carries no
// product scope bar, which is the gap to close before it can hold documents for
// "the product under review". Auth is still enforced upstream by <AuthProvider>
// in the root layout.

export const metadata: Metadata = {
  title: "Compliance Studio | Amneal REGWATCH",
  description: "Review and check CMC documents against ICH, USP, 21 CFR and internal SOPs.",
};

export default function StudioLayout({ children }: { children: React.ReactNode }) {
  return children;
}
