# SPDX-License-Identifier: GPL-3.0-or-later
"""Reconciliar existe para NÃO perder o que já foi calculado.

`--rescan` corrige o índice jogando fora o volume inteiro, e com ele a
miniatura, o hash e o metadado de áudio de cada arquivo dele -- no acervo que
motivou isto, 99.556 linhas de `image_meta` por causa de algumas dezenas de
arquivos que tinham mudado de lugar.

O que está fixado aqui é a propriedade que torna isso seguro:

  - quem não mudou MANTÉM o id, e com ele a miniatura e o hash;
  - quem mudou mantém o id mas PERDE a lateral, porque ela descreve o conteúdo
    velho, e hash desatualizado é pior que hash nenhum: parece prova;
  - quem sumiu sai, com as laterais junto;
  - etiqueta não é tocada em nenhum dos três casos.

Roda:  python3 tests/test_reconciliar.py
"""
import os, shutil, sqlite3, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from diskmap import db, reconciliar, scan                           # noqa: E402

falhas = []


def ok(cond, msg):
    if not cond:
        falhas.append(msg)


def mundo():
    """Um volume de verdade, indexado, com laterais já preenchidas."""
    raiz = tempfile.mkdtemp()
    for rel, txt in (("acervo/um.mkv", "um"), ("acervo/dois.mkv", "dois"),
                     ("acervo/tres.mkv", "tres"), ("musica/faixa.mp3", "faixa")):
        cam = os.path.join(raiz, rel)
        os.makedirs(os.path.dirname(cam), exist_ok=True)
        with open(cam, "w") as fh:
            fh.write(txt)
    # O banco vive FORA da árvore: dentro, ele próprio aparece como arquivo
    # que mudou a cada escrita, e o teste mediria a si mesmo.
    con = db.connect(os.path.join(tempfile.mkdtemp(), "indice.sqlite"))
    scan.scan_volume(con, raiz, "VOL", progress=False)
    con.executescript("""
      CREATE TABLE IF NOT EXISTS image_meta (file INTEGER PRIMARY KEY, thumb TEXT);
      CREATE TABLE IF NOT EXISTS media_meta (file INTEGER PRIMARY KEY, thumb TEXT);
      CREATE TABLE IF NOT EXISTS file_hash (file INTEGER PRIMARY KEY, full TEXT);
      CREATE TABLE IF NOT EXISTS audio_meta (file INTEGER PRIMARY KEY, artist TEXT);
    """)
    for r in con.execute("SELECT id FROM files"):
        con.execute("INSERT INTO media_meta (file,thumb) VALUES (?,?)",
                    (r["id"], "mini/%d.png" % r["id"]))
        con.execute("INSERT INTO file_hash (file,full) VALUES (?,?)",
                    (r["id"], "sha-de-%d" % r["id"]))
    con.commit()
    return raiz, con


def ids(con):
    return {(r["relpath"], r["name"]): r["id"] for r in con.execute(
        "SELECT f.id, f.name, d.relpath FROM files f JOIN dirs d ON d.id=f.dir")}


def test_nao_mexe_em_nada_quando_nada_mudou():
    raiz, con = mundo()
    try:
        antes = ids(con)
        s, a, m, g, ef = reconciliar.reconciliar(con, raiz, "VOL", progress=False)
        ok(not s and not a and not m,
           "inventou mudança: %d/%d/%d" % (len(s), len(a), len(m)))
        ok(ef is None, "ensaio não devia ter aplicado nada")
        ok(ids(con) == antes, "mexeu nos ids sem motivo")
    finally:
        shutil.rmtree(raiz, ignore_errors=True)
        con.close()


