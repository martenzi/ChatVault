from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from chatvault import browse
from chatvault.cli import app
from tests.test_browse import _cir, _load, make_block, make_message


def _seed(conn, rows):
    """rows: (uid, platform, started_at) — one text block each."""
    for uid, platform, day in rows:
        msg = make_message(0, "user", None, f"{day}T00:00:00", [make_block(0, "text", f"hi {uid}", raw={})], raw={})
        _load(conn, _cir(uid, uid, f"{day}T00:00:00", [msg], platform=platform))


NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)


def test_relative_since_forms():
    r = browse.resolve_relative_since
    assert r(last="7d", now=NOW) == "2026-10-12T12:00:00"
    assert r(last="24h", now=NOW) == "2026-10-18T12:00:00"
    assert r(last="2w", now=NOW) == "2026-10-05T12:00:00"
    assert r(week=True, now=NOW) == r(last="7d", now=NOW)
    assert r(today=True, now=datetime(2026, 10, 19, 1, 0)) == "2026-10-19"
    assert r(since="2026-01-01") == "2026-01-01"
    assert r() is None


def test_relative_since_rejects_conflicts_and_junk():
    with pytest.raises(ValueError, match="can't be combined"):
        browse.resolve_relative_since(since="2026-01-01", last="7d")
    with pytest.raises(ValueError, match="can't be combined"):
        browse.resolve_relative_since(today=True, week=True)
    with pytest.raises(ValueError, match="24h, 7d"):
        browse.resolve_relative_since(last="last tuesday")


def test_latest_limit_and_days_ago(conn):
    _seed(conn, [("a", "chatgpt", "2020-01-01"), ("b", "chatgpt", "2020-02-01"), ("c", "grok", "2020-03-01")])
    rows, total = browse.latest(conn, limit=2)
    assert [r["uid"].split(":", 1)[1] for r in rows] == ["c", "b"] and total == 3
    assert all(r["days_ago"] > 365 for r in rows)


def test_latest_per_platform_groups_and_caps(conn):
    _seed(conn, [(f"g{i}", "chatgpt", f"2020-01-0{i}") for i in range(1, 5)] + [("k1", "grok", "2020-01-01")])
    rows, total = browse.latest(conn, per_platform=2)
    assert [r["uid"].split(":", 1)[1] for r in rows] == ["g4", "g3", "k1"]  # platform order, newest first, <=2 each
    assert total == 5


def test_latest_filters_by_platform_and_since(conn):
    _seed(conn, [("a", "chatgpt", "2020-01-01"), ("b", "chatgpt", "2021-01-01"), ("c", "grok", "2021-01-01")])
    rows, _ = browse.latest(conn, platform="chatgpt", since="2020-06-01")
    assert [r["uid"].split(":", 1)[1] for r in rows] == ["b"]


def test_rows_by_ids_keeps_order_and_skips_non_live(conn):
    _seed(conn, [("a", "chatgpt", "2020-01-01"), ("b", "chatgpt", "2020-02-01")])
    ids = {r["uid"].split(":", 1)[1]: r["id"] for r in browse.latest(conn)[0]}
    conn.execute("UPDATE conversations SET excluded = 1 WHERE uid LIKE '%:a'")
    conn.commit()
    got = browse.rows_by_ids(conn, [ids["b"], ids["a"], 999999])
    assert [r["uid"] for r in got] == ["chatgpt:b"]


def _run(vault, monkeypatch, *args):
    monkeypatch.setenv("CHATVAULT_ROOT", str(vault.root))
    return CliRunner().invoke(app, list(args))


def test_latest_cli_writes_pick_cache(vault, conn, monkeypatch):
    _seed(conn, [("a", "chatgpt", "2020-01-01"), ("b", "grok", "2020-02-01")])
    conn.close()
    res = _run(vault, monkeypatch, "latest", "--per-platform")
    assert res.exit_code == 0, res.output
    assert "days ago" in res.output
    cache = json.loads(browse.cache_path(vault).read_text())
    assert [r["row"] for r in cache["rows"]] == [1, 2]


