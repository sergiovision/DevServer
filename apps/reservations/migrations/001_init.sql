-- Reservations PoC schema (Postgres 17). Own database, never DevServer's.
-- Part 1: domain tables (hand-written).
-- Part 2: PgQueuer 0.26.3 tables, generated verbatim with
--         `uv run pgq install --dry-run` (do not edit by hand; regenerate
--         if the pgqueuer pin changes).

BEGIN;

CREATE TABLE properties (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id   text NOT NULL,
    name        text NOT NULL,
    capacity    int  NOT NULL CHECK (capacity > 0),
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX properties_client_idx ON properties (client_id);

CREATE TABLE reservation_batches (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id        text NOT NULL,
    idempotency_key  text NOT NULL,
    request_hash     text NOT NULL,
    status           text NOT NULL DEFAULT 'accepted' CHECK (status IN (
                         'accepted', 'processing', 'completed',
                         'completed_with_errors', 'cancelled')),
    total_items      int  NOT NULL CHECK (total_items > 0),
    succeeded        int  NOT NULL DEFAULT 0,
    failed           int  NOT NULL DEFAULT 0,
    cancelled        int  NOT NULL DEFAULT 0,
    callback_url     text NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    started_at       timestamptz NULL,
    finished_at      timestamptz NULL,
    UNIQUE (client_id, idempotency_key)
);

CREATE TABLE reservation_batch_items (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id         uuid NOT NULL REFERENCES reservation_batches (id) ON DELETE CASCADE,
    client_id        text NOT NULL,
    seq              int  NOT NULL,
    client_reference text NOT NULL,
    group_id         text NULL,
    payload          jsonb NOT NULL,
    status           text NOT NULL DEFAULT 'pending' CHECK (status IN (
                         'pending', 'processing', 'succeeded', 'failed', 'cancelled')),
    error_code       text NULL,
    error_message    text NULL,
    error_details    jsonb NULL,
    reservation_id   uuid NULL,
    attempts         int  NOT NULL DEFAULT 0,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (batch_id, seq)
);
CREATE INDEX reservation_batch_items_status_idx
    ON reservation_batch_items (batch_id, status, seq);

CREATE TABLE reservations (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id        text NOT NULL,
    property_id      uuid NOT NULL REFERENCES properties (id),
    client_reference text NOT NULL,
    guest_name       text NOT NULL,
    guests           int  NOT NULL CHECK (guests > 0),
    check_in         date NOT NULL,
    check_out        date NOT NULL,
    status           text NOT NULL DEFAULT 'confirmed' CHECK (status IN ('confirmed')),
    batch_item_id    uuid NULL REFERENCES reservation_batch_items (id) ON DELETE SET NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    CHECK (check_out > check_in),
    UNIQUE (client_id, client_reference)
);
CREATE INDEX reservations_overlap_idx ON reservations (property_id, check_in, check_out);

CREATE TABLE rate_limit_counters (
    client_id     text NOT NULL,
    bucket        text NOT NULL,
    window_start  timestamptz NOT NULL,
    count         int  NOT NULL,
    PRIMARY KEY (client_id, bucket, window_start)
);

-- One row per webhook delivery attempt (audit + "exactly one success").
CREATE TABLE webhook_deliveries (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_id     uuid NOT NULL REFERENCES reservation_batches (id) ON DELETE CASCADE,
    client_id    text NOT NULL,
    attempt      int  NOT NULL,
    status_code  int  NULL,
    success      boolean NOT NULL,
    error        text NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX webhook_deliveries_batch_idx ON webhook_deliveries (batch_id);
CREATE UNIQUE INDEX webhook_deliveries_one_success
    ON webhook_deliveries (batch_id) WHERE success;

-- ---------------------------------------------------------------------------
-- PgQueuer 0.26.3 (generated: uv run pgq install --dry-run)
-- ---------------------------------------------------------------------------
CREATE TYPE pgqueuer_status AS ENUM ('queued', 'picked', 'successful', 'exception', 'canceled', 'deleted');
    CREATE  TABLE pgqueuer (
        id SERIAL PRIMARY KEY,
        priority INT NOT NULL,
        queue_manager_id UUID,
        created TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
        updated TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
        heartbeat TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
        execute_after TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
        status pgqueuer_status NOT NULL,
        entrypoint TEXT NOT NULL,
        dedupe_key TEXT,
        payload BYTEA,
        headers JSONB
    );
    CREATE INDEX pgqueuer_priority_id_id1_idx ON pgqueuer (priority ASC, id DESC)
        INCLUDE (id) WHERE status = 'queued';
    CREATE INDEX pgqueuer_updated_id_id1_idx ON pgqueuer (updated ASC, id DESC)
        INCLUDE (id) WHERE status = 'picked';
    CREATE INDEX pgqueuer_queue_manager_id_idx ON pgqueuer (queue_manager_id)
        WHERE queue_manager_id IS NOT NULL;
    CREATE UNIQUE INDEX IF NOT EXISTS pgqueuer_unique_dedupe_key ON
        pgqueuer (dedupe_key) WHERE ((status IN ('queued', 'picked') AND dedupe_key IS NOT NULL));

    CREATE  TABLE pgqueuer_log (
        id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        created TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
        job_id BIGINT NOT NULL,
        status pgqueuer_status NOT NULL,
        priority INT NOT NULL,
        entrypoint TEXT NOT NULL,
        traceback JSONB DEFAULT NULL,
        aggregated BOOLEAN DEFAULT FALSE
    );
    CREATE INDEX pgqueuer_log_not_aggregated ON pgqueuer_log ((1)) WHERE not aggregated;
    CREATE INDEX pgqueuer_log_created ON pgqueuer_log (created);
    CREATE INDEX pgqueuer_log_status ON pgqueuer_log (status);
    CREATE INDEX pgqueuer_log_job_id_status ON pgqueuer_log (job_id, created DESC);

    CREATE  TABLE pgqueuer_statistics (
        id SERIAL PRIMARY KEY,
        created TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT DATE_TRUNC('sec', NOW() at time zone 'UTC'),
        count BIGINT NOT NULL,
        priority INT NOT NULL,
        status pgqueuer_status NOT NULL,
        entrypoint TEXT NOT NULL
    );
    CREATE UNIQUE INDEX pgqueuer_statistics_unique_count ON pgqueuer_statistics (
        priority,
        DATE_TRUNC('sec', created at time zone 'UTC'),
        status,
        entrypoint
    );

    CREATE  TABLE pgqueuer_schedules (
        id SERIAL PRIMARY KEY,
        expression TEXT NOT NULL, -- Crontab-like schedule definition (e.g., '* * * * *')
        entrypoint TEXT NOT NULL,
        heartbeat TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
        created TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
        updated TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
        next_run TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
        last_run TIMESTAMP WITH TIME ZONE,
        status pgqueuer_status DEFAULT 'queued',
        UNIQUE (expression, entrypoint)
    );

    CREATE FUNCTION fn_pgqueuer_changed() RETURNS TRIGGER AS $$
    DECLARE
        to_emit BOOLEAN := false;  -- Flag to decide whether to emit a notification
    BEGIN
        -- Check operation type and set the emit flag accordingly
        IF TG_OP = 'UPDATE' AND OLD IS DISTINCT FROM NEW THEN
            to_emit := true;
        ELSIF TG_OP = 'DELETE' THEN
            to_emit := true;
        ELSIF TG_OP = 'INSERT' THEN
            to_emit := true;
        ELSIF TG_OP = 'TRUNCATE' THEN
            to_emit := true;
        END IF;

        -- Perform notification if the emit flag is set
        IF to_emit THEN
            PERFORM pg_notify(
                'ch_pgqueuer',
                json_build_object(
                    'channel', 'ch_pgqueuer',
                    'operation', lower(TG_OP),
                    'sent_at', NOW(),
                    'table', TG_TABLE_NAME,
                    'type', 'table_changed_event'
                )::text
            );
        END IF;

        -- Return appropriate value based on the operation
        IF TG_OP IN ('INSERT', 'UPDATE') THEN
            RETURN NEW;
        ELSIF TG_OP = 'DELETE' THEN
            RETURN OLD;
        ELSE
            RETURN NULL; -- For TRUNCATE and other non-row-specific contexts
        END IF;

    END;
    $$ LANGUAGE plpgsql;

    CREATE TRIGGER tg_pgqueuer_changed
    AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON pgqueuer
    EXECUTE FUNCTION fn_pgqueuer_changed();
        

COMMIT;
