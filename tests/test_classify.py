# SPDX-License-Identifier: GPL-3.0-or-later
"""Classification is pure and cheap to check, so it gets a real test.

Three bugs in here were found by hand and are pinned below: a `/fbx/` pattern
that missed a volume-root path with no leading slash, `Documents/` outranking an
asset pack that happened to live under it, and a machine-specific rule that
outranked a launcher install.

Everything here runs against `builtin_rules()`, never the live rule list: a test
that read ~/.diskmap/rules.json would pass or fail depending on whose machine it
ran on.

Run:  python3 tests/test_classify.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from diskmap import images  # noqa: E402
from diskmap.images import classify, builtin_rules, ORIGIN, GENERIC, _compile  # noqa: E402

BUILTIN = builtin_rules()

CASES = [
    # (relpath, name, ext, expected library)
    # a volume-root path has no leading slash - the bug that `(^|/)` fixed
    ("FBX/the-bathroom-free/source", "Wall_trim_normal.png", "png", "modelo-3d"),
    # content beats location: an asset pack under Documents is an asset pack
    ("Users/me/Documents/dev/My project/Assets/StarterPack/Misc",
     "x.png", "png", "asset-pack"),
    ("dev/Unity/Templates/Core/Art/Misc/Sky", "Station.hdr", "hdr", "asset-pack"),
    ("project/docs/art/Maps", "Street_Names.psd", "psd", "mapa-arte"),
    ("Archive/2019/AssetsZip", "Street_Names.psd", "psd", "arquivo-morto"),
    # origin wins over everything: this is a game install, not a map
    ("SteamLibrary/steamapps/common/SomeGame/media/maps",
     "worldmap.png", "png", "jogo-instalado"),
    ("InvokeAI/outputs/images", "a.png", "png", "ia-gerada"),
    ("Desktop", "Screenshot 2026-05-12 at 10.47.01.png", "png", "screenshot"),
    ("engine/res/textures", "F.png", "png", "textura"),
    ("Users/me/AppData/Local/Programs/OSGeo4W/share/proj", "au.tif", "tif", "sistema"),
    ("Downloads", "25-08.jpeg", "jpeg", "download"),
    ("Pictures", "foto.jpg", "jpg", "foto"),
    ("", "algo-solto.png", "png", "outros"),
    # extension-only rule still applies wherever the file sits
    ("qualquer/lugar", "IMG_0001.heic", "heic", "foto"),
]

# Your own rules from ~/.diskmap/rules.json splice in between the two built-in
# tiers. These cases pin that position, which is the whole contract: a rule of
# yours outranks "what the file is" but never "where it came from".
MINE = _compile([("projeto-dev", r"(myengine|coolgame)", None)])
SPLICED = _compile(ORIGIN) + MINE + _compile(GENERIC)

SPLICE_CASES = [
    # beats a GENERIC rule: this would be `textura` on the built-ins alone
    ("myengine/res/textures", "F.png", "png", "projeto-dev", "textura"),
    # but never beats an ORIGIN rule: a launcher's copy stays the launcher's
    ("SteamLibrary/steamapps/common/coolgame/art", "a.png", "png",
     "jogo-instalado", "jogo-instalado"),
]


def quarentena_ignorada():
    """A quarentena tem de ser invisível para o scanner.

    Sem isso, mandar uma cópia para a quarentena e reindexar faz o par voltar a
    aparecer como duplicata — agora apontando para dentro da quarentena. A
    desduplicação ficaria invisível para a própria ferramenta que a fez.

    O nome é literal nos dois lados (`classify` não pode importar `actions`, que
    importa `classify`), então o teste existe para que eles não divirjam.
    """
    from diskmap.actions import QUAR_NAME
    from diskmap.classify import SKIP_DIRS
    if QUAR_NAME in SKIP_DIRS:
        return []
    return [("SKIP_DIRS", f"contém {QUAR_NAME}",
             "não contém: o scanner indexaria o que está saindo do disco")]


def main():
    bad = []
    for relpath, name, ext, want in CASES:
        got = classify(relpath, name, ext, BUILTIN)
        if got != want:
            bad.append((f"{relpath}/{name}", want, got))
    n = len(CASES)

    for relpath, name, ext, want, want_builtin in SPLICE_CASES:
        n += 2
        got = classify(relpath, name, ext, SPLICED)
        if got != want:
            bad.append((f"[com regra sua] {relpath}/{name}", want, got))
        got = classify(relpath, name, ext, BUILTIN)
        if got != want_builtin:
            bad.append((f"[só built-in] {relpath}/{name}", want_builtin, got))

    n += 1
    bad += quarentena_ignorada()

    # the live list must still be buildable - a broken rules.json must not
    # take classification down with it
    n += 1
    try:
        images.rules()
    except Exception as e:                                  # noqa: BLE001
        bad.append(("images.rules()", "compila", repr(e)))

    for path, want, got in bad:
        print(f"ERRO {path}\n     esperado {want}, obtido {got}")
    print(f"{n - len(bad)}/{n} corretos")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
