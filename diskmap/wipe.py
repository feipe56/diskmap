# SPDX-License-Identifier: GPL-3.0-or-later
"""Zero-fill: overwrite a file's bytes with zeros, then remove it.

This is the one irreversible thing diskmap does, and it is the only place that
calls `os.remove`. Everything else moves to quarantine or to the Trash, with an
undo. So it sits behind its own flag (`serve --allow-wipe`), and it refuses more
than it accepts.

**What a zero-fill can and cannot promise.** The honest answer depends entirely
on the filesystem and the hardware under it, and on most modern setups the
answer is "less than you think":

  copy-on-write (APFS)   Writing over a file allocates *new* blocks. The old
                         ones keep the original bytes until they are reused.
                         Apple removed "Secure Empty Trash" in 10.11 for this
                         reason.
  APFS snapshots         Worse: a snapshot *pins* the old blocks on purpose, so
                         the data survives for as long as the snapshot does.
  flash (SSD/NVMe)       The controller remaps writes for wear levelling. The
                         physical pages holding the original data stay until
                         garbage collection reaches them, and nothing above the
                         controller can address them.
  exFAT/NTFS on a HDD    Here it works as advertised: no copy-on-write, and the
                         magnetic sectors really are overwritten in place.

So `assess()` runs before every wipe and says which of those you are in. When
the answer is "this cannot work", the wipe is refused rather than performed as
theatre. The only real guarantee for a whole disk is to erase the whole disk.
"""
import ctypes, os, stat, subprocess, sys

CHUNK = 4 << 20            # 4 MiB of zeros per write
F_NOCACHE = 48             # macOS fcntl: bypass the buffer cache
ZEROS = b"\0" * CHUNK

# Filesystems that overwrite in place. Anything not listed is treated as
# copy-on-write, which is the safe assumption to make about an unknown one.
INPLACE_FS = {"exfat", "ntfs", "msdos", "hfs", "ufsd_NTFS", "fusefs_txantfs"}
COW_FS = {"apfs", "zfs", "btrfs"}

# How much a zero-fill is worth here, worst to best.
LEVELS = {
    "impossivel": "o volume está montado somente-leitura; nada pode ser escrito",
    "inutil": "APFS com snapshot: o snapshot fixa os blocos antigos de propósito, "
              "então sobrescrever não alcança o dado original",
    "fraco": "APFS é copy-on-write: escrever aloca blocos novos e os antigos "
             "guardam os bytes originais até serem reusados",
    "parcial": "o filesystem sobrescreve no lugar, mas em memória flash o "
               "controlador remapeia a escrita e as páginas físicas originais "
               "só somem no garbage collection",
    "efetivo": "filesystem sem copy-on-write em disco mecânico: o setor é "
               "fisicamente sobrescrito",
}


def _run(argv, timeout=15):
    try:
        r = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout, shell=False)
        return r.returncode, (r.stdout or "")
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def _fstype(mount):
    """Filesystem name and read-only flag for the volume holding `mount`.

    The read-only answer comes from statvfs on the path itself, never from the
    mount table: macOS firmlinks `/Users` onto `/System/Volumes/Data`, so a
    longest-prefix match against the table lands on `/`, which is the sealed
    read-only system volume - and every path under the home directory would be
    declared unwritable.
    """
    try:
        ro = bool(os.statvfs(mount).f_flag & os.ST_RDONLY)
    except (OSError, AttributeError):
        ro = False
    rc, out = _run(["/sbin/mount"])
    best = None
    for line in out.splitlines():
        # /dev/disk5s1 on /Volumes/Dados (exfat, local, nodev, ...)
        if " on " not in line or "(" not in line:
            continue
        rest = line.split(" on ", 1)[1]
        point, _, opts = rest.rpartition(" (")
        if not (mount == point or mount.startswith(point.rstrip("/") + "/")):
            continue
        if best is None or len(point) > len(best[0]):
            parts = [o.strip() for o in opts.rstrip(")").split(",")]
            best = (point, parts[0] if parts else "?")
    return (best[1] if best else "?"), ro


