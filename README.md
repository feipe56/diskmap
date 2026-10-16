# diskmap

Índice somente-leitura de vários discos, com análise de redundância e um painel local.

## A dor que isso resolve

Você sabe que a coisa existe em algum lugar. O que você não sabe é em qual disco.

Então começa: abre uma pasta, abre outra, desce três níveis, lembra que talvez esteja
no HD que está na gaveta, pluga, procura de novo. No fim você acha — ou desiste e baixa
de novo, e passa a ter duas cópias. A bagunça não vem de desorganização: vem de nunca
ter tido **visão do todo**.

O Finder não resolve isso porque ele mostra uma pasta por vez, e só das que estão
montadas agora. Não existe a pergunta "o que eu tenho?" — só "o que tem aqui dentro?".

diskmap lê os diretórios **uma vez**, guarda num índice e passa a responder a partir
dele, sem abrir pasta nenhuma:

| A pergunta | Como ele responde |
|---|---|
| Onde está X? | `search` acha mesmo com o disco desplugado, e diz **qual plugar** |
| O que eu tenho, somando tudo? | painel com o total por volume, por tipo de conteúdo e por pasta |
| Onde está o peso? | tamanho recursivo de cada diretório, na profundidade que você pedir |
| Isso está repetido? | cópias idênticas, **árvores clonadas** e imagens visualmente iguais |
| O que dá pra apagar? | plano revisável — nada sai do lugar até você rodar o script |
| Que imagens eu tenho? | galeria com miniatura, separada por biblioteca |
| Que projeto meu já morreu? | `node_modules` e build de projeto que você não toca há anos |

O caso que originou isso: discos externos que vivem desconectados, o mesmo projeto em
três lugares, e nenhum deles é o oficial.

- **Nada é apagado.** O comando `plan` gera um script que *move* para quarentena,
  junto com um `undo.sh`.
- **Sem dependências.** Só a biblioteca padrão do Python 3.8+. Nada de pip.
- **Responde offline.** O índice vive em SQLite: o disco só precisa estar plugado na
  hora de indexar.

## Uso

```bash
# indexar (retomável: se parar no meio, rodar de novo continua)
python3 -m diskmap scan "/Volumes/Jogos" --label Jogos
python3 -m diskmap scan ~ --label Casa --root Documents --root Desktop

# ou importar um índice TSV que você já tem
python3 -m diskmap import caminho/para/index.tsv

# calcular achados (duplicata, árvore clonada, build/dep parado)
python3 -m diskmap analyze

# ver no navegador
python3 -m diskmap images         # miniatura, dimensão, hash visual, biblioteca
python3 -m diskmap serve          # http://127.0.0.1:8777

# onde está isso?
python3 -m diskmap search minha-engine

# confirmar duplicatas por conteúdo antes de mexer
python3 -m diskmap verify --stage quick        # lê 64 KB por arquivo
python3 -m diskmap verify --stage full --limit 200   # sha256 completo nos maiores

# gerar o plano de quarentena (só duplicatas confirmadas por hash)
python3 -m diskmap plan --out ./plano
bash ./plano/apply.sh      # move
bash ./plano/undo.sh       # desfaz
```

## Ações pelo painel

Desligadas por padrão. Um servidor em `localhost` é alcançável por qualquer página
aberta no seu navegador, então executar comando não fica ligado sem você pedir:

```bash
python3 -m diskmap serve --allow-actions
```

Ligado, cada linha do painel ganha um botão **ações**: abrir no Finder, abrir a pasta,
mover para quarentena, mover para a Lixeira, e o que você declarar. Tudo fica na aba
**Histórico**, com o comando exato e um botão de desfazer.

O que protege isso:

| | |
|---|---|
| Token por execução, exigido em todo POST | bloqueia requisição de outra página |
| `Host` e `Origin` validados | bloqueia DNS rebinding |
| Comando vem do seu arquivo, não do navegador | o browser manda o *nome* da ação |
| `subprocess` com argv, `shell=False` | sem injeção de shell |
| Alvo precisa existir no índice | não dá para apontar para `/etc/passwd` |
| Remover = Lixeira ou quarentena | nunca `unlink`; sempre reversível |
| Zerar tem bandeira separada | `--allow-wipe`; é o único caminho irreversível |

### Rodar os comandos pelo painel

