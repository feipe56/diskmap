# SPDX-License-Identifier: GPL-3.0-or-later
"""O que mudou no disco desde a última varredura, sem jogar fora o que vale.

`scan` sabe acrescentar, não sabe reconciliar: o laço dele só faz INSERT, não
há DELETE de arquivo que sumiu, e uma raiz já marcada em `scan_state` é pulada
inteira. A única saída era `--rescan`, que apaga o índice do volume -- e com
ele `image_meta`, `media_meta`, `file_hash` e `audio_meta` daquele volume,
porque `files.id` é INTEGER PRIMARY KEY e o rowid liberado volta a ser
distribuído. Num acervo já processado isso custa horas de miniatura para
corrigir algumas dezenas de linhas.

Aqui a comparação é por CAMINHO, não por id, e é isso que permite guardar o
trabalho: o arquivo que não mudou mantém o id, e com o id a miniatura e o hash.

Três conjuntos, e cada um com uma consequência diferente:

  sumiu      está no índice, não está no disco. A linha sai, e as laterais
             dela também -- apontam para um arquivo que não existe.
  apareceu   está no disco, não está no índice. Entra como linha nova, sem
             lateral nenhuma, que é o correto: ninguém o processou ainda.
  mudou      está nos dois, com tamanho ou mtime diferente. O id FICA (o
             caminho é o mesmo arquivo), mas as laterais SAEM: a miniatura e o
             hash descrevem o conteúdo velho, e um hash desatualizado é pior
             que hash nenhum, porque parece prova.
  guardado   está no índice, não está na caminhada, e o DIÁRIO DE AÇÕES diz
             por quê: a própria ferramenta o mandou para a quarentena ou para
             a Lixeira. Não é ausência, é um lugar que `classify.SKIP_DIRS`
             esconde do scanner de propósito (decisão 23). Nada sai do índice.

A quarta categoria existe porque sem ela o reconciliador mentia sobre o acervo
real: num volume de 591 mil arquivos, 70 dos 109 "sumiram" eram os 89,5 GB
da quarentena, e aplicar teria apagado o id -- com ele o hash e a miniatura --
de arquivos que estão no disco e que o Histórico ainda pode devolver. "Sumiu"
autoriza esquecer; "guardado" não.

Etiqueta nunca é tocada. Ela é por (rótulo do volume, caminho) e sobrevive a
tudo -- é o único dado aqui que nenhuma varredura recomputa.

Ensaio é o default. Nada é escrito sem `aplicar=True`.
"""
import os, sqlite3, sys, time

from . import db
from .scan import caminhar

# As laterais são chaveadas por `files.id`. A lista mora aqui e em `scan.py`
# porque as duas apagam pelos mesmos motivos; se divergirem, a que esquecer uma
# tabela deixa linha órfã apontando para id reaproveitado.
LATERAIS = ("file_hash", "image_meta", "media_meta", "audio_meta")


def _no_disco(con, mount, vol, raizes, exclude=()):
    """{(relpath, nome): (tamanho, mtime, ext, cls)} do que está lá agora."""
    out = {}
    for raiz in raizes:
        for rel, nome, tam, mt, ext, cls in caminhar(con, mount, vol, raiz,
                                                     exclude, blobs=False):
            out[(rel, nome)] = (tam, mt, ext, cls)
    return out


def _no_indice(con, vol, raizes):
    """{(relpath, nome): (id, tamanho, mtime)} do que o índice acredita."""
    out = {}
    q = ("SELECT f.id, f.name, f.size, f.mtime, d.relpath "
         "FROM files f JOIN dirs d ON d.id=f.dir WHERE f.vol=?")
    args = [vol]
    if raizes and raizes != [""]:
        # LIKE com ESCAPE porque '_' e '%' são curinga e nome de pasta os tem.
        cond, mais = [], []
        for r in raizes:
            cond.append("(d.relpath=? OR d.relpath LIKE ? ESCAPE '\\')")
            mais += [r, r.replace("\\", "\\\\").replace("_", "\\_")
                          .replace("%", "\\%") + "/%"]
        q += " AND (%s)" % " OR ".join(cond)
        args += mais
    for r in con.execute(q, args):
        out[(r["relpath"], r["name"])] = (r["id"], r["size"], r["mtime"])
    return out


def comparar(con, mount, vol, raizes=("",), exclude=()):
    """(sumiu, apareceu, mudou, guardado) sem escrever nada.

    `guardado` sai do diário de ações, não da caminhada: é o que esta
    ferramenta tirou do lugar e sabe onde pôs. Ele é subtraído de `sumiu`
    porque as consequências são opostas -- ver a docstring do módulo.
    """
    from . import actions as _act
    disco = _no_disco(con, mount, vol, raizes, exclude)
    indice = _no_indice(con, vol, list(raizes))
    fora = _act.fora_do_lugar(con)
    ausentes = [(k, v[0]) for k, v in indice.items() if k not in disco]
    sumiu, guardado = [], []
    for (rel, nome), fid in ausentes:
        abspath = os.path.join(mount, ("%s/%s" % (rel, nome)).lstrip("/"))
        onde = fora.get(abspath)
        (guardado if onde else sumiu).append(
            ((rel, nome), fid, onde) if onde else ((rel, nome), fid))
    apareceu = [(k, disco[k]) for k in disco if k not in indice]
    mudou = []
    for k, (fid, tam, mt) in indice.items():
        d = disco.get(k)
        if d and (d[0] != tam or d[1] != mt):
            mudou.append((k, fid, (tam, mt), (d[0], d[1]), d[2], d[3]))
    return sumiu, apareceu, mudou, guardado