def test_view_run_with_ids_feeds_pick_cache(vault, conn, monkeypatch):
    _seed(conn, [("a", "chatgpt", "2020-01-01")])
    conn.close()
    _run(vault, monkeypatch, "view", "save", "all", "--sql", "SELECT id FROM v_conversations")
    res = _run(vault, monkeypatch, "view", "run", "all")
    assert res.exit_code == 0 and "saved as last query" in res.output
    assert len(json.loads(browse.cache_path(vault).read_text())["rows"]) == 1


def test_date_flag_conflict_errors_on_cli(vault, conn, monkeypatch):
    conn.close()
    for cmd in (["find", "x"], ["keyword", "x"], ["latest"]):
        res = _run(vault, monkeypatch, *cmd, "--last", "7d", "--since", "2026-01-01")
        assert res.exit_code == 1 and "can't be combined" in res.output


# --- doc 70: find fetches ids first, limits in SQL, counts separately ---


def _seed_find(conn):
    # titles hit "needle" (t1, t2); bodies hit it (b1, b2); one only via injected context (inj)
    for uid, title, day, btype, text in [
        ("t1", "needle one", "2020-01-05", "text", "x"),
        ("t2", "needle two", "2020-01-02", "text", "x"),
        ("b1", "plain a", "2020-01-04", "text", "has needle inside"),
        ("b2", "plain b", "2020-01-03", "text", "another needle"),
        ("inj", "plain c", "2020-01-06", "injected_context", "memory needle"),
    ]:
        msg = make_message(0, "user", None, f"{day}T00:00:00", [make_block(0, btype, text, raw={})], raw={})
        _load(conn, _cir(uid, title, f"{day}T00:00:00", [msg]))


def test_find_total_is_union_size_and_limit_applies_after_title_first_ordering(conn):
    _seed_find(conn)
    rows, total, hidden = browse.find_with_hidden(conn, query="needle", limit=3)
    assert total == 4 and hidden == 1
    # titles first (newest first), then body matches (newest first), cut at 3
    assert [r["uid"].split(":", 1)[1] for r in rows] == ["t1", "t2", "b1"]


def test_find_bare_limit_and_total(conn):
    _seed_find(conn)
    rows, total, _ = browse.find_with_hidden(conn, limit=2)
    assert total == 5 and [r["uid"].split(":", 1)[1] for r in rows] == ["inj", "t1"]


def test_find_rows_never_carry_raw_json(conn):
    _seed_find(conn)
    rows, _, _ = browse.find_with_hidden(conn, query="needle")
    assert "raw_json" not in rows[0].keys()


def test_browse_lister_never_selects_raw_json():
    from pathlib import Path
    src = Path(browse.__file__).read_text()
    assert "raw_json" not in src and "c.*" not in src


def test_keyword_cache_points_each_block_at_its_conversation_without_dedup(vault, conn, monkeypatch):
    token = "zqxdupetoken"
    msg = make_message(
        0, "user", None, "2020-01-01T00:00:00",
        [make_block(0, "text", f"first {token}", raw={}), make_block(1, "text", f"second {token}", raw={})], raw={},
    )
    _load(conn, _cir("one", "One", "2020-01-01T00:00:00", [msg]))
    msg2 = make_message(0, "user", None, "2020-01-02T00:00:00", [make_block(0, "text", f"third {token}", raw={})], raw={})
    _load(conn, _cir("two", "Two", "2020-01-02T00:00:00", [msg2]))
    conn.close()
    res = _run(vault, monkeypatch, "keyword", token)
    assert res.exit_code == 0, res.output
    cache = json.loads(browse.cache_path(vault).read_text())["rows"]
    assert [r["row"] for r in cache] == [1, 2, 3]
    ids = [r["conv_id"] for r in cache]
    assert len(set(ids)) == 2 and sorted(ids.count(i) for i in set(ids)) == [1, 2]
