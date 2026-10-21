# SPDX-License-Identifier: GPL-3.0-or-later
"""Run diskmap's own commands as background jobs, with a live log.

Each job is a subprocess running `python3 -m diskmap <sub> ...`, which buys three
things over calling the functions in-process: the server stays responsive while a
scan grinds for hours, SQLite never sees two writers in one process, and a job can
actually be cancelled.

The argv is built here from validated parameters. The browser sends a job *kind*
and typed fields - never a command line.
"""
import json, os, re, subprocess, sys, threading, time
from collections import deque

MAX_LOG = 4000          # lines kept per job
LABEL_OK = re.compile(r"^[A-Za-z0-9._-]{1,40}$")
NAME_OK = re.compile(r"^[A-Za-z0-9._-]{1,60}$")

# Jobs that write to the index. Only one runs at a time, so they never fight over
# the database; read-only jobs are unrestricted.
EXCLUSIVE = {"scan", "import", "analyze", "verify", "images", "acervo"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS job_log (
  id       INTEGER PRIMARY KEY,
  kind     TEXT NOT NULL,
  params   TEXT,
  argv     TEXT,
  started  INTEGER,
  finished INTEGER,
  rc       INTEGER,
  tail     TEXT
);
"""


class Job:
    def __init__(self, jid, kind, params, argv):
        self.id = jid
        self.kind = kind
        self.params = params
        self.argv = argv
        self.lines = deque(maxlen=MAX_LOG)
        self.seq = 0               # total lines ever produced
        self.started = time.time()
        self.finished = None
        self.rc = None
        self.proc = None
        self.cancelled = False
        self.lock = threading.Lock()

    @property
    def state(self):
        if self.rc is not None:
            return "cancelado" if self.cancelled else ("ok" if self.rc == 0 else "falhou")
        return "rodando"

    def add(self, line):
        with self.lock:
            self.seq += 1
            self.lines.append((self.seq, line))

    def since(self, n):
        with self.lock:
            return [{"n": s, "line": l} for s, l in self.lines if s > n]

    def info(self):
        return {"id": self.id, "kind": self.kind, "state": self.state,
                "rc": self.rc, "started": self.started, "finished": self.finished,
                "elapsed": round((self.finished or time.time()) - self.started, 1),
                "params": self.params, "argv": " ".join(self.argv), "seq": self.seq}


class Runner:
    def __init__(self, db_path):
        self.db_path = db_path
        self.jobs = {}
        self.order = []
        self.next_id = 1
        self.lock = threading.Lock()

    # ---- parameter validation, one builder per kind ----
    def _argv(self, kind, p):
        base = [sys.executable, "-m", "diskmap", "--db", self.db_path]

        if kind == "scan":
            mount = os.path.abspath(os.path.expanduser(str(p.get("mount", ""))))
            if not os.path.isdir(mount):
                raise ValueError(f"não é uma pasta: {mount}")
            label = str(p.get("label") or os.path.basename(mount.rstrip("/")) or "vol")
            label = label.replace(" ", "_")
            if not LABEL_OK.match(label):
                raise ValueError("rótulo inválido (use letras, números, . _ -)")
            argv = base + ["scan", mount, "--label", label]
            for r in p.get("roots") or []:
                r = str(r).strip().strip("/")
                if not r or ".." in r.split("/"):
                    raise ValueError(f"subpasta inválida: {r}")
                argv += ["--root", r]
            for x in p.get("exclude") or []:
                x = str(x).strip()
                if not x or "/" in x:
                    raise ValueError("exclude é um nome de pasta, sem barra")
                argv += ["--exclude", x]
            if p.get("rescan"):
                argv.append("--rescan")
            return argv

        if kind == "analyze":
            d = int(p.get("depth", 2))
            if not 1 <= d <= 6:
                raise ValueError("profundidade fora de 1..6")
            return base + ["analyze", "--depth", str(d)]

        if kind == "verify":
            stage = str(p.get("stage", "quick"))
            if stage not in ("quick", "full"):
                raise ValueError("stage deve ser quick ou full")
            argv = base + ["verify", "--stage", stage]
            if p.get("limit"):
                lim = int(p["limit"])
                if lim < 1:
                    raise ValueError("limite inválido")
                argv += ["--limit", str(lim)]
            if p.get("min_size"):
                argv += ["--min-size", str(int(p["min_size"]))]
            return argv

        if kind == "plan":
            name = str(p.get("name") or "diskmap-plan")
            if not NAME_OK.match(name):
                raise ValueError("nome do plano inválido")
            out = os.path.join(os.path.expanduser("~"), name)
            argv = base + ["plan", "--out", out]
            for c in p.get("categories") or []:
                if c not in ("duplicata", "arquivo+extraido", "dump/temp"):
                    raise ValueError(f"categoria desconhecida: {c}")
                argv += ["--category", c]
            if p.get("min_size"):
                argv += ["--min-size", str(int(p["min_size"]))]
            if p.get("unverified"):
                argv.append("--unverified")
            return argv

        if kind == "acervo":
            argv = base + ["acervo"]
            c = str(p.get("cls") or "")
            if c:
                # lista fechada: `cls` entra num argv, e argv montado a partir
                # do navegador é como se ganha execução arbitrária
                if c not in ("video", "doc", "audio", "archive", "code",
                             "model3d", "binary", "gamedata", "ml_model", "db"):
                    raise ValueError("classe desconhecida: %s" % c)
                argv += ["--cls", c]
            lim = int(p.get("limit") or 0)
            if lim:
                if not 1 <= lim <= 1_000_000:
                    raise ValueError("limite fora de 1..1000000")
                argv += ["--limit", str(lim)]
            if p.get("refresh"):
                argv += ["--refresh"]
            return argv

        if kind == "images":
            argv = base + ["images"]
            ms = int(p.get("min_size", 100_000))
            if ms < 0:
                raise ValueError("tamanho mínimo inválido")
            argv += ["--min-size", str(ms)]
            w = int(p.get("workers", 8))
            if not 1 <= w <= 32:
                raise ValueError("workers fora de 1..32")
            argv += ["--workers", str(w)]
            th = int(p.get("threshold", 10))
            if not 0 <= th <= 32:
                raise ValueError("distância fora de 0..32")
            argv += ["--threshold", str(th)]
            if p.get("include_dds"):
                argv.append("--include-dds")
            if p.get("refresh"):
                argv.append("--refresh")
            if p.get("only_cluster"):
                argv.append("--only-cluster")
            if p.get("limit"):
                argv += ["--limit", str(int(p["limit"]))]
            return argv

        if kind == "import":
            tsv = os.path.abspath(os.path.expanduser(str(p.get("tsv", ""))))
            if not os.path.isfile(tsv):
                raise ValueError(f"arquivo não encontrado: {tsv}")
            return base + ["import", tsv]

        raise ValueError(f"comando desconhecido: {kind}")

    def start(self, kind, params):
        argv = self._argv(kind, params)
        with self.lock:
            if kind in EXCLUSIVE:
                busy = [j for j in self.jobs.values()
                        if j.rc is None and j.kind in EXCLUSIVE]
                if busy:
                    raise ValueError(
                        f"já existe um '{busy[0].kind}' rodando (#{busy[0].id}). "
                        "Espere ou cancele antes de começar outro.")
            jid = self.next_id
            self.next_id += 1
            job = Job(jid, kind, params, argv)
            self.jobs[jid] = job
            self.order.append(jid)
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def _run(self, job):
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        job.add(f"$ {' '.join(job.argv)}")
        try:
            job.proc = subprocess.Popen(
                job.argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env=env, cwd=root)
        except OSError as e:
            job.add(f"falha ao iniciar: {e}")
            job.rc = 127
            job.finished = time.time()
            return
        for line in job.proc.stdout:
            job.add(line.rstrip("\n"))
        job.proc.wait()
        job.rc = job.proc.returncode
        job.finished = time.time()
        job.add(f"— terminou com código {job.rc} em {job.info()['elapsed']}s")
        self._persist(job)

    def _persist(self, job):
        try:
            import sqlite3
            con = sqlite3.connect(self.db_path, timeout=30)
            con.executescript(SCHEMA)
            tail = "\n".join(l for _, l in list(job.lines)[-60:])
            con.execute("INSERT INTO job_log (kind,params,argv,started,finished,rc,tail) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (job.kind, json.dumps(job.params, ensure_ascii=False),
                         " ".join(job.argv), int(job.started), int(job.finished),
                         job.rc, tail[:8000]))
            con.commit()
            con.close()
        except Exception as e:
            job.add(f"(não gravei no histórico: {e})")

    def cancel(self, jid):
        job = self.jobs.get(jid)
        if not job:
            raise ValueError("tarefa não encontrada")
        if job.rc is not None:
            raise ValueError("essa tarefa já terminou")
        job.cancelled = True
        job.add("— cancelando…")
        if job.proc:
            job.proc.terminate()
            threading.Thread(target=self._hard_kill, args=(job,), daemon=True).start()
        return job.info()

    def _hard_kill(self, job):
        try:
            job.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            job.add("— não respondeu ao terminate; matando")
            job.proc.kill()

    def list(self, limit=30):
        with self.lock:
            ids = self.order[-limit:][::-1]
        return [self.jobs[i].info() for i in ids if i in self.jobs]

    def get(self, jid, since=0):
        job = self.jobs.get(jid)
        if not job:
            raise ValueError("tarefa não encontrada")
        return {**job.info(), "lines": job.since(since)}

    def shutdown(self):
        for j in self.jobs.values():
            if j.rc is None and j.proc:
                j.proc.terminate()


def discover_volumes(con=None):
    """Mount points worth offering in the scan form."""
    out = []
    seen = set()
    known = {}
    if con is not None:
        known = {r["mount"]: r["label"] for r in
                 con.execute("SELECT mount, label FROM volumes WHERE mount IS NOT NULL")}
    cands = []
    vol_dir = "/Volumes"
    if os.path.isdir(vol_dir):
        for n in sorted(os.listdir(vol_dir)):
            if n.startswith("."):
                continue          # .timemachine and friends are not volumes to index
            p = os.path.join(vol_dir, n)
            if os.path.isdir(p) and not os.path.islink(p):
                cands.append(p)
    cands.append(os.path.expanduser("~"))
    for p in cands:
        if p in seen:
            continue
        seen.add(p)
        try:
            st = os.statvfs(p)
            cap, free = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
        except OSError:
            cap = free = None
        out.append({"mount": p, "name": os.path.basename(p.rstrip("/")) or "/",
                    "label": known.get(p), "capacity": cap, "free": free,
                    "indexed": p in known})
    return out
