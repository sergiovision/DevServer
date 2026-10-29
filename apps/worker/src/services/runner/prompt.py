"""The user message sent to the agent CLI on each attempt.

A fresh session gets the full task: description, acceptance criteria, the
evidence blocks, the approved plan and the house rules. A resumed session
already holds all of that in its history, so it only gets the next message —
the failure to fix, or a nudge to continue. Re-sending ~5K tokens of static
context on every retry was the main cause of vendor rate limits.
"""

from __future__ import annotations

from services import compaction

from .context import TaskContext

_ROLE_BY_TYPE = {
    "test": "an autonomous test-running agent working on repository",
    "script": "an autonomous agent that runs an operator-specified script in repository",
}
_DEFAULT_ROLE = "an autonomous coding agent working on repository"

_TEST_TASK_BLOCK = [
    "## Test task — what to do",
    "This is a TEST task, not a feature-implementation task. The tests "
    "to run are described in the Task Description above.",
    "1. Identify and run exactly the tests the description specifies "
    "(use the project's own test runner — e.g. pytest, npm test, go "
    "test, dotnet test, cargo test).",
    "2. Print the FULL test command and its complete output so the "
    "operator can read it in the Task Log.",
    "3. The task SUCCEEDS only if every test listed under "
    "'Tests that must pass' passes. If any required test fails, "
    "report which ones failed and why — do NOT modify product code to "
    "force a pass unless the description explicitly asks you to fix "
    "failing tests.",
    "4. End with a concise PASS/FAIL summary line.",
]

_SCRIPT_TASK_BLOCK = [
    "## Script task — what to do",
    "This is a SCRIPT task. The Task Description specifies a script to "
    "run: its location, the interpreter/language (bash, python, ts, "
    "…), any parameters, and the working folder.",
    "1. Locate the script at the path given and run it from the "
    "specified working folder with the specified parameters.",
    "2. Use the correct interpreter (e.g. `bash x.sh`, `python x.py`, "
    "`npx tsx x.ts` / `node x.js`). Install nothing unless the "
    "description says to.",
    "3. Print the EXACT command you ran and stream its full stdout/stderr to the Task Log.",
    "4. Report the script's exit code and a short PASS/FAIL summary. Do not edit unrelated files.",
]

_SKIP_VERIFY_BLOCK = [
    "## Verification policy — SKIPPED",
    "The operator has set `skip_verify=true` for this task. Do NOT run",
    "the project's test suite, build, or lint commands. Do NOT spawn",
    "`npm test`, `pytest`, `cargo test`, `go test`, `dotnet test`,",
    "`npm run build`, `tsc`, `eslint`, `ruff`, or equivalent. Read and",
    "edit code freely, but skip the verify step — the operator will",
    "run checks manually after you finish. Proceed straight from the",
    "implementation to the commit.",
]


def build_prompt(ctx: TaskContext, *, notes_block: str = "") -> str:
    """Prompt for the next attempt; resumed sessions get only the delta."""
    if ctx.session_id is not None:
        return _resume_prompt(ctx, notes_block=notes_block)

    parts = _header(ctx) + _context_blocks(ctx) + _instructions(ctx)
    for block in (ctx.skill_prompt, notes_block):
        if block:
            parts.extend(["", block])
    if ctx.gate_prompt:
        parts.append(ctx.gate_prompt)
    if ctx.error_context:
        parts.extend(["", ctx.error_context])
    return "\n".join(parts)


def _resume_prompt(ctx: TaskContext, *, notes_block: str) -> str:
    if ctx.error_context:
        message = ctx.error_context
    elif ctx.git_flow == "untracked":
        message = (
            "Continue with the previous task. Resume where you left off "
            "and finish the implementation. Do NOT commit — leave all "
            "changes as uncommitted edits in the working tree."
        )
    else:
        message = (
            "Continue with the previous task. Resume where you left off "
            "and finish the implementation. Commit your changes when done."
        )
    return f"{message}\n\n{notes_block}" if notes_block else message


def _header(ctx: TaskContext) -> list[str]:
    task = ctx.task
    role = _ROLE_BY_TYPE.get(task.task_type, _DEFAULT_ROLE)
    parts = [
        f"You are {role}: {ctx.repo.name}",
        f"Branch: {ctx.branch_name}",
        f"Task: {task.key} - {task.title}",
        "",
        "## Task Description",
        task.description or "(no description provided)",
    ]
    if task.task_type not in ("skill", "research"):
        heading = (
            "## Tests that must pass" if task.task_type == "test" else "## Acceptance Criteria"
        )
        parts += ["", heading, task.acceptance or "(none specified)"]
    return parts


def _context_blocks(ctx: TaskContext) -> list[str]:
    """Evidence blocks, or the compacted summary that replaces them."""
    compacted = compaction.build_compacted_prompt_block(ctx.compacted_context)
    if compacted:
        blocks = [compacted]
    else:
        evidence = ctx.evidence
        blocks = [evidence.repo_map_text, evidence.reality_text, evidence.memory_text]
    blocks.append(ctx.approved_plan_text)
    parts: list[str] = []
    for block in blocks:
        if block:
            parts.extend(["", block])
    return parts


def _instructions(ctx: TaskContext) -> list[str]:
    if ctx.git_flow == "untracked":
        commit_rule = (
            "6. Do NOT create branches, do NOT commit and do NOT push — leave "
            "every change as uncommitted edits in the working tree"
        )
    else:
        commit_rule = f"6. Commit your changes with a clear message referencing {ctx.task.key}"
    parts = [
        "",
        "## Instructions",
        "1. Read the CLAUDE.md file first for project context and conventions",
        "2. Understand the existing codebase before making changes",
        "3. Implement the task following existing patterns and conventions",
        "4. Make minimal, focused changes -- only what the task requires",
        "5. Do NOT add extra features, refactoring, or improvements beyond the task",
        commit_rule,
        "7. Ensure all acceptance criteria are met",
    ]
    if ctx.repo.is_local:
        parts.append(
            "8. This is a LOCAL-ONLY repository: NEVER run `git push`, never "
            "add or change git remotes, and never open pull requests"
        )
    if ctx.task.task_type == "test":
        parts += ["", *_TEST_TASK_BLOCK]
    elif ctx.task.task_type == "script":
        parts += ["", *_SCRIPT_TASK_BLOCK]
    if ctx.task.skip_verify:
        parts += ["", *_SKIP_VERIFY_BLOCK]
    return parts
