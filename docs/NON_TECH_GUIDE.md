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

The current project is a proof of concept. It already focuses on FDA
Product-Specific Guidances, usually called PSGs.

Today, it can:

- Download and store PSG records from FDA.
- Parse PSG PDFs page by page.
- Split the PDF text into searchable pieces.
- Store those searchable pieces in a vector database.
- Ask questions over the PSG corpus.
- Return cited answers with page references.
- Refuse to answer when it cannot find support in the FDA source text.
- Build a simple product dossier from stored PSG data.
- Track a product watchlist.
- Match FDA PSG changes against watchlist products.
- Log every Q&A interaction for auditability.

## What It Should Eventually Cover

Your manager provided six FDA database areas:

1. Orange Book
2. Product-Specific Guidances
3. Drugs@FDA
4. Drug Shortages
5. NDC Directory
6. REMS

REGWATCH should eventually treat each of these as a first-class FDA source.

The important point: not every source should be handled the same way.

Product-Specific Guidances are PDFs, so they need text search and citations.
Orange Book, NDC, shortages, Drugs@FDA, and REMS are better handled as
structured records, like database rows.

## The Six FDA Sources In Plain English

| FDA source | What people use it for | How REGWATCH should use it |
|---|---|---|
| Orange Book | RLD, reference standard, TE codes, patents, exclusivity | Store as structured data and look it up |
| Product-Specific Guidances | FDA product-specific bioequivalence guidance | Parse PDFs, search text, cite pages |
| Drugs@FDA | Applications, sponsors, approval history, labels | Store as structured data and look it up |
| Drug Shortages | Current shortage status | Store as structured data and refresh often |
| NDC Directory | NDC product/package information | Store as structured data and look it up |
| REMS | REMS programs and requirements | Store as structured data and cite the source |

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

## What The Audit Log Does

Every Q&A interaction is logged.

The log records:

- question
- retrieved evidence
- answer text
- citations
- whether the system refused
- model name

This matters because a reviewer may ask:

> Why did the system say this?

The audit log helps reconstruct the answer.

## What Docker Adds

Docker is packaging for the application.

It lets the same code run in a predictable container instead of depending on a
developer's laptop setup.

The current Docker setup can run:

- the API
- the temporary Streamlit UI
- a separate ingest job for loading FDA data

This is useful groundwork, but it is not the same as a full production
deployment. Production still needs security, hosting, backups, monitoring, and
an approved way to manage secrets.

## Important Current Limitations

This is still a proof of concept.

Important limitations:

- It has a Docker/container baseline, but it is not a full production deployment.
- The UI is currently Streamlit, which is fine for a demo but not ideal for production.
- Most current work focuses on PSGs.
- Other FDA databases still need stronger structured loaders and handlers.
- The model provider supports OpenAI Responses API, but the final in-house or
  enterprise model target still needs a production decision.
- More evaluation examples are needed before production use.
- Human review is still required.

## Future Direction

The recommended future architecture is:

1. A TypeScript web UI for users.
2. A Python backend for FDA evidence, retrieval, citations, and AI.
3. A router that decides which FDA source should answer.
4. Source handlers for each FDA database.
5. A final answer synthesizer that writes cited answers.
6. Strong validation before anything is shown to users.
7. Audit logs for every decision.

The goal is not to build a chatbot that guesses. The goal is to build a
research system that knows where to look and shows the source.

## One-Sentence Summary

REGWATCH helps Clinical Regulatory Affairs teams research public FDA sources
faster by finding relevant FDA evidence, organizing it by product, and producing
cited answers or refusing when the evidence is not there.
