# SPDX-License-Identifier: GPL-3.0-or-later
"""O auto-etiquetador por artista.

O que ele faz é barato de errar e caro de desfazer: etiqueta é o único dado do
projeto que não se recalcula, e uma rodada ruim espalha centenas de nomes pela
árvore que alguém depois tem de tirar à mão. Estes testes fixam as três coisas
que impedem isso -- o ensaio não grava, o lixo do campo não vira etiqueta, e o
piso de arquivos segura o resto.
"""
import os, sqlite3, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from diskmap import artistas, db, tags            # noqa: E402

FALHAS = []


def ok(cond, msg):
    if not cond:
        FALHAS.append(msg)


def mundo(tmp, arquivos):
    """Índice de mentira com os arquivos pedidos, e os arquivos no disco."""
    con = db.connect(os.path.join(tmp, "i.db"))
    con.execute("INSERT INTO volumes (id,label,mount) VALUES (1,'D1',?)", (tmp,))
    con.execute("INSERT INTO dirs (id,vol,relpath,depth) VALUES (1,1,'Musica',1)")
    for i, (nome, _) in enumerate(arquivos, 1):
        con.execute("INSERT INTO files (id,vol,dir,name,ext,size,mtime,cls) "
                    "VALUES (?,1,1,?,'mp3',100,0,'audio')", (i, nome))
    os.makedirs(os.path.join(tmp, "Musica"), exist_ok=True)
    for nome, _ in arquivos:
        open(os.path.join(tmp, "Musica", nome), "w").close()
    con.commit()
    return con


def grava(con, arquivos):
    """Põe o artista em audio_meta direto, sem depender do ffprobe estar aí."""
    con.executescript(artistas.SCHEMA)
    for i, (_, art) in enumerate(arquivos, 1):
        con.execute("INSERT OR REPLACE INTO audio_meta (file,artist,album,done_at) "
                    "VALUES (?,?,NULL,0)", (i, artistas.aceitavel(art) or None))
    con.commit()


def test_placeholder_nao_vira_etiqueta():
    """'Unknown Artist' não é um artista: viraria uma etiqueta cobrindo
    centenas de arquivos sem relação nenhuma, que é o oposto de etiquetar."""
    for lixo in ("Unknown Artist", "unknown artist", "  Various Artists ",
                 "VA", "n/a", "-", "", "  ", "2003", "01"):
        ok(artistas.aceitavel(lixo) == "", "aceitou lixo: %r" % lixo)
    for bom in ("Banda Azul", "Trio & Cia", "K2", "Lo-Fi"):
        ok(artistas.aceitavel(bom) == bom, "recusou artista bom: %r" % bom)


def test_maiuscula_nao_separa_artista():
    """A etiqueta não diferencia maiúscula (UNIQUE COLLATE NOCASE), então
    contar 'Banda Azul' e 'BANDA AZUL' como dois faria o piso recusar o que passa."""
    with tempfile.TemporaryDirectory() as tmp:
        arq = [("a.mp3", "Banda Azul"), ("b.mp3", "BANDA AZUL"), ("c.mp3", "banda azul")]
        con = mundo(tmp, arq)
        grava(con, arq)
        plano, _ = artistas.plano(con, minimo=3)
        ok(len(plano) == 1, "devia ser 1 artista, veio %d: %s" % (len(plano), list(plano)))
        ok(len(list(plano.values())[0]) == 3,
           "os três arquivos deviam cair no mesmo artista")
        con.close()


def test_piso_segura_o_avulso():
    with tempfile.TemporaryDirectory() as tmp:
        arq = [("a.mp3", "Banda Azul"), ("b.mp3", "Banda Azul"), ("c.mp3", "Avulso")]
        con = mundo(tmp, arq)
        grava(con, arq)
        plano, fora = artistas.plano(con, minimo=2)
        ok(list(plano) == ["Banda Azul"], "esperava só Banda Azul, veio %s" % list(plano))
        ok(fora == 1, "devia contar 1 descartado, veio %s" % fora)
        plano1, fora1 = artistas.plano(con, minimo=1)
        ok(len(plano1) == 2 and fora1 == 0, "--min 1 devia trazer os dois")
        con.close()


