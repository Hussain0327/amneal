"""Streamlit POC UI.

Three pages: Ask, Assemble, Watch. Every source is clickable. No command line.
Run with:
    uv run streamlit run src/regwatch/ui/app.py
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import streamlit as st
from config.settings import get_settings

from regwatch.assemble.dossier import build_dossier
from regwatch.generate.grounded_qa import ask
from regwatch.store.db import init_db
from regwatch.watch.alerts import latest_digest_records
from regwatch.watch.watchlist import list_watchlist

st.set_page_config(page_title="REGWATCH", layout="wide")


# region agent log
_AGENT_DEBUG_LOG_PATH = Path("/Users/hussain/amneal/.cursor/debug-04b0f1.log")


def _agent_debug_log(
    *,
    run_id: str,
    hypothesis_id: str,
    location: str,
    message: str,
    data: dict[str, object],
) -> None:
    payload = {
        "sessionId": "04b0f1",
        "runId": run_id,
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data,
        "timestamp": int(time.time() * 1000),
    }
    try:
        _AGENT_DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _AGENT_DEBUG_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, sort_keys=True) + "\n")
    except Exception:
        pass


# endregion


@st.cache_resource
def _init_db_once() -> None:
    if os.getenv("REGWATCH_DB_INITIALIZED") != "1":
        init_db()


_init_db_once()


def render_sidebar() -> str:
    s = get_settings()
    st.sidebar.title("REGWATCH")
    st.sidebar.caption(
        "Operational POC. Public FDA data only. Surfaces and cites; never authors submissions."
    )
    st.sidebar.markdown("---")
    st.sidebar.markdown(f"**Embedding:** `{s.embedding_provider}`")
    st.sidebar.markdown(f"**LLM:** `{s.llm_provider}` / `{s.llm_model}`")
    page = st.sidebar.radio("Page", ["Ask", "Assemble", "Watch"])
    return page


def render_ask() -> None:
    st.header("Ask")
    st.caption(
        "Plain-language Q&A over the FDA guidance corpus. Every claim is cited. "
        "Type a drug name or a question — it'll guide you if it needs more."
    )

    # The active query lives in session_state so clarify buttons (which trigger
    # their own rerun) can update it without the form being re-submitted.
    with st.form("ask_form"):
        typed = st.text_area(
            "Question",
            value=st.session_state.get("ask_query", ""),
            placeholder="propranolol   ·   What BE study design is recommended for metformin?",
        )
        c1, c2 = st.columns(2)
        with c1:
            ingredient_filter = st.text_input("Filter: active ingredient (optional)", "")
        with c2:
            dosage_filter = st.text_input("Filter: dosage form (optional)", "")
        submitted = st.form_submit_button("Ask")

    # region agent log
    _agent_debug_log(
        run_id="initial",
        hypothesis_id="H5",
        location="src/regwatch/ui/app.py:82",
        message="ask_form_submit_state",
        data={
            "submitted": bool(submitted),
            "typed_blank": not bool(typed.strip()),
            "had_previous_query": bool(st.session_state.get("ask_query")),
            "ingredient_filter_set": bool(ingredient_filter.strip()),
            "dosage_filter_set": bool(dosage_filter.strip()),
        },
    )
    # endregion

    if submitted and typed.strip():
        filters: dict[str, str] = {}
        if ingredient_filter.strip():
            filters["normalized_name"] = ingredient_filter.strip().lower()
        if dosage_filter.strip():
            filters["dosage_form"] = dosage_filter.strip()
        st.session_state["ask_query"] = typed.strip()
        st.session_state["ask_filters"] = filters
    elif submitted:
        st.warning("Enter a question.")

    query = st.session_state.get("ask_query")
    if not query:
        return
    filters = st.session_state.get("ask_filters") or {}
    # region agent log
    _agent_debug_log(
        run_id="initial",
        hypothesis_id="H5",
        location="src/regwatch/ui/app.py:104",
        message="ask_active_query_state",
        data={
            "submitted": bool(submitted),
            "typed_blank": not bool(typed.strip()),
            "query_present": bool(query),
            "filters_keys": sorted(filters.keys()),
            "reusing_previous_after_empty_submit": bool(submitted and not typed.strip() and query),
        },
    )
    # endregion

    with st.spinner("Thinking..."):
        result = ask(query, filters=filters or None)

    if result.status == "clarify":
        st.info(result.interpretation or result.answer)
        for i, opt in enumerate(result.clarify):
            # Key on the option target so clicks never collide across clarifies.
            if st.button(opt.label, key=f"opt::{query}::{i}", use_container_width=True):
                st.session_state["ask_query"] = opt.query
                st.session_state["ask_filters"] = opt.filters or {}
                st.rerun()
        return

    if result.refused:
        st.warning(result.answer)
    else:
        st.markdown(result.answer)

    st.markdown("---")
    st.subheader("Sources")
    if not result.citations:
        st.info("No citations.")
    for c in result.citations:
        st.markdown(
            f"- **{c.short_name}**, p.{c.page} — [{c.source_url}]({c.source_url})\n"
            f"  > {c.snippet}"
        )
    with st.expander("Retrieval debug"):
        st.json(result.retrieved)


def render_assemble() -> None:
    st.header("Assemble")
    st.caption(
        "Build a cited dossier for a target product. "
        "This is a scaffold of what the FDA calls for, not what your team has done."
    )
    with st.form("assemble_form"):
        c1, c2, c3 = st.columns(3)
        with c1:
            active_ingredient = st.text_input("Active ingredient", "")
        with c2:
            dosage_form = st.text_input("Dosage form (optional)", "")
        with c3:
            rld = st.text_input("RLD (brand or application number, optional)", "")
        submitted = st.form_submit_button("Build dossier")
    if not submitted:
        return
    if not active_ingredient.strip():
        st.warning("Enter an active ingredient.")
        return

    with st.spinner("Assembling..."):
        dossier = build_dossier(
            active_ingredient=active_ingredient.strip(),
            dosage_form=dosage_form.strip() or None,
            rld=rld.strip() or None,
        )
    if dossier.get("refused"):
        st.warning(dossier["markdown"])
        return
    st.markdown(dossier["markdown"])
    with st.expander("Raw sections"):
        st.json(dossier["sections"])


def render_watch() -> None:
    st.header("Watch")
    st.caption("Recent alerts from the change feed. Watchlist drives what surfaces here.")
    records = latest_digest_records(limit=100)
    if not records:
        st.info(
            "No alerts yet. Run an ingest cycle and let the matcher build alerts against your watchlist."
        )
    else:
        for r in records:
            with st.container():
                st.markdown(
                    f"**{r.get('active_ingredient')}** — "
                    f"PSG {r.get('listing_appl_no')} "
                    f"({r.get('listing_psg_type')})"
                )
                if r.get("diff_summary"):
                    st.markdown(f"> {r['diff_summary']}")
                st.markdown(f"[Source]({r.get('source_url')})  •  confidence {r.get('confidence')}")
                st.markdown("---")

    st.subheader("Watchlist")
    items = list_watchlist()
    if items:
        st.dataframe(items, use_container_width=True)
    else:
        st.info("Watchlist is empty. Add products via the API or `regwatch watchlist add`.")


def main() -> None:
    page = render_sidebar()
    if page == "Ask":
        render_ask()
    elif page == "Assemble":
        render_assemble()
    else:
        render_watch()


main()
