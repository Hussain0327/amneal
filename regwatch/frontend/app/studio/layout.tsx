import type { Metadata } from "next";

import "./studio.css";

// The studio sits outside the (shell) group on purpose: it takes the whole
// viewport and runs its own dark token set, so it must not inherit the
// parchment sidebar or the canvas padding. Auth is still enforced upstream by
// <AuthProvider> in the root layout.

export const metadata: Metadata = {
  title: "Compliance Studio | Amneal REGWATCH",
  description: "Review and check CMC documents against ICH, USP, 21 CFR and internal SOPs.",
};

export default function StudioLayout({ children }: { children: React.ReactNode }) {
  return children;
}
