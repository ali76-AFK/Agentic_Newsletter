-- Portable schema: identical text runs on PostgreSQL 16 (Alembic) and SQLite (local tests).
-- Conventions: TEXT ids (uuid hex), TEXT UTC timestamps (YYYY-MM-DDTHH:MM:SSZ), INTEGER booleans, TEXT json.
-- Do not put semicolons inside comments or strings in this file (statements are split on them).

CREATE TABLE topics (
  topic_key TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  keywords_en TEXT NOT NULL DEFAULT '[]',
  keywords_de TEXT NOT NULL DEFAULT '[]',
  is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE sources (
  source_key TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  feed_url TEXT NOT NULL,
  language TEXT NOT NULL DEFAULT 'en',
  mode TEXT NOT NULL DEFAULT 'live',
  quality TEXT NOT NULL DEFAULT 'high',
  enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE source_documents (
  id TEXT PRIMARY KEY,
  source_key TEXT NOT NULL REFERENCES sources(source_key),
  external_id TEXT NOT NULL,
  url TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  language TEXT,
  published_at TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  UNIQUE (source_key, external_id)
);
CREATE UNIQUE INDEX ux_docs_url ON source_documents(url);
CREATE UNIQUE INDEX ux_docs_hash ON source_documents(content_hash);
CREATE INDEX ix_docs_published ON source_documents(published_at);

CREATE TABLE subscribers (
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  language TEXT NOT NULL DEFAULT 'en',
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  unsubscribed_at TEXT
);

CREATE TABLE subscriber_topics (
  subscriber_id TEXT NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
  topic_key TEXT NOT NULL REFERENCES topics(topic_key),
  PRIMARY KEY (subscriber_id, topic_key)
);

CREATE TABLE subscription_preferences (
  subscriber_id TEXT PRIMARY KEY REFERENCES subscribers(id) ON DELETE CASCADE,
  free_text_request TEXT,
  plan_json TEXT,
  keywords_json TEXT NOT NULL DEFAULT '[]',
  excluded_keywords_json TEXT NOT NULL DEFAULT '[]',
  source_keys_json TEXT NOT NULL DEFAULT '[]',
  languages_json TEXT NOT NULL DEFAULT '[]',
  regions_json TEXT NOT NULL DEFAULT '[]',
  max_articles INTEGER NOT NULL DEFAULT 5,
  lookback_hours INTEGER NOT NULL DEFAULT 168,
  summary_style TEXT NOT NULL DEFAULT 'concise',
  is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE newsletter_schedules (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  topics_json TEXT NOT NULL DEFAULT '[]',
  source_keys_json TEXT NOT NULL DEFAULT '[]',
  weekdays_json TEXT NOT NULL DEFAULT '[0,1,2,3,4,5,6]',
  delivery_time TEXT NOT NULL,
  timezone TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

CREATE TABLE schedule_runs (
  id TEXT PRIMARY KEY,
  schedule_id TEXT NOT NULL REFERENCES newsletter_schedules(id),
  run_key TEXT NOT NULL UNIQUE,
  scheduled_for TEXT,
  status TEXT NOT NULL,
  message TEXT,
  newsletter_id TEXT,
  started_at TEXT NOT NULL,
  completed_at TEXT
);
CREATE INDEX ix_runs_schedule ON schedule_runs(schedule_id, started_at);

CREATE TABLE newsletters (
  id TEXT PRIMARY KEY,
  schedule_run_id TEXT REFERENCES schedule_runs(id),
  status TEXT NOT NULL,
  topics_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);

CREATE TABLE draft_emails (
  id TEXT PRIMARY KEY,
  newsletter_id TEXT NOT NULL REFERENCES newsletters(id),
  subscriber_id TEXT NOT NULL REFERENCES subscribers(id),
  recipient_email TEXT NOT NULL,
  language TEXT NOT NULL,
  subject TEXT,
  html_body TEXT,
  text_body TEXT,
  status TEXT NOT NULL,
  relevance REAL,
  risk TEXT,
  decision TEXT,
  decision_reason TEXT,
  error TEXT,
  error_stage TEXT,
  message_id TEXT,
  sent_at TEXT,
  reviewed_at TEXT,
  review_note TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX ix_drafts_status ON draft_emails(status);
CREATE INDEX ix_drafts_newsletter ON draft_emails(newsletter_id);

CREATE TABLE newsletter_source_documents (
  newsletter_id TEXT NOT NULL REFERENCES newsletters(id),
  draft_id TEXT NOT NULL REFERENCES draft_emails(id),
  document_id TEXT NOT NULL REFERENCES source_documents(id),
  relevance_score REAL NOT NULL,
  relevance_reason TEXT,
  selected_at TEXT NOT NULL,
  PRIMARY KEY (draft_id, document_id)
);

CREATE TABLE newsletter_claims (
  id TEXT PRIMARY KEY,
  draft_id TEXT NOT NULL REFERENCES draft_emails(id),
  newsletter_id TEXT NOT NULL REFERENCES newsletters(id),
  document_id TEXT,
  label TEXT NOT NULL,
  claim_text TEXT NOT NULL,
  verdict TEXT NOT NULL,
  score REAL NOT NULL,
  reason TEXT,
  quote TEXT,
  kept INTEGER NOT NULL
);

CREATE TABLE verification_runs (
  id TEXT PRIMARY KEY,
  draft_id TEXT NOT NULL REFERENCES draft_emails(id),
  claims_total INTEGER NOT NULL,
  supported INTEGER NOT NULL,
  unsupported INTEGER NOT NULL,
  contradicted INTEGER NOT NULL,
  removed INTEGER NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE generation_runs (
  id TEXT PRIMARY KEY,
  draft_id TEXT NOT NULL REFERENCES draft_emails(id),
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  mode TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  input_document_count INTEGER NOT NULL,
  status TEXT NOT NULL,
  error TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE article_deliveries (
  id TEXT PRIMARY KEY,
  subscriber_id TEXT NOT NULL REFERENCES subscribers(id),
  document_id TEXT NOT NULL REFERENCES source_documents(id),
  newsletter_id TEXT NOT NULL REFERENCES newsletters(id),
  draft_id TEXT NOT NULL REFERENCES draft_emails(id),
  delivered_at TEXT NOT NULL,
  UNIQUE (subscriber_id, document_id)
);

CREATE TABLE agent_runs (
  id TEXT PRIMARY KEY,
  newsletter_id TEXT,
  draft_id TEXT,
  node TEXT NOT NULL,
  model TEXT,
  prompt_version TEXT,
  input_summary TEXT,
  output_summary TEXT,
  status TEXT NOT NULL,
  error TEXT,
  attempts INTEGER NOT NULL DEFAULT 1,
  duration_ms INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE automation_state (
  state_key TEXT PRIMARY KEY,
  value TEXT,
  updated_at TEXT NOT NULL
);
