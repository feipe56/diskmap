# SPDX-License-Identifier: GPL-3.0-or-later
"""Local HTTP server: JSON API over the index, plus the static frontend.

Binds to 127.0.0.1 only. Read-only: no endpoint moves or deletes a file.
"""
import json, mimetypes, os, secrets, sqlite3, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from . import actions as actions_mod
from . import db
from . import jobs as jobs_mod
from . import tags as tags_mod
from .analyze import (BLOB_STALE_YEARS, keep_reason, keep_score,
                      overlap_files, recoverable)

RUNNER = None   # set by serve(); the job registry lives for the server's lifetime

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def overview(con):
    vols = [dict(r) for r in con.execute(
        "SELECT label, mount, capacity, free, files, bytes, scanned_at "
        "FROM volumes ORDER BY bytes DESC")]
    by, pervol, n, _, bdrop = recoverable(con)
    stats = {}
    for kind in ("class", "age", "ext", "junk"):
        stats[kind] = [dict(r) for r in con.execute(
            "SELECT key, bytes, files FROM stat_rollup WHERE kind=? "
            "ORDER BY bytes DESC", (kind,))]
    dup = con.execute(
        "SELECT COUNT(*) g, COALESCE(SUM(wasted),0) w FROM dup_groups").fetchone()
    ver = {r["verified"]: r["n"] for r in con.execute(
        "SELECT verified, COUNT(*) n FROM dup_groups GROUP BY verified")}
    blobs = [dict(r) for r in con.execute(
        "SELECT kind, SUM(bytes) bytes, SUM(files) files FROM blobs "
        "GROUP BY kind ORDER BY bytes DESC LIMIT 12")]
    br = {r["verdict"]: dict(r) for r in con.execute(
        "SELECT verdict, COUNT(*) dirs, COALESCE(SUM(bytes),0) bytes, "
        "COALESCE(SUM(files),0) files FROM blob_reclaim GROUP BY verdict")}
    return {
        "volumes": vols, "stats": stats, "blobs": blobs,
        "blob_reclaim": {"by_verdict": br,
                         "stale": {"dirs": len(bdrop),
                                   "bytes": sum(b[2] for b in bdrop),
                                   "years": BLOB_STALE_YEARS}},
        "recoverable": {"by": by, "pervol": pervol, "files": n,
                        "total": sum(by.values())},
        "dup": {"groups": dup["g"], "wasted": dup["w"], "verified": ver},
        "totals": {"files": sum(v["files"] or 0 for v in vols),
                   "bytes": sum(v["bytes"] or 0 for v in vols),
                   "capacity": sum(v["capacity"] or 0 for v in vols),
                   "free": sum(v["free"] or 0 for v in vols)},
    }


def dup_list(con, limit=60, offset=0, verified=None, min_size=0):
    q = ("SELECT id, name, size, copies, wasted, verified FROM dup_groups "
         "WHERE size >= ?")
    args = [min_size]
    if verified:
        q += " AND verified = ?"
        args.append(verified)
    q += " ORDER BY wasted DESC LIMIT ? OFFSET ?"
    args += [limit, offset]
    out = []
    for g in con.execute(q, args):
        paths = [f"{r['label']}/{r['relpath']}/{r['name']}".replace("//", "/")
                 for r in con.execute(
                     "SELECT v.label, d.relpath, f.name FROM dup_members m "
                     "JOIN files f ON f.id=m.file JOIN dirs d ON d.id=f.dir "
                     "JOIN volumes v ON v.id=f.vol WHERE m.grp=? LIMIT 12", (g["id"],))]
        out.append({**dict(g), "paths": paths})
    return out


def overlap(con, limit=40):
    return [dict(r) for r in con.execute(
        "SELECT a, b, shared, files, a_bytes, b_bytes, pct FROM tree_overlap "
        "ORDER BY shared DESC LIMIT ?", (limit,))]


# A aba Acervo: a mesma pergunta de sempre ("o que eu tenho?") feita por tipo.
# Imagens continuam vindo de `image_meta`, que sabe dimensão, biblioteca e hash
# visual; o resto vem de `media_meta`, que só sabe mostrar. Uma consulta só
# devolveria metade das colunas vazias para cada lado.
TIPOS = {"imagem": ("image",), "documento": ("doc",), "video": ("video",),
         "outro": ("audio", "archive", "code", "model3d", "binary",
                   "gamedata", "ml_model", "db")}


