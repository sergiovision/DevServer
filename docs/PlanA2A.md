# DevServer as an A2A 1.0 peer — agent-reachable task delegation

> **Design note.** This is the architecture write-up for DevServer's A2A gateway,
> which ships in DevServer Pro. It is published here for reference; file paths under
> `pro/` and `routes/pro_internal.py` are not part of the free edition.

## Context

Autonomous agents increasingly browse for capabilities their orchestrator lacks. What
they cannot do themselves is **durable, verified, long-horizon execution**: hold a repo
lock for two hours, retry with error classification, run a verifier, secret-scan the
diff, and hand back a PR. That is exactly what DevServer already does — but today it is
reachable only by a human at a dashboard, or by a local stdio MCP server.

Two facts make this the right feature now:

1. **The domain model is already A2A-shaped.** A2A's core object is a long-running
   `Task` with states `submitted → working → input-required → completed/failed/canceled`,
   carrying `Message`s and `Artifact`s. DevServer's `tasks` lifecycle, `task_messages`
   bus, plan gate, and artifacts (PR URL, `combined.mbox`, `RESULT.md`) map onto that
   almost 1:1. This is a protocol adapter over existing business logic, not new machinery.
2. **It is already the stated roadmap.** Exposing DevServer as an open A2A 1.0 peer
   (Agent Card + JSON-RPC bridge) was the documented next phase. A2A hit 1.0 in early
   2026 with broad industry adoption, so peers that speak it are increasingly common.

The differentiator is *not* "DevServer has an API". It is **the governed delegate**:
the only agent peer that accepts work with a forced budget ceiling, a human plan gate,
a secret-scan egress filter, and a full audit trail — governance and cost discipline,
not feature mimicry.

**Decisions taken:** A2A first, remote MCP as a follow-on phase sharing the same
credential layer · private/allow-listed peers only (no public card) · new peers default
to **human plan approval required**.

**Status: phases 1–7 built and verified.**

---

## The blocking security finding

**The worker has no authentication of any kind.** No middleware, no `Depends()`, no
token check on any route. `config.py` binds `worker_host="0.0.0.0"`, and
`docker/docker-compose.yml:118` publishes `8000:8000` on all interfaces. The
`/internal` namespace is flat and shared by `internal.py`, `env_config.py`, and
`pro_internal.py` — so `GET /internal/env` (returns every secret in cleartext) and
`PUT /internal/env` (rewrites `.env` on disk) sit beside `POST /internal/tasks/create`.
`routes/internal.py:1-5` documents an `X-Internal-Token` guard that **does not exist**
anywhere in the codebase.

Consequences that shape the whole design:

- The A2A gateway **must not** live in the worker, and the worker must not become
  internet- or LAN-reachable. The gateway goes in the Next.js tier.
- A path-prefix exposure rule can never be safe here. Only an exact-route allow-list is.
- Fixing the worker's auth gap is worth doing regardless, but is **out of scope** for
  this plan — it is called out separately at the end.

---

## Architecture

```
external agent
   │  GET /.well-known/agent-card.json        ← discovery (rewrite → pro route)
   │  POST /api/pro/a2a  (JSON-RPC 2.0)       ← Bearer <peer token>
   ▼
Next.js tier (apps/web) ── the ONLY externally reachable surface
   │  • verifies peer token, scope, quota
   │  • enqueueTask()  ← already the sole pgqueuer writer
   │  • SSE via task_events cursor poll
   ▼
Postgres ── tasks / task_events / task_messages / a2a_peers
   ▲
worker (apps/worker) ── unchanged, stays internal-only
```

**Why Next.js, not the worker:** it is already the public tier; `lib/queue.ts::enqueueTask`
is already the only legal `pgqueuer` producer (so no `queue_bridge.enqueue_via_web`
round-trip and no silent-`False` failure mode); the HMAC/token pattern already exists in
`app/api/pro/webhooks/[token]/route.ts`; and it avoids bolting auth onto a service that
has none and exposes `.env`.

---

## A2A ↔ DevServer mapping

### Task state

The worker never writes `done` — it writes **`test`** on success (`agent_runner.py:1599,2108`),
meaning "succeeded, awaiting human QA, branch + session still live". `done`/`retired` are
human-set via the dashboard.

**Wire encoding.** A2A 1.0's data model is Protocol Buffers, where these enums are
`TASK_STATE_SUBMITTED` / `ROLE_USER`. The JSON-RPC binding does **not** use those names —
every shipping implementation (AWS Bedrock AgentCore, LangChain Agent Server, the
`a2a-sdk` clients) puts lowercase kebab on the wire, and parts are discriminated by a
`kind` field rather than the proto oneof. The table below is the wire form, which is what
`lib/a2a/protocol.ts` implements.

