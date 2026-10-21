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
| Meus discos estão bem? | SMART por disco, e quanto você perderia se aquele morrer |

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

# o que mudou no disco desde a varredura (ensaio)
python3 -m diskmap reconciliar /Volumes/DISCO

# miniaturas
python3 -m diskmap images         # imagens: miniatura, dimensão, hash visual, biblioteca
python3 -m diskmap acervo --cls video   # vídeo, documento e afins

# ver no navegador
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

**Aviso.** Com `--allow-actions` ligado, o painel executa comandos na sua máquina
e move arquivos seus. Você assume o risco de tudo que rodar com essa bandeira
ativa: nem o projeto nem seus autores respondem por perda, corrupção ou
indisponibilidade de dado nenhum. Isto inclui os comandos que você mesmo declarar
no `actions.json`, que rodam exatamente como escritos. O software é distribuído
sem garantia de espécie alguma, nos termos da GPL-3.0 que acompanha o repositório
— leia as seções 15 e 16 da licença. Se o dado importa, tenha backup antes de
ligar a bandeira, e prefira rodar sem ela quando só quiser olhar.

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

## Saúde dos discos (`health`)

```bash
python3 -m diskmap health        # ou a seção no fim da aba Resumo
```

Identidade de cada disco físico (modelo, barramento, SSD ou mecânico, tamanho),
o estado SMART, e — o que nenhum utilitário de SMART sabe — **quanto do seu
acervo mora ali e quanto disso não tem cópia conhecida em outro volume**. SMART
feio num disco inteiramente replicado é aborrecimento; o mesmo SMART no disco que
guarda a única cópia de 300 GB é outra conversa.

### Por que `Verified` não quer dizer saudável

O macOS entrega um booleano e nada mais. Ele só vira `Failing` quando o disco já
passou dos limites do fabricante — tarde demais para servir de aviso. O estudo do
Google de 2007, com mais de cem mil discos, achou que **mais da metade** dos que
falharam nunca acusou nada nos atributos mais fortes.

Por isso, sem `smartctl` o veredito aqui nunca passa de **desconhecido**, mesmo
com o macOS dizendo `Verified`. Fingir que o booleano basta seria o erro.

```bash
brew install smartmontools     # aí sim: os atributos que avisam antes
```

Com ele: setor pendente e setor incorrigível (**crítico** — são os que preveem
falha), setor realocado e erro CRC (atenção), desgaste e reserva de flash, horas
ligado, temperatura. Ponte USB costuma não deixar o comando passar; o `health`
tenta `-d sat` e `-d nvme` antes de desistir.

Desligamento sujo é mostrado mas **não** alarma: toda máquina acumula alguns, e
isso fala de risco de arquivo corrompido, não de disco falhando.

## Atividade: quem está lendo e gravando agora (`io`)

```bash
python3 -m diskmap io                 # uma janela de 2 s
python3 -m diskmap io --watch         # fica repetindo até Ctrl-C
```

Ou a aba **Atividade** no painel, que se atualiza sozinha a cada 2 segundos.

São **duas** medidas do kernel, e elas respondem perguntas diferentes:

| | |
|---|---|
| por disco | `ioreg`, classe `IOBlockStorageDriver`: bytes lidos e gravados por dispositivo, separados |
| por processo | `proc_pid_rusage`: bytes lidos e gravados por processo — é o que o Monitor de Atividade mostra |

### Por que a soma dos processos não fecha com a soma dos discos

Porque o kernel **não cruza as duas**: o contador do processo não guarda em qual
disco o byte caiu. A coluna "volumes abertos" vem do `lsof` — quais volumes o
processo tem abertos neste instante — e é **palpite de onde ele escreve, não
prova de onde o byte caiu**. Um processo com arquivo aberto em dois discos
aparece nos dois.

Três outros motivos, todos esperados:

- processo de outro usuário (root, em geral) recusa o contador sem `sudo` — é por
  isso que o `backupd` escrevendo no disco do Time Machine aparece no disco e não
  na lista de processos;
- processo que nasceu e morreu dentro da janela não tem dois pontos para
  subtrair, então some da lista — mas o contador do dispositivo o inclui;
- leitura servida pelo cache de página não toca o disco.

O cruzamento exato existe — `sudo fs_usage -w -f filesys` mostra o caminho de
cada chamada — mas exige root, e nada aqui pede root para olhar.

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

A sub-aba **Imagens** do Acervo é a especialista: além da miniatura, ela mede
dimensão, calcula um hash perceptual e separa o acervo em bibliotecas.

```bash
python3 -m diskmap images                      # miniatura + dimensão + hash + biblioteca
python3 -m diskmap images --reclassify         # só refaz a biblioteca (segundos)
python3 -m diskmap images --only-cluster --threshold 8
```

Ela tem duas visões:

- **Galeria** — grade de miniaturas com filtro por biblioteca, volume, formato, dimensão
  mínima e ordem. Clicar abre o visualizador com caminho, dimensão e as ações.
