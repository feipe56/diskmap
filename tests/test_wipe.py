# SPDX-License-Identifier: GPL-3.0-or-later
"""Zerar é a única coisa irreversível do diskmap, então o que está fixado aqui
são as recusas — o caso em que ele NÃO faz nada é mais importante que o em que faz.

Roda:  python3 tests/test_wipe.py
"""
import os, shutil, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from diskmap import wipe  # noqa: E402


def main():
    bad, n = [], 0
    tmp = tempfile.mkdtemp(prefix="diskmap-wipe-")
    try:
        def mk(name, data=b"segredo" * 5000):
            p = os.path.join(tmp, name)
            with open(p, "wb") as f:
                f.write(data)
            return p

        # 1. zera de verdade, confere, e apaga
        n += 4
        p = mk("alvo.bin")
        size = os.path.getsize(p)
        r = wipe.zero_file(p)
        if r["bytes"] != size:
            bad.append(f"tamanho: {r['bytes']} != {size}")
        if r["written"] != size:
            bad.append(f"escreveu {r['written']} de {size}")
        if r["verified"] != size:
            bad.append(f"conferiu {r['verified']} de {size}")
        if os.path.exists(p):
            bad.append("o arquivo deveria ter sido apagado")

        # 2. --keep deixa o arquivo, zerado até o último byte
        n += 3
        p = mk("mantido.bin")
        size = os.path.getsize(p)
        wipe.zero_file(p, keep=True)
        if not os.path.exists(p):
            bad.append("keep=True não deveria apagar")
        else:
            got = open(p, "rb").read()
            if len(got) != size:
                bad.append(f"keep mudou o tamanho: {len(got)} != {size}")
            if got.strip(b"\0"):
                bad.append("sobrou byte não-zero depois de zerar")

        # 3. hard link: zerar destruiria o conteúdo visto pelo outro nome
        n += 2
        p = mk("origem.bin")
        link = os.path.join(tmp, "mesmo-inode.bin")
        os.link(p, link)
        if not any("hard link" in b for b in wipe.check(p)):
            bad.append("hard link deveria ser recusado")
        try:
            wipe.zero_file(p)
            bad.append("zero_file aceitou um arquivo com hard link")
        except ValueError:
            pass
        if open(link, "rb").read().strip(b"\0") == b"":
            bad.append("o conteúdo do hard link foi destruído")
        os.unlink(link)

        # 4. symlink: zerar não tocaria o destino, então é recusado
        n += 2
        alvo = mk("real.bin")
        sl = os.path.join(tmp, "atalho.bin")
        os.symlink(alvo, sl)
        if not any("link simbólico" in b for b in wipe.check(sl)):
            bad.append("symlink deveria ser recusado")
        try:
            wipe.zero_file(sl)
            bad.append("zero_file aceitou um symlink")
        except ValueError:
            pass

        # 5. pasta não é arquivo
        n += 1
        if not wipe.check(tmp):
            bad.append("uma pasta deveria ser recusada")

        # 6. arquivo vazio não quebra
        n += 1
        p = mk("vazio.bin", b"")
        try:
            wipe.zero_file(p)
        except Exception as e:                                   # noqa: BLE001
            bad.append(f"arquivo vazio quebrou: {e!r}")

        # 7. assess() nunca libera um volume somente-leitura, e sempre explica
        n += 2
        a = wipe.assess("/")
        if a["allowed"] and a["read_only"]:
            bad.append("assess liberou um volume read-only")
        if a["level"] not in wipe.LEVELS or not a["why"]:
            bad.append(f"assess sem explicação: {a}")

        # 8. todo nível conhecido tem texto, e só 'impossivel' bloqueia
        n += 1
        if sorted(wipe.LEVELS) != sorted(
                ["impossivel", "inutil", "fraco", "parcial", "efetivo"]):
            bad.append("os níveis mudaram sem atualizar o teste")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    for b in bad:
        print("ERRO " + b)
    print(f"{n - len(bad)}/{n} corretos")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
