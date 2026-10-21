# SPDX-License-Identifier: GPL-3.0-or-later
"""Resolver clone é a ÚNICA escrita do diskmap que substitui arquivo existente.

Todo o resto do código nunca sobrescreve: a decisão 6 diz que remover é Lixeira
ou quarentena, e substituir em cima é uma remoção com outro nome. Então aqui
está fixado o que, se quebrar, perde dado do usuário:

  - o arquivo substituído vai para a quarentena ANTES, e o Histórico desfaz;
  - caminho de launcher é recusado nos DOIS lados;
  - se a cópia sair diferente da origem, o original volta e nada fica no lugar;
  - se a origem mudou desde o índice, não se escreve nada.

Roda:  python3 tests/test_resolve_clone.py
"""
import hashlib, os, shutil, sqlite3, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from diskmap import actions, db                                      # noqa: E402

falhas = []


def ok(cond, msg):
    if not cond:
        falhas.append(msg)


def sha(b):
    return hashlib.sha256(b).hexdigest()


ARQS = {
    "A/x.bin": b"conteudo do lado A\n" * 100,
    "B/x.bin": b"conteudo do lado B\n" * 100,
    "SteamLibrary/steamapps/common/jogo/dlc.rpf": b"instalacao do steam\n" * 10,
    "C/x.bin": b"conteudo do lado A\n" * 100,          # igual ao A, de propósito
}