- **Repetidas** — grupos visualmente parecidos lado a lado. A maior de cada grupo vem
  marcada para manter; as outras têm botão de quarentena direto.

### Como funciona sem dependência

Tudo pelo `sips`, que já vem no macOS. Ele lê todos os formatos deste acervo — png, jpg,
dds, tga, psd, tif, exr, hdr, webp, bmp — mas **não extrai pixel de dds**, então dds fica
com metadado e nunca ganha miniatura nem hash.

### Sidecar do macOS nunca vira miniatura

Em volume sem atributo estendido nativo, o macOS grava um irmão `._<original>`
ao lado do arquivo: o fork AppleDouble. Ele herda a extensão do original, então
`._notas.txt` parece texto e `._foto.jpg` parece imagem. Nenhum dos dois é.
Renderizar um deles produz um retângulo de binário embaralhado que, na grade,
não se distingue de um documento de verdade.

Num acervo real isso não é detalhe: 1,2 milhão deles, um quinto da classe
`doc` e um nono das imagens. O índice continua listando o sidecar — ele existe
e ocupa espaço —, mas `images` e `acervo` não gastam um processo para
desenhá-lo.

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

## Etiquetas

Uma etiqueta é a única coisa neste índice que não se recalcula — tudo o mais é
derivado — então ela é guardada pelo **caminho**, e não pelo id do arquivo: um
rescan apaga as linhas e o SQLite reaproveita os ids, de modo que qualquer coisa
guardada por id reaparece pendurada em outro arquivo.

Etiquete uma pasta e vale para tudo que está dentro, inclusive o que entrar
depois. Na busca e no acervo elas aparecem como balões: clicar filtra, o × tira.
Uma etiqueta pode viver dentro de outra, e filtrar pela mãe traz as filhas.

Para marcar muita coisa de uma vez há seleção por caixa, com uma barra que
etiqueta ou desetiqueta o conjunto inteiro — e que lembra, quando você selecionou
arquivos soltos, que etiquetar a pasta costuma ser a resposta melhor.

### Etiqueta por artista, lida do arquivo

```bash
python3 -m diskmap artistas              # ensaio: mostra o que criaria
python3 -m diskmap artistas --aplicar    # grava, sob a mãe "Artistas"
python3 -m diskmap artistas --min 1 --aplicar   # inclui artista de um arquivo só
```

Artista é o caso raro em que o juízo já está escrito: o arquivo diz quem fez.
Lê com `ffprobe` (sem ele, o comando recusa e explica), guarda em `audio_meta`
para ser retomável, e cria uma sub-etiqueta por artista dentro de `Artistas`.

Duas coisas que a medida mudou no desenho óbvio. **A minoria dos arquivos tem o
campo** — numa amostra de 120, 20 tinham artista; `.wav` quase nunca tem — então
o relatório diz quantos ficaram mudos em vez de deixar a impressão de que o que
foi etiquetado é o acervo. E **o campo não é limpo**: guarda `Unknown Artist`,
nome de coletânea e a mesma banda em três grafias. Por isso há uma lista de
não-artistas, o agrupamento ignora maiúscula, e o piso `--min 2` segura o avulso.

Ensaio é o default, porque etiqueta é o único dado aqui que não se recalcula.

Renomear uma etiqueta: **dois cliques no balão**. Só o nome muda — o que ela
cobre e onde está na árvore apontam para o id, não para o texto.

## Acervo: o mesmo disco visto por tipo

A aba **Acervo** responde "o que eu tenho?" separado em Imagens, Documentos,
Vídeos, Música, Jogos e Outros, cada um com os filtros que fazem sentido —
volume, formato, ordem, e "só com miniatura". Jogos é categoria própria porque
num acervo com jogos instalados ela costuma ser a maior de todas: empacotamento
(`.pak`, `.rpf`, `.vpk`, `.ucas`) que nenhuma ferramenta abre e que, somado,
pesa mais que o resto junto.

Em **Música**, a miniatura é a capa embutida no arquivo — e só ela. O QuickLook
devolve uma nota musical genérica para quem não tem capa, idêntica para todos e
indistinguível de uma capa real na tela; o `ffprobe` responde se existe capa em
0,06 s, contra 0,4 s para renderizar um ícone falso. Quem não tem capa fica com
o ícone desenhado, que ao menos não finge.

A miniatura é real sempre que a máquina souber fazê-la: o `qlmanage` do macOS
renderiza a primeira página de um PDF ou de uma planilha e um quadro dos vídeos
que o QuickLook abre; o `ffmpeg`, **se estiver instalado**, cobre mkv, avi, wmv,
flv e webm. O que ninguém sabe renderizar ganha um ícone desenhado em SVG pela
família da extensão — nada é baixado, e a ferramenta continua funcionando
offline.

```bash
python3 -m diskmap acervo --cls video        # ou doc, audio, archive…
python3 -m diskmap acervo --limit 500        # começa pelos maiores
```

## O que ele encontra que um buscador de duplicata não encontra

### Reconciliar em vez de reindexar

