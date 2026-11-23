# Feature catalogue

One row per catalogued behaviour. Every test that proves a row carries a
`// Feature: F-…` (TypeScript) or `# Feature: F-…` (Python) tag on the line
above it. `node scripts/feature-coverage.mjs` reports rows without tests and
test citations without rows; CI runs it non-blocking.

Layer: `web-lib` (pure web module), `web-api` (route handler), `web-ui`
(component), `worker`, `db`, `docs`, `ci`. Rows in the `ui`, `docs`, `ci` and
`db` layers are not expected to have unit tests. `web-ui` rows are reported as
uncovered until a DOM test harness exists — their logic lives in the cited
`web-lib` rows.

This file complements [`features.md`](features.md) (the narrative tour); it
does not replace it.

## Web request plumbing (A3)

| ID | Behaviour | Source | Layer |
|---|---|---|---|
| F-WEB-01 | `api.get/post/patch/delete` decode JSON and throw a typed `ApiError` (status, message, detail); network failure → status 0 | apps/web/src/lib/api-client.ts | web-lib |
| F-WEB-02 | `useResource` drops stale responses (only the newest request writes state) and never sets state after unmount | apps/web/src/lib/use-async.ts | web-lib |
| F-WEB-03 | A bad `[id]` segment answers 400, never reaches SQL | apps/web/src/lib/route-utils.ts | web-lib |
| F-WEB-04 | `proxyWorker` forwards the worker's error detail and status; unreachable → 502 "Worker unreachable" | apps/web/src/lib/route-utils.ts | web-lib |
| F-WEB-05 | API routes parse ids and proxy the worker through `route-utils` | apps/web/src/app/api/** | docs |

## Navigation (A1)

| ID | Behaviour | Source | Layer |
|---|---|---|---|
| F-NAV-01 | Palette ranking: exact > prefix > word-prefix > substring, stable ties, empty query → pages only | apps/web/src/lib/command-palette.ts | web-lib |
| F-NAV-02 | `?` and single-letter shortcuts never fire in input/textarea/select/contenteditable (or during IME) | apps/web/src/lib/shortcuts.ts | web-lib |
| F-NAV-03 | ⌘/Ctrl+K opens the command palette; ⌘/Ctrl+J toggles Ask Agent | apps/web/src/lib/shortcuts.ts | web-lib |
| F-NAV-04 | `?` shows the shortcut list, rendered from the single `SHORTCUTS` table | apps/web/src/components/ShortcutHelp.tsx | ui |
| F-NAV-05 | A non-empty query always ends with exactly one "Ask Agent: <query>" row | apps/web/src/lib/command-palette.ts | web-lib |

## Needs You (A2)

| ID | Behaviour | Source | Layer |
|---|---|---|---|
| F-NEED-01 | Counting rule: blocked + live undecided plans, each task counted once; reuses `isAwaitingPlanDecision`; `GET /api/needs-you` | apps/web/src/lib/task-steering.ts | web-lib |
| F-NEED-02 | One shared poll per tab: one timer and one fetch per tick for N subscribers; last unsubscribe stops it | apps/web/src/lib/needs-you.ts | web-lib |
| F-NEED-03 | The poll skips ticks while the tab is hidden | apps/web/src/lib/needs-you.ts | web-lib |

## Ask Agent (A4)

| ID | Behaviour | Source | Layer |
|---|---|---|---|
| F-ASK-01 | History keeps the 20 most recent conversations, most recently updated first (open/rename/delete/new) | apps/web/src/lib/ask-agent-history.ts | web-lib |
| F-ASK-02 | Secret-looking values are masked before history is stored | apps/web/src/lib/ask-agent-history.ts | web-lib |
| F-ASK-03 | A different storage version or corrupt JSON is discarded without throwing | apps/web/src/lib/ask-agent-history.ts | web-lib |
| F-ASK-04 | Undecided proposals are stored as stale; the worker session id is never stored | apps/web/src/lib/ask-agent-history.ts | web-lib |
| F-ASK-05 | Composer keys: Enter sends, Shift+Enter newline, IME untouched, ↑/↓ recall prompts | apps/web/src/lib/ask-agent-composer.ts | web-lib |
| F-ASK-06 | `@TASK-KEY` / `@repo:name` / `@idea:id` mentions resolve into a context block; failures leave the raw token | apps/web/src/lib/ask-agent-composer.ts | web-lib |
| F-ASK-07 | Export a conversation as Markdown (secrets masked) | apps/web/src/lib/ask-agent-composer.ts | web-lib |
| F-ASK-08 | Slash command menu (`/new`, `/clear`, `/export`, `/history`, `/help`) | apps/web/src/lib/ask-agent-composer.ts | web-lib |
| F-ASK-09 | Edit and resend after a stopped answer | apps/web/src/lib/ask-agent-composer.ts | web-lib |
| F-ASK-10 | Panel wiring: history list, slash menu, mentions, export, edit-and-resend | apps/web/src/components/AskAgentPanel.tsx | ui |
| F-ASK-11 | A reopened conversation sends its transcript on the agent engine's first turn | apps/worker/src/routes/assistant.py | worker |

## Connectivity (A5)

| ID | Behaviour | Source | Layer |
|---|---|---|---|
| F-CONN-01 | State machine online / offline / server-down / worker-down (2-failure debounce) | apps/web/src/lib/connectivity.ts | web-lib |
| F-CONN-02 | "Restored" fires exactly once per outage | apps/web/src/lib/connectivity.ts | web-lib |
| F-CONN-03 | Per-request toasts the banner already explains are suppressed | apps/web/src/lib/connectivity.ts | web-lib |
| F-CONN-04 | Panels reload on restored: listeners called once, unsubscribe works, a throwing listener is isolated | apps/web/src/lib/connectivity-events.ts | web-lib |

## Tasks list (A6)

| ID | Behaviour | Source | Layer |
|---|---|---|---|
| F-TASK-01 | Search query capped at 8 terms, each ≤ 100 chars | apps/web/src/lib/task-search.ts | web-lib |
| F-TASK-02 | A task matches only if every term (case-insensitive) hits a list field; grid and board share the matcher | apps/web/src/lib/task-search.ts | web-lib |
| F-TASK-03 | Only http(s) URLs are linkified, `rel="noopener noreferrer"`; trailing punctuation excluded | apps/web/src/lib/linkify.ts | web-lib |
| F-TASK-04 | Shared `EmptyState` replaces ad-hoc empty-list texts | apps/web/src/components/EmptyState.tsx | ui |
| F-TASK-05 | `/tasks` honours `?status=a,b` (unknown ignored) and `?q=` | apps/web/src/lib/task-search.ts | web-lib |

## Analytics (A7)

| ID | Behaviour | Source | Layer |
|---|---|---|---|
| F-ANL-01 | Avg output tokens/s per vendor (Σ output ÷ Σ agent-active seconds) | apps/web/src/lib/analytics-metrics.ts | web-lib |
| F-ANL-02 | Avg tokens per task: input (incl. cache reads/writes) vs output | apps/web/src/lib/analytics-metrics.ts | web-lib |
| F-ANL-03 | Metrics are zero-division safe (null, never NaN/Infinity) | apps/web/src/lib/analytics-metrics.ts | web-lib |
| F-ANL-04 | Statistics modal with the tabular breakdown | apps/web/src/components/AnalyticsStatisticsModal.tsx | ui |
| F-ANL-05 | The worker records CLI token usage on the `cost_update` event (keys omitted when the vendor reports none) | apps/worker/src/services/runner/attempt.py, apps/worker/src/services/agent_backends.py | worker |

## Prompt-injection hygiene (B1)

| ID | Behaviour | Source | Layer |
|---|---|---|---|
| F-SEC-01 | Zero-width / bidi / tag invisible characters are stripped from task text | apps/worker/src/services/injection_scan.py, apps/web/src/lib/injection-scan.ts | worker |
| F-SEC-02 | Override phrases are flagged, never removed; a task is never refused | apps/worker/src/services/injection_scan.py, apps/web/src/lib/injection-scan.ts | worker |
| F-SEC-03 | Imperatives hidden in HTML comments (incl. unterminated) are flagged | apps/worker/src/services/injection_scan.py, apps/web/src/lib/injection-scan.ts | worker |
| F-SEC-04 | Description, acceptance and failure evidence are fenced in entity-escaped `<untrusted_task>` / `<untrusted_evidence>`; breakout escaped | apps/worker/src/services/runner/prompt.py | worker |
| F-SEC-05 | Ingress (task form, webhooks, A2A, imports) sanitises and emits `injection_sanitised` | apps/web/src/lib/injection-ingress.ts | web-lib |
| F-SEC-06 | Python/TS parity via the shared fixture `apps/worker/tests/fixtures/injection_cases.json` | apps/worker/tests/fixtures/injection_cases.json | worker |

## Action on completion (B2)

| ID | Behaviour | Source | Layer |
|---|---|---|---|
| F-PCA-01 | Validator accepts task/skill shapes, null clears, rejects `script` / missing target / bad trigger | apps/web/src/lib/post-completion-action.ts | web-lib |
| F-PCA-02 | Trigger matrix: test/done = success, failed/blocked = failure; on_success / on_failure / always | apps/worker/src/services/post_completion.py | worker |
| F-PCA-03 | Self-reference and in-chain targets are refused | apps/worker/src/services/post_completion.py | worker |
| F-PCA-04 | `triggered_by_task_id` chain capped at 3 automatic hops; cycles refused | apps/worker/src/services/post_completion.py | worker |
| F-PCA-05 | Cancelled (and gate-suspended) tasks never fire | apps/worker/src/services/post_completion.py | worker |
| F-PCA-06 | Migration 004 is idempotent (`ADD COLUMN IF NOT EXISTS`) | database/migrations/004_post_completion_action.sql | db |
| F-PCA-07 | `PATCH /api/tasks/[id]` validates the action (400 on invalid, self-reference, missing target) | apps/web/src/app/api/tasks/[id]/route.ts | docs |
| F-PCA-08 | An enqueue not marked `X-DevServer-Triggered` clears the stale `triggered_by_task_id` link; skill/research tasks fire their action too | apps/web/src/app/api/tasks/[id]/enqueue/route.ts, apps/worker/src/services/skill_runner.py | docs |

## Release check (B3)

| ID | Behaviour | Source | Layer |
|---|---|---|---|
| F-UPD-01 | Release version parsing and comparison (`v` prefix, numeric order, unknown on garbage) | apps/worker/src/services/release_check.py | worker |
| F-UPD-02 | A dismissed release hides only that release | apps/web/src/lib/update-notice.ts | web-lib |
| F-UPD-03 | Network / GitHub failure yields `unknown` and the notice shows nothing | apps/worker/src/services/release_check.py, apps/web/src/lib/update-notice.ts | worker |
| F-UPD-04 | 6 h in-process cache (30 min for failures) | apps/worker/src/services/release_check.py | worker |
| F-UPD-05 | `update_notify_enabled=false` skips the network call | apps/worker/src/services/release_check.py | worker |
| F-UPD-06 | Update command chosen by deployment / OS | apps/web/src/lib/update-notice.ts | web-lib |

## Visual, catalogue, restyle (V, C, R)

| ID | Behaviour | Source | Layer |
|---|---|---|---|
| F-UI-01 | Cockpit tokens: compact shell, 6 px radius, mono token, WCAG AA accents | apps/web/src/app/cockpit.css | ui |
| F-CAT-01 | Feature-coverage report: rows without tests, citations without rows | scripts/feature-coverage.mjs | ci |
| F-STYLE-01 | Every worker module and web `lib/*` file starts with a purpose docstring | apps/worker/src/**, apps/web/src/lib/** | docs |
