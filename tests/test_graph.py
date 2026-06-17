"""Tests for the relationship GRAPH layer (Phase 7). Stdlib + in-memory SQLite only."""

from __future__ import annotations

import unittest

from assistant.memory import graph
from assistant.storage import db
from assistant.storage import repositories as repo


class GraphTest(unittest.TestCase):
    def setUp(self) -> None:
        # In-memory DB only; never touches the live file.
        self.conn = db.open_db(":memory:")

    def tearDown(self) -> None:
        self.conn.close()

    # ── nodes ────────────────────────────────────────────────────────────────
    def test_upsert_node_creates_and_returns_id(self) -> None:
        nid = graph.upsert_node(self.conn, "p1", "person", "Alex", {"role": "founder"})
        self.assertEqual(nid, "p1")
        nodes = graph.query_by_type(self.conn, "person")
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["name"], "Alex")
        self.assertEqual(nodes[0]["attrs"]["role"], "founder")

    def test_upsert_node_idempotent_and_merges_attrs(self) -> None:
        graph.upsert_node(self.conn, "p1", "person", "Alex", {"role": "founder"})
        graph.upsert_node(self.conn, "p1", "person", "Alex Smith", {"city": "NYC"})
        nodes = graph.query_by_type(self.conn, "person")
        self.assertEqual(len(nodes), 1)  # no duplicate
        n = nodes[0]
        self.assertEqual(n["name"], "Alex Smith")          # name refreshed
        self.assertEqual(n["attrs"]["role"], "founder")    # old attr preserved
        self.assertEqual(n["attrs"]["city"], "NYC")        # new attr merged

    def test_query_by_type_filters(self) -> None:
        graph.upsert_node(self.conn, "p1", "person", "Alex")
        graph.upsert_node(self.conn, "c1", "company", "Acme")
        self.assertEqual(len(graph.query_by_type(self.conn, "person")), 1)
        self.assertEqual(len(graph.query_by_type(self.conn, "company")), 1)
        self.assertEqual(graph.query_by_type(self.conn, "project"), [])

    # ── edges ────────────────────────────────────────────────────────────────
    def test_add_edge_idempotent_on_triple(self) -> None:
        e1 = graph.add_edge(self.conn, "p1", "p2", "knows")
        e2 = graph.add_edge(self.conn, "p1", "p2", "knows")
        self.assertEqual(e1, e2)  # same edge id returned, no duplicate
        count = self.conn.execute("SELECT COUNT(*) c FROM graph_edges").fetchone()["c"]
        self.assertEqual(count, 1)

    def test_add_edge_distinct_types_coexist(self) -> None:
        graph.add_edge(self.conn, "p1", "p2", "knows")
        graph.add_edge(self.conn, "p1", "p2", "introduced")
        count = self.conn.execute("SELECT COUNT(*) c FROM graph_edges").fetchone()["c"]
        self.assertEqual(count, 2)

    # ── neighbors / connected_to ───────────────────────────────────────────────
    def test_neighbors_directions(self) -> None:
        graph.upsert_node(self.conn, "p1", "person", "Alex")
        graph.upsert_node(self.conn, "p2", "person", "Bo")
        graph.upsert_node(self.conn, "p3", "person", "Cy")
        graph.add_edge(self.conn, "p1", "p2", "knows")     # p1 -> p2
        graph.add_edge(self.conn, "p3", "p1", "introduced")  # p3 -> p1

        out = graph.neighbors(self.conn, "p1", direction="out")
        self.assertEqual({n["node_id"] for n in out}, {"p2"})
        self.assertEqual(out[0]["name"], "Bo")
        self.assertEqual(out[0]["edge_type"], "knows")

        inc = graph.neighbors(self.conn, "p1", direction="in")
        self.assertEqual({n["node_id"] for n in inc}, {"p3"})

        both = graph.neighbors(self.conn, "p1", direction="both")
        self.assertEqual({n["node_id"] for n in both}, {"p2", "p3"})

    def test_neighbors_edge_type_filter(self) -> None:
        graph.add_edge(self.conn, "p1", "p2", "knows")
        graph.add_edge(self.conn, "p1", "c1", "works_with")
        ww = graph.neighbors(self.conn, "p1", edge_type="works_with", direction="out")
        self.assertEqual({n["node_id"] for n in ww}, {"c1"})

    def test_connected_to_both_directions(self) -> None:
        graph.add_edge(self.conn, "p1", "p2", "knows")
        graph.add_edge(self.conn, "p3", "p1", "related_to")
        conn_nodes = graph.connected_to(self.conn, "p1")
        self.assertEqual({n["node_id"] for n in conn_nodes}, {"p2", "p3"})
        types = {n["node_id"]: n["edge_type"] for n in conn_nodes}
        self.assertEqual(types["p2"], "knows")
        self.assertEqual(types["p3"], "related_to")

    # ── waiting_on_me ──────────────────────────────────────────────────────────
    def test_waiting_on_me(self) -> None:
        graph.upsert_node(self.conn, "me", "person", "Jatin")
        graph.upsert_node(self.conn, "p2", "person", "Bo")
        graph.upsert_node(self.conn, "p3", "person", "Cy")
        # Two people are blocked on me; I am blocked on a third (outgoing, excluded).
        graph.add_edge(self.conn, "p2", "me", "waiting_on")
        graph.add_edge(self.conn, "p3", "me", "waiting_on")
        graph.add_edge(self.conn, "me", "p2", "waiting_on")
        blockers = graph.waiting_on_me(self.conn, "me")
        self.assertEqual({n["node_id"] for n in blockers}, {"p2", "p3"})
        for n in blockers:
            self.assertEqual(n["edge_direction"], "in")

    # ── sync_from_persons ──────────────────────────────────────────────────────
    def test_sync_from_persons_populates_nodes_and_edges(self) -> None:
        repo.person_add(
            self.conn, person_id="pa", display_name="Alex Founder",
            emails=["alex@acme.com"], company="acme.com",
        )
        repo.person_add(
            self.conn, person_id="pb", display_name="Bo Buyer",
            emails=["bo@acme.com"], company="acme.com",
        )
        repo.person_add(
            self.conn, person_id="pc", display_name="Cy Solo",
            emails=["cy@gmail.com"], company="",  # no company -> no company node/edge
        )
        counts = graph.sync_from_persons(self.conn)
        self.assertEqual(counts["persons"], 3)

        persons = {n["id"]: n for n in graph.query_by_type(self.conn, "person")}
        self.assertEqual(set(persons), {"pa", "pb", "pc"})
        self.assertEqual(persons["pa"]["name"], "Alex Founder")

        companies = graph.query_by_type(self.conn, "company")
        self.assertEqual([c["id"] for c in companies], ["company:acme.com"])

        # Both Acme people have a works_with edge to the single company node.
        acme_inc = graph.neighbors(
            self.conn, "company:acme.com", edge_type="works_with", direction="in"
        )
        self.assertEqual({n["node_id"] for n in acme_inc}, {"pa", "pb"})

        # Cy (no company) has no works_with edge.
        cy_out = graph.neighbors(self.conn, "pc", edge_type="works_with", direction="out")
        self.assertEqual(cy_out, [])

    def test_sync_from_persons_idempotent(self) -> None:
        repo.person_add(
            self.conn, person_id="pa", display_name="Alex",
            emails=["a@acme.com"], company="acme.com",
        )
        graph.sync_from_persons(self.conn)
        graph.sync_from_persons(self.conn)  # second run must not duplicate
        self.assertEqual(len(graph.query_by_type(self.conn, "person")), 1)
        self.assertEqual(len(graph.query_by_type(self.conn, "company")), 1)
        edge_count = self.conn.execute(
            "SELECT COUNT(*) c FROM graph_edges WHERE type='works_with'"
        ).fetchone()["c"]
        self.assertEqual(edge_count, 1)

    # ── defensiveness ──────────────────────────────────────────────────────────
    def test_bad_inputs_never_raise(self) -> None:
        self.assertIsNone(graph.upsert_node(self.conn, "", "person", "x"))
        self.assertIsNone(graph.add_edge(self.conn, "", "p2", "knows"))
        self.assertIsNone(graph.add_edge(self.conn, "p1", "", "knows"))
        self.assertEqual(graph.neighbors(self.conn, ""), [])
        self.assertEqual(graph.waiting_on_me(self.conn, ""), [])


if __name__ == "__main__":
    unittest.main()