def acervo(con, q):
    from . import acervo as ac
    tipo = (q.get("tipo") or ["imagem"])[0]
    classes = TIPOS.get(tipo)
    if not classes:
        return {"erro": "tipo desconhecido: %s" % tipo}
    con.executescript(ac.SCHEMA)
    # As extensões que algum renderizador alcança. Sem isto o painel mostra o
    # mesmo ícone para "ainda não processei" e para "não existe como processar",
    # e as duas pedem coisas diferentes de quem está olhando: uma pede paciência,
    # a outra não pede nada.
    render = sorted(ac.QL_EXTS | (ac.FF_EXTS if ac.have_ffmpeg() else set()))
    lim = max(1, min(int((q.get("limit") or ["120"])[0]), 500))
    off = max(0, int((q.get("offset") or ["0"])[0]))
    ordem = {"size": "f.size DESC", "recent": "f.mtime DESC",
             "old": "f.mtime ASC", "name": "f.name COLLATE NOCASE"}.get(
                 (q.get("sort") or ["size"])[0], "f.size DESC")
    where = ["f.cls IN (%s)" % ",".join("?" * len(classes))]
    args = list(classes)
    vol = (q.get("vol") or [""])[0]
    if vol:
        where.append("v.label = ?")
        args.append(vol)
    ext = (q.get("ext") or [""])[0]
    if ext:
        where.append("f.ext = ?")
        args.append(ext.lower().lstrip("."))
    minb = int((q.get("min") or ["0"])[0])
    if minb:
        where.append("f.size >= ?")
        args.append(minb)
    if (q.get("comthumb") or [""])[0] == "1":
        where.append("(im.thumb IS NOT NULL OR mm.thumb IS NOT NULL)")
    # Etiqueta é eixo transversal: ela corta o acervo do mesmo jeito que corta a
    # busca. Sem isto ela só existiria num painel, e um filtro que vale em um
    # lugar e não no outro é um filtro em que ninguém confia.
    for nome in (q.get("tag") or []):
        sql, a = tags_mod.clausula(con, [nome])
        where.append(sql)
        args += a
    rows = []
    for r in con.execute(
            "SELECT f.id, f.name, f.size, f.mtime, f.ext, f.cls, v.label, "
            "v.mount, d.relpath, im.thumb AS ithumb, im.width, im.height, "
            "im.lib, mm.thumb AS mthumb, mm.engine, mm.note "
            "FROM files f JOIN dirs d ON d.id=f.dir JOIN volumes v ON v.id=f.vol "
            "LEFT JOIN image_meta im ON im.file=f.id "
            "LEFT JOIN media_meta mm ON mm.file=f.id "
            "WHERE " + " AND ".join(where) +
            " ORDER BY " + ordem + " LIMIT ? OFFSET ?", args + [lim, off]):
        rel = ("%s/%s" % (r["relpath"], r["name"])) if r["relpath"] else r["name"]
        rows.append({
            "id": r["id"], "name": r["name"], "size": r["size"],
            "mtime": r["mtime"], "ext": r["ext"], "cls": r["cls"],
            "label": r["label"], "rel": rel,
            "abs": os.path.join(r["mount"], rel) if r["mount"] else None,
            "thumb": bool(r["ithumb"] or r["mthumb"]),
            # 'image' tem pipeline próprio; para o resto, quem decide é a lista
            "pode": (r["cls"] == "image"
                     or (r["ext"] or "").lower() in render),
            "width": r["width"], "height": r["height"], "lib": r["lib"],
            "note": r["note"],
        })
    tot, bts = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(f.size),0) FROM files f "
        "JOIN volumes v ON v.id=f.vol WHERE f.cls IN (%s)"
        % ",".join("?" * len(classes)), list(classes)).fetchone()
    # Quantos sobram sob os filtros ATUAIS: é esse número que diz quantas
    # páginas existem. Paginar sobre o total da classe faria o botão "próximas"
    # prometer páginas que vêm vazias.
    filtrado, fbytes = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(f.size),0) FROM files f "
        "JOIN dirs d ON d.id=f.dir JOIN volumes v ON v.id=f.vol "
        "LEFT JOIN image_meta im ON im.file=f.id "
        "LEFT JOIN media_meta mm ON mm.file=f.id "
        "WHERE " + " AND ".join(where), args).fetchone()
    exts = [dict(r) for r in con.execute(
        "SELECT f.ext, COUNT(*) n FROM files f WHERE f.cls IN (%s) AND f.ext <> '' "
        "GROUP BY f.ext ORDER BY n DESC LIMIT 14" % ",".join("?" * len(classes)),
        list(classes))]
    vols = [r[0] for r in con.execute(
        "SELECT DISTINCT v.label FROM files f JOIN volumes v ON v.id=f.vol "
        "WHERE f.cls IN (%s) ORDER BY v.label" % ",".join("?" * len(classes)),
        list(classes))]
    # Quantos dos que faltam são alcançáveis: é esse o número que vale como
    # "gere o resto", e não o total sem miniatura.
    if "image" in classes:
        faltam = con.execute(
            "SELECT COUNT(*) FROM files f LEFT JOIN image_meta im ON im.file=f.id "
            "WHERE f.cls='image' AND im.thumb IS NULL").fetchone()[0]
    else:
        faltam = con.execute(
            "SELECT COUNT(*) FROM files f "
            "LEFT JOIN media_meta mm ON mm.file=f.id "
            "WHERE f.cls IN (%s) AND mm.thumb IS NULL AND lower(f.ext) IN (%s)"
            % (",".join("?" * len(classes)), ",".join("?" * len(render))),
            list(classes) + render).fetchone()[0]
    comthumb = con.execute(
        "SELECT COUNT(*) FROM files f LEFT JOIN image_meta im ON im.file=f.id "
        "LEFT JOIN media_meta mm ON mm.file=f.id WHERE f.cls IN (%s) "
        "AND (im.thumb IS NOT NULL OR mm.thumb IS NOT NULL)"
        % ",".join("?" * len(classes)), list(classes)).fetchone()[0]
    return {"tipo": tipo, "rows": rows, "total": tot, "bytes": bts,
            "filtrado": filtrado, "bytes_filtrado": fbytes, "faltam": faltam,
            "com_thumb": comthumb, "exts": exts, "vols": vols,
            "ffmpeg": ac.have_ffmpeg()}