| DevServer | A2A `state` | Notes |
|---|---|---|
| `pending`, `queued` | `submitted` | |
| `running`, `verifying` | `working` | |
| `test`, `done`, `retired` | `completed` | `test` is the worker's success terminal |
| `failed` | `failed` | |
| `cancelled` | `canceled` | one `l` — A2A uses the US spelling |
| `blocked` | `input-required` | peer can unblock via `message/send` |
| `mode='interactive'` **and a run has `plan_json`** and neither `plan_approved_at` nor `plan_rejected_at` | `input-required` | the plan gate — **the headline capability** |
| `abstain_reason IS NOT NULL` | `rejected` | reality gate refused the work |

`input-required` is what turns DevServer from a tool into a subordinate agent: an
orchestrator can *be* the approver, or relay the question to its own human.

The `plan_json` condition is load-bearing, not defensive. Only the heavy runner
(`agent_runner.run_task`) has a plan gate — `skill_runner`, which handles every `skill`
and `research` task, has none. So a research task created by a peer whose credential
forces plan approval is `mode='interactive'` but never produces a plan; keying on mode
alone would report it `input-required` while it ran happily, stranding the caller waiting
to approve something that does not exist.

### Artifacts

Built from the latest `task_runs` row (`task_latest_run` view already does the
`DISTINCT ON (task_id) … ORDER BY attempt DESC`):

| Source | A2A Artifact |
|---|---|
| `task_runs.pr_url` | `url` Part |
| `{log_dir}/{key}.patches/combined.mbox` | `url` Part → scoped download route |
| `task_runs.claude_output` (holds `RESULT.md` for skill/research) | `text` Part, redacted |
| `{log_dir}/{key}.log` tail | `text` Part, redacted |
| `task_runs.plan_json` | `data` Part (only while `input-required`) |

Every text Part passes through `services/pro/_secret_rules.redact_secrets()` — the
existing egress filter already used by `task_messaging.send_message`. Also strip absolute
host paths: `PatchSet.to_dict()["directory"]` leaks them today.

---

## Implementation phases

> **All phases are built.**

### Phase 1 — peer credentials and scoping (do first; nothing is reachable until this exists)

New table `a2a_peers`, modelled on `webhook_triggers` but hardening its known gaps
(plaintext secret, no quota, no replay defence):

```
id, name, slug UNIQUE
token_sha256 CHAR(64) UNIQUE NOT NULL     -- hash only; plaintext shown once at creation
allowed_repo_ids INT[]      DEFAULT '{}'  -- empty = no repo access
allowed_task_types TEXT[]   DEFAULT '{research}'
require_plan_approval BOOL  DEFAULT TRUE  -- forces mode='interactive'
force_git_flow VARCHAR(16)  DEFAULT 'patch'
max_cost_usd NUMERIC(10,4), max_wall_seconds INT   -- ceilings, clamp any request
daily_task_quota INT DEFAULT 10, tasks_today INT, quota_reset_at DATE
enabled BOOL DEFAULT TRUE, last_seen_at, call_count, created_at, updated_at
```

Token issuance copies `webhook-triggers/route.ts:63` —
`crypto.randomBytes(32).toString('base64url')` — but stores only the SHA-256 and returns
the plaintext exactly once. Auth is a `Bearer` header compared with
`crypto.timingSafeEqual` (keep the length pre-check from
`webhooks/[token]/route.ts:70` — `timingSafeEqual` throws on unequal lengths).

Admin CRUD at `app/api/pro/a2a-peers/`, following the `webhook-triggers` shape:
`has_token` boolean projection, never echo the token, no edit form for the credential
(rotate = delete + recreate).

Every task created through the gateway sets `created_by = 'a2a:<slug>'`, reusing the
existing column for a free audit trail.

**Migration note:** only `001_initial.sql` and `002_external_import.sql` exist on disk —
the 004/006/…/010 migrations referenced in `CLAUDE.md` were consolidated into `001`. The
next file is therefore **`003_a2a.sql`**. `scripts/migrate.sh` globs `*.sql` in order, so
it must be idempotent (`CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`) like `002`.

### Phase 2 — Agent Card + discovery

`app/api/pro/a2a/card/route.ts` serves the public card; a rewrite in `next.config.ts`
maps `/.well-known/agent-card.json` → that path. `next.config.ts` already computes
`edition` by probing for `pro/` folders — gate the rewrite on it so a stripped free build
404s cleanly.

