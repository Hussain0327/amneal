"use client";

// Root-level error boundary: catches render errors that escape the root
// layout, reports them to Sentry (no-op when the DSN is unset), and shows a
// quiet recovery page. global-error replaces the root layout entirely, so it
// must render its own <html>/<body>; styles are inline because the layout's
// font variables and globals.css are not in scope here.
import * as Sentry from "@sentry/nextjs";
import { useEffect } from "react";

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    Sentry.captureException(error);
  }, [error]);

  return (
    <html lang="en">
      <body
        style={{
          margin: 0,
          minHeight: "100vh",
          display: "grid",
          placeItems: "center",
          background: "#faf6ec",
          color: "#16213a",
          fontFamily: "ui-sans-serif, system-ui, sans-serif",
        }}
      >
        <div style={{ textAlign: "center", padding: "2rem" }}>
          <p
            style={{
              fontFamily: "ui-monospace, monospace",
              fontSize: "0.66rem",
              fontWeight: 600,
              letterSpacing: "0.34em",
              textTransform: "uppercase",
              color: "#8a5b00",
              margin: 0,
            }}
          >
            Regwatch · Fault
          </p>
          <h1 style={{ fontWeight: 600, fontSize: "1.4rem", margin: "0.9rem 0 0.4rem" }}>
            Something failed while rendering.
          </h1>
          <p style={{ color: "#6c7286", fontSize: "0.95rem", margin: "0 0 1.4rem" }}>
            The fault has been recorded. Your data is unaffected.
          </p>
          <button
            onClick={() => reset()}
            style={{
              fontFamily: "ui-monospace, monospace",
              fontSize: "0.74rem",
              fontWeight: 600,
              letterSpacing: "0.16em",
              textTransform: "uppercase",
              color: "#2a1c00",
              background: "linear-gradient(96deg, #f5b400 0%, #c98a0c 100%)",
              border: 0,
              borderRadius: "2px",
              padding: "0.72rem 1.4rem",
              cursor: "pointer",
            }}
          >
            Reload
          </button>
        </div>
      </body>
    </html>
  );
}
