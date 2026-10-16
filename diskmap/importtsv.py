# SPDX-License-Identifier: GPL-3.0-or-later
"""Import an index produced by the first-pass scanner.

Format, tab separated with a header row:
    vol  kind(F|BLOB)  relpath  bytes  count  mtime  ext
Lets a scan that already cost hours be reused instead of repeated.
"""
import os, sys, time
from . import db, userconf
from .classify import classify


def import_tsv(con, path, mounts=None, progress=True):
    # A TSV carries labels, not mount points, so the label -> mount map comes
    # from your own rules file. Without it the volumes still import; they just
    # have no capacity, no free space and no "is it plugged in".
    mounts = {**userconf.mounts(), **(mounts or {})}
    vol_ids, dir_cache = {}, {}
    pending = []
    nf = nb = 0
    t0 = time.time()

    def vol_of(label):
        if label not in vol_ids:
            mount = mounts.get(label)
            cap = free = None
            if mount and os.path.isdir(mount):
                try:
                    st = os.statvfs(mount)
                    cap, free = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
                except OSError:
                    pass
            vol_ids[label] = db.volume_id(con, label, mount=mount, capacity=cap,
                                          free=free, scanned_at=int(time.time()))
            for r in con.execute("SELECT relpath, id FROM dirs WHERE vol=?",
                                 (vol_ids[label],)):
                dir_cache[(vol_ids[label], r["relpath"])] = r["id"]
        return vol_ids[label]

    def dir_of(vol, rel):
        key = (vol, rel)
        got = dir_cache.get(key)
        if got is not None:
            return got
        cur = con.execute("INSERT INTO dirs (vol, relpath, depth) VALUES (?,?,?)",
                          (vol, rel, rel.count("/") + 1 if rel else 0))
        dir_cache[key] = cur.lastrowid
        return cur.lastrowid

    with open(path, encoding="utf-8", errors="replace") as fh:
        first = fh.readline()
        if not first.startswith("vol\t"):
            fh.seek(0)
        for line in fh:
            p = line.rstrip("\n").split("\t")
            if len(p) != 7:
                continue
            label, kind, rel, size, count, mtime, ext = p
            vol = vol_of(label)
            try:
                size = int(size); count = int(count); mtime = int(mtime)
            except ValueError:
                continue
            if kind == "BLOB":
                con.execute("INSERT INTO blobs (vol,relpath,kind,bytes,files) "
                            "VALUES (?,?,?,?,?)", (vol, rel, ext, size, count))
                nb += 1
                continue
            head, _, name = rel.rpartition("/")
            pending.append((vol, dir_of(vol, head), name, size, mtime, ext,
                            classify(ext)))
            nf += 1
            if len(pending) >= 20000:
                con.executemany("INSERT INTO files (vol,dir,name,size,mtime,ext,cls) "
                                "VALUES (?,?,?,?,?,?,?)", pending)
                con.commit(); pending.clear()
                if progress and nf % 500000 == 0:
                    print(f"  {nf} arquivos  {time.time()-t0:.0f}s", file=sys.stderr)
    if pending:
        con.executemany("INSERT INTO files (vol,dir,name,size,mtime,ext,cls) "
                        "VALUES (?,?,?,?,?,?,?)", pending)
    for label, vol in vol_ids.items():
        con.execute("INSERT OR REPLACE INTO scan_state (vol, root, done_at) "
                    "VALUES (?,?,?)", (vol, "", int(time.time())))
    con.commit()
    db.refresh_volume_totals(con)
    if progress:
        print(f"importados {nf} arquivos e {nb} blobs de {len(vol_ids)} volumes "
              f"em {time.time()-t0:.0f}s", file=sys.stderr)
    return nf, nb
