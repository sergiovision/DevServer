# Features

A tour of every screen and subsystem. Back to the [README](../README.md).

## Dashboard — what's running, what's queued, what cost what

<a href="../assets/dashboard.png"><img src="../assets/dashboard.png" alt="DevServer dashboard — worker status bar, running agents, queue controls, analytics summary row with cost and success rate, duration and turns-per-task charts" width="100%" /></a>

The landing page. Top: worker status bar with online/offline indicator, queue depth, and active/pending/running task counts. Middle: running agents list and queue control toolbar (Add Task, Pause Queue, Resume Queue). Below: colour-coded stat cards (running, queued, completed today, failed today). **Analytics** section with a summary row (completed, failed, success rate %, total cost, agent time, total turns) plus two governance KPIs neither competitor surfaces — **Cost / PR** (average LLM spend per pull request) and **Abstain Savings** (spend avoided on tasks the reality gate refused to start) — and Avg Duration per Task (bar) and Avg Turns per Task (line) charts, configurable from 7 to 90 days. Everything updates in real time over the WebSocket.

📂 [`apps/web/src/components/Dashboard.tsx`](../apps/web/src/components/Dashboard.tsx) · [`apps/web/src/components/DashboardCharts.tsx`](../apps/web/src/components/DashboardCharts.tsx)

---

## Tasks — the full backlog with status and priority

<a href="../assets/tasks.png"><img src="../assets/tasks.png" alt="DevServer tasks list — priority colour badges, task key, title, repo, status badge, turns count, created date, items per page selector" width="100%" /></a>

The full task backlog. Columns: colour-coded priority badge, task key, title, repo, status badge, turns used, and created date. Filter by status (`pending` / `queued` / `running` / `verifying` / `test` / `failed` / `blocked` / `cancelled`), toggle retired tasks, and paginate with items-per-page selector. Each row links to the task detail view. The **+ New Task** button opens the creation form with an optional template picker. An **Import** button pulls Confluence pages straight into the backlog (see *Import from Confluence* below), and grid rows carry checkboxes for **bulk retire** — select any number of tasks across repo groups and retire them in one click.

A **Grid ⇄ Board** toggle switches the same data between the table and a **Pipeline Board** — a kanban projection with lifecycle-native lanes (**Queued / Active / Needs You / Shipped / Stalled**). Cards carry a vendor badge, priority flag, reality-score chip, and inline actions (Enqueue / Cancel / Continue); lanes transition live over the websocket with no polling, and filters cover repo / vendor / "needs you only". A **"Needs You"** count pill on the sidebar surfaces tasks awaiting a human decision.

📂 [`apps/web/src/app/tasks/page.tsx`](../apps/web/src/app/tasks/page.tsx) · [`apps/web/src/components/TaskTable.tsx`](../apps/web/src/components/TaskTable.tsx) · [`apps/web/src/components/PipelineBoard.tsx`](../apps/web/src/components/PipelineBoard.tsx)

---

## Task Detail — events, runs, agent settings

<a href="../assets/viewtask.png"><img src="../assets/viewtask.png" alt="DevServer task detail — event log with reality signal and preflight results, task log, agent settings sidebar with vendor/model and backup model pickers, patches panel with download button" width="100%" /></a>

The single most information-dense view in the product. From left:

- **Live event log** — every agent step (`repo_map_built`, `reality_signal`, `error_classified`, `pr_preflight_pass`, `rate_limit_backoff`, `vendor_failover`) as it streams in over PG `NOTIFY` → WebSocket.
- **Task log** — real-time tail of the per-task log file with run result, diff stats, and `git am`-ready output.
- **Agent settings sidebar** — per-task overrides for billing mode (API / Max subscription), vendor + model picker, backup vendor + model for auto-failover, git flow (Branch + PR / Direct commit / Patch only — Local Git repos offer Untracked changes / Patch only instead), verification toggle, and a Save button.
- **Plan card** (interactive tasks), **Patches panel** (patch-flow tasks) and **Task Notes** — see the sections below.

📂 [`apps/web/src/app/tasks/[id]/page.tsx`](../apps/web/src/app/tasks/[id]/page.tsx) · [`apps/web/src/components/TaskDetail.tsx`](../apps/web/src/components/TaskDetail.tsx)

---

## Task Templates — saved presets for repetitive work

<a href="../assets/templates.png"><img src="../assets/templates.png" alt="DevServer task templates — table with name, git flow badge, vendor/model, billing mode, max turns, and edit/delete actions" width="100%" /></a>

