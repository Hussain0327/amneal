"""Tier-1 knowledge-graph tables + deterministic derivation (migration 0018).

Nodes and edges are DERIVED from the PSG spine -- string joins over rows that
already exist, never mined and never model-generated in this tier. Chunks
remain the ONLY citable unit: graph rows navigate TO chunks through
``graph_node_chunk`` refs, and nothing in these tables may reach answer text
directly (INV-1).

Recorded decision (2026-08-18): populated-but-unread was retired from the
ingest path. Nothing reads these tables at runtime, so the ingest pipeline no
longer derives graph rows at chunk-write time. The tables, this derivation,
and the ``regwatch graph-backfill`` CLI command remain as the revival path:
when a traversal consumer actually ships, re-populate via the CLI backfill
and only then re-wire ingest-time derivation.

v1 scope (deliberately minimal -- see DECISIONS.md):
- node types: ``application``, ``psg_doc``, ``psg_section``
- edge types: ``HAS_PSG``, ``HAS_SECTION``, ``FOLLOWS``
- refs: ``member`` (chunk belongs to doc/section) and ``primary`` (the node's
  most-relevant chunk, by the deterministic min-ordinal rule)
No node embeddings and no cross-source (Orange Book / SPL) hub nodes until a
consumer exists for them.

Like ``embedding_profiles``, these are Core tables registered in
SQLModel.metadata (fresh-Postgres create_all + stamp-head stays converged with
migration replay) and callers use explicit SQL, not ORM rows.
"""

from __future__ import annotations

import hashlib
import json
from itertools import pairwise
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection, ForeignKey, Index, Table
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import SQLModel

from regwatch.common.logging import get_logger

log = get_logger(__name__)

# Bump when the derivation rules change; stamped on every ref row (`method`)
# so a re-derive can tell which rule produced what.
GRAPH_DERIVATION_VERSION = "graph-tier1-v1"

_NODE_TYPES = ("application", "psg_doc", "psg_section")
_EDGE_TYPES = ("HAS_PSG", "HAS_SECTION", "FOLLOWS")
_REF_TYPES = ("primary", "member", "mention")


def _json_type() -> Any:
    return sa.JSON().with_variant(JSONB(), "postgresql")


