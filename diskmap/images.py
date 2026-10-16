# SPDX-License-Identifier: GPL-3.0-or-later
"""Visual layer: dimensions, thumbnails, perceptual hash, and a library per image.

Everything here leans on `sips`, which ships with macOS, so the zero-dependency
rule holds. sips reads every format in this collection - png, jpg, dds, tga, psd,
tif, exr, hdr, webp, bmp - though it cannot extract pixels from dds, so dds gets
metadata only and never a thumbnail or a visual hash.

Two sips calls per image: one makes the thumbnail and prints the dimensions, the
second turns that thumbnail into a 9x9 PNG that we decode with zlib to build a
dHash. Working from the thumbnail instead of the original keeps the second call
cheap.
"""
import os, re, struct, subprocess, sys, tempfile, time, zlib
from concurrent.futures import ThreadPoolExecutor
from . import db

THUMB_DIR = os.path.expanduser("~/.diskmap/thumbs")
THUMB_PX = 320
HASH_N = 9                      # 9x9 grey -> 8x8 comparisons -> 64 bits
NO_PIXELS = {"dds"}             # sips knows the format but cannot decode it
SIPS_TIMEOUT = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS image_meta (
  file   INTEGER PRIMARY KEY REFERENCES files(id),
  width  INTEGER,
  height INTEGER,
  format TEXT,
  dhash  TEXT,
  thumb  TEXT,          -- path relative to THUMB_DIR, NULL when none
  lib    TEXT,          -- which library this image belongs to
  done_at INTEGER,
  note   TEXT           -- why it has no thumbnail or hash
);
CREATE INDEX IF NOT EXISTS im_lib   ON image_meta(lib);
CREATE INDEX IF NOT EXISTS im_dhash ON image_meta(dhash);
CREATE INDEX IF NOT EXISTS im_dims  ON image_meta(width, height);

