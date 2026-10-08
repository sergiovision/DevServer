# RLM-Inspired Memory Navigation — Implementation Plan

> **Design note.** This is the architecture write-up for DevServer's pull-on-demand
> memory lanes, which ship in DevServer Pro. It is published here for reference; the
> free edition includes basic hybrid recall only (`services/memory.py`). File paths
> under `pro/` and `routes/pro_internal.py` are not part of the free edition.

Adopt the **Recursive Language Models (RLM)** *pattern* — "context as a
navigable variable the model pulls on demand" — but **only the parts that need
no recursive fan-out, no new dependency, and carry no rate-limit risk.**

This plan deliberately **excludes the heavy/risky RLM concepts** (recursive
map-reduce compaction, recursive deep-transcript recall, and the `rlms`
library). Those are documented in §4 as *evaluated and deferred*, with the
rationale, so the decision is on record — not silently dropped.

> **One-line decision:** build **E1 (memory-as-tool / pull-first)** and
> **E4 (repo-map drill-down)**. Do **not** build E2/E3 fan-out or adopt
> `rlms`. Never wrap the primary coding agent in RLM.

Companion to `docs/repo-memory-refactor-plan.md` (the KB this builds on).

---

## 1. The gap (brief)

DevServer's memory is **push + lossy-compress, decided before the agent acts**:
top-k recall is chosen *once* from the task title/description before the agent
reads any file; the static budgets (repo map 4 KB, recall 3×200 chars) are fixed
regardless of need; and the verbatim transcript drawers we pay to embed are
excluded from default recall and read by nothing.

The RLM insight we adopt: **don't guess a fixed budget up front — let the agent
navigate the corpus it needs.** DevServer already *has* the corpus (pgvector KB,
transcript drawers, facts, repo map). The only missing piece is an
**agent-driven pull layer** over it. That layer needs **no recursion and no
fan-out** — just read-only tools over the existing `RepoMemory` methods.

One seam already exists: `GET /internal/tasks/{key}/memory/recall` (PR-8). This
plan makes *pull* the primary path and adds the missing lanes.

---

## 2. Scope — what we WILL build

### E1 — Memory-as-tool (pull-first) · PRO · highest value, low effort

Stop pushing a blind top-3 recall; expose the KB as callable lanes the agent
uses when it needs history. Keep the cheap deterministic wake-up digest pushed
on attempt 1 (it's free and grounds the agent), but **demote the pushed recall
from 3 entries to 1–2** and let the agent pull the rest.

**New worker endpoints** (`routes/pro_internal.py`, mirroring the existing
`/memory/recall`; all read-only, bounded `limit`, repo-less task → empty):

| Method | Path | Backed by (`RepoMemory`) |
|---|---|---|
| `GET` | `/internal/tasks/{key}/memory/transcripts?q=&topic=&limit=` | `recall_transcripts` — **activates the write-only drawers** |
| `GET` | `/internal/tasks/{key}/memory/facts?q=&subject=&limit=` | `search_facts` / `query_facts` (task-scoped, semantic) |
| `GET` | `/internal/tasks/{key}/memory/decisions?q=&limit=` | new `recall_decisions` → `search_memory(memory_type='decision')` |

(`/memory/recall`, `/memory/remember`, `POST /facts`, `/facts/{id}/invalidate`,
`GET /repos/{id}/facts` already exist from PR-4/PR-8.)

**`RepoMemory` additions** (`services/pro/repo_kb.py`): one small method —
`recall_decisions(query, *, limit)` delegating to
`_memory.search_memory(..., memory_type='decision', limit=limit)`. The
transcript and fact lanes reuse existing methods.

**Agent prompt block** — extend the existing `_HAS_PRO`-gated "## Project memory
(optional)" block in `agent_runner._build_prompt` to teach the lanes, e.g.:
*"Before retrying an approach, query `/memory/transcripts?q=...` to check whether
a past attempt already failed it; query `/memory/facts?q=...` for established
project facts."*

**Setting:** `memory_pushed_recall_limit` (default `2`) — how many recall
entries are pushed on attempt 1 (set `3` to keep today's behaviour, `0` for
fully pull-first). Read in the `agent_runner` recall block.

**Event:** `memory_queried` emitted by each GET lane (count + lane), riding the
existing PG NOTIFY → WebSocket pipeline for dashboard visibility.

**Why it's safe:** every lane is a single bounded query over pgvector/Postgres
— no LLM fan-out, no recursion, no new infra, zero rate-limit exposure.

### E4 — Repo-map drill-down · PRO endpoint over FREE compute · low effort

Let the agent request a deeper symbol map of the directory it's editing instead
of the global 4 KB snapshot.

- **`services/repo_map.py`** — add a `subdir: str | None = None` parameter to
  `build_repo_map(...)` that scopes `_scan_worktree` to
  `worktree_path/subdir` (path-traversal–guarded: reject `..`/absolute). The
  existing `max_chars` already lets the caller ask for more.
- **Endpoint** `GET /internal/tasks/{key}/repo-map?subdir=&max_chars=` in
  `pro_internal.py` — resolves the task's active worktree path (from the
  task/repo record; local repos use the local root), calls `build_repo_map`,
  returns the rendered text + stats. Degrades to an empty/`unavailable` body if
  the worktree can't be resolved.
