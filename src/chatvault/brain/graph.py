from __future__ import annotations

import sqlite3
from typing import Iterator

from chatvault.config import Config

# `brain graph rebuild`/`brain graph query` (docs/51_SPEC_brain_parity_and_naming.md
# §2.1 `graph` row), mirroring chatvault/graph.py's Neo4j projection
# pattern with Brain's own nodes/edges: `items` as nodes, `item_links` as
# edges, `tags`/`tag_links` for a TAGGED edge, same idea as ChatVault's own
# projection. Per spec §7 non-goals: built as scaffolding only — never run
# against a real Neo4j instance, never wired into MCP registration. It is
# verified in tests against a scratch Brain populated with synthetic
# item_links, using a fake driver so no real Neo4j connection is ever
# opened by the test suite. `brain doctor` (chatvault/brain/doctor.py)
# already reports the real item_links count against the ~200-edge
# threshold from docs/44_SPEC_BRAIN_options_and_neo4j.md — that part of
# this row was already done in parity batch A.

BATCH_SIZE = 5000

CONSTRAINTS = [
    "CREATE CONSTRAINT item_ref IF NOT EXISTS FOR (i:Item) REQUIRE i.uid IS UNIQUE",
    "CREATE CONSTRAINT brain_tag_name IF NOT EXISTS FOR (t:Tag) REQUIRE t.name IS UNIQUE",
]


def get_driver(config: Config):
    """Not called by any test or CLI code path run for real in this batch
    (§7 non-goal) — exists so `brain graph rebuild`/`query` have somewhere
    to get a real driver from once the user decides to actually run this
    against Neo4j, exactly mirroring chatvault.graph.get_driver."""
    from neo4j import GraphDatabase

    return GraphDatabase.driver(config.neo4j_uri, auth=(config.neo4j_user, config.neo4j_password))


def _chunks(items: list, size: int) -> Iterator[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _drop_all(tx) -> None:
    tx.run("MATCH (n) DETACH DELETE n")


def _ensure_constraints(session) -> None:
    for stmt in CONSTRAINTS:
        session.run(stmt)


def _item_uid(row: sqlite3.Row) -> str:
    """Items don't all have a `ref` (it's optional) — the numeric id is
    always present and unique, so the graph node key is `item:<id>`,
    stable regardless of whether the item has a ref."""
    return f"item:{row['id']}"


def _load_items(session, conn: sqlite3.Connection) -> int:
    rows = [dict(r) for r in conn.execute("SELECT id, ref, kind, title FROM items WHERE status = 'active'").fetchall()]
    for r in rows:
        r["uid"] = _item_uid(r)
    for batch in _chunks(rows, BATCH_SIZE):
        session.run(
            """
            UNWIND $rows AS row
            MERGE (i:Item {uid: row.uid})
            SET i.ref = row.ref, i.kind = row.kind, i.title = row.title
            """,
            rows=batch,
        )
    return len(rows)


def _load_item_links(session, conn: sqlite3.Connection) -> int:
    rows = [dict(r) for r in conn.execute("SELECT from_id, to_id, rel, note FROM item_links").fetchall()]
    for r in rows:
        r["from_uid"] = f"item:{r['from_id']}"
        r["to_uid"] = f"item:{r['to_id']}"
    for batch in _chunks(rows, BATCH_SIZE):
        session.run(
            """
            UNWIND $rows AS row
            MATCH (a:Item {uid: row.from_uid})
            MATCH (b:Item {uid: row.to_uid})
            MERGE (a)-[r:LINKED {rel: row.rel}]->(b)
            SET r.note = row.note
            """,
            rows=batch,
        )
    return len(rows)


def _load_tags(session, conn: sqlite3.Connection) -> int:
    rows = [
        dict(r)
        for r in conn.execute(
            """
            SELECT i.id AS item_id, t.name AS tag
            FROM tag_links tl JOIN tags t ON t.id = tl.tag_id JOIN items i ON i.id = tl.item_id
            WHERE i.status = 'active'
            """
        ).fetchall()
    ]
    for r in rows:
        r["uid"] = f"item:{r['item_id']}"
    for batch in _chunks(rows, BATCH_SIZE):
        session.run(
            """
            UNWIND $rows AS row
            MATCH (i:Item {uid: row.uid})
            MERGE (t:Tag {name: row.tag})
            MERGE (i)-[:TAGGED]->(t)
            """,
            rows=batch,
        )
    return len(rows)


def rebuild(conn: sqlite3.Connection, driver) -> dict[str, int]:
    """Drop-and-reseed, same as chatvault.graph.rebuild — Neo4j is always
    disposable, the source of truth stays brain.sqlite."""
    counts: dict[str, int] = {}
    with driver.session() as session:
        session.execute_write(_drop_all)
        _ensure_constraints(session)
        counts["items"] = _load_items(session, conn)
        counts["item_links"] = _load_item_links(session, conn)
        counts["tag_edges"] = _load_tags(session, conn)
    return counts


def run_query(driver, cypher: str, **params) -> list[dict]:
    with driver.session() as session:
        result = session.run(cypher, **params)
        return [record.data() for record in result]


def node_and_rel_counts(driver) -> dict[str, int]:
    with driver.session() as session:
        nodes = session.run("MATCH (n) RETURN count(n) AS n").single()["n"]
        rels = session.run("MATCH ()-[r]->() RETURN count(r) AS n").single()["n"]
    return {"nodes": nodes, "relationships": rels}
