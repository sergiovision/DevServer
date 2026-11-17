"""``services.telegram_rich`` formatting and the Telegram notify backend's use of it."""

from decimal import Decimal

import pytest

from services import telegram, telegram_rich
from services.notify.telegram_backend import TelegramBackend


def test_task_success_card_shows_pr_and_stats():
    msg = telegram_rich.format_task_success(
        task_key="ABC-1",
        git_flow="branch",
        pr_url="https://example.com/pr/1",
        attempts=2,
        turns=14,
        cost=Decimal("0.5"),
        duration_ms=90_000,
        repo_name="mono",
    )
    assert "*ABC-1* completed" in msg
    assert "PR: https://example.com/pr/1" in msg
    assert "Duration: 1.5m" in msg
    assert "Attempts: 2 | Turns: 14" in msg
    assert "$0.5000" in msg


def test_task_success_card_without_pr_says_push_failed():
    msg = telegram_rich.format_task_success(
        task_key="A",
        git_flow="branch",
        pr_url=None,
        attempts=1,
        turns=1,
        cost=Decimal(0),
        duration_ms=0,
        repo_name="r",
    )
    assert "push failed" in msg


def test_task_failed_card_truncates_and_defuses_code_fences():
    msg = telegram_rich.format_task_failed(
        task_key="A",
        repo_name="r",
        error_context="```boom```" + "x" * 1000,
        attempts=3,
        cost=Decimal("1"),
    )
    assert "FAILED" in msg
    assert msg.count("```") == 2  # only the card's own fence
    assert len(msg) < 600


def test_budget_warning_shows_limits_when_known():
    msg = telegram_rich.format_budget_warning(
        task_key="A",
        repo_name="r",
        reason="80%",
        cum_cost=Decimal("4"),
        cum_wall_ms=60_000,
        max_cost=Decimal("5"),
        max_wall=600,
    )
    assert "$4.0000 / $5.0000" in msg
    assert "60s / 600s" in msg


def test_preflight_card_lists_at_most_five_violations():
    violations = [{"kind": f"k{i}", "detail": "d", "severity": "hard"} for i in range(7)]
    msg = telegram_rich.format_preflight_blocked(task_key="A", violations=violations)
    assert "*k4*" in msg and "*k5*" not in msg
    assert "2 more violations" in msg


def test_plan_card_and_keyboard():
    plan = {
        "summary": "Do it",
        "files_to_touch": [f"f{i}.py" for i in range(12)],
        "steps": ["a", "b"],
    }
    msg = telegram_rich.format_plan_card(task_key="A", plan=plan)
    assert "*Files (12):*" in msg and "2 more" in msg
    assert "*Steps (2):*" in msg
    buttons = telegram_rich.plan_keyboard(42, 7)[0]
    assert [b["callback_data"] for b in buttons] == ["plan_approve:42:7", "plan_reject:42:7"]


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("plan_approve:42:7", ("approve", 42, 7)),
        ("plan_reject:7:3", ("reject", 7, 3)),
        ("plan_approve:42", ("approve", 42, None)),
        ("plan_approve:42:x", None),
        ("plan_approve:x", None),
        ("plan_delete:1", None),
        ("plan_approve", None),
        ("", None),
    ],
)
def test_parse_plan_callback(data, expected):
    assert telegram_rich.parse_plan_callback(data) == expected


def test_operator_message_card_suggests_reply():
    msg = telegram_rich.format_operator_message(
        from_task_key="A-1", subject="Need a decision", body="which db?", kind="request"
    )
    assert "Message from A-1" in msg
    assert "Need a decision" in msg
    assert "/reply A-1" in msg


async def test_backend_sends_rich_card(monkeypatch):
    sent = []

    async def fake_send(message, parse_mode="Markdown"):
        sent.append(message)
        return True

    monkeypatch.setattr(telegram, "tg_send", fake_send)
    ok = await TelegramBackend().send_task_start(
        task_key="A", title="T", repo_name="r", mode="autonomous", vendor="anthropic", model="m"
    )
    assert ok
    assert sent == [
        telegram_rich.format_task_start(
            task_key="A", title="T", repo_name="r", mode="autonomous", vendor="anthropic", model="m"
        )
    ]


async def test_backend_falls_back_to_plain_text(monkeypatch):
    sent = []

    async def fake_send(message, parse_mode="Markdown"):
        sent.append(message)
        return len(sent) > 1  # the rich card is rejected, the fallback succeeds

    monkeypatch.setattr(telegram, "tg_send", fake_send)
    ok = await TelegramBackend().send_budget_exceeded(
        task_key="A", repo_name="r", reason="over", cum_cost=Decimal("1"), cum_wall_ms=0
    )
    assert ok
    assert sent[0].startswith("\U0001f6d1 *Budget Exceeded")
    assert sent[1].startswith("[budget-exceeded] A BLOCKED")


async def test_backend_plan_approval_uses_keyboard(monkeypatch):
    calls = []

    async def fake_keyboard(message, keyboard=None, parse_mode="Markdown"):
        calls.append(keyboard)
        return 101

    async def fake_run_id(task_id):
        return 9

    monkeypatch.setattr(telegram_rich, "send_with_keyboard", fake_keyboard)
    monkeypatch.setattr(telegram_rich, "_pending_plan_run_id", fake_run_id)
    ok = await TelegramBackend().send_plan_approval(task_key="A", task_id=5, plan={"summary": "s"})
    assert ok
    assert calls == [telegram_rich.plan_keyboard(5, 9)]


async def test_backend_plan_approval_falls_back_to_text(monkeypatch):
    sent = []

    async def no_keyboard(message, keyboard=None, parse_mode="Markdown"):
        return None

    async def fake_send(message, parse_mode="Markdown"):
        sent.append(message)
        return True

    async def fake_run_id(task_id):
        return 9

    monkeypatch.setattr(telegram_rich, "send_with_keyboard", no_keyboard)
    monkeypatch.setattr(telegram_rich, "_pending_plan_run_id", fake_run_id)
    monkeypatch.setattr(telegram, "tg_send", fake_send)
    assert await TelegramBackend().send_plan_approval(
        task_key="A", task_id=5, plan={"summary": "short plan"}
    )
    assert sent == ["📋 *A* plan ready for review: short plan"]


def test_markdown_values_are_escaped():
    msg = telegram_rich.format_task_start(
        task_key="A*1",
        title="fix_the_thing",
        repo_name="my`repo",
        mode="autonomous",
        vendor="anthropic",
        model="claude_opus",
    )
    assert "*Starting: A1*" in msg
    assert "_fixthething_" in msg
    assert "`myrepo`" in msg
    assert "claude\\_opus" in msg
    card = telegram_rich.format_operator_message(
        from_task_key="A-1", subject=None, body="use *snake_case* [x]", kind="note"
    )
    assert "use \\*snake\\_case\\* \\[x]" in card