- **Prompt:** one line in the same memory block — *"Need more detail on the area
  you're editing? `GET /repo-map?subdir=apps/worker`."*

**Why it's safe:** pure local compute, **no LLM at all**. Pairs naturally with
E1 as another navigable lane.

---

## 3. Phasing

Each phase ships alone and degrades to today's behaviour when its setting is at
the safe default — matching the `memory_*` convention.

- **Phase 1 — E1 (Pro) · ✅ DONE.** Added GET lanes
  `/memory/transcripts`, `/memory/facts`, `/memory/decisions` in
  `pro_internal.py` + `RepoMemory.recall_decisions` + `memory_queried` event;
  extended the `_HAS_PRO` "## Project memory (optional)" prompt block to teach
  the lanes; demoted pushed recall via `memory_pushed_recall_limit` (default 2,
  `0` = fully pull-first, `3` = pre-RLM). No dependency.
- **Phase 2 — E4 (Pro) · ✅ DONE.** Added a path-traversal–guarded `subdir`
  param to `repo_map.build_repo_map` (+ `_safe_subdir`) and the
  `GET /tasks/{key}/repo-map?subdir=&max_chars=` endpoint (resolves the task's
  worktree; local repos use the local root) + prompt mention.

No spike phase was needed — neither E1 nor E4 touches `rlms` or any fan-out.
20 worker tests pass (the 4 `test_scheduler.py` failures are pre-existing/env).

---

## 4. Deferred — what we deliberately do NOT build (risk register)

These are recorded as **evaluated and rejected for now**, not forgotten. Revisit
only if the rate-limit/latency picture changes materially.

| Concept | Status | Why deferred |
|---|---|---|
| **E2 — Recursive (map-reduce) compaction** | **Deferred** | Fan-out of *blocking, non-cached* sub-calls is exactly what caused the 30K-TPM pain; minute-scale runtimes; conflicts with the budget circuit breaker's guarantees. Keep `compaction.py` (last-6 runs / ≤20 KB) as-is. |
| **E3 — Recursive deep-transcript recall** | **Deferred** | Same fan-out risk. E1's `/memory/transcripts` (a *single* hybrid query) already **activates the drawers** and captures most of the value with zero fan-out; the recursive map-reduce lane is the heavy part we skip. |
| **`rlms` library dependency** | **Not adopted** | Pulls sandbox integrations (modal / e2b / docker) we don't need; young 2025 research artifact. Adopt the *pattern* (~tools over our store), not the library. |
| **Wrapping the primary coding agent in `rlm.completion`** | **Never** | Claude Code / Codex / Gemini CLIs manage their own context + prefix caching; wrapping them would fight caching, multiply latency, and void the budget breaker. RLM belongs on the system-LLM / memory side only — and in this plan, not even there. |