A aba **Tarefas** tem um formulário para `scan`, `analyze`, `verify` e `plan`, com log
ao vivo e botão de cancelar. Cada um roda como subprocesso, o que dá três coisas:
o painel continua respondendo enquanto uma varredura leva horas, o SQLite nunca vê
dois escritores no mesmo processo, e a tarefa é cancelável de verdade.

O navegador manda um *tipo* de tarefa e campos tipados; o `argv` é montado no servidor
a partir deles. Rótulo fora de `[A-Za-z0-9._-]`, subpasta com `..`, categoria inventada
ou profundidade fora de 1..6 são recusados antes de qualquer processo nascer.

Só uma tarefa que escreve no índice (`scan`, `import`, `analyze`, `verify`) roda por vez;
a segunda é recusada com o número da que está em andamento. `plan` é leitura e não
disputa. O histórico fica em `job_log`, e sobrevive ao fim do servidor.

`serve` cria um índice vazio se não existir, para o primeiro `scan` poder sair do
próprio painel.

### Seus próprios comandos

Em `~/.diskmap/actions.json` (criado na primeira execução):

```json
{
  "actions": [
    { "id": "7z_list", "name": "Listar conteúdo (7z)",
      "argv": ["7z", "l", "{path}"], "capture": true, "needs": "7z",
      "help": "o que tem dentro, sem extrair" }
  ]
}
```

`{path}` é o alvo e `{dir}` é a pasta dele. Nada passa por shell, então `|` e `;` não
funcionam — se precisar de pipeline, aponte `argv` para um script seu. `needs` esconde
a ação quando o programa não está no PATH. `confirm` pede confirmação; `capture` mostra
a saída.

## Zerar um arquivo (`wipe`)

```bash
python3 -m diskmap wipe "/Volumes/X/segredo.bin"     # pergunta antes
python3 -m diskmap serve --allow-actions --allow-wipe # libera no painel
```

Sobrescreve cada byte com zero, **confere lendo de volta** e então apaga. É a
única coisa irreversível do diskmap e o único lugar que chama `os.remove`, então
tem bandeira própria: `--allow-actions` sozinho não libera.

### O que ele consegue prometer depende do disco

Antes de escrever qualquer coisa, o `assess()` responde quanto um zero-fill vale
naquele volume, e o painel mostra isso **antes** de pedir confirmação:

| Veredito | Quando | O que significa |
|---|---|---|
| `efetivo` | sem copy-on-write, disco mecânico | o setor magnético é sobrescrito de verdade |
| `parcial` | sem copy-on-write, memória flash | o controlador remapeia a escrita; as páginas físicas originais só somem no garbage collection |
| `fraco` | APFS | copy-on-write: escrever aloca blocos **novos**, e os antigos guardam os bytes originais |
| `inutil` | APFS com snapshot | o snapshot **fixa** os blocos antigos de propósito |
| `impossivel` | volume somente-leitura | recusado: nada pode ser escrito |

É por isso que a Apple removeu o "Esvaziar Lixeira com Segurança" no 10.11. Para
um disco inteiro, a única garantia real é apagar o disco inteiro — `diskutil
secureErase`, ou criptografia desde o começo.

### O que ele recusa

Hard link (zerar destruiria o conteúdo visto pelos outros nomes), link simbólico
(não tocaria o destino), pasta (zerar uma árvore é outra promessa), volume
somente-leitura, e qualquer caminho fora do índice. Se a leitura de verificação
achar um byte não-zero, o arquivo **não** é apagado.

O `action_log` registra, mas a aba Histórico não desfaz — não há o que desfazer.

## As regras da sua máquina

Nada sobre os seus discos mora no código. Os labels dos seus volumes, os nomes dos
seus projetos, a pasta onde você joga arquivo velho — isso é seu, e fica em
`~/.diskmap/rules.json`:

```json
{
  "libraries": [
    { "lib": "projeto-dev", "match": "(minha-engine|meu-jogo|pathtool)" },
    { "lib": "arquivo-morto", "match": "(^|/)coisas antigas/" }
  ],
  "prefer_drop": ["/rascunho/"],
  "protected": ["/Volumes/Backup/"],
  "mounts": { "Jogos": "/Volumes/Jogos", "Casa": "~" }
}
```

