-- DevServer v2 — consolidated initial schema
-- Single migration: run once on a fresh database.
-- Usage: psql -U devserver -d devserver -f database/migrations/001_initial.sql

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
    active              BOOLEAN      DEFAULT true,
    created_at          TIMESTAMPTZ  DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  DEFAULT NOW()
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
-- event_type values: status_change, log_line, progress, cost_update, error,
--   repo_map_built, reality_signal, memory_recall, error_classified,
--   plan_pending, plan_approved, plan_rejected,
--   budget_warning, budget_exceeded,
--   pr_preflight_pass, pr_preflight_fail, patches_generated
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
-- memory_type values: experience, error_pattern, solution, context
CREATE TABLE IF NOT EXISTS agent_memory (
    id          BIGSERIAL PRIMARY KEY,
    repo_id     INT         REFERENCES repos(id) ON DELETE CASCADE,
    task_id     INT         REFERENCES tasks(id) ON DELETE SET NULL,
    content     TEXT        NOT NULL,
    embedding   vector(1536),
    memory_type VARCHAR(32) DEFAULT 'experience',
    metadata    JSONB       DEFAULT '{}',
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
CREATE TABLE IF NOT EXISTS ideas (
    id         SERIAL PRIMARY KEY,
    parent_id  INT          REFERENCES ideas(id) ON DELETE CASCADE,
    kind       VARCHAR(8)   NOT NULL CHECK (kind IN ('folder', 'idea')),
    title      VARCHAR(256) NOT NULL,
    content    TEXT         NOT NULL DEFAULT '',
    tasked     BOOLEAN      NOT NULL DEFAULT FALSE,
    task_id    INT          REFERENCES tasks(id) ON DELETE SET NULL,
    sort_order INT          NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
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

-- ─── Default Settings ────────────────────────────────────────────────────────
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
    ('system_llm_model',      '"glm-5.1"')
ON CONFLICT (key) DO NOTHING;

