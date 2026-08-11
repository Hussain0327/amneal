import type { Metadata } from "next";

import "./research.css";

// The Research Studio sits outside the (shell) group for the same reason the
// Compliance Studio does: it is a workbench, not a page, so it must not inherit
// the canvas padding or the product scope bar that every (shell) route gets.
//
// AND ONE DIFFERENCE THAT MATTERS. The Compliance Studio covers the viewport
// and renders OVER the spine rail. This one renders INSIDE it: the rail stays
// on screen down the left edge and the studio fills the space beside it. That
// is not a styling preference -- the rail now carries two studios, R and C, and
// a room you cannot leave without the browser's back button is not a room in a
// building. The consequence is that this surface needs the same providers the
// (shell) group hands its routes; they are mounted in page.tsx rather than
// here, because this file has to stay a server component to export metadata and
// the session provider is keyed off the signed-in identity.
//
// Auth is still enforced upstream by <AuthProvider> in the root layout.

export const metadata: Metadata = {
  title: "Research Studio | Amneal REGWATCH",
  description:
    "Research public FDA guidance and turn it into threads, dossiers, bulletins and papers.",
};

export default function ResearchLayout({ children }: { children: React.ReactNode }) {
  return children;
}