| | |
|---|---|
| `libraries` | regras de biblioteca de imagem; `match` é regex sobre `relpath/nome` |
| `prefer_drop` | pasta de rascunho: se o grupo de duplicatas tem cópia aqui, é essa que sai |
| `protected` | nunca entra em plano nenhum, somado à lista de launcher embutida |
| `mounts` | label → ponto de montagem, usado pelo `import` para ligar um TSV aos discos |

O arquivo é opcional — sem ele o diskmap funciona, só com menos opinião. As suas regras
de biblioteca entram **depois** das regras de origem embutidas (instalação de launcher,
saída de IA, screenshot) e **antes** de todas as outras. Ou seja: uma regra sua ganha de
"o que o arquivo é", mas nunca de "de onde ele veio" — a cópia que o Steam mantém
continua sendo do Steam, mesmo que o nome do seu projeto apareça no caminho.

Ancore nome de pasta com `(^|/)`, nunca com barra à esquerda: `relpath` é relativo à raiz
do volume, então `FBX/x.png` não tem barra antes de `FBX`.

## Imagens: galeria, biblioteca e duplicata visual

```bash
python3 -m diskmap images                      # miniatura + dimensão + hash + biblioteca
python3 -m diskmap images --reclassify         # só refaz a biblioteca (segundos)
python3 -m diskmap images --only-cluster --threshold 8
```

A aba **Imagens** do painel tem duas visões:

- **Galeria** — grade de miniaturas com filtro por biblioteca, volume, formato, dimensão
  mínima e ordem. Clicar abre o visualizador com caminho, dimensão e as ações.
- **Repetidas** — grupos visualmente parecidos lado a lado. A maior de cada grupo vem
  marcada para manter; as outras têm botão de quarentena direto.

### Como funciona sem dependência

Tudo pelo `sips`, que já vem no macOS. Ele lê todos os formatos deste acervo — png, jpg,
dds, tga, psd, tif, exr, hdr, webp, bmp — mas **não extrai pixel de dds**, então dds fica
com metadado e nunca ganha miniatura nem hash.

O hash perceptual é um dHash de 64 bits: o `sips` renderiza um PNG 9×9 a partir da
miniatura e o `zlib` da stdlib decodifica. Mesma imagem redimensionada dá distância baixa;
imagem diferente dá distância alta. O agrupamento usa LSH em quatro faixas de 16 bits, o
que troca uma varredura O(n²) sobre 38 mil hashes por algo que roda em segundos.

Três chamadas de `sips` por imagem — propriedade, miniatura, 9×9 — porque ele recusa ler
propriedade e gerar arquivo na mesma invocação (`Error 6`). Cerca de 26 ms por imagem de
tamanho típico com 8 processos; arquivos de centenas de MB custam muito mais, e o tempo
limite escala com o tamanho até 3 minutos.

Miniatura a 320px em `~/.diskmap/thumbs`, ~15 KB cada. O servidor serve por id numérico
em `/thumb/<id>.jpg` e resolve o caminho no banco — a rota nunca aceita caminho do cliente.

### Bibliotecas

A classificação é uma função pura do caminho, então `--reclassify` custa segundos e nunca
exige reprocessar imagem. As regras estão em `images.LIBRARIES`, e a ordem importa:
origem específica primeiro (jogo instalado, saída de IA, screenshot), depois "isso é meu",
depois asset e textura, e por último as regras que só dizem *onde* o arquivo está
(Downloads, Documents) — essas nunca devem ganhar de uma regra que identifica o conteúdo.

Rode `python3 tests/test_classify.py` depois de mexer nelas.

## O que ele encontra que um buscador de duplicata não encontra

**Árvores clonadas.** Ferramenta de duplicata acha arquivo repetido. Ela não te diz
que a pasta de build de um projeto seu e a instalação que o Steam mantém são 96% a
mesma árvore —
porque isso não é um arquivo, é um padrão entre milhares deles. O `analyze` compara
árvores por impressão digital (nome + tamanho exato) e reporta o par.

**Compactado com a pasta extraída ao lado.** Um `.7z` de 55 GB ao lado da pasta que
saiu dele conta duas vezes o mesmo acervo.

**Tamanho recursivo de pasta.** `dir_rollup` cobra cada byte de todos os diretórios
acima dele, então "onde o peso está" responde na profundidade que você pedir.

