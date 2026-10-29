"""The coding-task runner, split into phases.

``agent_runner.run_task`` orchestrates; this package does the work:

- :mod:`.context`    — immutable snapshots of the task and repo plus the
  mutable per-run state (:class:`~.context.TaskContext`).
- :mod:`.store`      — the small database operations the runner needs, each
  in its own short-lived session.
- :mod:`.phases`     — prepare the workspace, gather evidence, run attempts,
  clean up.
- :mod:`.attempt`    — one attempt: budget → agent → verify → preflight → git.
- :mod:`.failures`   — classify a failed attempt: retry, back off or escalate.
- :mod:`.results`    — the verdict vocabulary shared by the modules above.
- :mod:`.evidence`, :mod:`.prompt`, :mod:`.preflight`, :mod:`.delivery` —
  the building blocks an attempt is made of.
- :mod:`.agent_call` — spawning one vendor CLI invocation.
- :mod:`.outcome`    — the terminal status, daily stats and notification,
  written once per task.
"""
