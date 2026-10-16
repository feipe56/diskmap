# SPDX-License-Identifier: GPL-3.0-or-later
"""SMART e identidade de cada disco físico, cruzados com o que o índice guarda.

macOS sozinho responde pouco: `diskutil` e `system_profiler` dão um booleano
("Verified" / "Failing") e nada mais. Esse booleano é um péssimo aviso - ele só
vira quando o disco já passou dos limites do fabricante, e o estudo de 2007 do
Google sobre mais de cem mil discos achou que **mais da metade** dos que
falharam nunca acusou nada nos atributos mais fortes. Ou seja: `Verified` não
quer dizer saudável, quer dizer "ainda não cruzou o limite".

Os atributos que realmente avisam - setor realocado, setor pendente, desgaste de
flash - só saem do `smartctl` (smartmontools, `brew install smartmontools`).
Quando ele existe no PATH, este módulo usa; quando não, diz exatamente o que
está faltando em vez de fingir que o booleano basta.

O que o diskmap acrescenta a qualquer um desses: o índice sabe **quanto do seu
acervo mora naquele disco** e quanto disso não tem cópia conhecida em outro
lugar. Um SMART ruim num disco inteiramente replicado é um aborrecimento; o
mesmo SMART num disco que guarda a única cópia de 200 GB é outra conversa.
"""
import json, os, plistlib, re, subprocess
from shutil import which

TIMEOUT = 25

# Atributos ATA que preveem falha de verdade, e quanto pesam.
# (id, chave, severidade quando > 0, o que significa)
ATA_SIGNALS = [
    (197, "pendentes", "crit",
     "setores que o disco não conseguiu ler e ainda não remapeou"),
    (198, "incorrigiveis", "crit",
     "setores com erro que não dá para corrigir"),
    (5, "realocados", "warn",
     "setores já remapeados; se o número cresce, o disco está se desfazendo"),
    (199, "erros_crc", "warn",
     "erro de transmissão no cabo ou na gaveta, não necessariamente no disco"),
]


def _dk(*args):
    """diskutil com -plist logo depois do subcomando, que é onde ele aceita."""
    try:
        r = subprocess.run(["diskutil", args[0], "-plist", *args[1:]],
                           capture_output=True, timeout=TIMEOUT)
        return plistlib.loads(r.stdout) if r.returncode == 0 and r.stdout else None
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException):
        return None


def _physical(dev):
    """Resolve um identificador até o disco físico que está embaixo.

    `diskutil` chama de disco tanto o hardware quanto o container APFS
    sintetizado e a imagem de disco montada. Sem desfazer isso, o mesmo SSD
    aparece duas vezes na lista e três imagens de disco entram como se fossem
    hardware.
    """
    seen = set()
    while dev and dev not in seen:
        seen.add(dev)
        d = _dk("info", dev) or {}
        if d.get("VirtualOrPhysical") == "Physical":
            return dev
        stores = d.get("APFSPhysicalStores") or []
        nxt = stores[0].get("APFSPhysicalStore") if stores else None
        if not nxt:
            return None            # imagem de disco: não há hardware embaixo
        # disk1s2 -> disk1. Não use split("s"): a própria palavra "disk" tem um
        # 's', então "disk1s2".split("s")[0] é "di" e a resolução morre em silêncio.
        m = re.match(r"(disk\d+)", nxt)
        dev = m.group(1) if m else nxt
    return None


def whole_disks():
    """Só os discos físicos, sem container APFS nem imagem montada."""
    d = _dk("list")
    if not d:
        return []
    out = []
    for e in d.get("AllDisksAndPartitions", []):
        dev = e.get("DeviceIdentifier")
        if not dev:
            continue
        phys = _physical(dev)
        if phys and phys not in out:
            out.append(phys)
    return out


def parent_of(mount):
    """Qual disco físico carrega este caminho.

    Sobe até o ponto de montagem antes de perguntar: `diskutil info` responde
    por volume, e um caminho qualquer dentro dele (o home, por exemplo) devolve
    vazio.
    """
    p = os.path.abspath(mount)
    while p != "/" and not os.path.ismount(p):
        p = os.path.dirname(p)
    d = _dk("info", p)
    return _physical(d.get("ParentWholeDisk")) if d else None