**If E2/E3 are ever revisited**, the non-negotiable guardrails from the research
must hold: fan-out only on the **system LLM in `max` mode** (no per-token
metering), a hard concurrency cap, reuse of `_RATE_LIMIT_BACKOFF_SCHEDULE`,
spend routed through `_check_budget`, and an off-by-default `memory_*` setting.
None of that is required for the E1/E4 scope above, which is why it's the scope.

---

## 5. Pro/Free split

Holds to the existing convention:

- **PRO** — all new endpoints (`pro_internal.py`), `RepoMemory.recall_decisions`,
  the agent prompt lanes (the runtime curl channel is already Pro-gated via
  `DEVSERVER_WORKER_URL`, injected only when `_HAS_PRO`).
- **FREE** — `repo_map.py` stays free; the `subdir` parameter is a free change.
  The `/repo-map` *endpoint* is Pro (it lives in `pro_internal.py`), wrapping the
  free compute. `FreeHooks` is unaffected (no new pushed-side behaviour);
  `memory_pushed_recall_limit` simply isn't consulted when recall returns `[]`.
- `scripts/strip-pro.sh` stays correct — deleting `pro/` removes the lanes; the
  agent prompt blocks are `_HAS_PRO`-gated so the free build never advertises
  absent endpoints.

---

## 6. Guardrails

1. **No fan-out anywhere in this scope** → no rate-limit exposure. Every lane is
   one bounded query (`limit` capped, e.g. `min(limit, 20)`), same as the
   existing `/memory/recall`.
2. **Read-only pull lanes** — `GET` endpoints never mutate; `remember`/`facts`
   writes are the existing PR-4/PR-8 paths, unchanged.
3. **Opt-in / safe defaults** — `memory_pushed_recall_limit=2` preserves a small
   pushed recall; set `3` to exactly reproduce today.
4. **Events** — `memory_queried` (and nothing heavier) on the existing pipeline.
5. **Path safety** — `repo-map?subdir=` rejects `..`/absolute paths before
   scanning.

---

## 7. Tests

- Each GET lane: returns the expected shape; repo-less task → empty; `limit`
  capped; emits `memory_queried`.
- `recall_decisions` returns only `memory_type='decision'` rows.
- `build_repo_map(subdir=...)` scopes the scan; traversal (`..`) rejected.
- Agent prompt block present only when `_HAS_PRO`; `memory_pushed_recall_limit`
  honoured (3 → unchanged behaviour, 0 → no pushed recall).
- Free build (pro stripped) still boots; lanes absent, no prompt mention.

---

## 8. Bottom line

DevServer is already strong at *retrieval and compression* (hybrid RRF, decay,
facts, typed KB, local embeddings). The cheapest RLM-shaped win is to **turn
memory into tools the agent pulls on demand** (E1) plus **repo-map drill-down**
(E4) — delivering "context-as-navigable-variable" with **zero recursive-fan-out
cost**. The heavy map-reduce paths (E2/E3) and the `rlms` library are explicitly
out of scope here; they buy little that E1's drawer activation doesn't, while
re-introducing the exact rate-limit/latency risk this pipeline is built to
avoid.

---

### Sources
- [alexzhang13/rlm (GitHub)](https://github.com/alexzhang13/rlm) · [README](https://github.com/alexzhang13/rlm/blob/main/README.md) · [rlm-minimal](https://github.com/alexzhang13/rlm-minimal)
- [Recursive Language Models — Alex L. Zhang (blog, 2025)](https://alexzhang13.github.io/blog/2025/rlm/)
- arXiv preprint `2512.24601` (Zhang, Kraska, Khattab)
- DevServer internals: `services/pro/repo_kb.py`, `services/compaction.py`, `services/repo_map.py`, `services/agent_runner.py` (`_build_prompt`), `routes/pro_internal.py`, `CLAUDE.md`, `docs/repo-memory-refactor-plan.md`