def acervo_contagem(con, q):
    """Quantos arquivos cada sub-aba tem sob as etiquetas ativas.

    Existe para o botão "ver no acervo" não cair numa aba vazia, e para a
    própria sub-aba poder dizer quanto tem sob o filtro. Uma consulta só, com
    GROUP BY: quatro chamadas separadas seriam quatro varreduras do mesmo
    conjunto para responder a mesma pergunta.
    """
    onde, args = ["f.cls IS NOT NULL"], []
    for nome in (q.get("tag") or []):
        sql, a = tags_mod.clausula(con, [nome])
        onde.append(sql)
        args += a
    porcls = {}
    for r in con.execute(
            "SELECT f.cls, COUNT(*) n FROM files f JOIN dirs d ON d.id=f.dir "
            "JOIN volumes v ON v.id=f.vol WHERE " + " AND ".join(onde) +
            " GROUP BY f.cls", args):
        porcls[r["cls"]] = r["n"]
    out = {}
    for tipo, classes in TIPOS.items():
        out[tipo] = sum(porcls.get(c, 0) for c in classes)
    return {"contagem": out, "tags": q.get("tag") or []}


def dup_detail(con, q):
    """Every copy in one duplicate group, best survivor first.

    The list view shows at most four paths and no way to act on them. This is
    what the group actually is: each copy with its volume, size, date, and the
    reason `keep_score` would or would not elect it to stay.
    """
    try:
        grp = int((q.get("grp") or ["0"])[0])
    except ValueError:
        return {"erro": "grupo inválido"}
    g = con.execute("SELECT id, name, size, copies, wasted, verified "
                    "FROM dup_groups WHERE id=?", (grp,)).fetchone()
    if not g:
        return {"erro": "grupo não encontrado"}
    membros = []
    for r in con.execute(
            "SELECT f.id, v.label, v.mount, d.relpath, f.name, f.size, f.mtime "
            "FROM dup_members m JOIN files f ON f.id=m.file "
            "JOIN dirs d ON d.id=f.dir JOIN volumes v ON v.id=f.vol "
            "WHERE m.grp=?", (grp,)):
        rel = ("%s/%s" % (r["relpath"], r["name"])) if r["relpath"] else r["name"]
        completo = "%s/%s" % (r["label"], rel)
        caminho = os.path.join(r["mount"], rel) if r["mount"] else None
        membros.append({
            "id": r["id"], "label": r["label"], "rel": rel, "full": completo,
            "abs": caminho,
            # O índice é uma fotografia: entre a varredura e agora o arquivo pode
            # ter sido apagado por fora. Quem não está mais lá não pode ser
            # eleito sobrevivente nem contado como cópia existente, e a tela tem
            # de dizer isso em vez de falhar depois com "inacessível".
            "exists": bool(caminho and os.path.isfile(caminho)),
            "size": r["size"], "mtime": r["mtime"],
            "reason": keep_reason(completo), "score": keep_score(completo),
        })
    # Ausente no disco vai para o fim: não serve de sobrevivente.
    membros.sort(key=lambda m: (not m["exists"], m["score"]))
    for m in membros:
        m.pop("score")
    vivos = sum(1 for m in membros if m["exists"])
    return {**dict(g), "membros": membros, "vivos": vivos,
            "sumidos": len(membros) - vivos}


def overlap_detail(con, q):
    """The file list behind one pair of cloned trees.

    Read-only like every other GET here. The pair comes in as the two tree
    labels, which is what /api/overlap already handed the frontend -- no new
    identifier to keep in sync.
    """
    a = (q.get("a") or [""])[0]
    b = (q.get("b") or [""])[0]
    if not a or not b:
        return {"erro": "faltam as duas árvores"}
    lim = max(1, min(int((q.get("limit") or ["400"])[0]), 2000))
    return overlap_files(con, a, b, lim)


