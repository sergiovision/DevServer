-- DevServer v2 — consolidated initial schema
-- Single migration: run once on a fresh database.
-- Usage: psql -U devserver -d devserver -f database/migrations/001_initial.sql
--
-- This file is the SINGLE SOURCE OF TRUTH for the baseline schema. The former
-- migrations 008 (messaging/webhooks/compaction), 009 (github provider),
-- 010 (memory quality), 003 (goal-graph decomposition), 004 (decision
-- points), 005 (skills + schedules) and 006 (drop projects) have been folded
-- in here — their tables, columns, indexes, triggers and settings now live
-- inline below. New schema changes go in 002_*.sql and up, on top of this
-- baseline.
--
-- Idempotent: every statement uses IF NOT EXISTS / ADD COLUMN IF NOT EXISTS /
-- ON CONFLICT DO NOTHING, so re-running on an existing database is safe. The
-- "Legacy upgrades" section near the end brings databases created from older
-- baselines (which CREATE TABLE IF NOT EXISTS cannot retrofit) up to date.

-- ─── Extensions ─────────────────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS vector;

-- ─── Enums ──────────────────────────────────────────────────────────────────
DO $$ BEGIN
    CREATE TYPE pgqueuer_status AS ENUM (
        'queued', 'picked', 'successful', 'exception', 'canceled', 'deleted'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ─── Repos ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS repos (
    id                  SERIAL PRIMARY KEY,
    name                VARCHAR(128) UNIQUE NOT NULL,
    gitea_url           VARCHAR(512) NOT NULL,
    gitea_owner         VARCHAR(128) NOT NULL,
    gitea_repo          VARCHAR(128) NOT NULL,
    clone_url           VARCHAR(512) NOT NULL,
    default_branch      VARCHAR(64)  DEFAULT 'main',
    build_cmd           TEXT         DEFAULT '',
    test_cmd            TEXT         DEFAULT '',
    lint_cmd            TEXT         DEFAULT '',
    pre_cmd             TEXT         DEFAULT '',
    claude_model        VARCHAR(32)  DEFAULT 'sonnet',
    claude_allowed_tools TEXT        DEFAULT 'Read,Write,Edit,Glob,Grep,Bash',
    max_retries         INT          DEFAULT 2,
    timeout_minutes     INT          DEFAULT 60,
    claude_md_path      VARCHAR(256) DEFAULT 'CLAUDE.md',
    gitea_token         VARCHAR(256) DEFAULT '',
    -- Git host provider: 'gitea' (default, legacy behaviour), 'github', or
    -- 'local'. Controls the clone-URL auth scheme and which PR REST API is
    -- used. 'local' means a folder on the worker host: the path lives in
    -- gitea_url (the "Local Root Folder"), clone_url stays empty, and the
    -- agent runs git directly in the folder — no clone, no push, no PR.
    -- (Formerly migration 009.)
    provider            VARCHAR(16)  NOT NULL DEFAULT 'gitea',
    active              BOOLEAN      DEFAULT true,
    created_at          TIMESTAMPTZ  DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  DEFAULT NOW()
);

-- ─── Skills (formerly migration 005) ─────────────────────────────────────────
-- Skills are SKILL.md folders (progressive disclosure) registered here so the
-- dashboard can list them and tasks can link to one. Declared before tasks
-- because tasks.skill_id references this table.
CREATE TABLE IF NOT EXISTS skills (
    id             SERIAL PRIMARY KEY,
    name           VARCHAR(64)  UNIQUE NOT NULL,
    description    TEXT         NOT NULL DEFAULT '',
    path           VARCHAR(512) NOT NULL,
    domain         VARCHAR(16),     -- free-form grouping label, or NULL = generic
    version        VARCHAR(16)  NOT NULL DEFAULT '1',
    enabled        BOOLEAN      NOT NULL DEFAULT TRUE,
    eval_pass_rate NUMERIC(5,2),    -- optional: % pass on a should/should-not eval
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ─── Tasks ──────────────────────────────────────────────────────────────────
-- statuses:  pending → queued → running → verifying → done / failed / blocked / cancelled
-- priority:  1=critical  2=high  3=medium  4=low
-- claude_mode: 'max' (Claude Max subscription, cost reported as 0) | 'api' (ANTHROPIC_API_KEY)
CREATE TABLE IF NOT EXISTS tasks (
    id                  SERIAL PRIMARY KEY,
    repo_id             INT          REFERENCES repos(id) ON DELETE CASCADE,
    task_key            VARCHAR(64)  NOT NULL,
    title               TEXT         NOT NULL,
    description         TEXT,
    acceptance          TEXT,
    priority            INT          NOT NULL DEFAULT 3,
    labels              TEXT[]       DEFAULT '{}',
    mode                VARCHAR(16)  DEFAULT 'autonomous',
    status              VARCHAR(24)  DEFAULT 'pending',
    depends_on          INT[]        DEFAULT '{}',
    queue_job_id        VARCHAR(128),
    skip_verify         BOOLEAN      DEFAULT FALSE,
    claude_mode         VARCHAR(8)   DEFAULT 'max',
    agent_vendor        VARCHAR(16)  NOT NULL DEFAULT 'anthropic',
    claude_model        VARCHAR(32)  DEFAULT NULL,
    -- Auto-vendor-failover: backup vendor/model used when the primary
    -- exhausts all retries (declared once here; no trailing ALTER).
    backup_vendor       VARCHAR(16)  DEFAULT NULL,
    backup_model        VARCHAR(32)  DEFAULT 'claude-sonnet-4-6',
    max_turns           INTEGER      DEFAULT NULL,
    is_continuation     BOOLEAN      NOT NULL DEFAULT FALSE,
    -- Phase 2: per-task budget circuit breaker
    max_cost_usd        NUMERIC(10,4),
    max_wall_seconds    INTEGER,
    -- Phase 1: interactive-mode plan gate
    plan_approved_at    TIMESTAMPTZ,
    plan_rejected_at    TIMESTAMPTZ,
    -- Git output mode: branch (PR), commit (direct to default), patch (no push)
    git_flow            VARCHAR(16)  DEFAULT 'branch',
    -- Context compaction (formerly migration 008): running summary for
    -- long-horizon tasks; on the next retry it replaces the Phase-1 context
    -- and session_id is cleared so the CLI starts fresh.
    compacted_context   TEXT,
    compacted_at        TIMESTAMPTZ,
    compact_count       INT          NOT NULL DEFAULT 0,
    -- Strict abstain gate (formerly migration 010): when the reality gate
    -- scores below the configured threshold the task is blocked before an
    -- agent run is spent, and the reason/score are recorded for the dashboard.
    abstain_reason      TEXT,
    reality_score       INT,
    -- A task may be augmented by a reusable Skill — its SKILL.md body is
    -- injected into the prompt at run time (formerly migration 005).
    skill_id            INT          REFERENCES skills(id) ON DELETE SET NULL,
    -- Side-effect gate suspension bookkeeping (formerly migration 004);
    -- parallels the plan-gate columns above.
    suspended_reason    TEXT,
    suspended_at        TIMESTAMPTZ,
    created_by          VARCHAR(64)  DEFAULT 'ui',
    created_at          TIMESTAMPTZ  DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  DEFAULT NOW(),
    UNIQUE(repo_id, task_key)
);

-- ─── Task Runs ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS task_runs (
    id          SERIAL PRIMARY KEY,
    task_id     INT          REFERENCES tasks(id) ON DELETE CASCADE,
    attempt     INT          NOT NULL DEFAULT 1,
    session_id  VARCHAR(128),
    branch      VARCHAR(256),
    pr_url      VARCHAR(512),
    status      VARCHAR(16)  NOT NULL DEFAULT 'started',
    started_at  TIMESTAMPTZ  DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    cost_usd    DECIMAL(10,4) DEFAULT 0,
    duration_ms BIGINT        DEFAULT 0,
    turns       INT           DEFAULT 0,
    error_log   TEXT,
    claude_output TEXT,
    -- Phase 1: plan produced during interactive-mode plan gate
    plan_json   JSONB,
    created_at  TIMESTAMPTZ  DEFAULT NOW()
);

-- ─── Task Events ─────────────────────────────────────────────────────────────
-- event_type is a free-form VARCHAR(32) (no enum). Known values:
--   status_change, log_line, progress, cost_update, error,
--   repo_map_built, reality_signal, memory_recall, error_classified,
--   plan_pending, plan_approved, plan_rejected,
--   budget_warning, budget_exceeded,
--   pr_preflight_pass, pr_preflight_fail, patches_generated,
--   rate_limit_backoff, vendor_failover,
--   reality_abstain, message_redacted, decision_recorded,
--   message_sent, message_received, webhook_triggered, context_compacted
CREATE TABLE IF NOT EXISTS task_events (
    id          BIGSERIAL PRIMARY KEY,
    task_id     INT         REFERENCES tasks(id) ON DELETE CASCADE,
    run_id      INT         REFERENCES task_runs(id) ON DELETE CASCADE,
    event_type  VARCHAR(32) NOT NULL,
    payload     JSONB       NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ─── Settings ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS settings (
    key        VARCHAR(64) PRIMARY KEY,
    value      JSONB       NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ─── Daily Stats ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS daily_stats (
    date               DATE PRIMARY KEY,
    completed          INT           DEFAULT 0,
    failed             INT           DEFAULT 0,
    cost_usd           DECIMAL(10,4) DEFAULT 0,
    total_duration_ms  BIGINT        DEFAULT 0,
    total_turns        INT           DEFAULT 0
);

-- ─── Agent Memory ────────────────────────────────────────────────────────────
-- memory_type values: experience, error_pattern, solution, context, decision
CREATE TABLE IF NOT EXISTS agent_memory (
    id          BIGSERIAL PRIMARY KEY,
    repo_id     INT         REFERENCES repos(id) ON DELETE CASCADE,
    task_id     INT         REFERENCES tasks(id) ON DELETE SET NULL,
    content     TEXT        NOT NULL,
    embedding   vector(1536),
    memory_type VARCHAR(32) DEFAULT 'experience',
    metadata    JSONB       DEFAULT '{}',
    -- Memory decay + auto-archive (formerly migration 010): recency weighting
    -- in recall, plus a soft-delete sweep that spares actually-used rows.
    archived_at      TIMESTAMPTZ,
    last_recalled_at TIMESTAMPTZ,
    recall_count     INT     NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ─── PgQueuer ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pgqueuer (
    id               SERIAL PRIMARY KEY,
    priority         INT          NOT NULL,
    queue_manager_id UUID,
    created          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    heartbeat        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    execute_after    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    status           pgqueuer_status NOT NULL,
    entrypoint       TEXT         NOT NULL,
    dedupe_key       TEXT,
    payload          BYTEA,
    headers          JSONB
);

CREATE TABLE IF NOT EXISTS pgqueuer_log (
    id          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    created     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    job_id      BIGINT      NOT NULL,
    status      pgqueuer_status NOT NULL,
    priority    INT         NOT NULL,
    entrypoint  TEXT        NOT NULL,
    traceback   JSONB       DEFAULT NULL,
    aggregated  BOOLEAN     DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS pgqueuer_statistics (
    id          SERIAL PRIMARY KEY,
    created     TIMESTAMPTZ NOT NULL DEFAULT DATE_TRUNC('sec', NOW() AT TIME ZONE 'UTC'),
    count       BIGINT      NOT NULL,
    priority    INT         NOT NULL,
    status      pgqueuer_status NOT NULL,
    entrypoint  TEXT        NOT NULL
);

CREATE TABLE IF NOT EXISTS pgqueuer_schedules (
    id          SERIAL PRIMARY KEY,
    expression  TEXT        NOT NULL,
    entrypoint  TEXT        NOT NULL,
    heartbeat   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    next_run    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_run    TIMESTAMPTZ,
    status      pgqueuer_status DEFAULT 'queued',
    UNIQUE (expression, entrypoint)
);

-- ─── Worker State ────────────────────────────────────────────────────────────
-- Persists night-cycle and other stateful worker flags across restarts.
CREATE TABLE IF NOT EXISTS worker_state (
    key        VARCHAR(64) PRIMARY KEY,
    value      JSONB       NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─── Repo Locks ──────────────────────────────────────────────────────────────
-- Per-repo advisory lock so only one agent runs per repo at a time.
CREATE TABLE IF NOT EXISTS repo_locks (
    repo_name   VARCHAR(128) PRIMARY KEY,
    task_key    VARCHAR(64)  NOT NULL,
    acquired_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMPTZ  NOT NULL
);

-- ─── Ideas ───────────────────────────────────────────────────────────────────
-- Hierarchical folder + idea tree; ideas can be promoted to tasks.
-- kind: 'folder' | 'idea'
--
-- The ideas tree doubles as the Goal Graph (formerly migration 003): rather
-- than a parallel goal_nodes table, parent/child = parent_id, leaf→task =
-- task_id + tasked, sibling order = sort_order, leaf execution order =
-- tasks.depends_on. The node_* columns hold the recursive-decomposition state:
--   node_type   : NULL = legacy idea/folder; else 'goal' | 'subtask' | 'leaf'
--   node_status : draft | expanding | ready | blocked | running | done | failed | abandoned
CREATE TABLE IF NOT EXISTS ideas (
    id              SERIAL PRIMARY KEY,
    parent_id       INT          REFERENCES ideas(id) ON DELETE CASCADE,
    kind            VARCHAR(8)   NOT NULL CHECK (kind IN ('folder', 'idea')),
    title           VARCHAR(256) NOT NULL,
    content         TEXT         NOT NULL DEFAULT '',
    tasked          BOOLEAN      NOT NULL DEFAULT FALSE,
    task_id         INT          REFERENCES tasks(id) ON DELETE SET NULL,
    sort_order      INT          NOT NULL DEFAULT 0,
    node_type       VARCHAR(12),
    node_status     VARCHAR(12)  NOT NULL DEFAULT 'draft',
    depth           INT          NOT NULL DEFAULT 0,
    evaluator_score INT,
    expand_reason   TEXT,
    stop_reason     TEXT,
    rollup_summary  TEXT,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ─── Schedules (formerly migrations 005 + 006) ───────────────────────────────
-- Cron-ish jobs that re-run an existing task: on every fire the task is reset
-- to 'pending' and enqueued (a task already queued/running/verifying is left
-- alone). Loaded by services/scheduler.py. Run history reuses
-- task_runs/task_events — there is intentionally no schedule_runs table.
CREATE TABLE IF NOT EXISTS schedules (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(128) NOT NULL,
    -- A small cron subset understood by scheduler.py:
    --   '@hourly' | '@daily' | 'every <N>m' | 'every <N>h' | 'HH:MM' (daily UTC)
    cron_expr   VARCHAR(64)  NOT NULL DEFAULT '@daily',
    -- The task this schedule re-runs. Deleting the task deletes its schedules.
    task_id     INT          REFERENCES tasks(id) ON DELETE CASCADE,
    enabled     BOOLEAN      NOT NULL DEFAULT TRUE,
    last_run_at TIMESTAMPTZ,
    next_run_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ─── Decision points (formerly migration 004) ────────────────────────────────
-- Human-in-the-loop side-effect gate. When an agent is about to take an
-- external / irreversible action (spend money, send a message, publish,
-- clinical/legal, delete), the gate classifier records a decision_point and
-- the task suspends on the existing ``blocked`` status until a human
-- approves/rejects it from the operator inbox. Resume reuses /continue.
-- Notification reuses task_messages ('operator' key); a resolution is
-- recorded in-row (status/resolved_at/resolved_by) — there is intentionally
-- no separate approvals table. Opt-in via the `side_effect_gate` setting.
CREATE TABLE IF NOT EXISTS decision_points (
    id              BIGSERIAL PRIMARY KEY,
    task_id         INT          REFERENCES tasks(id) ON DELETE CASCADE,
    node_id         INT          REFERENCES ideas(id) ON DELETE SET NULL,
    kind            VARCHAR(16)  NOT NULL
                        CHECK (kind IN ('spend_money','send_message','publish',
                                        'clinical','legal','irreversible','ambiguous')),
    severity        VARCHAR(12)  NOT NULL DEFAULT 'blocking'
                        CHECK (severity IN ('blocking','non_blocking')),
    payload         JSONB        NOT NULL DEFAULT '{}',
    proposed_action TEXT,
    status          VARCHAR(12)  NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open','approved','rejected','edited','expired')),
    resume_token    VARCHAR(64),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ,
    resolved_by     VARCHAR(64)
);

-- ─── Task Templates ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS task_templates (
    id                  SERIAL PRIMARY KEY,
    name                VARCHAR(128) NOT NULL,
    description         TEXT,
    acceptance          TEXT,
    git_flow            VARCHAR(16)  DEFAULT 'branch',
    claude_mode         VARCHAR(8)   DEFAULT 'max',
    agent_vendor        VARCHAR(16)  DEFAULT 'anthropic',
    claude_model        VARCHAR(32)  DEFAULT NULL,
    backup_vendor       VARCHAR(16)  DEFAULT NULL,
    backup_model        VARCHAR(32)  DEFAULT NULL,
    max_turns           INTEGER      DEFAULT NULL,
    skip_verify         BOOLEAN      DEFAULT FALSE,
    created_at          TIMESTAMPTZ  DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  DEFAULT NOW()
);

-- ─── Inter-task messaging (formerly migration 008) ──────────────────────────
-- A small bus so one task can ask another task to do something, poll its
-- state, or hand off a result. A message is either between two concrete tasks
-- (from_task_id + to_task_id) or addressed to a human operator
-- (to_task_id IS NULL, to_task_key = 'operator'). The notify trigger converts
-- every insert into a PG NOTIFY so the dashboard sees messages live.
CREATE TABLE IF NOT EXISTS task_messages (
    id           BIGSERIAL PRIMARY KEY,
    from_task_id INT         REFERENCES tasks(id) ON DELETE SET NULL,
    to_task_id   INT         REFERENCES tasks(id) ON DELETE CASCADE,
    -- Destination key resolved at insert time so a dropped task still keeps its history.
    to_task_key  VARCHAR(64) NOT NULL,
    from_task_key VARCHAR(64),
    -- 'request' | 'response' | 'note' | 'handoff'
    kind         VARCHAR(16) NOT NULL DEFAULT 'note',
    subject      VARCHAR(256),
    body         TEXT        NOT NULL,
    payload      JSONB       NOT NULL DEFAULT '{}',
    read_at      TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─── Webhook triggers (formerly migration 008) ──────────────────────────────
-- A webhook binds an opaque URL token to a repo + (optional) task template.
-- External systems POST JSON at /api/webhooks/<token>; the handler validates
-- the optional HMAC secret, applies the template, and inserts a row in tasks.
CREATE TABLE IF NOT EXISTS webhook_triggers (
    id               SERIAL PRIMARY KEY,
    token            VARCHAR(64)  UNIQUE NOT NULL,
    name             VARCHAR(128) NOT NULL,
    repo_id          INT          REFERENCES repos(id) ON DELETE CASCADE NOT NULL,
    template_id      INT          REFERENCES task_templates(id) ON DELETE SET NULL,
    -- Optional HMAC-SHA256 secret. When set, incoming requests must present a
    -- matching ``X-DevServer-Signature`` header (sha256=HEX).
    secret           VARCHAR(128),
    -- Mustache-ish templates: ``{{json.path}}`` gets replaced with payload
    -- fields at fire time. Fallbacks used when not provided.
    title_template   TEXT         DEFAULT 'Webhook: {{name}}',
    description_template TEXT     DEFAULT '',
    task_key_prefix  VARCHAR(16)  DEFAULT 'HOOK',
    priority         INT          NOT NULL DEFAULT 3,
    enabled          BOOLEAN      NOT NULL DEFAULT TRUE,
    last_fired_at    TIMESTAMPTZ,
    fire_count       INT          NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ─── Indexes ─────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_tasks_repo_status    ON tasks(repo_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_status         ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_priority       ON tasks(priority);
CREATE INDEX IF NOT EXISTS idx_tasks_agent_vendor    ON tasks(agent_vendor);
CREATE INDEX IF NOT EXISTS idx_tasks_plan_pending   ON tasks(id)
    WHERE plan_approved_at IS NULL
      AND plan_rejected_at IS NULL
      AND mode   = 'interactive'
      AND status = 'running';

CREATE INDEX IF NOT EXISTS idx_task_runs_task_id    ON task_runs(task_id);
CREATE INDEX IF NOT EXISTS idx_task_runs_status     ON task_runs(status);

CREATE INDEX IF NOT EXISTS idx_task_events_task_id  ON task_events(task_id);
CREATE INDEX IF NOT EXISTS idx_task_events_created  ON task_events(created_at);

CREATE INDEX IF NOT EXISTS idx_agent_memory_repo    ON agent_memory(repo_id);
CREATE INDEX IF NOT EXISTS idx_agent_memory_type    ON agent_memory(memory_type);
CREATE INDEX IF NOT EXISTS idx_agent_memory_embedding ON agent_memory
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
-- Partial index so "active memories for a repo" stays cheap after archiving
-- (formerly migration 010).
CREATE INDEX IF NOT EXISTS idx_agent_memory_active
    ON agent_memory (repo_id) WHERE archived_at IS NULL;

CREATE INDEX IF NOT EXISTS pgqueuer_priority_id_id1_idx ON pgqueuer(priority ASC, id DESC)
    INCLUDE (id) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS pgqueuer_updated_id_id1_idx  ON pgqueuer(updated ASC, id DESC)
    INCLUDE (id) WHERE status = 'picked';
CREATE INDEX IF NOT EXISTS pgqueuer_queue_manager_id_idx ON pgqueuer(queue_manager_id)
    WHERE queue_manager_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS pgqueuer_unique_dedupe_key ON pgqueuer(dedupe_key)
    WHERE status IN ('queued', 'picked') AND dedupe_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS pgqueuer_log_not_aggregated  ON pgqueuer_log ((1)) WHERE NOT aggregated;
CREATE INDEX IF NOT EXISTS pgqueuer_log_created         ON pgqueuer_log(created);
CREATE INDEX IF NOT EXISTS pgqueuer_log_status          ON pgqueuer_log(status);
CREATE INDEX IF NOT EXISTS pgqueuer_log_job_id_status   ON pgqueuer_log(job_id, created DESC);

CREATE UNIQUE INDEX IF NOT EXISTS pgqueuer_statistics_unique_count ON pgqueuer_statistics(
    priority,
    DATE_TRUNC('sec', created AT TIME ZONE 'UTC'),
    status,
    entrypoint
);

CREATE INDEX IF NOT EXISTS repo_locks_expires_idx ON repo_locks(expires_at);

CREATE INDEX IF NOT EXISTS idx_ideas_parent ON ideas(parent_id);
CREATE INDEX IF NOT EXISTS idx_ideas_task   ON ideas(task_id);
-- Fast dashboard filtering of live goal-graph nodes — excludes legacy ideas
-- (formerly migration 003).
CREATE INDEX IF NOT EXISTS idx_ideas_node_status
    ON ideas (node_status) WHERE node_type IS NOT NULL;

-- Skills + schedules indexes (formerly migration 005).
CREATE INDEX IF NOT EXISTS idx_skills_domain ON skills(domain);
CREATE INDEX IF NOT EXISTS idx_schedules_enabled ON schedules(enabled) WHERE enabled;

-- Decision-point indexes (formerly migration 004): fast lookup of the open
-- gate for a running/blocked task.
CREATE INDEX IF NOT EXISTS idx_decision_points_open
    ON decision_points (task_id) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_decision_points_status
    ON decision_points (status, created_at);

-- Messaging indexes (formerly migration 008).
CREATE INDEX IF NOT EXISTS idx_task_messages_to_unread
    ON task_messages(to_task_id, created_at)
    WHERE read_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_task_messages_from   ON task_messages(from_task_id);
CREATE INDEX IF NOT EXISTS idx_task_messages_to_key ON task_messages(to_task_key);

-- Webhook indexes (formerly migration 008).
CREATE INDEX IF NOT EXISTS idx_webhook_triggers_repo   ON webhook_triggers(repo_id);
CREATE INDEX IF NOT EXISTS idx_webhook_triggers_token  ON webhook_triggers(token);

-- ─── Views ───────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW task_latest_run AS
SELECT DISTINCT ON (task_id)
    task_id, id AS run_id, attempt, status, pr_url, cost_usd,
    duration_ms, turns, error_log, started_at, finished_at
FROM task_runs
ORDER BY task_id, attempt DESC;

-- ─── Functions & Triggers ────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION notify_task_event() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('task_events', json_build_object(
        'id',         NEW.id,
        'task_id',    NEW.task_id,
        'run_id',     NEW.run_id,
        'event_type', NEW.event_type,
        'payload',    NEW.payload,
        'created_at', NEW.created_at
    )::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS task_event_notify ON task_events;
CREATE TRIGGER task_event_notify
    AFTER INSERT ON task_events
    FOR EACH ROW EXECUTE FUNCTION notify_task_event();

CREATE OR REPLACE FUNCTION notify_task_update() RETURNS trigger AS $$
BEGIN
    IF OLD.status IS DISTINCT FROM NEW.status THEN
        PERFORM pg_notify('task_updates', json_build_object(
            'task_id',    NEW.id,
            'old_status', OLD.status,
            'new_status', NEW.status,
            'repo_id',    NEW.repo_id,
            'updated_at', NEW.updated_at
        )::text);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS task_update_notify ON tasks;
CREATE TRIGGER task_update_notify
    AFTER UPDATE ON tasks
    FOR EACH ROW EXECUTE FUNCTION notify_task_update();

CREATE OR REPLACE FUNCTION fn_pgqueuer_changed() RETURNS TRIGGER AS $$
DECLARE
    to_emit BOOLEAN := false;
BEGIN
    IF TG_OP = 'UPDATE' AND OLD IS DISTINCT FROM NEW THEN to_emit := true;
    ELSIF TG_OP IN ('DELETE', 'INSERT', 'TRUNCATE')  THEN to_emit := true;
    END IF;

    IF to_emit THEN
        PERFORM pg_notify('ch_pgqueuer', json_build_object(
            'channel',   'ch_pgqueuer',
            'operation', lower(TG_OP),
            'sent_at',   NOW(),
            'table',     TG_TABLE_NAME,
            'type',      'table_changed_event'
        )::text);
    END IF;

    IF TG_OP IN ('INSERT', 'UPDATE') THEN RETURN NEW;
    ELSIF TG_OP = 'DELETE'           THEN RETURN OLD;
    ELSE                                   RETURN NULL;
    END IF;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tg_pgqueuer_changed ON pgqueuer;
CREATE TRIGGER tg_pgqueuer_changed
    AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON pgqueuer
    EXECUTE FUNCTION fn_pgqueuer_changed();

CREATE OR REPLACE FUNCTION ideas_touch_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS ideas_updated_at ON ideas;
CREATE TRIGGER ideas_updated_at
    BEFORE UPDATE ON ideas
    FOR EACH ROW EXECUTE FUNCTION ideas_touch_updated_at();

-- PG NOTIFY on every messaging insert so the dashboard can live-tail an inbox
-- (formerly migration 008).
CREATE OR REPLACE FUNCTION notify_task_message() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('task_messages', json_build_object(
        'id',           NEW.id,
        'from_task_id', NEW.from_task_id,
        'to_task_id',   NEW.to_task_id,
        'to_task_key',  NEW.to_task_key,
        'from_task_key', NEW.from_task_key,
        'kind',         NEW.kind,
        'subject',      NEW.subject,
        'created_at',   NEW.created_at
    )::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS task_message_notify ON task_messages;
CREATE TRIGGER task_message_notify
    AFTER INSERT ON task_messages
    FOR EACH ROW EXECUTE FUNCTION notify_task_message();

-- ─── Default Settings ────────────────────────────────────────────────────────
-- Includes the opt-in memory-quality / abstain keys (formerly migration 010);
-- every threshold defaults to 0 / false so an unconfigured deployment behaves
-- exactly as before those features existed.
INSERT INTO settings (key, value) VALUES
    ('mode',                  '"autonomous"'),
    ('paused',                'false'),
    ('max_concurrent_agents', '2'),
    ('night_mode_start',      '"23:00"'),
    ('night_mode_end',        '"08:00"'),
    ('auto_enqueue',          'false'),
    ('default_model',         '""'),
    ('notifications_enabled', 'true'),
    ('system_llm_vendor',     '"glm"'),
    ('system_llm_model',      '"glm-5.1"'),
    -- Memory quality + strict abstain (opt-in, safe defaults):
    -- reality_abstain_threshold : 0 disables the abstain gate; e.g. 40 blocks
    --     tasks scoring below 40/100 on the reality gate.
    -- memory_decay_half_life_days: 0 disables recency decay; e.g. 90 halves a
    --     memory's effective score every 90 days.
    -- memory_archive_days        : 0 disables the archive sweep; e.g. 180.
    -- memory_iterative_recall    : false keeps single-pass recall; true enables
    --     LLM-planned multi-hop recall (uses the system LLM).
    ('reality_abstain_threshold',   '0'),
    ('memory_decay_half_life_days', '0'),
    ('memory_archive_days',         '0'),
    ('memory_iterative_recall',     'false'),
    -- Side-effect gate (formerly migration 004): false = the gate never
    -- fires; behaviour unchanged.
    ('side_effect_gate',            'false')
ON CONFLICT (key) DO NOTHING;

-- ─── Legacy upgrades (idempotent) ────────────────────────────────────────────
-- Databases created from an older baseline already have the tables above, so
-- CREATE TABLE IF NOT EXISTS cannot retrofit columns added since. These
-- ALTERs bring such databases up to date; all no-ops on a fresh database.

-- Goal-graph decomposition columns on ideas (formerly migration 003).
ALTER TABLE ideas ADD COLUMN IF NOT EXISTS node_type       VARCHAR(12);
ALTER TABLE ideas ADD COLUMN IF NOT EXISTS node_status     VARCHAR(12) NOT NULL DEFAULT 'draft';
ALTER TABLE ideas ADD COLUMN IF NOT EXISTS depth           INT         NOT NULL DEFAULT 0;
ALTER TABLE ideas ADD COLUMN IF NOT EXISTS evaluator_score INT;
ALTER TABLE ideas ADD COLUMN IF NOT EXISTS expand_reason   TEXT;
ALTER TABLE ideas ADD COLUMN IF NOT EXISTS stop_reason     TEXT;
ALTER TABLE ideas ADD COLUMN IF NOT EXISTS rollup_summary  TEXT;

-- Skill link + side-effect-gate suspension on tasks (formerly 004 + 005).
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS skill_id INT REFERENCES skills(id) ON DELETE SET NULL;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS suspended_reason TEXT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS suspended_at     TIMESTAMPTZ;

-- Remove the Projects feature (formerly migration 006). Projects grouped
-- tasks/ideas into domains and bound schedules to a project; tasks bind to
-- repos directly, the goal graph lives in `ideas`, and schedules re-run an
-- existing task instead. No-op once applied (and on fresh databases).
ALTER TABLE schedules ADD COLUMN IF NOT EXISTS task_id INT REFERENCES tasks(id) ON DELETE CASCADE;
ALTER TABLE schedules DROP COLUMN IF EXISTS project_id;
ALTER TABLE schedules DROP COLUMN IF EXISTS target_kind;
ALTER TABLE schedules DROP COLUMN IF EXISTS skill_id;
-- Legacy project-based schedules have no task to run — remove them. New rows
-- always carry a task_id (enforced by the API), so this is a no-op on replay.
DELETE FROM schedules WHERE task_id IS NULL;
ALTER TABLE tasks DROP COLUMN IF EXISTS project_id;
ALTER TABLE ideas DROP COLUMN IF EXISTS project_id;
DROP TABLE IF EXISTS projects CASCADE;

-- ─── Data backfills (idempotent) ─────────────────────────────────────────────
-- Provider backfill (formerly migration 009): any repo whose clone URL is on
-- github.com is GitHub even if it predates the provider column. No-op on a
-- fresh database; safe to replay.
UPDATE repos
   SET provider = 'github'
 WHERE provider <> 'github'
   AND clone_url ~* '^https?://([^/@]+@)?github\.com/';
