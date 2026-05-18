-- 009 — Git host provider (GitHub support)
--
-- DevServer historically assumed every repo lived on a Gitea host: the
-- clone URL was authenticated with the ``token:<pat>@host`` userinfo form
-- and PRs were opened against the Gitea REST API. GitHub rejects that
-- userinfo form ("Password authentication is not supported for Git
-- operations") and exposes a different PR API, so each repo now records
-- which provider it belongs to.
--
-- ``gitea`` keeps the exact previous behaviour, so the default is safe for
-- every existing row. Idempotent: safe to re-run (migrate.sh replays all).

ALTER TABLE repos
    ADD COLUMN IF NOT EXISTS provider VARCHAR(16) NOT NULL DEFAULT 'gitea';

-- Backfill: any repo whose clone URL is on github.com is GitHub even if it
-- was added before this column existed.
UPDATE repos
   SET provider = 'github'
 WHERE provider <> 'github'
   AND clone_url ~* '^https?://([^/@]+@)?github\.com/';