def test_ensaio_nao_escreve():
    """A trava central: sem --aplicar, o índice sai intacto."""
    raiz, con = mundo()
    try:
        os.remove(os.path.join(raiz, "acervo/um.mkv"))
        antes = ids(con)
        n_mm = con.execute("SELECT COUNT(*) c FROM media_meta").fetchone()["c"]
        s, a, m, g, ef = reconciliar.reconciliar(con, raiz, "VOL", progress=False)
        ok(len(s) == 1, "não viu o arquivo que sumiu: %r" % s)
        ok(ef is None, "ensaio aplicou")
        ok(ids(con) == antes, "ensaio mexeu no índice")
        ok(con.execute("SELECT COUNT(*) c FROM media_meta").fetchone()["c"] == n_mm,
           "ensaio mexeu nas laterais")
    finally:
        shutil.rmtree(raiz, ignore_errors=True)
        con.close()


def test_quem_nao_mudou_guarda_id_miniatura_e_hash():
    """A razão de o reconciliador existir. Se isto quebrar, use --rescan."""
    raiz, con = mundo()
    try:
        antes = ids(con)
        os.remove(os.path.join(raiz, "acervo/um.mkv"))
        with open(os.path.join(raiz, "acervo/novo.mkv"), "w") as fh:
            fh.write("novo")
        reconciliar.reconciliar(con, raiz, "VOL", aplicar_=True, progress=False)
        depois = ids(con)
        for k in (("acervo", "dois.mkv"), ("acervo", "tres.mkv"),
                  ("musica", "faixa.mp3")):
            ok(depois.get(k) == antes[k],
               "%s trocou de id: %r -> %r" % (k, antes.get(k), depois.get(k)))
            fid = antes[k]
            ok(con.execute("SELECT COUNT(*) c FROM media_meta WHERE file=?",
                           (fid,)).fetchone()["c"] == 1,
               "perdeu a miniatura de %s" % (k,))
            ok(con.execute("SELECT COUNT(*) c FROM file_hash WHERE file=?",
                           (fid,)).fetchone()["c"] == 1,
               "perdeu o hash de %s" % (k,))
    finally:
        shutil.rmtree(raiz, ignore_errors=True)
        con.close()


def test_quem_sumiu_leva_as_laterais_junto():
    """Linha lateral órfã volta a casar com o id reaproveitado do próximo."""
    raiz, con = mundo()
    try:
        fid = ids(con)[("acervo", "um.mkv")]
        os.remove(os.path.join(raiz, "acervo/um.mkv"))
        reconciliar.reconciliar(con, raiz, "VOL", aplicar_=True, progress=False)
        ok(("acervo", "um.mkv") not in ids(con), "a linha do sumido ficou")
        for t in ("media_meta", "file_hash"):
            ok(con.execute("SELECT COUNT(*) c FROM %s WHERE file=?" % t,
                           (fid,)).fetchone()["c"] == 0,
               "sobrou lateral órfã em %s para o id %d" % (t, fid))
    finally:
        shutil.rmtree(raiz, ignore_errors=True)
        con.close()


def test_quem_mudou_mantem_id_mas_perde_a_lateral():
    """Miniatura do conteúdo velho é mentira; hash velho é mentira que parece prova."""
    raiz, con = mundo()
    try:
        alvo = os.path.join(raiz, "acervo/dois.mkv")
        fid = ids(con)[("acervo", "dois.mkv")]
        with open(alvo, "w") as fh:
            fh.write("conteudo completamente diferente e maior")
        os.utime(alvo, (time.time() + 60, time.time() + 60))
        s, a, m, g, _ = reconciliar.reconciliar(con, raiz, "VOL", aplicar_=True,
                                             progress=False)
        ok(len(m) == 1 and not s and not a,
           "classificou errado: %d sumiu, %d apareceu, %d mudou"
           % (len(s), len(a), len(m)))
        ok(ids(con).get(("acervo", "dois.mkv")) == fid,
           "quem mudou não devia trocar de id — o caminho é o mesmo arquivo")
        tam = con.execute("SELECT size FROM files WHERE id=?",
                          (fid,)).fetchone()["size"]
        ok(tam == os.path.getsize(alvo), "não atualizou o tamanho: %r" % tam)
        for t in ("media_meta", "file_hash"):
            ok(con.execute("SELECT COUNT(*) c FROM %s WHERE file=?" % t,
                           (fid,)).fetchone()["c"] == 0,
               "manteve %s de conteúdo que mudou" % t)
    finally:
        shutil.rmtree(raiz, ignore_errors=True)
        con.close()


