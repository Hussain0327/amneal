"use client";

import { notFound } from "next/navigation";
import { useEffect, useState } from "react";

import { StatusTicker } from "@/components/StatusTicker";
import { AssistantTurn, ProvisionalDraft, UserTurn } from "@/components/Turns";
import { DossierPlan } from "@/components/assemble/DossierPlan";
import { DossierView } from "@/components/assemble/DossierView";
import { Intake } from "@/components/assemble/Intake";
import { AlertEntry } from "@/components/WatchEntry";
import type { AlertRecord, Citation } from "@/lib/api";
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
  createdAt: "2026-01-07T14:32:00Z",
  statusLog: [],
  streamFellBack: false,
  draftWithdrawn: null,
  id: null,
  meta: META,
};

// Bibliography-style answer: the model cited with bare [n] markers and a
// trailing "Sources:" list (the prod placement bug). The UI splits the trailer
// off (its own references replace it) and resolves each marker through the
// trailer into a real stamp; an unlisted marker ([3]) stays literal prose.
const BIBLIO: Turn = {
  ...ANSWER,
  content:
    "The recommended bioequivalence study design is a fasting, single-dose, two-way crossover in vivo study [1]. In vitro studies should include single actuation content through container life and aerodynamic particle size distribution [2]. A marker with no bibliography entry stays plain text [3].\n\nSources:\n[1] [PSG_021457, p.2]\n[2] [PSG_021457, p.4]",
  interpretation: null,
  meta: { ...META, audit_id: 4222 },
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
  createdAt: "2026-01-07T14:41:00Z",
  statusLog: [],
  streamFellBack: false,
  draftWithdrawn: null,
  id: null,
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
  createdAt: "2026-01-07T14:47:00Z",
  statusLog: [],
  streamFellBack: false,
  draftWithdrawn: null,
  id: null,
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
  createdAt: "2026-01-07T14:52:00Z",
  statusLog: [],
  streamFellBack: false,
  draftWithdrawn: null,
  id: null,
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

// ---- Assemble (02) / Watch (03) fixtures ----

// Faithful to the markdown build_dossier emits: H1 title + lettered ## A–F.
const DOSSIER_MD = `# albuterol sulfate dossier

## A. Product-Specific Guidance(s)
- **Albuterol Sulfate** (metered aerosol; inhalation) — [final, recommended Jun 2024](https://www.accessdata.fda.gov/drugsatfda_docs/psg/PSG_020503.pdf)
  - Latest change: Added the in vitro-only Q1/Q2 sameness route.

## B. Extracted BE Requirements
### From https://www.accessdata.fda.gov/drugsatfda_docs/psg/PSG_020503.pdf
- **study_type**: In vitro option or in vivo PK BE study
    > Two options are recommended: an in vitro only approach, or an in vivo PK BE study with clinical endpoint considerations.
- **in_vitro_battery**: Single actuation content; aerodynamic particle size distribution

## C. Reference Listed Drug (RLD) Label
- Brand: PROVENTIL HFA  /  Generic: albuterol sulfate
- Application: NDA020503
- Source: https://api.fda.gov/drug/label.json
  - **Indications**: Treatment or prevention of bronchospasm in adults and children 4 years of age and older with reversible obstructive airway disease…

## D. Applicable Guidance — Q&A Summary
For albuterol sulfate inhalation aerosol, the guidance recommends demonstrating bioequivalence through either an in vitro only approach (when Q1/Q2 sameness holds) or an in vivo PK study with the full in vitro battery.

### Sources
- PSG_020503, p.2: https://www.accessdata.fda.gov/drugsatfda_docs/psg/PSG_020503.pdf

## E. Dissolution Method
- See FDA Dissolution Methods Database: https://www.accessdata.fda.gov/scripts/cder/dissolution/

## F. Requirements Checklist (scaffold)
_This is what the PSG calls for. It does not assert what the company has done._
- [ ] Single actuation content through container life (sac)
- [ ] Aerodynamic particle size distribution (apsd)
- [ ] Spray pattern and plume geometry (spray)`;

function makeFixtureAlert(overrides: Partial<AlertRecord>): AlertRecord {
  return {
    product_id: 1,
    active_ingredient: "Albuterol Sulfate",
    listing_appl_no: "NDA020503",
    listing_psg_type: "final",
    psg_document_id: 10,
    psg_version_id: 2,
    captured_at: "2026-06-01T12:00:00Z",
    diff_summary: "Strength table updated; dissolution method revised.",
    confidence: 0.9,
    rationale: "canonical",
    source_url: "https://www.fda.test/psg.pdf",
    ...overrides,
  };
}

const FIXTURE_ALERTS: AlertRecord[] = [
  makeFixtureAlert({
    change_kind: "new",
    active_ingredient: "Budesonide",
    listing_appl_no: "NDA020929",
    psg_document_id: 11,
    captured_at: "2026-06-03T08:00:00Z",
    diff_summary: "Initial version ingested. Begins: This guidance addresses…",
    confidence: 0.97,
  }),
  makeFixtureAlert({ change_kind: "revised" }),
  makeFixtureAlert({
    change_kind: "revised",
    active_ingredient: "Cetirizine Hydrochloride",
    listing_appl_no: "NDA022155",
    listing_psg_type: "draft",
    psg_document_id: 12,
    captured_at: "2026-05-28T09:30:00Z",
    diff_summary: null,
    confidence: 0.74,
  }),
];

// The intake is a controlled component; the fixture owns throwaway state so
// typing works in the sandbox.
function IntakeDemo() {
  const [ingredient, setIngredient] = useState("albuterol sulfate");
  const [dosage, setDosage] = useState("");
  const [rld, setRld] = useState("");
  return (
    <Intake
      ingredient={ingredient}
      dosage={dosage}
      rld={rld}
      onIngredient={setIngredient}
      onDosage={setDosage}
      onRld={setRld}
      onSubmit={(e) => e.preventDefault()}
      loading={false}
    />
  );
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
            {/* Bare avatar (no marginalia): a draft has no provenance yet. The
                margin wrapper keeps the column aligned with settled turns. */}
            <div className="chat-row__margin">
              <span className="avatar" aria-hidden>
                RW
              </span>
            </div>
            <div className="msg">
              <ProvisionalDraft text="For albuterol sulfate inhalation aerosol, the PSG recommends two routes to demonstrating bioequivalence [PSG_021457, p.2]. The in vitro option covers single actuation content and aerodynamic particle size distribution" />
            </div>
          </div>
        </Section>

        <Section no="F2" title="Answer · cited finding">
          <UserTurn content="What BE study design is recommended for albuterol sulfate inhalation aerosol?" live />
          <AssistantTurn turn={ANSWER} sessionId="s_fixture" onPick={noop} onCite={noop} busy={false} threshold={0.3} />
        </Section>

        <Section no="F2b" title="Answer · bibliography markers resolved to stamps">
          <UserTurn content="What BE study design is recommended for albuterol sulfate inhalation aerosol?" live />
          <AssistantTurn turn={BIBLIO} sessionId="s_fixture" onPick={noop} onCite={noop} busy={false} threshold={0.3} />
        </Section>

        <Section no="F3" title="Helpful refusal · not in corpus">
          <UserTurn content="What BE study design is recommended for atorvastatin oral tablets?" live />
          <AssistantTurn turn={REFUSAL} sessionId="s_fixture" onPick={noop} onCite={noop} busy={false} threshold={0.3} />
        </Section>

        <Section no="F4" title="Clarify · options + free-text reply">
          <UserTurn content="propranolol" live />
          <AssistantTurn turn={CLARIFY} sessionId="s_fixture" onPick={noop} onCite={noop} busy={false} threshold={0.3} />
        </Section>

        <Section no="F5" title="Scope warning · declined">
          <UserTurn content="Which BE pathway should we pick for our ANDA?" live />
          <AssistantTurn turn={SCOPE} sessionId="s_fixture" onPick={noop} onCite={noop} busy={false} threshold={0.3} />
        </Section>

        <Section no="F6" title="Assemble · intake — the compilation order">
          <div className="mt-4">
            <IntakeDemo />
          </div>
        </Section>

        <Section no="F7" title="Assemble · contents plan (idle + compiling)">
          <DossierPlan compiling />
        </Section>

        <Section no="F8" title="Assemble · the bound dossier">
          <div className="mt-4">
            <DossierView markdown={DOSSIER_MD} />
          </div>
        </Section>

        <Section no="F9" title="Watch · bulletin entries (new / revised / sparse)">
          <div className="doc bulletin">
            {FIXTURE_ALERTS.map((a, i) => (
              <AlertEntry
                key={a.psg_document_id}
                alert={a}
                scopeable
                scoped={i === 1}
                onScope={noop}
              />
            ))}
          </div>
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