`scan` sabe acrescentar, não sabe reconciliar: o laço dele só faz `INSERT`, não
remove o que sumiu, e uma raiz já concluída é pulada. A única saída era
`--rescan`, que descarta o índice do volume — e com ele `image_meta`,
`media_meta`, `file_hash` e `audio_meta` daquele volume, porque `files.id` é
`INTEGER PRIMARY KEY` e o rowid liberado volta a ser distribuído. Num acervo já
processado isso custa horas de miniatura para corrigir algumas dezenas de
linhas.

`reconciliar` compara por **caminho**, não por id, e é isso que permite guardar
o trabalho:

| caso | o id | a miniatura e o hash |
|---|---|---|
| não mudou | fica | **ficam** |
| mudou (tamanho ou mtime) | fica | saem — descrevem o conteúdo velho |
| sumiu | sai | saem |
| apareceu | novo | não existem, e é o certo |

Hash desatualizado é pior que hash nenhum: parece prova. Etiqueta nunca é
tocada — ela é por caminho, e é o único dado aqui que varredura nenhuma
recomputa.

Ensaio é o padrão; só escreve com `--aplicar`.

**Árvores clonadas.** Ferramenta de duplicata acha arquivo repetido. Ela não te diz
que a pasta de build de um projeto seu e a instalação que o Steam mantém são 96% a
mesma árvore —
porque isso não é um arquivo, é um padrão entre milhares deles. O `analyze` compara
árvores por impressão digital (nome + tamanho exato) e reporta o par.

No painel, o número de arquivos iguais é clicável: a seta abre a lista do que
sustenta o par — nome, tamanho, o caminho de cada lado, a data de cada cópia e o
veredito de hash de quem já passou pelo `verify`. É a diferença entre "estas duas
pastas têm 23,6 GB iguais" e "são estes 12 arquivos, e destes, N estão provados".

Cada lado da linha também diz se o arquivo **ainda está lá**. O índice é uma
fotografia: entre a varredura e agora a cópia pode ter ido para a quarentena pelo
próprio painel, para a Lixeira, ou ter sido apagada por fora. Quem saiu do lugar
ganha etiqueta, as ações daquela linha ficam desligadas, e um aviso no topo diz
quantas das cópias listadas não estão mais onde o índice as viu. Volume
desconectado é dito com essas palavras e não como "apagado" — num disco que não
está plugado todo arquivo some do `exists`, e as duas afirmações pedem coisas
bem diferentes de você.

Com `--allow-actions`, cada linha dessa lista oferece duas ações, e elas fazem
coisas diferentes: **descartar** manda uma das cópias para a quarentena e libera
o espaço dela; **sincronizar** faz um lado virar cópia do outro e não libera
nada — serve para quando as duas divergiram.

Nas duas, o arquivo que sai vai para a quarentena (nada é apagado, e o histórico
desfaz), caminho de instalação de launcher é recusado, e o aviso muda com a
prova de hash: igual pede só confirmação, diferente avisa que são versões
distintas, e sem prova aparece um botão para conferir na hora. Depois da ação o
painel avisa que o par continua na lista até você reindexar — os achados são
recalculados, não observados.

Na aba **Duplicatas** o mesmo gesto resolve o grupo inteiro: a seta ao lado do
número de cópias abre todas elas, com o caminho, a data e o motivo pelo qual cada
uma é ou não a melhor candidata a ficar. Você escolhe a sobrevivente — o padrão
já vem da regra que protege instalações de launcher e descarta build e dumps
primeiro — e um botão manda as outras para a quarentena.

O grupo divergente é recusado: se o hash provou que as cópias têm conteúdo
diferente, não há redundância para remover. E cópia que sumiu do disco desde a
última varredura aparece marcada, porque o índice é uma fotografia.

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
| `acervo.py` | miniatura do que não é imagem: `qlmanage`, e `ffmpeg` se existir |
| `tags.py` | etiquetas do usuário; guardadas por caminho, não por id de arquivo |
| `iowatch.py` | I/O ao vivo por disco e por processo |
| `userconf.py` | as regras da sua máquina, lidas de `~/.diskmap/rules.json` |
| `wipe.py` | zero-fill com verificação, e o veredito honesto por volume |
| `health.py` | SMART por disco físico, cruzado com o que o índice guarda |
| `tests/test_classify.py` | fixa as regras de biblioteca que já quebraram |
| `tests/test_blobs.py` | fixa o veredito de pasta agregada: `.git` nunca sai |
| `tests/test_wipe.py` | fixa as recusas do zerar (hard link, symlink, pasta) |
| `tests/test_health.py` | fixa a leitura do smartctl com saída real |
| `tests/test_tags.py` | fixa que a etiqueta sobrevive a um rescan |
| `tests/test_acervo.py` | fixa qual renderizador atende cada formato |
| `tests/test_overlap.py` | fixa que a lista de um par bate com a contagem dele |
| `tests/test_resolve_clone.py` | fixa as travas da única escrita que substitui arquivo |
| `tests/test_iowatch.py` | fixa a leitura dos contadores de I/O |
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
