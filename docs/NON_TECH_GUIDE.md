# REGWATCH Non-Technical Guide

Last updated: 2026-08-13

Plain English, for Clinical Regulatory Affairs, business stakeholders, managers,
and reviewers who do not need to read code.

## What REGWATCH Is

REGWATCH is a research assistant for the public FDA drug databases. Its job is
to help a Clinical Regulatory Affairs team find, organize, and cite FDA
information faster.

It does not replace a regulatory professional, make regulatory decisions, or
write FDA submissions. It helps people research FDA sources and see exactly
where each answer came from.

## The Problem It Helps With

Regulatory teams answer questions like:

- Is there an FDA Product-Specific Guidance for this product?
- What bioequivalence studies does the guidance describe?
- What is the RLD or reference standard?
- Has the FDA guidance changed?
- Is the product listed in an FDA database?
- Where did this information come from?

Without a tool that means checking several FDA websites by hand, opening PDFs,
searching pages, comparing records, and copying source links. REGWATCH is meant
to cut that time down.

## What It Does Today

REGWATCH is deployed and running on approved hosting, with connections
encrypted in transit. Some finishing work remains before it is opened up more
widely (see "What Is Still Missing"), but the core research features are built,
tested, and live.

Today it can:

- Crawl and store FDA Product-Specific Guidances from the A-Z letter listings,
  not just the handful the FDA index page shows at a glance.
- Parse PSG PDFs page by page and split the text into searchable pieces.
- Store those pieces so they can be searched by meaning, not just by keyword.
- Discover and version the approved FDA source universe: Drugs@FDA, its
  approval/action packages, Product-Specific Guidances, general FDA
  bioequivalence guidance, and the Orange Book.
- Answer questions in a chat, with FDA citations attached.
- Work out which exact product you mean before answering, and ask you which one
  when the name is ambiguous.
- Build a product dossier ("Assemble") from stored FDA data.
- Build a multi-source White Paper for a product, with every cell traced to its
  FDA source, and export it to Word (.docx).
- Track a product watchlist, match FDA PSG changes against it, and write an
  alert digest.
- Require sign-in. Every user has their own account, chat history, and rate
  limits.
- Log every question and answer for auditability.

The currently serving PSG corpus held 5,494 searchable text pieces when it was
measured on 2026-08-11. A replacement FDA-only corpus is building but is not yet
activated. Its frozen production manifest contains 140,438 source records;
those are source records, not chunks or embeddings. Every record must end as a
searchable indexed document or a narrowly evidenced terminal outcome, and every
searchable chunk must be embedded before cutover. Final totals will be reported
only after full acceptance.

## The FDA Sources

REGWATCH now has one exact five-family source policy. Anything outside it is
rejected instead of used as a quiet fallback.

| FDA source | What people use it for | How REGWATCH uses it |
|---|---|---|
| Orange Book | RLD, reference standard, TE codes, patents, exclusivity | Structured data, looked up |
| Product-Specific Guidances | Product-specific bioequivalence guidance | PDFs parsed, text searched, pages cited |
| Drugs@FDA | Applications, products, sponsors, approval history, approved labels and letters | Official snapshot rows plus FDA documents |
| SBOA / action packages | Clinical, statistical, clinical-pharmacology, quality, integrated and multidisciplinary reviews | FDA review documents parsed and cited |
| FDA BE guidance | General bioequivalence guidance | Reviewed FDA guidance PDFs parsed and cited |

Not every source is represented the same way. Structured FDA snapshot rows are
stored as citable records; FDA PDFs and pages are parsed into page-aware
passages. Every record keeps its exact source family, document type, source URL,
version, and locator.

## How The Assistant Answers

This changed in August 2026, and it is worth understanding, because the old
behaviour was noticeably more robotic.

The rule used to be "cite every sentence or refuse". It made the assistant read
like a machine, and when it had nothing useful it produced a stiff canned
refusal.

The rule now is **cite the facts, talk like a person.**

Every sentence the assistant writes falls into one of three buckets, and it
labels itself by how it is written:

1. **An FDA fact.** Anything it tells you that FDA guidance requires,
   recommends, permits, or prohibits carries a citation right there in the
   sentence. No citation, no claim: the system removes an FDA fact that arrives
   without a source before you ever see it.
2. **Its own reading.** Sometimes the useful thing to say goes a step past what
   the documents literally say. The assistant is allowed to do that, but it has
   to flag it, and it always opens with a phrase like "My reading is ..." or
   "The guidance does not state this directly; my reading is ...". So you can
   always tell an FDA fact from an interpretation at a glance. It is also not
   allowed to slip a requirement or a prohibition into one of those sentences.
   If it says what is required, it counts as an FDA fact and needs a citation.