Card contents: `capabilities: {streaming: true, pushNotifications: true,
extendedAgentCard: true}`; `securitySchemes` declaring HTTP Bearer; `supportedInterfaces`
declaring the JSON-RPC 2.0 binding at `/api/pro/a2a`. `skills[]` is derived from the task
types in `GET /internal/agent-registry` (`coding`, `test`, `script`, `skill`, `research`)
— the registry is already the source of truth, so no second list to maintain.

The public card advertises capability only. `agent/getExtendedAgentCard` (authenticated)
adds the calling peer's actual `allowed_repo_ids` as per-repo skills.

### Phase 3 — JSON-RPC core

Single `POST /api/pro/a2a` route dispatching on `method`. Hand-roll the JSON-RPC layer
with explicit validation rather than pulling in an SDK — the server side is ~8 methods,
and the spec moved 0.1→0.3→1.0 fast enough that a pinned SDK is a liability. The worker's
`pyproject.toml` shows the house style of keeping deps lean and pinned deliberately.

| Method | Maps to |
|---|---|
| `message/send` (no `taskId`) | validate scope → `INSERT INTO tasks` (mirror the allow-list in `api/tasks/route.ts`) → `enqueueTask()` |
| `message/send` (with `taskId`) | the unblock path — see Phase 5 |
| `tasks/get` | task row + latest run → Task object with artifacts |
| `tasks/list` | scoped to `created_by = 'a2a:<slug>'` — a peer never sees others' tasks |
| `tasks/cancel` | existing cancel logic; refuse `cancelled/done/retired` per `internal.py` |
| `agent/getExtendedAgentCard` | authenticated card |

Required A2A error codes: `-32001` TaskNotFound, `-32002` TaskNotCancelable,
`-32003` PushNotificationNotSupported, `-32004` UnsupportedOperation,
`-32005` ContentTypeNotSupported, plus standard JSON-RPC `-32600/-32601/-32602/-32603`.

**Task-key collision caveat:** `internal.py` looks up `task_key` globally with
`scalar_one_or_none()` while the constraint is `UNIQUE(repo_id, task_key)` — duplicates
across repos raise `MultipleResultsFound`. The gateway should key on the numeric
`tasks.id` (A2A task ids are opaque strings anyway), sidestepping the bug entirely.

### Phase 4 — streaming (`message/stream`, `tasks/subscribe`) — **BUILT**

This is the first SSE surface in the codebase — none exists today
(`grep text/event-stream` returns nothing; live updates reach the browser only via the
WebSocket in `apps/web/server.ts`).

Implement as a Next.js route handler returning a `ReadableStream` that polls
`task_events WHERE task_id = $1 AND id > $cursor` every ~1s, emitting
`TaskStatusUpdateEvent` on `status_change` and `TaskArtifactUpdateEvent` when a run
finishes. Deliberately **not** reusing `server.ts`'s shared `pg.Client` LISTEN — a cursor
poll needs no shared state, survives reconnects (the peer resumes from its last cursor),
and 1s latency is irrelevant for hour-long tasks.

### Phase 5 — `input-required` (the differentiating capability) — **BUILT**

Two triggers, one response shape.

**Plan approval.** When `mode='interactive'` and a plan is pending, `tasks/get` returns
`input-required` with `plan_json` as a `data` Part. The peer replies with
`message/send {taskId, parts:[{text:"approve"}]}` → the gateway writes
`tasks.plan_approved_at` (or `plan_rejected_at`), which is exactly what
`plan_gate.wait_for_approval` busy-polls for (`POLL_INTERVAL_SECONDS = 5`, 1h timeout).
Reuse the guards from `app/api/pro/tasks/[id]/approve/route.ts` (409 if already decided).

**Free-form steering / unblocking.** Any other `message/send` with a `taskId` goes through
the logic already implemented in
`app/api/pro/tasks/[id]/messages/as-operator/route.ts`: insert a `task_messages` row with
`from_task_key='operator'`, flip `autonomous → interactive`, and auto-kick
(`/continue` + `enqueueTask`) when status ∈ `CONTINUABLE_IDLE = {test, failed, blocked, cancelled}`.
That route is the substrate — factor its body into a shared helper in `lib/` and call it
from both the dashboard route and the A2A gateway rather than duplicating it.

Note `plan_gate.wait_for_approval` holds a repo lock and a queue concurrency slot for up
to an hour while blocked. That is pre-existing behaviour, but external peers make it much
easier to hit — worth surfacing `blocked`-task counts in the peer admin UI.