def identity(dev):
    """Modelo, barramento, mídia e o booleano SMART do macOS."""
    d = _dk("info", dev) or {}
    ssd = d.get("SolidState")
    return {
        "dev": dev,
        "model": (d.get("MediaName") or "").strip() or "?",
        "protocol": d.get("BusProtocol") or "?",
        "size": d.get("TotalSize") or d.get("Size") or 0,
        "solid_state": ssd,
        "internal": bool(d.get("Internal")),
        "removable": bool(d.get("RemovableMediaOrExternalDevice")),
        "smart_os": d.get("SMARTStatus") or "Not Supported",
    }


def _smartctl(dev):
    """smartctl -j, tentando passar pela ponte USB quando a direta não responde."""
    if not which("smartctl"):
        return None, "smartctl não instalado"
    node = f"/dev/{dev}"
    for extra in ([], ["-d", "sat"], ["-d", "nvme"]):
        try:
            r = subprocess.run(["smartctl", "-j", "-a", *extra, node],
                               capture_output=True, text=True, timeout=TIMEOUT)
        except (OSError, subprocess.SubprocessError) as e:
            return None, str(e)
        if not r.stdout:
            continue
        try:
            j = json.loads(r.stdout)
        except ValueError:
            continue
        # bit 0/1 de exit_status = não conseguiu abrir / comando recusado
        if j.get("device") and (j.get("ata_smart_attributes")
                                or j.get("nvme_smart_health_information_log")
                                or j.get("smart_status")):
            return j, None
    return None, "smartctl não conseguiu ler este dispositivo (ponte USB costuma bloquear)"


def signals(j):
    """Transforma a saída do smartctl numa lista de sinais legíveis.

    Devolve [(chave, valor, severidade, explicação)], pior primeiro.
    """
    if not j:
        return []
    out = []
    nvme = j.get("nvme_smart_health_information_log") or {}
    if nvme:
        used = nvme.get("percentage_used")
        if used is not None:
            sev = "crit" if used >= 100 else "warn" if used >= 80 else "ok"
            out.append(("desgaste", f"{used}%", sev,
                        "quanto da vida útil estimada da flash já foi gasta"))
        spare = nvme.get("available_spare")
        thr = nvme.get("available_spare_threshold")
        if spare is not None:
            sev = "crit" if thr is not None and spare <= thr else "ok"
            out.append(("reserva", f"{spare}%", sev,
                        "blocos reserva que ainda restam para substituir os ruins"))
        v = nvme.get("media_errors")
        if v:
            out.append(("erros_de_midia", str(v), "crit",
                        "dados que o disco não conseguiu entregar corretos"))
        # Informativo, nunca alarme: toda máquina acumula algumas quedas sem
        # desmontar, e o número não diz nada sobre o disco estar falhando - diz
        # sobre o risco de ter corrompido arquivo. Alarmar aqui era falso positivo.
        v = nvme.get("unsafe_shutdowns")
        if v is not None:
            out.append(("desligamentos_sujos", str(v), "ok",
                        "quedas sem desmontar; risco de arquivo corrompido, "
                        "não sinal de disco falhando"))
    table = ((j.get("ata_smart_attributes") or {}).get("table")) or []
    by_id = {a.get("id"): a for a in table}
    for aid, key, sev, why in ATA_SIGNALS:
        a = by_id.get(aid)
        if not a:
            continue
        raw = (a.get("raw") or {}).get("value", 0)
        out.append((key, str(raw), sev if raw else "ok", why))
    hours = ((j.get("power_on_time") or {}).get("hours"))
    if hours is not None:
        anos = hours / 24 / 365
        out.append(("horas_ligado", f"{hours} ({anos:.1f} anos)",
                    "warn" if anos >= 5 else "ok",
                    "tempo acumulado girando/ligado"))
    t = ((j.get("temperature") or {}).get("current"))
    if t is not None:
        out.append(("temperatura", f"{t} °C",
                    "warn" if t >= 55 else "ok", "acima de 55 °C encurta a vida"))
    rank = {"crit": 0, "warn": 1, "ok": 2}
    out.sort(key=lambda s: rank.get(s[2], 3))
    return out


