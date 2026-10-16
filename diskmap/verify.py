# SPDX-License-Identifier: GPL-3.0-or-later
"""Confirm duplicate candidates by content before anything is moved.

Two stages, because hashing 300 GB to answer a question you can answer with
64 KB is a waste: `quick` reads the first and last 32 KB plus the size, `full`
reads everything. A group that disagrees at either stage is marked mismatch
and dropped from the recoverable set.
"""
import hashlib, os, sys, time
from . import db

HEAD = TAIL = 32768


def quick_digest(path, size):
    h = hashlib.sha256()
    h.update(str(size).encode())
    with open(path, "rb") as fh:
        h.update(fh.read(HEAD))
        if size > HEAD + TAIL:
            fh.seek(-TAIL, os.SEEK_END)
            h.update(fh.read(TAIL))
    return h.hexdigest()


def full_digest(path, chunk=4 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def abs_path(con, file_id, _cache={}):
    r = con.execute(
        "SELECT v.mount, d.relpath, f.name FROM files f "
        "JOIN dirs d ON d.id=f.dir JOIN volumes v ON v.id=f.vol WHERE f.id=?",
        (file_id,)).fetchone()
    if not r:
        return None
    rel = f"{r['relpath']}/{r['name']}" if r["relpath"] else r["name"]
    return os.path.join(r["mount"], rel)


def verify_groups(con, stage="quick", limit=None, min_size=0, progress=True):
    """Hash every member of each unverified duplicate group."""
    q = ("SELECT id, size, copies, wasted FROM dup_groups "
         "WHERE verified IN ('no','quick') AND size >= ? ORDER BY wasted DESC")
    rows = [dict(r) for r in con.execute(q, (min_size,))]
    if stage == "full":
        rows = [r for r in rows if True]
    if limit:
        rows = rows[:limit]

    ok = mism = skipped = 0
    hashed_bytes = 0
    t0 = time.time()
    for i, g in enumerate(rows, 1):
        if stage == "quick" and g.get("verified") == "quick":
            continue
        members = [r["file"] for r in con.execute(
            "SELECT file FROM dup_members WHERE grp=?", (g["id"],))]
        digests = {}
        missing = False
        for fid in members:
            p = abs_path(con, fid)
            if not p or not os.path.exists(p):
                missing = True
                break
            try:
                d = quick_digest(p, g["size"]) if stage == "quick" else full_digest(p)
            except OSError as e:
                print(f"  erro lendo {p}: {e}", file=sys.stderr)
                missing = True
                break
            digests[fid] = d
            hashed_bytes += (HEAD + TAIL) if stage == "quick" else g["size"]
            con.execute(
                "INSERT INTO file_hash (file, quick, full, hashed_at) VALUES (?,?,?,?) "
                "ON CONFLICT(file) DO UPDATE SET %s=excluded.%s, hashed_at=excluded.hashed_at"
                % (stage, stage), (fid, d if stage == "quick" else None,
                                   d if stage == "full" else None, int(time.time())))
        if missing:
            skipped += 1
            continue
        same = len(set(digests.values())) == 1
        con.execute("UPDATE dup_groups SET verified=? WHERE id=?",
                    (stage if same else "mismatch", g["id"]))
        ok += same
        mism += (not same)
        if progress and i % 50 == 0:
            con.commit()
            print(f"  {i}/{len(rows)} grupos · {db.human(hashed_bytes)} lidos · "
                  f"{time.time()-t0:.0f}s", file=sys.stderr)
    con.commit()
    if progress:
        print(f"verificação {stage}: {ok} confirmados, {mism} divergentes, "
              f"{skipped} inacessíveis · {db.human(hashed_bytes)} lidos em "
              f"{time.time()-t0:.0f}s", file=sys.stderr)
    return ok, mism, skipped
