"""Additive industry resources. No imports and no existing-table mutation."""

SCHEMA = """
CREATE TABLE industry_mom_sources(
 id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL,
 created_at TEXT NOT NULL,metadata_digest TEXT NOT NULL,
 idempotency_key TEXT NOT NULL UNIQUE,request_digest TEXT NOT NULL
);
CREATE TABLE industry_mom_experiments(
 id TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES industry_mom_sources(id),
 source_digest TEXT NOT NULL,input TEXT NOT NULL,input_digest TEXT NOT NULL,config_digest TEXT NOT NULL,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL,status TEXT NOT NULL,
 attempt_count INTEGER NOT NULL DEFAULT 0,attempt_id TEXT,worker_id TEXT,heartbeat REAL,
 error TEXT,phase TEXT,result_digest TEXT,verification TEXT,
 idempotency_key TEXT NOT NULL UNIQUE,request_digest TEXT NOT NULL,record_digest TEXT NOT NULL,
 event_count INTEGER NOT NULL DEFAULT 0,event_digest TEXT NOT NULL
);
CREATE TABLE industry_mom_attempts(
 id TEXT PRIMARY KEY,experiment_id TEXT NOT NULL REFERENCES industry_mom_experiments(id),
 number INTEGER NOT NULL,worker_id TEXT NOT NULL,status TEXT NOT NULL,
 started_at TEXT NOT NULL,finished_at TEXT,error TEXT,result_digest TEXT,verification TEXT,record_digest TEXT NOT NULL,
 UNIQUE(experiment_id,number)
);
CREATE TABLE industry_mom_events(
 id INTEGER PRIMARY KEY AUTOINCREMENT,experiment_id TEXT NOT NULL REFERENCES industry_mom_experiments(id),
 attempt_id TEXT REFERENCES industry_mom_attempts(id),kind TEXT NOT NULL,created_at TEXT NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL
);
CREATE TABLE industry_mom_receipts(
 operation TEXT NOT NULL,idempotency_key TEXT NOT NULL,request TEXT NOT NULL,request_digest TEXT NOT NULL,
 response TEXT NOT NULL,response_digest TEXT NOT NULL,resource_id TEXT NOT NULL,created_at TEXT NOT NULL,receipt_digest TEXT NOT NULL,
 PRIMARY KEY(operation,idempotency_key)
);
CREATE INDEX industry_mom_sources_created ON industry_mom_sources(created_at,id);
CREATE INDEX industry_mom_queue ON industry_mom_experiments(status,created_at,id);
CREATE INDEX industry_mom_attempt_history ON industry_mom_attempts(experiment_id,number);
CREATE INDEX industry_mom_event_history ON industry_mom_events(experiment_id,id);
CREATE INDEX industry_mom_receipt_resource ON industry_mom_receipts(resource_id,operation);
"""