def verdict(ident, sigs, detailed):
    """Uma palavra sobre o disco, e por quê.

    Sem `smartctl` isto nunca passa de 'desconhecido', mesmo com o booleano do
    macOS dizendo Verified - porque é exatamente isso que ele vale.
    """
    smart = (ident.get("smart_os") or "").lower()
    if smart.startswith("failing"):
        return "crit", "o macOS já marca este disco como FAILING; troque agora"
    if any(s[2] == "crit" for s in sigs):
        nomes = ", ".join(s[0] for s in sigs if s[2] == "crit")
        return "crit", f"atributo crítico: {nomes}"
    if any(s[2] == "warn" for s in sigs):
        nomes = ", ".join(s[0] for s in sigs if s[2] == "warn")
        return "warn", f"atenção: {nomes}"
    if detailed:
        return "ok", "nenhum atributo preocupante"
    if smart.startswith("verified"):
        return "desconhecido", ("o macOS diz Verified, que só significa 'ainda não "
                                "cruzou o limite do fabricante'. Instale smartmontools "
                                "para ver os atributos que avisam antes")
    return "desconhecido", "este dispositivo não expõe SMART (ponte USB, em geral)"


def coverage(con):
    """Por volume: quanto do acervo tem cópia conhecida em OUTRO volume.

    É estimativa por nome+tamanho, o mesmo critério dos grupos de duplicata, e
    portanto um palpite - não prova de conteúdo igual. Serve para a pergunta
    "se este disco morrer hoje, quanto some de vez", que é a que importa quando
    o SMART fica feio.
    """
    tot = {r["label"]: (r["bytes"] or 0) for r in con.execute(
        "SELECT label, bytes FROM volumes")}
    dup = {}
    try:
        for r in con.execute(
                "SELECT v.label, SUM(f.size) b FROM dup_members m "
                "JOIN files f ON f.id=m.file JOIN volumes v ON v.id=f.vol "
                "WHERE m.grp IN (SELECT grp FROM dup_members dm "
                "  JOIN files df ON df.id=dm.file "
                "  GROUP BY grp HAVING COUNT(DISTINCT df.vol) > 1) "
                "GROUP BY v.label"):
            dup[r["label"]] = r["b"] or 0
    except Exception:                                            # noqa: BLE001
        dup = {}
    return {lb: {"bytes": b, "replicado": dup.get(lb, 0),
                 "so_aqui": max(0, b - dup.get(lb, 0))}
            for lb, b in tot.items()}


def report(con=None):
    """Um registro por disco físico, com os volumes indexados que moram nele."""
    vols = []
    if con is not None:
        vols = [dict(r) for r in con.execute(
            "SELECT label, mount, bytes, files FROM volumes ORDER BY bytes DESC")]
    cov = coverage(con) if con is not None else {}

    por_disco = {}
    for v in vols:
        if v["mount"] and os.path.isdir(v["mount"]):
            p = parent_of(v["mount"])
            if p:
                por_disco.setdefault(p, []).append(v["label"])

    out = []
    for dev in whole_disks():
        ident = identity(dev)
        j, err = _smartctl(dev)
        sigs = signals(j)
        lvl, why = verdict(ident, sigs, bool(j))
        labels = por_disco.get(dev, [])
        out.append({
            **ident,
            "level": lvl, "why": why,
            "detailed": bool(j), "smartctl_error": err,
            "signals": [{"key": k, "value": v, "level": s, "help": h}
                        for k, v, s, h in sigs],
            "volumes": labels,
            "indexado": sum(cov.get(l, {}).get("bytes", 0) for l in labels),
            "so_aqui": sum(cov.get(l, {}).get("so_aqui", 0) for l in labels),
        })
    rank = {"crit": 0, "warn": 1, "desconhecido": 2, "ok": 3}
    out.sort(key=lambda d: (rank.get(d["level"], 4), -d["so_aqui"]))
    return {"disks": out, "smartctl": bool(which("smartctl")),
            "hint": "brew install smartmontools"}
