# SPDX-License-Identifier: GPL-3.0-or-later
"""A leitura do smartctl é pura, então é testada com saída de verdade em vez de
exigir o binário instalado.

O que está fixado: setor pendente é crítico, desgaste alto de flash é crítico,
disco saudável não vira alarme, e — o mais importante — **sem smartctl o
veredito nunca é "ok"**, porque o booleano do macOS não autoriza dizer isso.

Roda:  python3 tests/test_health.py
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from diskmap import health  # noqa: E402

# HDD velho começando a falhar: pendente e realocado diferentes de zero.
ATA_RUIM = {
    "device": {"name": "/dev/disk5", "type": "sat"},
    "smart_status": {"passed": True},
    "power_on_time": {"hours": 61320},
    "temperature": {"current": 41},
    "ata_smart_attributes": {"table": [
        {"id": 5, "name": "Reallocated_Sector_Ct", "raw": {"value": 48}},
        {"id": 9, "name": "Power_On_Hours", "raw": {"value": 61320}},
        {"id": 197, "name": "Current_Pending_Sector", "raw": {"value": 8}},
        {"id": 198, "name": "Offline_Uncorrectable", "raw": {"value": 0}},
        {"id": 199, "name": "UDMA_CRC_Error_Count", "raw": {"value": 0}},
    ]},
}
ATA_BOM = {
    "device": {"name": "/dev/disk3", "type": "sat"},
    "smart_status": {"passed": True},
    "power_on_time": {"hours": 2400},
    "temperature": {"current": 33},
    "ata_smart_attributes": {"table": [
        {"id": 5, "name": "Reallocated_Sector_Ct", "raw": {"value": 0}},
        {"id": 197, "name": "Current_Pending_Sector", "raw": {"value": 0}},
        {"id": 198, "name": "Offline_Uncorrectable", "raw": {"value": 0}},
        {"id": 199, "name": "UDMA_CRC_Error_Count", "raw": {"value": 0}},
    ]},
}
NVME_GASTO = {
    "device": {"name": "/dev/disk0", "type": "nvme"},
    "smart_status": {"passed": True},
    "nvme_smart_health_information_log": {
        "percentage_used": 104, "available_spare": 4,
        "available_spare_threshold": 10, "media_errors": 12,
        "unsafe_shutdowns": 31, "power_on_hours": 19000},
    "temperature": {"current": 58},
}
NVME_NOVO = {
    "device": {"name": "/dev/disk1", "type": "nvme"},
    "smart_status": {"passed": True},
    "nvme_smart_health_information_log": {
        "percentage_used": 3, "available_spare": 100,
        "available_spare_threshold": 10, "media_errors": 0,
        "unsafe_shutdowns": 2},
    "temperature": {"current": 40},
}

VERIFIED = {"smart_os": "Verified"}
FAILING = {"smart_os": "Failing"}
NOSMART = {"smart_os": "Not Supported"}


def lv(sigs, key):
    for k, v, s, h in sigs:
        if k == key:
            return s, v
    return None, None


def main():
    bad, n = [], 0

    # 1. HDD ruim: pendente e realocado aparecem com a severidade certa
    n += 4
    s = health.signals(ATA_RUIM)
    if lv(s, "pendentes")[0] != "crit":
        bad.append(f"setor pendente deveria ser crítico, veio {lv(s,'pendentes')}")
    if lv(s, "realocados")[0] != "warn":
        bad.append(f"realocado deveria ser atenção, veio {lv(s,'realocados')}")
    if lv(s, "incorrigiveis")[0] != "ok":
        bad.append("zero incorrigíveis não deveria alarmar")
    if lv(s, "horas_ligado")[0] != "warn":
        bad.append("61 mil horas (7 anos) deveria ser atenção")

    # 2. e o veredito do disco inteiro acompanha
    n += 2
    l, why = health.verdict(VERIFIED, s, True)
    if l != "crit":
        bad.append(f"disco com setor pendente deveria ser crit, veio {l}")
    if "pendentes" not in why:
        bad.append(f"o motivo deveria nomear o atributo: {why!r}")

    # 3. HDD bom não vira alarme
    n += 2
    s = health.signals(ATA_BOM)
    if any(x[2] == "crit" for x in s):
        bad.append(f"disco saudável gerou crítico: {s}")
    if health.verdict(VERIFIED, s, True)[0] != "ok":
        bad.append("disco saudável com smartctl deveria ser ok")

    # 4. NVMe no fim da vida
    n += 4
    s = health.signals(NVME_GASTO)
    if lv(s, "desgaste")[0] != "crit":
        bad.append(f"104% de desgaste deveria ser crítico, veio {lv(s,'desgaste')}")
    if lv(s, "reserva")[0] != "crit":
        bad.append(f"reserva 4% abaixo do limite 10% deveria ser crítico")
    if lv(s, "erros_de_midia")[0] != "crit":
        bad.append("erro de mídia deveria ser crítico")
    if health.verdict(VERIFIED, s, True)[0] != "crit":
        bad.append("NVMe gasto deveria ser crit")

    # 5. NVMe novo
    n += 2
    s = health.signals(NVME_NOVO)
    if lv(s, "desgaste")[0] != "ok":
        bad.append("3% de desgaste não deveria alarmar")
    if health.verdict(VERIFIED, s, True)[0] != "ok":
        bad.append("NVMe novo deveria ser ok")

    # 6. O ponto todo: sem smartctl, "Verified" NÃO autoriza dizer ok
    n += 3
    l, why = health.verdict(VERIFIED, [], False)
    if l == "ok":
        bad.append("sem smartctl, Verified não pode virar 'ok'")
    if l != "desconhecido":
        bad.append(f"sem smartctl deveria ser desconhecido, veio {l}")
    if "smartmontools" not in why:
        bad.append(f"deveria dizer o que falta: {why!r}")

    # 7. Failing do macOS é crítico mesmo sem nenhum atributo
    n += 1
    if health.verdict(FAILING, [], False)[0] != "crit":
        bad.append("Failing do macOS deveria ser crítico sozinho")

    # 8. Sem SMART nenhum (ponte USB) também não é 'ok'
    n += 1
    if health.verdict(NOSMART, [], False)[0] != "desconhecido":
        bad.append("sem SMART deveria ser desconhecido")

    # 9. signals() aguenta entrada vazia e lixo sem explodir
    n += 2
    if health.signals(None) != []:
        bad.append("signals(None) deveria ser vazio")
    try:
        health.signals({"device": {}, "ata_smart_attributes": {"table": None}})
    except Exception as e:                                       # noqa: BLE001
        bad.append(f"signals quebrou com tabela nula: {e!r}")

    # 10. pior primeiro
    n += 1
    s = health.signals(ATA_RUIM)
    sev = [x[2] for x in s]
    if sev != sorted(sev, key=lambda x: {"crit": 0, "warn": 1, "ok": 2}[x]):
        bad.append(f"sinais fora de ordem: {sev}")

    for b in bad:
        print("ERRO " + b)
    print(f"{n - len(bad)}/{n} corretos")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