def test_album_no_campo_do_artista_dobra_no_artista():
    """Rip de CD inteiro grava "Artista - Álbum" no campo do artista. Sem
    desfazer, vira uma etiqueta por disco em vez de uma por artista -- a
    fragmentação que etiquetar deveria evitar.

    A regra é estreita de propósito: só dobra quando o prefixo já é um artista
    do próprio acervo. Nome com separador que NÃO é álbum fica inteiro."""
    with tempfile.TemporaryDirectory() as tmp:
        arq = [("a.mp3", "Banda Azul"), ("b.mp3", "Banda Azul"),
               ("c.mp3", "Banda Azul - Ao Vivo"), ("d.mp3", "Banda Azul - Ao Vivo"),
               ("e.mp3", "Trio & Cia"), ("f.mp3", "Trio & Cia"),
               # prefixo que NÃO é artista do acervo: tem de ficar inteiro,
               # senão a dobra inventa um artista que ninguém tem.
               ("g.mp3", "Lo-Fi - Coletânea"), ("h.mp3", "Lo-Fi - Coletânea")]
        con = mundo(tmp, arq)
        grava(con, arq)
        plano, _ = artistas.plano(con, minimo=1)
        ok("Banda Azul - Ao Vivo" not in plano,
           "o álbum virou etiqueta própria: %s" % list(plano))
        ok(len(plano.get("Banda Azul", [])) == 4,
           "os 4 deviam cair sob o artista, vieram %d"
           % len(plano.get("Banda Azul", [])))
        # a maioria do balde é do disco (2 do álbum contra 2 do artista): o
        # nome tem de vir da dobra, não da contagem
        arq2 = [("a.mp3", "Banda Azul"),
                ("c.mp3", "Banda Azul - Ao Vivo"), ("d.mp3", "Banda Azul - Ao Vivo"),
                ("e.mp3", "Banda Azul - Ao Vivo")]
        con2 = mundo(os.path.join(tmp, "b"), arq2)
        grava(con2, arq2)
        p2, _ = artistas.plano(con2, minimo=1)
        ok(list(p2) == ["Banda Azul"],
           "o nome veio do disco em vez do artista: %s" % list(p2))
        con2.close()
        ok("Trio & Cia" in plano, "dobrou um nome que não é álbum: %s" % list(plano))
        ok("Lo-Fi - Coletânea" in plano and "Lo-Fi" not in plano,
           "dobrou num prefixo que não é artista do acervo: %s" % list(plano))
        con.close()


def test_gravador_de_tela_nao_e_artista():
    """'Microsoft Game DVR' no campo do artista é o software que gravou, não
    quem fez. Viraria uma etiqueta cobrindo clipes de jogos sem relação."""
    for soft in ("Microsoft Game DVR", "microsoft game dvr", "OBS", "Bandicam"):
        ok(artistas.aceitavel(soft) == "", "aceitou gravador: %r" % soft)


def test_lista_de_nao_artistas_vale_no_plano_tambem():
    """Acrescentar um nome à lista tem de surtir efeito sem reler o acervo.
    O cache guarda o que passou pela lista do dia em que foi lido; se a
    política só valesse na leitura, corrigir a lista custaria dezenas de
    milhares de arquivos relidos para tirar uma etiqueta."""
    with tempfile.TemporaryDirectory() as tmp:
        arq = [("a.mp3", "Banda Azul"), ("b.mp3", "Banda Azul")]
        con = mundo(tmp, arq)
        con.executescript(artistas.SCHEMA)
        # escreve direto no cache, como se tivesse sido lido antes da lista crescer
        for i in (1, 2):
            con.execute("INSERT OR REPLACE INTO audio_meta (file,artist,album,done_at) "
                        "VALUES (?,?,NULL,0)", (i, "Microsoft Game DVR"))
        con.commit()
        plano, _ = artistas.plano(con, minimo=1)
        ok(plano == {}, "o cache velho passou pela lista: %s" % list(plano))
        con.close()


def test_plano_nao_grava_nada():
    """O ensaio é a defesa principal: ninguém revisa trezentas etiquetas depois
    de elas já estarem na árvore."""
    with tempfile.TemporaryDirectory() as tmp:
        arq = [("a.mp3", "Banda Azul"), ("b.mp3", "Banda Azul")]
        con = mundo(tmp, arq)
        grava(con, arq)
        artistas.plano(con, minimo=1)
        tags.ensure(con)
        n = con.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
        ok(n == 0, "o plano gravou %d etiqueta(s) -- devia gravar zero" % n)
        con.close()


def test_aplicar_poe_sob_a_mae():
    """Centenas de artistas soltos na raiz tornam a fileira de balões inútil;
    sob uma mãe, filtrar 'Artistas' traz todos e a lista fica navegável."""
    with tempfile.TemporaryDirectory() as tmp:
        arq = [("a.mp3", "Banda Azul"), ("b.mp3", "Banda Azul")]
        con = mundo(tmp, arq)
        grava(con, arq)
        plano, _ = artistas.plano(con, minimo=1)
        criadas, marcadas, falhas = artistas.aplicar(con, plano, progress=False)
        ok(not falhas, "falhou: %s" % falhas)
        ok(criadas == 1 and marcadas == 2, "veio %d/%d" % (criadas, marcadas))
        linha = [t for t in tags.listar(con) if t["name"] == "Banda Azul"]
        ok(linha and linha[0]["pai_nome"] == artistas.MAE,
           "não ficou sob a mãe: %s" % linha)
        casam = tags.clausula(con, ["Banda Azul"])
        ok(casam is not None, "a etiqueta não filtra nada")
        con.close()


def test_rescan_nao_pendura_artista_no_arquivo_errado():
    """audio_meta é por id de arquivo, e o SQLite recicla rowid. Se o rescan
    não derrubar a tabela, o artista de um arquivo passa a descrever outro --
    o mesmo defeito da decisão 25, numa tabela nova."""
    import re
    fonte = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "diskmap", "scan.py")).read()
    m = re.search(r'for _t in \(([^)]*)\)', fonte)
    ok(m and "audio_meta" in m.group(1),
       "audio_meta não está na limpeza do rescan em scan.py")


def main():
    for fn in sorted(g for g in globals() if g.startswith("test_")):
        globals()[fn]()
    if FALHAS:
        print("artistas: %d FALHA(S)" % len(FALHAS))
        for f in FALHAS:
            print("  - %s" % f)
        return 1
    print("artistas: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
