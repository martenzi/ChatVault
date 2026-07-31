from __future__ import annotations

import sqlite3
from typing import Iterator

from chatvault.config import Config

CONSTRAINTS = [
    "CREATE CONSTRAINT conv_uid IF NOT EXISTS FOR (c:Conversation) REQUIRE c.uid IS UNIQUE",
    "CREATE CONSTRAINT model_name IF NOT EXISTS FOR (m:Model) REQUIRE m.name IS UNIQUE",
    "CREATE CONSTRAINT tool_name IF NOT EXISTS FOR (t:Tool) REQUIRE t.name IS UNIQUE",
    "CREATE CONSTRAINT tag_name IF NOT EXISTS FOR (t:Tag) REQUIRE t.name IS UNIQUE",
    "CREATE CONSTRAINT platform_name IF NOT EXISTS FOR (p:Platform) REQUIRE p.name IS UNIQUE",
    "CREATE CONSTRAINT project_name IF NOT EXISTS FOR (p:Project) REQUIRE p.name IS UNIQUE",
]

BATCH_SIZE = 5000

# The five canned Cypher queries from the architecture spec, §7.3 + a threads-by-tag lookup.
CANNED_QUERIES = {
    "models-by-conversation-count": """
        MATCH (c:Conversation)-[r:USED_MODEL]->(m:Model)
        RETURN m.name AS model, count(c) AS conversations, sum(r.message_count) AS messages
        ORDER BY conversations DESC
    """,
    "tools-by-usage": """
        MATCH (c:Conversation)-[r:USED_TOOL]->(t:Tool)
        RETURN t.name AS tool, count(c) AS conversations, sum(r.call_count) AS calls
        ORDER BY calls DESC
    """,
    "conversations-by-platform": """
        MATCH (c:Conversation)-[:ON_PLATFORM]->(p:Platform)
        RETURN p.name AS platform, count(c) AS conversations
        ORDER BY conversations DESC
    """,
    "threads-by-tag": """
        MATCH (c:Conversation)-[:TAGGED]->(t:Tag {name: $tag})
        RETURN c.uid AS uid, c.title AS title, c.started_at AS started_at
        ORDER BY started_at
    """,
    "projects-and-their-models": """
        MATCH (c:Conversation)-[:IN_PROJECT]->(p:Project)
        MATCH (c)-[:USED_MODEL]->(m:Model)
        RETURN p.name AS project, collect(DISTINCT m.name) AS models, count(DISTINCT c) AS conversations
        ORDER BY conversations DESC
    """,
}


def get_driver(config: Config):
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


def _load_conversations(session, conn: sqlite3.Connection, since: str | None = None) -> int:
    sql = "SELECT * FROM v_conversations"
    params: tuple = ()
    if since:
        sql += " WHERE started_at >= ?"
        params = (since,)
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    for batch in _chunks(rows, BATCH_SIZE):
        session.run(
            """
            UNWIND $rows AS row
            MERGE (c:Conversation {uid: row.uid})
            SET c.title = row.title,
                c.platform = row.platform,
                c.project = row.project,
                c.started_at = row.started_at,
                c.message_count = row.message_count
            MERGE (p:Platform {name: row.platform})
            MERGE (c)-[:ON_PLATFORM]->(p)
            WITH c, row
            FOREACH (_ IN CASE WHEN row.project IS NOT NULL THEN [1] ELSE [] END |
                MERGE (proj:Project {name: row.project})
                MERGE (c)-[:IN_PROJECT]->(proj)
            )
            """,
            rows=batch,
        )
    return len(rows)


def _load_models(session, conn: sqlite3.Connection, since: str | None = None) -> int:
    sql = """
        SELECT c.uid AS uid, m.model AS model, COUNT(*) AS message_count
        FROM v_messages m JOIN v_conversations c ON c.id = m.conv_id
        WHERE m.model IS NOT NULL {since_clause}
        GROUP BY c.uid, m.model
    """.format(since_clause="AND c.started_at >= ?" if since else "")
    rows = [dict(r) for r in conn.execute(sql, (since,) if since else ()).fetchall()]
    for batch in _chunks(rows, BATCH_SIZE):
        session.run(
            """
            UNWIND $rows AS row
            MATCH (c:Conversation {uid: row.uid})
            MERGE (m:Model {name: row.model})
            MERGE (c)-[r:USED_MODEL]->(m)
            SET r.message_count = row.message_count
            """,
            rows=batch,
        )
    return len(rows)


def _load_tools(session, conn: sqlite3.Connection, since: str | None = None) -> int:
    sql = """
        SELECT c.uid AS uid, b.tool_name AS tool_name, COUNT(*) AS call_count
        FROM v_blocks b JOIN v_conversations c ON c.id = b.conv_id
        WHERE b.type = 'tool_use' AND b.tool_name IS NOT NULL {since_clause}
        GROUP BY c.uid, b.tool_name
    """.format(since_clause="AND c.started_at >= ?" if since else "")
    rows = [dict(r) for r in conn.execute(sql, (since,) if since else ()).fetchall()]
    for batch in _chunks(rows, BATCH_SIZE):
        session.run(
            """
            UNWIND $rows AS row
            MATCH (c:Conversation {uid: row.uid})
            MERGE (t:Tool {name: row.tool_name})
            MERGE (c)-[r:USED_TOOL]->(t)
            SET r.call_count = row.call_count
            """,
            rows=batch,
        )
    return len(rows)


