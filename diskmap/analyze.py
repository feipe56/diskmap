# SPDX-License-Identifier: GPL-3.0-or-later
"""Rollups and findings, rebuilt from the file index.

Everything here is derived: dropping these tables and re-running `analyze`
costs minutes and loses nothing.
"""
import collections, os, sys, time
from . import db
from .classify import ARCHIVE_EXTS, JUNK_EXTS

YEAR = 365 * 86400
AGE_BUCKETS = [(0.5, "<6m"), (1, "6m-1a"), (2, "1-2a"), (3, "2-3a"), (5, "3-5a")]
MIN_DUP_SIZE = 1_000_000       # ignore small-file noise
MIN_FP_SIZE = 65_536           # tree fingerprint floor
MIN_JUNK_SIZE = 1_000_000
MIN_OVERLAP = 500_000_000      # only report tree pairs sharing this much

# Paths a launcher or package manager owns. Removing one file from inside a game
# install corrupts it, and the manager will just re-download anyway, so these are
# never offered up - even when they hold the redundant copy.
PROTECTED = (
    "steamlibrary/steamapps", "/steamapps/", "epic games/", "origin games/",
    "program files", "programdata/", "/windows/", "appdata/local/packages",
    "library/application support/steam", "ue_5.", "/vulkansdk",
)
# Scratch and derived locations: when a group has a copy here, this is the copy to
# give up first. Lower score wins the survivor slot.
PREFER_DROP = (
    "/build/", "-old/", "_old/", "/old/", "/dumps/", "/dump/",
    "backup", "/bck", "/temp/", "/tmp/", "/cache/", ".convert_cache",
    "/recycle", "$recycle", "/obj/", "/bin/debug", "/bin/release",
)


_LISTS = None


def _lists():
    """Both lists, with your additions from ~/.diskmap/rules.json.

    Which folder on *your* disk is scratch and which is sacred is a fact about
    your disk, not about the tool, so it is read from your file. Built once:
    keep_score runs per duplicate member.
    """
    global _LISTS
    if _LISTS is None:
        from . import userconf
        _LISTS = (PROTECTED + userconf.protected(),
                  PREFER_DROP + userconf.prefer_drop())
    return _LISTS


def keep_score(rel):
    """Lower is a better survivor. Protected paths sort first so they always stay."""
    prot, drop = _lists()
    low = rel.lower()
    return (0 if any(k in low for k in prot) else 1,
            1 if any(k in low for k in drop) else 0,
            low.count("/"), len(low))


def _age(mtime, now):
    if not mtime or mtime <= 0:
        return "desconhecido"
    a = (now - mtime) / YEAR
    for lim, lab in AGE_BUCKETS:
        if a < lim:
            return lab
    return ">5a"


def paths(con):
    """relpath lookup for every directory, keyed by dir id."""
    return {r["id"]: (r["vol"], r["relpath"])
            for r in con.execute("SELECT id, vol, relpath FROM dirs")}


def rollups(con, verbose=True):
    now = time.time()
    con.execute("DELETE FROM stat_rollup")
    con.execute("DELETE FROM dir_rollup")

    cls = collections.Counter(); clsn = collections.Counter()
    ext = collections.Counter(); extn = collections.Counter()
    age = collections.Counter(); agen = collections.Counter()
    junk = collections.Counter(); junkn = collections.Counter()
    dsz = collections.Counter(); dn = collections.Counter()

    dmap = paths(con)
    for r in con.execute("SELECT dir, size, mtime, ext, cls FROM files"):
        size = r["size"]
        cls[r["cls"]] += size; clsn[r["cls"]] += 1
        ext[r["ext"] or "(nenhuma)"] += size; extn[r["ext"] or "(nenhuma)"] += 1
        a = _age(r["mtime"], now)
        age[a] += size; agen[a] += 1
        if r["ext"] in JUNK_EXTS and size >= MIN_JUNK_SIZE:
            junk[r["ext"]] += size; junkn[r["ext"]] += 1
        vol, rel = dmap[r["dir"]]
        # charge every ancestor, so dir_rollup is a real recursive size
        parts = rel.split("/") if rel else []
        acc = ""
        dsz[(vol, "")] += size; dn[(vol, "")] += 1
        for seg in parts:
            acc = f"{acc}/{seg}" if acc else seg
            dsz[(vol, acc)] += size; dn[(vol, acc)] += 1

    rows = []
    for k, v in cls.items(): rows.append(("class", k, v, clsn[k]))
    for k, v in age.items(): rows.append(("age", k, v, agen[k]))
    for k, v in junk.items(): rows.append(("junk", k, v, junkn[k]))
    for k, v in ext.most_common(80): rows.append(("ext", k, v, extn[k]))
    con.executemany("INSERT OR REPLACE INTO stat_rollup (kind,key,bytes,files) VALUES (?,?,?,?)", rows)
    con.executemany(
        "INSERT OR REPLACE INTO dir_rollup (vol,relpath,depth,bytes,files) VALUES (?,?,?,?,?)",
        [(v, p, p.count("/") + 1 if p else 0, b, dn[(v, p)]) for (v, p), b in dsz.items()])
    con.commit()
    if verbose:
        print(f"  rollups: {len(cls)} classes, {len(dsz)} diretórios", file=sys.stderr)