def _apaga_laterais(con, ids):
    n = 0
    for lote in (ids[i:i + 500] for i in range(0, len(ids), 500)):
        marca = ",".join("?" * len(lote))
        for t in LATERAIS:
            try:
                cur = con.execute(
                    "DELETE FROM %s WHERE file IN (%s)" % (t, marca), lote)
                n += cur.rowcount or 0
            except sqlite3.OperationalError:
                pass          # tabela ainda não existe neste índice
    return n


def aplicar(con, vol, sumiu, apareceu, mudou):
    """Escreve a reconciliação. O chamador já decidiu; aqui só se executa."""
    ids_fora = [fid for _k, fid in sumiu]
    laterais = _apaga_laterais(con, ids_fora + [m[1] for m in mudou])
    for lote in (ids_fora[i:i + 500] for i in range(0, len(ids_fora), 500)):
        con.execute("DELETE FROM files WHERE id IN (%s)"
                    % ",".join("?" * len(lote)), lote)
    # mudou: o id fica, o metadado é atualizado.
    con.executemany("UPDATE files SET size=?, mtime=?, ext=?, cls=? WHERE id=?",
                    [(novo[0], novo[1], ext, cls, fid)
                     for _k, fid, _velho, novo, ext, cls in mudou])
    # apareceu: linha nova, e a pasta pode ser nova também.
    cache = {r["relpath"]: r["id"] for r in con.execute(
        "SELECT relpath, id FROM dirs WHERE vol=?", (vol,))}

    def did(rel):
        got = cache.get(rel)
        if got is None:
            cur = con.execute(
                "INSERT INTO dirs (vol, relpath, depth) VALUES (?,?,?)",
                (vol, rel, rel.count("/") + 1 if rel else 0))
            got = cache[rel] = cur.lastrowid
        return got

    con.executemany(
        "INSERT INTO files (vol,dir,name,size,mtime,ext,cls) VALUES (?,?,?,?,?,?,?)",
        [(vol, did(rel), nome, tam, mt, ext, cls)
         for (rel, nome), (tam, mt, ext, cls) in apareceu])
    db.refresh_volume_totals(con)
    con.commit()
    return len(ids_fora), len(apareceu), len(mudou), laterais


def reconciliar(con, mount, label, raizes=("",), exclude=(), aplicar_=False,
                progress=True, limite_mostra=12):
    r = con.execute("SELECT id FROM volumes WHERE label=?", (label,)).fetchone()
    if not r:
        raise ValueError("volume não está no índice: %s" % label)
    vol = r["id"]
    t0 = time.time()
    if progress:
        print("caminhando %s…" % mount, file=sys.stderr)
    sumiu, apareceu, mudou, guardado = comparar(con, mount, vol, raizes,
                                                exclude)
    if progress:
        print("  %s: %d sumiram · %d apareceram · %d mudaram · "
              "%d guardados · %.0fs"
              % (label, len(sumiu), len(apareceu), len(mudou), len(guardado),
                 time.time() - t0), file=sys.stderr)
        for titulo, itens in (("sumiram", [k for k, _ in sumiu]),
                              ("apareceram", [k for k, _ in apareceu]),
                              ("mudaram", [m[0] for m in mudou]),
                              ("guardados", [g[0] for g in guardado])):
            for rel, nome in itens[:limite_mostra]:
                print("    %-10s %s" % (titulo[:-1],
                                        ("%s/%s" % (rel, nome)).lstrip("/")),
                      file=sys.stderr)
            if len(itens) > limite_mostra:
                print("    %-10s … e mais %d" % ("", len(itens) - limite_mostra),
                      file=sys.stderr)
    if guardado and progress:
        # Dito em voz alta porque a ação certa aqui não é reconciliar: é a aba
        # Histórico, que devolve, ou a decisão de esvaziar a quarentena.
        por_onde = {}
        for _k, _f, onde in guardado:
            por_onde[onde] = por_onde.get(onde, 0) + 1
        print("  %s: %s — o índice MANTÉM estes, com hash e miniatura."
              % (label, " · ".join("%d na %s" % (n, o)
                                   for o, n in sorted(por_onde.items()))),
              file=sys.stderr)
    if not aplicar_:
        if progress:
            print("ensaio: nada foi escrito. Repita com --aplicar.",
                  file=sys.stderr)
        return sumiu, apareceu, mudou, guardado, None
    efeito = aplicar(con, vol, sumiu, apareceu, mudou)
    if progress:
        print("aplicado: %d linhas removidas, %d acrescentadas, %d atualizadas, "
              "%d laterais descartadas" % efeito, file=sys.stderr)
        print("rode `analyze` para os achados refletirem o disco.",
              file=sys.stderr)
    return sumiu, apareceu, mudou, guardado, efeito
