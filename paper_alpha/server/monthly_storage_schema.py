"""Schema-ten monthly resources, independent of daily task identities and paths."""

SCHEMA = """
CREATE TABLE monthly_experiments(
 id TEXT PRIMARY KEY,protocol_id TEXT NOT NULL REFERENCES research_protocols(id),
 protocol TEXT NOT NULL,config TEXT NOT NULL,input TEXT NOT NULL,input_digest TEXT NOT NULL,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL,status TEXT NOT NULL,
 attempt_count INTEGER NOT NULL DEFAULT 0,attempt_id TEXT,worker_id TEXT,heartbeat REAL,
 error TEXT,phase TEXT,result_digest TEXT,verification TEXT
);
CREATE TABLE monthly_attempts(
 id TEXT PRIMARY KEY,experiment_id TEXT NOT NULL REFERENCES monthly_experiments(id),
 number INTEGER NOT NULL,worker_id TEXT NOT NULL,status TEXT NOT NULL,
 started_at TEXT NOT NULL,finished_at TEXT,error TEXT,result_digest TEXT,verification TEXT,
 UNIQUE(experiment_id,number)
);
CREATE TABLE monthly_events(
 id INTEGER PRIMARY KEY AUTOINCREMENT,experiment_id TEXT NOT NULL REFERENCES monthly_experiments(id),
 attempt_id TEXT REFERENCES monthly_attempts(id),kind TEXT NOT NULL,created_at TEXT NOT NULL,payload TEXT NOT NULL
);
CREATE TABLE monthly_reviews(
 id TEXT PRIMARY KEY,experiment_id TEXT NOT NULL REFERENCES monthly_experiments(id),
 attempt_id TEXT NOT NULL REFERENCES monthly_attempts(id),result_digest TEXT NOT NULL,
 payload TEXT NOT NULL,digest TEXT NOT NULL,created_at TEXT NOT NULL
);
CREATE TABLE monthly_reports(
 id TEXT PRIMARY KEY,experiment_id TEXT NOT NULL REFERENCES monthly_experiments(id),
 attempt_id TEXT NOT NULL REFERENCES monthly_attempts(id),result_digest TEXT NOT NULL,
 payload TEXT NOT NULL,digest TEXT NOT NULL,created_at TEXT NOT NULL
);
CREATE TABLE monthly_receipts(
 operation TEXT NOT NULL,idempotency_key TEXT NOT NULL,request_digest TEXT NOT NULL,
 response TEXT NOT NULL,response_digest TEXT NOT NULL,
 experiment_id TEXT NOT NULL REFERENCES monthly_experiments(id),created_at TEXT NOT NULL,
 PRIMARY KEY(operation,idempotency_key)
);
CREATE INDEX monthly_queue ON monthly_experiments(status,created_at,id);
CREATE INDEX monthly_attempt_history ON monthly_attempts(experiment_id,number);
CREATE INDEX monthly_event_history ON monthly_events(experiment_id,id);
CREATE INDEX monthly_review_history ON monthly_reviews(experiment_id,created_at,id);
CREATE INDEX monthly_report_history ON monthly_reports(experiment_id,created_at,id);
"""
