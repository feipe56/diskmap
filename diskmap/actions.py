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
import json, os, shlex, sqlite3, subprocess, sys, time
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
    # Behind its own flag: it is the only action with no way back.
    "wipe": {"name": "Zerar e apagar", "id": "wipe",
             "argv": None, "confirm": True, "mutates": True,
             "irreversible": True, "needs_wipe": True,
             "help": "sobrescreve cada byte com zero, confere a leitura e então "
                     "apaga. NÃO TEM UNDO, e o que isso garante depende do "
                     "filesystem e do disco"},
}


def ensure_log(con):
    con.executescript(LOG_SCHEMA)
    con.commit()


# Ações que tiram o arquivo de onde ele estava. `undo` devolve.
SAIDAS = {"discard-clone": "quarentena", "quarantine": "quarentena",
          "trash": "lixeira"}


def fora_do_lugar(con):
    """{caminho absoluto: 'quarentena'|'lixeira'} pelo diário de ações.

    Só a ÚLTIMA ação de cada caminho conta. `undo` devolve o arquivo e grava
    outra linha em vez de apagar a anterior, então quem olhasse qualquer linha
    diria "está na quarentena" para sempre, inclusive do que já voltou.

    Isto responde metade da pergunta "esse arquivo ainda está lá?". A outra
    metade é o disco: o usuário pode ter apagado por fora, e disso o diário não
    sabe nada. Quem mostra uma lista de candidatos precisa das duas.
    """
    ensure_log(con)
    fora = {}
    for r in con.execute(
            "SELECT path, action FROM action_log "
            "WHERE action IN (%s) OR action = 'undo' ORDER BY at, id"
            % ",".join("?" * len(SAIDAS)), tuple(SAIDAS)):
        if r["action"] == "undo":
            fora.pop(r["path"], None)
        else:
            fora[r["path"]] = SAIDAS[r["action"]]
    return fora


def quem_ficou(argv):
    """O caminho da cópia que ficou, se o diário registrou.

    Linhas gravadas antes desta informação existir devolvem None, e quem lê tem
    de tratar isso como "não sei" em vez de "não havia" -- são coisas
    diferentes, e confundi-las é o tipo de erro que esvazia uma quarentena.
    """
    marca = "fica="
    if not argv or marca not in argv:
        return None
    return argv.split(marca, 1)[1].strip() or None


def estado_no_disco(abspath, fora, raiz=None, _mem=None):
    """'ok' | 'quarentena' | 'lixeira' | 'sumido' | 'desmontado'.

    `raiz` é o ponto de montagem do volume, e a ordem importa: num volume que
    não está plugado TODO arquivo some do `os.path.exists`, e chamar isso de
    "sumido" seria dizer que foi apagado. São afirmações diferentes -- uma pede
    ação, a outra pede um cabo USB.
    """
    if not abspath:
        return "desmontado"
    if raiz is not None:
        if _mem is None:
            _mem = {}
        if raiz not in _mem:
            _mem[raiz] = os.path.isdir(raiz)
        if not _mem[raiz]:
            return "desmontado"
    if os.path.exists(abspath):
        return "ok"
    return fora.get(abspath) or "sumido"


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


def available(path=CONFIG, allow_wipe=False):
    """Action descriptors for the UI, with unavailable ones filtered out."""
    out = []
    for a in BUILTIN.values():
        if a.get("needs_wipe") and not allow_wipe:
            continue
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


def do_wipe(abspath, kind, mount):
    """Zero-fill one file. Refuses anything it cannot actually do.

    A directory is refused outright rather than walked: wiping a tree is a very
    different promise from wiping a file, and doing it by implication is how
    someone loses a folder they meant to keep.
    """
    from . import wipe as wipe_mod
    if kind != "file":
        return 1, "zerar só vale para arquivo, não para pasta", None
    a = wipe_mod.assess(mount)
    if not a["allowed"]:
        return 1, f"não dá para zerar aqui: {a['why']}", None
    try:
        r = wipe_mod.zero_file(abspath)
    except (ValueError, OSError) as e:
        return 1, str(e), None
    note = {"efetivo": "", "parcial": " (ATENÇÃO: " + a["why"] + ")",
            "fraco": " (ATENÇÃO: " + a["why"] + ")",
            "inutil": " (ATENÇÃO: " + a["why"] + ")"}[a["level"]]
    return 0, (f"{r['bytes']} bytes zerados e conferidos, arquivo apagado. "
               f"Garantia neste volume: {a['level']}{note}"), None


