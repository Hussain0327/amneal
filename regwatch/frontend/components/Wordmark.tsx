// The Amneal brush wordmark beside a gold wax-seal emblem — the masthead mark.
export function Wordmark({ size = "lg" }: { size?: "lg" | "sm" }) {
  const lg = size === "lg";
  return (
    <div className="flex items-center gap-3">
      <span className="seal" aria-hidden style={lg ? undefined : { width: "1.7rem", height: "1.7rem" }} />
      <span className="wordmark" style={{ fontSize: lg ? "3.1rem" : "2.2rem" }}>
        Amneal
      </span>
    </div>
  );
}
