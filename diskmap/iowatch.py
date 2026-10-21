# SPDX-License-Identifier: GPL-3.0-or-later
"""Quem está mexendo no disco agora.

São **duas** medidas do kernel, e elas não são a mesma coisa:

  por disco      `ioreg`, classe `IOBlockStorageDriver`: bytes lidos e escritos
                 por dispositivo, separados, acumulados desde o boot.
  por processo   `proc_pid_rusage`: bytes lidos e escritos por processo,
                 acumulados desde que ele nasceu. É o que o Monitor de Atividade
                 mostra na aba Disco.

O kernel **não cruza as duas**: o contador do processo não guarda em qual
dispositivo o byte caiu. Então a coluna de volume ao lado do processo vem do
`lsof` - quais volumes ele tem abertos neste instante - e é palpite, não prova.
Um processo com arquivo aberto em dois discos aparece nos dois.

O cruzamento exato existe (`fs_usage -w -f filesys` mostra caminho por chamada),
mas exige root, e este projeto não pede root para olhar. Quem quiser a prova
roda `sudo fs_usage` ao lado.

Três motivos para a soma dos processos não fechar com a soma dos discos, todos
esperados:

  - processo de outro usuário (root, em geral) recusa o contador sem sudo;
  - processo que nasceu e morreu dentro da janela não tem dois pontos para
    subtrair, então some da lista - mas o contador do dispositivo o inclui;
  - leitura servida pelo cache de página não toca o disco: conta como I/O do
    processo em uma medida e não na outra.
"""
import ctypes, os, subprocess, time

TIMEOUT = 10
_LIB = None
_VOLS = (0.0, {})          # (quando, {dev: [label, ...]}) - diskutil é caro
VOL_TTL = 60.0


# ---------------------------------------------------------------- por disco

def _ioreg():
    """ioreg em plist, que a stdlib lê - mesma saída do `diskutil -plist`."""
    import plistlib
    try:
        # -l é obrigatório: sem ele o filho IOMedia vem sem propriedade nenhuma,
        # inclusive o nome BSD, e a estatística fica sem dono.
        r = subprocess.run(["ioreg", "-a", "-c", "IOBlockStorageDriver",
                            "-r", "-l", "-w0"],
                           capture_output=True, timeout=TIMEOUT)
        return plistlib.loads(r.stdout) if r.stdout else []
    except (OSError, subprocess.SubprocessError, Exception):      # noqa: BLE001
        return []


def parse_ioreg(nodes):
    """{diskN: {read, write, rops, wops}} acumulado desde o boot.

    O nó que guarda a estatística é o driver, e o nome BSD mora no filho
    `IOMedia`: sem descer um nível, dá para somar bytes sem saber de qual disco.
    Separado de quem chama o `ioreg` para poder ser testado com saída fixa.
    """
    out = {}
    for node in nodes:
        st = node.get("Statistics") or {}
        dev = None
        for ch in node.get("IORegistryEntryChildren", []):
            if ch.get("BSD Name"):
                dev = ch["BSD Name"]
                break
        if not dev or not st:
            continue
        out[dev] = {"read": st.get("Bytes (Read)", 0),
                    "write": st.get("Bytes (Write)", 0),
                    "rops": st.get("Operations (Read)", 0),
                    "wops": st.get("Operations (Write)", 0)}
    return out


def disk_counters():
    return parse_ioreg(_ioreg())


# ------------------------------------------------------------- por processo

class _RUsage(ctypes.Structure):
    """rusage_info_v4. Os campos antes de diskio existem só para posicioná-lo.

    `proc_pid_rusage(pid, flavor, buffer)` recebe o endereço da struct. Passar
    o endereço de um *ponteiro* para ela (o que o tipo `rusage_info_t *` sugere)
    faz o kernel escrever 300 bytes sobre uma variável de 8 e derruba o
    processo com SIGSEGV.
    """
    _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [
        (n, ctypes.c_uint64) for n in (
            "user_time", "system_time", "pkg_idle_wkups", "interrupt_wkups",
            "pageins", "wired_size", "resident_size", "phys_footprint",
            "proc_start_abstime", "proc_exit_abstime",
            "child_user_time", "child_system_time", "child_pkg_idle_wkups",
            "child_interrupt_wkups", "child_pageins", "child_elapsed_abstime",
            "diskio_bytesread", "diskio_byteswritten",
            "qos_default", "qos_maintenance", "qos_background", "qos_utility",
            "qos_legacy", "qos_user_initiated", "qos_user_interactive",
            "billed_system_time", "serviced_system_time",
            "logical_writes", "lifetime_max_phys_footprint", "instructions",
            "cycles", "billed_energy", "serviced_energy",
            "interval_max_phys_footprint", "runnable_time", "flags")]