def run(con, action_id, raw_path, config_path=CONFIG, allow_wipe=False):
    """Execute one named action. Returns a dict for the API."""
    ensure_log(con)
    abspath, kind, label, mount = resolve_target(con, raw_path)

    if action_id == "wipe":
        if not allow_wipe:
            raise ValueError("zerar está desligado; suba o serve com --allow-wipe")
        rc, out, moved = do_wipe(abspath, kind, mount)
        argv = ["<interno>", "zerar"]
    elif action_id == "quarantine":
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


# ----------------------------------------------------------- clone resolver
def _readonly(mount):
    try:
        return bool(os.statvfs(mount).f_flag & os.ST_RDONLY)
    except OSError:
        return False


def _protected(abspath):
    """True when the path sits inside something a launcher owns.

    Launcher installs are protected both ways: writing one file into a game's
    install corrupts it, and the launcher re-downloads anyway. Both sides are
    refused, not just the one being replaced -- copying *out of* a launcher over
    your own copy is how you lose the version you actually edited.
    """
    from .analyze import PROTECTED
    low = abspath.lower()
    return any(p in low for p in PROTECTED)


class _impaciente:
    """Baixa o busy_timeout dentro do bloco e devolve ao sair.

    A conexão espera 10 s pela trava de propósito, que é o certo para uma
    escrita que precisa acontecer. Para *descobrir* se a trava está livre, não
    é: quem a segura é um scan/analyze/verify, que fica com ela por minutos, e
    gastar os 10 s para então dizer "não deu" transforma uma resposta instantânea
    em dez segundos parado.
    """

    def __init__(self, con, ms=250):
        self.con, self.ms = con, ms

    def __enter__(self):
        self.con.execute("PRAGMA busy_timeout=%d" % self.ms)
        return self.con

    def __exit__(self, *exc):
        self.con.execute("PRAGMA busy_timeout=10000")
        return False


def _index_writable(con):
    """True when this connection can take the write lock right now.

    scan/import/analyze/verify run one at a time and hold the index's single
    writer slot for minutes. Anything destructive has to know that BEFORE it
    starts: replacing a file without being able to record it in `action_log`
    would leave a change the history tab cannot undo.
    """
    try:
        with _impaciente(con):
            con.execute("BEGIN IMMEDIATE")
        con.rollback()
        return True
    except sqlite3.OperationalError:
        return False


def compare_pair(con, a_raw, b_raw, persist=True):
    """sha256 both sides of a cloned pair and say whether they are the same.

    The panel can only show what `verify` happened to have hashed already, so
    most pairs read "não verificado" -- and the whole point of this feature is
    to not act on a guess. This turns the guess into an answer, and keeps it:
    `file_hash` is keyed by file id and survives a re-`analyze`, which rebuilds
    the derived findings from scratch.
    """
    from . import verify as verify_mod
    out = {}
    ids = {}
    for key, raw in (("a", a_raw), ("b", b_raw)):
        abspath, kind, label, mount = resolve_target(con, raw)
        if kind != "file":
            raise ValueError("comparação de hash só vale para arquivo")
        out[key] = {"path": abspath,
                    "sha": verify_mod.full_digest(abspath, nocache=True),
                    "bytes": os.path.getsize(abspath)}
        rel = os.path.relpath(abspath, mount)
        head, _, name = rel.rpartition("/")
        r = con.execute("SELECT f.id FROM files f JOIN dirs d ON d.id=f.dir "
                        "JOIN volumes v ON v.id=f.vol "
                        "WHERE v.label=? AND d.relpath=? AND f.name=? LIMIT 1",
                        (label, head, name)).fetchone()
        if r:
            ids[key] = r["id"]
    # Guardar é bônus, nunca requisito: a resposta que o usuário pediu — "estes
    # dois são o mesmo arquivo?" — já está calculada, e perdê-la porque um
    # `verify` está com a trava do índice seria jogar fora a leitura do disco
    # que acabou de ser feita.
    out["persisted"] = False
    if persist and ids:
        try:
            with _impaciente(con):
                for key, fid in ids.items():
                    con.execute(
                        "INSERT INTO file_hash (file, quick, full, hashed_at) "
                        "VALUES (?,NULL,?,?) ON CONFLICT(file) DO UPDATE SET "
                        "full=excluded.full, hashed_at=excluded.hashed_at",
                        (fid, out[key]["sha"], int(time.time())))
                con.commit()
            out["persisted"] = True
        except sqlite3.OperationalError as e:
            con.rollback()
            out["persist_erro"] = ("não deu para guardar no índice (%s). O "
                                   "veredito abaixo vale; ele só não fica "
                                   "salvo até o índice liberar." % e)
    out["equal"] = out["a"]["sha"] == out["b"]["sha"]
    return out


