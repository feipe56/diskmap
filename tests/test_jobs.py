# SPDX-License-Identifier: GPL-3.0-or-later
"""O `argv` que a aba Tarefas monta vem do navegador.

Este é o único lugar do diskmap onde texto digitado numa página vira a linha de
comando de um processo. Nada passa por shell -- `subprocess.Popen` recebe uma
lista -- então não há `;` nem pipe que valha; o risco real é outro, e é por
isso que cada validação aqui tem um teste:

  - um caminho com `..` sobe para fora do volume e faz a varredura caminhar
    onde ninguém pediu;
  - um rótulo livre vira o nome de um volume no índice, e o índice é o que
    decide o que o painel oferece para apagar;
  - uma classe ou categoria livre vira uma flag que o CLI não conhece, e o
    processo morre com uma mensagem que ninguém entende.

Também está fixado aqui que `reconciliar` é ENSAIO por padrão: a flag
`--aplicar` só aparece se alguém a pedir. Um default invertido aqui escreveria
no índice a cada clique de "Conferir".

Roda:  python3 tests/test_jobs.py
"""
import os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from diskmap.jobs import EXCLUSIVE, Runner                          # noqa: E402

falhas = []


def ok(cond, msg):
    if not cond:
        falhas.append(msg)


def runner():
    return Runner(os.path.join(tempfile.mkdtemp(), "i.db"))


def recusa(kind, params, oque):
    r = runner()
    try:
        argv = r._argv(kind, params)
    except ValueError:
        return
    falhas.append("%s aceitou %s: %r" % (kind, oque, argv))


def test_reconciliar_e_ensaio_por_padrao():
    """Sem pedir, não escreve. O default errado aqui é silencioso e caro."""
    d = tempfile.mkdtemp()
    argv = runner()._argv("reconciliar", {"mount": d, "label": "VOL"})
    ok("--aplicar" not in argv, "o ensaio veio com --aplicar: %r" % argv)
    ok("reconciliar" in argv, "não chamou o subcomando certo: %r" % argv)
    com = runner()._argv("reconciliar",
                         {"mount": d, "label": "VOL", "aplicar": True})
    ok("--aplicar" in com, "pediu para aplicar e a flag não foi: %r" % com)


def test_reconciliar_monta_raiz_e_exclude():
    d = tempfile.mkdtemp()
    argv = runner()._argv("reconciliar", {
        "mount": d, "label": "VOL", "roots": ["a/b", "/c/"],
        "exclude": ["node_modules"]})
    ok(argv.count("--root") == 2, "perdeu uma raiz: %r" % argv)
    ok("a/b" in argv and "c" in argv, "não normalizou a barra: %r" % argv)
    ok("--exclude" in argv and "node_modules" in argv,
       "perdeu o exclude: %r" % argv)


def test_reconciliar_recusa_o_que_escapa_do_volume():
    d = tempfile.mkdtemp()
    recusa("reconciliar", {"mount": d, "roots": ["../fora"]}, "raiz com ..")
    recusa("reconciliar", {"mount": d, "roots": ["a/../../fora"]}, "..  no meio")
    recusa("reconciliar", {"mount": d, "roots": [""]}, "raiz vazia")
    recusa("reconciliar", {"mount": d, "exclude": ["a/b"]}, "exclude com barra")
    recusa("reconciliar", {"mount": "/nao/existe/mesmo"}, "mount inexistente")


def test_rotulo_nao_aceita_qualquer_coisa():
    """O rótulo nomeia um volume no índice, que é o que o painel age em cima."""
    d = tempfile.mkdtemp()
    for mau in ("a;rm -rf /", "../..", "a/b", "a b c|d", "$(echo)", "-x"):
        recusa("reconciliar", {"mount": d, "label": mau}, "rótulo %r" % mau)
        recusa("scan", {"mount": d, "label": mau}, "rótulo %r" % mau)
    bom = runner()._argv("reconciliar", {"mount": d, "label": "1 TB Dados"})
    ok("1_TB_Dados" in bom, "espaço devia virar _: %r" % bom)


def test_scan_e_reconciliar_validam_igual():
    """São o mesmo gesto com consequências diferentes; divergir aqui surpreende."""
    d = tempfile.mkdtemp()
    for params, oque in (({"mount": d, "roots": ["../x"]}, "raiz com .."),
                         ({"mount": d, "exclude": ["a/b"]}, "exclude com barra"),
                         ({"mount": "/nao/existe"}, "mount inexistente")):
        for kind in ("scan", "reconciliar"):
            recusa(kind, dict(params), "%s em %s" % (oque, kind))


def test_classes_e_categorias_sao_lista_fechada():
    d = tempfile.mkdtemp()
    recusa("acervo", {"cls": "qualquer"}, "classe desconhecida")
    recusa("acervo", {"cls": "--refresh"}, "classe que é flag")
    recusa("plan", {"categories": ["inventada"]}, "categoria desconhecida")
    recusa("verify", {"stage": "meio"}, "stage desconhecido")
    ok("--cls" in runner()._argv("acervo", {"cls": "video"}),
       "recusou uma classe que existe")
    del d


def test_reconciliar_e_exclusivo():
    """Ele caminha o disco e escreve no índice: não pode correr junto do scan."""
    ok("reconciliar" in EXCLUSIVE,
       "reconciliar fora da exclusão mútua: rodaria junto de um scan")


def test_comando_desconhecido_e_recusado():
    recusa("formatar", {}, "comando que não existe")


def main():
    for nome, fn in sorted(globals().items()):
        if nome.startswith("test_"):
            fn()
    if falhas:
        print("jobs: %d FALHA(S)" % len(falhas))
        for f in falhas:
            print("  - %s" % f)
        return 1
    print("jobs: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
