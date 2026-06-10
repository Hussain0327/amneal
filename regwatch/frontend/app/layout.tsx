import type { Metadata } from "next";
import { Fraunces, IBM_Plex_Mono, Public_Sans, Yellowtail } from "next/font/google";

import { AuthProvider } from "@/components/AuthProvider";
import "./globals.css";

// Editorial display serif — gravitas with warmth (page titles, pull quotes).
const fraunces = Fraunces({
  subsets: ["latin"],
  variable: "--font-display",
  weight: ["400", "500", "600", "900"],
  style: ["normal", "italic"],
});
// Public Sans — the US government design-system typeface. The right body voice
// for an FDA-facing tool, and pointedly not Inter.
const publicSans = Public_Sans({
  subsets: ["latin"],
  variable: "--font-body",
  weight: ["300", "400", "500", "600", "700"],
});
// Monospace for the codes this domain runs on: PSG / application / NDC numbers.
const plexMono = IBM_Plex_Mono({
  subsets: ["latin"],
  variable: "--font-mono",
  weight: ["400", "500", "600"],
});
// The Amneal brush wordmark, kept as the brand mark.
const yellowtail = Yellowtail({ subsets: ["latin"], variable: "--font-script", weight: "400" });

export const metadata: Metadata = {
  title: "Amneal REGWATCH",
  description: "FDA guidance intelligence. Public data only; every claim is cited.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body
        className={`${fraunces.variable} ${publicSans.variable} ${plexMono.variable} ${yellowtail.variable}`}
      >
        {/* The shell (sidebar + canvas) lives behind the auth gate. */}
        <AuthProvider>{children}</AuthProvider>
      </body>
    </html>
  );
}