def mundo():
    base = tempfile.mkdtemp(prefix="resolve-")
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(db.SCHEMA)
    con.execute("INSERT INTO volumes (id,label,mount) VALUES (1,'T',?)", (base,))
    nd = [0]
    vistos = {}
    for rel, dados in ARQS.items():
        d, _, nome = rel.rpartition("/")
        p = os.path.join(base, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(dados)
        if d not in vistos:
            nd[0] += 1
            vistos[d] = nd[0]
            con.execute("INSERT INTO dirs (id,vol,relpath,depth) VALUES (?,1,?,?)",
                        (nd[0], d, d.count("/") + 1))
        con.execute("INSERT INTO files (vol,dir,name,size,mtime,ext,cls) "
                    "VALUES (1,?,?,?,1700000000,'bin','outros')",
                    (vistos[d], nome, len(dados)))
    con.commit()
    actions.ensure_log(con)
    return base, con


def ler(p):
    with open(p, "rb") as f:
        return f.read()


# --------------------------------------------------------------- testes
def test_substitui_e_desfaz():
    base, con = mundo()
    try:
        a, b = os.path.join(base, "A/x.bin"), os.path.join(base, "B/x.bin")
        antes_b = ler(b)
        r = actions.resolve_clone(con, a, b)
        ok(r["ok"], "deveria ter dado certo: %s" % r)
        ok(ler(b) == ARQS["A/x.bin"], "o destino não ficou igual à origem")
        ok(ler(a) == ARQS["A/x.bin"], "a ORIGEM foi alterada")
        ok(r["moved_to"] and os.path.exists(r["moved_to"]),
           "o arquivo substituído não foi para a quarentena")
        ok(ler(r["moved_to"]) == antes_b,
           "o que foi para a quarentena não é o conteúdo antigo")
        ok(not [x for x in os.listdir(os.path.join(base, "B"))
                if "parcial" in x], "sobrou temporário de cópia")
        # e o Histórico desfaz
        log = actions.log(con, 5)
        ok(log and log[0]["action"] == "resolve-clone",
           "não foi registrado no action_log: %s" % log)
        os.remove(b)                       # undo recusa se o destino existe
        actions.undo(con, log[0]["id"])
        ok(ler(b) == antes_b, "o undo não trouxe o conteúdo antigo de volta")
    finally:
        shutil.rmtree(base)
        con.close()


def test_recusa_launcher_dos_dois_lados():
    base, con = mundo()
    try:
        steam = os.path.join(base, "SteamLibrary/steamapps/common/jogo/dlc.rpf")
        a = os.path.join(base, "A/x.bin")
        for src, dst, lado in ((a, steam, "escrevendo NO steam"),
                               (steam, a, "lendo DO steam")):
            antes = (ler(src), ler(dst))
            try:
                actions.resolve_clone(con, src, dst)
                falhas.append("aceitou mexer em caminho protegido (%s)" % lado)
            except ValueError as e:
                ok("protegido" in str(e), "recusou por outro motivo: %s" % e)
            ok((ler(src), ler(dst)) == antes,
               "mexeu nos arquivos mesmo recusando (%s)" % lado)
    finally:
        shutil.rmtree(base)
        con.close()


def test_recusa_origem_mudada():
    """O índice é uma fotografia. Se o disco andou desde ela, não se escreve."""
    base, con = mundo()
    try:
        a, b = os.path.join(base, "A/x.bin"), os.path.join(base, "B/x.bin")
        antes = ler(b)
        try:
            actions.resolve_clone(con, a, b, expect=sha(b"outra coisa"))
            falhas.append("aceitou substituir com sha256 de origem divergente")
        except ValueError as e:
            ok("mudou desde a leitura" in str(e), "recusou por outro motivo: %s" % e)
        ok(ler(b) == antes, "mexeu no destino mesmo recusando")
        ok(not os.path.exists(os.path.join(base, actions.QUAR_NAME)),
           "quarentenou antes de validar o sha256 esperado")
    finally:
        shutil.rmtree(base)
        con.close()


def test_recusa_mesmo_arquivo_e_fora_do_indice():
    base, con = mundo()
    try:
        a = os.path.join(base, "A/x.bin")
        try:
            actions.resolve_clone(con, a, a)
            falhas.append("aceitou substituir o arquivo por ele mesmo")
        except ValueError as e:
            ok("mesmo arquivo" in str(e), "recusou por outro motivo: %s" % e)
        try:
            actions.resolve_clone(con, a, "/etc/hosts")
            falhas.append("aceitou escrever fora dos volumes indexados")
        except ValueError as e:
            ok("fora dos volumes" in str(e) or "não está no índice" in str(e),
               "recusou por outro motivo: %s" % e)
    finally:
        shutil.rmtree(base)
        con.close()


def test_copia_ruim_devolve_o_original():
    """Se a releitura não bater com a origem, o original tem de voltar ao lugar.

    Simulado forçando o digest a mentir: é a única forma de exercitar o ramo
    sem corromper um disco de verdade."""
    base, con = mundo()
    from diskmap import verify as verify_mod
    real = verify_mod.full_digest
    chamadas = [0]

    def mentiroso(path, chunk=4 << 20, nocache=False):
        chamadas[0] += 1
        d = real(path, chunk, nocache)
        return "0" * 64 if chamadas[0] == 2 else d   # 2ª chamada = a releitura

    try:
        a, b = os.path.join(base, "A/x.bin"), os.path.join(base, "B/x.bin")
        antes = ler(b)
        verify_mod.full_digest = mentiroso
        try:
            actions.resolve_clone(con, a, b)
            falhas.append("aceitou uma cópia que não bate com a origem")
        except ValueError as e:
            ok("não deu para substituir" in str(e), "erro inesperado: %s" % e)
        ok(os.path.exists(b), "o destino sumiu depois da falha")
        ok(ler(b) == antes, "o original não voltou com o conteúdo certo")
        ok(not [x for x in os.listdir(os.path.join(base, "B")) if "parcial" in x],
           "deixou temporário para trás")
    finally:
        verify_mod.full_digest = real
        shutil.rmtree(base)
        con.close()


def test_compare_pair():
    base, con = mundo()
    try:
        a, b = os.path.join(base, "A/x.bin"), os.path.join(base, "B/x.bin")
        c = os.path.join(base, "C/x.bin")
        r = actions.compare_pair(con, a, b)
        ok(r["equal"] is False, "disse que A e B são iguais")
        r2 = actions.compare_pair(con, a, c)
        ok(r2["equal"] is True, "disse que A e C são diferentes")
        ok(r2["a"]["sha"] == sha(ARQS["A/x.bin"]), "sha256 de A errado")
        n = con.execute("SELECT COUNT(*) c FROM file_hash WHERE full IS NOT NULL"
                        ).fetchone()["c"]
        ok(n >= 3, "não guardou os hashes no índice: %d linhas" % n)
    finally:
        shutil.rmtree(base)
        con.close()


def test_indice_travado(tmp=True):
    """Com o índice travado por outra tarefa, a substituição é RECUSADA antes de
    tocar em arquivo, e a comparação de hash ainda responde — só não guarda.

    A diferença é o que cada uma promete: substituir sem poder registrar no
    action_log seria uma mudança que o Histórico não desfaz; comparar é leitura,
    e a resposta já foi paga em I/O."""
    base = tempfile.mkdtemp(prefix="resolve-lock-")
    dbp = os.path.join(base, "i.db")
    con = sqlite3.connect(dbp)
    con.row_factory = sqlite3.Row
    con.executescript(db.SCHEMA)
    con.execute("INSERT INTO volumes (id,label,mount) VALUES (1,'T',?)", (base,))
    con.execute("INSERT INTO dirs (id,vol,relpath,depth) VALUES (1,1,'A',1)")
    con.execute("INSERT INTO dirs (id,vol,relpath,depth) VALUES (2,1,'B',1)")
    for d, rel in ((1, "A/x.bin"), (2, "B/x.bin")):
        p = os.path.join(base, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(ARQS[rel])
        con.execute("INSERT INTO files (vol,dir,name,size,mtime,ext,cls) "
                    "VALUES (1,?,'x.bin',?,1,'bin','outros')", (d, len(ARQS[rel])))
    con.commit()
    actions.ensure_log(con)
    outro = sqlite3.connect(dbp)
    outro.execute("PRAGMA busy_timeout=100")
    try:
        outro.execute("BEGIN IMMEDIATE")        # a outra tarefa segura a trava
        con.execute("PRAGMA busy_timeout=100")
        a_, b_ = os.path.join(base, "A/x.bin"), os.path.join(base, "B/x.bin")
        antes = ler(b_)
        try:
            actions.resolve_clone(con, a_, b_)
            falhas.append("substituiu com o índice travado (sem poder registrar)")
        except ValueError as e:
            ok("travado por outra tarefa" in str(e),
               "recusou por outro motivo: %s" % e)
        ok(ler(b_) == antes, "mexeu no arquivo mesmo com o índice travado")
        ok(not os.path.exists(os.path.join(base, actions.QUAR_NAME)),
           "quarentenou antes de checar se podia registrar")
        r = actions.compare_pair(con, a_, b_)
        ok(r["equal"] is False, "a comparação devia responder mesmo travada")
        ok(r["persisted"] is False, "disse que guardou com o índice travado")
        ok("persist_erro" in r, "não explicou por que não guardou")
    finally:
        outro.rollback(); outro.close(); con.close()
        shutil.rmtree(base)


def test_descartar_libera_e_desfaz():
    """Descartar é o que de fato tira a redundância: um lado sai do caminho.

    `resolve_clone` deixa os dois arquivos no disco e não libera nada — ele
    sincroniza. Quem desduplica é este."""
    base, con = mundo()
    try:
        a, b = os.path.join(base, "A/x.bin"), os.path.join(base, "C/x.bin")
        antes = ler(b)
        r = actions.discard_one(con, b, keep=a)
        ok(r["ok"], "deveria ter dado certo: %s" % r)
        ok(not os.path.exists(b), "o arquivo descartado continua no lugar")
        ok(ler(a) == ARQS["A/x.bin"], "mexeu no lado que ficou")
        # A invariante que importa: descartar MOVE, nunca apaga. Conferida em
        # dois passos para a falha dizer qual dos dois quebrou.
        ok(bool(r["moved_to"]), "descartou sem mover para lugar nenhum — isso é "
                                "apagar, e apagar não se desfaz")
        ok(r["moved_to"] and os.path.exists(r["moved_to"]),
           "disse que moveu para %s, mas não há nada lá" % r["moved_to"])
        ok(r["moved_to"] and os.path.exists(r["moved_to"])
           and ler(r["moved_to"]) == antes,
           "o descartado não está íntegro na quarentena")
        ok(r["moved_to"] and actions.QUAR_NAME in r["moved_to"],
           "foi para fora da quarentena: %s" % r["moved_to"])
        ok(r["bytes"] == len(ARQS["C/x.bin"]), "não reportou o espaço liberado")
        log = actions.log(con, 3)
        ok(log[0]["action"] == "discard-clone", "não registrou: %s" % log[0])
        if r["moved_to"]:
            actions.undo(con, log[0]["id"])
            ok(os.path.exists(b) and ler(b) == antes, "o undo não trouxe de volta")
        else:
            falhas.append("sem moved_to não há o que desfazer — undo não testado")
    finally:
        shutil.rmtree(base)
        con.close()


def test_descartar_recusa_launcher_e_origem_mudada():
    base, con = mundo()
    try:
        steam = os.path.join(base, "SteamLibrary/steamapps/common/jogo/dlc.rpf")
        antes = ler(steam)
        try:
            actions.discard_one(con, steam, keep=os.path.join(base, "A/x.bin"))
            falhas.append("aceitou descartar de dentro de um launcher")
        except ValueError as e:
            ok("protegido" in str(e), "recusou por outro motivo: %s" % e)
        ok(os.path.exists(steam) and ler(steam) == antes,
           "mexeu no arquivo do launcher mesmo recusando")
        a = os.path.join(base, "A/x.bin")
        try:
            actions.discard_one(con, a, keep=os.path.join(base, "C/x.bin"),
                                expect=sha(b"outra coisa"))
            falhas.append("aceitou descartar com sha256 divergente")
        except ValueError as e:
            ok("mudou desde a leitura" in str(e), "recusou por outro motivo: %s" % e)
        ok(os.path.exists(a), "descartou mesmo recusando")
    finally:
        shutil.rmtree(base)
        con.close()


def test_nunca_descarta_a_ultima_copia():
    """Um descarte cujo último clique deixa zero cópias não é desduplicação, é
    exclusão com passos extras.

    A cópia que fica é conferida NO DISCO, agora — não no índice, que é uma
    fotografia e pode estar velha. Três formas de ficar sem nenhuma, todas
    recusadas antes de mover qualquer coisa."""
    base, con = mundo()
    try:
        a, c = os.path.join(base, "A/x.bin"), os.path.join(base, "C/x.bin")
        antes = ler(a)
        # 1. sem dizer quem fica
        try:
            actions.discard_one(con, a)
            falhas.append("descartou sem exigir uma cópia sobrevivente")
        except ValueError as e:
            ok("qual cópia fica" in str(e), "recusou por outro motivo: %s" % e)
        # 2. apontando para si mesmo
        try:
            actions.discard_one(con, a, keep=a)
            falhas.append("aceitou a si mesmo como cópia sobrevivente")
        except ValueError as e:
            ok("mesmo arquivo" in str(e), "recusou por outro motivo: %s" % e)
        # 3. apontando para algo que não está lá
        try:
            actions.discard_one(con, a, keep=os.path.join(base, "A/sumido.bin"))
            falhas.append("aceitou como sobrevivente um arquivo inexistente")
        except ValueError as e:
            ok("não está lá" in str(e), "recusou por outro motivo: %s" % e)
        ok(ler(a) == antes, "mexeu no arquivo em alguma das recusas")
        # 4. e o caso real: a sobrevivente já está na quarentena
        r1 = actions.discard_one(con, c, keep=a)
        ok(r1["ok"], "o primeiro descarte devia funcionar")
        try:
            actions.discard_one(con, a, keep=r1["moved_to"])
            falhas.append("descartou a última cópia apontando para a quarentena")
        except ValueError as e:
            ok("já está na quarentena" in str(e), "recusou por outro motivo: %s" % e)
        ok(os.path.isfile(a), "a última cópia do acervo foi embora")
    finally:
        shutil.rmtree(base)
        con.close()


def grupo(con, base, nome, rels):
    """Cria um grupo de duplicata com os membros dados."""
    con.execute("INSERT INTO dup_groups (name,size,copies,wasted,verified) "
                "VALUES (?,?,?,?,'full')",
                (nome, len(ARQS[rels[0]]), len(rels),
                 len(ARQS[rels[0]]) * (len(rels) - 1)))
    g = con.execute("SELECT last_insert_rowid() i").fetchone()["i"]
    for rel in rels:
        d, _, n = rel.rpartition("/")
        fid = con.execute("SELECT f.id FROM files f JOIN dirs d ON d.id=f.dir "
                          "WHERE d.relpath=? AND f.name=?", (d, n)).fetchone()["id"]
        con.execute("INSERT INTO dup_members (grp,file) VALUES (?,?)", (g, fid))
    con.commit()
    return g


def test_grupo_descarta_todas_menos_a_escolhida():
    base, con = mundo()
    try:
        g = grupo(con, base, "x.bin", ["A/x.bin", "C/x.bin"])
        fica = os.path.join(base, "A/x.bin")
        r = actions.discard_group(con, g, fica)
        ok(r["ok"], "deveria ter dado certo: %s" % r)
        ok(len(r["descartados"]) == 1, "devia descartar 1, descartou %d"
           % len(r["descartados"]))
        ok(os.path.isfile(fica), "descartou a cópia que devia ficar")
        ok(not os.path.exists(os.path.join(base, "C/x.bin")), "a outra ficou")
        ok(r["bytes"] == len(ARQS["C/x.bin"]), "espaço liberado errado: %s" % r["bytes"])
    finally:
        shutil.rmtree(base); con.close()


def test_grupo_divergente_e_recusado():
    """Se o hash provou que as cópias diferem, não há redundância para remover:
    são arquivos distintos que por acaso têm o mesmo nome e tamanho."""
    base, con = mundo()
    try:
        g = grupo(con, base, "x.bin", ["A/x.bin", "C/x.bin"])
        con.execute("UPDATE dup_groups SET verified='mismatch' WHERE id=?", (g,))
        con.commit()
        antes = ler(os.path.join(base, "C/x.bin"))
        try:
            actions.discard_group(con, g, os.path.join(base, "A/x.bin"))
            falhas.append("descartou de um grupo provado DIVERGENTE")
        except ValueError as e:
            ok("DIVERGENTE" in str(e), "recusou por outro motivo: %s" % e)
        ok(ler(os.path.join(base, "C/x.bin")) == antes, "mexeu mesmo recusando")
    finally:
        shutil.rmtree(base); con.close()


def test_grupo_exige_que_a_escolhida_seja_do_grupo():
    base, con = mundo()
    try:
        g = grupo(con, base, "x.bin", ["A/x.bin", "C/x.bin"])
        intruso = os.path.join(base, "B/x.bin")
        try:
            actions.discard_group(con, g, intruso)
            falhas.append("aceitou como sobrevivente um arquivo de fora do grupo")
        except ValueError as e:
            ok("não é membro deste grupo" in str(e), "recusou por outro motivo: %s" % e)
        for rel in ("A/x.bin", "C/x.bin"):
            ok(os.path.isfile(os.path.join(base, rel)), "mexeu em %s" % rel)
    finally:
        shutil.rmtree(base); con.close()


def main():
    for nome, fn in sorted(globals().items()):
        if nome.startswith("test_"):
            fn()
    if falhas:
        print("resolve_clone: %d FALHA(S)" % len(falhas))
        for f in falhas:
            print("  - %s" % f)
        return 1
    print("resolve_clone: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