CREATE TABLE IF NOT EXISTS img_groups (
  id     INTEGER PRIMARY KEY,
  kind   TEXT,          -- 'visual'
  n      INTEGER,
  bytes  INTEGER,       -- total held by the group
  wasted INTEGER        -- all but the largest member
);
CREATE TABLE IF NOT EXISTS img_members (
  grp  INTEGER NOT NULL REFERENCES img_groups(id),
  file INTEGER NOT NULL REFERENCES files(id),
  dist INTEGER          -- hamming distance to the group's anchor
);
CREATE INDEX IF NOT EXISTS igm_grp ON img_members(grp);
"""

# ---------------------------------------------------------------- libraries
#
# Ordered: the first rule that matches wins, so the specific ones come first.
# Each entry is (library, path regex or None, extension set or None).
#
# Anchor every path rule with `(^|/)`, never a bare leading slash: relpath is
# relative to the volume root, so `FBX/the-bathroom/x.png` has no slash in front
# of `FBX` and a `/fbx/` pattern silently misses it.
#
# Three tiers, in this order:
#
#   ORIGIN   unambiguous origins - a launcher's install tree, an AI tool's
#            output folder, a screenshot. These win over everything because
#            they say where the file came from.
#   user     your own rules, from ~/.diskmap/rules.json. The names of your
#            projects and the folder you dump old archives into are yours, not
#            the tool's, so they live in your file and not in this repository.
#   GENERIC  what the file *is* (asset, texture, 3D model), then, last, rules
#            that only say *where* it sits (Downloads, Documents) - those must
#            never outrank a rule that identifies the content.
ORIGIN = [
    ("jogo-instalado", r"(steamlibrary/steamapps|(^|/)steamapps/|(^|/)epic games/|(^|/)origin games/)", None),
    ("ia-gerada",      r"(invokeai|stable[-_ ]?diffusion|(^|/)outputs/images|automatic1111|comfyui)", None),
    ("ia-gerada",      r"chatgpt image", None),
    ("screenshot",     r"(screenshot|screen recording|captura|(^|/)screenshots?/|radeon relive|(^|/)captures/)", None),
]
GENERIC = [
    ("arquivo-morto",  r"(^|/)(arquivo[-_ ]morto|archives?|archived|old[-_ ]files)/", None),
    ("engine-sdk",     r"(ue_5\.|unrealengine|(^|/)engine/content|vulkansdk|epic games/launcher)", None),
    ("asset-pack",     r"(unrealassets|unity3d packages|asset[-_ ]?pack|(^|/)featurepacks/|"
                       r"(^|/)templates?/|(^|/)assets?/|starterpack|(^|/)polygon(^|/|_))", None),
    ("textura",        r"((^|/)textures?/|(^|/)materials?/|(^|/)mat_|_textures/|(^|/)tex/|"
                       r"(^|/)skins?/)", None),
    ("mapa-arte",      r"((^|/)docs/art/|(^|/)maps?/|(^|/)art/)", None),
    ("modelo-3d",      r"((^|/)fbx/|models3d|(^|/)meshes?/|\.fbm/|(^|/)source/|\.stl$)", None),
    ("sistema",        r"((^|/)appdata/|(^|/)library/|site-packages|program files|programdata|"
                       r"(^|/)windows/|node_modules|\.git/|(^|/)osgeo4w/|\.vscode|(^|/)python3|"
                       r"(^|/)recovery/|\$recycle)", None),
    ("foto",           r"((^|/)dcim/|(^|/)camera/|photos library|(^|/)fotos/)", None),
    ("foto",           None, {"heic", "cr2", "nef", "arw", "dng"}),
    # location-only rules, deliberately last
    ("download",       r"(^|/)downloads?/", None),
    ("documento",      r"(^|/)(documents?|documentos)/", None),
    ("foto",           r"(^|/)pictures/", None),
]

_LIB_RX = None


def _compile(rs):
    return [(lib, re.compile(rx, re.I) if rx else None, exts)
            for lib, rx, exts in rs]


def builtin_rules():
    """The shipped rules alone, with nothing from this machine.

    The tests pin these: a test that read ~/.diskmap/rules.json would pass or
    fail depending on whose laptop it ran on.
    """
    return _compile(ORIGIN + GENERIC)


def rules():
    """The compiled rule list, with your own spliced in. Built once."""
    global _LIB_RX
    if _LIB_RX is None:
        from . import userconf
        _LIB_RX = _compile(ORIGIN + userconf.libraries() + GENERIC)
    return _LIB_RX


LIB_LABEL = {
    "jogo-instalado": "Jogo instalado",
    "ia-gerada": "Gerada por IA",
    "screenshot": "Screenshot e gravação",
    "arquivo-morto": "Arquivo morto",
    "engine-sdk": "Engine e SDK",
    "asset-pack": "Asset pack",
    "textura": "Textura",
    "mapa-arte": "Mapa e arte de jogo",
    "modelo-3d": "Modelo 3D",
    "projeto-dev": "Projeto seu",
    "sistema": "Sistema e dependência",
    "download": "Download",
    "foto": "Foto",
    "documento": "Documento",
    "outros": "Não classificado",
}


def classify(relpath, name, ext, rs=None):
    """Which library an image belongs to. Path beats extension.

    `rs` overrides the rule list, which is how the tests ask for the shipped
    rules without this machine's additions.
    """
    full = f"{relpath}/{name}".lower() if relpath else name.lower()
    for lib, rx, exts in (rs if rs is not None else rules()):
        if rx is not None and rx.search(full):
            return lib
        if exts is not None and ext in exts:
            return lib
    return "outros"


def reclassify(con, progress=True):
    """Re-file every processed image without redoing any sips work.

    Classification is a pure function of the path, so a rule change costs
    seconds - it must never mean re-reading 38k files.
    """
    con.executescript(SCHEMA)
    rows = [(r["file"], r["relpath"], r["name"], r["ext"], r["lib"]) for r in con.execute(
        "SELECT m.file, d.relpath, f.name, f.ext, m.lib FROM image_meta m "
        "JOIN files f ON f.id=m.file JOIN dirs d ON d.id=f.dir")]
    upd, moved = [], 0
    for fid, relpath, name, ext, old in rows:
        new = classify(relpath, name, ext)
        if new != old:
            moved += 1
        upd.append((new, fid))
    con.executemany("UPDATE image_meta SET lib=? WHERE file=?", upd)
    con.commit()
    if progress:
        print(f"reclassificadas {len(upd)} imagens ({moved} mudaram de biblioteca)",
              file=sys.stderr)
    return moved


# ---------------------------------------------------------------- sips work
def _sips(args, size=0):
    """A 400 MB PSD at 18300x24900 needs more than the default budget, so the
    timeout scales with the file and caps at three minutes."""
    t = min(SIPS_TIMEOUT + size // (10 << 20), 180)
    return subprocess.run(["sips", *args], capture_output=True, text=True, timeout=t)


def _props(out):
    d = {}
    for line in out.splitlines():
        if ":" in line:
            k, _, v = line.strip().partition(":")
            d[k.strip()] = v.strip()
    return d


def _decode_png_grey(data):
    """Minimal PNG decode to grey rows. Enough for a 9x9 thumbnail."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    i, w, h, ct, idat = 8, None, None, None, b""
    while i + 8 <= len(data):
        ln = struct.unpack(">I", data[i:i + 4])[0]
        typ = data[i + 4:i + 8]
        body = data[i + 8:i + 8 + ln]
        i += 12 + ln
        if typ == b"IHDR":
            w, h, _bd, ct = struct.unpack(">IIBB", body[:10])
        elif typ == b"IDAT":
            idat += body
        elif typ == b"IEND":
            break
    if not w or not idat:
        return None
    raw = zlib.decompress(idat)
    ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ct, 3)
    stride, rows, prev, p = w * ch, [], bytearray(w * ch), 0
    for _ in range(h):
        if p >= len(raw):
            break
        f = raw[p]; p += 1
        line = bytearray(raw[p:p + stride]); p += stride
        if len(line) < stride:
            break
        for x in range(stride):
            a = line[x - ch] if x >= ch else 0
            b = prev[x]
            c = prev[x - ch] if x >= ch else 0
            if f == 1:   line[x] = (line[x] + a) & 255
            elif f == 2: line[x] = (line[x] + b) & 255
            elif f == 3: line[x] = (line[x] + ((a + b) >> 1)) & 255
            elif f == 4:
                pp = a + b - c
                pa, pb, pc = abs(pp - a), abs(pp - b), abs(pp - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[x] = (line[x] + pr) & 255
        rows.append([sum(line[x:x + min(ch, 3)]) // min(ch, 3)
                     for x in range(0, stride, ch)])
        prev = line
    return rows


def _dhash_from(path):
    """dHash via a 9x9 PNG that sips renders from `path`."""
    out = None
    try:
        out = tempfile.mktemp(suffix=".png", dir=tempfile.gettempdir())
        r = _sips(["-s", "format", "png", "-z", str(HASH_N), str(HASH_N),
                   path, "--out", out])
        if r.returncode != 0 or not os.path.exists(out):
            return None
        rows = _decode_png_grey(open(out, "rb").read())
        if not rows or len(rows) < 8:
            return None
        bits = []
        for row in rows[:8]:
            for x in range(min(8, len(row) - 1)):
                bits.append("1" if row[x] < row[x + 1] else "0")
        if len(bits) < 64:
            bits += ["0"] * (64 - len(bits))
        return f"{int(''.join(bits[:64]), 2):016x}"
    except (OSError, subprocess.TimeoutExpired, zlib.error, struct.error, ValueError):
        return None
    finally:
        if out and os.path.exists(out):
            try: os.unlink(out)
            except OSError: pass


def process_one(fid, abspath, ext, thumb_rel, size=0):
    """Returns the image_meta row for one file. Never raises."""
    res = {"file": fid, "width": None, "height": None, "format": None,
           "dhash": None, "thumb": None, "note": None}
    if not os.path.exists(abspath):
        res["note"] = "arquivo não encontrado"
        return res
    thumb_abs = os.path.join(THUMB_DIR, thumb_rel)
    os.makedirs(os.path.dirname(thumb_abs), exist_ok=True)
    want_pixels = ext not in NO_PIXELS
    # sips refuses to read properties and write an output in one invocation
    # ("Error 6: cannot get properties and modify file in the same invocation"),
    # so the metadata and the thumbnail are two separate calls.
    try:
        r = _sips(["-g", "pixelWidth", "-g", "pixelHeight", "-g", "format", abspath], size)
    except (subprocess.TimeoutExpired, OSError) as e:
        res["note"] = f"sips: {e}"
        return res
    p = _props(r.stdout)
    for k, col in (("pixelWidth", "width"), ("pixelHeight", "height")):
        v = p.get(k)
        if v and v.isdigit():
            res[col] = int(v)
    res["format"] = p.get("format")

    if want_pixels:
        try:
            r = _sips(["-s", "format", "jpeg", "-Z", str(THUMB_PX),
                       abspath, "--out", thumb_abs], size)
        except (subprocess.TimeoutExpired, OSError) as e:
            res["note"] = f"miniatura: {e}"
            return res
    if want_pixels and os.path.exists(thumb_abs) and os.path.getsize(thumb_abs) > 0:
        res["thumb"] = thumb_rel
        res["dhash"] = _dhash_from(thumb_abs)
        if not res["dhash"]:
            res["note"] = "sem hash visual"
    elif want_pixels:
        res["note"] = (r.stderr or "sips não gerou miniatura").strip()[:160]
        if os.path.exists(thumb_abs):
            try: os.unlink(thumb_abs)
            except OSError: pass
    else:
        res["note"] = "formato sem extração de pixel (dds)"
    return res


# ---------------------------------------------------------------- driver
def pending(con, min_size, include_dds, refresh):
    q = """SELECT f.id, f.name, f.ext, f.size, d.relpath, v.mount
           FROM files f JOIN dirs d ON d.id=f.dir JOIN volumes v ON v.id=f.vol
           WHERE f.cls='image' AND f.size >= ? AND v.mount IS NOT NULL"""
    args = [min_size]
    if not include_dds:
        q += " AND f.ext NOT IN ('dds','ico')"
    if not refresh:
        q += " AND f.id NOT IN (SELECT file FROM image_meta)"
    q += " ORDER BY f.size DESC"
    out = []
    for r in con.execute(q, args):
        rel = f"{r['relpath']}/{r['name']}" if r["relpath"] else r["name"]
        out.append((r["id"], os.path.join(r["mount"], rel), r["ext"],
                    r["relpath"], r["name"], r["size"]))
    return out


def enrich(con, min_size=100_000, workers=8, include_dds=False, refresh=False,
           limit=None, progress=True):
    con.executescript(SCHEMA)
    con.commit()
    os.makedirs(THUMB_DIR, exist_ok=True)
    todo = pending(con, min_size, include_dds, refresh)
    if limit:
        todo = todo[:limit]
    if not todo:
        if progress:
            print("nada novo para processar", file=sys.stderr)
        return 0, 0
    total = len(todo)
    if progress:
        print(f"processando {total} imagens com {workers} processos…", file=sys.stderr)

    SQL = ("INSERT INTO image_meta (file,width,height,format,dhash,thumb,lib,done_at,note) "
           "VALUES (:file,:width,:height,:format,:dhash,:thumb,:lib,:at,:note) "
           "ON CONFLICT(file) DO UPDATE SET width=excluded.width, height=excluded.height, "
           "format=excluded.format, dhash=excluded.dhash, thumb=excluded.thumb, "
           "lib=excluded.lib, done_at=excluded.done_at, note=excluded.note")
    t0 = time.time()

    def work(item):
        fid, abspath, ext, relpath, name, size = item
        res = process_one(fid, abspath, ext, f"{fid % 256:02x}/{fid}.jpg", size)
        res["lib"] = classify(relpath, name, ext)
        return res

    # Flushed in batches as results arrive, not at the end: this run takes over an
    # hour, and a crash or a cancel at 90% must not throw away 90% of the work.
    # The scan is resumable for the same reason - `pending` skips what is stored.
    batch, saved, withhash = [], 0, 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for res in ex.map(work, todo):
            batch.append({**res, "at": int(time.time())})
            withhash += 1 if res["dhash"] else 0
            if len(batch) >= 200:
                con.executemany(SQL, batch)
                con.commit()
                saved += len(batch)
                batch.clear()
                if progress:
                    el = time.time() - t0
                    rate = saved / max(el, 0.01)
                    print(f"  {saved}/{total}  {el:.0f}s  "
                          f"{rate:.1f}/s  (~{(total - saved) / max(rate, .01) / 60:.0f} min "
                          f"restantes)", file=sys.stderr)
    if batch:
        con.executemany(SQL, batch)
        con.commit()
        saved += len(batch)
    if progress:
        print(f"concluído: {saved} imagens, {withhash} com hash visual, "
              f"{time.time() - t0:.0f}s", file=sys.stderr)
    return saved, withhash


# ---------------------------------------------------------------- near dupes
def _hamming(a, b):
    return bin(a ^ b).count("1")


MAX_GROUP = 60          # beyond this a "group" stops being reviewable
HASH_FLOOR = 8          # a dHash with almost no set bits is a flat image
DEGENERATE_SHARE = 40   # one hash on more than this many files is a placeholder


def cluster(con, threshold=10, progress=True):
    """Group images whose dHash is within `threshold` bits of a group anchor.

    Star clustering, not transitive closure. The first version used union-find,
    and chaining (A~B, B~C so A~C) collapsed 7868 unrelated images - flat
    textures and normal maps all sit near each other in dHash space - into one
    group that was neither reviewable nor safe to act on. Here each group has an
    anchor and every member is within `threshold` of *that anchor*, so a group
    stays tight no matter how dense the neighbourhood is.

    Banded LSH still does the candidate lookup: a 64-bit hash splits into four
    16-bit bands and only images sharing a band get compared, which keeps this
    at seconds over 38k hashes instead of an O(n^2) sweep.
    """
    con.executescript(SCHEMA)
    con.execute("DELETE FROM img_members")
    con.execute("DELETE FROM img_groups")
    rows = [(r["file"], int(r["dhash"], 16), r["size"]) for r in con.execute(
        "SELECT m.file, m.dhash, f.size FROM image_meta m JOIN files f ON f.id=m.file "
        "WHERE m.dhash IS NOT NULL")]
    if not rows:
        if progress:
            print("nenhum hash visual ainda - rode 'diskmap images' primeiro",
                  file=sys.stderr)
        return 0
    t0 = time.time()

    # Drop hashes that carry no information: a nearly blank image, and any hash
    # shared by a crowd (engine placeholders, solid-colour tiles). They match
    # everything and would dominate every group.
    freq = {}
    for _fid, h, _sz in rows:
        freq[h] = freq.get(h, 0) + 1
    usable, skipped = [], 0
    for fid, h, sz in rows:
        bits = bin(h).count("1")
        if bits < HASH_FLOOR or bits > 64 - HASH_FLOOR or freq[h] > DEGENERATE_SHARE:
            skipped += 1
            continue
        usable.append((fid, h, sz))

    bands = {}
    for fid, h, _sz in usable:
        for b in range(4):
            bands.setdefault((b, (h >> (16 * b)) & 0xFFFF), []).append(fid)

    size_of = {fid: sz for fid, _, sz in usable}
    hash_of = {fid: h for fid, h, _ in usable}
    # biggest first, so the survivor a group suggests is the highest-resolution copy
    order = sorted(size_of, key=lambda f: -size_of[f])

    taken = set()
    kept = pairs = 0
    for anchor in order:
        if anchor in taken:
            continue
        ha = hash_of[anchor]
        cands = set()
        for b in range(4):
            for fid in bands.get((b, (ha >> (16 * b)) & 0xFFFF), ()):
                if fid != anchor and fid not in taken:
                    cands.add(fid)
        members = [anchor]
        for fid in cands:
            pairs += 1
            if _hamming(ha, hash_of[fid]) <= threshold:
                members.append(fid)
                if len(members) >= MAX_GROUP:
                    break
        if len(members) < 2:
            taken.add(anchor)
            continue
        members.sort(key=lambda f: -size_of[f])
        taken.update(members)
        tot = sum(size_of[f] for f in members)
        cur = con.execute(
            "INSERT INTO img_groups (kind,n,bytes,wasted) VALUES ('visual',?,?,?)",
            (len(members), tot, tot - size_of[members[0]]))
        con.executemany("INSERT INTO img_members (grp,file,dist) VALUES (?,?,?)",
                        [(gid_f, f, _hamming(ha, hash_of[f]))
                         for gid_f in (cur.lastrowid,) for f in members])
        kept += 1
    con.commit()
    if progress:
        print(f"{kept} grupos visuais de {len(usable)} imagens "
              f"({skipped} com hash sem informação, {pairs} comparações, "
              f"{time.time() - t0:.1f}s)", file=sys.stderr)
    return kept


def stats(con):
    con.executescript(SCHEMA)
    out = {"libs": [], "total": 0, "bytes": 0, "with_hash": 0, "no_thumb": 0}
    for r in con.execute(
            "SELECT m.lib, COUNT(*) n, SUM(f.size) b, "
            "SUM(CASE WHEN m.thumb IS NOT NULL THEN 1 ELSE 0 END) t "
            "FROM image_meta m JOIN files f ON f.id=m.file GROUP BY m.lib ORDER BY b DESC"):
        out["libs"].append({"lib": r["lib"], "label": LIB_LABEL.get(r["lib"], r["lib"]),
                            "n": r["n"], "bytes": r["b"] or 0, "thumbs": r["t"]})
    r = con.execute("SELECT COUNT(*) n, SUM(f.size) b, "
                    "SUM(CASE WHEN m.dhash IS NOT NULL THEN 1 ELSE 0 END) hh, "
                    "SUM(CASE WHEN m.thumb IS NULL THEN 1 ELSE 0 END) nt "
                    "FROM image_meta m JOIN files f ON f.id=m.file").fetchone()
    out.update(total=r["n"] or 0, bytes=r["b"] or 0,
               with_hash=r["hh"] or 0, no_thumb=r["nt"] or 0)
    g = con.execute("SELECT COUNT(*) n, SUM(wasted) w FROM img_groups").fetchone()
    out["groups"] = {"n": g["n"] or 0, "wasted": g["w"] or 0}
    return out