def test_quem_apareceu_entra_sem_lateral():
    raiz, con = mundo()
    try:
        with open(os.path.join(raiz, "acervo/quatro.mkv"), "w") as fh:
            fh.write("quatro")
        reconciliar.reconciliar(con, raiz, "VOL", aplicar_=True, progress=False)
        fid = ids(con).get(("acervo", "quatro.mkv"))
        ok(fid is not None, "o arquivo novo não entrou no índice")
        if fid:
            ok(con.execute("SELECT COUNT(*) c FROM media_meta WHERE file=?",
                           (fid,)).fetchone()["c"] == 0,
               "arquivo novo nasceu com miniatura que ninguém gerou")
    finally:
        shutil.rmtree(raiz, ignore_errors=True)
        con.close()


def test_pasta_nova_tambem_entra():
    """`apareceu` pode trazer um diretório que o índice nunca viu."""
    raiz, con = mundo()
    try:
        os.makedirs(os.path.join(raiz, "colecao/caixa 1"))
        with open(os.path.join(raiz, "colecao/caixa 1/ep.mkv"), "w") as fh:
            fh.write("ep")
        reconciliar.reconciliar(con, raiz, "VOL", aplicar_=True, progress=False)
        ok(("colecao/caixa 1", "ep.mkv") in ids(con),
           "arquivo em pasta nova ficou de fora: %r" % list(ids(con)))
    finally:
        shutil.rmtree(raiz, ignore_errors=True)
        con.close()


def test_raiz_limitada_nao_declara_o_resto_sumido():
    """Com --root, o que está fora do recorte não pode contar como ausente.

    Sem o filtro no lado do índice, reconciliar uma subpasta apagaria o volume
    inteiro do índice: tudo que não foi caminhado "não está no disco".
    """
    raiz, con = mundo()
    try:
        s, a, m, g, _ = reconciliar.reconciliar(con, raiz, "VOL", raizes=("acervo",),
                                             progress=False)
        ok(not s, "declarou sumido o que está fora do recorte: %r" % s)
        ok(not a and not m, "inventou mudança no recorte: %r %r" % (a, m))
    finally:
        shutil.rmtree(raiz, ignore_errors=True)
        con.close()


def test_pasta_com_sublinhado_nao_puxa_a_irma():
    """'a_b' é curinga em LIKE e casaria 'axb'. Quinta vez neste projeto.

    O arquivo precisa estar UM NÍVEL ABAIXO. Com ele direto na pasta, o
    `relpath` é 'axb' e casa pela igualdade, não pelo `LIKE prefixo/%` -- que é
    o único ramo onde o escape importa. A primeira versão deste teste passava
    com e sem o ESCAPE, ou seja, não provava nada.
    """
    raiz, con = mundo()
    try:
        for p in ("a_b", "axb"):
            os.makedirs(os.path.join(raiz, p, "dentro"))
            with open(os.path.join(raiz, p, "dentro", "x.mkv"), "w") as fh:
                fh.write(p)
        scan.scan_volume(con, raiz, "VOL", rescan=True, progress=False)
        s, a, m, g, _ = reconciliar.reconciliar(con, raiz, "VOL", raizes=("a_b",),
                                             progress=False)
        ok(not s and not a and not m,
           "o recorte 'a_b' vazou para a irmã 'axb': %r %r %r" % (s, a, m))
    finally:
        shutil.rmtree(raiz, ignore_errors=True)
        con.close()


