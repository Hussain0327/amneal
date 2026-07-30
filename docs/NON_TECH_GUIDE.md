# REGWATCH Non-Technical Guide

This guide explains REGWATCH in plain English for Clinical Regulatory Affairs,
business stakeholders, managers, and reviewers who do not need to read code.

## What REGWATCH Is

REGWATCH is a research assistant for FDA public drug databases.

Its job is to help a Clinical Regulatory Affairs team find, organize, and cite
FDA information faster.

It does not replace a regulatory professional. It does not make regulatory
decisions. It does not write FDA submissions. It helps people research FDA
sources and see exactly where each answer came from.

## The Problem It Helps With

Clinical Regulatory Affairs teams often need to answer questions like:

- Is there an FDA Product-Specific Guidance for this product?
- What bioequivalence studies does the guidance describe?
- What is the RLD or reference standard?
- Has the FDA guidance changed?
- Is the product listed in an FDA database?
- Are there related FDA records that need to be checked?
- Where did this information come from?

Without a tool, this means manually checking multiple FDA websites, opening
PDFs, searching pages, comparing records, and copying source links.

REGWATCH is meant to reduce that research time.

## What It Does Today

REGWATCH is a working system, not just a sketch. It is deployed and running on
approved hosting, with connections encrypted in transit. Some finishing work
remains before it is opened up more widely (see "Important Current
Limitations"), but the core research features are built, tested, and live.

Today, it can:

- Download and store the full FDA PSG catalog — roughly 1,795 Product-Specific
  Guidances, pulled across the A-Z letter listings (not just the ~70 the FDA
  index page shows at a glance).
- Parse PSG PDFs page by page.
- Split the PDF text into searchable pieces.
- Store those searchable pieces in a vector database.
- Ingest and store the other FDA sources as structured records: Orange Book
  (products, patents, exclusivity), Drugs@FDA, the NDC Directory, DailyMed SPL
  labels, Drug Shortages, and REMS.
- Ask questions over the FDA corpus through a cited conversational chat.
- Return cited answers with page references and links to the FDA source.
- Refuse to answer when it cannot find support in the FDA source text.
- Resolve a question to one exact product before answering, and ask for
  clarification when the product is ambiguous.
- Build a product dossier ("Assemble") from stored FDA data.
- Build a multi-source White Paper for a product, with every cell traced to its
  FDA source, and export it to a Word (.docx) file.
- Track a product watchlist.
- Match FDA PSG changes against watchlist products and write an alert digest.
- Require sign-in: every user has their own account, their own chat history, and
  their own rate limits.
- Log every Q&A interaction for auditability.

## The Six FDA Source Areas

Your manager provided six FDA database areas:

1. Orange Book
2. Product-Specific Guidances
3. Drugs@FDA
4. Drug Shortages
5. NDC Directory
6. REMS

REGWATCH treats each of these as a first-class FDA source today (DailyMed SPL
labels are also ingested as a seventh source).

The important point: not every source is handled the same way.

Product-Specific Guidances are PDFs, so they get text search and citations.
Orange Book, NDC, shortages, Drugs@FDA, DailyMed, and REMS are handled as
structured records, like database rows.

## The Six FDA Sources In Plain English

| FDA source | What people use it for | How REGWATCH uses it |
|---|---|---|
| Orange Book | RLD, reference standard, TE codes, patents, exclusivity | Stored as structured data and looked up |
| Product-Specific Guidances | FDA product-specific bioequivalence guidance | PDFs parsed, text searched, pages cited |
| Drugs@FDA | Applications, sponsors, approval history, labels | Stored as structured data and looked up |
| Drug Shortages | Current shortage status | Stored as structured data and refreshed |
| NDC Directory | NDC product/package information | Stored as structured data and looked up |
| REMS | REMS programs and requirements | Stored as structured data with source cited |
| DailyMed SPL | Structured product labels for the RLD | Stored as structured data and cited |

## What A User Experience Should Feel Like

A regulatory user should be able to ask:

> What BE studies does FDA recommend for albuterol sulfate inhalation aerosol?

The system should:

1. Understand the user is asking about PSG bioequivalence guidance.
2. Resolve the product as albuterol sulfate inhalation aerosol.
3. Search only the relevant PSG material for that product.
4. Produce an answer with inline citations.
5. Show the FDA source and page number.

The answer should look conceptually like:

> The PSG describes the recommended bioequivalence study design for this
> product and cites the relevant page in the FDA guidance. [PSG_020503, p.2]

Then it should show a source list:

- PSG_020503, page 2, FDA source link

## Why Citations Matter

REGWATCH is for regulatory research. A confident answer without a source is not
acceptable.

Every factual answer should be traceable to:

- FDA source name
- document or record
- page number when the source is a PDF
- source URL
- time the source was captured

If REGWATCH cannot support an answer with FDA evidence, it should refuse.

That refusal is not a failure. In this domain, refusal is a safety feature.

## What It Must Not Do

REGWATCH must not:

- Write FDA submission content.
- Recommend a regulatory strategy.
- Decide what study the company should run.
- Invent missing facts.
- Use model memory as a source.
- Claim it checked a database if it did not.
- Send anything to FDA.
- Take autonomous regulatory action.

The system is for research and organization, not decision-making.

## Why The System Sometimes Asks For Clarification

FDA drug names can be confusing. Different products can have similar language in
their guidances.

For example, two different PSGs may both mention:

- single actuation
- crossover design
- fasting study
- bioequivalence

If the system searches all PSGs at once, it might find the right phrase in the
wrong product's guidance.

To prevent that, REGWATCH should first identify the exact product. If the user
is unclear, it should ask:

> Which product do you mean?

Then it should show choices.

This is better than silently answering from the wrong FDA document.

## How REGWATCH Thinks About A Question

In simple terms, REGWATCH should follow this flow:

1. What is the user asking about?
2. Which FDA source should answer this?
3. Which product or application is the user asking about?
4. Do we have trusted FDA evidence?
5. Can we answer with citations?
6. If yes, answer with sources.
7. If no, ask for clarification or refuse.

## The Current Safety Rules

The code has compliance invariants. These are hard rules, not suggestions.

| Rule | Plain-English meaning |
|---|---|
| INV-1 Grounding | Every factual claim needs a source and page |
| INV-2 Refuse over guess | If evidence is weak, do not guess |
| INV-3 Operational only | Do not write submissions or make regulatory judgments |
| INV-4 No fabricated execution | Do not report work that did not actually happen |
| INV-5 Verified provenance | Product facts must come from verified sources |
| INV-6 Auditability | Log every query and answer path |

The White Paper feature adds three more guards (INV-7, INV-8, INV-9) that stop
one product's facts from leaking into another product's white paper and collapse
any cell whose citation does not actually back it. All of these rules are
enforced as automated tests, not just written down.

## What The Watch Feature Does

The watch feature is meant to help the team notice relevant FDA guidance
changes.

It works like this:

1. REGWATCH has a verified product watchlist.
2. It crawls FDA PSG listings.
3. It checks which FDA PSG records match watchlist products.
4. It only emits alerts for PSG versions that were actually fetched and stored.
5. It writes a local digest that the UI or API can show.

This avoids saying "there was a change" unless the system actually has the
underlying FDA record.

## What The Dossier Feature Does

The dossier feature builds a research brief for a product.

It can include:

- matched PSGs
- extracted bioequivalence requirements
- cited PSG fields
- RLD label information from openFDA when available
- a PSG-based Q&A summary
- a checklist scaffold

The checklist is not saying what the company has done. It is only organizing
what FDA source material appears to call for.

## What The White Paper Feature Does

The White Paper feature builds a structured product brief that pulls from all of
the FDA sources at once — Orange Book, Drugs@FDA, NDC, DailyMed, Drug Shortages,
REMS, and the PSGs.

Each cell in the white paper is one of three things:

- Populated — filled from an FDA source, with the citation attached.
- "No" — the system checked and the source confirms the answer is absent.
- Analyst input required — the system could not confirm it from FDA evidence, so
  it leaves the cell for a human to fill.

Some cells are marked as analyst-authored on purpose. The system never writes
those; only a person does. The finished white paper can be exported to a Word
(.docx) file that matches exactly what was reviewed on screen, and the FDA
source freshness (when each source was last fetched) is recorded with it.

## What The Audit Log Does

Every Q&A interaction is logged.

The log records:

- question
- retrieved evidence
- answer text
- citations
- whether the system refused
- model name
- the chat session and turn it belonged to, and the response type
  (answer / summary / clarify / scope warning / refused)

This matters because a reviewer may ask:

> Why did the system say this?

The audit log helps reconstruct the answer.

## What Docker Adds

Docker is packaging for the application.

It lets the same code run in a predictable container instead of depending on a
developer's laptop setup.

The current Docker setup can run:

- the API
- a separate ingest job for loading FDA data

(The web UI is a separate Next.js app under `regwatch/frontend/` and is run on
its own.) This same packaging is what the deployed service runs on today, so
the code behaves the same way in production as it does in testing.

## How The Web App Is Organized

The web app (built with Next.js) puts all four surfaces — Ask, Assemble, Watch,
and White Paper — inside one shell, with a single sidebar and a shared design.

A few things to know as a user:

- Ask is a chat. You type a question and get a cited answer back, with the FDA
  sources shown as clickable chips; if the product is ambiguous, it offers you
  clarify options to pick from. The answer is shown being typed out live as a
  draft, but the final cited answer only appears once its citations have been
  checked against the FDA sources.
- At the top of every surface there is an "Under review" product-scope bar. It
  shows which product the whole app is currently focused on, and you can change
  the focus there with a product picker.
- The product focus is shareable: it lives in the page address, so a link you
  send to a colleague opens on the same product. You can also set the focus from
  the White Paper (after a successful build) or from a Watch row.
- The picker is backed by a deterministic resolver. If your product can't be
  matched to a single FDA application, it declines rather than guessing.

## Important Current Limitations

REGWATCH is deployed and running, but some finishing work remains before it is
opened up more widely. The consolidated list of remaining work lives in
`docs/ROADMAP.md`. The headline items:

- It runs on approved hosting with encrypted connections. What remains is
  connecting it to the company's single sign-on, and a rehearsed exercise that
  proves backups can actually be restored.
- The database move is done: one managed database now holds everything the
  system stores. The only remaining piece there is that restore exercise.
- The data-handling decision was made on 2026-07-28: the AI model that writes
  answers now runs inside the company's own Databricks environment, so analyst
  questions no longer leave the company boundary to get answered. One piece
  still uses OpenAI: the "matching" step that finds the right FDA passages for
  a question. Its in-company replacement is already set up in Databricks and
  is waiting to be connected.
- The daily watcher runs as a scheduled production job (on GitHub's
  scheduler). What is still missing is fuller monitoring that raises an alarm
  if a run fails.
- The evaluation gold set should grow (from 12 Q&A + 16 white-paper rows toward
  30-50).
- Human review is still required.

## The Architecture (Now Built)

The system is organized exactly the way it was originally recommended:

1. A TypeScript web UI for users (the Next.js app described above).
2. A gatekeeper service (written in Go) that guards the door: it checks
   logins, manages sessions, applies rate limits, and keeps the record of
   every question asked, standing in front of the answering engine.
3. A Python answering engine for FDA evidence, retrieval, citations, and AI.
4. A router that decides which FDA source should answer.
5. Source handlers for each FDA database.
6. A final answer synthesizer that writes cited answers.
7. Strong validation before anything is shown to users.
8. Audit logs for every decision.

The language model and embedding model are pluggable: no specific model name is
hard-coded into the logic. That is what made the recent swap possible: the
language model was switched to one running inside the company's own
environment (the data-handling decision described in the Limitations above),
without changing how the system works.

The goal is not to build a chatbot that guesses. The goal is to build a
research system that knows where to look and shows the source.

## One-Sentence Summary

REGWATCH helps Clinical Regulatory Affairs teams research public FDA sources
faster by finding relevant FDA evidence, organizing it by product, and producing
cited answers or refusing when the evidence is not there.
