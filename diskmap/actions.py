# SPDX-License-Identifier: GPL-3.0-or-later
"""Actions the frontend may trigger on a path.

Three rules make this safe enough to expose to a browser:

1. No string from the browser ever reaches a shell. The browser sends an action
   *name* and a path; the argv comes from a declaration on disk that the user
   owns, and subprocess runs it as a list with shell=False.
2. The path must already exist in the index. You cannot aim an action at
   /etc/passwd, because diskmap never indexed it.
3. Removal means Trash or quarantine, never unlink. Every run is logged with
   enough detail to put the file back.

User actions live in ~/.diskmap/actions.json and are created on first run.
"""
import json, os, shlex, subprocess, sys, time
from . import db

CONFIG = os.path.expanduser("~/.diskmap/actions.json")
TIMEOUT = 60
QUAR_NAME = ".diskmap-quarantine"

LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS action_log (
  id      INTEGER PRIMARY KEY,
  at      INTEGER NOT NULL,
  action  TEXT NOT NULL,
  path    TEXT NOT NULL,
  moved_to TEXT,
  argv    TEXT,
  rc      INTEGER,
  output  TEXT
);
"""

DEFAULT_CONFIG = {
    "_comment": [
        "Ações extras do diskmap. Cada entrada vira um botão no painel.",
        "'argv' é uma lista: o primeiro item é o programa, e {path} / {dir} são",
        "substituídos pelo alvo. Nada passa por shell, então pipe e ; não funcionam",
        "- se precisar de pipeline, aponte para um script seu.",
        "'confirm': pede confirmação no painel. 'capture': mostra a saída.",
        "'needs': só aparece se o programa existir no PATH.",
    ],
    "actions": [
        {"name": "Listar conteúdo (7z)", "id": "7z_list",
         "argv": ["7z", "l", "{path}"], "capture": True, "needs": "7z",
         "help": "o que tem dentro do compactado, sem extrair"},
        {"name": "Tamanho real (du)", "id": "du",
         "argv": ["du", "-sh", "{path}"], "capture": True,
         "help": "confere o tamanho em disco agora"},
        {"name": "Tipo do arquivo (file)", "id": "file",
         "argv": ["file", "-b", "{path}"], "capture": True,
         "help": "identifica o formato pelo conteúdo"},
        {"name": "Contar itens", "id": "count",
         "argv": ["bash", "-c", "find \"$1\" -type f | wc -l", "_", "{path}"],
         "capture": True, "help": "quantos arquivos a pasta tem hoje"},
    ],
}

# Built-ins: always present, never read from config.
BUILTIN = {
    "reveal": {"name": "Abrir no Finder", "id": "reveal",
               "argv": ["open", "-R", "{path}"],
               "help": "revela o item no Finder"},
    "open_dir": {"name": "Abrir a pasta", "id": "open_dir",
                 "argv": ["open", "{dir}"],
                 "help": "abre a pasta que contém o item"},
    "open_with": {"name": "Abrir com o app padrão", "id": "open_with",
                  "argv": ["open", "{path}"], "confirm": True,
                  "help": "abre o arquivo no app padrão do macOS"},
    "quarantine": {"name": "Mover para quarentena", "id": "quarantine",
                   "argv": None, "confirm": True, "mutates": True,
                   "help": "move para .diskmap-quarantine no mesmo volume "
                           "(instantâneo, reversível aqui mesmo)"},
    "trash": {"name": "Mover para a Lixeira", "id": "trash",
              "argv": None, "confirm": True, "mutates": True,
              "help": "vai para a Lixeira do volume; dá para usar "
                      "'Colocar de volta' no Finder"},
}


def ensure_log(con):
    con.executescript(LOG_SCHEMA)
    con.commit()


def load_config(path=CONFIG, create=True):
    if not os.path.exists(path):
        if create:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
            # the other file that is yours to edit, so both show up together
            from . import userconf
            userconf.write_template()
        return DEFAULT_CONFIG
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        print(f"actions.json inválido ({e}); usando só as ações embutidas",
              file=sys.stderr)
        return {"actions": []}


def available(path=CONFIG):
    """Action descriptors for the UI, with unavailable ones filtered out."""
    out = []
    for a in BUILTIN.values():
        out.append({k: v for k, v in a.items() if k != "argv"} | {"builtin": True})
    for a in load_config(path).get("actions", []):
        if not isinstance(a, dict) or "id" not in a or "argv" not in a:
            continue
        need = a.get("needs")
        if need and not _which(need):
            continue
        out.append({"id": a["id"], "name": a.get("name", a["id"]),
                    "help": a.get("help", ""), "confirm": bool(a.get("confirm")),
                    "capture": bool(a.get("capture")), "builtin": False,
                    "mutates": bool(a.get("mutates"))})
    return out


def _which(prog):
    from shutil import which
    return which(prog) is not None


def resolve_target(con, raw):
    """Map a path from the browser onto something the index actually holds.

    Returns (abspath, kind) or raises ValueError. This is the gate that keeps an
    action from being aimed at an arbitrary location on the machine.
    """
    p = os.path.abspath(os.path.expanduser(raw))
    if "\x00" in raw:
        raise ValueError("caminho inválido")
    vols = [(r["label"], r["mount"]) for r in
            con.execute("SELECT label, mount FROM volumes WHERE mount IS NOT NULL")]
    for label, mount in sorted(vols, key=lambda v: -len(v[1] or "")):
        mount = os.path.abspath(mount)
        if p == mount or p.startswith(mount.rstrip("/") + "/"):
            rel = os.path.relpath(p, mount)
            rel = "" if rel == "." else rel
            head, _, name = rel.rpartition("/")
            hit = con.execute(
                "SELECT 1 FROM files f JOIN dirs d ON d.id=f.dir "
                "JOIN volumes v ON v.id=f.vol "
                "WHERE v.label=? AND d.relpath=? AND f.name=? LIMIT 1",
                (label, head, name or rel)).fetchone()
            if hit:
                return p, "file", label, mount
            hit = con.execute(
                "SELECT 1 FROM dirs d JOIN volumes v ON v.id=d.vol "
                "WHERE v.label=? AND d.relpath=? LIMIT 1", (label, rel)).fetchone()
            if hit:
                return p, "dir", label, mount
            # An aggregated dir (node_modules, .git, a build cache) is a real
            # directory the index holds as one row in `blobs`; the scanner never
            # walks into it, so it appears in neither table above. Without this
            # lookup the Liberar tab could list it and no action could touch it.
            hit = con.execute(
                "SELECT 1 FROM blobs b JOIN volumes v ON v.id=b.vol "
                "WHERE v.label=? AND b.relpath=? LIMIT 1", (label, rel)).fetchone()
            if hit:
                return p, "dir", label, mount
            raise ValueError("esse caminho não está no índice "
                             "(reindexe o volume se ele é novo)")
    raise ValueError("caminho fora dos volumes indexados")


def _move(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        stem, ext = os.path.splitext(dst)
        dst = f"{stem}.{int(time.time())}{ext}"
    os.rename(src, dst)      # same filesystem by construction: instant
    return dst


def do_quarantine(con, abspath, label, mount):
    rel = os.path.relpath(abspath, mount)
    dst = _move(abspath, os.path.join(mount, QUAR_NAME, rel))
    return 0, f"movido para {dst}", dst


def do_trash(con, abspath, label, mount):
    """Finder's own delete, so the item keeps 'Put Back'."""
    script = ('tell application "Finder" to delete POSIX file %s'
              % json.dumps(abspath))
    r = subprocess.run(["osascript", "-e", script], capture_output=True,
                       text=True, timeout=TIMEOUT)
    if r.returncode != 0:
        # Finder automation refused (not granted, or headless): fall back
        home_trash = os.path.expanduser("~/.Trash")
        base = mount if not abspath.startswith(os.path.expanduser("~")) else home_trash
        dst = (os.path.join(mount, ".Trashes", str(os.getuid()),
                            os.path.basename(abspath))
               if base == mount else os.path.join(home_trash, os.path.basename(abspath)))
        try:
            dst = _move(abspath, dst)
            return 0, f"Finder recusou ({r.stderr.strip()[:120]}); movido para {dst}", dst
        except OSError as e:
            return 1, f"não foi para a Lixeira: {e}", None
    return 0, "na Lixeira (use 'Colocar de volta' no Finder)", None


