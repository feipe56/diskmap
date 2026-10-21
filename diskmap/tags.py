# SPDX-License-Identifier: GPL-3.0-or-later
"""User-authored tags: the one thing in this index that cannot be recomputed.

Everything else here is derived. Delete `dup_groups` and `analyze` rebuilds it;
delete the thumbnails and the renderers make them again; even the sha256 can be
paid for a second time with enough hours. A tag is a judgement someone made --
"this is music", "this is work" -- and nothing on the disk implies it.

So it is stored differently from everything else, and the difference is the
whole point:

  keyed by PATH, never by file id. `files.id` is an INTEGER PRIMARY KEY, and a
  rescan deletes the rows and hands the freed rowids back out in walk order, so
  anything keyed by id silently re-attaches to a different file. That is a
  nuisance for a hash and a catastrophe for a tag: nobody notices their archive
  is labelled wrong.

  keyed by the volume LABEL, not the volume id, so a disk that is forgotten and
  re-added keeps its tags.

  a tag on a directory covers everything under it, resolved at query time. A
  tag stored per file would go stale the moment a file is added to the folder,
  and the point of tagging a folder is to say something about the folder.
"""
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS tags (
  id      INTEGER PRIMARY KEY,
  name    TEXT NOT NULL UNIQUE COLLATE NOCASE,
  parent  INTEGER REFERENCES tags(id),
  created INTEGER
);
CREATE TABLE IF NOT EXISTS tag_paths (
  tag     INTEGER NOT NULL REFERENCES tags(id),
  label   TEXT NOT NULL,        -- volume label; a forgotten disk keeps its tags
  relpath TEXT NOT NULL,        -- path inside the volume, '' is the root
  is_dir  INTEGER NOT NULL DEFAULT 0,
  added   INTEGER,
  PRIMARY KEY (tag, label, relpath)
);
CREATE INDEX IF NOT EXISTS tp_tag ON tag_paths(tag);
"""

MAX_NOME = 48


def ensure(con):
    con.executescript(SCHEMA)
    # Índice que já existia antes das sub-etiquetas não ganha coluna por
    # CREATE TABLE IF NOT EXISTS. Acrescentar é barato e não toca os dados.
    cols = {r[1] for r in con.execute("PRAGMA table_info(tags)")}
    if "parent" not in cols:
        con.execute("ALTER TABLE tags ADD COLUMN parent INTEGER REFERENCES tags(id)")
        con.commit()


def _limpa(nome):
    """A tag is a label a person types, so it gets the treatment of one."""
    n = " ".join(str(nome or "").split())
    if not n:
        raise ValueError("a etiqueta precisa de um nome")
    if len(n) > MAX_NOME:
        raise ValueError("nome longo demais (máximo %d)" % MAX_NOME)
    return n


def _parte(con, caminho):
    """Absolute path -> (label, relpath, is_dir). Raises if outside the index.

    The same gate `actions.resolve_target` uses, and for the same reason: a tag
    is written from the browser, and nothing typed there should be able to name
    an arbitrary place on the machine.
    """
    p = os.path.abspath(os.path.expanduser(str(caminho or "")))
    if "\x00" in str(caminho):
        raise ValueError("caminho inválido")
    vols = [(r["label"], r["mount"]) for r in
            con.execute("SELECT label, mount FROM volumes WHERE mount IS NOT NULL")]
    for label, mount in sorted(vols, key=lambda v: -len(v[1] or "")):
        m = os.path.abspath(mount)
        if p == m or p.startswith(m.rstrip("/") + "/"):
            rel = os.path.relpath(p, m)
            rel = "" if rel == "." else rel
            return label, rel, os.path.isdir(p)
    raise ValueError("caminho fora dos volumes indexados")


def listar(con):
    """Every tag with how many paths carry it, and who its parent is."""
    ensure(con)
    return [dict(r) for r in con.execute(
        "SELECT t.id, t.name, t.parent, p2.name AS pai_nome, t.created, "
        "COUNT(p.relpath) AS caminhos "
        "FROM tags t LEFT JOIN tag_paths p ON p.tag=t.id "
        "LEFT JOIN tags p2 ON p2.id=t.parent "
        "GROUP BY t.id ORDER BY t.name COLLATE NOCASE")]


def _id(con, nome):
    r = con.execute("SELECT id FROM tags WHERE name=? COLLATE NOCASE",
                    (_limpa(nome),)).fetchone()
    return r[0] if r else None


def _com_descendentes(con, tid):
    """A tag and everything under it.

    Filtering by a parent has to bring what sits under it: the parent is an
    umbrella, not an older sibling. Iterative walk with a visited set because an
    accidental cycle in the tree must not turn a query into an endless loop.
    """
    vistos, fila = set(), [tid]
    while fila:
        cur = fila.pop()
        if cur in vistos:
            continue
        vistos.add(cur)
        for r in con.execute("SELECT id FROM tags WHERE parent=?", (cur,)):
            fila.append(r[0])
    return vistos


def marcar(con, nome, caminho, pai=None):
    """Tag a path. `pai` nests this tag under another one.

    The name stays globally unique: a name means one thing wherever it lives in
    the tree. A sub-tag is a parent LINK, not a namespace -- otherwise a name in
    a URL would be ambiguous, and two unrelated children sharing a name under
    two different parents would be impossible to tell apart in a chip row.
    """
    ensure(con)
    n = _limpa(nome)
    label, rel, is_dir = _parte(con, caminho)
    pid = None
    if pai:
        pid = _id(con, pai)
        if pid is None:
            raise ValueError("etiqueta-mãe não existe: %s" % pai)
    con.execute("INSERT OR IGNORE INTO tags (name, parent, created) VALUES (?,?,?)",
                (n, pid, int(time.time())))
    tid = con.execute("SELECT id FROM tags WHERE name=? COLLATE NOCASE",
                      (n,)).fetchone()[0]
    if pid is not None:
        # Pôr uma etiqueta dentro da própria descendência fecharia um ciclo, e
        # um ciclo aqui é uma consulta que não termina.
        if pid in _com_descendentes(con, tid):
            raise ValueError("'%s' já está dentro de '%s': isso faria um ciclo"
                             % (pai, n))
        con.execute("UPDATE tags SET parent=? WHERE id=?", (pid, tid))
    con.execute("INSERT OR REPLACE INTO tag_paths (tag,label,relpath,is_dir,added) "
                "VALUES (?,?,?,?,?)", (tid, label, rel, 1 if is_dir else 0,
                                       int(time.time())))
    con.commit()
    return {"ok": True, "tag": n, "label": label, "relpath": rel,
            "is_dir": bool(is_dir)}


MAX_LOTE = 5000


def marcar_varios(con, nome, caminhos, pai=None, acao="add"):
    """Tag (or untag) many paths in one go.

    Returns what happened per path instead of failing on the first bad one: in
    a selection of two hundred, one unmounted volume should not cost the other
    hundred and ninety-nine. The caller gets counts and the reasons.

    A cap exists because this is one transaction and the paths come from a
    browser; without it a stray loop could write a million rows before anyone
    noticed.
    """
    ensure(con)
    if acao not in ("add", "remove"):
        raise ValueError("ação desconhecida: %s" % acao)
    n = _limpa(nome)
    caminhos = [c for c in (caminhos or []) if str(c).strip()]
    if not caminhos:
        raise ValueError("nenhum caminho informado")
    if len(caminhos) > MAX_LOTE:
        raise ValueError("lote grande demais: %d caminhos (máximo %d). "
                         "Etiquetar a pasta cobre tudo que está dentro."
                         % (len(caminhos), MAX_LOTE))
    pid = None
    if pai:
        pid = _id(con, pai)
        if pid is None:
            raise ValueError("etiqueta-mãe não existe: %s" % pai)
    if acao == "add":
        con.execute("INSERT OR IGNORE INTO tags (name, parent, created) VALUES (?,?,?)",
                    (n, pid, int(time.time())))
    tid = _id(con, n)
    if tid is None:
        raise ValueError("etiqueta não existe: %s" % n)
    if pid is not None:
        if pid in _com_descendentes(con, tid):
            raise ValueError("'%s' já está dentro de '%s': isso faria um ciclo"
                             % (pai, n))
        con.execute("UPDATE tags SET parent=? WHERE id=?", (pid, tid))

    feitos, pulados = 0, []
    agora = int(time.time())
    for c in caminhos:
        try:
            label, rel, is_dir = _parte(con, c)
        except ValueError as e:
            pulados.append({"path": str(c), "erro": str(e)})
            continue
        if acao == "add":
            con.execute("INSERT OR REPLACE INTO tag_paths "
                        "(tag,label,relpath,is_dir,added) VALUES (?,?,?,?,?)",
                        (tid, label, rel, 1 if is_dir else 0, agora))
        else:
            con.execute("DELETE FROM tag_paths WHERE tag=? AND label=? AND relpath=?",
                        (tid, label, rel))
        feitos += 1
    con.commit()
    return {"ok": not pulados, "tag": n, "acao": acao, "feitos": feitos,
            "pulados": pulados,
            "output": "%d caminho(s) %s a '%s'%s"
                      % (feitos, "etiquetados" if acao == "add" else "desetiquetados",
                         n, "" if not pulados else "; %d recusado(s)" % len(pulados))}


def desmarcar(con, nome, caminho):
    ensure(con)
    n = _limpa(nome)
    label, rel, _d = _parte(con, caminho)
    r = con.execute("SELECT id FROM tags WHERE name=? COLLATE NOCASE", (n,)).fetchone()
    if not r:
        raise ValueError("etiqueta não existe: %s" % n)
    con.execute("DELETE FROM tag_paths WHERE tag=? AND label=? AND relpath=?",
                (r[0], label, rel))
    # Uma etiqueta sem nenhum caminho é ruído no seletor, mas apagá-la sozinha
    # perderia o nome que a pessoa escolheu. Fica, e a lista mostra zero.
    con.commit()
    return {"ok": True, "tag": n}


def renomear(con, antigo, novo):
    ensure(con)
    a, b = _limpa(antigo), _limpa(novo)
    try:
        con.execute("UPDATE tags SET name=? WHERE name=? COLLATE NOCASE", (b, a))
    except sqlite3.IntegrityError:
        raise ValueError("já existe uma etiqueta chamada %s" % b)
    con.commit()
    return {"ok": True, "de": a, "para": b}


def apagar(con, nome):
    ensure(con)
    n = _limpa(nome)
    r = con.execute("SELECT id FROM tags WHERE name=? COLLATE NOCASE", (n,)).fetchone()
    if not r:
        raise ValueError("etiqueta não existe: %s" % n)
    con.execute("DELETE FROM tag_paths WHERE tag=?", (r[0],))
    con.execute("DELETE FROM tags WHERE id=?", (r[0],))
    con.commit()
    return {"ok": True, "tag": n}


def _escapa(p):
    return p.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def clausula(con, nomes):
    """(sql, args) that match every file carried by any of these tags.

    A tagged file matches itself; a tagged directory matches everything beneath
    it. Resolved here, at query time, so adding a file to a tagged folder puts
    it under the tag without anyone touching the tag.
    """
    ensure(con)
    partes, args = [], []
    for nome in nomes:
        tid = _id(con, nome)
        if tid is None:
            continue
        familia = sorted(_com_descendentes(con, tid))
        for p in con.execute(
                "SELECT label, relpath, is_dir FROM tag_paths WHERE tag IN (%s)"
                % ",".join("?" * len(familia)), familia):
            if p["is_dir"]:
                if p["relpath"] == "":
                    partes.append("(v.label = ?)")
                    args.append(p["label"])
                else:
                    partes.append("(v.label = ? AND (d.relpath = ? OR "
                                  "d.relpath LIKE ? ESCAPE '\\'))")
                    args += [p["label"], p["relpath"],
                             _escapa(p["relpath"]) + "/%"]
            else:
                head, _, name = p["relpath"].rpartition("/")
                partes.append("(v.label = ? AND d.relpath = ? AND f.name = ?)")
                args += [p["label"], head, name]
    if not partes:
        return "0", []          # etiqueta sem caminho não casa com nada
    return "(" + " OR ".join(partes) + ")", args


def do_caminho(con, caminho):
    """Which tags cover this path, directly or through a parent folder."""
    ensure(con)
    try:
        label, rel, _d = _parte(con, caminho)
    except ValueError:
        return []
    pais = [""]
    acc = []
    for parte in rel.split("/"):
        acc.append(parte)
        pais.append("/".join(acc))
    out = []
    for r in con.execute(
            "SELECT DISTINCT t.name, p.relpath, p.is_dir FROM tag_paths p "
            "JOIN tags t ON t.id=p.tag WHERE p.label=?", (label,)):
        if r["relpath"] == rel or (r["is_dir"] and r["relpath"] in pais):
            out.append({"name": r["name"], "herdada": r["relpath"] != rel,
                        "de": r["relpath"]})
    return sorted(out, key=lambda x: x["name"].lower())
