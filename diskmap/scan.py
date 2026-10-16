# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only filesystem walker.

Never opens a file; stats only. Resumable per root: a root that finished is
recorded in scan_state, so re-running skips it unless --rescan is passed.
"""
import os, sys, time
from . import db
from .classify import BLOB_DIRS, SKIP_DIRS, classify

BATCH = 20000


def _blob_totals(path):
    """Aggregate a blob dir without emitting a row per file."""
    tot = n = 0
    for root, dirs, files in os.walk(path, onerror=lambda e: None):
        for f in files:
            try:
                tot += os.lstat(os.path.join(root, f)).st_size
                n += 1
            except OSError:
                pass
    return tot, n


def volume_stats(mount):
    try:
        st = os.statvfs(mount)
        return st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
    except OSError:
        return None, None


def scan_volume(con, mount, label, roots=None, rescan=False, progress=True,
                exclude=()):
    cap, free = volume_stats(mount)
    vol = db.volume_id(con, label, mount=mount, capacity=cap, free=free,
                       scanned_at=int(time.time()))
    if rescan:
        con.execute("DELETE FROM files WHERE vol=?", (vol,))
        con.execute("DELETE FROM dirs WHERE vol=?", (vol,))
        con.execute("DELETE FROM blobs WHERE vol=?", (vol,))
        con.execute("DELETE FROM scan_state WHERE vol=?", (vol,))
        con.commit()

    done = {r["root"] for r in con.execute(
        "SELECT root FROM scan_state WHERE vol=? AND done_at IS NOT NULL", (vol,))}
    targets = list(roots) if roots else [""]
    excl = set(exclude)

    dir_cache = {r["relpath"]: r["id"] for r in con.execute(
        "SELECT relpath, id FROM dirs WHERE vol=?", (vol,))}

    def dir_id(rel, depth):
        got = dir_cache.get(rel)
        if got is not None:
            return got
        cur = con.execute("INSERT INTO dirs (vol, relpath, depth) VALUES (?,?,?)",
                          (vol, rel, depth))
        dir_cache[rel] = cur.lastrowid
        return cur.lastrowid

    total_files = 0
    t0 = time.time()
    for root_rel in targets:
        if root_rel in done:
            if progress:
                print(f"  [{label}] pulando '{root_rel or '/'}' (já indexado)", file=sys.stderr)
            continue
        start = os.path.join(mount, root_rel) if root_rel else mount
        if not os.path.isdir(start):
            print(f"  [{label}] raiz ausente: {root_rel}", file=sys.stderr)
            continue
        pending = []
        for cur_root, dirs, files in os.walk(start, onerror=lambda e: None):
            keep = []
            for d in dirs:
                if d in SKIP_DIRS or d in excl:
                    continue
                if d in BLOB_DIRS:
                    full = os.path.join(cur_root, d)
                    b, c = _blob_totals(full)
                    con.execute(
                        "INSERT INTO blobs (vol, relpath, kind, bytes, files) VALUES (?,?,?,?,?)",
                        (vol, os.path.relpath(full, mount), d, b, c))
                    continue
                keep.append(d)
            dirs[:] = keep

            rel = os.path.relpath(cur_root, mount)
            rel = "" if rel == "." else rel
            did = dir_id(rel, rel.count("/") + 1 if rel else 0)
            for f in files:
                try:
                    st = os.lstat(os.path.join(cur_root, f))
                except OSError:
                    continue
                ext = os.path.splitext(f)[1].lower().lstrip(".")[:12]
                pending.append((vol, did, f, st.st_size, int(st.st_mtime), ext,
                                classify(ext)))
                total_files += 1
            if len(pending) >= BATCH:
                con.executemany(
                    "INSERT INTO files (vol,dir,name,size,mtime,ext,cls) VALUES (?,?,?,?,?,?,?)",
                    pending)
                con.commit()
                pending.clear()
                if progress:
                    print(f"  [{label}] {total_files} arquivos  {time.time()-t0:.0f}s",
                          file=sys.stderr)
        if pending:
            con.executemany(
                "INSERT INTO files (vol,dir,name,size,mtime,ext,cls) VALUES (?,?,?,?,?,?,?)",
                pending)
        con.execute(
            "INSERT OR REPLACE INTO scan_state (vol, root, done_at) VALUES (?,?,?)",
            (vol, root_rel, int(time.time())))
        con.commit()

    db.refresh_volume_totals(con)
    if progress:
        print(f"  [{label}] concluído: {total_files} arquivos em {time.time()-t0:.0f}s",
              file=sys.stderr)
    return total_files