def archives(con, limit=40):
    return [dict(r) for r in con.execute(
        "SELECT p.relpath, p.bytes, v.label FROM archive_pairs p "
        "JOIN volumes v ON v.id=p.vol ORDER BY p.bytes DESC LIMIT ?", (limit,))]


def dirs(con, vol=None, depth=1, limit=50):
    q = ("SELECT v.label, r.relpath, r.bytes, r.files FROM dir_rollup r "
         "JOIN volumes v ON v.id=r.vol WHERE r.depth = ?")
    args = [depth]
    if vol:
        q += " AND v.label = ?"
        args.append(vol)
    q += " ORDER BY r.bytes DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in con.execute(q, args)]


def search(con, term, limit=200, etiquetas=()):
    """Find a file across every volume, including ones not plugged in.

    Tags narrow the result instead of replacing it: a tag with an empty term
    lists everything under it, and a tag with a term searches inside it. Two
    tags narrow further -- the file has to carry both -- because that is what
    stacking filters means to the person stacking them.
    """
    from . import tags as tg
    like = f"%{term}%"
    onde = ["f.name LIKE ? COLLATE NOCASE"] if term else ["1"]
    args = [like] if term else []
    for nome in etiquetas:
        sql, a = tg.clausula(con, [nome])
        onde.append(sql)
        args += a
    rows = [dict(r) for r in con.execute(
        "SELECT v.label, d.relpath, f.name, f.size, f.mtime, f.ext, f.id "
        "FROM files f JOIN dirs d ON d.id=f.dir JOIN volumes v ON v.id=f.vol "
        "WHERE " + " AND ".join(onde) +
        " ORDER BY f.size DESC LIMIT ?", args + [limit])]
    dirs_ = []
    if term and not etiquetas:
        dirs_ = [dict(r) for r in con.execute(
            "SELECT v.label, r.relpath, r.bytes, r.files FROM dir_rollup r "
            "JOIN volumes v ON v.id=r.vol WHERE r.relpath LIKE ? COLLATE NOCASE "
            "ORDER BY r.bytes DESC LIMIT 40", (like,))]
    bytes_ = sum(r["size"] for r in rows)
    return {"files": rows, "dirs": dirs_, "tags": list(etiquetas),
            "bytes": bytes_, "truncado": len(rows) >= limit}


def tag_list(con):
    from . import tags as tg
    return {"tags": tg.listar(con)}


def biggest(con, limit=60):
    return [dict(r) for r in con.execute(
        "SELECT v.label, d.relpath, f.name, f.size, f.mtime FROM files f "
        "JOIN dirs d ON d.id=f.dir JOIN volumes v ON v.id=f.vol "
        "ORDER BY f.size DESC LIMIT ?", (limit,))]


def cold(con, min_size=1_000_000_000, years=2, limit=60):
    import time
    cutoff = int(time.time() - years * 365 * 86400)
    return [dict(r) for r in con.execute(
        "SELECT v.label, d.relpath, f.name, f.size, f.mtime FROM files f "
        "JOIN dirs d ON d.id=f.dir JOIN volumes v ON v.id=f.vol "
        "WHERE f.size >= ? AND f.mtime > 0 AND f.mtime < ? "
        "ORDER BY f.size DESC LIMIT ?", (min_size, cutoff, limit))]


def action_list(con, allow, allow_wipe=False):
    return {"allowed": allow, "allow_wipe": bool(allow_wipe),
            "actions": actions_mod.available(allow_wipe=allow_wipe) if allow else []}


def img_overview(con, q=None):
    from . import images as im
    partes, args = [], []
    for nome in ((q or {}).get("tag") or []):
        sql, a = tags_mod.clausula(con, [nome])
        partes.append(sql)
        args += a
    d = im.stats(con, " AND ".join(partes), args)
    d["tags"] = (q or {}).get("tag") or []
    return d