def run(con, action_id, raw_path, config_path=CONFIG):
    """Execute one named action. Returns a dict for the API."""
    ensure_log(con)
    abspath, kind, label, mount = resolve_target(con, raw_path)

    if action_id == "quarantine":
        rc, out, moved = do_quarantine(con, abspath, label, mount)
        argv = ["<interno>", "quarentena"]
    elif action_id == "trash":
        rc, out, moved = do_trash(con, abspath, label, mount)
        argv = ["<interno>", "lixeira"]
    else:
        spec = BUILTIN.get(action_id)
        if spec is None or spec.get("argv") is None:
            for a in load_config(config_path).get("actions", []):
                if a.get("id") == action_id:
                    spec = a
                    break
        if spec is None or not spec.get("argv"):
            raise ValueError(f"ação desconhecida: {action_id}")
        d = abspath if kind == "dir" else os.path.dirname(abspath)
        argv = [str(x).replace("{path}", abspath).replace("{dir}", d)
                for x in spec["argv"]]
        try:
            r = subprocess.run(argv, capture_output=True, text=True,
                               timeout=TIMEOUT, shell=False)
            rc = r.returncode
            out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
        except subprocess.TimeoutExpired:
            rc, out = 124, f"tempo esgotado depois de {TIMEOUT}s"
        except OSError as e:
            rc, out = 127, str(e)
        moved = None

    out = (out or "").strip()
    con.execute("INSERT INTO action_log (at,action,path,moved_to,argv,rc,output) "
                "VALUES (?,?,?,?,?,?,?)",
                (int(time.time()), action_id, abspath, moved,
                 " ".join(shlex.quote(a) for a in argv), rc, out[:8000]))
    con.commit()
    return {"ok": rc == 0, "rc": rc, "output": out[:8000], "path": abspath,
            "moved_to": moved, "action": action_id,
            "argv": " ".join(shlex.quote(a) for a in argv)}


