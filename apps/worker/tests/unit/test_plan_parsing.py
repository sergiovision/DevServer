"""``services.plan_gate.parse_plan``: turning an agent reply into a Plan."""

import pytest

from services.plan_gate import Plan, PlanParseError, parse_plan


def test_valid_json_plan_in_prose_and_fences():
    reply = (
        "Here is my plan:\n```json\n"
        '{"summary": "Add retry", "steps": ["write helper", "use it"],'
        ' "files_to_touch": ["src/retry.py"], "risks": ["timing"]}\n```'
    )

    plan = parse_plan(reply)

    assert plan == Plan(
        summary="Add retry",
        steps=("write helper", "use it"),
        files_to_touch=("src/retry.py",),
        risks=("timing",),
    )


def test_non_string_items_and_wrong_types_are_dropped():
    plan = parse_plan('{"summary": "S", "steps": "not a list", "files_to_touch": ["a.py", {}]}')
    assert plan.steps == ()
    assert plan.files_to_touch == ("a.py",)


def test_malformed_json_salvages_first_paragraph_as_summary():
    reply = "Refactor the parser into two modules.\n\nThen {broken json"

    plan = parse_plan(reply)

    assert plan.summary == "Refactor the parser into two modules."
    assert plan.files_to_touch == ()


@pytest.mark.parametrize("reply", ["", "   \n  "])
def test_empty_reply_is_rejected(reply):
    with pytest.raises(PlanParseError):
        parse_plan(reply)


def test_render_for_prompt_lists_allowed_files():
    text = Plan(summary="S", files_to_touch=("a.py",)).render_for_prompt()
    assert "## Approved Plan" in text
    assert "- a.py" in text