def _lib():
    global _LIB
    if _LIB is None:
        try:
            _LIB = ctypes.CDLL("/usr/lib/libSystem.dylib", use_errno=True)
            _LIB.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int,
                                             ctypes.c_void_p]
            _LIB.proc_pid_rusage.restype = ctypes.c_int
        except (OSError, AttributeError):
            _LIB = False
    return _LIB or None


def proc_io(pid):
    """(lidos, escritos) do processo, ou None quando o kernel recusa.

    Recusa é o caso normal para processo de outro usuário: sem sudo, só dá para
    ver os seus. Devolver None em vez de zero importa - zero seria mentira.
    """
    lib = _lib()
    if lib is None:
        return None
    buf = _RUsage()
    for flavor in (4, 2):
        if lib.proc_pid_rusage(pid, flavor, ctypes.byref(buf)) == 0:
            return buf.diskio_bytesread, buf.diskio_byteswritten
    return None


def process_list():
    """{pid: (user, comando)} - um fork só, em vez de um por processo."""
    try:
        r = subprocess.run(["ps", "-axo", "pid=,user=,comm="],
                           capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return {}
    out = {}
    for line in r.stdout.splitlines():
        p = line.split(None, 2)
        if len(p) == 3 and p[0].isdigit():
            out[int(p[0])] = (p[1], p[2])
    return out


def proc_counters(procs=None):
    """{pid: {user, comm, read, write}} e quantos o kernel recusou."""
    procs = process_list() if procs is None else procs
    out, denied = {}, 0
    for pid, (user, comm) in procs.items():
        io = proc_io(pid)
        if io is None:
            denied += 1
            continue
        out[pid] = {"user": user, "comm": comm, "read": io[0], "write": io[1]}
    return out, denied


# ------------------------------------------------- de qual volume é este byte

def volume_map(con, now=None):
    """{diskN: [rótulo, ...]} para os volumes indexados que estão montados.

    `diskutil` é lento demais para o intervalo de amostragem, então o resultado
    vive um minuto. Um disco plugado agora aparece no próximo minuto, não neste.
    """
    global _VOLS
    now = time.monotonic() if now is None else now
    if now - _VOLS[0] < VOL_TTL and _VOLS[1]:
        return _VOLS[1]
    from . import health
    out = {}
    if con is not None:
        for r in con.execute("SELECT label, mount FROM volumes"):
            if r["mount"] and os.path.isdir(r["mount"]):
                dev = health.parent_of(r["mount"])
                if dev:
                    out.setdefault(dev, []).append(r["label"])
    _VOLS = (now, out)
    return out


def mount_points(con):
    """[(ponto de montagem, rótulo)] do mais longo para o mais curto.

    A ordem importa: `/` casa com qualquer caminho, então quem compara prefixo
    tem que testar o mais específico primeiro.
    """
    if con is None:
        return []
    # rstrip("/") sem o `or "/"` transforma a raiz em string vazia, e aí o
    # volume montado em "/" nunca casa com caminho nenhum.
    mp = [(r["mount"].rstrip("/") or "/", r["label"]) for r in
          con.execute("SELECT label, mount FROM volumes") if r["mount"]]
    return sorted(mp, key=lambda m: -len(m[0]))


def open_volumes(pids, mounts):
    """{pid: [rótulo, ...]} pelos arquivos que cada processo tem aberto agora.

    Palpite, não prova: diz onde o processo *pode* estar escrevendo, não onde os
    bytes contados caíram. Uma chamada só do `lsof`, para os processos que
    interessam - varrer a máquina inteira custaria segundos.
    """
    if not pids or not mounts:
        return {}
    try:
        r = subprocess.run(["lsof", "-nP", "-Fpn", "-p",
                            ",".join(str(p) for p in pids)],
                           capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse_lsof(r.stdout, mounts)


def parse_lsof(text, mounts):
    """Saída `-Fpn` do lsof -> {pid: [rótulo, ...]}.

    O formato é uma linha por campo: `p<pid>` abre um processo e cada `n<path>`
    seguinte pertence a ele. `mounts` tem que vir do mais longo para o mais
    curto, senão `/` engole todo caminho antes de `/Volumes/X` ser testado.
    """
    out, cur = {}, None
    for line in text.splitlines():
        if not line:
            continue
        if line[0] == "p":
            cur = int(line[1:]) if line[1:].isdigit() else None
        elif line[0] == "n" and cur is not None:
            path = line[1:]
            for mount, label in mounts:
                hit = (path.startswith("/") if mount == "/" else
                       (path == mount or path.startswith(mount + "/")))
                if hit:
                    s = out.setdefault(cur, [])
                    if label not in s:
                        s.append(label)
                    break
    return out


# ------------------------------------------------------------------ amostra

def snapshot(con=None):
    procs, denied = proc_counters()
    return {"t": time.monotonic(), "disks": disk_counters(),
            "procs": procs, "denied": denied}


def _rate(b, a, dt):
    """Bytes por segundo entre duas leituras acumuladas.

    Contador que anda para trás significa que o pid foi reciclado ou o
    dispositivo sumiu e voltou. Nesse caso a janela não vale: devolve 0 em vez
    de um pico inventado de gigabytes.
    """
    d = b - a
    return 0.0 if d < 0 or dt <= 0 else d / dt


def rates(a, b, con=None, top=12):
    """Taxas entre duas amostras, por disco e por processo."""
    dt = max(1e-6, b["t"] - a["t"])
    vols = volume_map(con)

    disks = []
    for dev, cur in b["disks"].items():
        old = a["disks"].get(dev)
        if not old:
            continue
        disks.append({
            "dev": dev, "volumes": vols.get(dev, []),
            "read": _rate(cur["read"], old["read"], dt),
            "write": _rate(cur["write"], old["write"], dt),
            "rops": _rate(cur["rops"], old["rops"], dt),
            "wops": _rate(cur["wops"], old["wops"], dt)})
    disks.sort(key=lambda d: -(d["read"] + d["write"]))

    procs = []
    for pid, cur in b["procs"].items():
        old = a["procs"].get(pid)
        if not old:
            continue                     # nasceu dentro da janela: sem base
        rd = _rate(cur["read"], old["read"], dt)
        wr = _rate(cur["write"], old["write"], dt)
        if rd or wr:
            procs.append({"pid": pid, "user": cur["user"], "comm": cur["comm"],
                          "read": rd, "write": wr, "volumes": []})
    procs.sort(key=lambda p: -(p["read"] + p["write"]))
    procs = procs[:top]

    ov = open_volumes([p["pid"] for p in procs], mount_points(con))
    for p in procs:
        p["volumes"] = ov.get(p["pid"], [])

    return {"seconds": dt, "disks": disks, "procs": procs,
            "denied": b["denied"],
            "total_read": sum(d["read"] for d in disks),
            "total_write": sum(d["write"] for d in disks)}


def sample(con=None, interval=1.0, top=12):
    """Uma janela fechada: lê, espera, lê de novo e devolve as taxas."""
    a = snapshot(con)
    time.sleep(max(0.2, float(interval)))
    b = snapshot(con)
    return rates(a, b, con=con, top=top)


class Watcher:
    """Amostra em segundo plano, para o painel não esperar a janela fechar.

    Uma janela de I/O leva o tempo que leva: são duas leituras com um intervalo
    entre elas. Se cada GET do painel abrisse a sua, o navegador ficaria
    segurando a conexão - e dois navegadores abertos dobrariam os forks de `ps`.
    Aqui existe uma linha do tempo só, e quem chega lê a última janela pronta.

    A thread morre sozinha depois de `idle` segundos sem ninguém perguntar: sem
    isso, abrir a aba uma vez deixaria um `ps` por segundo rodando para sempre.
    """

    def __init__(self, db_path, interval=2.0, top=12, idle=45.0):
        self.db_path, self.interval, self.top, self.idle = db_path, interval, top, idle
        self._last = None
        self._asked = 0.0
        self._thread = None
        import threading
        self._lock = threading.Lock()

    def latest(self):
        """A última janela fechada, ligando o amostrador se estiver parado."""
        import threading
        self._asked = time.monotonic()
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, daemon=True)
                self._thread.start()
        return self._last

    def _loop(self):
        from . import db as db_mod
        con = None
        try:
            con = db_mod.connect(self.db_path, create=False)
        except Exception:                                        # noqa: BLE001
            con = None                   # sem índice ainda: mede sem rótulo
        try:
            a = snapshot(con)
            while time.monotonic() - self._asked < self.idle:
                time.sleep(self.interval)
                b = snapshot(con)
                try:
                    self._last = rates(a, b, con=con, top=self.top)
                except Exception as e:                           # noqa: BLE001
                    self._last = {"error": str(e)}
                a = b
        finally:
            if con is not None:
                con.close()
