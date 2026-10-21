# SPDX-License-Identifier: GPL-3.0-or-later
"""O roteamento de quem renderiza o quê, que é onde o custo mora.

QuickLook não recusa rápido o que não sabe abrir: ele PENDURA, oito segundos por
arquivo. Chamar o renderizador em tudo e esperar levaria dias num acervo de
milhares de vídeos e compactados. Por isso ele só é chamado no que foi medido
como renderizável, e por isso esta lista é um teste e não um comentário.

Roda:  python3 tests/test_acervo.py
"""
import os, sqlite3, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from diskmap import acervo, classify, db                             # noqa: E402

# Medido em arquivos reais: o qlmanage pendurou 8s em cada um destes.
PENDURAM = ["mkv", "avi", "wmv", "flv", "webm", "rtf", "zip", "rar", "7z",
            "iso", "exe", "epub"]
falhas = []


def ok(cond, msg):
    if not cond:
        falhas.append(msg)


def test_quicklook_nunca_ve_o_que_pendura():
    """A falha cara não é 'não consigo', é 'vou pensar por oito segundos'."""
    for e in PENDURAM:
        ok(e not in acervo.QL_EXTS,
           ".%s pendura o QuickLook e não pode estar em QL_EXTS" % e)


def test_as_duas_listas_nao_se_cruzam():
    """Uma extensão nos dois lugares torna o renderizador uma questão de ordem
    de leitura do código, não de decisão."""
    dupla = acervo.QL_EXTS & acervo.FF_EXTS
    ok(not dupla, "extensões em QL_EXTS e FF_EXTS ao mesmo tempo: %s" % sorted(dupla))


def test_roteamento():
    casos = [("pdf", "ql"), ("docx", "ql"), ("mp4", "ql"),
             ("zip", None), ("jpg", None), ("", None)]
    for ext, quem in casos:
        got = acervo.engine_for(ext)
        ok(got == quem, "engine_for(%r) deu %r, esperava %r" % (ext, got, quem))
    # mkv depende do ffmpeg existir: a ferramenta é opcional, nunca exigida
    esperado = "ffmpeg" if acervo.have_ffmpeg() else None
    ok(acervo.engine_for("mkv") == esperado,
       "com ffmpeg=%s, .mkv devia rotear para %r" % (acervo.have_ffmpeg(), esperado))


def test_imagem_nao_passa_por_aqui():
    """Imagem tem pipeline próprio (dimensão, hash visual, biblioteca). Se
    `engine_for` aceitasse jpg, os dois gerariam miniatura do mesmo arquivo em
    tabelas diferentes e o /thumb serviria uma das duas por sorte."""
    for e in ("jpg", "jpeg", "png", "gif", "bmp", "tga", "dds", "webp"):
        ok(acervo.engine_for(e) is None, ".%s não é trabalho deste módulo" % e)


def test_thumb_rel_espalha():
    """Dezenas de milhares de arquivos num diretório só é lento em qualquer
    sistema de arquivos; o mesmo leque de dois níveis que images.py usa."""
    a = acervo._thumb_rel(7)
    b = acervo._thumb_rel(263)       # 263 % 256 == 7: mesmo galho, nomes distintos
    ok(a.split(os.sep)[0] == b.split(os.sep)[0] == "07",
       "o leque não agrupou por resto: %s %s" % (a, b))
    ok(a != b, "dois ids diferentes deram o mesmo caminho: %s" % a)


def test_preview_de_arquivo_ausente_nao_quebra():
    k, rel, eng, note = acervo.preview_one(1, "/nao/existe/x.pdf", "pdf", "doc")
    ok(rel is None and eng == "ausente", "devia reportar ausente, veio %r/%r" % (rel, eng))
    ok(note and "disco" in note, "não explicou: %r" % note)


def test_pending_nao_enfileira_o_que_ninguem_renderiza():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(db.SCHEMA)
    con.execute("INSERT INTO volumes (id,label,mount) VALUES (1,'T','/t')")
    con.execute("INSERT INTO dirs (id,vol,relpath,depth) VALUES (1,1,'a',1)")
    for nome, ext, cls in (("doc.pdf", "pdf", "doc"), ("pacote.zip", "zip", "archive"),
                           ("filme.mkv", "mkv", "video"), ("foto.jpg", "jpg", "image")):
        con.execute("INSERT INTO files (vol,dir,name,size,mtime,ext,cls) "
                    "VALUES (1,1,?,100,1,?,?)", (nome, ext, cls))
    con.commit()
    nomes = sorted(os.path.basename(t[1]) for t in acervo.pending(con))
    esperado = ["doc.pdf", "filme.mkv"] if acervo.have_ffmpeg() else ["doc.pdf"]
    ok(nomes == esperado, "fila devia ser %s, veio %s" % (esperado, nomes))
    ok("pacote.zip" not in nomes, "enfileirou um .zip: cada um custa 8s pendurado")
    ok("foto.jpg" not in nomes, "enfileirou imagem, que tem pipeline próprio")
    ok(all(len(t) == 5 for t in acervo.pending(con)),
       "pending tem de devolver o tamanho junto, senão o piso não funciona")
    con.close()


