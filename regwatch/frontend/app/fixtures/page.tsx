"use client";

import { notFound } from "next/navigation";
import { useEffect, useState } from "react";

import { StatusTicker } from "@/components/StatusTicker";
import { AssistantTurn, ProvisionalDraft, UserTurn } from "@/components/Turns";
import type { Citation } from "@/lib/api";
import type { Turn } from "@/lib/turns";

// Design fixtures: the cited-chat states rendered from static fake data, so the
// UI can be exercised and polished without the backend. Renders bare (no auth,
// no sidebar). Not part of the product — production builds 404 unless
// NEXT_PUBLIC_FIXTURES=1.

const CITATIONS: Citation[] = [
  {
    short_name: "PSG_021457",
    page: 2,
    chunk_id: "psg-021457-p2-c1",
    doc_id: 312,
    version_id: 4,
    source_url: "https://www.accessdata.fda.gov/drugsatfda_docs/psg/PSG_021457.pdf",
    snippet:
      "Two options are recommended: an in vitro only approach, or an in vivo PK BE study with clinical endpoint considerations for locally acting products.",
    score: 0.71,
    recommended_date: "Mar 2023",
    diff_summary: "Added the in vitro-only Q1/Q2 sameness route.",
  },
  {
    short_name: "PSG_021457",
    page: 4,
    chunk_id: "psg-021457-p4-c2",
    doc_id: 312,
    version_id: 4,
    source_url: "https://www.accessdata.fda.gov/drugsatfda_docs/psg/PSG_021457.pdf",
    snippet:
      "In vitro studies should include single actuation content through container life and aerodynamic particle size distribution.",
    score: 0.64,
  },
];

const META = { model_name: "gpt-5.2", audit_id: 4217, turn_id: "t_fixture" };

const ANSWER: Turn = {
  role: "assistant",
  status: "answer",
  refused: false,
  // Inline citation tags ([short_name, p.N]) exercise the stamp substitution —
  // matched tags become clickable [n] stamps, an unmatched one stays literal.
  content:
    "For albuterol sulfate inhalation aerosol, the PSG recommends two routes to demonstrating bioequivalence — that is, showing the generic behaves the same as the reference product [PSG_021457, p.2].\n\n1. **In vitro option**: single actuation content, aerodynamic particle size distribution, spray pattern, plume geometry, and priming/repriming studies [PSG_021457, p.4].\n2. **In vivo option**: a PK BE study with the in vitro studies above.\n\nThe in vitro-only route applies when formulation and device are qualitatively (Q1) and quantitatively (Q2) the same as the reference. A non-matching reference like [PSG_999999, p.9] stays plain text.",
  citations: CITATIONS,
  clarify: [],
  related: [],
  interpretation: "BE study design for albuterol sulfate inhalation aerosol",
  reason: null,
  live: true,
  meta: META,
};

const REFUSAL: Turn = {
  role: "assistant",
  status: "refused",
  refused: true,
  content:
    "I searched the corpus for a product-specific guidance on atorvastatin oral tablets and did not find one — 1,795 documents checked, none covering this product.",
  citations: [],
  clarify: [],
  // "Related, not an answer": product names the analyst can re-ask about. These
  // are re-runnable queries, never citations — they cannot open the drawer.
  related: [
    {
      label: "atorvastatin calcium · oral tablet",
      query: "atorvastatin calcium oral tablet",
      filters: { normalized_name: "atorvastatin calcium" },
    },
    {
      label: "rosuvastatin calcium · oral tablet",
      query: "rosuvastatin calcium oral tablet",
      filters: { normalized_name: "rosuvastatin calcium" },
    },
  ],
  interpretation: null,
  reason: "no_product",
  live: true,
  meta: { ...META, audit_id: 4218 },
};

const CLARIFY: Turn = {
  role: "assistant",
  status: "clarify",
  refused: false,
  content: "Which propranolol product do you mean?",
  citations: [],
  clarify: [
    {
      label: "propranolol hydrochloride · oral tablet",
      query: "propranolol hydrochloride oral tablet",
      filters: { normalized_name: "propranolol hydrochloride", dosage_form: "tablet" },
    },
    {
      label: "propranolol hydrochloride · extended-release capsule",
      query: "propranolol hydrochloride extended-release capsule",
      filters: { normalized_name: "propranolol hydrochloride", dosage_form: "capsule, extended release" },
    },
    {
      label: "propranolol hydrochloride · oral solution",
      query: "propranolol hydrochloride oral solution",
      filters: { normalized_name: "propranolol hydrochloride", dosage_form: "solution" },
    },
  ],
  related: [],
  interpretation: "There are three propranolol guidances in the corpus — which dosage form?",
  reason: "multi_form",
  live: true,
  meta: { ...META, audit_id: 4220 },
};

const SCOPE: Turn = {
  role: "assistant",
  status: "scope_warning",
  refused: false,
  content:
    "That asks for a regulatory strategy recommendation. I surface and cite public FDA guidance; I don't draft, recommend, or decide.",
  citations: [],
  clarify: [],
  related: [],
  interpretation: null,
  reason: "scope_warning",
  live: true,
  meta: { ...META, audit_id: 4221 },
};

