# SPDX-License-Identifier: GPL-3.0-or-later
"""SQLite storage for the disk index.

Paths are interned: one row per directory, files point at their directory.
That keeps a 5.4M-file index around a gigabyte instead of several.
"""
import os, sqlite3, time

DEFAULT_DB = os.path.expanduser("~/.diskmap/index.db")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS volumes (
  id         INTEGER PRIMARY KEY,
  label      TEXT UNIQUE NOT NULL,   -- stable name used in reports
  mount      TEXT,                   -- where it was mounted at scan time
  fstype     TEXT,
  capacity   INTEGER,
  free       INTEGER,
  scanned_at INTEGER,
  files      INTEGER DEFAULT 0,
  bytes      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS dirs (
  id      INTEGER PRIMARY KEY,
  vol     INTEGER NOT NULL REFERENCES volumes(id),
  relpath TEXT NOT NULL,             -- '' for the volume root
  depth   INTEGER NOT NULL,
  UNIQUE (vol, relpath)
);

CREATE TABLE IF NOT EXISTS files (
  id    INTEGER PRIMARY KEY,
  vol   INTEGER NOT NULL REFERENCES volumes(id),
  dir   INTEGER NOT NULL REFERENCES dirs(id),
  name  TEXT NOT NULL,
  size  INTEGER NOT NULL,
  mtime INTEGER,
  ext   TEXT,
  cls   TEXT                         -- content class, filled by analyze
);

-- node_modules/.git and friends: recorded as one aggregate row, not walked
CREATE TABLE IF NOT EXISTS blobs (
  id      INTEGER PRIMARY KEY,
  vol     INTEGER NOT NULL REFERENCES volumes(id),
  relpath TEXT NOT NULL,
  kind    TEXT NOT NULL,
  bytes   INTEGER NOT NULL,
  files   INTEGER NOT NULL
);

-- which roots of a volume finished, so an interrupted scan resumes
CREATE TABLE IF NOT EXISTS scan_state (
  vol  INTEGER NOT NULL REFERENCES volumes(id),
  root TEXT NOT NULL,
  done_at INTEGER,
  PRIMARY KEY (vol, root)
);

CREATE TABLE IF NOT EXISTS file_hash (
  file   INTEGER PRIMARY KEY REFERENCES files(id),
  quick  TEXT,      -- size + head/tail digest, cheap
  full   TEXT,      -- sha256 of the whole file
  hashed_at INTEGER
);

-- ---- findings, rebuilt by `analyze` ----
CREATE TABLE IF NOT EXISTS dup_groups (
  id       INTEGER PRIMARY KEY,
  name     TEXT,
  size     INTEGER,
  copies   INTEGER,
  wasted   INTEGER,
  verified TEXT DEFAULT 'no'   -- no | quick | full | mismatch
);
CREATE TABLE IF NOT EXISTS dup_members (
  grp  INTEGER NOT NULL REFERENCES dup_groups(id),
  file INTEGER NOT NULL REFERENCES files(id)
);

CREATE TABLE IF NOT EXISTS tree_overlap (
  id      INTEGER PRIMARY KEY,
  a       TEXT, b TEXT,
  shared  INTEGER,
  files   INTEGER,
  a_bytes INTEGER, b_bytes INTEGER,
  pct     REAL
);

CREATE TABLE IF NOT EXISTS archive_pairs (
  id      INTEGER PRIMARY KEY,
  file    INTEGER REFERENCES files(id),
  relpath TEXT,
  vol     INTEGER,
  bytes   INTEGER,
  extracted_dir TEXT
);

CREATE TABLE IF NOT EXISTS dir_rollup (
  vol   INTEGER NOT NULL,
  relpath TEXT NOT NULL,
  depth INTEGER,
  bytes INTEGER,
  files INTEGER,
  PRIMARY KEY (vol, relpath)
);

CREATE TABLE IF NOT EXISTS stat_rollup (
  kind  TEXT NOT NULL,      -- 'class' | 'age' | 'ext' | 'junk'
  key   TEXT NOT NULL,
  bytes INTEGER,
  files INTEGER,
  PRIMARY KEY (kind, key)
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS files_vol   ON files(vol);
CREATE INDEX IF NOT EXISTS files_dir   ON files(dir);
CREATE INDEX IF NOT EXISTS files_name  ON files(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS files_ns    ON files(name COLLATE NOCASE, size);
CREATE INDEX IF NOT EXISTS files_size  ON files(size DESC);
CREATE INDEX IF NOT EXISTS files_ext   ON files(ext);
CREATE INDEX IF NOT EXISTS dirs_rel    ON dirs(vol, relpath);
CREATE INDEX IF NOT EXISTS dup_mem_grp ON dup_members(grp);
CREATE INDEX IF NOT EXISTS dup_mem_f   ON dup_members(file);
CREATE INDEX IF NOT EXISTS roll_bytes  ON dir_rollup(bytes DESC);
"""


def connect(path=DEFAULT_DB, create=True):
    d = os.path.dirname(os.path.abspath(path))
    if create and d:
        os.makedirs(d, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def build_indexes(con):
    con.executescript(INDEXES)
    con.commit()


def volume_id(con, label, **kw):
    """Get or create a volume row, updating the metadata we were given."""
    cur = con.execute("SELECT id FROM volumes WHERE label=?", (label,))
    row = cur.fetchone()
    if row:
        if kw:
            sets = ", ".join(f"{k}=?" for k in kw)
            con.execute(f"UPDATE volumes SET {sets} WHERE id=?",
                        (*kw.values(), row["id"]))
        return row["id"]
    cols = ["label", *kw.keys()]
    con.execute(f"INSERT INTO volumes ({','.join(cols)}) VALUES ({','.join('?'*len(cols))})",
                (label, *kw.values()))
    return con.execute("SELECT id FROM volumes WHERE label=?", (label,)).fetchone()["id"]


def refresh_volume_totals(con):
    con.execute("""UPDATE volumes SET
      files = (SELECT COUNT(*) FROM files f WHERE f.vol = volumes.id),
      bytes = (SELECT COALESCE(SUM(size),0) FROM files f WHERE f.vol = volumes.id)""")
    con.commit()


def human(b):
    b = float(b or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(b) < 1024:
            return f"{b:.0f} {u}" if u in ("B", "KB") else f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} PB"
