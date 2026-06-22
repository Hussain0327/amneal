// Auto-grow a chat composer <textarea> to fit its content: reset to "auto" so it
// can shrink when text is deleted, then lock the height to scrollHeight. The
// element's CSS max-height caps it and overflow scroll takes over past the cap.
// `field-sizing: content` does this natively where supported; this is the
// fallback for browsers that lack it. Extracted from the Ask page so the height
// logic is unit-testable (the page just wires it to onInput + a value effect).
export function syncTextareaHeight(el: HTMLTextAreaElement | null): void {
  if (!el) return;
  el.style.height = "auto";
  el.style.height = `${el.scrollHeight}px`;
}