def _rotational(mount):
    """True for a spinning disk, False for flash, None when unknown."""
    rc, out = _run(["diskutil", "info", mount])
    for line in out.splitlines():
        if "Solid State:" in line:
            v = line.split(":", 1)[1].strip().lower()
            if v.startswith("yes"):
                return False
            if v.startswith("no"):
                return True
            return None
    return None


def _snapshots(mount):
    """How many APFS snapshots pin old blocks on this volume.

    Two commands are needed: on a Time Machine destination `listlocalsnapshots`
    refuses and points at `listbackups`, and missing those would report zero
    snapshots on the one disk where they matter most.
    """
    rc, out = _run(["/usr/bin/tmutil", "listlocalsnapshots", mount])
    n = sum(1 for ln in out.splitlines() if "com.apple.TimeMachine" in ln)
    if n:
        return n
    if rc != 0 or "backup disk" in out:
        rc, out = _run(["/usr/bin/tmutil", "listbackups", "-d", mount], timeout=25)
        if rc == 0:
            return sum(1 for ln in out.splitlines() if ".backup" in ln)
    return 0


def assess(mount):
    """What a zero-fill is actually worth on this volume.

    Returns a dict with the verdict and the facts behind it, so the UI can show
    the reasoning instead of a yes/no the user has to take on faith.
    """
    fs, ro = _fstype(mount)
    low = fs.lower()
    rot = None if ro else _rotational(mount)
    snaps = _snapshots(mount) if low in COW_FS else 0

    if ro:
        level = "impossivel"
    elif low in COW_FS:
        level = "inutil" if snaps else "fraco"
    elif low in INPLACE_FS or any(k in low for k in ("ntfs", "exfat", "fat")):
        level = "efetivo" if rot else "parcial"
    else:
        level = "fraco"      # unknown filesystem: assume the worst
    return {"mount": mount, "fs": fs, "read_only": ro, "rotational": rot,
            "snapshots": snaps, "level": level, "why": LEVELS[level],
            "allowed": level != "impossivel"}


def _nocache(fd):
    """Ask macOS to bypass the buffer cache, so the verify reads the disk."""
    try:
        import fcntl
        fcntl.fcntl(fd, F_NOCACHE, 1)
        return True
    except (ImportError, OSError, ValueError):
        return False


def check(path):
    """Reasons this file must not be wiped. Empty list means it may be."""
    bad = []
    try:
        st = os.lstat(path)
    except OSError as e:
        return [f"não dá para ler: {e}"]
    if stat.S_ISLNK(st.st_mode):
        bad.append("é um link simbólico; zerar não tocaria o arquivo de destino")
    elif not stat.S_ISREG(st.st_mode):
        bad.append("não é um arquivo comum")
    if st.st_nlink > 1:
        bad.append(f"tem {st.st_nlink} hard links: zerar destrói o conteúdo "
                   f"visto por todos os outros nomes também")
    return bad


def zero_file(path, keep=False, verify=True, progress=None):
    """Overwrite every byte with zeros, verify, then remove unless keep.

    Returns a dict. Raises ValueError when `check` refuses the file: a wipe that
    cannot do what it says is never performed silently.
    """
    bad = check(path)
    if bad:
        raise ValueError("; ".join(bad))
    size = os.lstat(path).st_size
    written = 0
    fd = os.open(path, os.O_WRONLY)
    try:
        nocache = _nocache(fd)
        while written < size:
            n = min(CHUNK, size - written)
            written += os.write(fd, ZEROS if n == CHUNK else ZEROS[:n])
            if progress:
                progress(written, size)
        os.fsync(fd)
    finally:
        os.close(fd)

    checked = 0
    if verify and size:
        fd = os.open(path, os.O_RDONLY)
        try:
            _nocache(fd)
            while checked < size:
                buf = os.read(fd, CHUNK)
                if not buf:
                    break
                if buf.strip(b"\0"):
                    raise OSError(
                        f"verificação falhou: byte não-zero em {checked}. "
                        f"O arquivo NÃO foi zerado; não vou apagá-lo.")
                checked += len(buf)
        finally:
            os.close(fd)

    removed = False
    if not keep:
        os.remove(path)          # the only os.remove in the project, on purpose
        removed = True
    return {"path": path, "bytes": size, "written": written,
            "verified": checked if verify else None, "removed": removed,
            "nocache": nocache}