### Phase 6 — push notifications — **BUILT**

`tasks/pushNotificationConfig/{create,get,list,delete}` over a `a2a_push_configs` table
(`task_id`, `peer_id`, `url`, `token`, `auth`). Delivery is a new backend under
`services/notify/` — `NotifyBackend` (`services/notify/base.py`) already defines the
event methods (`send_task_success`, `send_task_failed`, `send_preflight_blocked`, …) and
the `Dispatcher` fans out in parallel with failures swallowed, so this is the documented
~30-line subclass case. The one difference: it looks up per-task configs instead of
reading a single env var.

### Phase 7 — remote MCP transport — **BUILT**

`apps/mcp-memory` was `FastMCP` stdio-only. It now also serves Streamable HTTP
(`DEVSERVER_MCP_TRANSPORT=http`), authenticated with the **same `a2a_peers` credential
and scope layer** as the A2A gateway. stdio remains the default, unauthenticated and
unchanged — the local pipe is its own trust boundary.

- **Auth** — `remote.DevServerTokenVerifier` (a FastMCP `TokenVerifier`) validates the
  bearer token against `GET /api/pro/a2a/introspect`, a new Next.js route that reuses
  `lib/a2a/auth.authenticatePeer`. The MCP server holds no database handle and no copy of
  the peer table, so revoking a credential in the dashboard revokes it here within the
  30s introspection cache (verified). Introspection is safe to leave unauthenticated-by-path
  because the token *is* the credential; it deliberately does not return the governance
  ceilings, which are the gateway's to enforce.
- **Per-request repo scoping.** The stdio server resolves the repo once from the console's
  cwd and caches it in a module global — correct for one console, catastrophic in a
  multi-tenant HTTP process, where it would serve one peer's repository memory to another.
  Remote mode resolves from the calling peer's grant on every request and caches nothing.
  Every repo-scoped tool gained an optional `repo_id`; omitted, a peer scoped to exactly
  one repo gets that one, and a peer with several must say which.
- **Task tools route through the A2A gateway**, not `/internal/tasks/*`, so a task created
  over MCP gets the same repo/task-type allow-lists, forced interactive mode, patch-only
  git flow, ceilings and daily quota. Vendor/model/billing/turns/`skip_verify` args are
  dropped in remote mode — honouring them would let a caller opt out of the operator's
  policy. `task_run` is refused remotely (no A2A equivalent, and re-running would sidestep
  the quota); `task_output` returns the gateway's redacted artifact rather than the host
  log file.
- **Write provenance** is forced to `a2a:<slug>` so a remote caller cannot forge
  attribution via its own `DEVSERVER_AGENT`.

**Deployment:** expose the MCP server, never the worker. Remote mode works precisely
because it is a scope-enforcing front door in front of a service that has none. It binds
`127.0.0.1` by default even in HTTP mode, so exposure is always an explicit operator
choice.

---

## Security design

Ordered by what actually matters here.

1. **Prompt injection is the primary risk.** A peer's message text becomes a task
   `description` that reaches a coding agent with shell access. Layered defence:
   `require_plan_approval=TRUE` by default (a human sees the plan before code runs);
   `force_git_flow='patch'` (nothing is ever pushed); `allowed_repo_ids` allow-list;
   forced `max_cost_usd`/`max_wall_seconds` feeding the existing budget circuit breaker;
   the existing `reality_abstain_threshold`. Additionally, `agent_runner._build_prompt`
   should wrap externally-sourced descriptions in an explicit delimited block marked as
   untrusted third-party data — the one worker-side change this plan requires.
2. **Egress.** Every Part returned to a peer goes through `_secret_rules.redact_secrets()`;
   absolute host paths scrubbed; `tasks/list` scoped to the peer's own `created_by`.
3. **Credential handling.** Hash-at-rest (improves on `webhook_triggers.secret`, stored
   plaintext today), constant-time compare, one-time plaintext display, `token[:6]+"…"`
   in audit events (the pattern at `pro_internal.py:958`).
4. **Quota + replay.** Per-peer daily task quota and per-minute call rate limit — neither
   exists in the webhook path today, which is replayable indefinitely.
5. **Audit.** Emit new `task_events` types `a2a_task_created`, `a2a_message_received`,
   `a2a_input_required`. These flow through the existing PG NOTIFY → WebSocket pipeline to
   the dashboard timeline with no extra plumbing.

