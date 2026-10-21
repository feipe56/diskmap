# SPDX-License-Identifier: GPL-3.0-or-later
"""Previews for the files that are not still images.

`images.py` is the specialist: it measures pixels, computes a perceptual hash
and groups near-duplicates. None of that applies to a spreadsheet. What the
panel needs from a video, a document or an archive is narrower -- something to
look at -- so this module only produces that, and says plainly when it cannot.

Two renderers, both already on the machine, and an honest third option:

  qlmanage   QuickLook, the same thing Finder uses for the space bar. Renders
             the first page of a document and a frame of the formats
             AVFoundation understands. Zero dependencies: it ships with macOS.
  ffmpeg     only if it happens to be installed, for the video formats
             QuickLook has no previewer for -- mkv and avi are the two that
             matter most in a real archive. Never required: the same pattern the
             7z action uses, where an optional binary adds a capability and its
             absence costs a feature, not an error.
  nothing    an archive, an installer, a disk image. The panel draws an icon
             from the extension; inventing a thumbnail for a .zip would be
             decoration pretending to be information.

The extension lists below were measured on a real archive, not guessed:
QuickLook *hangs* on the formats it cannot preview -- eight seconds each for
mkv, avi, zip, rar, 7z, iso, exe, epub -- so calling it on everything and
waiting would take days. It is only ever called on what it is known to render,
and still under a timeout, because the next unknown format is always out there.
"""
import os, shutil, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor

from . import classify, db
from .images import THUMB_DIR, THUMB_PX

SCHEMA = """
-- Previews for everything that is not a still image. Images keep their own
-- table because they carry what nothing else does (perceptual hash, libraries,
-- visual clustering); folding them together would mean a dhash column that is
-- NULL for most rows and a table name that lies about what it holds.
CREATE TABLE IF NOT EXISTS media_meta (
  file   INTEGER PRIMARY KEY REFERENCES files(id),
  cls    TEXT,          -- content class at the time of the preview
  thumb  TEXT,          -- path relative to THUMB_DIR, NULL when none
  engine TEXT,          -- which renderer produced it, or why none did
  done_at INTEGER,
  note   TEXT
);
CREATE INDEX IF NOT EXISTS mm_cls ON media_meta(cls);
"""

QL_TIMEOUT = 20
FF_TIMEOUT = 25

# Measured: QuickLook renders these. Anything else it either refuses fast or
# hangs, and hanging is the expensive failure.
QL_EXTS = {
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv", "rtfd",
    "pages", "numbers", "key", "psd", "ai", "eps",
    "mp4", "mov", "m4v", "3gp", "mp3", "m4a", "aac", "wav", "aiff",
}
# ffmpeg handles these when it is around; QuickLook does not.
FF_EXTS = {"mkv", "avi", "wmv", "flv", "webm", "mpg", "mpeg", "ts", "m2ts",
           "vob", "ogv", "divx", "rm", "rmvb", "asf", "mts"}


def have_ffmpeg():
    return shutil.which("ffmpeg") is not None


def engine_for(ext):
    """Which renderer can show this extension, or None."""
    e = (ext or "").lower().lstrip(".")
    if e in QL_EXTS:
        return "ql"
    if e in FF_EXTS and have_ffmpeg():
        return "ffmpeg"
    return None


def _thumb_rel(fid):
    """Same two-level fan-out images.py uses: one flat directory with tens of
    thousands of entries is slow to list on any filesystem."""
    return os.path.join("%02x" % (fid % 256), "%d.png" % fid)


def _ql(abspath, dest):
    """QuickLook writes '<name>.png' into a directory of its choosing, so it
    gets a directory of ours and we move the single file it produced."""
    out = os.path.dirname(dest)
    os.makedirs(out, exist_ok=True)
    antes = set(os.listdir(out))
    subprocess.run(["qlmanage", "-t", "-s", str(THUMB_PX), "-o", out, abspath],
                   capture_output=True, timeout=QL_TIMEOUT)
    novos = [f for f in os.listdir(out) if f not in antes and f.endswith(".png")]
    if not novos:
        return False
    os.replace(os.path.join(out, novos[0]), dest)
    for f in novos[1:]:
        os.remove(os.path.join(out, f))
    return True


