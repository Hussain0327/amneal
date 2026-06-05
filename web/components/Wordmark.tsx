// The Amneal brush-script wordmark, rendered as gold gradient text (no image),
// mirroring the .amneal-wordmark style in the Streamlit branding.

export function Wordmark({ size = "lg" }: { size?: "lg" | "sm" }) {
  return (
    <span className={`amneal-wordmark inline-block ${size === "lg" ? "text-6xl" : "text-3xl"}`}>
      Amneal
    </span>
  );
}
