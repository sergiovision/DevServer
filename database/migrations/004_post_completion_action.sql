-- 004 — Run an action when a task completes
--
-- Lets a task (or a template) name a follow-up that the worker fires after
-- the runner writes a terminal status: re-run another task, or start a new
-- ``skill`` task in the same repo. Removes the "watch the board and click Run
-- on the next task" chore for simple pipelines without building a workflow
-- engine. See ``services/post_completion.py`` and docs/architecture.md.
--
-- Shape of ``post_completion_action`` (validated by the web tier,
-- ``apps/web/src/lib/post-completion-action.ts``, and re-parsed defensively
-- by the worker):
--   {"kind": "task",  "task_id": 42,          "trigger": "on_success"}
--   {"kind": "skill", "skill_name": "review", "trigger": "always"}
-- trigger: 'on_success' | 'on_failure' | 'always'. NULL = no action, which is
-- the default, so an unconfigured deployment behaves exactly as before.
--
-- Idempotent: safe to re-run on an existing database.

-- ─── Tasks ───────────────────────────────────────────────────────────────────
ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS post_completion_action JSONB;

-- Loop guard: which task's completion started this one. The worker walks this
-- chain to cap automatic hops and to refuse cycles. SET NULL so deleting a
-- task never cascades into the tasks it triggered.
ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS triggered_by_task_id INTEGER
        REFERENCES tasks(id) ON DELETE SET NULL;

-- ─── Templates ───────────────────────────────────────────────────────────────
-- A template carries the action; creating a task from it copies the value.
ALTER TABLE task_templates
    ADD COLUMN IF NOT EXISTS post_completion_action JSONB;
