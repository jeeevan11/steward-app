"""Relationship GRAPH layer (Phase 7) — a typed node/edge view over the world.

The `persons` table (identity.py) is the canonical store of PERSON entities and the
cross-channel identifier resolution that backs them. This module does NOT duplicate
that work: it sits ALONGSIDE it as a lightweight, queryable graph so the brain can ask
relational questions — "who introduced me to X?", "who is waiting on me?", "everyone
connected to this company" — without re-deriving identity each time.

Persons remain the canonical person nodes; `sync_from_persons` projects them (and the
companies they work at) into graph nodes/edges so the graph can be rebuilt from existing
data at any time, idempotently, without owning identity resolution.

Design follows the house storage idiom (own `ensure(conn)` table creation; best-effort;
stdlib + sqlite3 only). Every public function is defensive and never raises into a caller;
on any failure it logs at debug and returns an empty / unchanged result.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Optional

from assistant.logging_setup import get_logger

log = get_logger("graph")

# Allowed node / edge types. Kept permissive at the SQL layer (no CHECK constraint, to
# stay forgiving of future types) but documented and validated softly here.
NODE_TYPES = frozenset({
    "person", "company", "project", "commitment", "conversation", "event",
})
EDGE_TYPES = frozenset({
    "knows", "introduced", "works_with", "invested_in",
    "waiting_on", "connected_to", "related_to",
})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS graph_nodes (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL,          -- person|company|project|commitment|conversation|event
    name        TEXT DEFAULT '',
    attrs_json  TEXT DEFAULT '{}',
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    updated_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_type ON graph_nodes(type);

CREATE TABLE IF NOT EXISTS graph_edges (
    id          TEXT PRIMARY KEY,
    src         TEXT NOT NULL,
    dst         TEXT NOT NULL,
    type        TEXT NOT NULL,          -- knows|introduced|works_with|invested_in|waiting_on|connected_to|related_to
    attrs_json  TEXT DEFAULT '{}',
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
-- A single edge per (src, dst, type): add_edge is idempotent on this triple.
CREATE UNIQUE INDEX IF NOT EXISTS idx_graph_edges_triple ON graph_edges(src, dst, type);
CREATE INDEX IF NOT EXISTS idx_graph_edges_src ON graph_edges(src);
CREATE INDEX IF NOT EXISTS idx_graph_edges_dst ON graph_edges(dst);
CREATE INDEX IF NOT EXISTS idx_graph_edges_type ON graph_edges(type);
"""


def ensure(conn: sqlite3.Connection) -> None:
    """Create the graph tables/indexes if absent (idempotent). Best-effort."""
    try:
        conn.executescript(_SCHEMA)
    except Exception:  # noqa: BLE001 - graph is additive; never break the caller
        log.debug("graph ensure failed (non-fatal)", exc_info=True)


def _dumps(attrs: Optional[dict]) -> str:
    try:
        return json.dumps(attrs or {})
    except (TypeError, ValueError):
        return "{}"


def _loads(s: Any) -> dict:
    try:
        v = json.loads(s) if s else {}
        return v if isinstance(v, dict) else {}
    except (ValueError, TypeError):
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# Mutations
# ─────────────────────────────────────────────────────────────────────────────
def upsert_node(
    conn: sqlite3.Connection, node_id: str, type: str, name: str = "",
    attrs: Optional[dict] = None,
) -> Optional[str]:
    """Create or refresh a node. Returns the node id (or None on failure).

    Idempotent on node_id: a second call with the same id updates name/attrs/type and
    bumps updated_at, never creating a duplicate. attrs is merged shallowly over the
    existing attrs so a refresh does not silently drop fields it did not mention."""
    if not node_id:
        return None
    try:
        ensure(conn)
        row = conn.execute(
            "SELECT attrs_json FROM graph_nodes WHERE id=?", (node_id,)
        ).fetchone()
        merged = _loads(row["attrs_json"]) if row is not None else {}
        if attrs:
            merged.update(attrs)
        conn.execute(
            "INSERT INTO graph_nodes (id, type, name, attrs_json) VALUES (?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET "
            " type=excluded.type, name=excluded.name, attrs_json=excluded.attrs_json, "
            " updated_at=strftime('%s','now')",
            (node_id, str(type or ""), str(name or ""), _dumps(merged)),
        )
        return node_id
    except Exception:  # noqa: BLE001
        log.debug("upsert_node failed (non-fatal)", exc_info=True)
        return None


def add_edge(
    conn: sqlite3.Connection, src: str, dst: str, type: str,
    attrs: Optional[dict] = None,
) -> Optional[str]:
    """Create a directed edge src --type--> dst. Idempotent on (src, dst, type):
    a repeat call refreshes attrs and returns the existing edge id rather than
    inserting a duplicate. Returns the edge id (or None on failure)."""
    if not src or not dst or not type:
        return None
    try:
        ensure(conn)
        existing = conn.execute(
            "SELECT id FROM graph_edges WHERE src=? AND dst=? AND type=?",
            (src, dst, str(type)),
        ).fetchone()
        if existing is not None:
            if attrs:
                conn.execute(
                    "UPDATE graph_edges SET attrs_json=? WHERE id=?",
                    (_dumps(attrs), existing["id"]),
                )
            return existing["id"]
        edge_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO graph_edges (id, src, dst, type, attrs_json) VALUES (?,?,?,?,?)",
            (edge_id, src, dst, str(type), _dumps(attrs)),
        )
        return edge_id
    except Exception:  # noqa: BLE001
        log.debug("add_edge failed (non-fatal)", exc_info=True)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Queries
