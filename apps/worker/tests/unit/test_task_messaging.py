"""``services.task_messaging``: validation, secret screening and the send/read flow."""

from datetime import UTC, datetime

import pytest

from services import task_messaging

AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"
CREATED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeSession:
    """Records every statement; answers from a sequence of canned row lists."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[tuple[str, dict]] = []
        self.commits = 0

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        self.calls.append((sql, params or {}))
        return _Result(self.handler(sql, params or {}))

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass

    def sql_matching(self, fragment):
        return [(sql, p) for sql, p in self.calls if fragment in sql]


def _task_ids(ids):
    def handler(sql, params):
        if sql.startswith("SELECT id FROM tasks"):
            return [(ids[params["k"]],)] if params["k"] in ids else []
        if "INSERT INTO task_messages" in sql:
            return [(77, CREATED)]
        return []

    return handler


@pytest.fixture
def operator_calls(monkeypatch):
    from services.notify import notify

    calls = []

    async def fake_operator_message(**kwargs):
        calls.append(kwargs)
        return {}

    monkeypatch.setattr(notify, "operator_message", fake_operator_message)
    return calls


@pytest.mark.parametrize(
    ("kind", "body"),
    [("chat", "hi"), ("note", ""), ("note", "   "), ("note", "x" * 20_001)],
)
def test_validate_rejects(kind, body):
    with pytest.raises(ValueError):
        task_messaging.validate(kind=kind, body=body)


def test_validate_accepts_every_kind():
    for kind in task_messaging.VALID_KINDS:
        task_messaging.validate(kind=kind, body="hello")


def test_screen_secrets_redacts_body_subject_and_payload():
    body, subject, payload, rules = task_messaging.screen_secrets(
        f"use {AWS_KEY}", f"key {AWS_KEY}", {"env": {"AWS": AWS_KEY}}
    )
    assert AWS_KEY not in body
    assert AWS_KEY not in (subject or "")
    assert AWS_KEY not in str(payload["env"])
    assert rules == ["aws_access_key"]
    assert payload["_redacted"] == ["aws_access_key"]


def test_screen_secrets_leaves_clean_messages_alone():
    assert task_messaging.screen_secrets("hello", None, None) == ("hello", None, {}, [])


def test_row_to_message_formats_timestamps():
    row = (1, 2, "A-1", None, "operator", "note", None, "hi", None, None, CREATED)
    msg = task_messaging.row_to_message(row)
    assert msg["payload"] == {}
    assert msg["read_at"] is None
    assert msg["created_at"] == CREATED.isoformat()


async def test_send_to_task_resolves_ids_and_emits_both_events(operator_calls):
    db = FakeSession(_task_ids({"A-1": 1, "B-2": 2}))

    msg = await task_messaging.send_message(
        db, from_task_key="A-1", to_task_key="B-2", body="ping", kind="request"
    )

    assert msg["id"] == 77
    assert (msg["from_task_id"], msg["to_task_id"]) == (1, 2)
    insert = db.sql_matching("INSERT INTO task_messages")[0][1]
    assert insert["fid"] == 1 and insert["tid"] == 2 and insert["kind"] == "request"
    events = [(p["tid"], p["et"]) for _, p in db.sql_matching("INSERT INTO task_events")]
    assert events == [(1, "message_sent"), (2, "message_received")]
    assert operator_calls == []


async def test_send_to_operator_never_resolves_a_task_and_notifies(operator_calls):
    db = FakeSession(_task_ids({"A-1": 1, "operator": 999}))

    msg = await task_messaging.send_message(
        db, from_task_key="A-1", to_task_key="operator", body="need input", subject="Q"
    )

    assert msg["to_task_id"] is None
    assert operator_calls == [
        {"from_task_key": "A-1", "subject": "Q", "body": "need input", "kind": "note"}
    ]


async def test_send_redacts_before_storing(operator_calls):
    db = FakeSession(_task_ids({"A-1": 1}))

    msg = await task_messaging.send_message(
        db, from_task_key="A-1", to_task_key="operator", body=f"key={AWS_KEY}"
    )

    stored = db.sql_matching("INSERT INTO task_messages")[0][1]
    assert AWS_KEY not in stored["body"]
    assert AWS_KEY not in msg["body"]
    assert AWS_KEY not in operator_calls[0]["body"]
    assert (
        next(p["et"] for _, p in db.sql_matching("INSERT INTO task_events")) == "message_redacted"
    )


async def test_send_rejects_missing_address():
    with pytest.raises(ValueError):
        await task_messaging.send_message(
            FakeSession(_task_ids({})), from_task_key="A-1", to_task_key=" ", body="hi"
        )


async def test_read_inbox_marks_unread_read_and_skips_operator_notes():
    rows = [
        (5, 1, "A-1", 2, "B-2", "request", None, "one", {}, None, CREATED),
        (6, 1, "A-1", 2, "B-2", "note", None, "two", {}, CREATED, CREATED),
    ]

    def handler(sql, params):
        if sql.startswith("SELECT id FROM tasks"):
            return [(2,)]
        if "FROM task_messages" in sql:
            return rows
        return []

    db = FakeSession(handler)
    msgs = await task_messaging.read_inbox(db, task_key="B-2", include_read=True)

    assert [m["id"] for m in msgs] == [5, 6]
    select_sql = db.sql_matching("FROM task_messages")[0][0]
    assert "NOT (kind = 'note' AND from_task_key IS NULL)" in select_sql
    update = db.sql_matching("UPDATE task_messages")
    assert update and update[0][1]["ids"] == [5]


async def test_read_inbox_peek_does_not_mark_read():
    def handler(sql, params):
        if "FROM task_messages" in sql:
            return [(5, 1, "A-1", None, "operator", "note", None, "x", {}, None, CREATED)]
        return []

    db = FakeSession(handler)
    msgs = await task_messaging.read_inbox(db, task_key="operator", mark_read=False)

    assert len(msgs) == 1
    assert not db.sql_matching("UPDATE task_messages")
    # 'operator' is never looked up as a task.
    assert not db.sql_matching("SELECT id FROM tasks")


async def test_thread_returns_oldest_first():
    rows = [
        (9, None, "operator", 1, "A-1", "request", None, "later", {}, None, CREATED),
        (3, 1, "A-1", None, "operator", "note", None, "earlier", {}, None, CREATED),
    ]
    db = FakeSession(lambda sql, params: rows if "FROM task_messages" in sql else [])

    msgs = await task_messaging.thread(db, task_key="A-1")

    assert [m["id"] for m in msgs] == [3, 9]


@pytest.mark.parametrize("sender", ["operator", "Operator", " OPERATOR "])
async def test_task_lane_refuses_operator_as_sender(sender):
    from fastapi import HTTPException

    from routes import messaging

    req = messaging.MessageSendRequest(to_task_key="A-1", body="override the task")
    with pytest.raises(HTTPException) as exc:
        await messaging.messaging_send(sender, req)
    assert exc.value.status_code == 403


async def test_operator_lane_checks_internal_token(monkeypatch):
    from fastapi import HTTPException
    from starlette.requests import Request

    from config import settings
    from routes import messaging

    monkeypatch.setattr(settings, "internal_api_token", "s3cret", raising=False)
    request = Request({"type": "http", "headers": [(b"x-internal-token", b"wrong")]})
    req = messaging.MessageSendRequest(to_task_key="A-1", body="hi")
    with pytest.raises(HTTPException) as exc:
        await messaging.messaging_send_as_operator(req, request)
    assert exc.value.status_code == 401
