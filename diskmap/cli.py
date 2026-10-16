# SPDX-License-Identifier: GPL-3.0-or-later
"""diskmap - indexa, analisa e serve o mapa dos seus discos. Somente leitura."""
import argparse, os, sys
from . import db


def cmd_scan(a):
    from .scan import scan_volume
    con = db.connect(a.db)
    label = a.label or os.path.basename(a.mount.rstrip("/")).replace(" ", "_")
    scan_volume(con, a.mount, label, roots=a.root or None, rescan=a.rescan,
                exclude=a.exclude or ())
    db.build_indexes(con)
    con.close()


def cmd_import(a):
    from .importtsv import import_tsv
    con = db.connect(a.db)
    import_tsv(con, a.tsv)
    db.build_indexes(con)
    con.close()


def cmd_analyze(a):
    from .analyze import run_all
    con = db.connect(a.db)
    db.build_indexes(con)
    by, pervol, n = run_all(con, depth=a.depth)
    print()
    print(f"{'RECUPERÁVEL (união, nada contado 2x)':<42}")
    for k, v in sorted(by.items(), key=lambda x: -x[1]):
        print(f"  {db.human(v):>10}  {k}")
    print(f"  {'-'*10}")
    print(f"  {db.human(sum(by.values())):>10}  TOTAL em {_n(n)} arquivos")
    print()
    print("POR VOLUME")
    for k, v in sorted(pervol.items(), key=lambda x: -x[1]):
        print(f"  {db.human(v):>10}  {k}")
    con.close()


def cmd_verify(a):
    from .verify import verify_groups
    con = db.connect(a.db)
    verify_groups(con, stage=a.stage, limit=a.limit, min_size=a.min_size)
    con.close()


def cmd_plan(a):
    from .plan import write_plan
    con = db.connect(a.db)
    n, ndirs, total, tsv, sh, undo, warns = write_plan(
        con, a.out, a.quarantine, only_verified=not a.unverified,
        categories=a.category or None, min_size=a.min_size)
    what = f"{_n(n - ndirs)} arquivos"
    if ndirs:
        what += f" e {_n(ndirs)} pastas de build/dep"
    print(f"{what}, {db.human(total)}")
    print(f"  plano:   {tsv}")
    print(f"  aplicar: bash {sh}")
    print(f"  desfazer: bash {undo}")
    for w in warns:
        print(f"\n  aviso: {w}")
    if not a.unverified:
        print("\nSó duplicatas confirmadas por hash entraram. Rode 'diskmap verify'")
        print("antes, ou passe --unverified para incluir as não verificadas.")
    con.close()


def cmd_images(a):
    from . import images
    con = db.connect(a.db)
    if a.reclassify:
        images.reclassify(con)
    elif a.only_cluster:
        images.cluster(con, threshold=a.threshold)
    else:
        images.enrich(con, min_size=a.min_size, workers=a.workers,
                      include_dds=a.include_dds, refresh=a.refresh, limit=a.limit)
        images.cluster(con, threshold=a.threshold)
    s = images.stats(con)
    print()
    print(f"{s['total']:,}".replace(",", ".") + f" imagens processadas, "
          f"{db.human(s['bytes'])}, {s['with_hash']:,}".replace(",", ".")
          + " com hash visual")
    if s["no_thumb"]:
        print(f"  {_n(s['no_thumb'])} sem miniatura (formato que o sips não decodifica)")
    print("\nPOR BIBLIOTECA")
    for L in s["libs"]:
        print(f"  {db.human(L['bytes']):>10}  {_n(L['n']):>8}  {L['label']}")
    g = s["groups"]
    if g["n"]:
        print(f"\n{_n(g['n'])} grupos visualmente parecidos · "
              f"{db.human(g['wasted'])} se mantiver só a maior de cada")
    con.close()