const TICKER_SCRIPT = [
  "Resolving product…",
  "Searching 1,795 documents…",
  "Found 6 passages — writing answer…",
];

function TickerDemo() {
  const [frames, setFrames] = useState<string[]>([TICKER_SCRIPT[0]]);
  useEffect(() => {
    const id = setInterval(() => {
      setFrames((prev) =>
        prev.length >= TICKER_SCRIPT.length ? [TICKER_SCRIPT[0]] : [...prev, TICKER_SCRIPT[prev.length]],
      );
    }, 1600);
    return () => clearInterval(id);
  }, []);
  return <StatusTicker frames={frames} />;
}

function Section({ no, title, children }: { no: string; title: string; children: React.ReactNode }) {
  return (
    <section style={{ marginTop: "3.6rem" }}>
      <div className="flex items-baseline gap-3">
        <span className="kicker">{no}</span>
        <h2 className="kicker" style={{ color: "var(--ink-soft)", margin: 0 }}>
          {title}
        </h2>
        <hr className="hair grow" />
      </div>
      {children}
    </section>
  );
}

export default function FixturesPage() {
  if (process.env.NODE_ENV === "production" && process.env.NEXT_PUBLIC_FIXTURES !== "1") {
    notFound();
  }
  const noop = () => {};
  return (
    <main className="canvas" style={{ minHeight: "100vh" }}>
      <div className="measure" style={{ margin: "0 auto" }}>
        <header className="mb-9">
          <div className="kicker">00 · REGWATCH · Design fixtures</div>
          <h1 className="display" style={{ fontSize: "clamp(2rem, 4vw, 2.8rem)", marginTop: "0.5rem" }}>
            Cited chat, every state.
          </h1>
          <hr className="rule-gold draw" style={{ marginTop: "0.9rem", maxWidth: "11rem" }} />
          <p style={{ marginTop: "1rem", maxWidth: "44rem", color: "var(--ink-soft)", fontSize: "0.95rem" }}>
            Static fake data. The clarify option buttons are inert; the answer-feedback thumbs are
            the live component (they will attempt a POST).
          </p>
        </header>

        <Section no="F0" title="Opening — the empty state (an invitation to act)">
          <div className="chat__empty">
            <p className="kicker chat__empty-kicker">Ask the corpus</p>
            <h2 className="chat__empty-lead">What does the FDA guidance say?</h2>
            <p className="chat__empty-note">
              Plain-language answers over FDA product-specific guidance &mdash; every claim cited to
              its source. Ask in your own words; if a question is ambiguous it asks rather than
              guesses.
            </p>
            <div className="chat__starters">
              {[
                {
                  kind: "Study design",
                  items: [
                    "BE study for albuterol sulfate inhalation aerosol",
                    "Beclomethasone dipropionate aerosol study type",
                  ],
                },
                { kind: "Dissolution & specs", items: ["Dissolution method for metformin hydrochloride"] },
                { kind: "Just a product name", items: ["propranolol"] },
              ].map((g) => (
                <div className="starter" key={g.kind}>
                  <p className="starter__kind">{g.kind}</p>
                  <div className="chat__examples">
                    {g.items.map((label) => (
                      <button key={label} className="pill" type="button">
                        {label}
                      </button>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </div>
        </Section>

        <Section no="F1" title="Status ticker — SSE docket log (cycles)">
          <TickerDemo />
        </Section>

        <Section no="F1b" title="Streaming draft — provisional, before citation validation">
          <div className="chat-row">
            <span className="avatar" aria-hidden>
              RW
            </span>
            <div className="msg">
              <ProvisionalDraft text="For albuterol sulfate inhalation aerosol, the PSG recommends two routes to demonstrating bioequivalence [PSG_021457, p.2]. The in vitro option covers single actuation content and aerodynamic particle size distribution" />
            </div>
          </div>
        </Section>

        <Section no="F2" title="Answer · cited finding">
          <UserTurn content="What BE study design is recommended for albuterol sulfate inhalation aerosol?" live />
          <AssistantTurn turn={ANSWER} sessionId="s_fixture" onPick={noop} onCite={noop} busy={false} />
        </Section>

        <Section no="F3" title="Helpful refusal · not in corpus">
          <UserTurn content="What BE study design is recommended for atorvastatin oral tablets?" live />
          <AssistantTurn turn={REFUSAL} sessionId="s_fixture" onPick={noop} onCite={noop} busy={false} />
        </Section>

        <Section no="F4" title="Clarify · options + free-text reply">
          <UserTurn content="propranolol" live />
          <AssistantTurn turn={CLARIFY} sessionId="s_fixture" onPick={noop} onCite={noop} busy={false} />
        </Section>

        <Section no="F5" title="Scope warning · declined">
          <UserTurn content="Which BE pathway should we pick for our ANDA?" live />
          <AssistantTurn turn={SCOPE} sessionId="s_fixture" onPick={noop} onCite={noop} busy={false} />
        </Section>

        <footer style={{ margin: "4rem 0 2rem" }}>
          <hr className="hair" />
          <p className="code mt-3" style={{ fontSize: "0.7rem", color: "var(--ink-faint)" }}>
            regwatch design fixtures · cited chat · not a product surface
          </p>
        </footer>
      </div>
    </main>
  );
}