3. **Normal conversation.** Greetings, an offer to look at something else, a
   question back to you. No citations, because there is nothing to cite.

**When it does not have the answer, it says so like a person would.** There is
no code word and no canned refusal any more. It tells you plainly that the
guidance it retrieved does not cover the question, names what it does have on
the topic, and suggests where to go next.

The underlying safety rule has not loosened at all. An FDA fact without a source
is still dropped. What changed is the tone around it.

## What A Session Feels Like

A regulatory user asks:

> What BE studies does FDA recommend for albuterol sulfate inhalation aerosol?

The system:

1. Recognizes this is a PSG bioequivalence question.
2. Resolves the product to albuterol sulfate inhalation aerosol.
3. Searches only that product's guidance material.
4. Writes an answer with citations in the sentences.
5. Lists the FDA sources and page numbers underneath.

The answer reads roughly like:

> A single-dose fasting study is recommended [PSG_020503, p.2]. My reading is
> that this is the study most reviewers will expect to see first. Want me to
> pull the fed study conditions too?

Then a source list:

- PSG_020503, page 2, with a link to the FDA source

## Why Citations Matter

This is regulatory research. A confident answer with no source is not
acceptable.

Every FDA fact should be traceable to the source name, the document or record,
the page number when the source is a PDF, the source URL, and when the source
was captured.

If REGWATCH cannot support a fact with FDA evidence, it does not state it. That
is a safety feature, not a failure.

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

It is for research and organization, not decision-making.

## Why It Sometimes Asks Which Product You Mean

Different FDA guidances share a lot of the same language. Two unrelated PSGs may
both mention single actuation, crossover design, fasting study, and
bioequivalence.

If the system searched every PSG at once it could find the right phrase in the
wrong product's guidance. So it pins down the exact product first. If your
wording could mean more than one product, it asks:

> Which product do you mean?

and shows you the choices. That is better than quietly answering from the wrong
FDA document.

## The Safety Rules

The code enforces these as hard rules with automated tests behind them, not as
written guidance.

| Rule | Plain-English meaning |
|---|---|
| INV-1 Grounding | Every FDA fact needs a source and page |
| INV-2 Refuse over guess | If the evidence is weak, do not guess |
| INV-3 Operational only | Do not write submissions or make regulatory judgments |
| INV-4 No fabricated execution | Do not report work that did not actually happen |
| INV-5 Verified provenance | Product facts must come from verified sources |
| INV-6 Auditability | Log every query and answer path |

The White Paper adds three more (INV-7, INV-8, INV-9). They stop one product's
facts from leaking into another product's white paper and collapse any cell
whose citation does not actually back it.

## What The Watch Feature Does

The watch feature helps the team notice relevant FDA guidance changes.

1. REGWATCH holds a verified product watchlist.
2. It crawls FDA PSG listings on a daily schedule.
3. It checks which FDA PSG records match watchlist products.
4. It only raises an alert for a PSG version it actually fetched and stored.
5. It writes a digest the UI or API can show.

That last point is the important one. It never says "there was a change" unless
it has the underlying FDA record in hand.

## What The Dossier Feature Does

The dossier builds a research brief for one product: matched PSGs, extracted
bioequivalence requirements, cited PSG fields, RLD label information from
approved Drugs@FDA labeling, a cited Q&A summary, and a checklist scaffold.

The checklist is not a claim about what the company has done. It only organizes
what the FDA source material appears to call for.

## What The White Paper Feature Does

The White Paper builds a structured product brief from approved Drugs@FDA
labeling and metadata, Orange Book records, PSGs, action-package evidence, and
FDA BE guidance. A field that would need a source outside that boundary stays
for analyst input.

Each cell is one of three things:

- **Populated**: filled from an FDA source, with the citation attached.
- **"No"**: the system checked and the source confirms the answer is absent.
- **Analyst input required**: it could not confirm this from FDA evidence, so it
  leaves the cell for a person.

Some cells are analyst-authored on purpose and the system never writes them. The
finished white paper exports to a Word file that matches exactly what was
reviewed on screen, and it records when each FDA source was last fetched.

## What The Audit Log Does

Every question and answer is logged: the question, the evidence retrieved, the
answer text, the citations, whether the system declined, the model name, and the
chat session and turn it belonged to.

This matters because a reviewer will eventually ask "why did the system say
this?", and the log is how you reconstruct it.

## How The Web App Is Organized

