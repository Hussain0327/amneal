"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { safeHref } from "@/lib/url";

// Renders model/dossier markdown as editorial prose (see .prose in globals.css).
export function Markdown({ children }: { children: string }) {
  return (
    <div className="prose">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          // Guard model-authored link schemes (no javascript:/data: click-to-run).
          a: ({ href, children }) => (
            <a href={safeHref(href)} target="_blank" rel="noreferrer">
              {children}
            </a>
          ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