Create reusable templates with pre-filled descriptions, acceptance criteria, agent vendor/model, git flow, billing mode, max turns, and all other agent settings. The template table shows name, git flow badge (Direct commit / Branch + PR), vendor/model, billing mode, and max turns at a glance. When creating a new task, pick a template from the dropdown and the form is pre-filled instantly.

📂 [`apps/web/src/app/templates/page.tsx`](../apps/web/src/app/templates/page.tsx) · [`apps/web/src/components/TemplateList.tsx`](../apps/web/src/components/TemplateList.tsx)

---

## Ideas — hierarchical brainstorm tree, convertible to tasks

<a href="../assets/ideas.png"><img src="../assets/ideas.png" alt="DevServer ideas tree — folders and idea leaves, convertible to tasks" width="100%" /></a>

A lightweight brainstorm space. Folders contain other folders or **idea leaves** (markdown content). Reorganise the tree by **drag-and-drop** — drop an idea onto a folder (or the empty root area) to reparent it, with cycle-safe validation and optimistic updates. When an idea is ready, click *Convert to Task* and it lands in the tasks backlog with the description pre-populated. Idea → task linkage is preserved in the database. An **Import** button brings Confluence pages in as ideas.

📂 [`apps/web/src/app/ideas/page.tsx`](../apps/web/src/app/ideas/page.tsx) · [`apps/web/src/components/IdeasView.tsx`](../apps/web/src/components/IdeasView.tsx)

---

## Import from Confluence — pages become Tasks or Ideas

A shared **Import** dialog on the Tasks and Ideas pages pulls documentation straight out of Confluence: pick the source, search by text or raw CQL (with an optional Space filter), tick the pages you want, preview the converted Markdown, then map them to **Tasks** (choose repo, priority, optional auto-enqueue) or **Ideas** (lands at the tree root).

- **Cloud and Data Center/Server** are both supported — email + API token (Basic auth) for Cloud, Personal Access Token (Bearer) for DC; the `/wiki` suffix is added automatically for `*.atlassian.net`.
- **Global or per-repo credentials** — configure once in Settings (stored in `.env`, never in the database), or give a repo its own Confluence connection override.
- **Idempotent re-imports** — an `external_imports` ledger maps each page to its target; unchanged versions are skipped, changed ones update the existing task/idea in place. Never duplicated.
- **Storage-format → Markdown** conversion (headings, lists, code macros, tables, links) with zero new dependencies, and the framework is pluggable — Confluence is the first source adapter.

📂 [`apps/web/src/components/ImportModal.tsx`](../apps/web/src/components/ImportModal.tsx) · [`apps/worker/src/services/import_sources/`](../apps/worker/src/services/import_sources/)

---

## Logs — live tail of worker and web process output

<a href="../assets/logs.png"><img src="../assets/logs.png" alt="DevServer logs page — live-tailed worker and web log with colour-coded severity levels" width="100%" /></a>

A real-time log viewer with two tabs — `worker.log` and `web.log` — polled every 1.5 seconds. Lines are colour-coded by severity (ERROR red, WARNING yellow, INFO blue, DEBUG green). Auto-scrolls to the bottom; a jump-to-bottom button appears when you scroll up.

📂 [`apps/web/src/app/logs/page.tsx`](../apps/web/src/app/logs/page.tsx) · [`apps/web/src/components/LogsView.tsx`](../apps/web/src/components/LogsView.tsx)

---

## Settings — global queue, system LLM, and environment variables

<a href="../assets/settings.png"><img src="../assets/settings.png" alt="DevServer settings page — max concurrency, queue paused toggle, auto-enqueue toggle, notifications toggle, system LLM vendor/model picker, and environment variables editor showing .env path, database connection, and API keys" width="100%" /></a>

A single-page control panel for the worker's global behaviour. **General** card: max concurrency (1–10), queue-paused and auto-enqueue toggles, Telegram notification toggle, the **System LLM** vendor + model picker (used by Fill Task and DevPlan), and a **Memory & Reality Gate** row (abstain threshold, memory decay half-life, archive window, iterative-recall toggle — all default to off so behaviour is unchanged until you opt in). **Environment Variables** card: live view of the `.env` file path, database connection details (host, port, user, database), and masked API keys with a Show/Hide toggle — plus a **Run Setup** button to re-run the interactive `.env` wizard. A **Confluence** card configures the global external-import connection (base URL, username/email, API token or PAT) with a one-click **Ping** connectivity test; values are written to `.env` and hot-reloaded into the running worker.

📂 [`apps/web/src/app/settings/page.tsx`](../apps/web/src/app/settings/page.tsx) · [`apps/web/src/components/SettingsForm.tsx`](../apps/web/src/components/SettingsForm.tsx)