def discard_one(con, raw, keep=None, expect=None):
    """Quarantine one copy, having checked that another one survives.

    This is what actually dedups.

    `resolve_clone` makes two copies identical; it frees nothing, because both
    files still exist. Removing the redundancy means keeping one and taking the
    other out of the way -- and "out of the way" here is the quarantine, never
    unlink: the file stays on the same volume, under its original relative path,
    and the history tab puts it back.

    The same two rails as the resolver, for the same reasons: a launcher's
    install is refused (pulling one file out of a game corrupts it), and a
    locked index means no record, which means no undo, which means don't.

    `keep` is the copy that must remain, and it is REQUIRED: a dedup action
    whose last click leaves zero copies is not a dedup, it is a deletion with
    extra steps. Nothing about "there are N copies" is taken on faith from the
    index, which is a photograph and may be stale -- the surviving file is
    stat-ed on disk, right now, before anything moves.

    `expect` is the sha256 the caller believed this file had; when it is given
    and the file no longer hashes to it, nothing moves.
    """
    ensure_log(con)
    if not _index_writable(con):
        raise ValueError(
            "o índice está travado por outra tarefa (um scan, analyze ou verify "
            "em curso na aba Tarefas). Sem poder registrar no histórico, o "
            "descarte não seria desfazível — espere a tarefa terminar.")
    abspath, kind, label, mount = resolve_target(con, raw)
    if kind != "file":
        raise ValueError("descartar só vale para arquivo, não para pasta")
    if not keep:
        raise ValueError("descartar exige dizer qual cópia fica: sem isso não há "
                         "como garantir que o acervo não fique sem nenhuma")
    guarda = os.path.abspath(os.path.expanduser(str(keep)))
    if guarda == os.path.abspath(abspath):
        raise ValueError("a cópia que fica e a que sai são o mesmo arquivo")
    if not os.path.isfile(guarda):
        raise ValueError("a cópia que deveria ficar não está lá: %s. Nada foi "
                         "movido — descartar agora deixaria o acervo sem nenhuma."
                         % guarda)
    if QUAR_NAME in guarda.split(os.sep):
        raise ValueError("a cópia que deveria ficar já está na quarentena (%s): "
                         "descartar esta deixaria as duas fora do acervo." % guarda)
    if _protected(abspath):
        raise ValueError(
            "caminho protegido (instalação de launcher): %s. Tirar um arquivo "
            "solto dali corrompe a instalação, e o launcher rebaixa de qualquer "
            "forma." % abspath)
    if _readonly(mount):
        raise ValueError("o volume está montado somente leitura")
    if expect:
        from . import verify as verify_mod
        atual = verify_mod.full_digest(abspath, nocache=True)
        if atual != expect:
            raise ValueError("o arquivo mudou desde a leitura do índice: sha256 "
                             "é %s, você viu %s" % (atual[:16], expect[:16]))
    tam = os.path.getsize(abspath)
    rc, out, moved = do_quarantine(con, abspath, label, mount)
    # A cópia que fica vai para o diário, não só para a tela. É a única
    # informação que torna possível esvaziar a quarentena com segurança depois:
    # "este saiu PORQUE aquele ficou". Sem ela, meses depois, a única saída é
    # reconstruir o par por nome e tamanho -- que é candidato, não prova.
    con.execute("INSERT INTO action_log (at,action,path,moved_to,argv,rc,output) "
                "VALUES (?,?,?,?,?,?,?)",
                (int(time.time()), "discard-clone", abspath, moved,
                 "discard-clone fica=%s" % guarda, rc, out[:8000]))
    con.commit()
    return {"ok": rc == 0, "path": abspath, "moved_to": moved, "bytes": tam,
            "kept": guarda, "output": out, "action": "discard-clone"}


