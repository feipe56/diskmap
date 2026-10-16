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
BLOB_STALE_YEARS = 1           # an aggregated dir is only waste once the
                               # project that owns it has gone quiet

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


def blob_reclaim(con, verbose=True):
    """Judge each aggregated dir by what it would cost to get it back.

    These folders are already in the index, as one row with a total - they were
    never offered up because the README called them "reconstructible, so they
    count as space used, not as a collection to organize". That is exactly
    backwards for cleaning: reconstructible is the *reason* to offer them.

    Three verdicts can be acted on, one cannot:

      cache      a tool refills it by itself, wherever it sits
      manifesto  rebuildable, and the manifest that rebuilds it is still there
      sem-prova  no evidence anything rebuilds it - reported, never offered
      vcs        the repository itself - never offered, at any age

    Staleness comes from the project's own files, not from the folder: a
    node_modules whose project was last touched in 2022 is dead weight, and the
    folder's own mtime says nothing about that. The aggregated dir contributes no
    rows to `files`, so this needs no rescan.
    """
    from .classify import VCS_DIRS, CACHE_DIRS, MANIFEST_DIRS
    t0 = time.time()
    con.execute("DELETE FROM blob_reclaim")
    rows = list(con.execute("SELECT id, vol, relpath, kind, bytes, files FROM blobs"))
    out = []
    for r in rows:
        kind, rel = r["kind"], r["relpath"]
        proj = rel.rsplit("/", 1)[0] if "/" in rel else ""
        proof = None
        if kind in VCS_DIRS:
            verdict = "vcs"
        elif kind in CACHE_DIRS:
            verdict, proof = "cache", "cache"
        elif kind in MANIFEST_DIRS:
            want = MANIFEST_DIRS[kind]
            pr = con.execute("SELECT id FROM dirs WHERE vol=? AND relpath=?",
                             (r["vol"], proj)).fetchone()
            if pr:
                qs = ",".join("?" * len(want))
                got = con.execute(
                    f"SELECT name FROM files WHERE dir=? AND "
                    f"name COLLATE NOCASE IN ({qs})", (pr["id"],) + want).fetchone()
                proof = got["name"] if got else None
            verdict = "manifesto" if proof else "sem-prova"
        else:
            verdict = "sem-prova"

        last = None
        if verdict != "vcs":
            # prefix range over the (vol, relpath) index: '/' is 0x2F and '0' is
            # 0x30, so [proj+'/', proj+'0') is exactly the subtree of proj
            if proj:
                q = ("SELECT MAX(f.mtime) m FROM files f WHERE f.dir IN "
                     "(SELECT id FROM dirs WHERE vol=? AND "
                     "(relpath=? OR (relpath>=? AND relpath<?)))")
                args = (r["vol"], proj, proj + "/", proj + "0")
            else:
                q = "SELECT MAX(f.mtime) m FROM files f WHERE f.vol=?"
                args = (r["vol"],)
            last = con.execute(q, args).fetchone()["m"]

        out.append((r["id"], r["vol"], rel, kind, proj, verdict, proof, last,
                    r["bytes"], r["files"]))

    con.executemany(
        "INSERT INTO blob_reclaim (blob,vol,relpath,kind,proj,verdict,proof,last,"
        "bytes,files) VALUES (?,?,?,?,?,?,?,?,?,?)", out)
    con.commit()
    if verbose:
        agg = collections.Counter()
        for o in out:
            agg[o[5]] += o[8]
        parts = ", ".join(f"{k} {db.human(v)}" for k, v in agg.most_common())
        print(f"  pastas agregadas: {len(out)} ({parts}) em {time.time()-t0:.0f}s",
              file=sys.stderr)
    return len(out)


def blob_drop(con, max_age_years=None):
    """The aggregated dirs that may be offered, newest-project-first excluded.

    Returns [(blob_id, kind, bytes, label, mount, relpath, verdict, last)].
    `vcs` and `sem-prova` never come back from here. A protected path never does
    either: a cache inside a launcher install is the launcher's business.
    """
    now = time.time()
    out = []
    for r in con.execute(
            "SELECT b.blob, b.kind, b.bytes, b.relpath, b.verdict, b.last, "
            "v.label, v.mount FROM blob_reclaim b JOIN volumes v ON v.id=b.vol "
            "WHERE b.verdict IN ('cache','manifesto') ORDER BY b.bytes DESC"):
        if keep_score(r["relpath"])[0] == 0:
            continue
        if max_age_years is not None:
            if not r["last"]:
                continue
            if (now - r["last"]) / YEAR < max_age_years:
                continue
        out.append((r["blob"], r["kind"], r["bytes"], r["label"], r["mount"],
                    r["relpath"], r["verdict"], r["last"]))
    return out


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

    # Aggregated dirs are a disjoint fifth category: the scanner records them as
    # one row and never walks inside, so not one byte here is already counted
    # above. Only the stale ones make the headline - a node_modules whose project
    # you touched last week is not waste, it is your next build.
    bdrop = blob_drop(con, max_age_years=BLOB_STALE_YEARS)
    for _, _, b, label, _, _, _, _ in bdrop:
        by["build/dep parado"] += b
        pervol[label] += b
    return dict(by), dict(pervol), len(drop), drop, bdrop


def run_all(con, depth=2, verbose=True):
    t0 = time.time()
    if verbose:
        print("analisando…", file=sys.stderr)
    rollups(con, verbose)
    duplicates(con, verbose)
    archive_pairs(con, verbose)
    tree_overlap(con, depth, verbose)
    blob_reclaim(con, verbose)
    by, pervol, n, _, bdrop = recoverable(con)
    if verbose:
        print(f"  recuperável (união): {db.human(sum(by.values()))} em {n} arquivos "
              f"+ {len(bdrop)} pastas de build/dep", file=sys.stderr)
        print(f"concluído em {time.time()-t0:.0f}s", file=sys.stderr)
    return by, pervol, n
