# Task types

The type is picked **when the task is created and cannot be changed afterwards**
— it decides which runner executes the task and which fields the form shows.

| Type | Repo | What it does | Output |
|---|---|---|---|
| `coding` | required | The full pipeline: worktree → agent → verifier → PR | PR / patch |
| `test` | required | The agent runs the tests itself; the worker verifier is skipped | Test results in the Task Log |
| `script` | required | The agent runs a script or command and reports what happened | Script output in the Task Log |
| `skill` | optional | A lightweight run in a scratch directory — no commit, no PR | `RESULT.md` + Task Log |
| `research` | optional | The description *is* the prompt; the answer is the deliverable | Answer in the Task Log |

## Choosing

- **coding** — anything that should end in reviewable code. This is the default.
- **test** — "run the integration suite against branch X and tell me what
  broke". The agent runs the tests, so the worker does not run them a second
  time. Acceptance criteria are relabelled *"Which tests must pass"*.
- **script** — "run the migration script and report the output". Same deal: the
  agent executes, the worker does not re-verify.
- **skill** — a task backed by a reusable SKILL.md prompt, run in a scratch
  directory. It never commits or pushes. If you attach a repo, it runs *inside*
  that repo (read-only in spirit) so it sees the right codebase.
- **research** — questions, comparisons, investigations. No code changes at all;
  you read the answer in the Task Log.

## Related switches

- **Skip verify** (coding only) — tells the agent not to bother running
  build/test/lint. Use it for docs and config changes where the suite is
  irrelevant and slow. It is hidden for `test` and `script`, which bypass the
  worker verifier by design but are still expected to test their own work.
- **Max turns** — how many agent turns before the run stops. `-1` means
  unlimited. A task that repeatedly hits the ceiling is usually
  under-specified rather than under-budgeted.
- **Mode: interactive** — the agent produces a plan first and waits for
  you to approve it before touching any code.

Templates carry a task type, so applying a template also sets the type.
