"use client";

import { useEffect, useRef } from "react";

import { BookIcon, ChatIcon, CloseIcon, SendIcon } from "@/components/studio/icons";
import type { AssistantMessage, StudioDoc } from "@/lib/studio-types";

interface AssistantPanelProps {
  doc: StudioDoc;
  messages: AssistantMessage[];
  draft: string;
  thinking: boolean;
  intro: string;
  onDraftChange: (v: string) => void;
  onSend: (prompt: string) => void;
  onClose: () => void;
}

export function AssistantPanel({
  doc,
  messages,
  draft,
  thinking,
  intro,
  onDraftChange,
  onSend,
  onClose,
}: AssistantPanelProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const boxRef = useRef<HTMLTextAreaElement>(null);

  // Follow the stream. Each token lands as a new render, so pinning to the
  // bottom here keeps the newest line visible without a scroll listener.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [messages, thinking]);

  // Grow with the draft. Collapsing first is required: scrollHeight never
  // reports less than the current height, so the box would only ever grow.
  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, [draft]);

  const canSend = draft.trim() !== "";

  function submit() {
    if (!canSend) return;
    onSend(draft.trim());
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    // Enter mid-composition commits an IME candidate; it is not a send.
    if (e.key !== "Enter" || e.shiftKey || e.nativeEvent.isComposing) return;
    e.preventDefault();
    submit();
  }

  return (
    <>
      <div className="st-panel__head">
        <span className="st-panel__title">
          <ChatIcon />
          Ask about this document
        </span>
        <button type="button" className="st-icon-btn st-panel__close" onClick={onClose} aria-label="Close assistant">
          <CloseIcon />
        </button>
      </div>

      <div className="st-panel__scroll" ref={scrollRef}>
        <p className="st-ctx">
          Context: <b>{doc.name}</b> plus linked CTD modules
        </p>

        {/* The opening line carries no sources because it makes no claim. */}
        <div className="st-msg">
          <div className="st-msg__body">{intro}</div>
        </div>

        {messages.map((m) =>
          m.role === "user" ? (
            <div key={m.id} className="st-msg st-msg--user">
              <div className="st-msg__user">{m.text}</div>
            </div>
          ) : (
            <div key={m.id} className="st-msg">
              <div className="st-msg__body">
                {m.text}
                {m.streaming && <span className="st-msg__caret" aria-hidden="true" />}
              </div>
              {/* Sources only once the answer is complete: a half-streamed
                  reply has not made its claim yet, so it cannot be sourced. */}
              {!m.streaming &&
                m.sources !== undefined &&
                (m.sources.length > 0 ? (
                  <div className="st-src">
                    {m.sources.map((s, i) => (
                      <div key={`${m.id}-src-${i}`} className="st-src__item">
                        <BookIcon size={11} />
                        <span>{s}</span>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="st-msg__nosrc">No source in this repository.</p>
                ))}
            </div>
          ),
        )}

        {thinking && (
          <p className="st-msg__nosrc" role="status">
            Reading {doc.name}...
          </p>
        )}
      </div>

      <div className="st-panel__foot">
        <div className="st-compose">
          <textarea
            ref={boxRef}
            rows={1}
            value={draft}
            onChange={(e) => onDraftChange(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder="Ask about this document, a guideline, or a cross-reference"
            aria-label="Ask about this document"
          />
          <button type="button" className="st-compose__send" onClick={submit} disabled={!canSend} aria-label="Send">
            <SendIcon />
          </button>
        </div>
        <p className="st-foot-note">Read-only. It explains and cites; it never edits your document.</p>
      </div>
    </>
  );
}
