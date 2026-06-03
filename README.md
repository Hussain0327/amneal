# REGWATCH

A regulatory prep accelerator for a generic-drug Clinical Regulatory Affairs team.

REGWATCH watches FDA Product-Specific Guidances (PSGs), matches changes
against a company's product pipeline, extracts cited bioequivalence
requirements, and answers questions over the FDA guidance corpus — every
answer carrying its source and page, or an explicit "not found."

**This is a POC, not a production system.** It surfaces, organizes,
compares, and cites public FDA information. It never authors submission
content, renders regulatory judgment, or takes autonomous action.
See [`CLAUDE.md`](./CLAUDE.md) for the operating rules and the spec's
Section 4 for the compliance invariants (INV-1..INV-6).

## Quick start

```bash
# Python 3.11+ and uv (https://docs.astral.sh/uv)
uv sync --extra dev

# Copy env template, then edit
cp .env.example .env

# Create DB + data dirs
uv run regwatch init-db

# Sanity check
uv run regwatch status

# Tests (smoke + invariants)
uv run pytest -q
```

## Build phases

REGWATCH is built phase by phase. After each phase, the test suite and the
phase's Definition of Done (DoD) must pass before moving on.

| Phase | Outcome | DoD |
|---|---|---|
| 0 | Scaffold | `uv run pytest` green; app boots |
| 1 | Ingest + extract (seed) | Three seed products in DB with cited BE fields; idempotent re-run |
| 2 | Retrieval + cited Q&A | INV-1, INV-2, INV-6 tests pass incl. adversarial refusals |
| 3 | Watch | Pasted batch flags albuterol + beclomethasone with cited diffs; INV-4/5 pass |
| 4 | Assemble | Seed product yields a fully cited dossier via `/assemble` |
| 5 | Eval + polish | recall@8 ≥ 0.90, citation precision ≥ 0.95, refusal accuracy ≥ 0.95 |

## Layout

See `pyproject.toml` and `src/regwatch/`. Top-level packages:

- `ingest/`  — crawlers + PDF parser
- `process/` — chunker, embedder, extractor, change detector
- `store/`   — Chroma + SQLite
- `retrieve/`— retrieval + reranker
- `generate/`— LLM provider + grounded Q&A
- `watch/`   — watchlist + matcher + alerts
- `assemble/`— dossier builder
- `eval/`    — gold set + metrics + scorecard
- `api/`     — FastAPI surface
- `ui/`      — Streamlit pages
- `common/`  — logging, audit, normalization

## Public data only

REGWATCH pulls from these public FDA endpoints:

- PSG database (scrape) — `accessdata.fda.gov/scripts/cder/psg/`
- Drugs@FDA (API) — `api.fda.gov/drug/drugsfda.json`
- Drug labeling SPL (API) — `api.fda.gov/drug/label.json`
- Orange Book (download)
- Dissolution Methods DB (scrape)

No internal SOPs, pipelines, or submission drafts are touched.