def img_list(con, q):
    """Gallery page: one row per image, newest filter wins."""
    from . import images as im
    where, args = ["m.thumb IS NOT NULL"], []
    lib = (q.get("lib") or [None])[0]
    if lib and lib != "all":
        where.append("m.lib = ?"); args.append(lib)
    vol = (q.get("vol") or [None])[0]
    if vol and vol != "all":
        where.append("v.label = ?"); args.append(vol)
    fmt = (q.get("format") or [None])[0]
    if fmt and fmt != "all":
        where.append("f.ext = ?"); args.append(fmt)
    minpx = int(q.get("minpx", ["0"])[0])
    if minpx:
        where.append("m.width >= ? AND m.height >= ?"); args += [minpx, minpx]
    term = (q.get("q") or [""])[0].strip()
    if term:
        where.append("(f.name LIKE ? COLLATE NOCASE OR d.relpath LIKE ? COLLATE NOCASE)")
        args += [f"%{term}%", f"%{term}%"]
    # A galeria de imagens tem fonte própria, e por isso ela precisa aplicar a
    # etiqueta por conta: sem isto o cabeçalho da sub-aba diz "Imagens 0" e a
    # grade embaixo continua mostrando o acervo inteiro. Uma tela que se
    # contradiz é pior que uma tela sem o filtro.
    for nome in (q.get("tag") or []):
        sql, a = tags_mod.clausula(con, [nome])
        where.append(sql)
        args += a
    order = {"size": "f.size DESC", "px": "(m.width*m.height) DESC",
             "new": "f.mtime DESC", "old": "f.mtime ASC",
             "name": "f.name COLLATE NOCASE"}.get(
                 (q.get("sort") or ["size"])[0], "f.size DESC")
    limit = min(int(q.get("limit", ["120"])[0]), 500)
    offset = int(q.get("offset", ["0"])[0])
    sql = f"""SELECT f.id, f.name, f.size, f.mtime, f.ext, d.relpath, v.label, v.mount,
                     m.width, m.height, m.lib, m.dhash
              FROM image_meta m JOIN files f ON f.id=m.file
              JOIN dirs d ON d.id=f.dir JOIN volumes v ON v.id=f.vol
              WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ? OFFSET ?"""
    rows = [dict(r) for r in con.execute(sql, (*args, limit, offset))]
    cnt = con.execute(f"""SELECT COUNT(*) n, SUM(f.size) b FROM image_meta m
              JOIN files f ON f.id=m.file JOIN dirs d ON d.id=f.dir
              JOIN volumes v ON v.id=f.vol WHERE {' AND '.join(where)}""",
                      args).fetchone()
    return {"images": rows, "total": cnt["n"], "bytes": cnt["b"] or 0,
            "libs": im.LIB_LABEL}


def img_groups(con, q):
    """Visually similar clusters, biggest waste first.

    Under a tag, a cluster only shows if at least one of its members carries
    the tag -- and then the whole cluster shows, members outside the tag
    included. Hiding those would be worse than useless: the point of a cluster
    is "these N are the same picture", and a cluster with members removed
    invites deleting the last copy you could see.
    """
    limit = min(int(q.get("limit", ["30"])[0]), 120)
    offset = int(q.get("offset", ["0"])[0])
    etiquetas = q.get("tag") or []
    filtro = ""
    fargs = []
    if etiquetas:
        partes = []
        for nome in etiquetas:
            sql, a = tags_mod.clausula(con, [nome])
            partes.append(sql)
            fargs += a
        filtro = (" AND id IN (SELECT im2.grp FROM img_members im2 "
                  "JOIN files f ON f.id=im2.file JOIN dirs d ON d.id=f.dir "
                  "JOIN volumes v ON v.id=f.vol WHERE " +
                  " AND ".join(partes) + ")")
    out = []
    for g in con.execute("SELECT id, n, bytes, wasted FROM img_groups "
                         "WHERE 1" + filtro +
                         " ORDER BY wasted DESC LIMIT ? OFFSET ?",
                         fargs + [limit, offset]):
        members = [dict(r) for r in con.execute(
            """SELECT f.id, f.name, f.size, f.ext, d.relpath, v.label, v.mount,
                      m.width, m.height, m.lib, im2.dist
               FROM img_members im2 JOIN files f ON f.id=im2.file
               JOIN image_meta m ON m.file=f.id JOIN dirs d ON d.id=f.dir
               JOIN volumes v ON v.id=f.vol WHERE im2.grp=?
               ORDER BY f.size DESC""", (g["id"],))]
        out.append({**dict(g), "members": members})
    tot = con.execute("SELECT COUNT(*) n, SUM(wasted) w FROM img_groups WHERE 1"
                      + filtro, fargs).fetchone()
    return {"groups": out, "count": tot["n"] or 0, "wasted": tot["w"] or 0,
            "tags": etiquetas}