**`node_modules` e build de projeto que morreu.** Um buscador de duplicata não olha
isso porque não é duplicata de nada. Veja abaixo.

## Build e dependência de projeto parado

Um `node_modules` de 1,6 GB num projeto que você não abre desde 2024 é desperdício puro:
o `package.json` ao lado recria ele inteiro. A aba **Liberar** mostra essas pastas com
filtro por idade, e elas entram no `plan` como categoria própria:

```bash
python3 -m diskmap plan --out ./plano --category 'build/dep parado'
```

Mover a pasta toda é **um** rename para a subárvore inteira — um `node_modules` de 60 mil
arquivos custa uma syscall, não 60 mil.

### O que decide é a prova, não o nome da pasta

| Veredito | O que é | Pode sair? |
|---|---|---|
| `cache` | a ferramenta refaz sozinha onde quer que esteja (`.gradle`, `.npm`, `__pycache__`) | sim |
| `manifesto` | reconstruível, e o arquivo que reconstrói está na raiz do projeto | sim |
| `sem-prova` | nada no disco prova que isso se recria | **não**, só reportado |
| `vcs` | `.git`, `.svn`, `.hg` | **nunca**, em idade nenhuma |

O nome da pasta não prova nada, e `site-packages` é a armadilha: dentro de um venv é
descartável, mas em `tools_ng/bin/python/App/Lib/site-packages` é o runtime Python que a
ferramenta embarca, e nada no mundo o reconstrói. Por isso ele é `sem-prova` e nunca é
oferecido. `node_modules` sem `package.json` ao lado cai na mesma regra.

E `.git` não é dependência baixada, é o histórico. Se ele não estiver num remoto, apagar
é perda definitiva — então nenhuma regra, nenhum corte de idade e nenhum filtro o
alcançam.

### "Parado" é o projeto, não a pasta

A data vem do arquivo mais recente **do projeto**, não da pasta agregada. A mtime de um
`node_modules` conta quando você rodou `npm install`, o que não diz nada sobre o projeto
estar vivo. Dois `node_modules` idênticos, um de projeto editado anteontem e outro de
projeto parado há dois anos: só o segundo é oferecido.

Como a pasta agregada não contribui com linha nenhuma em `files`, isso sai do índice que
já existe — sem reescanear nada.

## Estrutura

| Arquivo | Papel |
|---|---|
| `db.py` | esquema SQLite; paths são internados em `dirs` para o índice caber em ~1 GB |
| `scan.py` | walker somente-leitura, retomável por raiz, agrega `node_modules`/`.git` |
| `classify.py` | extensão → classe de conteúdo; conjuntos de junk e de compactados |
| `analyze.py` | rollups e achados; tudo derivado, dá para recalcular sempre |
| `verify.py` | hash em dois estágios (parcial, depois completo) |
| `plan.py` | plano de quarentena + undo; nunca apaga |
| `server.py` | API JSON + estáticos, só em 127.0.0.1 |
| `importtsv.py` | importa índice de varredura anterior |
| `actions.py` | ações por caminho; argv declarado em arquivo, alvo validado contra o índice |
| `jobs.py` | roda os comandos do diskmap como subprocesso, com log, lock e cancelamento |
| `images.py` | miniatura, dimensão, hash perceptual e biblioteca, via `sips` |
| `userconf.py` | as regras da sua máquina, lidas de `~/.diskmap/rules.json` |
| `wipe.py` | zero-fill com verificação, e o veredito honesto por volume |
| `tests/test_classify.py` | fixa as regras de biblioteca que já quebraram |
| `tests/test_blobs.py` | fixa o veredito de pasta agregada: `.git` nunca sai |
| `tests/test_wipe.py` | fixa as recusas do zerar (hard link, symlink, pasta) |
| `web/` | front: HTML, CSS e um `app.js` sem build |

## Notas

O `verify` existe porque duplicata por nome + tamanho é um *candidato*. Acerta quase
sempre, mas "quase" não serve para apagar. Um grupo que divergir no hash é marcado
como `mismatch` e nunca entra no plano.

Pastas agregadas (`node_modules`, `.git`, `.venv`, `Pods`…) entram como uma linha com
o total, não arquivo por arquivo. Ser reconstruível não as tira da conta — é o motivo
de serem oferecidas; veja a seção acima.

## Licença

GPL-3.0-or-later. Veja `LICENSE`.
