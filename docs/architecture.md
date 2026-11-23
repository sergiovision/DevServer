# DevServer architecture

A walkthrough of how a coding task becomes a pull request, written for a
reviewer who wants to know *where things are and why they are shaped that way*.
For the decision log behind the shapes, see [design-decisions.md](design-decisions.md).

> DevServer is the open-source MVP, and this is its blueprint. Use it to build your own pipeline, or see what the same design became in [DevAgentForce →](https://github.com/sergiovision/DevAgentForce)

## 1. The moving parts

| Process | Code | Responsibility |
|---|---|---|
| Web | `apps/web` (Next.js) | Dashboard, REST API, the **only producer** of queue jobs, WebSocket relay of task events |
| Worker | `apps/worker` (FastAPI) | Queue consumer, the task runner, internal API under `/internal/*` |
| PostgreSQL | `database/migrations` | Domain data, the PgQueuer job table, `LISTEN/NOTIFY` for live events |

There is no broker besides PostgreSQL. A task event is a row in `task_events`;
a trigger issues `NOTIFY`, and the web process forwards it to the browser.

## 2. From job to run

```
pgqueuer row ──► queue_consumer.execute_job
                   │  RunningTasks.track(task_id, asyncio.Task)
                   ▼
                 agent_runner.run_task
                   │  repo_lock.hold(repo, owner=task_key)   ◄─ lease + heartbeat
                   │  TaskLog(log_dir, task_key)
                   ▼
                 phases.prepare_workspace → gather_evidence → run_all_attempts
                   │                                              │
                   │                                  outcome.record(result)
                   ▼
                 phases.finalise (WIP commit, reset / restore branch)
```

* **`RepoLockBusy`** escapes `run_task` *before* anything is written. The
  consumer re-enqueues the same payload with `execute_after = 60 s` and points
  `tasks.queue_job_id` at the new job: a busy repo is a delay, not a failure.
* **`asyncio.CancelledError`** is the cancellation path. `task_control.cancel`
  marks the task cancelled and calls `RunningTasks.cancel(task_id)`; the
  cancellation travels down to `proc.run`, which kills the agent's process group;
  `run_task` records a terminal status and re-raises; the consumer recognises an
  operator cancel (as opposed to shutdown) and ends the job normally.

## 3. The runner package (`services/runner/`)

| Module | Owns | Notes |
|---|---|---|
| `context.py` | `TaskSpec`, `RepoSpec` (frozen snapshots), `TaskContext` (mutable run state) | No phase ever holds an ORM object, and so never a DB session. |
| `store.py` | `emit_event`, `set_task_status`, `start_run`, `finish_run`, `add_daily_stat`, … | One short session per call; `emit_event` is also the OpenTelemetry choke point. |
| `phases.py` | `prepare_workspace`, `gather_evidence`, `run_attempts`, `run_all_attempts`, `finalise` | The backup model is just a second `run_attempts` call after `_switch_to_backup`. |
| `evidence.py` | repo map, reality gate, abstain gate, memory recall | Each source is optional; only the opt-in abstain gate can stop a run. |
| `prompt.py` | `build_prompt(ctx, notes_block=)` | A resumed session gets only the delta (the failure hint, notes). |
| `attempt.py` | `run_attempt` | budget → notes → prompt → agent → gate check → verify → preflight → handover. |
| `failures.py` | `agent_failed`, `verify_failed`, `preflight_failed` | Classify, hint, and decide retry / back off / escalate. |
| `results.py` | `Verdict`, `AttemptResult`, `Success` | The vocabulary every other module speaks. |
| `preflight.py`, `delivery.py` | PR preflight bookkeeping; branch / commit / patch / untracked handover | |
| `outcome.py` | `record`, `record_cancelled`, `record_crash` | Terminal status, daily stats, notification, success memory, patch export — once. |
| `agent_call.py` | `AgentInvocation`, `run_agent` | Install-on-first-use, spawn with timeout, 429 retry via `retry_async`. |
| `task_log.py` | `TaskLog` | The per-task log file the dashboard tails. |

### Why a `Verdict` enum

The original loop expressed "what next" with `break`, `continue` and a handful
of booleans (`success`, `budget_blocked`, …) spread over 1,600 lines; one of
those paths let a preflight *hard* failure be overwritten as `failed` and then
retried on the backup model. Now every attempt returns a value:

```python
match result.verdict:
    case Verdict.SUCCEEDED:       ...  # outcome writes status 'test'
    case Verdict.SUSPENDED:       ...  # side-effect gate already parked it
    case Verdict.BLOCKED:         ...  # preflight hard failure — terminal
    case Verdict.BUDGET_EXCEEDED: ...  # terminal, no failover
    case Verdict.RETRY | Verdict.ESCALATE: ...  # exhausted — maybe the backup
```

Illegal transitions become impossible to write instead of merely unlikely.

### Prompt hygiene: untrusted text is fenced (B1)

Task text comes from people and systems DevServer does not control (webhook
payloads, A2A peers, imported pages). Two layers keep it from steering the
agent, and neither ever refuses a task:

* **Ingress scan** — `services/injection_scan.py` and its TS twin
  `apps/web/src/lib/injection-scan.ts` (kept in sync by the shared cases in
  `apps/worker/tests/fixtures/injection_cases.json`) strip zero-width, bidi
  and Unicode-tag characters and *flag* override phrases ("ignore all previous
  instructions", "you are now …", "system prompt:") and imperatives hidden in
  `<!-- … -->`. The web tier runs it in `POST /api/tasks`, webhook firing and
  A2A task creation via `lib/injection-ingress.ts` and records an
  `injection_sanitised` task event when anything was found; import adapters
  return drafts through `import_sources.base.sanitised_draft`, which stores the
  same payload under `metadata.injection_scan`.
* **Prompt fence** — `runner/prompt.build_prompt` puts the description and
  acceptance criteria in `<untrusted_task>` and failure evidence in
  `<untrusted_evidence>`, entity-escaped (`& < >`) so a literal
  `</untrusted_task>` cannot close the fence, after a one-line rule that fenced
  text is data. Resume prompts stay delta-only: the rule plus the fenced
  failure. Operator notes are deliberately not fenced — they are meant to
  amend the task.

### Action on completion (B2)

`tasks.post_completion_action` (JSONB, migration 004; templates carry it too
and the task form copies it) names one follow-up:
`{kind: 'task', task_id, trigger}` re-runs another task,
`{kind: 'skill', skill_name, trigger}` creates a new `skill` task in the same
repo; `trigger` is `on_success` | `on_failure` | `always`. There is no
`script` kind. The web tier validates every write with
`lib/post-completion-action.ts` (`PATCH` / `POST /api/tasks`, the template
routes); a malformed value is a 400.

`phases.finalise` calls `services/post_completion.after_completion(task_id)`
after the terminal status is written. It reads the row inline (no action → no
work, no event) and detaches the rest, so the follow-up never delays or fails
the source and is enqueued after the repo lock is released. The pure
`decide_post_completion(action, status, task_id, ancestors)` decides:
`test`/`done` are success, `failed`/`blocked` failure; `cancelled`, and a task
parked by the side-effect gate, never fire. Loop guard: a task never targets
itself or a task already in its chain, and the `triggered_by_task_id` chain
stops after 3 automatic hops. A `task` target is reset to `pending` only when it
is not queued/running/verifying (one guarded `UPDATE`); both kinds set
`triggered_by_task_id` and go through `queue_bridge.enqueue_via_web`. Every
outcome — fired, skipped and why, error — is a `post_completion_action` event
on the source task. Lightweight tasks (`skill` / `research`) bypass
`finalise`; `skill_runner.run_lightweight_task` makes the same call once the
repo lock is released.

The chain link survives only enqueues the worker marks as triggered
(`enqueue_via_web(..., triggered=True)` → `X-DevServer-Triggered: 1`). Any
other enqueue — the dashboard's Run, MCP `task_run`, a schedule — is a fresh
start, and `POST /api/tasks/[id]/enqueue` clears `triggered_by_task_id` so an
old chain never counts against the depth cap.

Upgrading: the Docker bootstrap only migrates an empty database, so an
existing install needs `./scripts/migrate.sh` for 004. Until then the hook
logs one warning per finished task and does nothing.

## 4. Concurrency and failure model

| Risk | Mechanism |
|---|---|
| Two tasks in one checkout | `repo_lock.hold`: insert-if-free with a 5-minute TTL, renewed every 60 s by a heartbeat task. |
| A crashed worker holding a repo | The lease expires; the next `try_acquire` deletes expired rows first. |
| Recovering a dead task without touching a live one | Stale recovery selects active tasks past their timeout **with no live lease**, resets them to `pending`, re-enqueues them and deletes only *their* lock rows. |
| A hung CLI or git remote | `proc.run(timeout=…)` everywhere; `git_ops._run` has a *required* `timeout` with local/network defaults. |
| An agent that forks helpers | Children start in a new session; timeout and cancel kill the whole process group. |
| Long DB transactions | None: sessions are opened per unit of work and never span a subprocess or a sleep. |
| Retry storms | `Backoff` schedules with jitter; a task-level circuit breaker on cost and wall time. |

## 5. Extension points

* **A new vendor** — subclass `AgentBackend` (argv, env, output parsing, 429
  detection), `register_backend()` it, add its `ModelInfo` entries.
* **A new notification channel** — implement `NotifyBackend.send_text` (and
  optionally rich per-event methods) and add it to the dispatcher.
* **A new import source** — an adapter under `services/import_sources/`.
* **A new pre-run signal** — a function in `runner/evidence.py` that fills a
  field of `Evidence`; `prompt.py` decides where it goes.

## 6. Testing strategy

* **Characterisation tests** (`tests/runner/test_run_task.py`) were written
  against the old runner first and kept unchanged through the refactor. They
  use the real schema in PostgreSQL, a real temporary git repository and
  `fake_agent.py` — a scripted subprocess that plays the vendor CLI (records the
  prompt it received, edits files, sleeps, exits with a chosen code).
* **Regression tests** for each correctness fix (`test_correctness_fixes.py`):
  lease heartbeat, owner-only release, stale recovery, requeue on contention,
  cancel within 10 s with the agent process provably gone.
* **Feature tests** (`test_demo_features.py`): plan approve / reject / timeout,
  notes injected exactly once, patch download and traversal rejection.
* **Unit tests** (`tests/unit/`): pure helpers — retry, budget, plan parsing,
  registry invariants, process-group kill, git timeouts, patch paths.
* **Web** (`npm test`): webhook template rendering, HMAC verification and the
  webhook handler, tested through an injected fake data store.