def blob_list(con, q):
    """Aggregated dirs with their verdict, for the consolidated panel.

    `verdict` filters; `min_years` keeps only the ones whose project has been
    quiet that long. Read-only: the row carries the path, and acting on it goes
    through the same action layer as everything else.
    """
    import time as _t
    verdict = (q.get("verdict") or [None])[0]
    min_years = (q.get("min_years") or [None])[0]
    limit = min(int(q.get("limit", ["80"])[0]), 500)
    sql = ("SELECT b.blob, b.kind, b.relpath, b.verdict, b.proof, b.last, "
           "b.bytes, b.files, v.label, v.mount FROM blob_reclaim b "
           "JOIN volumes v ON v.id=b.vol")
    args, where = [], []
    if verdict in ("cache", "manifesto", "sem-prova", "vcs"):
        where.append("b.verdict = ?")
        args.append(verdict)
    if min_years:
        try:
            cut = _t.time() - float(min_years) * 365 * 86400
        except ValueError:
            cut = None
        if cut is not None:
            where.append("b.last IS NOT NULL AND b.last <= ?")
            args.append(cut)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY b.bytes DESC LIMIT ?"
    args.append(limit)
    now = _t.time()
    out = []
    for r in con.execute(sql, args):
        d = dict(r)
        d["years"] = round((now - r["last"]) / (365 * 86400), 1) if r["last"] else None
        d["path"] = os.path.join(r["mount"], r["relpath"]) if r["mount"] else None
        d["offerable"] = r["verdict"] in ("cache", "manifesto")
        out.append(d)
    return {"rows": out, "stale_years": BLOB_STALE_YEARS}


def _health(con):
    """SMART por disco. Leitura pura: só consulta diskutil e smartctl."""
    from . import health
    return health.report(con)


ROUTES = {
    "/api/overview": lambda con, q: overview(con),
    "/api/dups":     lambda con, q: dup_list(con, int(q.get("limit", ["60"])[0]),
                                             int(q.get("offset", ["0"])[0]),
                                             (q.get("verified") or [None])[0],
                                             int(q.get("min_size", ["0"])[0])),
    "/api/overlap":  lambda con, q: overlap(con, int(q.get("limit", ["40"])[0])),
    "/api/overlap-files": lambda con, q: overlap_detail(con, q),
    "/api/dup-members": lambda con, q: dup_detail(con, q),
    "/api/acervo":     lambda con, q: acervo(con, q),
    "/api/acervo-contagem": lambda con, q: acervo_contagem(con, q),
    "/api/archives": lambda con, q: archives(con, int(q.get("limit", ["40"])[0])),
    "/api/dirs":     lambda con, q: dirs(con, (q.get("vol") or [None])[0],
                                         int(q.get("depth", ["1"])[0]),
                                         int(q.get("limit", ["50"])[0])),
    "/api/tags":     lambda con, q: tag_list(con),
    "/api/path-tags": lambda con, q: {"tags": tags_mod.do_caminho(
        con, (q.get("path") or [""])[0])},
    "/api/search":   lambda con, q: search(con, (q.get("q") or [""])[0],
                                           int(q.get("limit", ["200"])[0]),
                                           q.get("tag") or []),
    "/api/biggest":  lambda con, q: biggest(con, int(q.get("limit", ["60"])[0])),
    "/api/cold":     lambda con, q: cold(con, limit=int(q.get("limit", ["60"])[0])),
    "/api/images":   lambda con, q: img_list(con, q),
    "/api/img-overview": lambda con, q: img_overview(con, q),
    "/api/img-groups":   lambda con, q: img_groups(con, q),
    "/api/blobs":    lambda con, q: blob_list(con, q),
    "/api/health":   lambda con, q: _health(con),
}


