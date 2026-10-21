# SPDX-License-Identifier: GPL-3.0-or-later
"""Derive artist tags from the metadata already inside the audio files.

An artist is the rare case where a judgement is already written down: the file
says who made it, so the tag can be read instead of typed. Everything else in
this project that produces tags asks a person first, and this one still does --
it just asks once, about a list, instead of once per file.

Two things the measurement changed about the obvious design:

Only a minority of files carry the tag at all. In a sample of 120 here, 20 had
an artist: `.wav` is 56% of the collection and almost never has one (game and
system audio), while `.mp3` runs about 44%. So the report says how many files
were silent, instead of leaving the impression that the tagged set is the
collection.

The field is not clean. It holds "Unknown Artist", compilation names like
"TO LOCO 10", and the same name in several casings. Writing those straight into
the tag tree makes a mess that someone then has to undo by hand, and undoing is
the expensive direction -- so `NAO_ARTISTA` drops the known placeholders, and a
floor on the number of files keeps one-off junk out by default.
"""
import collections, os, shutil, subprocess, time
from concurrent.futures import ThreadPoolExecutor

from . import tags

FF_TIMEOUT = 12
MAE = "Artistas"

SCHEMA = """
CREATE TABLE IF NOT EXISTS audio_meta (
  file INTEGER PRIMARY KEY, artist TEXT, album TEXT, done_at INTEGER);
"""

# Placeholders that players and rippers write when nobody filled the field in.
# They are not artists, and each one would otherwise become a tag covering
# hundreds of unrelated files -- the opposite of what a tag is for.
NAO_ARTISTA = {
    "unknown artist", "unknown", "various artists", "various", "va",
    "artist", "n/a", "na", "none", "null", "undefined", "untitled",
    "desconhecido", "artista desconhecido", "sem artista", "track",
    "audiotrack", "no artist", "<unknown>", "-", "--", "?",
    # Gravadores de tela e de jogo escrevem o próprio nome no campo. É uma
    # lista de casos conhecidos, não uma regra: não há como deduzir que um nome
    # é software, e inventar uma heurística aqui recusaria artista de verdade.
    "microsoft game dvr", "game dvr", "nvidia shadowplay", "shadowplay",
    "obs studio", "obs", "bandicam", "fraps", "xbox game bar", "quicktime player",
}

CAMPOS = "format_tags=artist,album_artist,ARTIST,ALBUM_ARTIST,album"


def _valor(saida, *chaves):
    d = {}
    for linha in (saida or "").strip().splitlines():
        if "=" in linha:
            k, v = linha.split("=", 1)
            d[k.strip().lower()] = v.strip()
    for c in chaves:
        v = d.get("tag:" + c.lower())
        if v:
            return v
    return ""


def ler_artista(caminho):
    """(artist, album) as written in the file, or ('', '') when it says nothing.

    Returns empty strings rather than raising: a file that cannot be probed is
    a file without an artist for this purpose, and a run over tens of thousands
    of files should not stop because one of them is a broken download.
    """
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", CAMPOS,
                            "-of", "default=nw=1", caminho],
                           capture_output=True, text=True, timeout=FF_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return ("", "")
    art = _valor(r.stdout, "artist", "album_artist")
    alb = _valor(r.stdout, "album")
    return (art, alb)


def aceitavel(nome):
    n = " ".join((nome or "").split())
    if len(n) < 2:
        return ""
    if n.lower() in NAO_ARTISTA:
        return ""
    if n.replace(".", "").replace("-", "").isdigit():
        return ""              # "2003", "01" -- numeração de faixa no campo errado
    return n


def pendentes(con, cls="audio", limit=None, refresh=False):
    con.executescript(SCHEMA)
    q = ("SELECT f.id, v.mount, d.relpath, f.name FROM files f "
         "JOIN dirs d ON d.id=f.dir JOIN volumes v ON v.id=f.vol "
         "WHERE f.cls=? AND v.mount IS NOT NULL")
    args = [cls]
    if not refresh:
        q += " AND f.id NOT IN (SELECT file FROM audio_meta)"
    q += " ORDER BY f.size DESC"
    if limit:
        q += " LIMIT %d" % int(limit)
    out = []
    for r in con.execute(q, args):
        rel = ("%s/%s" % (r["relpath"], r["name"])) if r["relpath"] else r["name"]
        out.append((r["id"], os.path.join(r["mount"], rel)))
    return out