def undo(con, log_id):
    """Put a quarantined item back where it came from."""
    ensure_log(con)
    r = con.execute("SELECT path, moved_to, action FROM action_log WHERE id=?",
                    (log_id,)).fetchone()
    if not r:
        raise ValueError("registro não encontrado")
    if not r["moved_to"]:
        raise ValueError("essa ação não moveu nada que o diskmap possa desfazer "
                         "(se foi para a Lixeira, use 'Colocar de volta' no Finder)")
    if not os.path.exists(r["moved_to"]):
        raise ValueError(f"não está mais em {r['moved_to']}")
    if os.path.exists(r["path"]):
        raise ValueError(f"já existe algo em {r['path']}")
    os.makedirs(os.path.dirname(r["path"]), exist_ok=True)
    os.rename(r["moved_to"], r["path"])
    con.execute("INSERT INTO action_log (at,action,path,moved_to,argv,rc,output) "
                "VALUES (?,?,?,?,?,?,?)",
                (int(time.time()), "undo", r["path"], None,
                 f"undo de #{log_id}", 0, f"restaurado de {r['moved_to']}"))
    con.commit()
    return {"ok": True, "restored": r["path"]}


def log(con, limit=100):
    ensure_log(con)
    return [dict(r) for r in con.execute(
        "SELECT id, at, action, path, moved_to, argv, rc, output FROM action_log "
        "ORDER BY id DESC LIMIT ?", (limit,))]
