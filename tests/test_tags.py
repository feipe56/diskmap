# SPDX-License-Identifier: GPL-3.0-or-later
"""Etiqueta é o único dado deste índice que não se recalcula.

Duplicata, miniatura, rollup, até o sha256: tudo isso é derivado e se refaz com
tempo. Uma etiqueta é um juízo que alguém fez — "isto é música" — e nada no
disco implica isso. Então o que está fixado aqui é a propriedade que a torna
confiável: **ela sobrevive a um rescan**.

A armadilha é concreta. `files.id` é INTEGER PRIMARY KEY, e um rescan apaga as
linhas; o SQLite devolve os rowids livres na ordem da varredura. Qualquer coisa
guardada por id reaparece pendurada em OUTRO arquivo — e numa etiqueta ninguém
percebe que o acervo passou a estar rotulado errado.

Roda:  python3 tests/test_tags.py
"""
import os, sqlite3, sys, tempfile, shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from diskmap import db, tags                                         # noqa: E402

falhas = []


def ok(cond, msg):
    if not cond:
        falhas.append(msg)


def mundo():
    base = tempfile.mkdtemp(prefix="tags-")
    for rel in ("Audio/Discos/f1.mkv", "Audio/Discos/f2.mkv",
                "Video/solto.mkv", "Outros/x.txt",
                # A armadilha do LIKE: '_' casa qualquer caractere. O vizinho
                # precisa ser MAIS FUNDO, senão o ramo `prefixo/%` nem é testado.
                "A_B/dentro/meu.mkv", "AxB/dentro/fundo/alheio.mkv"):
        p = os.path.join(base, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write("x")
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(db.SCHEMA)
    con.execute("INSERT INTO volumes (id,label,mount) VALUES (1,'T',?)", (base,))
    dirs = {"Audio/Discos": 1, "Video": 2, "Outros": 3,
            "A_B/dentro": 4, "AxB/dentro/fundo": 5}
    for rel, i in dirs.items():
        con.execute("INSERT INTO dirs (id,vol,relpath,depth) VALUES (?,1,?,?)",
                    (i, rel, rel.count("/") + 1))
    for d, nome in ((1, "f1.mkv"), (1, "f2.mkv"), (2, "solto.mkv"),
                    (3, "x.txt"), (4, "meu.mkv"), (5, "alheio.mkv")):
        con.execute("INSERT INTO files (vol,dir,name,size,mtime,ext,cls) "
                    "VALUES (1,?,?,10,1,'mkv','video')", (d, nome))
    con.commit()
    return base, con


def casam(con, *nomes):
    sql, args = tags.clausula(con, list(nomes))
    return sorted(r[0] for r in con.execute(
        "SELECT f.name FROM files f JOIN dirs d ON d.id=f.dir "
        "JOIN volumes v ON v.id=f.vol WHERE " + sql, args))


def test_pasta_etiquetada_cobre_o_conteudo():
    base, con = mundo()
    try:
        tags.marcar(con, "Música", os.path.join(base, "Audio/Discos"))
        ok(casam(con, "Música") == ["f1.mkv", "f2.mkv"],
           "a pasta devia cobrir as duas faixas, veio %s" % casam(con, "Música"))
        # arquivo novo na pasta entra sem ninguém tocar na etiqueta
        con.execute("INSERT INTO files (vol,dir,name,size,mtime,ext,cls) "
                    "VALUES (1,1,'f3.mkv',10,1,'mkv','video')")
        con.commit()
        ok("f3.mkv" in casam(con, "Música"),
           "arquivo novo na pasta etiquetada ficou de fora")
    finally:
        shutil.rmtree(base); con.close()


def test_arquivo_etiquetado_sozinho():
    base, con = mundo()
    try:
        tags.marcar(con, "Clipes", os.path.join(base, "Video/solto.mkv"))
        ok(casam(con, "Clipes") == ["solto.mkv"],
           "devia casar só o arquivo, veio %s" % casam(con, "Clipes"))
    finally:
        shutil.rmtree(base); con.close()


def test_sobrevive_ao_rescan():
    """A propriedade que justifica guardar por caminho em vez de por id."""
    base, con = mundo()
    try:
        tags.marcar(con, "Música", os.path.join(base, "Audio/Discos"))
        antes = casam(con, "Música")
        # o rescan apaga tudo que é por id e reinsere em outra ordem
        con.execute("DELETE FROM files WHERE vol=1")
        con.execute("DELETE FROM dirs WHERE vol=1")
        con.execute("INSERT INTO dirs (vol,relpath,depth) VALUES (1,'Outros',1)")
        con.execute("INSERT INTO dirs (vol,relpath,depth) VALUES (1,'Audio/Discos',2)")
        d = con.execute("SELECT id FROM dirs WHERE relpath='Audio/Discos'").fetchone()[0]
        for nome in ("f2.mkv", "f1.mkv"):
            con.execute("INSERT INTO files (vol,dir,name,size,mtime,ext,cls) "
                        "VALUES (1,?,?,10,1,'mkv','video')", (d, nome))
        con.commit()
        ok(casam(con, "Música") == antes,
           "a etiqueta não sobreviveu ao rescan: antes %s, depois %s"
           % (antes, casam(con, "Música")))
    finally:
        shutil.rmtree(base); con.close()


def test_like_nao_vaza_para_a_pasta_vizinha():
    """'A_B' e 'AxB' são irmãs, e o `_` do LIKE casa qualquer caractere: sem
    escape, etiquetar uma arrastaria os arquivos da outra — e o acervo passaria
    a estar rotulado errado sem ninguém perceber."""
    base, con = mundo()
    try:
        tags.marcar(con, "Meu", os.path.join(base, "A_B/dentro"))
        got = casam(con, "Meu")
        ok(got == ["meu.mkv"],
           "a etiqueta vazou para a pasta vizinha: %s" % got)
    finally:
        shutil.rmtree(base); con.close()


def test_subetiqueta_e_o_guarda_chuva_do_pai():
    """Filtrar pela mãe traz o que está na filha: 'Música' tem de incluir 'Ao vivo'.
    O contrário não — filtrar 'Ao vivo' não traz o resto de Séries."""
    base, con = mundo()
    try:
        tags.marcar(con, "Música", os.path.join(base, "Audio/Discos"))
        tags.marcar(con, "Ao vivo", os.path.join(base, "Video"), pai="Música")
        mae = casam(con, "Música")
        filha = casam(con, "Ao vivo")
        ok(sorted(mae) == ["f1.mkv", "f2.mkv", "solto.mkv"],
           "a mãe devia cobrir a filha também, veio %s" % mae)
        ok(filha == ["solto.mkv"], "a filha puxou coisa da mãe: %s" % filha)
        t = [x for x in tags.listar(con) if x["name"] == "Ao vivo"][0]
        ok(t["pai_nome"] == "Música", "a lista não diz quem é a mãe: %s" % t)
    finally:
        shutil.rmtree(base); con.close()


def test_ciclo_e_recusado():
    base, con = mundo()
    try:
        tags.marcar(con, "Música", os.path.join(base, "Audio/Discos"))
        tags.marcar(con, "Ao vivo", os.path.join(base, "Video"), pai="Música")
        for nome, pai in (("Música", "Ao vivo"), ("Música", "Música")):
            try:
                tags.marcar(con, nome, os.path.join(base, "Outros"), pai=pai)
                falhas.append("aceitou ciclo: %s dentro de %s" % (nome, pai))
            except ValueError as e:
                ok("ciclo" in str(e), "recusou por outro motivo: %s" % e)
        ok(casam(con, "Música"), "a árvore quebrou depois das recusas")
    finally:
        shutil.rmtree(base); con.close()


def test_mae_inexistente_e_recusada():
    base, con = mundo()
    try:
        try:
            tags.marcar(con, "X", os.path.join(base, "Outros"), pai="NãoExiste")
            falhas.append("aceitou mãe inexistente")
        except ValueError as e:
            ok("não existe" in str(e), "recusou por outro motivo: %s" % e)
        ok(tags.listar(con) == [], "criou a etiqueta mesmo recusando")
    finally:
        shutil.rmtree(base); con.close()


def test_lote_marca_varios_e_nao_para_no_primeiro_erro():
    """Numa seleção de duzentos, um caminho ruim não pode custar os outros 199.
    O que falhou volta nomeado, com o motivo."""
    base, con = mundo()
    try:
        bons = [os.path.join(base, p) for p in
                ("Audio/Discos/f1.mkv", "Audio/Discos/f2.mkv", "Video/solto.mkv")]
        r = tags.marcar_varios(con, "Favoritos", bons + ["/etc/passwd"])
        ok(r["feitos"] == 3, "devia marcar 3, marcou %s" % r["feitos"])
        ok(len(r["pulados"]) == 1 and "/etc/passwd" in r["pulados"][0]["path"],
           "não reportou o caminho recusado: %s" % r["pulados"])
        ok(r["ok"] is False, "disse ok com um caminho recusado")
        ok(casam(con, "Favoritos") == ["f1.mkv", "f2.mkv", "solto.mkv"],
           "o lote não casou os três: %s" % casam(con, "Favoritos"))
        # e tira em lote
        r2 = tags.marcar_varios(con, "Favoritos", bons[:2], acao="remove")
        ok(r2["feitos"] == 2, "devia tirar 2")
        ok(casam(con, "Favoritos") == ["solto.mkv"],
           "sobrou o que não devia: %s" % casam(con, "Favoritos"))
    finally:
        shutil.rmtree(base); con.close()


def test_lote_respeita_mae_e_teto():
    base, con = mundo()
    try:
        tags.marcar(con, "Coletâneas", os.path.join(base, "Outros"))
        tags.marcar_varios(con, "Curtas", [os.path.join(base, "Video/solto.mkv")],
                           pai="Coletâneas")
        t = [x for x in tags.listar(con) if x["name"] == "Curtas"][0]
        ok(t["pai_nome"] == "Coletâneas", "o lote ignorou a mãe: %s" % t)
        try:
            tags.marcar_varios(con, "X", ["/a"] * (tags.MAX_LOTE + 1))
            falhas.append("aceitou lote acima do teto")
        except ValueError as e:
            ok("grande demais" in str(e), "recusou por outro motivo: %s" % e)
        try:
            tags.marcar_varios(con, "X", [])
            falhas.append("aceitou lote vazio")
        except ValueError as e:
            ok("nenhum caminho" in str(e), "recusou por outro motivo: %s" % e)
    finally:
        shutil.rmtree(base); con.close()


def test_fora_do_indice_e_recusado():
    base, con = mundo()
    try:
        for ruim in ("/etc/passwd", "/", os.path.expanduser("~")):
            try:
                tags.marcar(con, "X", ruim)
                falhas.append("aceitou etiquetar caminho fora do índice: %s" % ruim)
            except ValueError:
                pass
    finally:
        shutil.rmtree(base); con.close()


def test_nome_e_tratado_como_texto_de_gente():
    base, con = mundo()
    try:
        r = tags.marcar(con, "  Música   ao   vivo  ", os.path.join(base, "Video"))
        ok(r["tag"] == "Música ao vivo", "não normalizou o espaço: %r" % r["tag"])
        tags.marcar(con, "música AO vivo", os.path.join(base, "Outros"))
        nomes = [t["name"] for t in tags.listar(con)]
        ok(nomes.count("Música ao vivo") == 1 and len(nomes) == 1,
           "maiúscula criou etiqueta duplicada: %s" % nomes)
        for vazio in ("", "   ", None):
            try:
                tags.marcar(con, vazio, os.path.join(base, "Video"))
                falhas.append("aceitou etiqueta sem nome: %r" % vazio)
            except ValueError:
                pass
    finally:
        shutil.rmtree(base); con.close()


def test_etiquetas_de_um_caminho_incluem_as_herdadas():
    base, con = mundo()
    try:
        tags.marcar(con, "Música", os.path.join(base, "Audio/Discos"))
        tags.marcar(con, "Favorito", os.path.join(base, "Audio/Discos/f1.mkv"))
        t = tags.do_caminho(con, os.path.join(base, "Audio/Discos/f1.mkv"))
        nomes = {x["name"]: x["herdada"] for x in t}
        ok(nomes == {"Favorito": False, "Música": True},
           "esperava Favorito direta e Séries herdada, veio %s" % nomes)
    finally:
        shutil.rmtree(base); con.close()


def test_desmarcar_e_apagar():
    base, con = mundo()
    try:
        tags.marcar(con, "Música", os.path.join(base, "Audio/Discos"))
        tags.desmarcar(con, "Música", os.path.join(base, "Audio/Discos"))
        ok(casam(con, "Música") == [], "desmarcar não tirou o caminho")
        ok([t["name"] for t in tags.listar(con)] == ["Música"],
           "desmarcar apagou o nome que a pessoa escolheu")
        tags.apagar(con, "Música")
        ok(tags.listar(con) == [], "apagar não removeu a etiqueta")
    finally:
        shutil.rmtree(base); con.close()


def main():
    for nome, fn in sorted(globals().items()):
        if nome.startswith("test_"):
            fn()
    if falhas:
        print("tags: %d FALHA(S)" % len(falhas))
        for f in falhas:
            print("  - %s" % f)
        return 1
    print("tags: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