def verify_group(con, grp, stage="full"):
    """Hash one duplicate group on demand and record the verdict.

    Delegates to `verify.verify_groups(only=grp)` instead of hashing here: the
    rule for "are these the same" has to live in one place, or the panel and the
    `verify` command are free to disagree about the same group.
    """
    ensure_log(con)
    if not _index_writable(con):
        raise ValueError(
            "o índice está travado por outra tarefa (scan, analyze ou verify em "
            "curso). A verificação precisa gravar o veredito — espere terminar.")
    from . import verify as verify_mod
    if stage not in ("quick", "full"):
        raise ValueError("estágio desconhecido: %s" % stage)
    # Quem não está mais no disco faz o verify pular o grupo inteiro e devolver
    # só "inacessível". Dizer QUAIS é o que torna isso acionável: significa que
    # o índice envelheceu e aquele volume precisa de um scan.
    sumidos = []
    for r in con.execute("SELECT file FROM dup_members WHERE grp=?", (grp,)):
        caminho = verify_mod.abs_path(con, r["file"])
        if not caminho or not os.path.exists(caminho):
            sumidos.append(caminho or "arquivo %s" % r["file"])
    if sumidos:
        raise ValueError(
            "%d das cópias não estão mais no disco, então o grupo não pode ser "
            "conferido: %s%s. O índice é de uma varredura anterior — rode "
            "'Indexar um volume' e 'Recalcular achados' para ele voltar a "
            "descrever o disco."
            % (len(sumidos), "; ".join(sumidos[:3]),
               "" if len(sumidos) <= 3 else " e outras %d" % (len(sumidos) - 3)))
    ok_n, mism, skipped = verify_mod.verify_groups(con, stage=stage,
                                                   progress=False, only=grp)
    r = con.execute("SELECT verified, copies FROM dup_groups WHERE id=?",
                    (grp,)).fetchone()
    return {"ok": skipped == 0, "grp": grp, "verified": r["verified"],
            "copies": r["copies"], "stage": stage,
            "output": ("inacessível: alguma cópia não pôde ser lida"
                       if skipped else
                       "as %d cópias têm o mesmo sha256" % r["copies"] if ok_n else
                       "as cópias NÃO são todas iguais")}


def discard_group(con, grp, keep):
    """Quarantine every copy of a group except the one that stays.

    A group has N copies, so acting on it one file at a time is both tedious and
    the easy way to end up with none. Here the survivor is named once, checked
    on disk once, and everything else moves.

    Refuses outright when the group is `mismatch`: if the copies are proven to
    differ, there is no redundancy to remove -- they are distinct files that
    happen to share a name and a size, and discarding one loses content. That is
    the one case where the honest answer is "this is not a duplicate group".
    """
    ensure_log(con)
    if not _index_writable(con):
        raise ValueError(
            "o índice está travado por outra tarefa (um scan, analyze ou verify "
            "em curso na aba Tarefas). Sem poder registrar no histórico, o "
            "descarte não seria desfazível — espere a tarefa terminar.")
    g = con.execute("SELECT id, name, copies, verified FROM dup_groups WHERE id=?",
                    (grp,)).fetchone()
    if not g:
        raise ValueError("grupo %s não existe" % grp)
    if g["verified"] == "mismatch":
        raise ValueError(
            "este grupo está marcado como DIVERGENTE: o hash provou que as "
            "cópias têm conteúdo diferente. Não há redundância para remover — "
            "descartar aqui perde conteúdo que não existe em outro lugar.")
    guarda = os.path.abspath(os.path.expanduser(str(keep or "")))
    if not keep or not os.path.isfile(guarda):
        raise ValueError("a cópia que fica tem de existir no disco: %s"
                         % (guarda or "(não informada)"))
    membros = []
    for r in con.execute(
            "SELECT v.mount, d.relpath, f.name FROM dup_members m "
            "JOIN files f ON f.id=m.file JOIN dirs d ON d.id=f.dir "
            "JOIN volumes v ON v.id=f.vol WHERE m.grp=?", (grp,)):
        if not r["mount"]:
            continue              # volume desmontado: não dá para agir
        rel = ("%s/%s" % (r["relpath"], r["name"])) if r["relpath"] else r["name"]
        membros.append(os.path.abspath(os.path.join(r["mount"], rel)))
    if guarda not in membros:
        raise ValueError("a cópia que fica não é membro deste grupo: %s" % guarda)
    alvos = [m for m in membros if m != guarda]
    if not alvos:
        raise ValueError("não sobrou nenhuma cópia para descartar neste grupo")

    feitos, falhas = [], []
    for p in alvos:
        try:
            r = discard_one(con, p, keep=guarda)
            feitos.append({"path": r["path"], "moved_to": r["moved_to"],
                           "bytes": r["bytes"]})
        except ValueError as e:
            falhas.append({"path": p, "erro": str(e)})
    return {"ok": not falhas, "grp": grp, "kept": guarda,
            "descartados": feitos, "falhas": falhas,
            "bytes": sum(f["bytes"] for f in feitos),
            "action": "discard-group",
            "output": "%d de %d cópias para a quarentena; %s liberado%s"
                      % (len(feitos), len(alvos),
                         db.human(sum(f["bytes"] for f in feitos)),
                         "" if not falhas else "; %d recusada(s)" % len(falhas))}


