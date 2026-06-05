"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

// Renders model/dossier markdown (headings, lists, tables, links) with the
// Amneal prose styling defined in globals.css. Links open in a new tab.
export function Markdown({ children }: { children: string }) {
  return (
    <div className="prose-amneal">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noreferrer">
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
