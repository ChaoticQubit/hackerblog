CREATE TABLE sites (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled', 'removed')),
    discovery_method TEXT CHECK (discovery_method IN ('feed', 'sitemap', 'linkdiff')),
    feed_url TEXT,
    sitemap_url TEXT,
    post_url_pattern TEXT,
    backfilled_at TEXT,
    last_polled_at TEXT,
    last_success_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    config_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE http_cache (
    url TEXT PRIMARY KEY,
    etag TEXT,
    last_modified TEXT,
    last_status INTEGER,
    fetched_at TEXT NOT NULL
);

CREATE TABLE robots_cache (
    origin TEXT PRIMARY KEY,
    body TEXT,
    policy TEXT NOT NULL CHECK (policy IN ('parsed', 'allow_all', 'disallow_all')),
    fetched_at TEXT NOT NULL
);

CREATE TABLE posts (
    id INTEGER PRIMARY KEY,
    site_id INTEGER NOT NULL REFERENCES sites(id),
    guid TEXT NOT NULL,
    url TEXT NOT NULL,
    canonical_url TEXT NOT NULL UNIQUE,
    title TEXT,
    author TEXT,
    published_at TEXT,
    discovered_at TEXT NOT NULL,
    origin TEXT NOT NULL CHECK (origin IN ('backfill', 'new', 'manual')),
    status TEXT NOT NULL CHECK (status IN ('discovered', 'queued', 'fetching', 'extracted', 'generating', 'rendering', 'ready', 'skipped', 'failed', 'dead')),
    skip_reason TEXT,
    reading_status TEXT NOT NULL DEFAULT 'unread' CHECK (reading_status IN ('unread', 'read', 'learned', 'archived')),
    body_md TEXT,
    word_count INTEGER,
    content_hash TEXT,
    truncated INTEGER NOT NULL DEFAULT 0,
    flags TEXT NOT NULL DEFAULT '[]',
    digest_json TEXT,
    last_error TEXT,
    processed_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (site_id, guid)
);
CREATE INDEX posts_site_published ON posts (site_id, published_at DESC);
CREATE INDEX posts_status ON posts (status);
CREATE INDEX posts_reading ON posts (reading_status, published_at DESC);
CREATE INDEX posts_content_hash ON posts (content_hash);

CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('process_post', 'deliver')),
    post_id INTEGER REFERENCES posts(id),
    notification_id INTEGER REFERENCES notifications(id),
    stage TEXT NOT NULL DEFAULT 'all' CHECK (stage IN ('all', 'lesson', 'render')),
    state TEXT NOT NULL CHECK (state IN ('pending', 'running', 'done', 'failed', 'dead', 'cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    run_after TEXT NOT NULL,
    lease_until TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((kind = 'process_post' AND post_id IS NOT NULL) OR (kind = 'deliver' AND notification_id IS NOT NULL))
);
CREATE INDEX jobs_ready ON jobs (state, run_after);
CREATE UNIQUE INDEX jobs_one_open_per_post ON jobs (post_id) WHERE kind = 'process_post' AND state IN ('pending', 'running');

CREATE TABLE artifacts (
    id INTEGER PRIMARY KEY,
    post_id INTEGER NOT NULL REFERENCES posts(id),
    kind TEXT NOT NULL CHECK (kind IN ('notes', 'lesson', 'digest')),
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    model TEXT,
    prompt_version TEXT,
    template_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (post_id, kind)
);

CREATE TABLE llm_cache (
    key TEXT PRIMARY KEY,
    stage TEXT NOT NULL CHECK (stage IN ('digest', 'lesson')),
    model TEXT NOT NULL,
    output_json TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE llm_usage (
    day TEXT NOT NULL,
    model TEXT NOT NULL,
    stage TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, model, stage)
);

CREATE TABLE poll_runs (
    id INTEGER PRIMARY KEY,
    trigger TEXT NOT NULL CHECK (trigger IN ('cli', 'scheduler', 'api')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    sites_ok INTEGER NOT NULL DEFAULT 0,
    sites_failed INTEGER NOT NULL DEFAULT 0,
    new_posts INTEGER NOT NULL DEFAULT 0,
    processed_posts INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE events (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL CHECK (type IN ('post.processed', 'post.failed', 'site.unhealthy', 'site.recovered', 'backfill.completed')),
    site_id INTEGER REFERENCES sites(id),
    post_id INTEGER REFERENCES posts(id),
    poll_run_id INTEGER REFERENCES poll_runs(id),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX events_created ON events (created_at);

CREATE TABLE notifications (
    id INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id),
    channel TEXT NOT NULL CHECK (channel IN ('webhook', 'apprise_instant', 'apprise_digest')),
    state TEXT NOT NULL CHECK (state IN ('pending', 'sent', 'failed', 'dead', 'suppressed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    sent_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (event_id, channel)
);
CREATE INDEX notifications_pending ON notifications (channel, state);

CREATE TABLE quiz_attempts (
    id INTEGER PRIMARY KEY,
    post_id INTEGER NOT NULL REFERENCES posts(id),
    score INTEGER NOT NULL CHECK (score >= 0),
    total INTEGER NOT NULL CHECK (total > 0 AND score <= total),
    created_at TEXT NOT NULL
);

CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE VIRTUAL TABLE search_index USING fts5 (
    title, summary, notes, body,
    post_id UNINDEXED,
    tokenize = 'porter unicode61'
);