def make_handler(db_path, token, allow_actions, origins, allow_wipe=False):
    from . import iowatch as iowatch_mod
    # Um amostrador por servidor, não por requisição: a janela de I/O é tempo de
    # relógio, e dois navegadores abertos não podem virar dois `ps` por segundo.
    watcher = iowatch_mod.Watcher(db_path)
    class Handler(BaseHTTPRequestHandler):
        server_version = "diskmap"

        def log_message(self, fmt, *args):
            pass  # keep the console for scan progress, not request noise

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _origin_ok(self):
            """Block CSRF and DNS rebinding: the page must be served from here."""
            host = (self.headers.get("Host") or "").lower()
            if host not in origins:
                return False
            origin = self.headers.get("Origin")
            if origin and origin.lower().rstrip("/") not in {f"http://{o}" for o in origins}:
                return False
            return True

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            if not self._origin_ok():
                self._send(403, b'{"error":"origem nao permitida"}')
                return
            if not allow_actions:
                self._send(403, json.dumps({"error":
                    "acoes desligadas. Suba com: diskmap serve --allow-actions"
                    }).encode())
                return
            if self.headers.get("X-Diskmap-Token") != token:
                self._send(403, b'{"error":"token invalido"}')
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, OSError) as e:
                self._send(400, json.dumps({"error": f"corpo invalido: {e}"}).encode())
                return
            # job endpoints do not touch the index from this process
            try:
                if u.path == "/api/job":
                    job = RUNNER.start(str(body.get("kind", "")),
                                       body.get("params") or {})
                    self._send(200, json.dumps(job.info(), ensure_ascii=False).encode())
                    return
                if u.path == "/api/job/cancel":
                    res = RUNNER.cancel(int(body.get("id")))
                    self._send(200, json.dumps(res, ensure_ascii=False).encode())
                    return
            except ValueError as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode())
                return
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False).encode())
                return

            con = db.connect(db_path, create=False)
            try:
                if u.path == "/api/action":
                    res = actions_mod.run(con, str(body.get("id", "")),
                                          str(body.get("path", "")),
                                          allow_wipe=allow_wipe)
                elif u.path == "/api/compare-hash":
                    # POST, not GET: it reads gigabytes off the disk, and that
                    # is work a page should not be able to start just by being
                    # open. Behind the token like every other write-ish call.
                    res = actions_mod.compare_pair(con, str(body.get("a", "")),
                                                   str(body.get("b", "")))
                elif u.path == "/api/tag":
                    acao = str(body.get("acao") or "add")
                    nome = str(body.get("name") or "")
                    if acao == "add":
                        res = tags_mod.marcar(con, nome, str(body.get("path") or ""),
                                              (body.get("pai") or None))
                    elif acao == "remove":
                        res = tags_mod.desmarcar(con, nome, str(body.get("path") or ""))
                    elif acao == "rename":
                        res = tags_mod.renomear(con, nome, str(body.get("novo") or ""))
                    elif acao == "delete":
                        res = tags_mod.apagar(con, nome)
                    else:
                        raise ValueError("ação desconhecida: %s" % acao)
                elif u.path == "/api/tag-bulk":
                    res = tags_mod.marcar_varios(
                        con, str(body.get("name") or ""),
                        body.get("paths") or [], (body.get("pai") or None),
                        str(body.get("acao") or "add"))
                elif u.path == "/api/discard-group":
                    res = actions_mod.discard_group(
                        con, int(body.get("grp") or 0), str(body.get("keep", "")))
                elif u.path == "/api/verify-group":
                    res = actions_mod.verify_group(con, int(body.get("grp") or 0),
                                                   str(body.get("stage") or "full"))
                elif u.path == "/api/discard-clone":
                    res = actions_mod.discard_one(con, str(body.get("path", "")),
                                                  (body.get("keep") or None),
                                                  (body.get("expect") or None))
                elif u.path == "/api/resolve-clone":
                    res = actions_mod.resolve_clone(
                        con, str(body.get("src", "")), str(body.get("dst", "")),
                        (body.get("expect") or None))
                elif u.path == "/api/undo":
                    res = actions_mod.undo(con, int(body.get("log_id")))
                else:
                    self._send(404, b'{"error":"not found"}')
                    return
                self._send(200, json.dumps(res, ensure_ascii=False).encode())
            except ValueError as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode())
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False).encode())
            finally:
                con.close()

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if not self._origin_ok():
                self._send(403, b'{"error":"origem nao permitida"}')
                return
            if u.path == "/api/wipe-check":
                # read-only: says what a wipe would be worth here, before the
                # user is asked to confirm one. Never writes anything.
                if not allow_wipe:
                    self._send(403, b'{"error":"zerar desligado"}')
                    return
                from . import wipe as wipe_mod
                con = db.connect(db_path, create=False)
                try:
                    p = (q.get("path") or [""])[0]
                    ap, kind, label, mount = actions_mod.resolve_target(con, p)
                    av = wipe_mod.assess(mount)
                    av["path"] = ap
                    av["label"] = label
                    av["refusals"] = (["zerar só vale para arquivo, não para pasta"]
                                      if kind != "file" else wipe_mod.check(ap))
                    av["size"] = os.path.getsize(ap) if kind == "file" else None
                    self._send(200, json.dumps(av, ensure_ascii=False).encode())
                except (ValueError, OSError) as e:
                    self._send(400, json.dumps({"error": str(e)},
                                               ensure_ascii=False).encode())
                finally:
                    con.close()
                return
            if u.path == "/api/io":
                r = watcher.latest()
                self._send(200, json.dumps(
                    r or {"warming": True, "interval": watcher.interval},
                    ensure_ascii=False).encode())
                return
            if u.path == "/api/actions":
                self._send(200, json.dumps(
                    action_list(None, allow_actions, allow_wipe),
                    ensure_ascii=False).encode())
                return
            if u.path == "/api/log":
                con = db.connect(db_path, create=False)
                try:
                    self._send(200, json.dumps(
                        actions_mod.log(con, int(q.get("limit", ["100"])[0])),
                        ensure_ascii=False).encode())
                finally:
                    con.close()
                return
            if u.path == "/api/jobs":
                self._send(200, json.dumps({
                    "allowed": bool(allow_actions),
                    "jobs": RUNNER.list(int(q.get("limit", ["30"])[0])) if RUNNER else [],
                }, ensure_ascii=False).encode())
                return
            if u.path == "/api/job":
                try:
                    res = RUNNER.get(int(q.get("id", ["0"])[0]),
                                     int(q.get("since", ["0"])[0]))
                    self._send(200, json.dumps(res, ensure_ascii=False).encode())
                except ValueError as e:
                    self._send(404, json.dumps({"error": str(e)},
                                               ensure_ascii=False).encode())
                return
            if u.path == "/api/mounts":
                con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                con.row_factory = sqlite3.Row
                try:
                    self._send(200, json.dumps(
                        jobs_mod.discover_volumes(con), ensure_ascii=False).encode())
                finally:
                    con.close()
                return
            if u.path in ROUTES:
                con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                con.row_factory = sqlite3.Row
                try:
                    data = ROUTES[u.path](con, q)
                    self._send(200, json.dumps(data, ensure_ascii=False).encode())
                except Exception as e:
                    self._send(500, json.dumps({"error": str(e)}).encode())
                finally:
                    con.close()
                return
            if u.path.startswith("/thumb/"):
                # thumbnails live outside WEB; serve them by file id only, never
                # by a path the caller supplies
                try:
                    fid = int(u.path[len("/thumb/"):].split(".")[0])
                except ValueError:
                    self._send(404, b'{"error":"not found"}')
                    return
                con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                con.row_factory = sqlite3.Row
                try:
                    r = con.execute("SELECT thumb FROM image_meta WHERE file=?",
                                    (fid,)).fetchone()
                    if not r or not r["thumb"]:
                        # imagens vêm de image_meta, o resto de media_meta: a
                        # rota serve as duas pelo mesmo id de arquivo
                        try:
                            r = con.execute(
                                "SELECT thumb FROM media_meta WHERE file=?",
                                (fid,)).fetchone()
                        except sqlite3.OperationalError:
                            r = None      # índice antigo, sem a tabela ainda
                finally:
                    con.close()
                if not r or not r["thumb"]:
                    self._send(404, b'{"error":"sem miniatura"}')
                    return
                from . import images as im
                tp = os.path.normpath(os.path.join(im.THUMB_DIR, r["thumb"]))
                if not tp.startswith(os.path.abspath(im.THUMB_DIR)) or not os.path.isfile(tp):
                    self._send(404, b'{"error":"miniatura ausente"}')
                    return
                with open(tp, "rb") as fh:
                    blob = fh.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(blob)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(blob)
                return

            rel = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
            target = os.path.normpath(os.path.join(WEB, rel))
            if not target.startswith(WEB) or not os.path.isfile(target):
                self._send(404, b'{"error":"not found"}')
                return
            ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
            with open(target, "rb") as f:
                blob = f.read()
            # O front é lido do disco a cada pedido e serve só a esta máquina, então
            # cache aqui não economiza nada e esconde correção: sem isto, um app.js
            # corrigido continua velho no navegador até o usuário forçar recarga.
            self.send_response(200)
            self.send_header("Cache-Control", "no-store")
            if target.endswith("index.html"):
                blob = blob.replace(
                    b"</head>",
                    (f'<script>window.DISKMAP_TOKEN={json.dumps(token)};'
                     f'window.DISKMAP_ACTIONS={json.dumps(bool(allow_actions))};'
                     f'window.DISKMAP_WIPE={json.dumps(bool(allow_wipe))};'
                     f'</script></head>').encode())
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)

    return Handler


