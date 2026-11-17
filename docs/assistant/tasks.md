# Working with tasks

## Creating one

Go to **Tasks → New Task** (`/tasks/new`), or click *New Task* from the
dashboard. The fields that matter:

- **Title** — short; it becomes the PR title and the task key.
- **Task type** — chosen once, at creation, and never editable afterwards. See
  the task-types topic.
- **Repository** — required for `coding`/`test`/`script`, optional for
  `skill`/`research`.
- **Description** — this is the prompt the agent receives. Be specific about
  files and expected behaviour; vagueness costs retries.
- **Acceptance criteria** — how the agent knows it is done. The verifier and
  the retry loop both read this.
- **Git flow** — what happens to the code afterwards (branch + PR, patch,
  direct commit, or untracked). Defaults to *Untracked*, which changes nothing
  outside the worktree.
- **Vendor / Model / Billing** — which agent CLI runs it and how it is paid for.
- **Priority** — 1 critical, 2 high, 3 medium (default), 4 low.

**Fill Task** uses the system LLM to expand a one-line description into a
complete, structured task. **Templates** pre-fill every field from a saved
preset.

## Task keys

Every task gets a stable key like `DEV-123` or `MCP-FIX-LOGIN`. It names the
branch (`agent/{task_key}`), the log file, and the patch directory, and it is
what Telegram commands and the API take as an argument.

## Statuses

| Status | Meaning |
|---|---|
| `pending` | Created, not yet on the queue |
| `queued` | Waiting for a free worker / the repo lock |
| `running` | An agent is working |
| `verifying` | Build/test/lint are running |
| `test` | Succeeded — awaiting your review. This is the success state |
| `failed` | Retries exhausted |
| `blocked` | Stopped and needs a human (preflight, budget, or an abstain gate) |
| `cancelled` | You stopped it |

`blocked` is the one to look at first: it means DevServer deliberately refused
to proceed, and the task detail page says why.

## Running, stopping, resuming

From the task detail page:

- **Run / Re-run** — puts the task back on the queue.
- **Cancel** — stops a running or verifying task.
- **Continue** — resumes a failed, blocked or running task *with its agent
  session intact*, keeping the existing branch and conversation. This is the
  right button when the agent was on the right track and just ran out of turns.
- **Compact** — summarises the run history into a short brief and starts the
  agent on a fresh conversation. Use it when a long task has filled its context
  with dead ends.

## Watching progress

The task detail page shows a live **event timeline** (each retry, each
classified error, each verification), the **runs** table with cost and turn
counts, and the **Task Log** — the agent's own output, tailed live.

## Priority and ordering

Tasks are dispatched by priority then age. One task per repository runs at a
time; a second task for the same repo waits for the lock rather than fighting
over the working tree.