def cmd_wipe(a):
    from . import wipe as wipe_mod
    con = db.connect(a.db, create=False)
    from .actions import ensure_log, resolve_target
    ensure_log(con)
    import shlex, time
    try:
        abspath, kind, label, mount = resolve_target(con, a.path)
    except ValueError as e:
        print(f"recusado: {e}", file=sys.stderr)
        return 1
    if kind != "file":
        print("zerar só vale para arquivo, não para pasta", file=sys.stderr)
        return 1

    av = wipe_mod.assess(mount)
    size = os.path.getsize(abspath)
    print(f"{abspath}\n  {db.human(size)} em {label} ({av['fs']})")
    print(f"\n  garantia aqui: {av['level'].upper()}\n  {av['why']}")
    if av["snapshots"]:
        print(f"  {av['snapshots']} snapshots neste volume")
    if not av["allowed"]:
        print("\nnão dá para zerar aqui.", file=sys.stderr)
        return 1
    for b in wipe_mod.check(abspath):
        print(f"\nrecusado: {b}", file=sys.stderr)
        return 1

    if not a.yes:
        print(f"\nIsto sobrescreve cada byte e "
              f"{'mantém o arquivo zerado' if a.keep else 'apaga o arquivo'}. "
              f"NÃO TEM UNDO.")
        try:
            if input("digite ZERAR para confirmar: ").strip() != "ZERAR":
                print("cancelado")
                return 1
        except (EOFError, KeyboardInterrupt):
            print("\ncancelado")
            return 1

    try:
        r = wipe_mod.zero_file(abspath, keep=a.keep)
    except (ValueError, OSError) as e:
        print(f"falhou: {e}", file=sys.stderr)
        return 1
    con.execute("INSERT INTO action_log (at,action,path,moved_to,argv,rc,output) "
                "VALUES (?,?,?,?,?,?,?)",
                (int(time.time()), "wipe", abspath, None,
                 f"wipe {shlex.quote(abspath)}", 0,
                 f"{r['bytes']} bytes zerados, garantia {av['level']}"))
    con.commit()
    con.close()
    print(f"\n{_n(r['bytes'])} bytes zerados e conferidos"
          f"{', arquivo apagado' if r['removed'] else ', arquivo mantido zerado'}")
    return 0


def cmd_serve(a):
    from .server import serve
    fresh = not os.path.exists(a.db)
    if fresh:
        # Create the empty index rather than refusing: with --allow-actions the
        # first scan can then be started from the Tarefas tab, which is otherwise
        # impossible - you would need an index to get the page that builds one.
        db.connect(a.db).close()
        print(f"índice novo em {a.db}")
        if a.allow_actions:
            print("  vazio ainda. Abra a aba Tarefas e rode um 'scan'.")
        else:
            print("  vazio ainda. Rode 'diskmap scan <volume>', ou suba com "
                  "--allow-actions para indexar pelo painel.")
    serve(a.db, a.host, a.port, allow_actions=a.allow_actions,
          allow_wipe=a.allow_wipe)


def cmd_search(a):
    from .server import search
    con = db.connect(a.db, create=False)
    res = search(con, a.term, a.limit)
    if res["dirs"]:
        print("DIRETÓRIOS")
        for d in res["dirs"][:15]:
            print(f"  {db.human(d['bytes']):>10}  {d['label']}/{d['relpath']}")
    if res["files"]:
        print("\nARQUIVOS")
        for f in res["files"][:40]:
            rel = f"{f['relpath']}/{f['name']}" if f["relpath"] else f["name"]
            print(f"  {db.human(f['size']):>10}  {f['label']}/{rel}")
    if not res["dirs"] and not res["files"]:
        print("nada encontrado")
    con.close()


def _n(v):
    return f"{v or 0:,}".replace(",", ".")


def cmd_status(a):
    con = db.connect(a.db, create=False)
    rows = list(con.execute(
        "SELECT label, mount, capacity, free, files, bytes, scanned_at "
        "FROM volumes ORDER BY bytes DESC"))
    import time
    print(f"{'VOLUME':<14} {'INDEXADO':>10} {'ARQUIVOS':>11} {'CAPAC.':>9} "
          f"{'LIVRE':>9}  MONTADO")
    for r in rows:
        on = "sim" if r["mount"] and os.path.isdir(r["mount"]) else "não"
        print(f"{r['label']:<14} {db.human(r['bytes']):>10} {_n(r['files']):>11} "
              f"{db.human(r['capacity']):>9} {db.human(r['free']):>9}  {on}")
    tot = con.execute("SELECT COUNT(*) n, SUM(size) b FROM files").fetchone()
    print(f"\ntotal: {_n(tot['n'])} arquivos, {db.human(tot['b'])}")
    d = con.execute("SELECT COUNT(*) g, SUM(wasted) w FROM dup_groups").fetchone()
    if d["g"]:
        v = {r["verified"]: r["n"] for r in con.execute(
            "SELECT verified, COUNT(*) n FROM dup_groups GROUP BY verified")}
        print(f"duplicatas: {_n(d['g'])} grupos, {db.human(d['w'])} redundantes "
              f"({v.get('no',0)} sem verificar, {v.get('quick',0)+v.get('full',0)} "
              f"confirmadas, {v.get('mismatch',0)} divergentes)")
    else:
        print("duplicatas: ainda não analisado - rode 'diskmap analyze'")
    con.close()


