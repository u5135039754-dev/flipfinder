-- flipFinder private storage (Cloudflare D1). Apply with:
--   npx wrangler d1 execute flipfinder --remote --file schema.sql

-- settings, the home area, run status, the scanner's search list, pending price questions...
CREATE TABLE IF NOT EXISTS kv (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);

-- one row per deal alert; `data` is the full record (JSON), status/queued/sent copied out for queries
CREATE TABLE IF NOT EXISTS deals (
  key TEXT PRIMARY KEY,
  n INTEGER,
  status TEXT NOT NULL DEFAULT 'new',
  sent REAL NOT NULL DEFAULT 0,
  queued INTEGER NOT NULL DEFAULT 0,
  data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS deals_status ON deals (status);
CREATE INDEX IF NOT EXISTS deals_n ON deals (n);
-- D1's free tier counts rows read: recent deals ("sent >= ?"), the overnight queue and the 30-day prune by index,
-- not by reading the whole table
CREATE INDEX IF NOT EXISTS deals_sent ON deals (sent);
CREATE INDEX IF NOT EXISTS deals_status_sent ON deals (status, sent);
CREATE INDEX IF NOT EXISTS deals_queued ON deals (queued);

-- 👎 votes, kept to tune the filters
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at REAL NOT NULL,
  data TEXT NOT NULL
);

-- the shared pot: one row per money action, never changed (a correction is a new row)
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at REAL NOT NULL,
  kind TEXT NOT NULL,
  amount REAL NOT NULL,
  data TEXT NOT NULL
);
