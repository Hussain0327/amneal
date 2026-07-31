"""Deficiency analysis: upload a CMC submission PDF, surface candidate deficiencies.

Vendored from DevDesai444/deficiency-chatbot (commit bdad5c5, "deterministic-first
fault detection") and rewired onto regwatch's seams:

* LLM calls    -> regwatch.generate.llm providers via regwatch.deficiency.structured
                  (inherits the D1 residency guard; no direct SDK usage)
* precedents   -> regwatch.store.deficiency_kb (pgvector) via
                  regwatch.deficiency.precedents
* job state    -> regwatch.store.deficiency_runs (Postgres, migration 0019)
* progress     -> regwatch.deficiency.events (log-only; upstream's WebSocket
                  event bus was dropped for the MVP)

Policy basis: DECISIONS.md 2026-07-30 (deficiency surfacing + cited historical
precedents; public data only).
"""
