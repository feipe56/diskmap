# SPDX-License-Identifier: GPL-3.0-or-later
"""Turn findings into a reviewable plan. Nothing here deletes anything.

Files are moved into a quarantine folder **at the root of their own volume**, so
the move is a rename inside one filesystem: instant, and it needs no free space.
A single shared quarantine would mean copying across volumes - slow, and it asks
for space you do not have. Pass --quarantine to override anyway.

An undo.sh is written beside apply.sh. Delete the quarantine yourself once you
have lived without it for a while; diskmap never does.
"""
import os, time
from . import db
from .analyze import recoverable

QUAR_NAME = ".diskmap-quarantine"

HEADER = """#!/bin/bash
# Gerado por diskmap em {when}
# MOVE arquivos redundantes para quarentena - NAO apaga nada.
# Revise antes de rodar. Para desfazer:  bash {undo}
set -u
moved=0; failed=0
mv_one() {{  # origem destino
  if [ ! -e "$1" ]; then echo "sumiu, pulando: $1"; return; fi
  mkdir -p "$(dirname "$2")" || {{ echo "FALHA mkdir: $2"; failed=$((failed+1)); return; }}
  if mv -n "$1" "$2"; then moved=$((moved+1)); else echo "FALHA mv: $1"; failed=$((failed+1)); fi
}}
"""

FOOTER = """
echo "---"
echo "movidos: $moved   falhas: $failed"
echo "quarentena por volume, em {quars}"
echo "conferir o que sobrou:  du -sh {quars}"
"""


def _q(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


def write_plan(con, out_dir, quarantine=None, only_verified=True, categories=None,
               min_size=0):
    """Emit plan.tsv, apply.sh and undo.sh.

    quarantine=None puts each file under <its volume>/.diskmap-quarantine.
    Returns (entries, dirs, bytes, tsv, sh, undo, warnings), where `dirs` counts
    the entries that are whole aggregated directories rather than single files.
    """
    os.makedirs(out_dir, exist_ok=True)
    by, pervol, n, drop, bdrop = recoverable(con)

    verified = set()
    for r in con.execute(
            "SELECT m.file FROM dup_groups g JOIN dup_members m ON m.grp=g.id "
            "WHERE g.verified IN ('quick','full')"):
        verified.add(r["file"])

    mounts = {r["label"]: r["mount"] for r in con.execute(
        "SELECT label, mount FROM volumes")}

    cats = set(categories) if categories else None
    rows, ndirs = [], 0
    for fid, (cat, size) in sorted(drop.items(), key=lambda kv: -kv[1][1]):
        if cats and cat not in cats:
            continue
        if size < min_size:
            continue
        if only_verified and cat == "duplicata" and fid not in verified:
            continue
        r = con.execute(
            "SELECT v.label, v.mount, d.relpath, f.name FROM files f "
            "JOIN dirs d ON d.id=f.dir JOIN volumes v ON v.id=f.vol WHERE f.id=?",
            (fid,)).fetchone()
        if not r or not r["mount"]:
            continue
        rel = f"{r['relpath']}/{r['name']}" if r["relpath"] else r["name"]
        src = os.path.join(r["mount"], rel)
        if quarantine:
            dst = os.path.join(quarantine, r["label"], rel)
        else:
            dst = os.path.join(r["mount"], QUAR_NAME, rel)
        rows.append((cat, size, r["label"], src, dst))

    # Aggregated dirs move as one directory, which is a single rename for the
    # whole subtree - a node_modules of 60k files costs one syscall, not 60k.
    if not cats or "build/dep parado" in cats:
        for _, kind, size, label, mount, rel, verdict, last in bdrop:
            if size < min_size or not mount:
                continue
            src = os.path.join(mount, rel)
            dst = (os.path.join(quarantine, label, rel) if quarantine
                   else os.path.join(mount, QUAR_NAME, rel))
            rows.append(("build/dep parado", size, label, src, dst))
            ndirs += 1

    # biggest first, so a plan read top-down is read in order of what it frees.
    # Safe to reorder: an aggregated dir contributes no rows to `files`, so no
    # entry here is ever inside another.
    rows.sort(key=lambda r: -r[1])

    when = time.strftime("%Y-%m-%d %H:%M")
    tsv = os.path.join(out_dir, "plan.tsv")
    sh = os.path.join(out_dir, "apply.sh")
    undo = os.path.join(out_dir, "undo.sh")

    quars = sorted({(quarantine or os.path.join(mounts.get(lb) or "", QUAR_NAME))
                    for _, _, lb, _, _ in rows})

    with open(tsv, "w") as f:
        f.write("categoria\tbytes\tvolume\torigem\tdestino\n")
        for cat, size, lb, src, dst in rows:
            f.write(f"{cat}\t{size}\t{lb}\t{src}\t{dst}\n")

    with open(sh, "w") as f:
        f.write(HEADER.format(when=when, undo=_q(undo)))
        for cat, size, lb, src, dst in rows:
            f.write(f"mv_one {_q(src)} {_q(dst)}\n")
        f.write(FOOTER.format(quars=" ".join(_q(q) for q in quars)))
    os.chmod(sh, 0o755)

    with open(undo, "w") as f:
        f.write(f"#!/bin/bash\n# Desfaz o apply.sh gerado em {when}\nset -u\n")
        for cat, size, lb, src, dst in rows:
            f.write(f"mkdir -p {_q(os.path.dirname(src))} && "
                    f"mv -n {_q(dst)} {_q(src)}\n")
    os.chmod(undo, 0o755)

    # a cross-volume quarantine has to copy, so it needs real free space
    warnings = []
    if quarantine:
        need = {}
        for cat, size, lb, src, dst in rows:
            need[lb] = need.get(lb, 0) + size
        try:
            st = os.statvfs(os.path.dirname(os.path.abspath(quarantine)) or "/")
            free = st.f_bavail * st.f_frsize
            total = sum(need.values())
            if total > free:
                warnings.append(
                    f"quarentena única em {quarantine} precisa de {db.human(total)} "
                    f"mas há {db.human(free)} livres ali. Sem --quarantine, cada "
                    f"arquivo vai para o próprio volume e o mv é instantâneo.")
        except OSError:
            pass

    return len(rows), ndirs, sum(r[1] for r in rows), tsv, sh, undo, warnings
