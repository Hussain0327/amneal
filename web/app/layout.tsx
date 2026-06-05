import type { Metadata } from "next";
import { Inter, Yellowtail } from "next/font/google";

import { Sidebar } from "@/components/Sidebar";
import "./globals.css";

const inter = Inter({ subsets: ["latin"], variable: "--font-inter" });
const yellowtail = Yellowtail({ weight: "400", subsets: ["latin"], variable: "--font-yellowtail" });

export const metadata: Metadata = {
  title: "Amneal REGWATCH",
  description: "Operational POC over the FDA guidance corpus. Public data only; cites every claim.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className={`${inter.variable} ${yellowtail.variable} font-sans`}>
        <div className="flex min-h-screen">
          <Sidebar />
          <main className="flex-1 px-8 py-7">{children}</main>
        </div>
      </body>
    </html>
  );
}