def serve(db_path, host="127.0.0.1", port=8777, allow_actions=False,
          allow_wipe=False):
    global RUNNER
    RUNNER = jobs_mod.Runner(os.path.abspath(db_path))
    token = secrets.token_urlsafe(24)
    origins = {f"{host}:{port}", f"127.0.0.1:{port}", f"localhost:{port}"}
    httpd = ThreadingHTTPServer((host, port),
                                make_handler(db_path, token, allow_actions,
                                             origins, allow_wipe))
    print(f"diskmap em http://{host}:{port}  (ctrl-c para parar)", flush=True)
    if allow_actions:
        cfg = actions_mod.load_config()
        n = len(cfg.get("actions", []))
        print(f"  ações LIGADAS · {n} do actions.json + {len(actions_mod.BUILTIN)} embutidas", flush=True)
        print(f"  config: {actions_mod.CONFIG}", flush=True)
        if allow_wipe:
            print("  ZERAR LIGADO: 'Zerar e apagar' sobrescreve e remove, SEM UNDO.", flush=True)
        else:
            print("  remover = Lixeira ou quarentena, com log e undo. Nada de rm.", flush=True)
        print("  comandos (scan, analyze, verify, plan) rodam na aba Tarefas", flush=True)
    else:
        print("  somente leitura. Para agir nos arquivos e rodar comandos: --allow-actions", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nparando")
    finally:
        running = [j for j in RUNNER.list() if j["state"] == "rodando"]
        if running:
            print(f"encerrando {len(running)} tarefa(s) em andamento")
            RUNNER.shutdown()
        httpd.server_close()