def main(argv=None):
    p = argparse.ArgumentParser(prog="diskmap", description=__doc__)
    p.add_argument("--db", default=db.DEFAULT_DB, help=f"padrão: {db.DEFAULT_DB}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="indexa um volume (somente leitura, retomável)")
    s.add_argument("mount")
    s.add_argument("--label")
    s.add_argument("--root", action="append", help="limita a estas subpastas")
    s.add_argument("--exclude", action="append", help="nome de pasta a ignorar")
    s.add_argument("--rescan", action="store_true", help="descarta o índice do volume")
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("import", help="importa um índice TSV já existente")
    s.add_argument("tsv")
    s.set_defaults(fn=cmd_import)

    s = sub.add_parser("analyze", help="recalcula rollups e achados")
    s.add_argument("--depth", type=int, default=2,
                   help="profundidade da árvore comparada (padrão 2)")
    s.set_defaults(fn=cmd_analyze)

    s = sub.add_parser("verify", help="confirma duplicatas por hash antes de mover")
    s.add_argument("--stage", choices=("quick", "full"), default="quick")
    s.add_argument("--limit", type=int, help="só os N grupos mais pesados")
    s.add_argument("--min-size", type=int, default=0, dest="min_size")
    s.set_defaults(fn=cmd_verify)

    s = sub.add_parser("plan", help="gera plano de quarentena (não apaga nada)")
    s.add_argument("--out", default="./diskmap-plan")
    s.add_argument("--quarantine", default=None,
                   help="pasta única (atravessa volumes e exige espaço livre); "
                        "o padrão é .diskmap-quarantine dentro de cada volume")
    s.add_argument("--category", action="append",
                   choices=("duplicata", "arquivo+extraido", "dump/temp",
                            "build/dep parado"))
    s.add_argument("--min-size", type=int, default=0, dest="min_size")
    s.add_argument("--unverified", action="store_true",
                   help="inclui duplicatas sem confirmação por hash")
    s.set_defaults(fn=cmd_plan)

    s = sub.add_parser("images", help="miniatura, dimensão, hash visual e biblioteca")
    s.add_argument("--min-size", type=int, default=100_000, dest="min_size",
                   help="ignora imagens menores que isso (padrão 100 KB)")
    s.add_argument("--workers", type=int, default=8)
    s.add_argument("--include-dds", action="store_true", dest="include_dds",
                   help="inclui dds e ico (só metadado: o sips não extrai pixel de dds)")
    s.add_argument("--refresh", action="store_true",
                   help="reprocessa o que já tem metadado")
    s.add_argument("--limit", type=int, help="só as N maiores (para testar)")
    s.add_argument("--threshold", type=int, default=10,
                   help="distância de hash que ainda conta como parecida (padrão 10 de 64)")
    s.add_argument("--only-cluster", action="store_true", dest="only_cluster",
                   help="só reagrupa, sem processar imagem nova")
    s.add_argument("--reclassify", action="store_true",
                   help="refaz só a biblioteca de cada imagem (segundos, sem sips)")
    s.set_defaults(fn=cmd_images)

    s = sub.add_parser("wipe", help="sobrescreve um arquivo com zeros e apaga "
                                    "(IRREVERSÍVEL)")
    s.add_argument("path")
    s.add_argument("--keep", action="store_true",
                   help="zera mas não apaga o arquivo")
    s.add_argument("--yes", action="store_true", help="não pergunta")
    s.set_defaults(fn=cmd_wipe)

    s = sub.add_parser("serve", help="sobe o front em localhost")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8777)
    s.add_argument("--allow-actions", action="store_true", dest="allow_actions",
                   help="libera ações no painel (Finder, quarentena, Lixeira e as "
                        "suas do actions.json). Desligado por padrão.")
    s.add_argument("--allow-wipe", action="store_true", dest="allow_wipe",
                   help="libera 'Zerar e apagar' no painel. Irreversível, "
                        "e exige --allow-actions junto.")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("search", help="onde está X (funciona com disco desconectado)")
    s.add_argument("term")
    s.add_argument("--limit", type=int, default=200)
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("status", help="o que já foi indexado")
    s.set_defaults(fn=cmd_status)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    main()