def _mandar_para_quarentena(con, raiz, rel):
    """Reproduz o que `actions.quarantine` faz: rename para dentro do volume,
    na pasta que `classify.SKIP_DIRS` esconde do scanner, e uma linha no diário.

    O destino é o caminho literal de propósito -- é assim que a quarentena
    existe no acervo real, e é por estar em SKIP_DIRS que a caminhada não a vê.
    """
    from diskmap import actions
    import time as _t
    origem = os.path.join(raiz, rel)
    destino = os.path.join(raiz, ".diskmap-quarantine", rel)
    os.makedirs(os.path.dirname(destino), exist_ok=True)
    os.rename(origem, destino)
    actions.ensure_log(con)
    con.execute("INSERT INTO action_log (at,action,path,moved_to,argv,rc,output)"
                " VALUES (?,?,?,?,?,?,?)",
                (int(_t.time()), "quarantine", origem, destino, "quarantine", 0, ""))
    con.commit()
    return origem


def test_quarentena_nao_e_sumico():
    """O que a ferramenta guardou ela sabe onde está: não pode virar ausência.

    Sem isto, reconciliar um volume com quarentena apaga do índice os arquivos
    (89,5 GB) que estão na quarentena -- e com o id iriam o hash e a miniatura
    de arquivos que o Histórico ainda pode devolver.
    """
    raiz, con = mundo()
    try:
        fid = ids(con)[("acervo", "tres.mkv")]
        _mandar_para_quarentena(con, raiz, "acervo/tres.mkv")
        s, a, m, g, _ = reconciliar.reconciliar(con, raiz, "VOL", progress=False)
        ok(not s, "chamou de sumido o que está na quarentena: %r" % (s,))
        ok(len(g) == 1, "não reconheceu o guardado: %r" % (g,))
        if g:
            ok(g[0][2] == "quarentena", "classificou o lugar errado: %r" % (g,))
        ok(not a and not m, "inventou mudança: %r %r" % (a, m))
    finally:
        shutil.rmtree(raiz, ignore_errors=True)
        con.close()


def test_aplicar_nao_apaga_o_que_esta_na_quarentena():
    """A consequência, que é o ponto: id, hash e miniatura sobrevivem."""
    raiz, con = mundo()
    try:
        fid = ids(con)[("acervo", "tres.mkv")]
        _mandar_para_quarentena(con, raiz, "acervo/tres.mkv")
        reconciliar.reconciliar(con, raiz, "VOL", aplicar_=True, progress=False)
        ok(("acervo", "tres.mkv") in ids(con),
           "apagou do índice um arquivo que está na quarentena")
        for t in ("media_meta", "file_hash"):
            ok(con.execute("SELECT COUNT(*) c FROM %s WHERE file=?" % t,
                           (fid,)).fetchone()["c"] == 1,
               "perdeu %s de arquivo guardado — o trabalho de hash foi embora" % t)
    finally:
        shutil.rmtree(raiz, ignore_errors=True)
        con.close()


def test_desfeito_volta_a_ser_arquivo_normal():
    """`undo` devolve o arquivo; aí não é guardado nem sumido, é presente.

    O diário não apaga a linha da quarentena, grava outra -- então quem lê só
    pela ação encontraria o arquivo "na quarentena" para sempre.
    """
    raiz, con = mundo()
    try:
        origem = _mandar_para_quarentena(con, raiz, "acervo/tres.mkv")
        os.rename(os.path.join(raiz, ".diskmap-quarantine", "acervo/tres.mkv"),
                  origem)
        import time as _t
        con.execute("INSERT INTO action_log (at,action,path,moved_to,argv,rc,"
                    "output) VALUES (?,?,?,?,?,?,?)",
                    (int(_t.time()) + 1, "undo", origem, "", "undo", 0, ""))
        con.commit()
        s, a, m, g, _ = reconciliar.reconciliar(con, raiz, "VOL", progress=False)
        ok(not s and not a and not m and not g,
           "o arquivo devolvido não voltou a ser normal: %r %r %r %r"
           % (s, a, m, g))
    finally:
        shutil.rmtree(raiz, ignore_errors=True)
        con.close()


def main():
    for nome, fn in sorted(globals().items()):
        if nome.startswith("test_"):
            fn()
    if falhas:
        print("reconciliar: %d FALHA(S)" % len(falhas))
        for f in falhas:
            print("  - %s" % f)
        return 1
    print("reconciliar: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