def _duration(abspath):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "format=duration", "-of", "default=nw=1:nk=1", abspath],
                           capture_output=True, text=True, timeout=FF_TIMEOUT)
        return float((r.stdout or "").strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def _ffmpeg(abspath, dest):
    """A frame from about a tenth of the way in.

    A fixed offset does not work: seeking to 30s in a four-second clip produces
    an empty file and no error, which is how this looked like a broken renderer
    until the clip lengths were checked. And the first frame is often black or a
    title card, so a tenth in is a better picture of the content.
    """
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    dur = _duration(abspath)
    tentativas = [max(1.0, min(dur * 0.1, 60.0))] if dur > 2 else []
    tentativas.append(0)
    for ss in tentativas:
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", str(ss),
                        "-i", abspath, "-frames:v", "1",
                        "-vf", "scale=%d:-1" % THUMB_PX, "-y", dest],
                       capture_output=True, timeout=FF_TIMEOUT)
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            return True
        if os.path.exists(dest):
            os.remove(dest)
    return False


def tem_capa(abspath):
    """True when the audio file carries an embedded picture.

    QuickLook renders audio, but for a file without cover art it returns its own
    generic music note -- byte-identical every time, 6 KB, and indistinguishable
    on screen from a real cover. Producing that costs a process per file
    (~0.4s, which is three hours across a real music collection) to end up
    worse than the icon the panel draws for free, because a fake cover tells
    you nothing while looking like it does.

    `ffprobe` answers the actual question in 0.06s. Without ffmpeg installed the
    answer is "do not know", and not knowing means not rendering: the icon is
    honest, the placeholder is not.
    """
    if not shutil.which("ffprobe"):
        return None
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v",
                            "-show_entries", "stream_disposition=attached_pic",
                            "-of", "default=nw=1:nk=1", abspath],
                           capture_output=True, text=True, timeout=FF_TIMEOUT)
        return "1" in (r.stdout or "")
    except (OSError, subprocess.SubprocessError):
        return None


def preview_one(fid, abspath, ext, cls):
    """(cls, thumb_rel, engine, note) for one file. Never raises."""
    eng = engine_for(ext)
    if eng is None:
        return (cls, None, "nenhum", "sem renderizador para .%s" % (ext or "?"))
    if not os.path.isfile(abspath):
        return (cls, None, "ausente", "não está no disco")
    if cls == "audio":
        capa = tem_capa(abspath)
        if capa is None:
            return (cls, None, "nenhum", "sem ffprobe para saber se há capa "
                                         "embutida; o painel desenha o ícone")
        if not capa:
            return (cls, None, "sem-capa", "não tem capa embutida")
    rel = _thumb_rel(fid)
    dest = os.path.join(THUMB_DIR, rel)
    try:
        ok = _ql(abspath, dest) if eng == "ql" else _ffmpeg(abspath, dest)
    except subprocess.TimeoutExpired:
        return (cls, None, eng, "o renderizador passou do tempo limite")
    except OSError as e:
        return (cls, None, eng, "erro: %s" % e)
    if not ok:
        return (cls, None, eng, "o renderizador não produziu imagem")
    return (cls, rel, eng, None)