def _load_tags(session, conn: sqlite3.Connection, since: str | None = None) -> int:
    sql = """
        SELECT c.uid AS uid, t.name AS tag
        FROM tag_links tl
        JOIN tags t ON t.id = tl.tag_id
        JOIN v_conversations c ON c.id = tl.target_id AND tl.target_type = 'conversation'
        {since_clause}
    """.format(since_clause="WHERE c.started_at >= ?" if since else "")
    rows = [dict(r) for r in conn.execute(sql, (since,) if since else ()).fetchall()]
    for batch in _chunks(rows, BATCH_SIZE):
        session.run(
            """
            UNWIND $rows AS row
            MATCH (c:Conversation {uid: row.uid})
            MERGE (t:Tag {name: row.tag})
            MERGE (c)-[:TAGGED]->(t)
            """,
            rows=batch,
        )
    return len(rows)


def _load_messages_and_blocks(session, conn: sqlite3.Connection) -> tuple[int, int]:
    msg_rows = [
        dict(r)
        for r in conn.execute(
            """
            SELECT c.uid AS conv_uid, m.id AS mid, m.idx AS idx, m.role AS role, m.created_at AS created_at
            FROM v_messages m JOIN v_conversations c ON c.id = m.conv_id
            ORDER BY c.id, m.idx
            """
        ).fetchall()
    ]
    for batch in _chunks(msg_rows, BATCH_SIZE):
        session.run(
            """
            UNWIND $rows AS row
            MATCH (c:Conversation {uid: row.conv_uid})
            MERGE (m:Message {uid: row.conv_uid + ':' + toString(row.idx)})
            SET m.idx = row.idx, m.role = row.role, m.created_at = row.created_at
            MERGE (c)-[:HAS_MESSAGE]->(m)
            """,
            rows=batch,
        )
    # NEXT chain per conversation
    for batch in _chunks(msg_rows, BATCH_SIZE):
        session.run(
            """
            UNWIND $rows AS row
            MATCH (m:Message {uid: row.conv_uid + ':' + toString(row.idx)})
            OPTIONAL MATCH (prev:Message {uid: row.conv_uid + ':' + toString(row.idx - 1)})
            FOREACH (_ IN CASE WHEN prev IS NOT NULL THEN [1] ELSE [] END |
                MERGE (prev)-[:NEXT]->(m)
            )
            """,
            rows=batch,
        )

    block_rows = [
        dict(r)
        for r in conn.execute(
            """
            SELECT c.uid AS conv_uid, m.idx AS midx, b.idx AS bidx, b.type AS type, b.tool_name AS tool_name
            FROM v_blocks b
            JOIN v_messages m ON m.id = b.msg_id
            JOIN v_conversations c ON c.id = b.conv_id
            ORDER BY c.id, m.idx, b.idx
            """
        ).fetchall()
    ]
    for batch in _chunks(block_rows, BATCH_SIZE):
        session.run(
            """
            UNWIND $rows AS row
            MATCH (m:Message {uid: row.conv_uid + ':' + toString(row.midx)})
            MERGE (b:Block {uid: row.conv_uid + ':' + toString(row.midx) + ':' + toString(row.bidx)})
            SET b.type = row.type, b.tool_name = row.tool_name
            MERGE (m)-[:HAS_BLOCK]->(b)
            """,
            rows=batch,
        )
    return len(msg_rows), len(block_rows)


def rebuild(conn: sqlite3.Connection, driver, depth: str = "meta") -> dict[str, int]:
    if depth not in ("meta", "full"):
        raise ValueError("depth must be 'meta' or 'full'")

    counts: dict[str, int] = {}
    with driver.session() as session:
        session.execute_write(_drop_all)
        _ensure_constraints(session)
        counts["conversations"] = _load_conversations(session, conn)
        counts["model_edges"] = _load_models(session, conn)
        counts["tool_edges"] = _load_tools(session, conn)
        counts["tag_edges"] = _load_tags(session, conn)
        if depth == "full":
            counts["messages"], counts["blocks"] = _load_messages_and_blocks(session, conn)
    return counts


def push(conn: sqlite3.Connection, driver, since: str | None = None, depth: str = "meta") -> dict[str, int]:
    """Incremental push — MERGE-based, so safe to re-run; does not drop existing data."""
    counts: dict[str, int] = {}
    with driver.session() as session:
        _ensure_constraints(session)
        counts["conversations"] = _load_conversations(session, conn, since=since)
        counts["model_edges"] = _load_models(session, conn, since=since)
        counts["tool_edges"] = _load_tools(session, conn, since=since)
        counts["tag_edges"] = _load_tags(session, conn, since=since)
        if depth == "full":
            counts["messages"], counts["blocks"] = _load_messages_and_blocks(session, conn)
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
