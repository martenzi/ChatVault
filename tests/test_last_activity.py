"""Last-activity date: display, sort, filters, date-health flags (doc 73)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from chatvault import browse, dates, loader
from chatvault.adapters import cursor_legacy_copilot, devin_desktop_acp
from chatvault.adapters.base import make_block, make_conversation, make_message


def _add(conn, native_id, started, ended, n_msgs=2, platform="synthetic", title=None):
    msgs = [
        make_message(i, "user" if i % 2 == 0 else "assistant", None, None, [make_block(0, "text", f"{native_id} m{i}", raw={})], raw={})
        for i in range(n_msgs)
    ]
    cir = make_conversation(
        platform=platform, native_id=native_id, title=title or native_id, project=None,
        started_at=started, ended_at=ended, source_path="synthetic", raw={}, messages=msgs,
    )
    r = loader.load_conversation(conn, cir, source_file_id=None)
    conn.commit()
    return r.conv_id


@pytest.fixture
def convs(conn):
    ids = {
        # started long ago, touched last week: the conversation --since misses
        "old_active": _add(conn, "old_active", "2026-03-31T10:00:00Z", "2026-10-20T10:00:00Z", n_msgs=6),
        "new_quick": _add(conn, "new_quick", "2026-10-01T10:00:00Z", "2026-10-01T10:05:00Z", n_msgs=2),
        "mid": _add(conn, "mid", "2026-06-01T10:00:00Z", "2026-06-02T10:00:00Z", n_msgs=3),
        "undated": _add(conn, "undated", "2026-10-15T10:00:00Z", None, n_msgs=4),
    }
    return ids


def _titles(rows):
    return [r["title"] for r in rows]


def test_recent_sorts_by_last_activity_and_undated_last(conn, convs):
    rows, _ = browse.find(conn, sort="recent")
    assert _titles(rows) == ["old_active", "new_quick", "mid", "undated"]


def test_started_sort_is_the_old_behaviour(conn, convs):
    rows, _ = browse.find(conn, sort="started")
    assert _titles(rows) == ["undated", "new_quick", "mid", "old_active"]


def test_since_filters_on_start_active_since_on_last_message(conn, convs):
    rows, _ = browse.find(conn, since="2026-10-01")
    assert "old_active" not in _titles(rows)  # the silent false negative
    rows, _ = browse.find(conn, active_since="2026-10-01")
    assert "old_active" in _titles(rows)
    assert "mid" not in _titles(rows)


def test_active_since_keeps_a_known_recent_start_with_no_end_date(conn, convs):
    rows, _ = browse.find(conn, active_since="2026-10-10")
    assert "undated" in _titles(rows)


def test_active_before_finds_dormant_threads(conn, convs):
    rows, _ = browse.find(conn, active_before="2026-09-01")
    assert _titles(rows) == ["mid"]


def test_min_span_needs_an_end_date(conn, convs):
    rows, _ = browse.find(conn, min_span=90)
    assert _titles(rows) == ["old_active"]
    rows, _ = browse.find(conn, min_span=1)
    assert set(_titles(rows)) == {"old_active", "mid"}


def test_min_and_max_msgs(conn, convs):
    rows, _ = browse.find(conn, min_msgs=4)
    assert set(_titles(rows)) == {"old_active", "undated"}
    rows, _ = browse.find(conn, max_msgs=3)
    assert set(_titles(rows)) == {"new_quick", "mid"}


def test_latest_orders_and_ages_by_last_activity(conn, convs):
    rows, _ = browse.latest(conn, limit=10)
    assert _titles(rows)[0] == "old_active"
    by_title = {r["title"]: r for r in rows}
    assert by_title["old_active"]["days_ago"] < by_title["new_quick"]["days_ago"]
    assert by_title["undated"]["days_ago"] is None  # never filled in from the start date
    rows, _ = browse.latest(conn, limit=10, by_started=True)
    assert _titles(rows)[0] == "undated"


def test_nothing_is_invented_for_missing_end_date(conn, convs):
    ended = conn.execute("SELECT ended_at FROM conversations WHERE id = ?", (convs["undated"],)).fetchone()[0]
    assert ended is None


def test_pick_cache_still_resolves_after_a_new_listing(conn, vault, convs):
    rows, _ = browse.find(conn, active_since="2026-10-01")
    browse.save_result_cache(conn, vault, rows, {"query": None})
    assert browse.resolve_pick(conn, vault, "1") == [rows[0]["id"]]


@pytest.fixture
def stockholm():
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Stockholm"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def test_time_of_day_uses_local_time_not_utc(stockholm):
    # 22:30Z is 00:30 in Stockholm (UTC+2 in summer): a night session, not an evening one
    assert browse.time_of_day("2026-06-01T22:30:00Z") == "night"
    assert browse.time_of_day("2026-06-01T22:30:00+00:00") == "night"
    assert browse.time_of_day("2026-06-01T20:30:00+02:00") == "evening"
    assert browse.time_of_day("2026-06-01T07:00:00+02:00") == "morning"
    assert browse.time_of_day("2026-06-01T13:00:00+02:00") == "afternoon"


def test_naive_timestamps_are_never_bucketed(stockholm):
    assert browse.time_of_day("2026-06-01T22:30:00") is None


def test_when_weekday_weekend_filters_exclude_naive(conn, stockholm):
    _add(conn, "sat_night", "2026-06-05T22:30:00Z", "2026-06-05T23:00:00Z")  # Sat 00:30 local
    _add(conn, "mon_noon", "2026-06-01T10:30:00Z", "2026-06-01T11:00:00Z")  # Mon 12:30 local
    _add(conn, "naive", "2026-06-01T01:00:00", "2026-06-01T02:00:00")
    rows, _ = browse.find(conn, when="night")
    assert _titles(rows) == ["sat_night"]
    rows, _ = browse.find(conn, weekend=True)
    assert _titles(rows) == ["sat_night"]
    rows, _ = browse.find(conn, weekday=True)
    assert _titles(rows) == ["mon_noon"]  # naive is in neither
    with pytest.raises(ValueError):
        browse.find(conn, when="lunch")
    with pytest.raises(ValueError):
        browse.find(conn, weekday=True, weekend=True)


def test_resolve_active_since():
    from datetime import datetime, timezone

    now = datetime(2026, 10, 22, tzinfo=timezone.utc)
    assert browse.resolve_active_since("2026-10-01") == "2026-10-01"
    assert browse.resolve_active_since("7d", now=now) == "2026-10-15T00:00:00"
    assert browse.resolve_active_since(None, "2w", now=now) == "2026-10-08T00:00:00"
    with pytest.raises(ValueError):
        browse.resolve_active_since("2026-10-01", "7d")
    with pytest.raises(ValueError):
        browse.resolve_active_since(None, "soon")


# ─────────────── date health ───────────────


def test_classify_flags_the_three_gaps(conn):
    a = _add(conn, "no_end", "2026-05-01T10:00:00Z", None)
    b = _add(conn, "naive", "2026-05-01T10:00:00", "2026-05-01T11:00:00")
    c = _add(conn, "epoch", "2025-05-01T10:00:00Z", "1970-01-01T00:00:00Z")
    d = _add(conn, "future", "2999-01-01T00:00:00Z", "2999-01-01T01:00:00Z")
    _add(conn, "fine", "2026-05-01T10:00:00Z", "2026-05-01T11:00:00Z")
    found = dates.classify(conn)
    assert found[dates.NO_END_DATE] == [a]
    assert found[dates.NAIVE_TIMESTAMP] == [b]
    assert sorted(found[dates.IMPLAUSIBLE_DATE]) == sorted([c, d])


def test_flag_tags_idempotently_and_never_changes_the_dates(conn):
    a = _add(conn, "no_end", "2026-05-01T10:00:00Z", None)
    dates.flag(conn)
    dates.flag(conn)
    n = conn.execute(
        "SELECT COUNT(*) FROM tag_links tl JOIN tags t ON t.id = tl.tag_id WHERE t.name = ? AND tl.target_id = ?",
        (dates.NO_END_DATE, a),
    ).fetchone()[0]
    assert n == 1
    assert conn.execute("SELECT ended_at FROM conversations WHERE id = ?", (a,)).fetchone()[0] is None


def test_capture_delay_counts_only_conversations_that_ended_after_first_ingest(conn):
    conn.execute(
        "INSERT INTO source_files (path, sha256, platform, adapter, bytes, ingested_at, status, kind) "
        "VALUES ('p', 's', 'synthetic', 'a', 1, '2026-08-01T00:00:00+00:00', 'ok', 'live')"
    )
    conn.commit()
    old = _add(conn, "before", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")
    late = _add(conn, "after", "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z")
    conn.execute("UPDATE conversations SET ingested_at = '2026-08-01T00:00:00+00:00' WHERE id = ?", (old,))
    conn.execute("UPDATE conversations SET ingested_at = '2026-09-12T00:00:00+00:00' WHERE id = ?", (late,))
    conn.commit()
    (row,) = dates.capture_delay(conn)
    assert row["platform"] == "synthetic"
    assert row["n"] == 1  # the January conversation says nothing about staleness
    assert round(row["max_days"]) == 10


# ─────────────── adapters + reprocess backfill ───────────────


def test_devin_message_timestamp_comes_from_item_meta_and_stays_none_without():
    ts = "cognition.ai/timestamp"
    msg = {"kind": "agent_thought", "payload": {"content": [
        {"type": "text", "text": "b", "_meta": {ts: "2026-08-30T10:00:05+00:00"}},
        {"type": "text", "text": "a", "_meta": {ts: "2026-08-30T10:00:01+00:00"}},
    ]}}
    assert devin_desktop_acp._message_timestamp(msg).startswith("2026-08-30")
    assert devin_desktop_acp._message_timestamp(msg) <= devin_desktop_acp._message_timestamp(
        {"payload": {"content": [{"_meta": {ts: "2026-08-30T10:00:05+00:00"}}]}}
    )
    assert devin_desktop_acp._message_timestamp({"kind": "plan", "payload": {"content": {"entries": []}}}) is None
    assert devin_desktop_acp._message_timestamp({"payload": {"content": [{"text": "x", "_meta": {}}]}}) is None


def test_devin_source_event_ms_is_the_fallback():
    out = devin_desktop_acp._message_timestamp({"payload": {"sourceEventTimestampMs": 1791301875921, "content": {"_meta": {}}}})
    assert out is not None and out[:4] == "2026"


def test_cursor_session_gets_ended_at_from_its_messages(tmp_path: Path):
    f = tmp_path / "s.jsonl"
    f.write_text(
        '{"type":"meta","extraction":"cursor-interactive-sessions-full-v1","session_id":"s1","created":"2025-01-01T00:00:00","title":"t","project":"p"}\n'
        '{"role":"user","text":"hi","iso":"2025-01-01T00:00:00"}\n'
        '{"role":"assistant","text":"yo","iso":"2025-01-03T12:00:00"}\n'
    )
    (conv,) = cursor_legacy_copilot.parse(f)
    assert conv["started_at"] == "2025-01-01T00:00:00Z"
    assert conv["ended_at"] == "2025-01-03T12:00:00Z"


def test_prompts_dump_stays_undated(tmp_path: Path):
    f = tmp_path / "p_prompts.jsonl"
    f.write_text(
        '{"type":"meta","extraction":"cursor-prompts-full-v1","session_id":"p1","title":"t","project":"p"}\n'
        '{"content":[{"text":"a"},{"text":"b"}]}\n'
    )
    (conv,) = cursor_legacy_copilot.parse(f)
    assert conv["ended_at"] is None and conv["started_at"] is None


def test_reprocess_fills_null_timestamps_but_never_overwrites(conn):
    def cir(created):
        return make_conversation(
            platform="synthetic", native_id="x", title="x", project=None,
            started_at=None, ended_at=created[-1], source_path="s", raw={},
            messages=[make_message(i, "user", None, c, [make_block(0, "text", f"m{i}", raw={})], raw={}) for i, c in enumerate(created)],
        )

    first = loader.load_conversation(conn, cir([None, None]), source_file_id=None)
    conn.commit()
    # normal re-ingest: identical content, timestamps untouched
    loader.load_conversation(conn, cir(["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"]), source_file_id=None)
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE conv_id = ? AND created_at IS NOT NULL", (first.conv_id,)).fetchone()[0] == 0
    # --reprocess: filled
    loader.load_conversation(conn, cir(["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"]), source_file_id=None, backfill_timestamps=True)
    got = [r[0] for r in conn.execute("SELECT created_at FROM messages WHERE conv_id = ? ORDER BY idx", (first.conv_id,))]
    assert got == ["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"]
    # and a value already there is never replaced
    loader.load_conversation(conn, cir(["2030-01-01T00:00:00Z", "2030-01-02T00:00:00Z"]), source_file_id=None, backfill_timestamps=True)
    got = [r[0] for r in conn.execute("SELECT created_at FROM messages WHERE conv_id = ? ORDER BY idx", (first.conv_id,))]
    assert got == ["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"]


# ─────────────── rendering ───────────────


@pytest.mark.parametrize("width", [100, 120])
def test_table_keeps_date_columns_whole_at_normal_widths(conn, convs, width):
    from rich.console import Console

    from chatvault import cli

    rows, total = browse.latest(conn, limit=10)
    cli.console = Console(width=width, record=True, force_terminal=False)
    cli._render_find_table("latest", rows, total, days=True)
    out = cli.console.export_text()
    assert "last active" in out and "days ago" in out
    assert "2026-03-31" in out and "2026-10-20" in out  # start and last-active dates both whole
    assert "same day" in out
    assert max(len(line) for line in out.splitlines()) <= width