def duplicates(con, verbose=True):
    """Group files sharing name + exact size. Candidates, not proof - see `verify`."""
    con.execute("DELETE FROM dup_members")
    con.execute("DELETE FROM dup_groups")
    groups = collections.defaultdict(list)
    for r in con.execute(
            "SELECT id, name, size FROM files WHERE size >= ? ORDER BY size DESC",
            (MIN_DUP_SIZE,)):
        groups[(r["name"].lower(), r["size"])].append(r["id"])
    n = waste = 0
    for (name, size), ids in groups.items():
        if len(ids) < 2:
            continue
        w = size * (len(ids) - 1)
        cur = con.execute(
            "INSERT INTO dup_groups (name,size,copies,wasted) VALUES (?,?,?,?)",
            (name, size, len(ids), w))
        gid = cur.lastrowid
        con.executemany("INSERT INTO dup_members (grp,file) VALUES (?,?)",
                        [(gid, i) for i in ids])
        n += 1; waste += w
    con.commit()
    restored = rederive_verification(con)
    if verbose:
        print(f"  duplicatas: {n} grupos, {db.human(waste)} redundantes "
              f"({restored} com hash já conhecido)", file=sys.stderr)
    return waste


def rederive_verification(con):
    """Recompute each group's verified state from the hashes already on disk.

    dup_groups is rebuilt on every analyze, so the state cannot live there and
    survive. file_hash is keyed by file id and does survive, so verification is
    derived from it: re-running analyze never throws away hashing work.
    """
    hashes = {r["file"]: (r["quick"], r["full"]) for r in
              con.execute("SELECT file, quick, full FROM file_hash")}
    if not hashes:
        return 0
    members = collections.defaultdict(list)
    for r in con.execute("SELECT grp, file FROM dup_members"):
        members[r["grp"]].append(r["file"])

    updates = []
    for gid, files in members.items():
        qs = [hashes.get(f, (None, None))[0] for f in files]
        fs = [hashes.get(f, (None, None))[1] for f in files]
        state = "no"
        if all(fs) :
            state = "full" if len(set(fs)) == 1 else "mismatch"
        elif all(qs):
            state = "quick" if len(set(qs)) == 1 else "mismatch"
        else:
            known = [x for x in fs if x] + [x for x in qs if x]
            # a disagreement among the members we did hash is already decisive
            if len([x for x in fs if x]) > 1 and len(set(x for x in fs if x)) > 1:
                state = "mismatch"
            elif len([x for x in qs if x]) > 1 and len(set(x for x in qs if x)) > 1:
                state = "mismatch"
        if state != "no":
            updates.append((state, gid))
    con.executemany("UPDATE dup_groups SET verified=? WHERE id=?", updates)
    con.commit()
    return len(updates)


def archive_pairs(con, verbose=True):
    """An archive sitting next to a directory of the same name."""
    con.execute("DELETE FROM archive_pairs")
    dmap = paths(con)
    have = {(v, p) for v, p in dmap.values()}
    found = waste = 0
    for r in con.execute(
            "SELECT f.id, f.vol, f.dir, f.name, f.size FROM files f "
            "WHERE f.ext IN (%s) AND f.size >= ?" % ",".join("?" * len(ARCHIVE_EXTS)),
            (*ARCHIVE_EXTS, MIN_DUP_SIZE)):
        vol, rel = dmap[r["dir"]]
        stem = os.path.splitext(r["name"])[0]
        cand = f"{rel}/{stem}" if rel else stem
        if (vol, cand) in have:
            full = f"{rel}/{r['name']}" if rel else r["name"]
            con.execute(
                "INSERT INTO archive_pairs (file,relpath,vol,bytes,extracted_dir) "
                "VALUES (?,?,?,?,?)", (r["id"], full, vol, r["size"], cand))
            found += 1; waste += r["size"]
    con.commit()
    if verbose:
        print(f"  arquivo+extraído: {found} pares, {db.human(waste)}", file=sys.stderr)
    return waste


