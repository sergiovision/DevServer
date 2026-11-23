"""Post-completion actions: when a finished task fires its follow-up.

Pure tests over :func:`services.post_completion.decide_post_completion` and
:func:`services.post_completion.parse_action` — no database, no network. They
pin the trigger matrix and the loop guard (self-reference, chain loops, depth
cap), the parts a wrong answer would turn into a runaway chain of runs.
"""

from __future__ import annotations

import pytest

from services.post_completion import (
    MAX_CHAIN_DEPTH,
    SUSPENDED,
    PostCompletionAction,
    Trigger,
    decide_post_completion,
    parse_action,
)

SOURCE_ID = 10
TARGET_ID = 20


def _task_action(trigger: Trigger, task_id: int = TARGET_ID) -> PostCompletionAction:
    return PostCompletionAction(kind="task", trigger=trigger, task_id=task_id)


# status → (fires on_success, fires on_failure, fires always)
_MATRIX: dict[str, tuple[bool, bool, bool]] = {
    "test": (True, False, True),
    "done": (True, False, True),
    "failed": (False, True, True),
    "blocked": (False, True, True),
    "cancelled": (False, False, False),
    SUSPENDED: (False, False, False),
    "running": (False, False, False),
    "pending": (False, False, False),
}


# Feature: F-PCA-02
@pytest.mark.parametrize(
    ("status", "trigger", "expected"),
    [
        (status, trigger, fires[index])
        for status, fires in _MATRIX.items()
        for index, trigger in enumerate(("on_success", "on_failure", "always"))
    ],
)
def test_trigger_matrix_fires_only_for_matching_terminal_statuses(
    status: str, trigger: Trigger, expected: bool
) -> None:
    decision = decide_post_completion(_task_action(trigger), status, SOURCE_ID, [])
    assert decision.fire is expected, decision.reason


# Feature: F-PCA-02
def test_skill_action_fires_like_a_task_action() -> None:
    action = PostCompletionAction(kind="skill", trigger="on_success", skill_name="review")
    assert decide_post_completion(action, "test", SOURCE_ID, []).fire


# Feature: F-PCA-02
def test_no_action_never_fires() -> None:
    assert not decide_post_completion(None, "test", SOURCE_ID, []).fire


# Feature: F-PCA-03
@pytest.mark.parametrize("trigger", ["on_success", "on_failure", "always"])
def test_task_that_targets_itself_is_refused(trigger: Trigger) -> None:
    for status in ("test", "failed"):
        decision = decide_post_completion(
            _task_action(trigger, task_id=SOURCE_ID), status, SOURCE_ID, []
        )
        assert not decision.fire
        assert "itself" in decision.reason


# Feature: F-PCA-03
def test_target_already_in_the_chain_is_refused_as_a_loop() -> None:
    decision = decide_post_completion(_task_action("always"), "test", SOURCE_ID, [TARGET_ID])
    assert not decision.fire
    assert "loop" in decision.reason


# Feature: F-PCA-03
def test_cyclic_chain_is_refused() -> None:
    decision = decide_post_completion(_task_action("always"), "test", SOURCE_ID, [30, SOURCE_ID])
    assert not decision.fire
    assert "loop" in decision.reason


# Feature: F-PCA-04
def test_depth_cap_is_three() -> None:
    assert MAX_CHAIN_DEPTH == 3


# Feature: F-PCA-04
def test_chain_below_the_cap_still_fires() -> None:
    # Source is two hops from an operator-started task; the target is hop 3.
    decision = decide_post_completion(_task_action("always"), "test", SOURCE_ID, [31, 32])
    assert decision.fire, decision.reason


# Feature: F-PCA-04
def test_chain_at_the_cap_is_refused() -> None:
    decision = decide_post_completion(_task_action("always"), "test", SOURCE_ID, [31, 32, 33])
    assert not decision.fire
    assert "depth" in decision.reason


# Feature: F-PCA-04
def test_depth_cap_applies_to_skill_actions_too() -> None:
    action = PostCompletionAction(kind="skill", trigger="always", skill_name="review")
    assert not decide_post_completion(action, "failed", SOURCE_ID, [31, 32, 33]).fire


# Feature: F-PCA-05
@pytest.mark.parametrize("trigger", ["on_success", "on_failure", "always"])
def test_cancelled_never_fires(trigger: Trigger) -> None:
    for action in (
        _task_action(trigger),
        PostCompletionAction(kind="skill", trigger=trigger, skill_name="review"),
    ):
        decision = decide_post_completion(action, "cancelled", SOURCE_ID, [])
        assert not decision.fire
        assert "cancelled" in decision.reason


# Feature: F-PCA-01
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            {"kind": "task", "task_id": 5, "trigger": "on_success"},
            PostCompletionAction(kind="task", trigger="on_success", task_id=5),
        ),
        (
            '{"kind": "skill", "skill_name": " review ", "trigger": "always"}',
            PostCompletionAction(kind="skill", trigger="always", skill_name="review"),
        ),
    ],
)
def test_parse_action_accepts_valid_shapes(raw: object, expected: PostCompletionAction) -> None:
    assert parse_action(raw) == expected


# Feature: F-PCA-01
@pytest.mark.parametrize(
    "raw",
    [
        None,
        "not json",
        [],
        {"kind": "script", "trigger": "always"},
        {"kind": "task", "trigger": "always"},
        {"kind": "task", "task_id": True, "trigger": "always"},
        {"kind": "task", "task_id": 0, "trigger": "always"},
        {"kind": "skill", "skill_name": "  ", "trigger": "always"},
        {"kind": "task", "task_id": 5, "trigger": "sometimes"},
    ],
)
def test_parse_action_rejects_malformed_values(raw: object) -> None:
    assert parse_action(raw) is None