def test_piso_de_tamanho():
    """A maioria dos documentos de um acervo é texto minúsculo, e renderizar a
    primeira página de um .txt de 2 KB custa o mesmo que a de um PDF de 90 MB.
    O piso é o que torna uma passada completa viável em horas e não em dias."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(db.SCHEMA)
    con.execute("INSERT INTO volumes (id,label,mount) VALUES (1,'T','/t')")
    con.execute("INSERT INTO dirs (id,vol,relpath,depth) VALUES (1,1,'a',1)")
    for nome, tam in (("grande.pdf", 5_000_000), ("miudo.pdf", 900)):
        con.execute("INSERT INTO files (vol,dir,name,size,mtime,ext,cls) "
                    "VALUES (1,1,?,?,1,'pdf','doc')", (nome, tam))
    con.commit()
    feitos, _f = acervo.enrich(con, min_size=1_000_000, progress=False, workers=1)
    gravados = [r[0] for r in con.execute(
        "SELECT f.name FROM media_meta m JOIN files f ON f.id=m.file")]
    ok(gravados == ["grande.pdf"],
       "o piso devia deixar só o grande, gravou %s" % gravados)
    con.close()


def test_audio_sem_capa_nao_vira_miniatura():
    """O QuickLook devolve a própria nota musical para áudio sem capa: 6 KB
    iguais para todo arquivo, indistinguíveis de uma capa de verdade na tela.
    Gastar um processo por arquivo para produzir isso é pagar caro por um ícone
    pior que o desenhado de graça — e que ainda mente, porque parece informação.

    O `ffprobe` responde a pergunta real em 0,06s. Sem ele a resposta é "não
    sei", e não saber significa não renderizar."""
    import subprocess as sp
    real = acervo.shutil.which
    try:
        acervo.shutil.which = lambda x: None          # fingindo máquina sem ffmpeg
        ok(acervo.tem_capa("/qualquer.mp3") is None,
           "sem ffprobe devia responder 'não sei'")
        k, rel, eng, note = acervo.preview_one(1, __file__, "mp3", "audio")
        ok(rel is None and eng == "nenhum",
           "sem ffprobe não devia renderizar: %r/%r" % (rel, eng))
        ok(note and "ffprobe" in note, "não explicou: %r" % note)
    finally:
        acervo.shutil.which = real
    # com ffprobe presente, um arquivo que não é áudio não tem capa
    if acervo.shutil.which("ffprobe"):
        k, rel, eng, note = acervo.preview_one(1, __file__, "mp3", "audio")
        ok(rel is None and eng == "sem-capa",
           "arquivo sem capa devia parar antes do renderizador: %r/%r" % (rel, eng))


# Nomes que a regra de sidecar precisa separar. O `.gitignore` e o `.env` estão
# aqui porque a primeira versão desta regra os descartava: `_` é curinga em
# LIKE, e um `'._%'` sem ESCAPE casa com todo arquivo que começa com ponto.
SIDECARS = ["._notas.txt", "._foto.jpg", "._", "._a", "._.DS_Store"]
NAO_SIDECARS = [".gitignore", ".env", ".bashrc", ".a.txt", "normal.txt",
                "foto.jpg", "a._b.txt", ".", "", "_x.txt"]


def test_sidecar_sql_casa_com_python():
    """A regra existe duas vezes -- em Python e em SQL -- porque a consulta não
    pode trazer 1,2 milhão de linhas só para filtrá-las. Duas cópias de uma
    regra sempre divergem neste projeto, então a equivalência é um teste.
    """
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE f (name TEXT)")
    nomes = SIDECARS + NAO_SIDECARS
    con.executemany("INSERT INTO f VALUES (?)", [(n,) for n in nomes])
    passam = {r[0] for r in con.execute(
        "SELECT name FROM f WHERE " + classify.sql_nao_sidecar("f.name"))}
    for n in nomes:
        py = not classify.eh_sidecar(n)
        ok(py == (n in passam),
           "SQL e Python discordam em %r: python=%s sql=%s"
           % (n, py, n in passam))


def test_sidecar_nao_descarta_dotfile():
    """O erro que o ESCAPE evita, dito pelo nome."""
    for n in [".gitignore", ".env", ".bashrc"]:
        ok(not classify.eh_sidecar(n), "%s não é sidecar" % n)
    for n in SIDECARS:
        ok(classify.eh_sidecar(n), "%s é sidecar" % n)


def test_pending_nao_oferece_sidecar():
    """O filtro tem de estar na consulta que o enrich usa, não só na regra."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("""
      CREATE TABLE volumes (id INTEGER PRIMARY KEY, mount TEXT);
      CREATE TABLE dirs (id INTEGER PRIMARY KEY, vol INTEGER, relpath TEXT);
      CREATE TABLE files (id INTEGER PRIMARY KEY, vol INTEGER, dir INTEGER,
                          name TEXT, size INTEGER, ext TEXT, cls TEXT);
      INSERT INTO volumes VALUES (1, '/mnt');
      INSERT INTO dirs VALUES (1, 1, 'pasta');
      INSERT INTO files VALUES (1, 1, 1, 'relatorio.pdf', 10, 'pdf', 'doc');
      INSERT INTO files VALUES (2, 1, 1, '._relatorio.pdf', 10, 'pdf', 'doc');
      INSERT INTO files VALUES (3, 1, 1, '.gitignore.txt', 10, 'txt', 'doc');
    """)
    nomes = {os.path.basename(t[1]) for t in acervo.pending(con)}
    ok("relatorio.pdf" in nomes, "o arquivo de verdade saiu da fila: %s" % nomes)
    ok("._relatorio.pdf" not in nomes, "o sidecar entrou na fila: %s" % nomes)
    ok(".gitignore.txt" in nomes, "dotfile virou sidecar: %s" % nomes)


def main():
    for nome, fn in sorted(globals().items()):
        if nome.startswith("test_"):
            fn()
    if falhas:
        print("acervo: %d FALHA(S)" % len(falhas))
        for f in falhas:
            print("  - %s" % f)
        return 1
    print("acervo: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