**Explicitly not in scope:** TLS/reverse proxy (none exists in `docker/`; private-network
exposure was the chosen posture) and fixing the worker's missing auth. Both should be
tracked separately — the second is a live risk independent of this feature.

---

## Files

**New (all Pro):**
- `database/migrations/003_a2a.sql` — `a2a_peers` (+ `a2a_push_configs`, forward-declared for phase 6)
- `apps/web/src/app/api/pro/a2a/route.ts` — JSON-RPC dispatcher
- `apps/web/src/app/api/pro/a2a/card/route.ts` — Agent Card
- `apps/web/src/app/api/pro/a2a-peers/{route.ts,[id]/route.ts}` — admin CRUD
- `apps/web/src/lib/a2a/{auth,mapping,artifacts,jsonrpc}.ts` — peer auth, state/artifact mapping, error codes
- `apps/web/src/app/pro/a2a/page.tsx` + `components/pro/A2APeersView.tsx`

**Modified:**
- `apps/web/src/components/pro-loader.tsx` — export `A2APeersView`, add nav to `PRO_NAV_ITEMS`
  (+ null stub and empty array in `pro-loader.free.tsx`)
- `apps/web/next.config.ts` — edition-gated `/.well-known/agent-card.json` rewrite
- `scripts/strip-pro.sh` — add `rm -rf` for `app/pro/a2a` (the `app/api/pro/` and
  `components/pro/` folders are already covered by existing lines)
- `CLAUDE.md` + `README.PRO.md` — document the feature

**Added by phases 4–6:** `lib/a2a/stream.ts` (SSE), `lib/a2a/push.ts` (push
configs + SSRF guard), `lib/task-steering.ts` (shared `decidePlan` /
`sendOperatorMessage`, now also used by the dashboard's approve + as-operator
routes), `apps/worker/src/services/notify/a2a_backend.py` (+ registered in
`services/notify/__init__.py`).

Note the streaming lane is *not* a separate route: A2A clients POST every method
to the one endpoint, so `message/stream` is dispatched inside
`app/api/pro/a2a/route.ts` and returns a `text/event-stream` Response instead of
JSON.

**Still deferred:** the `_build_prompt` untrusted-input delimiter (worker-side).

Pro/Free rule followed throughout: Pro logic lives under a `pro/` folder, components are
consumed only via `pro-loader`, and the free build compiles with the gateway absent.

---

## Verification

1. **Migration** — `./scripts/migrate.sh`; confirm `a2a_peers` exists and re-running is a no-op.
2. **Free build is unbroken** — on a scratch copy run `bash scripts/strip-pro.sh`, then
   `cd apps/web && npx tsc --noEmit`. `GET /.well-known/agent-card.json` must 404.
3. **Discovery** — `curl localhost:3200/.well-known/agent-card.json | jq .skills`.
4. **Auth** — no `Authorization` → 401; wrong token → 401; disabled peer → 403;
   over-quota → 429.
5. **Scope** — `message/send` naming a repo outside `allowed_repo_ids` → JSON-RPC `-32004`.
6. **Happy path** — issue a peer scoped to one local repo, `message/send` a small coding
   task, assert the row lands with `created_by='a2a:<slug>'`, `mode='interactive'`,
   `git_flow='patch'`, and clamped budget columns; confirm it reaches `queued` via
   `enqueueTask`.
7. **Egress** — plant a fake `sk-ant-…` string in a task's output; assert it comes back
   `[REDACTED:…]` and that no absolute host path appears in any artifact.
8. **Interop** — validate the card and a `message/send`/`tasks/get` round-trip against the
   official `a2a-sdk` client (Python) rather than only curl, so spec drift surfaces early.

9. **Streaming** — `message/stream` a research task; expect frames
   `task` → `status-update(working)` → `artifact-update` → `status-update(final)`.
10. **SSRF guard** — `tasks/pushNotificationConfig/create` must refuse
    `localhost`, `127.0.0.1`, `[::1]`, `169.254.169.254`, RFC1918 and CGNAT
    addresses, and any non-http(s) scheme. This matters more than usual: the
    worker sits on `localhost:8000` with no auth and serves `/internal/env`.
11. **input-required** — stage a task with `plan_json` and no decision; assert
    `tasks/get` reports `input-required` and carries the plan as a data part,
    an ambiguous reply is refused, `"approve"` sets `plan_approved_at`, and an
    explicit `metadata.action` after the gate closed is refused rather than
    silently downgraded to a chat message.
12. **Steering** — on a `blocked` task, `message/send` must land a
    `from_task_key='operator'` row in `task_messages` and resume the task.
