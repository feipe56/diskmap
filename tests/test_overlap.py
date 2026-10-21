# SPDX-License-Identifier: GPL-3.0-or-later
"""A lista de arquivos por trás de um par de árvores clonadas.

O que está fixado aqui é a propriedade que torna o número clicável confiável:
`overlap_files` tem de achar EXATAMENTE os mesmos arquivos que `tree_overlap`
contou. Se as duas derivarem a impressão digital de formas diferentes, a tabela
promete 12 e a gaveta mostra 15 — e aí nenhum dos dois números vale nada: regra
duplicada é regra que diverge.

Roda:  python3 tests/test_overlap.py
"""
import os, sqlite3, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from diskmap import db                                              # noqa: E402
from diskmap.analyze import (MAX_FP_TREES, MIN_FP_SIZE, _tree_files,  # noqa: E402
                             _verdict, overlap_files, split_tree,
                             tree_overlap)

GRANDE = 260_000_000          # 3 destes passam do piso de MIN_OVERLAP
falhas = []


def ok(cond, msg):
    if not cond:
        falhas.append(msg)


def build():
    """Dois clones de verdade, um nome onipresente e uma armadilha de LIKE."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(db.SCHEMA)
    # Um rótulo COM barra, porque nada impede e a resolução tem de aguentar.
    con.execute("INSERT INTO volumes (id,label,mount) VALUES (1,'disco/um','/a')")
    con.execute("INSERT INTO volumes (id,label,mount) VALUES (2,'dois','/b')")
    nd = [0]

    def d(vol, rel):
        nd[0] += 1
        con.execute("INSERT INTO dirs (id,vol,relpath,depth) VALUES (?,?,?,?)",
                    (nd[0], vol, rel, rel.count("/") + 1 if rel else 0))
        return nd[0]

    def f(did, vol, name, size=GRANDE):
        con.execute("INSERT INTO files (vol,dir,name,size,mtime,ext,cls) "
                    "VALUES (?,?,?,?,1700000000,'mkv','video')",
                    (vol, did, name, size))
        return con.execute("SELECT last_insert_rowid() i").fetchone()["i"]

    # O par clonado: mesmos três nomes e tamanhos dos dois lados.
    a = d(1, "All/Filmes")
    b = d(2, "Filmes/Aventura")
    ids = {}
    for nome in ("um.mkv", "dois.mkv", "tres.mkv"):
        ids[nome] = (f(a, 1, nome), f(b, 2, nome))
    # Pequeno demais para ser impressão digital: não pode aparecer.
    f(a, 1, "miudo.mkv", MIN_FP_SIZE - 1)
    f(b, 2, "miudo.mkv", MIN_FP_SIZE - 1)
    # Nome onipresente: existe em muitas árvores, logo não prova clonagem.
    for i in range(MAX_FP_TREES + 2):
        t = d(1, "banal%d/sub" % i)
        f(t, 1, "leiame.mkv")
    # Armadilha do LIKE: 'a_b' não pode puxar arquivo de 'axb'. Os nomes são
    # DIFERENTES de propósito — com o mesmo nome os dois seriam clones de
    # verdade e o teste passaria sem provar nada sobre o escape.
    sub = d(1, "a_b/dentro")
    f(sub, 1, "so_do_sublinhado.mkv")
    outro = d(1, "axb/dentro")
    f(outro, 1, "so_do_xis.mkv")
    # Precisa ser MAIS FUNDO: o vazamento é no ramo `LIKE prefixo/%`, e
    # 'axb/dentro' sozinho não casa 'a_b/dentro/%' nem sem escape.
    fundo = d(1, "axb/dentro/fundo")
    f(fundo, 1, "do_fundo_do_xis.mkv")
    con.commit()
    return con, ids


def test_conta_bate_com_o_agregado(con):
    """A propriedade central: a gaveta mostra o que a coluna promete."""
    pares = list(con.execute("SELECT a,b,files FROM tree_overlap"))
    ok(pares, "o cenário não produziu par nenhum para conferir")
    for p in pares:
        r = overlap_files(con, p["a"], p["b"])
        ok(r["total"] == p["files"],
           "par %s x %s: a tabela diz %d, a lista achou %d"
           % (p["a"], p["b"], p["files"], r["total"]))


def test_o_par_clonado_traz_os_tres(con):
    r = overlap_files(con, "disco/um/All/Filmes", "dois/Filmes/Aventura")
    nomes = sorted(x["name"] for x in r["rows"])
    ok(nomes == ["dois.mkv", "tres.mkv", "um.mkv"],
       "esperava os três clonados, veio %s" % nomes)
    ok(r["bytes"] == 3 * GRANDE, "bytes somados errados: %d" % r["bytes"])
    ok(all(x["a_path"].startswith("All/Filmes/") for x in r["rows"]),
       "o caminho do lado A não é o da árvore A")
    ok(all(x["b_path"].startswith("Filmes/Aventura/") for x in r["rows"]),
       "o caminho do lado B não é o da árvore B")


def test_miudo_fica_de_fora(con):
    r = overlap_files(con, "disco/um/All/Filmes", "dois/Filmes/Aventura")
    ok(not [x for x in r["rows"] if x["name"] == "miudo.mkv"],
       "arquivo abaixo de MIN_FP_SIZE entrou na lista")


def test_nome_onipresente_nao_vira_prova(con):
    """Um nome em mais de MAX_FP_TREES árvores é chapa, não clone. O agregado
    descarta, e a lista tem de descartar igual — senão ela mostraria linhas que
    o número ao lado não conta."""
    a, b = "disco/um/banal0/sub", "disco/um/banal1/sub"
    r = overlap_files(con, a, b)
    ok(r["total"] == 0,
       "fingerprint em %d árvores devia ser descartada, vieram %d"
       % (MAX_FP_TREES + 2, r["total"]))


def test_like_nao_vaza_entre_irmas(con):
    """'a_b' e 'axb' são irmãs, e o `_` do LIKE casa qualquer caractere: sem
    escape, a árvore 'a_b' enxergaria os arquivos de 'axb' e o par apareceria
    com arquivos que não são dele."""
    vistos = _tree_files(con, 1, "a_b/dentro")
    nomes = sorted(n for n, _sz in vistos)
    ok(nomes == ["so_do_sublinhado.mkv"],
       "o prefixo 'a_b' enxergou além dele: %s" % nomes)
    ok("do_fundo_do_xis.mkv" not in nomes,
       "o `_` do LIKE puxou o arquivo de 'axb/dentro/fundo'")
    r = overlap_files(con, "disco/um/a_b/dentro", "disco/um/axb/dentro")
    ok(r["total"] == 0,
       "árvores com arquivos diferentes não deviam compartilhar nada: %s"
       % [x["name"] for x in r["rows"]])


def test_split_tree(con):
    ok(split_tree(con, "disco/um/All/Filmes") == (1, "All/Filmes"),
       "rótulo com barra não foi resolvido: %s"
       % (split_tree(con, "disco/um/All/Filmes"),))
    ok(split_tree(con, "dois/(raiz)") == (2, ""),
       "(raiz) devia virar prefixo vazio: %s" % (split_tree(con, "dois/(raiz)"),))
    ok(split_tree(con, "inexistente/x") is None,
       "volume que não existe devia devolver None")


def test_verdict():
    casos = [
        (("q1", "f1"), ("q2", "f1"), "full"),       # sha256 igual: prova
        (("q1", "f1"), ("q1", "f2"), "mismatch"),   # sha256 diferente: NÃO são
        (("q1", None), ("q1", None), "quick"),      # só o parcial, e bate
        (("q1", None), ("q2", None), "mismatch"),
        (None, ("q1", "f1"), "no"),                 # um lado sem hash: sem prova
        (("q1", "f1"), None, "no"),
    ]
    for ha, hb, esperado in casos:
        got = _verdict(ha, hb)
        ok(got == esperado, "verdict(%s,%s) deu %s, esperava %s"
           % (ha, hb, got, esperado))


def main():
    con, _ids = build()
    tree_overlap(con, depth=2, verbose=False)
    test_conta_bate_com_o_agregado(con)
    test_o_par_clonado_traz_os_tres(con)
    test_miudo_fica_de_fora(con)
    test_nome_onipresente_nao_vira_prova(con)
    test_like_nao_vaza_entre_irmas(con)
    test_split_tree(con)
    test_verdict()
    con.close()
    if falhas:
        print("overlap: %d FALHA(S)" % len(falhas))
        for f in falhas:
            print("  - %s" % f)
        return 1
    print("overlap: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
