# SPDX-License-Identifier: GPL-3.0-or-later
r"""Mandar para a Lixeira é a única ação do painel que não é rename interno.

Dois defeitos reais nasceram aqui, os dois descobertos esvaziando uma
quarentena de 68 arquivos, e os dois silenciosos:

  - `json.dumps` escapa não-ASCII como `\uXXXX` por padrão, e AppleScript não
    tem escape `\u`: ele responde erro de sintaxe. Todo caminho com acento
    falhava no Finder e caía no plano B, que não preserva "Colocar de volta".
    Num acervo em português isso é a maioria dos caminhos.

  - o desambiguador do `_move` era uma tentativa só, com `int(time.time())`.
    Rename no mesmo filesystem é instantâneo, então três arquivos de mesmo
    nome no mesmo segundo recebiam o mesmo destino — e `os.rename` sobrescreve
    sem avisar. Dois sobrevivem por sorte; o terceiro apaga o segundo.

Roda:  python3 tests/test_lixeira.py
"""
import json, os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from diskmap import actions                                      # noqa: E402

falhas = []


def ok(cond, msg):
    if not cond:
        falhas.append(msg)


def test_move_nunca_sobrescreve_mesmo_no_mesmo_segundo():
    """O caso que o acervo real tinha: vários de mesmo nome, de pastas
    diferentes, movidos para a mesma pasta de Lixeira em sequência."""
    d = tempfile.mkdtemp()
    destino = os.path.join(d, "lixo")
    finais = []
    for i in range(5):
        orig = os.path.join(d, "de%d" % i, "filme.avi")
        os.makedirs(os.path.dirname(orig))
        with open(orig, "w") as fh:
            fh.write("conteudo %d" % i)      # cada um é distinguível
        finais.append(actions._move(orig, os.path.join(destino, "filme.avi")))
    ok(len(set(finais)) == 5,
       "dois arquivos foram para o mesmo destino: %r" % (finais,))
    vivos = sorted(os.listdir(destino))
    ok(len(vivos) == 5, "sobraram %d de 5 arquivos em %s: %r"
       % (len(vivos), destino, vivos))
    conteudos = set()
    for f in vivos:
        with open(os.path.join(destino, f)) as fh:
            conteudos.add(fh.read())
    ok(conteudos == {"conteudo %d" % i for i in range(5)},
       "conteúdo perdido por sobrescrita: %r" % (sorted(conteudos),))


def test_move_nao_pisa_em_symlink_quebrado():
    """`os.path.exists` responde False para link pendurado, e aí o rename
    substituiria o link sem ninguém ver."""
    d = tempfile.mkdtemp()
    alvo = os.path.join(d, "destino.txt")
    os.symlink(os.path.join(d, "nao-existe"), alvo)
    orig = os.path.join(d, "orig.txt")
    with open(orig, "w") as fh:
        fh.write("x")
    final = actions._move(orig, alvo)
    ok(final != alvo, "sobrescreveu um symlink quebrado: %s" % final)
    ok(os.path.islink(alvo), "o symlink deixou de existir")


def test_script_do_finder_nao_escapa_acento():
    """A regressão central. O script vai inteiro para o `osascript -e`."""
    capturado = {}

    class Falso:
        returncode = 0
        stdout = stderr = ""

    def run_falso(argv, **kw):
        capturado["script"] = argv[argv.index("-e") + 1]
        return Falso()

    orig = actions.subprocess.run
    actions.subprocess.run = run_falso
    try:
        caminho = "/Volumes/X/Caixa 2°Edição/açaí não-ASCII.mkv"
        rc, _out, _mv = actions.do_trash(None, caminho, "X", "/Volumes/X")
    finally:
        actions.subprocess.run = orig
    s = capturado.get("script", "")
    ok(rc == 0, "do_trash falhou com Finder fingindo sucesso: rc=%r" % rc)
    ok("\\u" not in s,
       "o caminho foi escapado em \\uXXXX e o AppleScript não entende: %s" % s)
    ok(caminho in s, "o caminho não saiu literal no script: %s" % s)


def test_script_do_finder_ainda_protege_aspas_e_barra():
    """Tirar o escape de acento não pode abrir a porta para o resto. Um nome
    com aspas fecharia a string do AppleScript no meio."""
    capturado = {}

    class Falso:
        returncode = 0
        stdout = stderr = ""

    def run_falso(argv, **kw):
        capturado["script"] = argv[argv.index("-e") + 1]
        return Falso()

    orig = actions.subprocess.run
    actions.subprocess.run = run_falso
    try:
        for mau in ('/Volumes/X/a"b.mkv', "/Volumes/X/a\\b.mkv",
                    '/Volumes/X/" & (do shell script "id") & ".mkv'):
            actions.do_trash(None, mau, "X", "/Volumes/X")
            s = capturado["script"]
            corpo = s.split("POSIX file ", 1)[1]
            ok(corpo == json.dumps(mau, ensure_ascii=False),
               "citação errada para %r: %s" % (mau, corpo))
            ok(corpo.count('"') >= 2 and corpo.startswith('"')
               and corpo.endswith('"'),
               "a string do AppleScript não ficou fechada: %s" % corpo)
    finally:
        actions.subprocess.run = orig


def main():
    for nome, fn in sorted(globals().items()):
        if nome.startswith("test_"):
            fn()
    if falhas:
        print("lixeira: %d FALHA(S)" % len(falhas))
        for f in falhas:
            print("  - %s" % f)
        return 1
    print("lixeira: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
