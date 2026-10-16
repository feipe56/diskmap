# SPDX-License-Identifier: GPL-3.0-or-later
"""O veredito de pasta agregada decide se algo pode ser oferecido para sair do
disco, então ele é testado em banco de verdade, não só por inspeção.

O que está fixado aqui são as regras que, se quebrarem, causam perda de dado:
`.git` nunca é oferecido, `site-packages` nunca é oferecido, e um `node_modules`
só é oferecido quando o manifesto que o recria está na raiz do projeto.

Roda:  python3 tests/test_blobs.py
"""
import os, sqlite3, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from diskmap import db                                              # noqa: E402
from diskmap.analyze import blob_drop, blob_reclaim                 # noqa: E402
from diskmap.classify import (BLOB_DIRS, CACHE_DIRS, MANIFEST_DIRS,  # noqa: E402
                              VCS_DIRS)

YEAR = 365 * 86400
OLD = time.time() - 4 * YEAR      # projeto parado
NEW = time.time() - 3 * 86400     # projeto vivo


def partition_checks():
    """Os três conjuntos precisam ser disjuntos e cobrir BLOB_DIRS."""
    bad = []
    man = set(MANIFEST_DIRS)
    for a, b, na, nb in ((VCS_DIRS, CACHE_DIRS, "VCS", "CACHE"),
                         (VCS_DIRS, man, "VCS", "MANIFEST"),
                         (CACHE_DIRS, man, "CACHE", "MANIFEST")):
        if a & b:
            bad.append(f"{na} e {nb} se sobrepõem em {sorted(a & b)}")
    sem = BLOB_DIRS - VCS_DIRS - CACHE_DIRS - man
    if sem != {"site-packages"}:
        bad.append(f"sem veredito deveria ser só site-packages, é {sorted(sem)}")
    faltando = (VCS_DIRS | CACHE_DIRS | man) - BLOB_DIRS
    if faltando:
        bad.append(f"classificado mas o scan não agrega: {sorted(faltando)}")
    for d in (".git", ".svn", ".hg"):
        if d in CACHE_DIRS or d in man:
            bad.append(f"{d} ficou num conjunto oferecível")
    return bad


def build():
    """Volume sintético: um projeto parado, um vivo, e as três armadilhas."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(db.SCHEMA)
    con.execute("INSERT INTO volumes (id,label,mount) VALUES (1,'T','/t')")

    def d(i, rel):
        con.execute("INSERT INTO dirs (id,vol,relpath,depth) VALUES (?,1,?,?)",
                    (i, rel, rel.count("/") + 1 if rel else 0))

    def f(did, name, mtime, size=1000):
        con.execute("INSERT INTO files (vol,dir,name,size,mtime,ext,cls) "
                    "VALUES (1,?,?,?,?,'js','code')", (did, name, size, mtime))

    def blob(i, rel, kind):
        con.execute("INSERT INTO blobs (id,vol,relpath,kind,bytes,files) "
                    "VALUES (?,1,?,?,5000000,100)", (i, rel, kind))

    d(1, "dev/morto");        f(1, "package.json", OLD); f(1, "index.js", OLD)
    d(2, "dev/vivo");         f(2, "package.json", NEW); f(2, "index.js", NEW)
    d(3, "dev/semmanifesto"); f(3, "leia.txt", OLD)
    d(4, "tool");             f(4, "run.exe", OLD)
    d(5, "dev/cacheado");     f(5, "build.gradle", OLD)

    blob(1, "dev/morto/node_modules", "node_modules")
    blob(2, "dev/morto/.git", ".git")
    blob(3, "dev/vivo/node_modules", "node_modules")
    blob(4, "dev/semmanifesto/node_modules", "node_modules")
    blob(5, "tool/python/Lib/site-packages", "site-packages")
    blob(6, "dev/cacheado/.gradle", ".gradle")
    con.commit()
    return con


CASES = [
    # (relpath, veredito esperado, pode ser oferecido parado há 1 ano?)
    ("dev/morto/node_modules",         "manifesto", True),
    ("dev/morto/.git",                 "vcs",       False),
    ("dev/vivo/node_modules",          "manifesto", False),  # vivo: cai no filtro
    ("dev/semmanifesto/node_modules",  "sem-prova", False),
    ("tool/python/Lib/site-packages",  "sem-prova", False),
    ("dev/cacheado/.gradle",           "cache",     True),
]


def main():
    bad = partition_checks()
    n = 1

    con = build()
    blob_reclaim(con, verbose=False)
    got = {r["relpath"]: r["verdict"]
           for r in con.execute("SELECT relpath, verdict FROM blob_reclaim")}
    offered = {r[5] for r in blob_drop(con, max_age_years=1)}

    for rel, want_v, want_off in CASES:
        n += 2
        if got.get(rel) != want_v:
            bad.append(f"{rel}: veredito {got.get(rel)!r}, esperado {want_v!r}")
        if (rel in offered) != want_off:
            bad.append(f"{rel}: oferecido={rel in offered}, esperado {want_off}")

    # o filtro de idade precisa mesmo filtrar, não só existir
    n += 1
    if "dev/vivo/node_modules" not in {r[5] for r in blob_drop(con, None)}:
        bad.append("sem corte de idade, o projeto vivo deveria aparecer")

    # nada marcado vcs pode escapar por nenhum corte de idade
    n += 1
    for yrs in (None, 0, 1, 5, 50):
        if any(r[1] in VCS_DIRS for r in blob_drop(con, yrs)):
            bad.append(f"blob_drop(max_age_years={yrs}) devolveu um repositório")
            break

    con.close()
    for b in bad:
        print("ERRO " + b)
    print(f"{n - len(bad)}/{n} corretos")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