graph_node_table = Table(
    "graph_node",
    SQLModel.metadata,
    sa.Column("id", sa.String(), primary_key=True),
    sa.Column("node_type", sa.String(), nullable=False),
    sa.Column("natural_key", sa.String(), nullable=False),
    sa.Column("canonical_name", sa.String(), nullable=False),
    sa.Column("attrs_json", _json_type(), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.CheckConstraint(
        "node_type IN ('application','psg_doc','psg_section')",
        name="ck_graph_node_type",
    ),
    sa.UniqueConstraint("node_type", "natural_key", name="uq_graph_node_type_key"),
)

graph_edge_table = Table(
    "graph_edge",
    SQLModel.metadata,
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column(
        "src_node_id",
        sa.String(),
        ForeignKey("graph_node.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "dst_node_id",
        sa.String(),
        ForeignKey("graph_node.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("edge_type", sa.String(), nullable=False),
    sa.Column("tier", sa.SmallInteger(), nullable=False),
    sa.Column("confidence", sa.Float(), nullable=False, server_default=sa.text("1.0")),
    sa.Column("provenance_json", _json_type(), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.CheckConstraint("tier IN (1, 2)", name="ck_graph_edge_tier"),
    sa.CheckConstraint("confidence > 0 AND confidence <= 1", name="ck_graph_edge_confidence"),
    sa.UniqueConstraint("src_node_id", "dst_node_id", "edge_type", name="uq_graph_edge"),
)
Index("ix_graph_edge_src", graph_edge_table.c.src_node_id, graph_edge_table.c.edge_type)
Index("ix_graph_edge_dst", graph_edge_table.c.dst_node_id, graph_edge_table.c.edge_type)

graph_node_chunk_table = Table(
    "graph_node_chunk",
    SQLModel.metadata,
    sa.Column(
        "node_id",
        sa.String(),
        ForeignKey("graph_node.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column(
        "chunk_id",
        sa.String(),
        ForeignKey("chunk.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("ref_type", sa.String(), primary_key=True),
    sa.Column("rank", sa.Integer(), nullable=False, server_default=sa.text("0")),
    sa.Column("method", sa.String(), nullable=False),
    sa.CheckConstraint(
        "ref_type IN ('primary','member','mention')", name="ck_graph_node_chunk_ref_type"
    ),
)
Index("ix_graph_node_chunk_chunk", graph_node_chunk_table.c.chunk_id)


def application_node_id(appl_no: str) -> str:
    return f"application:{appl_no}"


def psg_doc_node_id(doc_id: int) -> str:
    return f"psg_doc:{doc_id}"


def psg_section_node_id(doc_id: int, version_id: int, section_path: str) -> str:
    # Identity fingerprint only, not a security boundary; sha256 keeps the
    # linter honest and the repo on one hash family.
    digest = hashlib.sha256(section_path.encode("utf-8")).hexdigest()[:12]
    return f"psg_section:{doc_id}-{version_id}-{digest}"


_UPSERT_NODE_SQL = """
INSERT INTO graph_node (id, node_type, natural_key, canonical_name, attrs_json)
VALUES (:id, :node_type, :natural_key, :canonical_name, :attrs_json)
ON CONFLICT (id) DO UPDATE SET
    canonical_name = EXCLUDED.canonical_name,
    attrs_json = EXCLUDED.attrs_json,
    updated_at = now()
"""

_INSERT_EDGE_SQL = """
INSERT INTO graph_edge (src_node_id, dst_node_id, edge_type, tier, confidence, provenance_json)
VALUES (:src, :dst, :edge_type, 1, 1.0, :provenance_json)
ON CONFLICT (src_node_id, dst_node_id, edge_type) DO NOTHING
"""

_INSERT_REF_SQL = """
INSERT INTO graph_node_chunk (node_id, chunk_id, ref_type, rank, method)
VALUES (:node_id, :chunk_id, :ref_type, :rank, :method)
ON CONFLICT (node_id, chunk_id, ref_type) DO UPDATE SET
    rank = EXCLUDED.rank,
    method = EXCLUDED.method
"""

_TIER1_PROVENANCE = json.dumps({"derivation": GRAPH_DERIVATION_VERSION})


def derive_document_graph(
    *,
    doc_id: int,
    version_id: int,
    chunk_ids: list[str],
    chunk_metas: list[dict[str, Any]],
    doc_attrs: dict[str, Any],
    conn: Connection,
) -> None:
    """Derive one document's tier-1 nodes/edges/refs on the CALLER'S
    connection, in the same transaction as the chunk write.

    Deterministic and idempotent: given the same chunks it produces the same
    rows. Section nodes are version-scoped and rebuilt from scratch on every
    call (their edges and refs die with them via FK CASCADE -- without the
    explicit delete they would accumulate forever, one generation per FDA
    revision). The doc/application nodes persist; their refs are rebuilt.

    ``chunk_metas`` must carry ``ordinal`` and ``section_path`` per chunk (the
    chunker always provides both since recipe v2).
    """
    if conn.dialect.name != "postgresql":
        raise ValueError("derive_document_graph requires a Postgres connection")
    if len(chunk_ids) != len(chunk_metas):
        raise ValueError("chunk_ids and chunk_metas must have equal lengths")
    if not chunk_ids:
        return

    doc_node = psg_doc_node_id(doc_id)
    appl_no = str(doc_attrs.get("appl_no") or "").strip()
    app_node = application_node_id(appl_no) if appl_no else None

    # GC: this doc's section-node generations (any version). The trailing '-'
    # in the LIKE pattern keeps doc 12 from matching doc 123's keys.
    conn.execute(
        sa_text(
            "DELETE FROM graph_node WHERE node_type = 'psg_section' AND natural_key LIKE :prefix"
        ),
        {"prefix": f"{doc_id}-%"},
    )
    # Rebuild the persistent nodes' refs from scratch so a superseded primary
    # can never linger next to its replacement.
    conn.execute(
        sa_text("DELETE FROM graph_node_chunk WHERE node_id = ANY(:nodes)"),
        {"nodes": [n for n in (doc_node, app_node) if n]},
    )

    name_bits = [
        str(doc_attrs.get("normalized_name") or "").strip(),
        str(doc_attrs.get("dosage_form") or "").strip(),
        str(doc_attrs.get("route") or "").strip(),
    ]
    psg_type = str(doc_attrs.get("psg_type") or "").strip()
    doc_name = " ".join(b for b in name_bits if b) or f"PSG doc {doc_id}"
    if psg_type:
        doc_name = f"{doc_name} PSG ({psg_type})"

    nodes: list[dict[str, Any]] = [
        {
            "id": doc_node,
            "node_type": "psg_doc",
            "natural_key": str(doc_id),
            "canonical_name": doc_name,
            "attrs_json": json.dumps(
                {
                    "doc_id": doc_id,
                    "appl_no": appl_no or None,
                    "normalized_name": doc_attrs.get("normalized_name"),
                    "dosage_form": doc_attrs.get("dosage_form"),
                    "route": doc_attrs.get("route"),
                    "psg_type": doc_attrs.get("psg_type"),
                }
            ),
        }
    ]
    if app_node:
        nodes.append(
            {
                "id": app_node,
                "node_type": "application",
                "natural_key": appl_no,
                "canonical_name": f"Application {appl_no}",
                "attrs_json": json.dumps({"appl_no": appl_no}),
            }
        )

    # Section nodes + membership, from the chunks themselves. min-ordinal per
    # section drives both FOLLOWS ordering and the section's primary ref.
    section_first_ordinal: dict[str, int] = {}
    section_members: dict[str, list[tuple[str, int]]] = {}
    for chunk_id, meta in zip(chunk_ids, chunk_metas, strict=True):
        section_path = meta.get("section_path") or None
        ordinal = int(meta.get("ordinal") or 0)
        if not section_path:
            continue
        key = str(section_path)
        section_first_ordinal[key] = min(ordinal, section_first_ordinal.get(key, ordinal))
        section_members.setdefault(key, []).append((chunk_id, ordinal))

    section_nodes: dict[str, str] = {}
    for section_path in section_first_ordinal:
        node_id = psg_section_node_id(doc_id, version_id, section_path)
        section_nodes[section_path] = node_id
        nodes.append(
            {
                "id": node_id,
                "node_type": "psg_section",
                "natural_key": f"{doc_id}-{version_id}-{section_path}"[:512],
                "canonical_name": section_path,
                "attrs_json": json.dumps(
                    {"doc_id": doc_id, "version_id": version_id, "section_path": section_path}
                ),
            }
        )
    conn.execute(sa_text(_UPSERT_NODE_SQL), nodes)

    edges: list[dict[str, Any]] = []
    if app_node:
        edges.append(
            {
                "src": app_node,
                "dst": doc_node,
                "edge_type": "HAS_PSG",
                "provenance_json": _TIER1_PROVENANCE,
            }
        )
    ordered_sections = sorted(section_first_ordinal, key=lambda s: section_first_ordinal[s])
    for section_path in ordered_sections:
        edges.append(
            {
                "src": doc_node,
                "dst": section_nodes[section_path],
                "edge_type": "HAS_SECTION",
                "provenance_json": _TIER1_PROVENANCE,
            }
        )
    for prev, nxt in pairwise(ordered_sections):
        edges.append(
            {
                "src": section_nodes[prev],
                "dst": section_nodes[nxt],
                "edge_type": "FOLLOWS",
                "provenance_json": _TIER1_PROVENANCE,
            }
        )
    if edges:
        conn.execute(sa_text(_INSERT_EDGE_SQL), edges)

    member_method = f"member-{GRAPH_DERIVATION_VERSION}"
    primary_method = f"primary-min-ordinal-{GRAPH_DERIVATION_VERSION}"
    refs: list[dict[str, Any]] = []
    for chunk_id, meta in zip(chunk_ids, chunk_metas, strict=True):
        ordinal = int(meta.get("ordinal") or 0)
        refs.append(
            {
                "node_id": doc_node,
                "chunk_id": chunk_id,
                "ref_type": "member",
                "rank": ordinal,
                "method": member_method,
            }
        )
        section_path = meta.get("section_path") or None
        if section_path:
            refs.append(
                {
                    "node_id": section_nodes[str(section_path)],
                    "chunk_id": chunk_id,
                    "ref_type": "member",
                    "rank": ordinal,
                    "method": member_method,
                }
            )

    # Primary refs: the doc's identity chunk is the lowest-ordinal chunk that
    # HAS a section (page-1 preamble carries furniture-adjacent identity text),
    # else plain lowest ordinal. Sections take their lowest-ordinal member.
    by_ordinal = sorted(
        zip(chunk_ids, chunk_metas, strict=True), key=lambda p: int(p[1].get("ordinal") or 0)
    )
    doc_primary = next((cid for cid, m in by_ordinal if m.get("section_path")), by_ordinal[0][0])
    primary_targets = [(doc_node, doc_primary)]
    if app_node:
        primary_targets.append((app_node, doc_primary))
    for section_path, members in section_members.items():
        members.sort(key=lambda p: p[1])
        primary_targets.append((section_nodes[section_path], members[0][0]))
    for node_id, chunk_id in primary_targets:
        refs.append(
            {
                "node_id": node_id,
                "chunk_id": chunk_id,
                "ref_type": "primary",
                "rank": 0,
                "method": primary_method,
            }
        )
    conn.execute(sa_text(_INSERT_REF_SQL), refs)
    log.info(
        "graph_derived",
        doc_id=doc_id,
        version_id=version_id,
        nodes=len(nodes),
        edges=len(edges),
        refs=len(refs),
    )