def varrer(con, cls="audio", limit=None, workers=6, refresh=False, progress=True):
    """Read the artist out of every audio file that has not been read yet.

    Commits in batches and skips what is already stored, so a run that is
    interrupted at minute nine of ten does not start over -- the same rule the
    thumbnail pass follows, for the same reason.
    """
    if not shutil.which("ffprobe"):
        raise RuntimeError(
            "ffprobe não encontrado. Ele é quem lê o metadado do arquivo; sem "
            "ele não há artista para ler. Instale o ffmpeg ou rode sem este "
            "comando -- nada mais do diskmap depende dele.")
    fila = pendentes(con, cls=cls, limit=limit, refresh=refresh)
    lidos = achados = 0
    t0 = time.time()
    lote = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for (fid, caminho), (art, alb) in zip(
                fila, ex.map(lambda x: ler_artista(x[1]), fila)):
            lidos += 1
            a = aceitavel(art)
            achados += bool(a)
            lote.append((fid, a or None, alb or None, int(time.time())))
            if len(lote) >= 200:
                con.executemany("INSERT OR REPLACE INTO audio_meta "
                                "(file,artist,album,done_at) VALUES (?,?,?,?)", lote)
                con.commit()
                lote = []
                if progress:
                    print("  %d/%d lidos · %d com artista · %.0fs"
                          % (lidos, len(fila), achados, time.time() - t0))
    if lote:
        con.executemany("INSERT OR REPLACE INTO audio_meta "
                        "(file,artist,album,done_at) VALUES (?,?,?,?)", lote)
        con.commit()
    return lidos, achados


def plano(con, cls="audio", minimo=2):
    """What would be tagged: artist -> list of absolute paths, biggest first.

    Separate from the writing on purpose. This is the list a person should see
    before several hundred tags appear in their tree, and the only way to show
    it is to build it without writing it.
    """
    con.executescript(SCHEMA)
    porart = collections.defaultdict(list)
    for r in con.execute(
            "SELECT am.artist, v.mount, d.relpath, f.name FROM audio_meta am "
            "JOIN files f ON f.id=am.file JOIN dirs d ON d.id=f.dir "
            "JOIN volumes v ON v.id=f.vol "
            "WHERE am.artist IS NOT NULL AND f.cls=? AND v.mount IS NOT NULL",
            (cls,)):
        # A lista de não-artistas vale também AQUI, não só na leitura. O que
        # está em `audio_meta` foi filtrado pela lista de quando foi lido;
        # acrescentar um nome a ela depois não pode exigir reler dezenas de
        # milhares de arquivos para surtir efeito.
        nome = aceitavel(r["artist"])
        if not nome:
            continue
        rel = ("%s/%s" % (r["relpath"], r["name"])) if r["relpath"] else r["name"]
        # Agrupa sem diferenciar maiúscula, porque a etiqueta também não
        # diferencia: "TO LOCO" e "to loco" são uma etiqueta só, e contá-las
        # como duas faria o piso recusar o que na verdade passa.
        porart[nome.casefold()].append((nome, os.path.join(r["mount"], rel)))
    # "Artista - Álbum" no campo do artista. Acontece em rip de CD inteiro, e
    # sem desfazer vira uma etiqueta por disco em vez de uma por artista --
    # justamente a fragmentação que etiquetar deveria evitar. A regra é
    # estreita de propósito: só dobra quando o prefixo JÁ É um artista do
    # próprio acervo, senão um nome com hífen ou "&" viraria picadinho.
    for chave in list(porart):
        for sep in (" - ", " – ", " / "):
            if sep not in chave:
                continue
            base = chave.split(sep)[0].strip()
            if base and base != chave and base in porart:
                # Reescreve o nome exibido junto com a mudança de balde. Sem
                # isto o nome sairia da maioria dos itens, e a etiqueta de um
                # artista com mais faixas de um disco que do resto ficaria
                # chamada pelo disco -- certo por acaso, errado quando o acaso
                # virasse. O nome canônico é o mais frequente do balde-destino.
                canon = collections.Counter(
                    n for n, _ in porart[base]).most_common(1)[0][0]
                porart[base].extend((canon, p) for _, p in porart.pop(chave))
            break
    saida = {}
    for _, itens in porart.items():
        nome = collections.Counter(n for n, _ in itens).most_common(1)[0][0]
        if len(itens) >= minimo:
            saida[nome] = [p for _, p in itens]
    descartados = sum(1 for _, i in porart.items() if len(i) < minimo)
    return saida, descartados


def aplicar(con, plano_dict, pai=MAE, progress=True):
    """Write the tags. Everything here is additive and reversible by hand."""
    tags.ensure(con)
    con.execute("INSERT OR IGNORE INTO tags (name, parent, created) VALUES (?,NULL,?)",
                (pai, int(time.time())))
    con.commit()
    criadas = marcadas = 0
    falhas = []
    for nome, caminhos in sorted(plano_dict.items(), key=lambda kv: -len(kv[1])):
        for i in range(0, len(caminhos), tags.MAX_LOTE):
            try:
                r = tags.marcar_varios(con, nome, caminhos[i:i + tags.MAX_LOTE],
                                       pai=pai)
                marcadas += r.get("feitos", 0)
            except Exception as e:
                falhas.append((nome, str(e)))
                break
        else:
            criadas += 1
        if progress and criadas % 50 == 0 and criadas:
            print("  %d etiquetas · %d caminhos" % (criadas, marcadas))
    return criadas, marcadas, falhas