def tree_overlap(con, depth=2, verbose=True):
    """Find directory trees that hold the same content under different names.

    Fingerprint = (filename, exact size). Two trees sharing many fingerprints
    are the same tree copied, which no duplicate finder reports as one thing.
    """
    con.execute("DELETE FROM tree_overlap")
    dmap = paths(con)
    vols = {r["id"]: r["label"] for r in con.execute("SELECT id, label FROM volumes")}

    def tree_of(vol, rel):
        parts = rel.split("/") if rel else []
        return vols[vol] + "/" + "/".join(parts[:depth]) if parts else vols[vol] + "/(raiz)"

    tb = collections.Counter()
    fp = collections.defaultdict(set)
    for r in con.execute("SELECT dir, name, size FROM files WHERE size >= ?", (MIN_FP_SIZE,)):
        vol, rel = dmap[r["dir"]]
        t = tree_of(vol, rel)
        tb[t] += r["size"]
        fp[(r["name"].lower(), r["size"])].add(t)

    pair = collections.Counter(); pairn = collections.Counter()
    for (name, size), trees in fp.items():
        if not 2 <= len(trees) <= 12:
            continue
        ts = sorted(trees)
        for i in range(len(ts)):
            for j in range(i + 1, len(ts)):
                pair[(ts[i], ts[j])] += size
                pairn[(ts[i], ts[j])] += 1

    rows = []
    for (a, b), shared in pair.items():
        if shared < MIN_OVERLAP:
            continue
        small = min(tb[a], tb[b]) or 1
        rows.append((a, b, shared, pairn[(a, b)], tb[a], tb[b],
                     round(100 * shared / small, 1)))
    rows.sort(key=lambda r: -r[2])
    con.executemany(
        "INSERT INTO tree_overlap (a,b,shared,files,a_bytes,b_bytes,pct) "
        "VALUES (?,?,?,?,?,?,?)", rows)
    con.commit()
    if verbose:
        print(f"  árvores clonadas: {len(rows)} pares acima de {db.human(MIN_OVERLAP)}",
              file=sys.stderr)
    return rows


def recoverable(con):
    """Union of the redundancy categories, counting each file exactly once.

    Returns (per-category bytes, per-volume bytes, file count). Picks one
    survivor per duplicate group, then adds archives and junk not already
    accounted for.
    """
    # One survivor per duplicate group. A copy inside a launcher-managed install
    # is never the one dropped; a copy in a build or backup path is dropped first.
    dmap = paths(con)
    groups = collections.defaultdict(list)
    for r in con.execute("SELECT g.id gid, g.size, m.file f, fi.dir FROM dup_groups g "
                         "JOIN dup_members m ON m.grp=g.id "
                         "JOIN files fi ON fi.id=m.file "
                         "WHERE g.verified != 'mismatch'"):
        rel = dmap[r["dir"]][1]
        groups[r["gid"]].append((keep_score(rel), r["f"], r["size"], rel))

    drop = {}
    for gid, members in groups.items():
        members.sort(key=lambda m: m[0])
        # every copy sits in a protected install: touch none of them
        if all(m[0][0] == 0 for m in members):
            continue
        for score, fid, size, rel in members[1:]:
            if score[0] == 0:
                continue          # never drop a protected copy
            drop[fid] = ("duplicata", size)

    for r in con.execute("SELECT file, relpath, bytes FROM archive_pairs"):
        if keep_score(r["relpath"])[0] == 0:
            continue
        drop.setdefault(r["file"], ("arquivo+extraido", r["bytes"]))
    for r in con.execute(
            "SELECT f.id, f.size, f.dir FROM files f WHERE f.ext IN (%s) AND f.size >= ?"
            % ",".join("?" * len(JUNK_EXTS)), (*JUNK_EXTS, MIN_JUNK_SIZE)):
        if keep_score(dmap[r["dir"]][1])[0] == 0:
            continue
        drop.setdefault(r["id"], ("dump/temp", r["size"]))

    by = collections.Counter()
    for _, (cat, b) in drop.items():
        by[cat] += b
    pervol = collections.Counter()
    if drop:
        ids = list(drop)
        for chunk in (ids[i:i + 5000] for i in range(0, len(ids), 5000)):
            q = ",".join("?" * len(chunk))
            for r in con.execute(
                    f"SELECT f.id, v.label FROM files f JOIN volumes v ON v.id=f.vol "
                    f"WHERE f.id IN ({q})", chunk):
                pervol[r["label"]] += drop[r["id"]][1]
    return dict(by), dict(pervol), len(drop), drop


def run_all(con, depth=2, verbose=True):
    t0 = time.time()
    if verbose:
        print("analisando…", file=sys.stderr)
    rollups(con, verbose)
    duplicates(con, verbose)
    archive_pairs(con, verbose)
    tree_overlap(con, depth, verbose)
    by, pervol, n, _ = recoverable(con)
    if verbose:
        print(f"  recuperável (união): {db.human(sum(by.values()))} em {n} arquivos", file=sys.stderr)
        print(f"concluído em {time.time()-t0:.0f}s", file=sys.stderr)
    return by, pervol, n