---

## Repositories — Gitea, GitHub, or any local git folder

Three repository types, selected per repo:

- **Gitea / Forgejo** — clones over HTTPS with a per-repo or global token, opens PRs through the Gitea REST API.
- **GitHub** — same pipeline with GitHub-correct auth (`x-access-token` works for classic PATs, fine-grained PATs and App tokens alike) and the GitHub PR API. GitHub Enterprise Server is supported too.
- **Local Git** — defined by a **Local Root Folder** instead of a clone URL. DevServer runs git directly inside that folder: no clone, no worktree copy, no tokens, and — by hard guarantee — **no push, ever**.

Why Local Git matters: it makes DevServer **provider-agnostic**. Clone a repo from **GitHub, GitLab, Bitbucket, Azure DevOps, a private bare repo on a NAS — anything** — and point DevServer at the folder. Since all work happens against the local clone, there is no host API to integrate, no token to provision, and nothing ever leaves your machine. Two git flows fit this model:

- **Untracked changes** — the agent only edits files in your working tree. No branch, no commit, no push; you review the diff with your normal tools and commit it yourself.
- **Patch only** — the agent commits on a local `agent/…` branch (your original branch is restored afterwards) and the commits are exported as `.patch` files you can `git am` anywhere.

Safety guarantees for your folder: DevServer never hard-resets or cleans it, refuses to start a patch-flow task on a dirty working tree (so your own uncommitted work can never be swept into agent commits), and the agent is explicitly instructed to never touch remotes. Local repos show a turquoise **Local** badge on the Repos page.

📂 [`apps/worker/src/services/git_ops.py`](../apps/worker/src/services/git_ops.py) · [`apps/web/src/components/RepoForm.tsx`](../apps/web/src/components/RepoForm.tsx)

---

## Architecture diagram — instant module map, no LLM

Each repo on the Repos page has a **Diagram** button that opens a full-screen, scrollable **Mermaid** flowchart of the repo's module tree. It's generated deterministically from the same directory walk as the repo map (`build_mermaid`) — no LLM call, no token cost, regenerates instantly — so you get a current architecture picture for free, on demand.

📂 [`apps/worker/src/services/repo_map.py`](../apps/worker/src/services/repo_map.py) (`build_mermaid`) · [`apps/web/src/components/RepoDiagramModal.tsx`](../apps/web/src/components/RepoDiagramModal.tsx)

---

## Jobs — cron schedules that re-run tasks

A schedule binds a name + cron expression (`@hourly`, `@daily`, `every 30m`, `every 2h`, `HH:MM` UTC) to an **existing task**. On every fire the task is reset to pending and enqueued through the normal pipeline; a task that is already queued or running is left alone — no double-runs. Run history lands in the task's own runs/events, so there is nothing extra to monitor.

📂 [`apps/web/src/components/SchedulesPanel.tsx`](../apps/web/src/components/SchedulesPanel.tsx) · [`apps/worker/src/services/scheduler.py`](../apps/worker/src/services/scheduler.py)

---

## Ask Agent — an assistant that knows the product and the page you're on

A button in the bottom-right of every screen (or `⌘/Ctrl + K`) opens a chat drawer that runs on your configured **System LLM** — so it costs whatever that model costs, and nothing else. It always knows which page you're looking at, and on a task or repo page it knows *which* task or repo, so "why is this stuck?" is answered about that one.

It is grounded in a curated handbook shipped in the repo (`docs/assistant/`) rather than the model's memory of DevServer, so it explains the actual screens, statuses, git flows and settings this version has. Answers stream token by token.

A combobox in the drawer's toolbar picks the **model** for the next turn, from whatever your System LLM vendor offers — reach for a bigger model on a hard question, drop back to a cheap one for the rest. The vendor itself stays a Settings decision, since changing it also changes credentials and billing. Your choice is remembered.

It answers questions and says plainly when it cannot see something. It has no tools and no shell: it can explain, never act.

📂 [`apps/web/src/components/AskAgentPanel.tsx`](../apps/web/src/components/AskAgentPanel.tsx) · [`apps/worker/src/routes/assistant.py`](../apps/worker/src/routes/assistant.py) · [`apps/worker/src/services/assistant_kb.py`](../apps/worker/src/services/assistant_kb.py)

## Agent guardrails — evidence, budget, and a deterministic preflight

Every coding task passes through three checks that need no extra LLM call:

- **Reality gate (lite).** Before the agent starts, DevServer scores the task 0–100 from two weighted signals that run in parallel: how many of the task's identifiers actually appear in the repo map, and whether the files it touches changed in the last 14 days. If a signal fails, its weight goes to the other one. The score, evidence and warnings are injected into the prompt. Set `reality_abstain_threshold` to block low-evidence tasks before any tokens are spent.
- **Budget circuit breaker.** `max_cost_usd` / `max_wall_seconds` on a task emit a `budget_warning` event at 80% and stop retries at 100%, marking the task `blocked`.
- **PR preflight.** Before anything is pushed, the diff is scanned for leaked secrets (API keys, tokens, private keys), forbidden files (`.env`, key files), files over 1 MB, and a wrong commit author. A failure marks the task `blocked` instead of opening a PR.

📂 [`reality_gate.py`](../apps/worker/src/services/reality_gate.py) · [`pr_preflight.py`](../apps/worker/src/services/pr_preflight.py) · [`secret_rules.py`](../apps/worker/src/services/secret_rules.py) · [`budget.py`](../apps/worker/src/services/budget.py)

## Memory — hybrid recall of past task experience

Every successful task is summarised into `agent_memory` with a local [fastembed](https://github.com/qdrant/fastembed) embedding (no cloud key). The next task on the same repo recalls the most relevant entries through two lanes: pgvector cosine similarity, and Postgres full-text search, which catches exact symbols, error strings and file paths. The two ranked lists are combined with **Reciprocal Rank Fusion** (k=60), and the top hits go into the prompt as "Prior Experience".

📂 [`apps/worker/src/services/memory.py`](../apps/worker/src/services/memory.py) · [`apps/worker/src/services/embeddings.py`](../apps/worker/src/services/embeddings.py)

## Plan Gate — approve the plan before any code is written

Set a task's mode to **interactive** and the agent first runs read-only with a small turn budget, answering with a structured plan: summary, steps, files to touch, risks. The task stops at a **Plan** card on its detail page. **Approve** and the plan is injected into the implementation prompt — and its `files_to_touch` becomes the PR-preflight allow-list, so the agent cannot quietly widen the scope it was approved for. **Reject**, or leave it unanswered for an hour (`plan_gate_timeout_seconds`), and the task is blocked. A planning run that edits files is refused.

📂 [`plan_gate.py`](../apps/worker/src/services/plan_gate.py) · [`PlanCard.tsx`](../apps/web/src/components/PlanCard.tsx)

## Task Notes — talk to a running agent

Post a note on a running task ("use the existing retry helper", "skip the docs"). The runner picks up unread notes at the start of the next attempt, adds them to that attempt's prompt and marks them read in the same SQL statement — so each note reaches the agent **exactly once**. Notes on a finished task are kept for the record and never replayed into a later run. Also available from Telegram as `/reply TASK-KEY <note>`.

📂 [`task_notes.py`](../apps/worker/src/services/task_notes.py) · [`TaskNotesPanel.tsx`](../apps/web/src/components/TaskNotesPanel.tsx)

## Patch Export — reviewable `.patch` files

Tasks using the **patch** git flow never push. When one succeeds, its commits are exported with `git format-patch` and listed in a **Patches** panel for download and `git am`. File names arrive from URLs, so they are validated twice — a strict name pattern, then a resolved-path containment check — and traversal attempts get a `400`.

📂 [`patch_export.py`](../apps/worker/src/services/patch_export.py) · [`PatchesPanel.tsx`](../apps/web/src/components/PatchesPanel.tsx)

## Webhook Triggers — turn external events into tasks

Create a trigger on the **Webhooks** page: a repo, an opaque URL token, a secret, and `{{json.path}}` templates for the title and description. `POST /api/webhooks/<token>` must carry `X-DevServer-Signature: sha256=<hmac>`; it is verified with HMAC-SHA256 and a constant-time comparison. A valid call creates and enqueues a task; a missing or wrong signature gets `401`, a disabled trigger `404`. A trigger without a secret cannot be created.

📂 [`apps/web/src/lib/webhooks/`](../apps/web/src/lib/webhooks/) · [`apps/web/src/app/webhooks/`](../apps/web/src/app/webhooks/)

## MCP server — drive DevServer from Claude Code, Codex or Gemini

A small stdio MCP server lets an interactive coding CLI create a task in the current repo with a chosen vendor and model, start it on the worker, poll its status and read its output (`task_options`, `task_create`, `task_run`, `task_list`, `task_status`, `task_output`, `task_cancel`). Setup for each CLI is in its README.

📂 [`apps/mcp-memory/`](../apps/mcp-memory/README.md) · design notes in [`docs/`]()

