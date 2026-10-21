# SPDX-License-Identifier: GPL-3.0-or-later
"""O que é medida e o que é palpite, fixado.

A leitura do `ioreg` e do `lsof` é pura, então roda com saída fixa em vez de
depender do que a máquina estiver fazendo. O que está fixado aqui:

  - estatística sem nome BSD não vira disco anônimo: some;
  - contador que anda para trás (pid reciclado, disco que sumiu e voltou) vira
    zero, nunca um pico inventado de gigabytes;
  - processo que nasceu dentro da janela não aparece, porque não há base para
    subtrair - e isso é o motivo de a soma dos processos não fechar;
  - o prefixo de montagem é testado do mais longo para o mais curto;
  - `proc_pid_rusage` recusado devolve None, nunca zero: zero seria mentira.

Roda:  python3 tests/test_iowatch.py
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from diskmap import iowatch  # noqa: E402


def stats(rd, wr, ro=0, wo=0):
    return {"Bytes (Read)": rd, "Bytes (Write)": wr,
            "Operations (Read)": ro, "Operations (Write)": wo}


IOREG = [
    {"Statistics": stats(1000, 2000, 10, 20),
     "IORegistryEntryChildren": [{"IOObjectClass": "IOMedia", "BSD Name": "disk0"}]},
    {"Statistics": stats(500, 0, 5, 0),
     "IORegistryEntryChildren": [{"IOObjectClass": "IOMedia"}]},   # sem nome BSD
    {"IORegistryEntryChildren": [{"BSD Name": "disk9"}]},          # sem estatística
    {"Statistics": stats(77, 88, 1, 2),
     "IORegistryEntryChildren": [{"IOObjectClass": "IOMedia", "BSD Name": "disk5"}]},
]


def snap(t, disks, procs, denied=0):
    return {"t": t, "disks": disks, "procs": procs, "denied": denied}


def P(user, comm, rd, wr):
    return {"user": user, "comm": comm, "read": rd, "write": wr}


def main():
    bad, n = [], 0

    # 1. ioreg: só vira disco o que tem estatística E nome
    n += 4
    d = iowatch.parse_ioreg(IOREG)
    if sorted(d) != ["disk0", "disk5"]:
        bad.append(f"discos deveriam ser disk0 e disk5, vieram {sorted(d)}")
    if d.get("disk0", {}).get("write") != 2000:
        bad.append("bytes escritos de disk0 errados")
    if "disk9" in d:
        bad.append("nó sem estatística não pode virar disco")
    if iowatch.parse_ioreg([]) != {}:
        bad.append("ioreg vazio deveria dar dicionário vazio")

    # 2. taxa: diferença por segundo, e contador que volta não inventa pico
    n += 4
    if iowatch._rate(3000, 1000, 2.0) != 1000.0:
        bad.append("taxa simples errada")
    if iowatch._rate(10, 999999, 2.0) != 0.0:
        bad.append("contador que andou para trás deveria dar 0")
    if iowatch._rate(10, 0, 0) != 0.0:
        bad.append("janela de duração zero deveria dar 0")
    if iowatch._rate(0, 0, 1.0) != 0.0:
        bad.append("sem movimento deveria dar 0")

    # 3. rates(): o que entra e o que fica de fora
    zero = {"read": 0, "write": 0, "rops": 0, "wops": 0}
    a = snap(100.0, {"disk0": dict(zero), "disk1": dict(zero)},
             {10: P("eu", "antigo", 0, 0), 11: P("eu", "parado", 7, 7)})
    b = snap(102.0,
             {"disk0": {"read": 2000, "write": 4000, "rops": 2, "wops": 4},
              "disk1": {"read": 0, "write": 0, "rops": 0, "wops": 0},
              "disk7": {"read": 9e9, "write": 9e9, "rops": 1, "wops": 1}},
             {10: P("eu", "antigo", 200, 400),
              11: P("eu", "parado", 7, 7),
              12: P("eu", "recem-nascido", 1 << 30, 1 << 30)}, denied=3)
    r = iowatch.rates(a, b, con=None, top=5)

    n += 7
    if abs(r["seconds"] - 2.0) > 1e-9:
        bad.append(f"janela deveria ser 2 s, veio {r['seconds']}")
    d0 = next((x for x in r["disks"] if x["dev"] == "disk0"), None)
    if not d0 or d0["write"] != 2000.0:
        bad.append(f"disk0 deveria gravar 2000 B/s, veio {d0 and d0['write']}")
    if any(x["dev"] == "disk7" for x in r["disks"]):
        bad.append("disco que apareceu no meio da janela não tem base: não pode entrar")
    pids = [p["pid"] for p in r["procs"]]
    if 12 in pids:
        bad.append("processo nascido dentro da janela não tem base: não pode entrar")
    if 11 in pids:
        bad.append("processo que não moveu byte nenhum não deveria aparecer")
    if pids[:1] != [10]:
        bad.append(f"ordem deveria começar pelo que mais moveu, veio {pids}")
    if r["denied"] != 3:
        bad.append("o número de processos recusados tem que chegar na saída")

    # 4. a ordem dos pontos de montagem decide o rótulo
    n += 3
    mounts = [("/Volumes/DISCO1/jogos", "JOGOS"), ("/Volumes/DISCO1", "DISCO1"),
              ("/", "Sistema")]
    got = iowatch.parse_lsof("p1\nn/Volumes/DISCO1/jogos/a.pak\n"
                             "p2\nn/Volumes/DISCO1/outro.bin\n"
                             "p3\nn/etc/hosts\n", mounts)
    if got.get(1) != ["JOGOS"]:
        bad.append(f"caminho mais específico deveria ganhar, veio {got.get(1)}")
    if got.get(2) != ["DISCO1"]:
        bad.append(f"pid 2 deveria ser DISCO1, veio {got.get(2)}")
    if got.get(3) != ["Sistema"]:
        bad.append(f'volume montado em "/" deveria casar, veio {got.get(3)}')

    # 5. prefixo tem que ser de pasta, não de texto
    n += 2
    got = iowatch.parse_lsof("p1\nn/Volumes/DISCO1-antigo/x\n",
                             [("/Volumes/DISCO1", "DISCO1")])
    if got:
        bad.append("DISCO1-antigo não é o volume DISCO1")
    got = iowatch.parse_lsof("p1\nn/Volumes/DISCO1\n", [("/Volumes/DISCO1", "DISCO1")])
    if got.get(1) != ["DISCO1"]:
        bad.append("o próprio ponto de montagem deveria casar")

    # 6. um volume só aparece uma vez por processo
    n += 1
    got = iowatch.parse_lsof("p1\nn/d/a\nn/d/b\nn/d/c\n", [("/d", "D")])
    if got.get(1) != ["D"]:
        bad.append(f"rótulo repetido deveria ser colapsado, veio {got.get(1)}")

    # 7. lsof sem nada para perguntar não chama processo nenhum
    n += 2
    if iowatch.open_volumes([], [("/d", "D")]) != {}:
        bad.append("sem pid não há o que perguntar")
    if iowatch.open_volumes([1], []) != {}:
        bad.append("sem volume indexado não há rótulo possível")

    # 8. o contador do processo: o seu responde, um pid inexistente recusa
    n += 3
    mine = iowatch.proc_io(os.getpid())
    if not (isinstance(mine, tuple) and len(mine) == 2):
        bad.append(f"proc_io de si mesmo deveria dar (lidos, escritos), veio {mine!r}")
    elif not all(isinstance(v, int) and v >= 0 for v in mine):
        bad.append(f"contadores deveriam ser inteiros não negativos: {mine!r}")
    if iowatch.proc_io(0x7FFFFFF0) is not None:
        bad.append("pid inexistente deveria devolver None, não zero")

    for b_ in bad:
        print("ERRO " + b_)
    print(f"{n - len(bad)}/{n} corretos")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
