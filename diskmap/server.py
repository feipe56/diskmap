# SPDX-License-Identifier: GPL-3.0-or-later
"""Local HTTP server: JSON API over the index, plus the static frontend.

Binds to 127.0.0.1 only. Read-only: no endpoint moves or deletes a file.
"""
import json, mimetypes, os, secrets, sqlite3, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from . import actions as actions_mod
from . import db
from . import jobs as jobs_mod
from .analyze import BLOB_STALE_YEARS, recoverable

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


def search(con, term, limit=200):
    """Find a file across every volume, including ones not plugged in."""
    like = f"%{term}%"
    rows = [dict(r) for r in con.execute(
        "SELECT v.label, d.relpath, f.name, f.size, f.mtime, f.ext "
        "FROM files f JOIN dirs d ON d.id=f.dir JOIN volumes v ON v.id=f.vol "
        "WHERE f.name LIKE ? COLLATE NOCASE ORDER BY f.size DESC LIMIT ?",
        (like, limit))]
    dirs_ = [dict(r) for r in con.execute(
        "SELECT v.label, r.relpath, r.bytes, r.files FROM dir_rollup r "
        "JOIN volumes v ON v.id=r.vol WHERE r.relpath LIKE ? COLLATE NOCASE "
        "ORDER BY r.bytes DESC LIMIT 40", (like,))]
    return {"files": rows, "dirs": dirs_}


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


def img_overview(con):
    from . import images as im
    return im.stats(con)


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
    """Visually similar clusters, biggest waste first."""
    limit = min(int(q.get("limit", ["30"])[0]), 120)
    offset = int(q.get("offset", ["0"])[0])
    out = []
    for g in con.execute("SELECT id, n, bytes, wasted FROM img_groups "
                         "ORDER BY wasted DESC LIMIT ? OFFSET ?", (limit, offset)):
        members = [dict(r) for r in con.execute(
            """SELECT f.id, f.name, f.size, f.ext, d.relpath, v.label, v.mount,
                      m.width, m.height, m.lib, im2.dist
               FROM img_members im2 JOIN files f ON f.id=im2.file
               JOIN image_meta m ON m.file=f.id JOIN dirs d ON d.id=f.dir
               JOIN volumes v ON v.id=f.vol WHERE im2.grp=?
               ORDER BY f.size DESC""", (g["id"],))]
        out.append({**dict(g), "members": members})
    tot = con.execute("SELECT COUNT(*) n, SUM(wasted) w FROM img_groups").fetchone()
    return {"groups": out, "count": tot["n"] or 0, "wasted": tot["w"] or 0}


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


ROUTES = {
    "/api/overview": lambda con, q: overview(con),
    "/api/dups":     lambda con, q: dup_list(con, int(q.get("limit", ["60"])[0]),
                                             int(q.get("offset", ["0"])[0]),
                                             (q.get("verified") or [None])[0],
                                             int(q.get("min_size", ["0"])[0])),
    "/api/overlap":  lambda con, q: overlap(con, int(q.get("limit", ["40"])[0])),
    "/api/archives": lambda con, q: archives(con, int(q.get("limit", ["40"])[0])),
    "/api/dirs":     lambda con, q: dirs(con, (q.get("vol") or [None])[0],
                                         int(q.get("depth", ["1"])[0]),
                                         int(q.get("limit", ["50"])[0])),
    "/api/search":   lambda con, q: search(con, (q.get("q") or [""])[0],
                                           int(q.get("limit", ["200"])[0])),
    "/api/biggest":  lambda con, q: biggest(con, int(q.get("limit", ["60"])[0])),
    "/api/cold":     lambda con, q: cold(con, limit=int(q.get("limit", ["60"])[0])),
    "/api/images":   lambda con, q: img_list(con, q),
    "/api/img-overview": lambda con, q: img_overview(con),
    "/api/img-groups":   lambda con, q: img_groups(con, q),
    "/api/blobs":    lambda con, q: blob_list(con, q),
}


def make_handler(db_path, token, allow_actions, origins, allow_wipe=False):
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
            if target.endswith("index.html"):
                blob = blob.replace(
                    b"</head>",
                    (f'<script>window.DISKMAP_TOKEN={json.dumps(token)};'
                     f'window.DISKMAP_ACTIONS={json.dumps(bool(allow_actions))};'
                     f'window.DISKMAP_WIPE={json.dumps(bool(allow_wipe))};'
                     f'</script></head>').encode())
            self._send(200, blob, ctype)

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
