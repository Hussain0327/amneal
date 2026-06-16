// Guard a backend- or model-supplied URL before binding it to an <a href>. FDA
// citation/evidence/source URLs arrive from the API (and markdown links from the
// model), so an attacker-influenced `javascript:`/`data:` scheme could otherwise
// become a click-to-run sink. Returns the URL only when it parses to an
// http/https/mailto scheme; otherwise undefined, so the caller renders the
// anchor inert (or as plain text). The CSP header is the defense-in-depth
// backstop for this same class.
export function safeHref(u?: string | null): string | undefined {
  if (!u) return undefined;
  try {
    // A relative URL resolves against the base and inherits its (safe) scheme;
    // an absolute `javascript:`/`data:` keeps its own and is rejected.
    const base = typeof window !== "undefined" ? window.location.origin : "https://regwatch.invalid";
    const { protocol } = new URL(u, base);
    return protocol === "http:" || protocol === "https:" || protocol === "mailto:" ? u : undefined;
  } catch {
    return undefined;
  }
}