# ─────────────────────────────────────────────────────────────────────────────
def query_by_type(conn: sqlite3.Connection, type: str) -> list[dict]:
    """All nodes of a given type, as plain dicts (attrs decoded). Empty on failure."""
    try:
        ensure(conn)
        rows = conn.execute(
            "SELECT * FROM graph_nodes WHERE type=? ORDER BY name", (str(type or ""),)
        ).fetchall()
        return [_node_dict(r) for r in rows]
    except Exception:  # noqa: BLE001
        log.debug("query_by_type failed (non-fatal)", exc_info=True)
        return []


def neighbors(
    conn: sqlite3.Connection, node_id: str, edge_type: Optional[str] = None,
    direction: str = "out",
) -> list[dict]:
    """Adjacent nodes one hop away.

    direction: 'out' follows src=node_id, 'in' follows dst=node_id, 'both' unions them.
    Each result is {node_id, type, name, attrs, edge_type, edge_attrs, edge_direction}.
    Optional edge_type filters by relation. Empty list on failure."""
    if not node_id:
        return []
    direction = (direction or "out").lower()
    if direction not in ("out", "in", "both"):
        direction = "out"
    try:
        ensure(conn)
        out: list[dict] = []
        if direction in ("out", "both"):
            out.extend(_edge_neighbors(conn, node_id, edge_type, going="out"))
        if direction in ("in", "both"):
            out.extend(_edge_neighbors(conn, node_id, edge_type, going="in"))
        return out
    except Exception:  # noqa: BLE001
        log.debug("neighbors failed (non-fatal)", exc_info=True)
        return []


def connected_to(conn: sqlite3.Connection, node_id: str) -> list[dict]:
    """Every node reachable one hop in EITHER direction, with the connecting edge type.
    A convenience wrapper over neighbors(direction='both')."""
    return neighbors(conn, node_id, edge_type=None, direction="both")


def waiting_on_me(conn: sqlite3.Connection, me_id: str) -> list[dict]:
    """Nodes with an incoming waiting_on edge pointing at me — i.e. the relations that
    are blocked on me. Each is a neighbor dict (edge_direction='in'). Empty on failure.

    This mirrors the 'awaiting' field on distill.open_situations: when a situation is
    awaiting Jatin, an edge other --waiting_on--> me captures the same blocked-on-me fact
    at the relational level."""
    if not me_id:
        return []
    return neighbors(conn, me_id, edge_type="waiting_on", direction="in")


# ─────────────────────────────────────────────────────────────────────────────
# Projection from existing data (does NOT own identity — just mirrors persons)
# ─────────────────────────────────────────────────────────────────────────────
def sync_from_persons(conn: sqlite3.Connection) -> dict:
    """Best-effort projection of the canonical `persons` table into graph nodes/edges.

    For each person row: upsert a person node (id = person id, name = display_name). When
    the person has a non-empty company, upsert a company node (id = 'company:<company>')
    and a person --works_with--> company edge. Idempotent: re-running refreshes names and
    never duplicates nodes or edges (works_with is unique on (src,dst,type)). Identity
    resolution stays entirely in identity.py — this only mirrors what is already there.

    Returns counts {persons, companies, edges} for observability. Never raises."""
    counts = {"persons": 0, "companies": 0, "edges": 0}
    try:
        ensure(conn)
        rows = conn.execute(
            "SELECT id, display_name, company FROM persons"
        ).fetchall()
        for r in rows:
            pid = r["id"]
            if not pid:
                continue
            if upsert_node(
                conn, pid, "person", (r["display_name"] or ""),
                attrs={"source": "persons"},
            ):
                counts["persons"] += 1
            company = (r["company"] or "").strip()
            if company:
                company_id = "company:" + company.lower()
                if upsert_node(
                    conn, company_id, "company", company, attrs={"source": "persons"}
                ):
                    counts["companies"] += 1
                if add_edge(conn, pid, company_id, "works_with", attrs={"source": "persons"}):
                    counts["edges"] += 1
        return counts
    except Exception:  # noqa: BLE001
        log.debug("sync_from_persons failed (non-fatal)", exc_info=True)
        return counts


# ─────────────────────────────────────────────────────────────────────────────
# Row → dict helpers (internal)
# ─────────────────────────────────────────────────────────────────────────────
def _node_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "type": row["type"],
        "name": row["name"],
        "attrs": _loads(row["attrs_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _edge_neighbors(
    conn: sqlite3.Connection, node_id: str, edge_type: Optional[str], *, going: str
) -> list[dict]:
    """One direction of neighbors. going='out' -> follow src; 'in' -> follow dst.
    Joins to graph_nodes so callers get the adjacent node's type/name/attrs in one pass."""
    self_col, other_col = ("src", "dst") if going == "out" else ("dst", "src")
    # COALESCE so an edge to a not-yet-materialized node still yields the endpoint id
    # (the join is a best-effort enrichment, not a requirement that the node row exists).
    sql = (
        f"SELECT e.id AS edge_id, e.type AS edge_type, e.attrs_json AS edge_attrs, "
        f"       COALESCE(n.id, e.{other_col}) AS node_id, n.type AS node_type, "
        f"       n.name AS node_name, n.attrs_json AS node_attrs "
        f"FROM graph_edges e LEFT JOIN graph_nodes n ON n.id = e.{other_col} "
        f"WHERE e.{self_col}=?"
    )
    params: list[Any] = [node_id]
    if edge_type:
        sql += " AND e.type=?"
        params.append(str(edge_type))
    rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        out.append({
            "node_id": r["node_id"],
            "type": r["node_type"],
            "name": r["node_name"],
            "attrs": _loads(r["node_attrs"]),
            "edge_id": r["edge_id"],
            "edge_type": r["edge_type"],
            "edge_attrs": _loads(r["edge_attrs"]),
            "edge_direction": going,
        })
    return out
