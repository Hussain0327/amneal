"""Tier-1 graph derivation: nodes/edges/refs are deterministic, version-scoped
section nodes are GC'd on re-derivation, and refs die with their chunks."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from regwatch.store.db import init_db, session_scope
from regwatch.store.graph_store import (
    application_node_id,
    derive_document_graph,
    psg_doc_node_id,
)
from regwatch.store.vector_store import add_chunks

_DOC_ATTRS = {
    "appl_no": "020503",
    "normalized_name": "albuterol sulfate",
    "dosage_form": "aerosol metered",
    "route": "inhalation",
    "psg_type": "final",
}


def _seed_chunks(doc_id: int, version_id: int, sections: list[str | None]) -> list[str]:
    ids = [f"{doc_id}-{version_id}-{i}" for i in range(len(sections))]
    metas: list[dict[str, Any]] = [
        {
            "doc_id": doc_id,
            "version_id": version_id,
            "ordinal": i,
            "page": 1,
            "section_path": sec,
            "appl_no": _DOC_ATTRS["appl_no"],
            "normalized_name": _DOC_ATTRS["normalized_name"],
        }
        for i, sec in enumerate(sections)
    ]
    add_chunks(
        ids=ids,
        embeddings=[None] * len(ids),
        documents=[f"chunk body {i} " * 30 for i in range(len(ids))],
        metadatas=metas,
    )
    return ids


def _derive(doc_id: int, version_id: int, ids: list[str], sections: list[str | None]) -> None:
    metas = [{"ordinal": i, "section_path": sec} for i, sec in enumerate(sections)]
    with session_scope() as s:
        derive_document_graph(
            doc_id=doc_id,
            version_id=version_id,
            chunk_ids=ids,
            chunk_metas=metas,
            doc_attrs=dict(_DOC_ATTRS),
            conn=s.connection(),
        )


def _counts() -> dict[str, int]:
    with session_scope() as s:
        conn = s.connection()
        return {
            "nodes": conn.execute(text("SELECT count(*) FROM graph_node")).scalar_one(),
            "edges": conn.execute(text("SELECT count(*) FROM graph_edge")).scalar_one(),
            "refs": conn.execute(text("SELECT count(*) FROM graph_node_chunk")).scalar_one(),
        }


def test_derivation_builds_expected_nodes_edges_refs() -> None:
    init_db()
    sections: list[str | None] = [None, "I Introduction", "I Introduction", "II Methods"]
    ids = _seed_chunks(7, 9, sections)
    _derive(7, 9, ids, sections)

    with session_scope() as s:
        conn = s.connection()
        node_types = {
            str(r[0]): int(r[1])
            for r in conn.execute(
                text("SELECT node_type, count(*) FROM graph_node GROUP BY node_type")
            )
        }
        assert node_types == {"application": 1, "psg_doc": 1, "psg_section": 2}

        edge_types = {
            str(r[0]): int(r[1])
            for r in conn.execute(
                text("SELECT edge_type, count(*) FROM graph_edge GROUP BY edge_type")
            )
        }
        assert edge_types == {"HAS_PSG": 1, "HAS_SECTION": 2, "FOLLOWS": 1}

        # FOLLOWS respects reading order (min member ordinal: I=1 before II=3).
        follows = conn.execute(
            text(
                "SELECT sn.canonical_name, dn.canonical_name FROM graph_edge e "
                "JOIN graph_node sn ON sn.id = e.src_node_id "
                "JOIN graph_node dn ON dn.id = e.dst_node_id "
                "WHERE e.edge_type = 'FOLLOWS'"
            )
        ).one()
        assert follows == ("I Introduction", "II Methods")

        # Primary rule: lowest-ordinal chunk WITH a section (ordinal 1), for
        # both the doc node and the application node.
        for node_id in (psg_doc_node_id(7), application_node_id("020503")):
            primary = conn.execute(
                text(
                    "SELECT chunk_id FROM graph_node_chunk "
                    "WHERE node_id = :n AND ref_type = 'primary'"
                ),
                {"n": node_id},
            ).scalar_one()
            assert primary == ids[1]

        # Members: every chunk -> doc (4), sectioned chunks -> section (3).
        member_counts = {
            str(r[0]): int(r[1])
            for r in conn.execute(
                text(
                    "SELECT gn.node_type, count(*) FROM graph_node_chunk gnc "
                    "JOIN graph_node gn ON gn.id = gnc.node_id "
                    "WHERE gnc.ref_type = 'member' GROUP BY gn.node_type"
                )
            )
        }
        assert member_counts == {"psg_doc": 4, "psg_section": 3}


def test_rederivation_is_idempotent_and_gcs_old_section_generation() -> None:
    init_db()
    sections: list[str | None] = [None, "I Introduction", "II Methods"]
    ids = _seed_chunks(7, 9, sections)
    _derive(7, 9, ids, sections)
    first = _counts()

    # Same inputs -> identical rows (idempotence).
    _derive(7, 9, ids, sections)
    assert _counts() == first

    # A new version's sections REPLACE the old generation: node/edge counts
    # stay flat instead of accumulating one generation per FDA revision.
    ids_v10 = _seed_chunks(7, 10, ["I Revised intro", "II Methods", None])
    _derive(7, 10, ids_v10, ["I Revised intro", "II Methods", None])
    with session_scope() as s:
        keys = (
            s.connection()
            .execute(
                text(
                    "SELECT natural_key FROM graph_node "
                    "WHERE node_type = 'psg_section' ORDER BY natural_key"
                )
            )
            .scalars()
            .all()
        )
    assert len(keys) == 2
    assert all(key.startswith("7-10-") for key in keys)


def test_refs_cascade_when_chunk_is_deleted() -> None:
    init_db()
    sections: list[str | None] = ["I Introduction", "II Methods"]
    ids = _seed_chunks(7, 9, sections)
    _derive(7, 9, ids, sections)
    with session_scope() as s:
        conn = s.connection()
        before = conn.execute(
            text("SELECT count(*) FROM graph_node_chunk WHERE chunk_id = :c"),
            {"c": ids[0]},
        ).scalar_one()
        assert before > 0
        conn.execute(text("DELETE FROM chunk WHERE id = :c"), {"c": ids[0]})
        after = conn.execute(
            text("SELECT count(*) FROM graph_node_chunk WHERE chunk_id = :c"),
            {"c": ids[0]},
        ).scalar_one()
        assert after == 0
        # The graph nodes themselves survive; only the refs die with the chunk.
        assert conn.execute(text("SELECT count(*) FROM graph_node")).scalar_one() > 0


def test_doc_without_sections_gets_doc_and_application_rows_only() -> None:
    init_db()
    sections: list[str | None] = [None, None]
    ids = _seed_chunks(11, 3, sections)
    _derive(11, 3, ids, sections)
    with session_scope() as s:
        conn = s.connection()
        node_types = {
            str(r[0]): int(r[1])
            for r in conn.execute(
                text("SELECT node_type, count(*) FROM graph_node GROUP BY node_type")
            )
        }
        assert node_types == {"application": 1, "psg_doc": 1}
        assert (
            conn.execute(
                text("SELECT count(*) FROM graph_edge WHERE edge_type <> 'HAS_PSG'")
            ).scalar_one()
            == 0
        )
        # Primary falls back to the lowest ordinal when no chunk has a section.
        primary = conn.execute(
            text(
                "SELECT chunk_id FROM graph_node_chunk "
                "WHERE node_id = :n AND ref_type = 'primary'"
            ),
            {"n": psg_doc_node_id(11)},
        ).scalar_one()
        assert primary == ids[0]