The web app puts five surfaces (Ask, Assemble, Watch, White Paper, and
Deficiency) inside one shell with a single sidebar and a shared design. A sixth,
the Compliance Studio, opens on its own full screen and is described below.

A few things to know as a user:

- **Ask is a chat.** You type a question and get a cited answer back, with the
  FDA sources shown as clickable chips. If the product is ambiguous it offers
  clarify options to pick from. You see the answer being typed out live as a
  draft, but the final cited version only appears once its citations have been
  checked against the FDA sources.
- **There is an "Under review" bar at the top of every surface.** It shows which
  product the whole app is focused on right now, and you can change it there.
- **The focus is shareable.** It lives in the page address, so a link you send a
  colleague opens on the same product. You can also set the focus from a
  finished White Paper or from a Watch row.
- **The product picker will not guess.** If your wording cannot be matched to a
  single FDA application, it declines instead of picking one.

### The Compliance Studio

Everything above reads public FDA material. The Compliance Studio is the one
place that reads our own draft documents instead.

It looks like a document editor. Your documents are on the left, the one you are
reading fills the middle, and two panels slide in from the right: the compliance
findings, and an assistant you can ask about any passage you select.

What a reviewer does there:

1. Open a CMC document and read it, or select a passage and ask the assistant to
   summarize or explain it.
2. Run the compliance check. Findings come back attached to the exact sentence
   that triggered them, highlighted in the text.
3. Fix each one and record what you decided: Fixed, Fixed elsewhere, Not
   applicable, or Disputed. The last three ask you to write down why.
4. Copy the resulting record out to paste into the comment-resolution log.

Two things are deliberate:

- **You cannot mark something "Fixed" until you have actually changed the text
  it points at.** If you have not edited it, the button explains why and offers
  "Fixed elsewhere" instead, which asks where the fix landed. The record cannot
  claim a fix that never happened.
- **What you record here is a working note, not a controlled record.** There is
  no electronic signature behind it and the timestamp comes from your own
  computer. The panel says so and the exported record repeats it. It is meant to
  be copied into a system that is controlled.

**It is a working prototype.** The documents in it are samples, the compliance
check is a stand-in rather than a real analysis, and nothing is saved. Close the
tab and your work is gone. It exists so we can agree on how the tool should feel
before the machinery behind it is built.

## Where The Data Goes

This was the biggest open question on the project and it is now settled.

All three pieces that touch an analyst's question run inside the company's own
Databricks environment:

- The model that writes the answer.
- The model that matches a question to the right FDA passages.
- The database that stores everything.

So a normal question does not leave the company boundary to get answered. The
outside vendor is kept configured only as a fallback if we ever need to switch
back, and it serves nothing today.

## What Is Still Missing

REGWATCH is deployed and running, but some work remains before it opens up more
widely. The full list lives in `docs/ROADMAP.md`. The headline items:

- **Single sign-on.** It runs on approved hosting with encrypted connections,
  but it is not connected to the company's SSO yet, so it is not exposed
  externally.
- **A rehearsed restore exercise** that proves backups can actually be brought
  back.
- **Tighter database credentials** for the application account.
- **Alerting on the daily watcher.** The scheduled job runs, but nothing raises
  an alarm yet if a run fails. There is also a known configuration gap: the
  scheduled job has not been given the settings for the new in-house matching
  model, so the first time a real FDA revision lands it could store material
  that the search index cannot see. Setting those values fixes it.
- **Re-tuning the confidence cut-off.** There is a score below which the system
  will not use a retrieved passage. That number was tuned against the previous
  matching model. The matching model has since changed, so the number needs
  checking again.
- **Human review is still required.** Nothing here is a substitute for a
  regulatory professional reading the source.

## The Architecture, In One Pass

1. A web UI for users.
2. A gatekeeper service that guards the door: it checks logins, manages
   sessions, applies rate limits, and keeps the record of every question asked.
3. An answering engine for FDA evidence, retrieval, citations, and AI.
4. A router that decides which FDA source should answer.
5. A handler for each FDA database.
6. A synthesizer that writes the cited answer.
7. Validation before anything reaches a user.
8. Audit logs for every decision.

The language model and the matching model are both pluggable. No specific model
name is baked into the logic. That is what made the move into the company's own
environment possible without rewriting how the system works.

The goal is not a chatbot that guesses. It is a research system that knows where
to look and shows you the source.

## One-Sentence Summary

REGWATCH helps Clinical Regulatory Affairs teams research public FDA sources
faster: it finds the relevant FDA evidence, organizes it by product, and gives
cited answers, in plain language, without claiming anything it cannot back up.
