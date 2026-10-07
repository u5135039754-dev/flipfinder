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

-- 👎 votes, kept to tune the filters
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at REAL NOT NULL,
  data TEXT NOT NULL
);
