# SPDX-License-Identifier: GPL-3.0-or-later
"""As promessas da varredura de imagens, que precisam valer no arquivo estranho.

Uma varredura de 400 mil imagens leva horas e é retomável de propósito. O que
ela não pode é PARAR: um arquivo esquisito no meio tem de custar uma linha com
nota, não a noite inteira. Em 2026-10-21 custou — duas horas de varredura
morreram num `UnicodeDecodeError` porque o `sips` repetiu na mensagem de erro
um nome de arquivo gravado em Mac Roman. Estes testes guardam as duas camadas
que impedem aquilo: o decodificador do `_sips` e a promessa de `process_one`.

Roda:  python3 tests/test_images.py
"""
import os, sqlite3, subprocess, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from diskmap import images                                          # noqa: E402

falhas = []


def ok(cond, msg):
    if not cond:
        falhas.append(msg)


def _sips_falso(corpo):
    """Põe um `sips` nosso na frente do PATH e devolve o diretório dele."""
    d = tempfile.mkdtemp()
    cam = os.path.join(d, "sips")
    with open(cam, "w") as fh:
        fh.write("#!/bin/sh\n" + corpo)
    os.chmod(cam, 0o755)
    os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]
    return d


def test_sips_aguenta_byte_que_nao_e_utf8():
    """0x99 é um caractere legítimo em Mac Roman e lixo em UTF-8.

    Com `text=True` o subprocess decodifica em modo estrito e levanta; a
    exceção sobe pelo ex.map e derruba a varredura toda. Foi exatamente isso.
    """
    path0 = os.environ["PATH"]
    try:
        _sips_falso('printf "erro no arquivo \\231.jpg" >&2\nexit 1\n')
        try:
            r = images._sips(["-g", "pixelWidth", "/qualquer.jpg"])
        except UnicodeDecodeError as e:
            ok(False, "_sips levantou no byte inválido: %s" % e)
            return
        ok("erro no arquivo" in (r.stderr or ""),
           "perdeu a mensagem de erro: %r" % (r.stderr,))
    finally:
        os.environ["PATH"] = path0


def test_process_one_nao_levanta_nunca():
    """A docstring de `process_one` diz 'Never raises'. Isto cobra a dívida."""
    path0 = os.environ["PATH"]
    try:
        _sips_falso('printf "\\231\\231\\231" >&2\nexit 1\n')
        try:
            res = images.process_one(1, __file__, "jpg", "00/1.jpg", 10)
        except Exception as e:                                   # noqa: BLE001
            ok(False, "process_one levantou %s: %s" % (type(e).__name__, e))
            return
        ok(res["file"] == 1, "devolveu linha de outro arquivo: %r" % res)
        ok(res["thumb"] is None, "inventou miniatura de arquivo que falhou")
    finally:
        os.environ["PATH"] = path0


def test_arquivo_sumido_vira_nota_e_nao_erro():
    """Volume desmontado no meio da varredura é o caso comum, não o raro."""
    res = images.process_one(7, "/volume/que/nao/existe/foto.jpg", "jpg",
                             "00/7.jpg", 10)
    ok(res["note"] and "não encontrado" in res["note"],
       "não explicou o sumiço: %r" % res["note"])
    ok(res["thumb"] is None and res["dhash"] is None,
       "arquivo ausente não pode ter miniatura nem hash: %r" % res)


def test_pending_nao_oferece_sidecar_nem_dds():
    """Os dois descartes da fila de imagens, na consulta e não só na regra."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("""
      CREATE TABLE volumes (id INTEGER PRIMARY KEY, mount TEXT);
      CREATE TABLE dirs (id INTEGER PRIMARY KEY, vol INTEGER, relpath TEXT);
      CREATE TABLE files (id INTEGER PRIMARY KEY, vol INTEGER, dir INTEGER,
                          name TEXT, size INTEGER, ext TEXT, cls TEXT);
      CREATE TABLE image_meta (file INTEGER PRIMARY KEY);
      INSERT INTO volumes VALUES (1, '/mnt');
      INSERT INTO dirs VALUES (1, 1, 'fotos');
      INSERT INTO files VALUES (1, 1, 1, 'foto.jpg',    10, 'jpg', 'image');
      INSERT INTO files VALUES (2, 1, 1, '._foto.jpg',  10, 'jpg', 'image');
      INSERT INTO files VALUES (3, 1, 1, 'textura.dds', 10, 'dds', 'image');
      INSERT INTO files VALUES (4, 1, 1, '.oculta.jpg', 10, 'jpg', 'image');
    """)
    nomes = {t[4] for t in images.pending(con, 0, False, False)}
    ok("foto.jpg" in nomes, "a imagem de verdade saiu da fila: %s" % nomes)
    ok("._foto.jpg" not in nomes, "sidecar entrou na fila: %s" % nomes)
    ok("textura.dds" not in nomes, "dds entrou na fila: %s" % nomes)
    ok(".oculta.jpg" in nomes,
       "dotfile não é sidecar e tem de entrar: %s" % nomes)


def main():
    for nome, fn in sorted(globals().items()):
        if nome.startswith("test_"):
            fn()
    if falhas:
        print("images: %d FALHA(S)" % len(falhas))
        for f in falhas:
            print("  - %s" % f)
        return 1
    print("images: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
