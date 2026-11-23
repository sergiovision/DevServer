"""Specification: a reopened Ask Agent conversation keeps its context on the agent engine."""

from routes.assistant import fold_transcript_into_first_turn


# Feature: F-ASK-11
def test_reopened_conversation_carries_its_transcript_into_the_first_agent_turn() -> None:
    folded = fold_transcript_into_first_turn(
        [("user", "What is DS-1?"), ("assistant", "A coding task.")], "Is it done?"
    )
    assert "Operator: What is DS-1?" in folded
    assert "Assistant: A coding task." in folded
    assert folded.endswith("Current question:\nIs it done?")


# Feature: F-ASK-11
def test_without_history_the_message_is_sent_unchanged() -> None:
    assert fold_transcript_into_first_turn([("user", "  "), ("assistant", "")], "Hi") == "Hi"