def pending(con, cls=None, limit=None, refresh=False):
    """Files that still need a preview attempt.

    `refresh=False` skips what was already tried, including what failed: a
    second pass over thousands of archives would spend minutes rediscovering
    that a .zip has no previewer.
    """
    con.executescript(SCHEMA)
    q = ("SELECT f.id, f.ext, f.cls, f.size, v.mount, d.relpath, f.name "
         "FROM files f JOIN dirs d ON d.id=f.dir JOIN volumes v ON v.id=f.vol "
         "WHERE v.mount IS NOT NULL AND f.cls IS NOT NULL AND f.cls <> 'image'"
         " AND " + classify.sql_nao_sidecar("f.name"))
    args = []
    if cls:
        q += " AND f.cls = ?"
        args.append(cls)
    if not refresh:
        q += " AND f.id NOT IN (SELECT file FROM media_meta)"
    q += " ORDER BY f.size DESC"
    if limit:
        q += " LIMIT %d" % int(limit)
    out = []
    for r in con.execute(q, args):
        if engine_for(r["ext"]) is None:
            continue              # sem renderizador: não gasta processo
        rel = ("%s/%s" % (r["relpath"], r["name"])) if r["relpath"] else r["name"]
        out.append((r["id"], os.path.join(r["mount"], rel), r["ext"], r["cls"],
                    r["size"]))
    return out


def enrich(con, cls=None, limit=None, refresh=False, progress=True, lote=200,
           workers=6, min_size=0):
    """Render previews, in parallel, committing as it goes.

    Serial, this is hours: a document takes about 0.7s and an archive has
    hundreds of thousands of them. Each render is one external process that
    spends its life waiting on disk, so threads are the right shape -- the same
    reason `images.enrich` uses a pool.

    Six by default and not more: every worker is a `qlmanage` or an `ffmpeg`
    reading from the same external disk, and past a point they only queue for
    the same head.

    In batches on purpose: a run that only commits at the end throws away an
    hour of work when it is cancelled at minute fifty-nine.
    """
    con.executescript(SCHEMA)
    os.makedirs(THUMB_DIR, exist_ok=True)
    fila = [t for t in pending(con, cls, limit, refresh) if t[4] >= min_size]
    if progress:
        print("acervo: %d arquivos, %d processos%s" % (
            len(fila), workers,
            "" if have_ffmpeg() else " (sem ffmpeg: mkv, avi, wmv, flv e webm "
                                    "ficam com ícone)"), file=sys.stderr)
    if not fila:
        return 0, 0
    feitos = falhou = 0
    t0 = time.time()
    por_fazer = []

    def trabalha(t):
        fid, caminho, ext, klass, _sz = t
        return (fid,) + preview_one(fid, caminho, ext, klass)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for n, (fid, k, rel, eng, note) in enumerate(ex.map(trabalha, fila), 1):
            por_fazer.append((fid, k, rel, eng, int(time.time()), note))
            if rel:
                feitos += 1
            else:
                falhou += 1
            if len(por_fazer) >= lote:
                _grava(con, por_fazer)
                por_fazer = []
                if progress:
                    el = time.time() - t0
                    resta = (len(fila) - n) / max(n / el, 0.001)
                    print("  %d/%d · %d com miniatura · %.0fs · faltam ~%.0f min"
                          % (n, len(fila), feitos, el, resta / 60), file=sys.stderr)
    _grava(con, por_fazer)
    if progress:
        print("acervo: %d miniaturas, %d sem · %.0fs"
              % (feitos, falhou, time.time() - t0), file=sys.stderr)
    return feitos, falhou


def _grava(con, linhas):
    if not linhas:
        return
    con.executemany(
        "INSERT INTO media_meta (file,cls,thumb,engine,done_at,note) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(file) DO UPDATE SET "
        "cls=excluded.cls, thumb=excluded.thumb, engine=excluded.engine, "
        "done_at=excluded.done_at, note=excluded.note", linhas)
    con.commit()


def stats(con):
    con.executescript(SCHEMA)
    out = {"ffmpeg": have_ffmpeg(), "classes": []}
    for r in con.execute(
            "SELECT f.cls, COUNT(*) n, COALESCE(SUM(f.size),0) b, "
            "SUM(CASE WHEN m.thumb IS NOT NULL THEN 1 ELSE 0 END) com "
            "FROM files f LEFT JOIN media_meta m ON m.file=f.id "
            "WHERE f.cls IS NOT NULL GROUP BY f.cls ORDER BY n DESC"):
        out["classes"].append(dict(r))
    return out
