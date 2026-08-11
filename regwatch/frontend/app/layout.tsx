import type { Metadata, Viewport } from "next";
import localFont from "next/font/local";

import { AuthProvider } from "@/components/AuthProvider";
import "./globals.css";

// Editorial display serif — gravitas with warmth (page titles, pull quotes).
const fraunces = localFont({
  src: [
    {
      path: "./fonts/fraunces/fraunces-latin-wght-normal.woff2",
      weight: "100 900",
      style: "normal",
    },
    {
      path: "./fonts/fraunces/fraunces-latin-wght-italic.woff2",
      weight: "100 900",
      style: "italic",
    },
  ],
  variable: "--font-display",
  display: "swap",
});
// Public Sans — the US government design-system typeface. The right body voice
// for an FDA-facing tool, and pointedly not Inter.
const publicSans = localFont({
  src: "./fonts/public-sans/public-sans-latin-wght-normal.woff2",
  variable: "--font-body",
  weight: "100 900",
  style: "normal",
  display: "swap",
});
// Monospace for the codes this domain runs on: PSG / application / NDC numbers.
const plexMono = localFont({
  src: [
    {
      path: "./fonts/ibm-plex-mono/ibm-plex-mono-latin-400-normal.woff2",
      weight: "400",
      style: "normal",
    },
    {
      path: "./fonts/ibm-plex-mono/ibm-plex-mono-latin-500-normal.woff2",
      weight: "500",
      style: "normal",
    },
    {
      path: "./fonts/ibm-plex-mono/ibm-plex-mono-latin-600-normal.woff2",
      weight: "600",
      style: "normal",
    },
  ],
  variable: "--font-mono",
  display: "swap",
});
// Text serif for the document body in the Compliance Studio. The artifact under
// review is a controlled printed record; setting it in a workhorse text serif
// keeps it visually distinct from the app chrome around it. Fraunces is a
// display face and does not hold up at 1rem across a full page of prose.
const sourceSerif = localFont({
  src: [
    {
      path: "./fonts/source-serif-4/source-serif-4-latin-wght-normal.woff2",
      weight: "200 900",
      style: "normal",
    },
    {
      path: "./fonts/source-serif-4/source-serif-4-latin-wght-italic.woff2",
      weight: "200 900",
      style: "italic",
    },
  ],
  variable: "--font-serif",
  display: "swap",
});
// The Amneal brush wordmark, kept as the brand mark.
const yellowtail = localFont({
  src: "./fonts/yellowtail/yellowtail-latin-400-normal.woff2",
  variable: "--font-script",
  weight: "400",
  style: "normal",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Amneal REGWATCH",
  description: "FDA guidance intelligence. Public data only; every claim is cited.",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body
        className={`${fraunces.variable} ${publicSans.variable} ${plexMono.variable} ${sourceSerif.variable} ${yellowtail.variable}`}
      >
        {/* The shell (sidebar + canvas) lives behind the auth gate. */}
        <AuthProvider>{children}</AuthProvider>
      </body>
    </html>
  );
}