def resolve_clone(con, src_raw, dst_raw, expect=None):
    """Make `dst` identical to `src`, reversibly.

    The only place in diskmap that replaces an existing file, so it carries by
    hand the rails the rest of the codebase gets by never writing at all:

      1. both sides must be in the index and outside any launcher's install;
      2. the file being replaced is QUARANTINED first, never clobbered, so the
         history tab can put it back: removing means Trash or quarantine here,
         never unlink, and replacing in place is a removal wearing a different
         hat;
      3. the new copy is written to a temporary beside the target and only
         renamed into place after a read-back with F_NOCACHE matches the source.
         If anything fails, the quarantined original goes back.

    `expect` is the sha256 the caller believed the source had. The index is a
    photograph and the disk may have moved since: when it no longer matches,
    nothing is written.
    """
    ensure_log(con)
    # Antes de qualquer coisa destrutiva: se não dá para gravar no action_log,
    # a substituição não poderia ser desfeita pelo Histórico — e aí ela não deve
    # acontecer. Um `verify`/`analyze` em curso segura a trava por minutos.
    if not _index_writable(con):
        raise ValueError(
            "o índice está travado por outra tarefa (um scan, analyze ou verify "
            "em curso na aba Tarefas). Sem poder registrar no histórico, a "
            "substituição não seria desfazível — espere a tarefa terminar.")
    src, skind, slabel, smount = resolve_target(con, src_raw)
    dst, dkind, dlabel, dmount = resolve_target(con, dst_raw)
    if skind != "file" or dkind != "file":
        raise ValueError("resolver clone só vale entre dois arquivos")
    if os.path.abspath(src) == os.path.abspath(dst):
        raise ValueError("origem e destino são o mesmo arquivo")
    for p in (src, dst):
        if _protected(p):
            raise ValueError(
                "caminho protegido (instalação de launcher): %s. Apagar ou "
                "sobrescrever um arquivo solto ali corrompe a instalação, e o "
                "launcher rebaixa de qualquer forma." % p)
    if _readonly(dmount):
        raise ValueError("o volume de destino está montado somente leitura")

    from . import verify as verify_mod
    sha_src = verify_mod.full_digest(src, nocache=True)
    if expect and expect != sha_src:
        raise ValueError("a origem mudou desde a leitura do índice: sha256 é %s, "
                         "você viu %s" % (sha_src[:16], expect[:16]))
    tam = os.path.getsize(src)

    quarantined = _move(dst, os.path.join(dmount, QUAR_NAME,
                                          os.path.relpath(dst, dmount)))
    tmp = dst + ".diskmap-parcial"
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.lexists(tmp):
            os.remove(tmp)
        with open(src, "rb") as fi, open(tmp, "xb") as fo:
            while True:
                b = fi.read(4 << 20)
                if not b:
                    break
                fo.write(b)
            fo.flush()
            os.fsync(fo.fileno())
        st = os.stat(src)
        os.utime(tmp, (st.st_atime, st.st_mtime))
        got = verify_mod.full_digest(tmp, nocache=True)
        if got != sha_src:
            raise OSError("a cópia saiu diferente da origem (%s != %s)"
                          % (got[:16], sha_src[:16]))
        os.rename(tmp, dst)
    except BaseException as e:
        if os.path.lexists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        if not os.path.lexists(dst) and os.path.lexists(quarantined):
            os.rename(quarantined, dst)      # põe o original de volta
            quarantined = None
        con.execute("INSERT INTO action_log (at,action,path,moved_to,argv,rc,output) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (int(time.time()), "resolve-clone", dst, quarantined,
                     "resolve-clone de %s" % src, 1, str(e)[:8000]))
        con.commit()
        raise ValueError("não deu para substituir: %s" % e)

    saida = ("substituído por %s (%s bytes, sha256 %s conferido na releitura); "
             "o anterior está em %s" % (src, tam, sha_src[:16], quarantined))
    con.execute("INSERT INTO action_log (at,action,path,moved_to,argv,rc,output) "
                "VALUES (?,?,?,?,?,?,?)",
                (int(time.time()), "resolve-clone", dst, quarantined,
                 "resolve-clone de %s" % src, 0, saida))
    con.commit()
    return {"ok": True, "path": dst, "source": src, "moved_to": quarantined,
            "sha256": sha_src, "bytes": tam, "output": saida,
            "action": "resolve-clone"}


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
