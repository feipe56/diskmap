/* SPDX-License-Identifier: GPL-3.0-or-later */
/* diskmap frontend: fetches the local JSON API and renders. No build step. */
'use strict';

const H = (b) => {
  b = Number(b || 0);
  const u = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let i = 0;
  while (Math.abs(b) >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  const s = (i <= 1) ? b.toFixed(0) : b.toFixed(1);
  return s.replace('.', ',') + ' ' + u[i];
};
const N = (n) => Number(n || 0).toLocaleString('pt-BR');
const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const el = (id) => document.getElementById(id);

const api = async (path) => {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  const j = await r.json();
  if (j && j.error) throw new Error(j.error);
  return j;
};

const CLS = {
  gamedata: 'Dados de jogo instalado', archive: 'Compactados', outros: 'Outros / não classificado',
  binary: 'Binários e libs', model3d: 'Modelos 3D e asset packs', sem_ext: 'Sem extensão',
  image: 'Imagens e texturas', video: 'Vídeo', junk: 'Dumps, logs, temp', code: 'Código-fonte',
  ml_model: 'Modelos de IA', audio: 'Áudio', db: 'Bancos de dados', doc: 'Documentos',
};
const AGE = {
  '<6m': 'menos de 6 meses', '6m-1a': '6 meses a 1 ano', '1-2a': '1 a 2 anos',
  '2-3a': '2 a 3 anos', '3-5a': '3 a 5 anos', '>5a': 'mais de 5 anos',
  desconhecido: 'data desconhecida',
};
const CAT = {
  duplicata: 'Cópias idênticas (mesmo nome e tamanho)',
  'arquivo+extraido': 'Compactado com a pasta extraída ao lado',
  'dump/temp': 'Crash dumps, .tmp e cache',
  'build/dep parado': 'node_modules e build de projeto parado',
};
const VERDICT = {
  cache: ['Cache', 'A ferramenta refaz sozinha, onde quer que esteja.'],
  manifesto: ['Reconstruível', 'O manifesto que recria está na raiz do projeto.'],
  'sem-prova': ['Sem prova', 'Nada no disco prova que isso se reconstrói. Só reportado.'],
  vcs: ['Repositório', 'É o histórico em si. Nunca é oferecido, em idade nenhuma.'],
};

/* ---------- small renderers ---------- */
function bars(items, { color } = {}) {
  if (!items.length) return '<div class="empty">sem dados</div>';
  const mx = Math.max(...items.map((i) => i.value)) || 1;
  return '<div class="bars">' + items.map((i) => {
    const c = (color && color(i)) || 'var(--accent)';
    return `<div class="bar"><div class="bar-l">${esc(i.label)}</div>
      <div class="bar-t" data-tip="${esc(i.tip || i.label + ': ' + H(i.value))}">
      <div class="bar-f" style="width:${(100 * i.value / mx).toFixed(1)}%;background:${c}"></div></div>
      <div class="bar-v">${H(i.value)}</div></div>`;
  }).join('') + '</div>';
}

function table(cols, rows) {
  if (!rows.length) return '<div class="empty">nada encontrado</div>';
  return `<div class="tw"><table><thead><tr>${
    cols.map((c) => `<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${
    rows.map((r) => `<tr>${r.join('')}</tr>`).join('')}</tbody></table></div>`;
}

const tdp = (s) => `<td class="path"><span class="hi">${esc(s)}</span></td>`;
const tdn = (s) => `<td class="num">${s}</td>`;
const fullPath = (r) => (r.relpath ? `${r.label}/${r.relpath}/${r.name}` : `${r.label}/${r.name}`);

/* Absolute paths, rebuilt from each volume's mount point. An action is only ever
   offered for a volume that is actually mounted. */
const mountOf = (label) => {
  const v = ((OV && OV.volumes) || []).find((x) => x.label === label);
  return v && v.mount ? v.mount.replace(/\/$/, '') : null;
};
const joinp = (...p) => p.filter(Boolean).join('/');
const abs = (r) => { const m = mountOf(r.label); return m ? joinp(m, r.relpath, r.name) : null; };
const absDir = (r) => { const m = mountOf(r.label); return m ? joinp(m, r.relpath) : null; };
const absRel = (label, rel) => { const m = mountOf(label); return m ? joinp(m, rel) : null; };

/* ---------- actions ---------- */
let TOKEN = window.DISKMAP_TOKEN || null;

/* O token nasce a cada start do servidor e é capturado uma vez, no carregamento
   da página. Reinicie o servidor e toda aba aberta passa a mandar um token
   morto: "token invalido" em tudo que escreve, sem pista do motivo.

   Buscar o token de novo é seguro porque a busca é na MESMA origem: o
   `_origin_ok` do servidor já recusa qualquer outra, e uma página de fora não
   consegue ler a resposta de um fetch cross-origin. O token continua sendo a
   segunda barreira que ele sempre foi — ele só deixa de exigir um F5 manual
   quando a primeira ainda está de pé. */
async function renovaToken() {
  const r = await fetch('/', { cache: 'no-store' });
  const txt = await r.text();
  const m = txt.match(/window\.DISKMAP_TOKEN\s*=\s*"([^"]+)"/);
  if (!m) throw new Error('o servidor não devolveu um token novo');
  TOKEN = m[1];
  return TOKEN;
}
const ACTIONS_ON = !!window.DISKMAP_ACTIONS;
let ACTS = [];

function toast(msg, kind) {
  const box = el('toasts');
  const t = document.createElement('div');
  t.className = 'toast' + (kind ? ' ' + kind : '');
  t.textContent = msg;
  box.appendChild(t);
  setTimeout(() => t.remove(), kind === 'err' ? 9000 : 5000);
}

const post = async (path, body, _retry) => {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Diskmap-Token': TOKEN || '' },
    body: JSON.stringify(body),
  });
  const j = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (!r.ok || j.error) {
    // Uma vez só: se o token morreu porque o servidor reiniciou, pega o novo e
    // repete. Duas vezes seria laço quando a recusa for por outro motivo.
    if (!_retry && /token/i.test(j.error || '')) {
      try { await renovaToken(); } catch (e) { throw new Error(j.error); }
      return post(path, body, true);
    }
    throw new Error(j.error || `HTTP ${r.status}`);
  }
  return j;
};

const sheet = () => el('sheet');
function closeSheet() { sheet().hidden = true; sheet().innerHTML = ''; }

/* The button an actionable row carries. The path travels in a dataset attribute,
   so it is never interpolated into HTML as code. */
function actCell(path) {
  if (!ACTIONS_ON || !path) return '<td class="act"></td>';
  return `<td class="act"><button class="actbtn" data-act-path="${esc(path)}">ações</button></td>`;
}

/* Zerar é a única ação sem volta, então ela tem o seu próprio passo: primeiro
   pergunta ao servidor o que um zero-fill vale naquele volume, e só então deixa
   confirmar. O usuário decide vendo o motivo, não um aviso genérico. */
const WIPE_LEVEL = {
  efetivo: ['ok', 'Efetivo aqui'],
  parcial: ['warn', 'Garantia parcial'],
  fraco: ['crit', 'Garantia fraca'],
  inutil: ['crit', 'Não funciona aqui'],
  impossivel: ['crit', 'Impossível aqui'],
};

async function confirmWipe(a, path) {
  sheet().innerHTML = `<div class="sheet-in"><h3>${esc(a.name)}</h3>
    <div class="sheet-path">${esc(path)}</div><pre class="out">conferindo o volume…</pre></div>`;
  const foot = '<div class="sheet-foot"><button class="btn" data-close>Fechar</button></div>';
  let w;
  try {
    w = await api(`/api/wipe-check?path=${encodeURIComponent(path)}`);
  } catch (e) {
    sheet().innerHTML = `<div class="sheet-in"><h3>${esc(a.name)}</h3>
      <div class="sheet-path">${esc(path)}</div>
      <div class="warnbox crit">${esc(e.message)}</div>${foot}</div>`;
    sheet().querySelector('[data-close]').addEventListener('click', closeSheet);
    return;
  }
  const [cls, title] = WIPE_LEVEL[w.level] || ['crit', w.level];
  const refusals = w.refusals || [];
  const blocked = !w.allowed || refusals.length;
  const facts = [`${esc(w.fs)}`,
    w.rotational === true ? 'disco mecânico' : w.rotational === false ? 'memória flash' : null,
    w.snapshots ? `${N(w.snapshots)} snapshots` : null,
    w.read_only ? 'montado somente-leitura' : null].filter(Boolean).join(' · ');
  const head = `<h3>${esc(a.name)}</h3><div class="sheet-path">${esc(path)}</div>`;
  const facha = `<p class="note">${esc(w.label)} · ${facts}${
    w.size != null ? ` · ${H(w.size)}` : ''}</p>`;

  /* Quando não dá para zerar, a recusa é a manchete e o veredito do volume
     desce para rodapé: ele responde "quanto isso vale aqui", e essa pergunta
     nem chega a existir. Antes a ordem era a inversa e a tela parecia um
     diálogo de confirmação a que faltava o botão. */
  if (blocked) {
    const pasta = refusals.some((r) => r.indexOf('pasta') >= 0);
    sheet().innerHTML = `<div class="sheet-in" role="dialog" aria-modal="true">
      ${head}
      <div class="warnbox crit"><b>Não dá para zerar isto.</b><br>
        ${refusals.length ? refusals.map(esc).join('<br>') : esc(w.why)}</div>
      ${pasta ? `<p class="note">Zerar sobrescreve byte a byte, então só alcança
        arquivo. Para a pasta inteira, use <b>Mover para a Lixeira</b> ou
        <b>Mover para quarentena</b> — as duas têm volta.</p>` : ''}
      ${facha}
      <div class="sheet-foot"><button class="btn" data-close>Fechar</button></div>
    </div>`;
    sheet().querySelector('[data-close]').addEventListener('click', closeSheet);
    return;
  }

  sheet().innerHTML = `<div class="sheet-in" role="dialog" aria-modal="true">
    ${head}
    <div class="warnbox ${cls}"><b>${esc(title)}.</b> ${esc(w.why)}</div>
    ${facha}
    <div class="warnbox crit"><b>Isto não tem undo.</b>
      Sobrescreve cada byte com zero, confere a leitura e apaga o arquivo.
      A aba Histórico vai registrar, mas não vai conseguir desfazer.</div>
    <p class="note">Para confirmar, digite <b>ZERAR</b>:</p>
    <input id="wipe-ok" type="text" class="textin" autocomplete="off" spellcheck="false"
           placeholder="ZERAR">
    <div class="sheet-foot">
      <button class="btn" data-close>Cancelar</button>
      <button class="btn danger" data-go disabled>Zerar e apagar</button>
    </div></div>`;
  sheet().querySelector('[data-close]').addEventListener('click', closeSheet);
  if (blocked) return;
  const inp = el('wipe-ok');
  const go = sheet().querySelector('[data-go]');
  inp.addEventListener('input', () => { go.disabled = inp.value.trim() !== 'ZERAR'; });
  go.addEventListener('click', () => {
    if (inp.value.trim() === 'ZERAR') runAction(a, path);
  });
  inp.focus();
}

function openActions(path) {
  const danger = (a) => a.id === 'trash' || a.id === 'quarantine' || a.id === 'wipe';
  sheet().innerHTML = `<div class="sheet-in" role="dialog" aria-modal="true" aria-label="Ações">
    <h3>Ações</h3>
    <div class="sheet-path">${esc(path)}</div>
    <div class="actlist">${ACTS.map((a) => `
      <button class="actrow${danger(a) ? ' danger' : ''}" data-run="${esc(a.id)}">
        <span><span class="an">${esc(a.name)}</span>
        ${a.help ? `<span class="ah">${esc(a.help)}</span>` : ''}</span>
        ${a.irreversible ? '<span class="amark">destrói, sem undo</span>'
          : (a.mutates ? '<span class="amark">move o arquivo</span>'
          : (a.capture ? '<span class="amark">mostra saída</span>' : ''))}
      </button>`).join('')}</div>
    <div class="sheet-foot"><button class="btn" data-close>Fechar</button></div>
  </div>`;
  sheet().hidden = false;
  editorDeTags(path);
  sheet().querySelectorAll('[data-run]').forEach((b) => {
    b.addEventListener('click', () => {
      const a = ACTS.find((x) => x.id === b.dataset.run);
      if (!a) return;
      if (a.id === 'wipe') confirmWipe(a, path);
      else if (a.confirm || a.mutates) confirmAction(a, path);
      else runAction(a, path);
    });
  });
  sheet().querySelector('[data-close]').addEventListener('click', closeSheet);
}

/* O editor de etiquetas mora dentro da folha de ações porque é lá que já está
   o caminho em questão. Ele se desenha depois, sozinho: as etiquetas vêm de
   outra consulta e a folha não deve esperar por ela para abrir. */
async function editorDeTags(path) {
  const caixa = document.createElement('div');
  caixa.className = 'tagbox';
  caixa.innerHTML = '<div class="empty">etiquetas…</div>';
  const alvo = sheet().querySelector('.sheet-in');
  if (!alvo) return;
  alvo.insertBefore(caixa, alvo.querySelector('.sheet-foot'));
  await carregaTags();
  let minhas = [];
  try { minhas = (await api('/api/path-tags?path=' + encodeURIComponent(path))).tags || []; }
  catch (_) { minhas = []; }
  const pinta = () => {
    caixa.innerHTML = `<div class="tagtit">etiquetas</div>
      <div class="chips">
        ${minhas.map((t) => `<span class="chip on${t.herdada ? ' herd' : ''}"
          title="${t.herdada ? 'vem da pasta ' + esc(t.de) : 'nesta própria coisa'}"
          >${esc(t.name)}${t.herdada ? '' :
            `<button data-tagoff="${esc(t.name)}" aria-label="tirar ${esc(t.name)}">×</button>`}
          </span>`).join('') || '<span class="cart-meta">nenhuma ainda</span>'}
      </div>
      <div class="row" style="margin-top:8px">
        <input class="textin" id="tagnova" list="taglist" placeholder="nova etiqueta…"
          maxlength="48" style="flex:1">
        <datalist id="taglist">${TAGS.map((t) =>
          `<option value="${esc(t.name)}">`).join('')}</datalist>
        <select id="tagpai" title="dentro de qual etiqueta">
          <option value="">solta</option>
          ${TAGS.filter((t) => !t.parent).map((t) =>
            `<option value="${esc(t.name)}">dentro de ${esc(t.name)}</option>`).join('')}
        </select>
        <button class="btn" data-tagadd>adicionar</button>
      </div>
      <p class="cart-meta" style="margin-top:6px">Etiqueta em pasta vale para
        tudo que está dentro, inclusive o que entrar depois.</p>`;
    caixa.querySelectorAll('[data-tagoff]').forEach((b) =>
      b.addEventListener('click', () => mexe('remove', b.dataset.tagoff)));
    const add = caixa.querySelector('[data-tagadd]');
    const inp = caixa.querySelector('#tagnova');
    const sel = caixa.querySelector('#tagpai');
    const manda = () => {
      const v = (inp.value || '').trim();
      if (v) mexe('add', v, sel && sel.value ? sel.value : null);
    };
    add.addEventListener('click', manda);
    inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') manda(); });
  };
  const mexe = async (acao, name, pai) => {
    caixa.innerHTML = '<div class="empty">gravando…</div>';
    try {
      await post('/api/tag', { acao, name, path, pai });
      await carregaTags();
      minhas = (await api('/api/path-tags?path=' + encodeURIComponent(path))).tags || [];
      toast(acao === 'add' ? `etiquetado: ${name}` : `tirado: ${name}`, 'ok');
      loaded.busca = false;
    } catch (e) { toast(e.message, 'err'); }
    pinta();
  };
  pinta();
}

function confirmAction(a, path) {
  const where = a.id === 'trash'
    ? 'Vai para a Lixeira do volume. Dá para recuperar com “Colocar de volta” no Finder.'
    : a.id === 'quarantine'
      ? 'Vai para <code>.diskmap-quarantine</code> no mesmo volume. O painel desfaz na aba Histórico.'
      : 'Isso roda o comando que você declarou no actions.json.';
  sheet().innerHTML = `<div class="sheet-in" role="dialog" aria-modal="true">
    <h3>${esc(a.name)}</h3>
    <div class="sheet-path">${esc(path)}</div>
    <div class="warnbox${a.id === 'trash' ? ' crit' : ''}">${where}</div>
    <div class="sheet-foot">
      <button class="btn" data-close>Cancelar</button>
      <button class="btn ${a.id === 'trash' ? 'danger' : 'primary'}" data-go>
        ${a.mutates ? 'Mover' : 'Executar'}</button>
    </div></div>`;
  sheet().querySelector('[data-close]').addEventListener('click', closeSheet);
  sheet().querySelector('[data-go]').addEventListener('click', () => runAction(a, path));
}

async function runAction(a, path) {
  sheet().innerHTML = `<div class="sheet-in"><h3>${esc(a.name)}</h3>
    <div class="sheet-path">${esc(path)}</div><pre class="out">executando…</pre></div>`;
  const foot = '<div class="sheet-foot"><button class="btn" data-close>Fechar</button></div>';
  try {
    const res = await post('/api/action', { id: a.id, path });
    sheet().innerHTML = `<div class="sheet-in"><h3>${esc(a.name)}</h3>
      <div class="sheet-path">${esc(res.path)}</div>
      <div class="cmd">${esc(res.argv)}</div>
      <pre class="out">${esc(res.output || (res.ok ? 'feito.' : 'sem saída'))}</pre>
      ${foot}</div>`;
    toast(res.ok ? `${a.name}: ok` : `${a.name}: código ${res.rc}`, res.ok ? 'ok' : 'err');
    if (a.mutates) { loaded.historico = false; loaded.resumo = false; }
  } catch (e) {
    sheet().innerHTML = `<div class="sheet-in"><h3>${esc(a.name)}</h3>
      <div class="sheet-path">${esc(path)}</div>
      <div class="warnbox crit">${esc(e.message)}</div>${foot}</div>`;
    toast(e.message, 'err');
  }
  sheet().querySelector('[data-close]').addEventListener('click', closeSheet);
}

function offBanner(what) {
  return `<div class="phead"><p class="eyebrow">desligado</p><h2>${esc(what)}</h2></div>
    <div class="offnote"><b>As ações vêm desligadas por padrão.</b> Um servidor em
    localhost que executa comandos é alcançável por qualquer página aberta no seu
    navegador, então isso não fica ligado sem você pedir. Para ligar:
    <code>python3 -m diskmap serve --allow-actions</code>
    <p style="margin-top:10px;color:var(--fg-3);font-size:.82rem">Ligado, o painel ganha
    abrir no Finder, mover para quarentena ou Lixeira (com undo), e os comandos que você
    declarar em <code>~/.diskmap/actions.json</code>. Nenhum texto digitado no navegador
    chega a um shell: o painel manda o <em>nome</em> da ação, e o comando vem do seu arquivo.</p></div>`;
}

async function panelHistorico() {
  const target = el('p-historico');
  if (!ACTIONS_ON) { target.innerHTML = offBanner('Histórico de ações'); return; }
  const rows = await api('/api/log?limit=120');
  let h = `<div class="phead"><p class="eyebrow">auditoria</p><h2>Histórico de ações</h2>
    <p>Tudo que o painel executou, com o comando exato. O que foi para a quarentena
    volta daqui.</p></div>`;
  if (!rows.length) { target.innerHTML = h + '<div class="empty">nenhuma ação ainda</div>'; return; }
  h += table(['Quando', 'Ação', 'Alvo', 'Saída', ''], rows.map((r) => [
    tdn(new Date(r.at * 1000).toLocaleString('pt-BR')),
    `<td>${esc(r.action)} ${r.rc === 0 ? '' : `<span class="tag crit">rc ${r.rc}</span>`}</td>`,
    tdp(r.path),
    `<td class="path">${esc((r.output || '').slice(0, 160))}</td>`,
    r.moved_to ? `<td class="act"><button class="actbtn" data-undo="${r.id}">desfazer</button></td>`
      : '<td class="act"></td>',
  ]));
  target.innerHTML = h;
  target.querySelectorAll('[data-undo]').forEach((b) => {
    b.addEventListener('click', async () => {
      try {
        const res = await post('/api/undo', { log_id: Number(b.dataset.undo) });
        toast('restaurado: ' + res.restored, 'ok');
        panelHistorico();
      } catch (e) { toast(e.message, 'err'); }
    });
  });
}

/* ---------- tarefas: diskmap's own commands, run as background jobs ---------- */
let MOUNTS = [];
let JOB_SEL = null;      // job id whose log is open
let JOB_SEEN = 0;        // last log line number received
let JOB_TIMER = null;

/* One descriptor per command. `fields` drives the form; the server rebuilds the
   argv from these values, so nothing here is a command line. */
const CMDS = [
  {
    kind: 'scan', title: 'Indexar um volume', cid: 'scan',
    desc: 'Percorre o disco e grava metadado. Só leitura, e retomável: se parar no '
      + 'meio, rodar de novo continua de onde ficou.',
    fields: [
      { k: 'mount', label: 'volume', type: 'mount' },
      { k: 'label', label: 'rótulo', type: 'text', hint: 'em branco usa o nome da pasta' },
      { k: 'roots', label: 'só estas subpastas', type: 'list',
        hint: 'separadas por vírgula; em branco indexa tudo' },
      { k: 'exclude', label: 'ignorar pastas com estes nomes', type: 'list' },
      { k: 'rescan', label: 'descartar o índice deste volume e começar de novo',
        type: 'check' },
    ],
    go: 'Indexar',
  },
  {
    kind: 'reconciliar', title: 'Conferir o índice com o disco', cid: 'reconciliar',
    desc: 'Diz o que sumiu, apareceu ou mudou desde a última varredura. Ensaio: '
      + 'só escreve se você marcar abaixo. Ao contrário de "descartar o índice", '
      + 'quem não mudou mantém a miniatura e o hash que já tem.',
    fields: [
      { k: 'mount', label: 'volume', type: 'mount' },
      { k: 'label', label: 'rótulo', type: 'text', hint: 'em branco usa o nome da pasta' },
      { k: 'roots', label: 'só estas subpastas', type: 'list',
        hint: 'separadas por vírgula; em branco confere o volume todo' },
      { k: 'exclude', label: 'ignorar pastas com estes nomes', type: 'list' },
      { k: 'aplicar', label: 'aplicar no índice (sem isto é só relatório)',
        type: 'check' },
    ],
    go: 'Conferir',
  },
  {
    kind: 'analyze', title: 'Recalcular achados', cid: 'analyze',
    desc: 'Refaz rollups, duplicatas, árvores clonadas e pares compactado+extraído. '
      + 'Não perde a verificação por hash: ela é derivada dos hashes já gravados.',
    fields: [
      { k: 'depth', label: 'profundidade da árvore comparada', type: 'number',
        def: 2, min: 1, max: 6, hint: '2 compara em nível de projeto' },
    ],
    go: 'Analisar',
  },
  {
    kind: 'verify', title: 'Confirmar duplicatas por hash', cid: 'verify',
    desc: 'Lê o conteúdo para provar que as cópias são iguais. Quem divergir é '
      + 'marcado e nunca entra no plano.',
    fields: [
      { k: 'stage', label: 'estágio', type: 'select',
        opts: [['quick', 'parcial — 64 KB por arquivo'], ['full', 'completo — sha256 inteiro']] },
      { k: 'limit', label: 'só os N grupos maiores', type: 'number',
        hint: 'em branco verifica todos' },
      { k: 'min_size', label: 'tamanho mínimo em bytes', type: 'number' },
    ],
    go: 'Verificar',
  },
    {
    kind: 'acervo', title: 'Gerar miniaturas do acervo', cid: 'acervo',
    desc: 'Vídeo, documento e afins. Usa o qlmanage do macOS, e o ffmpeg quando '
      + 'ele existe, para os formatos de vídeo que o QuickLook não abre. '
      + 'Deixe a classe em branco para processar todas.',
    fields: [
      { k: 'cls', label: 'classe', type: 'select',
        opts: [['video', 'vídeos'], ['doc', 'documentos'], ['audio', 'áudio'],
               ['archive', 'compactados'], ['', 'todas que não são imagem']] },
      { k: 'limit', label: 'quantos arquivos no máximo', type: 'number', def: 500,
        min: 1, hint: 'começa pelos maiores' },
      { k: 'refresh', label: 'refazer o que já foi tentado', type: 'check' },
    ],
    go: 'Gerar',
  },
{
    kind: 'images', title: 'Processar imagens', cid: 'images',
    desc: 'Gera miniatura, dimensão e hash visual, e classifica cada imagem numa '
      + 'biblioteca. Usa o sips do macOS — ~26 ms por imagem com 8 processos.',
    fields: [
      { k: 'min_size', label: 'tamanho mínimo em bytes', type: 'number', def: 100000,
        hint: '100000 cobre 59,5 dos 77 GB e ignora ícone' },
      { k: 'workers', label: 'processos em paralelo', type: 'number', def: 8, min: 1, max: 32 },
      { k: 'threshold', label: 'distância que conta como parecida', type: 'number',
        def: 10, min: 0, max: 32, hint: 'de 64 bits; 10 é o padrão' },
      { k: 'include_dds', label: 'incluir dds e ico (só metadado)', type: 'check' },
      { k: 'refresh', label: 'reprocessar o que já tem metadado', type: 'check' },
      { k: 'only_cluster', label: 'só reagrupar, sem processar imagem nova', type: 'check' },
    ],
    go: 'Processar',
  },
  {
    kind: 'plan', title: 'Gerar plano de quarentena', cid: 'plan',
    desc: 'Escreve plan.tsv, apply.sh e undo.sh na sua home. Não move nada: '
      + 'quem roda o apply.sh é você.',
    fields: [
      { k: 'name', label: 'nome da pasta (dentro da sua home)', type: 'text',
        def: 'diskmap-plan' },
      { k: 'categories', label: 'categorias', type: 'cats' },
      { k: 'min_size', label: 'tamanho mínimo em bytes', type: 'number' },
      { k: 'unverified', label: 'incluir duplicatas sem confirmação por hash',
        type: 'check' },
    ],
    go: 'Gerar plano',
  },
];

const CATS = ['duplicata', 'arquivo+extraido', 'dump/temp'];

function field(cmd, f) {
  const id = `f-${cmd.kind}-${f.k}`;
  if (f.type === 'check') {
    return `<label class="fcheck"><input type="checkbox" id="${id}"> ${esc(f.label)}</label>`;
  }
  if (f.type === 'cats') {
    return `<div class="f"><label>${esc(f.label)}</label>${CATS.map((c, i) =>
      `<label class="fcheck"><input type="checkbox" id="${id}-${i}" value="${esc(c)}" checked>
       ${esc(CAT[c] || c)}</label>`).join('')}</div>`;
  }
  let inner;
  if (f.type === 'mount') {
    inner = `<select id="${id}">${MOUNTS.map((m) =>
      `<option value="${esc(m.mount)}">${esc(m.mount)}${
        m.indexed ? ` — já indexado como ${esc(m.label)}` : ''}</option>`).join('')}</select>`;
  } else if (f.type === 'select') {
    inner = `<select id="${id}">${f.opts.map(([v, t]) =>
      `<option value="${esc(v)}">${esc(t)}</option>`).join('')}</select>`;
  } else if (f.type === 'number') {
    inner = `<input type="number" id="${id}"${f.def != null ? ` value="${f.def}"` : ''}${
      f.min != null ? ` min="${f.min}"` : ''}${f.max != null ? ` max="${f.max}"` : ''}>`;
  } else {
    inner = `<input type="text" id="${id}"${f.def ? ` value="${esc(f.def)}"` : ''}>`;
  }
  return `<div class="f"><label for="${id}">${esc(f.label)}</label>${inner}
    ${f.hint ? `<span class="hint">${esc(f.hint)}</span>` : ''}</div>`;
}

function collect(cmd) {
  const p = {};
  for (const f of cmd.fields) {
    const id = `f-${cmd.kind}-${f.k}`;
    if (f.type === 'check') { if (el(id).checked) p[f.k] = true; continue; }
    if (f.type === 'cats') {
      const on = CATS.map((c, i) => el(`${id}-${i}`))
        .filter((x) => x && x.checked).map((x) => x.value);
      if (on.length && on.length < CATS.length) p[f.k] = on;
      continue;
    }
    const v = (el(id).value || '').trim();
    if (!v) continue;
    if (f.type === 'list') p[f.k] = v.split(',').map((s) => s.trim()).filter(Boolean);
    else if (f.type === 'number') p[f.k] = Number(v);
    else p[f.k] = v;
  }
  return p;
}

async function startJob(cmd) {
  let params;
  try { params = collect(cmd); } catch (e) { toast(e.message, 'err'); return; }
  try {
    const job = await post('/api/job', { kind: cmd.kind, params });
    toast(`${cmd.title}: tarefa #${job.id} iniciada`, 'ok');
    JOB_SEL = job.id; JOB_SEEN = 0;
    await panelTarefas();
    pollJob();
  } catch (e) { toast(e.message, 'err'); }
}

async function pollJob() {
  clearTimeout(JOB_TIMER);
  if (JOB_SEL == null) return;
  try {
    const j = await api(`/api/job?id=${JOB_SEL}&since=${JOB_SEEN}`);
    const pre = el('joblog');
    if (pre && j.lines.length) {
      const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
      pre.textContent += j.lines.map((l) => l.line).join('\n') + '\n';
      JOB_SEEN = j.lines[j.lines.length - 1].n;
      if (atBottom) pre.scrollTop = pre.scrollHeight;
    }
    const badge = el('jobstate');
    if (badge) {
      badge.className = 'st ' + j.state;
      badge.textContent = j.state;
    }
    const sp = el('jobspin');
    if (sp) sp.hidden = j.state !== 'rodando';
    const cx = el('jobcancel');
    if (cx) cx.hidden = j.state !== 'rodando';
    if (j.state === 'rodando') {
      JOB_TIMER = setTimeout(pollJob, 1000);
    } else {
      loaded.resumo = false; loaded.liberar = false; loaded.duplicatas = false;
      loaded.arvores = false; loaded.pastas = false; loaded.frio = false;
      const list = await api('/api/jobs?limit=30');
      renderJobList(list.jobs);
    }
  } catch (_) { /* job gone: stop polling quietly */ }
}

function renderJobList(jobs) {
  const box = el('joblist');
  if (!box) return;
  if (!jobs.length) { box.innerHTML = '<div class="empty">nenhuma tarefa ainda</div>'; return; }
  box.innerHTML = jobs.map((j) => `
    <div class="job${j.id === JOB_SEL ? ' sel' : ''}" data-job="${j.id}">
      <div class="job-r1"><span class="job-k">#${j.id} ${esc(j.kind)}</span>
        <span class="st ${j.state}">${esc(j.state)}</span>
        <span class="job-t">${j.elapsed}s${j.rc != null && j.rc !== 0
          ? ` · código ${j.rc}` : ''}</span></div>
      <div class="job-argv">${esc(j.argv)}</div></div>`).join('');
  box.querySelectorAll('[data-job]').forEach((d) => {
    d.addEventListener('click', () => {
      JOB_SEL = Number(d.dataset.job); JOB_SEEN = 0;
      const pre = el('joblog');
      if (pre) pre.textContent = '';
      box.querySelectorAll('.job').forEach((x) => x.classList.toggle(
        'sel', Number(x.dataset.job) === JOB_SEL));
      pollJob();
    });
  });
}

async function panelTarefas() {
  const target = el('p-tarefas');
  if (!ACTIONS_ON) { target.innerHTML = offBanner('Tarefas'); return; }
  if (!MOUNTS.length) {
    try { MOUNTS = await api('/api/mounts'); } catch (_) { MOUNTS = []; }
  }
  const list = await api('/api/jobs?limit=30');
  let h = `<div class="phead"><p class="eyebrow">comandos</p><h2>Tarefas</h2>
    <p>Os comandos do diskmap, rodando em segundo plano com log ao vivo. Cada um roda
    como subprocesso, então o painel continua respondendo enquanto uma varredura leva
    horas — e dá para cancelar.</p></div>`;
  h += '<div class="cmds">' + CMDS.map((c) => `
    <div class="cmd-card">
      <h3>${esc(c.title)} <span class="cid">${esc(c.cid)}</span></h3>
      <p class="d">${esc(c.desc)}</p>
      <div class="fields">${c.fields.map((f) => field(c, f)).join('')}</div>
      <button class="btn primary go" data-start="${esc(c.kind)}">${esc(c.go)}</button>
    </div>`).join('') + '</div>';
  h += `<div class="logbar"><h3>Histórico de tarefas</h3></div>
    <div class="joblist" id="joblist"></div>`;
  h += `<div class="logbar"><h3>Log</h3>
      <span class="spin" id="jobspin" hidden></span>
      <span class="st" id="jobstate"></span>
      <button class="btn danger" id="jobcancel" hidden>Cancelar</button></div>
    <pre class="joblog" id="joblog">selecione uma tarefa acima, ou inicie uma.</pre>`;
  target.innerHTML = h;

  target.querySelectorAll('[data-start]').forEach((b) => {
    b.addEventListener('click', () => {
      const c = CMDS.find((x) => x.kind === b.dataset.start);
      if (c) startJob(c);
    });
  });
  el('jobcancel').addEventListener('click', async () => {
    try {
      await post('/api/job/cancel', { id: JOB_SEL });
      toast('cancelando tarefa #' + JOB_SEL);
    } catch (e) { toast(e.message, 'err'); }
  });
  renderJobList(list.jobs);
  if (JOB_SEL != null) { el('joblog').textContent = ''; JOB_SEEN = 0; pollJob(); }
}

/* ---------- imagens ---------- */
let IMG_OV = null;
let IMG_F = { lib: 'all', vol: 'all', format: 'all', sort: 'size', minpx: 0, q: '' };
let IMG_PAGE = 0;
/* Quantas imagens EXISTEM sob o filtro, processadas ou não. A galeria só mostra
   as que têm miniatura, então sem este número a sub-aba diz 80 e a grade diz 0 —
   os dois certos, e a tela sem explicar a diferença. */
let ACV_CONT = null;
let IMG_ROWS = [];        // what the grid currently shows, for the viewer
let IMG_MODE = 'galeria'; // galeria | repetidas

const thumb = (id) => `/thumb/${id}.jpg`;
const imgPath = (r) => (r.relpath ? `${r.label}/${r.relpath}/${r.name}` : `${r.label}/${r.name}`);
const imgAbs = (r) => (r.mount ? joinp(r.mount.replace(/\/$/, ''), r.relpath, r.name) : null);

function shot(r) {
  const px = (r.width && r.height) ? `${r.width}×${r.height}` : '';
  const abs = imgAbs(r);
  return `<figure class="sh${SEL.has(abs) ? ' sel' : ''}" data-img="${r.id}">
    ${selCheck(abs)}
    <div class="box"><img src="${thumb(r.id)}" alt="${esc(r.name)}" loading="lazy" decoding="async">
      ${px ? `<span class="px">${px}</span>` : ''}</div>
    <figcaption><div class="nm" title="${esc(r.name)}">${esc(r.name)}</div>
      <div class="mt"><span>${H(r.size)}</span><span>${esc(r.ext)}</span>
      <span>${esc(r.label)}</span></div></figcaption></figure>`;
}

function openViewer(r) {
  const v = el('viewer');
  const abs = imgAbs(r);
  v.innerHTML = `<img src="${thumb(r.id)}" alt="${esc(r.name)}">
    <div class="meta">
      <div class="pth">${esc(imgPath(r))}</div>
      <div class="row2">
        <span>${H(r.size)}</span>
        ${r.width ? `<span>${r.width}×${r.height} px</span>` : ''}
        <span>${esc(r.ext)}</span>
        <span>${esc((IMG_OV && IMG_OV.libs && IMG_OV.libs[r.lib]) || r.lib || '')}</span>
        ${r.mtime ? `<span>${new Date(r.mtime * 1000).toLocaleDateString('pt-BR')}</span>` : ''}
      </div>
      <div class="sheet-foot">
        <button class="btn" data-vclose>Fechar</button>
        ${ACTIONS_ON && abs ? `<button class="btn" data-vact="${esc(abs)}">Ações</button>` : ''}
      </div></div>`;
  v.hidden = false;
  v.querySelector('[data-vclose]').addEventListener('click', () => { v.hidden = true; });
  const a = v.querySelector('[data-vact]');
  if (a) a.addEventListener('click', () => { v.hidden = true; openActions(a.dataset.vact); });
}

async function renderGrid() {
  const p = new URLSearchParams({
    lib: IMG_F.lib, vol: IMG_F.vol, format: IMG_F.format, sort: IMG_F.sort,
    minpx: String(IMG_F.minpx), q: IMG_F.q, limit: '120', offset: String(IMG_PAGE * 120),
  });
  // A galeria é fonte própria: a etiqueta tem de ir junto, senão o cabeçalho
  // diz "Imagens 0" e a grade mostra o acervo inteiro logo abaixo.
  [...TAGS_ON].forEach((n) => p.append('tag', n));
  const d = await api('/api/images?' + p);
  IMG_ROWS = d.images;
  el('imgcount').textContent =
    `${N(d.total)} imagens · ${H(d.bytes)}` + (d.total > 120 ? ` · página ${IMG_PAGE + 1}` : '');
  const g = el('grid');
  const existem = ACV_CONT ? (ACV_CONT.imagem || 0) : 0;
  g.innerHTML = d.images.length ? d.images.map(shot).join('')
    : (TAGS_ON.size && existem)
      ? `<div class="empty">A galeria mostra só o que tem miniatura, e aqui não
         há nenhuma.<br><br>Sob ${TAGS_ON.size > 1 ? 'estas etiquetas' : 'esta etiqueta'}
         existem <b>${N(existem)}</b> imagens no índice${IMG_OV
           ? `, ${N(IMG_OV.total)} processada(s)${IMG_OV.no_thumb
             ? ` e ${N(IMG_OV.no_thumb)} sem miniatura` : ''}` : ''}.<br><br>
         <code>python3 -m diskmap images</code> ou a aba Tarefas.</div>`
      : '<div class="empty">nenhuma imagem com esse filtro</div>';
  el('imgprev').hidden = IMG_PAGE === 0;
  el('imgnext').hidden = (IMG_PAGE + 1) * 120 >= d.total;
  // A galeria é redesenhada a cada página e a cada filtro, então a seleção é
  // religada aqui e não uma vez só no painel.
  const painel = g.closest('.panel') || document;
  ligaSelecao(painel, () => renderGrid());
  sincronizaBarra(painel, () => renderGrid());
}

async function renderClusters() {
  const gp = new URLSearchParams({ limit: '25' });
  [...TAGS_ON].forEach((n) => gp.append('tag', n));
  const d = await api('/api/img-groups?' + gp);
  const box = el('clusters');
  el('imgcount').textContent =
    `${N(d.count)} grupos parecidos · ${H(d.wasted)} se mantiver só a maior de cada`;
  if (!d.groups.length && TAGS_ON.size) {
    box.innerHTML = `<div class="empty">nenhum grupo de imagens parecidas sob
      ${TAGS_ON.size > 1 ? 'estas etiquetas' : 'esta etiqueta'}. Os grupos saem
      do hash visual, que só existe para imagem já processada.</div>`;
  } else if (!d.groups.length) {
    box.innerHTML = `<div class="empty">nenhum grupo visual ainda.<br><br>
      Rode <code>diskmap images</code> (ou a aba Tarefas) para gerar os hashes.</div>`;
    return;
  }
  box.innerHTML = d.groups.map((g) => `
    <div class="gcluster">
      <div class="ch"><b>${g.n} imagens parecidas</b>
        <span class="badge">${H(g.bytes)} no total</span>
        <span class="w">${H(g.wasted)} redundante</span></div>
      <div class="gmembers">${g.members.map((m, i) => `
        <div class="gmem${i === 0 ? ' keep' : ''}">
          <div class="box"><img src="${thumb(m.id)}" alt="${esc(m.name)}" loading="lazy"></div>
          <div class="info">
            ${i === 0 ? '<span class="badge keep">maior — manter</span>'
              : `<span class="badge">distância ${m.dist}</span>`}
            <div style="margin-top:4px">${H(m.size)} · ${m.width || '?'}×${m.height || '?'}</div>
            <div class="p">${esc(imgPath(m))}</div>
          </div>
          ${ACTIONS_ON && imgAbs(m) ? `<div class="bar">
            <button class="actbtn" data-act-path="${esc(imgAbs(m))}">ações</button>
            ${i === 0 ? '' : `<button class="actbtn" data-qpath="${esc(imgAbs(m))}">quarentena</button>`}
          </div>` : ''}
        </div>`).join('')}</div></div>`).join('');
  box.querySelectorAll('[data-qpath]').forEach((b) => {
    b.addEventListener('click', () => {
      const a = ACTS.find((x) => x.id === 'quarantine');
      if (a) confirmAction(a, b.dataset.qpath);
      else toast('ações desligadas', 'err');
    });
  });
}

async function panelImagens(target) {
  // Refeito a cada chamada quando há etiqueta: o resumo é por filtro, e um
  // resumo cacheado do acervo inteiro contradiz a grade logo abaixo dele.
  const chaveTags = [...TAGS_ON].sort().join('|');
  if (!IMG_OV || IMG_OV._tags !== chaveTags) {
    const p = new URLSearchParams();
    [...TAGS_ON].forEach((n) => p.append('tag', n));
    try {
      IMG_OV = await api('/api/img-overview' + (String(p) ? '?' + p : ''));
      IMG_OV._tags = chaveTags;
    } catch (_) { IMG_OV = null; }
  }
  if (!IMG_OV || !IMG_OV.total) {
    target.innerHTML = `<div class="phead"><p class="eyebrow">visual</p><h2>Imagens</h2></div>
      <div class="offnote"><b>Nenhuma imagem processada ainda.</b> O índice sabe onde elas
      estão, mas miniatura, dimensão e hash visual exigem abrir cada arquivo.
      <code>python3 -m diskmap images</code>
      <p style="margin-top:10px;color:var(--fg-3);font-size:.82rem">Usa o <code>sips</code>,
      que já vem no macOS — sem dependência nova. Cerca de 26 ms por imagem com 8 processos.</p></div>`;
    return;
  }
  const o = IMG_OV;
  const fmts = ['all', 'png', 'jpg', 'psd', 'tga', 'tif', 'webp', 'bmp', 'hdr', 'exr', 'gif'];
  let h = `<div class="phead"><p class="eyebrow">visual</p><h2>Imagens</h2>
    <p>${N(o.total)} imagens processadas, ${H(o.bytes)}, ${N(o.with_hash)} com hash visual.
    Separadas por biblioteca: o que é textura, o que é screenshot, o que é seu.</p></div>`;

  h += '<div class="libs">' + [{ lib: 'all', label: 'Tudo', n: o.total, bytes: o.bytes }]
    .concat(o.libs).map((L) =>
      `<button class="lib" data-lib="${esc(L.lib)}"
        aria-pressed="${L.lib === IMG_F.lib}">${esc(L.label || L.lib)}
        <span class="c">${N(L.n)}</span></button>`).join('') + '</div>';

  h += `<div class="gridbar">
    <button class="btn ${IMG_MODE === 'galeria' ? 'primary' : ''}" data-mode="galeria">Galeria</button>
    <button class="btn ${IMG_MODE === 'repetidas' ? 'primary' : ''}" data-mode="repetidas">Repetidas</button>
    <select id="imgvol"><option value="all">todos os volumes</option>${
      (OV ? OV.volumes : []).map((v) =>
        `<option value="${esc(v.label)}">${esc(v.label)}</option>`).join('')}</select>
    <select id="imgfmt">${fmts.map((f) =>
      `<option value="${f}">${f === 'all' ? 'todos os formatos' : f}</option>`).join('')}</select>
    <select id="imgsort">
      <option value="size">maiores primeiro</option>
      <option value="px">mais pixels</option>
      <option value="new">mais recentes</option>
      <option value="old">mais antigas</option>
      <option value="name">nome</option>
    </select>
    <select id="imgpx">
      <option value="0">qualquer dimensão</option>
      <option value="1024">1024px ou mais</option>
      <option value="2048">2048px ou mais</option>
      <option value="4096">4096px ou mais</option>
    </select>
    <span class="count" id="imgcount"></span></div>`;

  h += `<div class="row" style="margin:10px 0"${IMG_MODE !== 'galeria' ? ' hidden' : ''}>
      <button class="btn" data-seltodos>marcar/desmarcar os desta página</button></div>
    <div id="grid" class="grid"${IMG_MODE !== 'galeria' ? ' hidden' : ''}></div>
    <div id="clusters"${IMG_MODE !== 'repetidas' ? ' hidden' : ''}></div>
    <div class="gridbar" style="margin-top:16px">
      <button class="btn" id="imgprev" hidden>← anteriores</button>
      <button class="btn" id="imgnext" hidden>próximas →</button></div>`;

  if (o.no_thumb) {
    h += `<p class="note">${N(o.no_thumb)} imagens ficaram sem miniatura — formato que o
      <code>sips</code> não decodifica (dds) ou arquivo grande demais para o tempo limite.
      Elas continuam no índice com caminho e tamanho.</p>`;
  }
  target.innerHTML = h;

  target.querySelectorAll('[data-lib]').forEach((b) => {
    b.addEventListener('click', () => {
      IMG_F.lib = b.dataset.lib; IMG_PAGE = 0;
      target.querySelectorAll('[data-lib]').forEach((x) =>
        x.setAttribute('aria-pressed', String(x.dataset.lib === IMG_F.lib)));
      if (IMG_MODE === 'galeria') renderGrid();
    });
  });
  target.querySelectorAll('[data-mode]').forEach((b) => {
    b.addEventListener('click', () => { IMG_MODE = b.dataset.mode; panelImagens(target); });
  });
  const bind = (id, key, num) => {
    const s = el(id);
    if (!s) return;
    s.value = String(IMG_F[key]);
    s.addEventListener('change', () => {
      IMG_F[key] = num ? Number(s.value) : s.value;
      IMG_PAGE = 0;
      if (IMG_MODE === 'galeria') renderGrid();
    });
  };
  bind('imgvol', 'vol'); bind('imgfmt', 'format');
  bind('imgsort', 'sort'); bind('imgpx', 'minpx', true);
  el('imgprev').addEventListener('click', () => { IMG_PAGE--; renderGrid(); });
  el('imgnext').addEventListener('click', () => { IMG_PAGE++; renderGrid(); });
  target.addEventListener('click', (e) => {
    const f = e.target.closest('[data-img]');
    if (f) {
      const r = IMG_ROWS.find((x) => String(x.id) === f.dataset.img);
      if (r) openViewer(r);
    }
  });

  if (IMG_MODE === 'galeria') await renderGrid();
  else await renderClusters();
}

/* ---------- panels ---------- */
let OV = null;

async function panelResumo() {
  const o = OV;
  const t = o.totals;
  const pct = t.capacity ? (100 * (t.capacity - t.free) / t.capacity) : 0;
  const tiles = [
    ['Capacidade', H(t.capacity), `${o.volumes.length} volumes`, ''],
    ['Livre', H(t.free), `${pct.toFixed(1)}% em uso`, t.free / t.capacity < 0.05 ? 'crit' : ''],
    ['Liberável', H(o.recoverable.total), `${N(o.recoverable.files)} arquivos redundantes`, 'ok'],
    ['Arquivos', N(t.files), H(t.bytes) + ' indexados', 'acc'],
  ];
  let h = `<div class="phead"><p class="eyebrow">situação</p><h2>Visão do todo</h2>
    <p>Índice local de ${o.volumes.length} volumes. Os discos desconectados continuam
    pesquisáveis — o índice não precisa deles montados.</p></div>
    <div class="tiles">${tiles.map(([k, v, s, c]) =>
      `<div class="tile"><div class="k">${k}</div><div class="v ${c}">${v}</div>
       <div class="s">${esc(s)}</div></div>`).join('')}</div>`;

  h += '<div class="vols">';
  for (const v of o.volumes) {
    const cap = v.capacity || v.bytes || 1;
    const used = v.capacity ? v.capacity - v.free : v.bytes;
    const p = 100 * used / cap;
    const waste = o.recoverable.pervol[v.label] || 0;
    const wp = Math.min(100 * waste / cap, p);
    const up = Math.max(p - wp, 0);
    const online = v.mount ? '' : '<span class="pill off">não montado</span>';
    const pill = p >= 98 ? '<span class="pill crit">lotado</span>'
      : p >= 95 ? '<span class="pill warn">crítico</span>' : '';
    h += `<div class="vol">
      <div class="vol-r1"><span class="vol-name">${esc(v.label)}</span>${pill}${online}
      <span class="vol-cap">${H(used)} / ${H(cap)} &middot; ${p.toFixed(0)}% &middot; ${N(v.files)} arq</span></div>
      <div class="meter" data-tip="${esc(v.label)}: ${H(used)} usados, ${H(v.free)} livres, ${H(waste)} liberáveis">
        <i style="width:${up.toFixed(2)}%;background:var(--accent)"></i>
        <i style="width:${wp.toFixed(2)}%;background:var(--crit)"></i></div>
      <div class="legend">
        <span><span class="sw" style="background:var(--accent)"></span>em uso ${H(used - waste)}</span>
        <span><span class="sw" style="background:var(--crit)"></span>liberável ${H(waste)}</span>
        <span><span class="sw" style="background:var(--surface-2);border:1px solid var(--line-2)"></span>livre ${H(v.free)}</span>
      </div></div>`;
  }
  h += '</div>';

  const cls = (o.stats.class || []).filter((c) => c.bytes > 2e9).map((c) => ({
    label: CLS[c.key] || c.key, value: c.bytes,
    tip: `${CLS[c.key] || c.key}: ${H(c.bytes)} em ${N(c.files)} arquivos`, key: c.key,
  }));
  h += `<h3 style="margin:28px 0 12px">O que são esses bytes</h3>${bars(cls, {
    color: (i) => i.key === 'junk' ? 'var(--crit)'
      : (i.key === 'gamedata' || i.key === 'archive') ? 'var(--orange)' : 'var(--accent)',
  })}`;

  const order = ['<6m', '6m-1a', '1-2a', '2-3a', '3-5a', '>5a', 'desconhecido'];
  const am = {}; (o.stats.age || []).forEach((a) => { am[a.key] = a; });
  const age = order.filter((k) => am[k]).map((k) => ({ label: AGE[k], value: am[k].bytes, key: k }));
  h += `<h3 style="margin:28px 0 12px">Idade do dado</h3>${bars(age, {
    color: (i) => ['<6m', '6m-1a'].includes(i.key) ? 'var(--accent)'
      : ['1-2a', '2-3a'].includes(i.key) ? 'var(--warn)' : 'var(--fg-3)',
  })}`;

  if (o.blobs.length) {
    h += `<h3 style="margin:28px 0 12px">Pastas agregadas (não percorridas)</h3>${
      bars(o.blobs.map((b) => ({ label: b.kind, value: b.bytes,
        tip: `${b.kind}: ${H(b.bytes)} em ${N(b.files)} arquivos` })))}
      <p class="note"><b>node_modules</b>, <b>.git</b> e afins entram como um número só.
      Ser reconstruível não as tira da conta — veja a aba Liberar.</p>`;
  }
  h += `<h3 style="margin:28px 0 12px">Saúde dos discos</h3>
    <div id="health" class="empty">lendo SMART…</div>`;
  el('p-resumo').innerHTML = h;
  loadHealth();
}

/* ---------- SMART ---------- */
const HL = {
  crit: ['crit', 'Crítico'], warn: ['warn', 'Atenção'],
  ok: ['ok', 'Sem atributo preocupante'], desconhecido: ['', 'Desconhecido'],
};

async function loadHealth() {
  const box = el('health');
  if (!box) return;
  let r;
  try {
    r = await api('/api/health');
  } catch (e) {
    box.textContent = 'não deu para ler: ' + e.message;
    return;
  }
  box.className = '';
  const media = (d) => (d.solid_state === true ? 'SSD'
    : d.solid_state === false ? 'disco mecânico' : 'mídia desconhecida');

  box.innerHTML = r.disks.map((d) => {
    const [cls, title] = HL[d.level] || ['', d.level];
    const sigs = d.signals.length
      ? `<div class="tw"><table><tbody>${d.signals.map((s) => `<tr>
          <td class="path"><span class="hi">${esc(s.key.replace(/_/g, ' '))}</span></td>
          <td class="num">${esc(s.value)}</td>
          <td class="s" style="color:var(--fg-3)">${esc(s.help)}</td></tr>`).join('')}
        </tbody></table></div>` : '';
    const vols = d.volumes.length
      ? `<p class="note">${esc(d.volumes.join(', '))} · ${H(d.indexado)} indexados,
         <b>${H(d.so_aqui)}</b> sem cópia conhecida em outro volume</p>`
      : '<p class="note">nenhum volume indexado neste disco</p>';
    return `<div class="box" style="margin-bottom:14px">
      <div class="vol-r1"><span class="vol-name">${esc(d.model)}</span>
        <span class="badge ${cls}">${esc(title)}</span></div>
      <p class="s" style="color:var(--fg-2)">${esc(d.dev)} · ${media(d)} ·
        ${esc(d.protocol)} · ${H(d.size)}${d.removable ? ' · externo' : ''}</p>
      <div class="warnbox ${cls}">${esc(d.why)}</div>
      ${vols}${sigs}</div>`;
  }).join('');

  if (!r.smartctl) {
    box.innerHTML += `<div class="offnote"><b>O macOS só entrega um booleano.</b>
      Ele vira <code>Failing</code> quando o disco já passou do limite do fabricante —
      tarde demais para ser aviso. O estudo do Google com mais de cem mil discos achou
      que mais da metade dos que falharam nunca acusou nada nos atributos mais fortes.
      <p style="margin-top:10px">Para ver setor realocado, setor pendente e desgaste de
      flash: <code>${esc(r.hint)}</code></p></div>`;
  }
}

async function panelLiberar() {
  const o = OV;
  const by = Object.entries(o.recoverable.by).map(([k, v]) => ({ label: CAT[k] || k, value: v }));
  by.sort((a, b) => b.value - a.value);
  const pv = Object.entries(o.recoverable.pervol).map(([k, v]) => ({ label: k, value: v }));
  pv.sort((a, b) => b.value - a.value);
  const ver = o.dup.verified || {};
  const confirmed = (ver.quick || 0) + (ver.full || 0);

  let h = `<div class="phead"><p class="eyebrow">ganho imediato</p>
    <h2>${H(o.recoverable.total)} sem decidir nada</h2>
    <p>Categorias em que a redundância é mecânica. Os números já estão desduplicados
    entre si: cada arquivo conta uma vez só.</p></div>${bars(by)}`;

  h += `<p class="note"><b>Estado da verificação:</b> ${N(confirmed)} grupos confirmados por hash,
    ${N(ver.no || 0)} ainda não verificados, ${N(ver.mismatch || 0)} divergentes
    (mesmo nome e tamanho, conteúdo diferente — esses nunca entram no plano).
    Rode <code>diskmap verify</code> antes de <code>diskmap plan</code>.</p>`;

  h += `<h3 style="margin:28px 0 12px">Ganho por volume</h3>${bars(pv)}`;
  h += `<p class="note">Para gerar o plano de quarentena:
    <code>diskmap plan --out ./plano</code>. Ele <b>move</b> para uma pasta de
    quarentena e escreve um <code>undo.sh</code> — nada é apagado.</p>`;

  h += `<h3 style="margin:34px 0 4px">Build e dependência</h3>
    <div id="blob-head"></div><div id="blob-body" class="empty">carregando…</div>`;
  el('p-liberar').innerHTML = h;
  renderBlobHead();
  loadBlobs();
}

/* ---------- aggregated dirs: node_modules, caches, .git ---------- */
let BLOB_YEARS = '1';

function renderBlobHead() {
  const br = (OV.blob_reclaim || {}).by_verdict || {};
  const order = ['cache', 'manifesto', 'sem-prova', 'vcs'];
  const cards = order.filter((k) => br[k]).map((k) => {
    const d = br[k];
    const [name, why] = VERDICT[k];
    const cls = (k === 'cache' || k === 'manifesto') ? 'ok' : 'off';
    return `<div class="tile"><div class="k">${esc(name)}</div>
      <div class="v ${cls}">${H(d.bytes)}</div>
      <div class="s">${N(d.dirs)} pastas · ${esc(why)}</div></div>`;
  }).join('');

  const opts = [['0', 'qualquer idade'], ['1', 'parado há 1 ano+'],
                ['2', '2 anos+'], ['3', '3 anos+']]
    .map(([v, l]) => `<option value="${v}"${v === BLOB_YEARS ? ' selected' : ''}>${l}</option>`)
    .join('');

  el('blob-head').innerHTML = `
    <p>Pastas que o scan agrega numa linha só. Nunca foram oferecidas porque são
    reconstruíveis — que é justamente o motivo para oferecê-las. O que decide não é o
    nome da pasta: é existir prova de que algo a recria.</p>
    <div class="tiles" style="margin:14px 0">${cards}</div>
    <p class="note"><b>O <code>.git</code> nunca entra.</b> Ele não é dependência
    baixada, é o histórico. Some com ele e o que não estiver num remoto acabou.</p>
    <div class="gridbar" style="margin-top:16px">
      <select id="blob-years">${opts}</select>
      <span class="count" id="blob-count"></span></div>`;

  el('blob-years').onchange = (e) => { BLOB_YEARS = e.target.value; loadBlobs(); };
}

async function loadBlobs() {
  const body = el('blob-body');
  body.className = 'empty';
  body.textContent = 'carregando…';
  const y = BLOB_YEARS === '0' ? '' : `&min_years=${encodeURIComponent(BLOB_YEARS)}`;
  let d;
  try {
    d = await api(`/api/blobs?limit=120${y}`);
  } catch (err) {
    body.textContent = 'falhou: ' + err.message;
    return;
  }
  const rows = (d.rows || []).filter((r) => r.offerable);
  const tot = rows.reduce((a, r) => a + r.bytes, 0);
  body.className = '';
  const cnt = el('blob-count');
  if (cnt) cnt.textContent = rows.length ? `${H(tot)} em ${N(rows.length)} pastas` : '';
  if (!rows.length) {
    body.innerHTML = `<div class="empty">nada reconstruível nesse corte de idade</div>`;
    return;
  }
  const when = (r) => (r.years == null ? '—'
    : r.years < 1 ? `${Math.round(r.years * 365)} d` : `${r.years} a`);
  body.innerHTML = `<p class="note">A coluna <b>parado</b> é o último toque nos arquivos
      <i>do projeto</i>, não na pasta: um <code>node_modules</code> de projeto vivo não é
      desperdício, é o seu próximo build.</p>`
    + table(['Pasta', 'Tamanho', 'Arquivos', 'Parado', 'Prova', ''],
      rows.map((r) => [
        tdp(`${r.label}/${r.relpath}`), tdn(H(r.bytes)), tdn(N(r.files)),
        tdn(when(r)), tdn(esc(r.proof || '—')), actCell(r.path)]));
}

/* The pair rows of the trees tab, so an expanded row can find its pair by
   index, plus a cache: reopening a row must not re-query the index. */
let OVL = [];
const OVL_CACHE = {};

/* The arrow lives next to the file count because the count is the claim and the
   files are the evidence -- clicking the number is the natural way to ask "which
   ones?". aria-expanded carries the state, so the CSS rotation and the screen
   reader read from the same place. */
const twigCell = (n, i) => `<td class="num twigcell">
  <button class="twig" data-twig="${i}" aria-expanded="false"
    title="ver os arquivos iguais">${N(n)}<span class="arrow" aria-hidden="true">▸</span>
  </button></td>`;

function twigBody(d, i) {
  if (d.erro) return `<div class="empty">${esc(d.erro)}</div>`;
  if (!d.rows || !d.rows.length) {
    return `<div class="empty">nenhum arquivo acima do piso de impressão digital</div>`;
  }
  const mes = (m) => (m ? new Date(m * 1000).toISOString().slice(0, 7) : '—');
  const mais = (n) => (n > 0
    ? ` <span class="tag">+${N(n)} na mesma árvore</span>` : '');
  const corte = d.shown < d.total
    ? `<p class="note">Mostrando os ${N(d.shown)} maiores de ${N(d.total)}.</p>` : '';
  const cols = ['Arquivo', 'Tamanho', 'Prova', `em ${esc(d.a_label)}`, 'quando',
    `em ${esc(d.b_label)}`, 'quando'];
  if (ACTIONS_ON) cols.push('');
  // Dito antes da tabela, não só na linha: com 400 linhas ninguém descobre
  // lendo. O índice é uma fotografia e esta tela oferece botões que apagam.
  const fora = d.nao_ok
    ? `<div class="warnbox warn"><b>${N(d.nao_ok)} de ${N(d.shown * 2)} cópias
       desta lista não estão onde o índice viu.</b> Podem ter ido para a
       quarentena pelo painel, para a Lixeira, ou ter sido apagadas por fora —
       cada linha diz qual. Para essas, descartar e sincronizar estão
       desligados. Rode <b>Indexar um volume</b> e <b>Recalcular achados</b>
       para a lista voltar a bater com o disco.</div>`
    : '';
  return `<div class="twigbox">${fora}
    <p class="note">${N(d.total)} arquivos com o mesmo nome e o mesmo tamanho nas
      duas árvores, somando <b>${H(d.bytes)}</b>. A coluna <b>prova</b> é só o que o
      <code>verify</code> já conferiu por hash — sem ela, nome e tamanho é
      candidato, não prova.</p>${corte}
    ${ACTIONS_ON ? `<p class="note"><b>descartar</b> tira uma das cópias do
      caminho e libera o espaço dela; <b>sincronizar</b> faz um lado virar cópia
      do outro e não libera nada — serve para quando os dois divergiram. As duas
      mandam o arquivo que sai para a quarentena, e o Histórico desfaz.</p>` : ''}
    ${table(cols, d.rows.map((x, j) => {
      const linha = [
        `<td class="path"><span class="hi">${esc(x.name)}</span></td>`,
        tdn(H(x.size)), tdn(VER_TAG[x.verdict] || VER_TAG.no),
        `<td class="path">${esc(x.a_path)}${mais(x.a_extra)}${estTag(x.a_estado)}</td>`,
        tdn(mes(x.mtime_a)),
        `<td class="path">${esc(x.b_path)}${mais(x.b_extra)}${estTag(x.b_estado)}</td>`,
        tdn(mes(x.mtime_b))];
      if (ACTIONS_ON) {
        // Dois verbos porque são duas operações diferentes, e confundi-las é
        // caro: sincronizar iguala os dois lados e não libera nada; descartar
        // tira um do caminho e libera o tamanho dele.
        // Nenhum dos dois faz sentido quando um lado já saiu do lugar: não há
        // par a resolver, e o botão só levaria a um erro lá na frente.
        const falta = (x.a_estado && x.a_estado !== 'ok')
          || (x.b_estado && x.b_estado !== 'ok');
        const porque = x.a_estado === 'desmontado' || x.b_estado === 'desmontado'
          ? 'o volume de um dos lados não está conectado'
          : 'um dos lados já saiu do lugar';
        linha.push(falta
          ? `<td class="act" title="${esc(porque)}"><span class="tag">—</span></td>`
          : `<td class="act">
            <button class="actbtn" data-descartar="${i}:${j}">descartar</button>
            <button class="actbtn" data-resolve="${i}:${j}">sincronizar</button></td>`);
      }
      return linha;
    }))}
  </div>`;
}

/* Resolver o clone: fazer um lado virar cópia do outro.
   É a única escrita do diskmap que substitui arquivo existente, então a folha
   tem de dizer exatamente o que vai acontecer ANTES, e a prova de hash decide
   o tom: prova de igualdade vira confirmação simples, prova de diferença vira
   alarme, e ausência de prova vira um convite a conferir em vez de adivinhar. */
let RESOLVE = null;

function resolveSheet() {
  const { d, x, dir } = RESOLVE;
  const srcLab = dir === 'a2b' ? d.a_label : d.b_label;
  const dstLab = dir === 'a2b' ? d.b_label : d.a_label;
  const srcRel = dir === 'a2b' ? x.a_path : x.b_path;
  const dstRel = dir === 'a2b' ? x.b_path : x.a_path;
  const src = absRel(srcLab, srcRel);
  const dst = absRel(dstLab, dstRel);
  const v = x.verdict;
  const caixa = v === 'full'
    ? `<div class="warnbox ok"><b>sha256 idêntico nos dois.</b> Os arquivos já são o
       mesmo conteúdo, então substituir não muda byte nenhum — serve só para
       deixar os dois com a mesma data. Confirme se é isso que você quer.</div>`
    : v === 'mismatch'
      ? `<div class="warnbox crit"><b>Os dois NÃO são o mesmo arquivo.</b> Mesmo
         nome e mesmo tamanho, sha256 diferente — são versões distintas. Substituir
         destrói uma delas (ela vai para a quarentena, mas some do lugar).
         <b>Só siga se você sabe qual das duas quer manter.</b></div>`
      : `<div class="warnbox warn"><b>Ninguém comparou o conteúdo destes dois.</b>
         Mesmo nome e mesmo tamanho é candidato, não prova — arquivos de formato
         fixo (modelos, imagens de disco, contêineres) batem em nome e tamanho
         sem terem o mesmo conteúdo. Confira antes de substituir.</div>`;
  const semMontar = !src || !dst;
  sheet().innerHTML = `<div class="sheet-in" role="dialog" aria-modal="true"
      aria-label="Resolver clone">
    <h3>Resolver clone · ${esc(x.name)}</h3>
    ${caixa}
    <div class="dirpick">
      <button class="dirbtn${dir === 'a2b' ? ' on' : ''}" data-dir="a2b">
        <span class="an">${esc(d.a_label)} → ${esc(d.b_label)}</span>
        <span class="ah">substitui o de ${esc(d.b_label)}</span></button>
      <button class="dirbtn${dir === 'b2a' ? ' on' : ''}" data-dir="b2a">
        <span class="an">${esc(d.b_label)} → ${esc(d.a_label)}</span>
        <span class="ah">substitui o de ${esc(d.a_label)}</span></button>
    </div>
    <div class="sheet-path"><b>mantém</b> ${esc(src || (srcLab + '/' + srcRel))}</div>
    <div class="sheet-path"><b>substitui</b> ${esc(dst || (dstLab + '/' + dstRel))}</div>
    ${semMontar ? `<div class="warnbox crit">Um dos volumes não está montado:
      não dá para agir sobre ele.</div>` : `<div class="warnbox">O arquivo
      substituído vai para <code>.diskmap-quarantine</code> no volume dele, e a
      aba <b>Histórico</b> desfaz. A cópia nova é relida do disco e só entra no
      lugar se o sha256 bater com a origem.</div>`}
    <div class="sheet-foot">
      <button class="btn" data-close>Cancelar</button>
      ${v === 'full' || v === 'mismatch' ? '' :
        '<button class="btn" data-check>Conferir agora (sha256)</button>'}
      <button class="btn danger" data-go ${semMontar ? 'disabled' : ''}>Substituir</button>
    </div></div>`;
  sheet().hidden = false;
  sheet().querySelector('[data-close]').addEventListener('click', closeSheet);
  sheet().querySelectorAll('[data-dir]').forEach((b) => b.addEventListener('click', () => {
    RESOLVE.dir = b.dataset.dir;
    resolveSheet();
  }));
  const chk = sheet().querySelector('[data-check]');
  if (chk) chk.addEventListener('click', () => conferirPar(src, dst));
  const go = sheet().querySelector('[data-go]');
  if (go && !semMontar) go.addEventListener('click', () => runResolve(src, dst));
}

/* Descartar: a operação que de fato tira a redundância. O portão de prova é o
   mesmo da sincronização, mas as consequências se invertem — aqui a prova de
   IGUALDADE é que libera a ação, porque descartar um de dois arquivos idênticos
   não perde conteúdo, e descartar um de dois DIFERENTES perde. */
function descarteSheet() {
  const { d, x, lado } = RESOLVE;
  const lab = lado === 'a' ? d.a_label : d.b_label;
  const outro = lado === 'a' ? d.b_label : d.a_label;
  const rel = lado === 'a' ? x.a_path : x.b_path;
  const relOutro = lado === 'a' ? x.b_path : x.a_path;
  const alvo = absRel(lab, rel);
  const fica = absRel(outro, relOutro);
  const v = x.verdict;
  const caixa = v === 'full'
    ? `<div class="warnbox ok"><b>sha256 idêntico nos dois.</b> Descartar este
       libera <b>${H(x.size)}</b> e não perde conteúdo nenhum: a cópia que fica
       é byte a byte a mesma.</div>`
    : v === 'mismatch'
      ? `<div class="warnbox crit"><b>Os dois NÃO são o mesmo arquivo.</b> sha256
         diferente — são versões distintas, e o conteúdo deste não existe do
         outro lado. Descartar aqui <b>perde</b> essa versão (ela fica na
         quarentena, mas sai do lugar).</div>`
      : `<div class="warnbox warn"><b>Ninguém comparou o conteúdo destes dois.</b>
         Descartar sem prova é apostar que são iguais. Confira primeiro.</div>`;
  const semMontar = !alvo || !fica;
  sheet().innerHTML = `<div class="sheet-in" role="dialog" aria-modal="true"
      aria-label="Descartar cópia">
    <h3>Descartar cópia · ${esc(x.name)}</h3>
    ${caixa}
    <div class="dirpick">
      <button class="dirbtn${lado === 'a' ? ' on' : ''}" data-lado="a">
        <span class="an">descartar o de ${esc(d.a_label)}</span>
        <span class="ah">fica o de ${esc(d.b_label)}</span></button>
      <button class="dirbtn${lado === 'b' ? ' on' : ''}" data-lado="b">
        <span class="an">descartar o de ${esc(d.b_label)}</span>
        <span class="ah">fica o de ${esc(d.a_label)}</span></button>
    </div>
    <div class="sheet-path"><b>sai</b> ${esc(alvo || (lab + '/' + rel))}</div>
    <div class="sheet-path"><b>fica</b> ${esc(fica || (outro + '/' + relOutro))}</div>
    ${semMontar ? `<div class="warnbox crit">Um dos volumes não está montado.</div>`
      : `<div class="warnbox">Vai para <code>.diskmap-quarantine</code> no mesmo
         volume, com o caminho original preservado dentro. O Histórico desfaz, e
         nada é apagado: o espaço só volta quando você esvaziar a quarentena à
         mão.</div>`}
    <div class="sheet-foot">
      <button class="btn" data-close>Cancelar</button>
      ${v === 'full' || v === 'mismatch' ? '' :
        '<button class="btn" data-check>Conferir agora (sha256)</button>'}
      <button class="btn danger" data-go ${semMontar ? 'disabled' : ''}>Descartar</button>
    </div></div>`;
  sheet().hidden = false;
  sheet().querySelector('[data-close]').addEventListener('click', closeSheet);
  sheet().querySelectorAll('[data-lado]').forEach((b) =>
    b.addEventListener('click', () => { RESOLVE.lado = b.dataset.lado; descarteSheet(); }));
  const chk = sheet().querySelector('[data-check]');
  if (chk) chk.addEventListener('click', () => conferirPar(alvo, fica, descarteSheet));
  const go = sheet().querySelector('[data-go]');
  if (go && !semMontar) go.addEventListener('click', () => runDescarte(alvo, fica));
}

async function runDescarte(alvo, fica) {
  const nome = RESOLVE.x.name;
  const tam = RESOLVE.x.size;
  sheet().innerHTML = `<div class="sheet-in"><h3>Descartar · ${esc(nome)}</h3>
    <pre class="out">movendo para a quarentena…</pre></div>`;
  const foot = '<div class="sheet-foot"><button class="btn" data-close>Fechar</button></div>';
  try {
    // `keep` não é decoração: o servidor confere no disco que essa cópia está
    // lá antes de mover a outra, para o último clique nunca deixar zero.
    const res = await post('/api/discard-clone',
      { path: alvo, keep: fica, expect: (RESOLVE.shas || {})[alvo] || null });
    sheet().innerHTML = `<div class="sheet-in"><h3>Descartar · ${esc(nome)}</h3>
      <div class="sheet-path">${esc(res.path)}</div>
      <pre class="out">${esc(res.output)}</pre>
      <div class="warnbox ok">A cópia que fica foi conferida no disco antes de
        mover: <code>${esc(res.kept)}</code></div>${reindexNota()}${foot}</div>`;
    toast(`descartado · ${H(tam)} na quarentena`, 'ok');
    loaded.historico = false; loaded.arvores = false; loaded.resumo = false;
  } catch (e) {
    sheet().innerHTML = `<div class="sheet-in"><h3>Descartar · ${esc(nome)}</h3>
      <div class="warnbox crit">${esc(e.message)}</div>${foot}</div>`;
    toast(e.message, 'err');
  }
  sheet().querySelector('[data-close]').addEventListener('click', closeSheet);
}

/* Os achados são derivados: `dup_groups` e `tree_overlap` são reconstruídos pelo
   analyze, que lê a tabela `files`, que só o scan atualiza. Enquanto os dois não
   rodarem, o painel continua mostrando o par que você acabou de resolver — e um
   painel que mente sobre o próprio trabalho é pior que um painel desatualizado
   em silêncio. Então ele diz. */
function reindexNota() {
  return `<div class="warnbox"><b>O painel ainda vai mostrar este par.</b> Os
    achados são recalculados, não observados: rode <b>Indexar um volume</b> nos
    dois volumes e depois <b>Recalcular achados</b>, na aba Tarefas, para a lista
    refletir o que você acabou de fazer.</div>`;
}

async function conferirPar(src, dst, redesenha) {
  const foot = sheet().querySelector('.sheet-foot');
  foot.innerHTML = '<pre class="out">lendo os dois arquivos do disco…</pre>';
  try {
    const r = await post('/api/compare-hash', { a: src, b: dst });
    RESOLVE.x.verdict = r.equal ? 'full' : 'mismatch';
    // Guardado por caminho absoluto, não por lado: o usuário pode inverter a
    // direção depois de conferir, e aí 'a' e 'b' trocam de papel.
    RESOLVE.shas = Object.assign(RESOLVE.shas || {},
      { [r.a.path]: r.a.sha, [r.b.path]: r.b.sha });
    toast(r.equal ? 'sha256 igual: são o mesmo arquivo'
      : 'sha256 diferente: NÃO são o mesmo arquivo', r.equal ? 'ok' : 'err');
    loaded.arvores = false;        // a coluna prova mudou
    (redesenha || resolveSheet)();
  } catch (e) {
    foot.innerHTML = `<div class="warnbox crit">${esc(e.message)}</div>
      <button class="btn" data-close>Fechar</button>`;
    foot.querySelector('[data-close]').addEventListener('click', closeSheet);
  }
}

async function runResolve(src, dst) {
  const nome = RESOLVE.x.name;
  sheet().innerHTML = `<div class="sheet-in"><h3>Resolver clone · ${esc(nome)}</h3>
    <pre class="out">copiando e conferindo…</pre></div>`;
  const foot = '<div class="sheet-foot"><button class="btn" data-close>Fechar</button></div>';
  try {
    // `expect` só vai quando foi realmente medido: ele faz o servidor recusar
    // se a origem mudou desde a leitura. Mandar um palpite ali seria pior que
    // não mandar nada.
    const res = await post('/api/resolve-clone',
      { src, dst, expect: (RESOLVE.shas || {})[src] || null });
    sheet().innerHTML = `<div class="sheet-in"><h3>Resolver clone · ${esc(nome)}</h3>
      <div class="sheet-path">${esc(res.path)}</div>
      <pre class="out">${esc(res.output)}</pre>${reindexNota()}${foot}</div>`;
    toast('cópias sincronizadas', 'ok');
    loaded.historico = false; loaded.arvores = false; loaded.resumo = false;
  } catch (e) {
    sheet().innerHTML = `<div class="sheet-in"><h3>Resolver clone · ${esc(nome)}</h3>
      <div class="warnbox crit">${esc(e.message)}</div>${foot}</div>`;
    toast(e.message, 'err');
  }
  sheet().querySelector('[data-close]').addEventListener('click', closeSheet);
}

/* No per-file action button here on purpose. Both sides of a pair are
   candidates and the panel cannot say which one should survive -- offering a
   destructive action next to an unproven pair is exactly the mistake the
   launcher protection and the sha256 verdict exist to prevent. Inspect here,
   act from Liberar or Duplicatas, where a verdict exists. */
async function toggleTwig(b) {
  const tr = b.closest('tr');
  if (b.getAttribute('aria-expanded') === 'true') {
    b.setAttribute('aria-expanded', 'false');
    const d = tr.nextElementSibling;
    if (d && d.classList.contains('det')) d.remove();
    return;
  }
  b.setAttribute('aria-expanded', 'true');
  const i = Number(b.dataset.twig);
  const det = document.createElement('tr');
  det.className = 'det';
  det.innerHTML = `<td colspan="${tr.children.length}">
    <div class="empty">consultando o índice…</div></td>`;
  tr.after(det);
  try {
    if (!OVL_CACHE[i]) {
      const r = OVL[i];
      OVL_CACHE[i] = await api(`/api/overlap-files?a=${encodeURIComponent(r.a)}`
        + `&b=${encodeURIComponent(r.b)}&limit=400`);
    }
    det.querySelector('td').innerHTML = twigBody(OVL_CACHE[i], i);
    det.querySelectorAll('[data-resolve]').forEach((bt) =>
      bt.addEventListener('click', () => openResolve(bt.dataset.resolve, 'sync')));
    det.querySelectorAll('[data-descartar]').forEach((bt) =>
      bt.addEventListener('click', () => openResolve(bt.dataset.descartar, 'descartar')));
  } catch (e) {
    det.querySelector('td').innerHTML =
      `<div class="empty">não deu para consultar: ${esc(e.message || e)}</div>`;
  }
}

function openResolve(chave, modo) {
  const [i, j] = chave.split(':').map(Number);
  const d = OVL_CACHE[i];
  if (!d || !d.rows[j]) return;
  RESOLVE = { d, x: d.rows[j], dir: 'a2b', lado: 'b' };
  (modo === 'descartar' ? descarteSheet : resolveSheet)();
}

async function panelArvores() {
  const rows = await api('/api/overlap?limit=40');
  OVL = rows;
  let h = `<div class="phead"><p class="eyebrow">causa raiz</p><h2>Árvores clonadas</h2>
    <p>Pares de diretórios que guardam o mesmo conteúdo com nomes diferentes.
    A coluna <em>igual</em> é quanto os dois têm de arquivo idêntico; <em>sobrep.</em>
    é essa fatia sobre a menor das duas árvores. Nenhum buscador de duplicata
    reporta isso como uma coisa só.</p>
    <p class="note">Clique no número de <b>arq.</b> para ver quais arquivos sustentam
    o par.</p></div>`;
  h += table(['Árvore A', 'Árvore B', 'Igual', 'Sobrep.', 'Arq.'],
    rows.map((r, i) => [tdp(r.a), tdp(r.b), tdn(H(r.shared)),
      tdn(`${Number(r.pct).toFixed(0)}%`), twigCell(r.files, i)]));
  const arch = await api('/api/archives?limit=30');
  h += `<h3 style="margin:28px 0 12px">Compactado com a pasta extraída ao lado</h3>`;
  h += table(['Arquivo', 'Volume', 'Tamanho', ''],
    arch.map((r) => [tdp(r.relpath), `<td>${esc(r.label)}</td>`, tdn(H(r.bytes)),
      actCell(absRel(r.label, r.relpath))]));
  el('p-arvores').innerHTML = h;
  el('p-arvores').querySelectorAll('[data-twig]').forEach(
    (b) => b.addEventListener('click', () => toggleTwig(b)));
}

/* ---------------------------------------------------------------- Acervo
   Uma aba para a pergunta "o que eu tenho?", respondida por tipo. Imagens
   continuam com o painel especialista que já existia — dimensão, biblioteca,
   hash visual, grupos parecidos. Vídeo, documento e o resto não têm nada disso
   e não precisam: o que falta ali é poder olhar.

   O ícone de quem não tem miniatura é desenhado aqui, em SVG, e não baixado de
   um banco de ícones: a ferramenta funciona offline por princípio, e um PNG de
   terceiro num repositório GPL é uma pergunta de licença que ninguém quer
   responder depois. */
const ACERVO_ABAS = [
  ['imagem', 'Imagens'], ['documento', 'Documentos'], ['video', 'Vídeos'],
  ['musica', 'Música'], ['jogos', 'Jogos'], ['outro', 'Outros'],
];
let ACV_TIPO = 'imagem';
const ACV_F = { vol: '', ext: '', sort: 'size', comthumb: '' };
const ACV_PG = 120;      // por página
let ACV_PAGE = 0;

/* Famílias de extensão, não extensões soltas: a lista fechada é o que mantém o
   ícone honesto — o que não está aqui ganha a folha genérica em vez de um
   desenho que sugere um conteúdo que ninguém verificou. */
const ICON_FAM = [
  [/^(pdf)$/, 'pdf', '#e5484d'],
  [/^(doc|docx|odt|rtf|pages|txt|md)$/, 'texto', '#3b82f6'],
  [/^(xls|xlsx|ods|csv|numbers)$/, 'tabela', '#22c55e'],
  [/^(ppt|pptx|odp|key)$/, 'slides', '#f97316'],
  [/^(zip|rar|7z|gz|bz2|xz|tar|iso|dmg)$/, 'caixa', '#a855f7'],
  [/^(mp3|wav|flac|aac|m4a|ogg|wma)$/, 'audio', '#06b6d4'],
  [/^(mp4|mkv|avi|mov|wmv|flv|webm|mpg|mpeg)$/, 'video', '#ec4899'],
  [/^(exe|msi|app|deb|pkg|bin)$/, 'binario', '#64748b'],
  [/^(c|h|cpp|cs|js|ts|py|java|go|rs|rb|php|sh|html|css|json|xml)$/, 'codigo', '#eab308'],
];

/* Quando a extensão não diz nada, a CLASSE diz. O servidor já manda `cls`, que
   sai de `classify.EXT2CLASS` — a lista verdadeira, com 63 extensões só de
   empacotamento de jogo. Repetir essas 63 aqui era garantir divergência: na
   primeira versão faltavam `dat`, `bsp`, `cache` e `pc`, e um `.dat` de 19 GB
   dentro de um jogo saía com a folha genérica. */
const ICON_CLS = {
  gamedata: ['jogo', '#8b5cf6'], audio: ['audio', '#06b6d4'],
  video: ['video', '#ec4899'], archive: ['caixa', '#a855f7'],
  code: ['codigo', '#eab308'], binary: ['binario', '#64748b'],
  doc: ['texto', '#3b82f6'], ml_model: ['binario', '#64748b'],
  db: ['tabela', '#22c55e'], model3d: ['jogo', '#8b5cf6'],
};

function iconFor(ext, cls) {
  const e = (ext || '').toLowerCase();
  const fam = ICON_FAM.find(([re]) => re.test(e));
  const porCls = ICON_CLS[cls];
  const [nome, cor] = fam ? [fam[1], fam[2]]
    : porCls || ['arquivo', 'var(--fg-3)'];
  const glifo = {
    pdf: 'PDF', texto: 'TXT', tabela: 'XLS', slides: 'PPT', caixa: 'ZIP',
    audio: '♪', video: '▶', binario: 'EXE', codigo: '{ }', jogo: '◆',
    arquivo: '',
  }[nome] || '';
  return `<svg viewBox="0 0 64 64" class="ficon" role="img"
    aria-label="${esc(e || 'arquivo')}">
    <path d="M14 4h26l12 12v44a4 4 0 0 1-4 4H14a4 4 0 0 1-4-4V8a4 4 0 0 1 4-4z"
      fill="var(--surface-2)" stroke="${cor}" stroke-width="2"/>
    <path d="M40 4v12h12" fill="none" stroke="${cor}" stroke-width="2"/>
    <text x="32" y="44" text-anchor="middle" fill="${cor}"
      font-family="var(--mono)" font-size="${glifo.length > 2 ? 13 : 20}"
      font-weight="600">${esc(glifo)}</text>
    <text x="32" y="57" text-anchor="middle" fill="var(--fg-3)"
      font-family="var(--mono)" font-size="8">${esc(e.slice(0, 6))}</text>
  </svg>`;
}

async function panelAcervo() {
  const alvo = el('p-acervo');
  // O hash primeiro, sempre: a sub-aba e as etiquetas vêm dele, e calcular a
  // contagem antes de ler o filtro dava contagem do acervo inteiro.
  // '#acervo:video&tag=Musica' é sub-aba 'video' filtrada por 'Musica'.
  const pedido = ((location.hash || '').split(':')[1] || '').split('&')[0];
  if (pedido && ACERVO_ABAS.some(([k]) => k === pedido)) ACV_TIPO = pedido;
  lerTagsDoHash();
  await carregaTags();
  // Sob filtro, cada sub-aba diz quanto tem: abrir uma aba vazia sem aviso faz
  // parecer que o filtro quebrou.
  let cont = null;
  if (TAGS_ON.size) {
    const q = new URLSearchParams();
    [...TAGS_ON].forEach((n) => q.append('tag', n));
    try { cont = (await api('/api/acervo-contagem?' + q)).contagem; } catch (_) {}
  }
  ACV_CONT = cont;
  alvo.innerHTML = `<div class="phead"><p class="eyebrow">o que você tem</p>
    <h2>Acervo</h2><p>O mesmo acervo visto por tipo de conteúdo. Cada aba traz
    os filtros que fazem sentido para ela.</p></div>
    <div class="subtabs" role="tablist">${ACERVO_ABAS.map(([k, t]) =>
      `<button role="tab" data-acv="${k}" aria-selected="${k === ACV_TIPO}"
        >${t}${cont ? ` <span class="cnt">${N(cont[k] || 0)}</span>` : ''}</button>`).join('')}</div>
    ${chipsHTML()}
    <div id="acv-corpo"></div>`;
  ligaChips(alvo, () => { ACV_PAGE = 0; panelAcervo(); });
  alvo.querySelectorAll('[data-acv]').forEach((b) => b.addEventListener('click', () => {
    ACV_TIPO = b.dataset.acv;
    ACV_F.vol = ''; ACV_F.ext = ''; ACV_F.comthumb = ''; ACV_PAGE = 0;
    const q = [...TAGS_ON].map((n) => 'tag=' + encodeURIComponent(n)).join('&');
    try { location.hash = 'acervo:' + ACV_TIPO + (q ? '&' + q : ''); } catch (_) {}
    panelAcervo();
  }));
  const corpo = el('acv-corpo');
  if (ACV_TIPO === 'imagem') { await panelImagens(corpo); return; }
  await listaAcervo(corpo);
}

async function listaAcervo(corpo) {
  corpo.innerHTML = '<div class="empty">consultando o índice…</div>';
  const qs = new URLSearchParams({ tipo: ACV_TIPO, limit: String(ACV_PG),
    offset: String(ACV_PAGE * ACV_PG), sort: ACV_F.sort });
  [...TAGS_ON].forEach((n) => qs.append('tag', n));
  if (ACV_F.vol) qs.set('vol', ACV_F.vol);
  if (ACV_F.ext) qs.set('ext', ACV_F.ext);
  if (ACV_F.comthumb) qs.set('comthumb', '1');
  let d;
  try { d = await api('/api/acervo?' + qs.toString()); } catch (e) {
    corpo.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return;
  }
  // `total` é a classe inteira; `filtrado` é o que os filtros atuais deixam, e
  // é ele que decide quantas páginas existem. Paginar sobre o número errado faz
  // o botão "próximas" prometer páginas vazias.
  const filtrado = d.filtrado;
  const paginas = Math.max(1, Math.ceil(filtrado / ACV_PG));
  if (ACV_PAGE >= paginas) { ACV_PAGE = paginas - 1; return listaAcervo(corpo); }
  const semRender = d.total - d.com_thumb - d.faltam;
  const mes = (m) => (m ? new Date(m * 1000).toISOString().slice(0, 7) : '—');
  corpo.innerHTML = `
    <div class="row" style="flex-wrap:wrap;gap:10px">
      <label>volume <select data-f="vol"><option value="">todos</option>
        ${d.vols.map((v) => `<option${v === ACV_F.vol ? ' selected' : ''}>${esc(v)}</option>`).join('')}
      </select></label>
      <label>tipo <select data-f="ext"><option value="">todos</option>
        ${d.exts.map((x) => `<option value="${esc(x.ext)}"${x.ext === ACV_F.ext ? ' selected' : ''}
          >.${esc(x.ext)} (${N(x.n)})</option>`).join('')}
      </select></label>
      <label>ordem <select data-f="sort">
        ${[['size', 'maiores'], ['recent', 'mais novos'], ['old', 'mais antigos'],
           ['name', 'nome']].map(([v, t]) =>
          `<option value="${v}"${v === ACV_F.sort ? ' selected' : ''}>${t}</option>`).join('')}
      </select></label>
      <label><input type="checkbox" data-f="comthumb"${ACV_F.comthumb ? ' checked' : ''}>
        só com miniatura</label>
    </div>
    <p class="note">${filtrado === d.total
      ? `${N(d.total)} arquivos, ${H(d.bytes)}`
      : `${N(filtrado)} de ${N(d.total)} arquivos sob estes filtros, ${H(d.bytes_filtrado)}`}.
      ${N(d.com_thumb)} têm miniatura${d.faltam > 0 ? `; <b>${N(d.faltam)} estão na
      fila</b> — gere com <code>python3 -m diskmap acervo --cls ${
        esc(d.rows[0] ? d.rows[0].cls : '')}</code> ou pela aba Tarefas` : ''}${
      semRender > 0 ? `; ${N(semRender)} não têm renderizador e ficam com ícone
      para sempre` : ''}.${!d.ffmpeg && ACV_TIPO === 'video'
      ? ' <b>ffmpeg não está instalado</b>, então mkv, avi, wmv, flv e webm ficam só com ícone.' : ''}</p>
    ${nadaPorInterseccao(filtrado)}
    ${barraSelecao()}
    <div class="row" style="margin-top:10px">
      <button class="btn" data-seltodos>marcar/desmarcar os desta página</button>
    </div>
    <div class="grade">${d.rows.map((r) => `
      <figure class="cart${SEL.has(r.abs) ? ' sel' : ''}">
        ${selCheck(r.abs)}
        <div class="cart-img">${r.thumb
          ? `<img src="/thumb/${r.id}.jpg" alt="" loading="lazy">`
          : iconFor(r.ext, r.cls)}${!r.thumb && r.pode && !r.note
            ? '<span class="pend" title="o renderizador ainda não passou por este arquivo">na fila</span>'
            : ''}</div>
        <figcaption>
          <div class="cart-nome" title="${esc(r.rel)}">${esc(r.name)}</div>
          <div class="cart-meta">${H(r.size)} · ${mes(r.mtime)} · ${esc(r.label)}</div>
          ${r.width ? `<div class="cart-meta">${r.width}×${r.height}</div>` : ''}
          ${r.note ? `<div class="cart-meta" style="color:var(--fg-3)">${esc(r.note)}</div>` : ''}
        </figcaption>
        ${ACTIONS_ON && r.abs ? `<button class="actbtn" data-act-path="${esc(r.abs)}"
          >ações</button>` : ''}
      </figure>`).join('')}</div>
    <div class="gridbar" style="margin-top:16px">
      <button class="btn" data-pg="-1"${ACV_PAGE === 0 ? ' hidden' : ''}>← anteriores</button>
      <span class="note" style="margin:0">${filtrado === d.total ? '' : N(filtrado) + ' de '}
        ${N(d.total)} arquivos${paginas > 1
          ? ` · página ${N(ACV_PAGE + 1)} de ${N(paginas)}` : ''}</span>
      <button class="btn" data-pg="1"${(ACV_PAGE + 1) * ACV_PG >= filtrado
        ? ' hidden' : ''}>próximas →</button>
    </div>`;
  corpo.querySelectorAll('[data-pg]').forEach((b) => b.addEventListener('click', () => {
    ACV_PAGE += Number(b.dataset.pg);
    listaAcervo(corpo).then(() => corpo.scrollIntoView({ block: 'start' }));
  }));
  ligaSelecao(corpo, () => listaAcervo(corpo));
  corpo.querySelectorAll('[data-f]').forEach((c) => c.addEventListener('change', () => {
    ACV_F[c.dataset.f] = c.type === 'checkbox' ? (c.checked ? '1' : '') : c.value;
    ACV_PAGE = 0;              // filtro novo, contagem nova: começar do início
    listaAcervo(corpo);
  }));
}

/* O que o índice NÃO sabe sozinho: o arquivo ainda está onde foi visto?
   'ok' não ganha etiqueta — a ausência de aviso já é a informação, e marcar o
   caso normal faria o anormal desaparecer no meio. */
const EST_TAG = {
  quarentena: '<span class="tag warn">na quarentena</span>',
  lixeira: '<span class="tag warn">na Lixeira</span>',
  sumido: '<span class="tag crit">não está mais no disco</span>',
  desmontado: '<span class="tag">volume desconectado</span>',
};
const estTag = (e) => (e && e !== 'ok' ? ` ${EST_TAG[e] || ''}` : '');

const VER_TAG = {
  full: '<span class="tag ok">hash completo</span>',
  quick: '<span class="tag ok">hash parcial</span>',
  mismatch: '<span class="tag crit">divergente</span>',
  no: '<span class="tag warn">não verificado</span>',
};

async function panelDuplicatas() {
  const sel = el('dupfilter');
  const f = sel ? sel.value : '';
  const rows = await api('/api/dups?limit=80' + (f ? `&verified=${f}` : ''));
  let h = `<div class="phead"><p class="eyebrow">cópias</p>
    <h2>${N(OV.dup.groups)} grupos repetidos</h2>
    <p>Mesmo nome e mesmo tamanho em lugares diferentes — candidatos, não prova.
    A etiqueta diz se o conteúdo já foi confirmado por hash.</p></div>
    <div class="row"><label for="dupfilter">mostrar</label>
    <select id="dupfilter">
      <option value="">todos</option>
      <option value="no">não verificados</option>
      <option value="quick">hash parcial</option>
      <option value="full">hash completo</option>
      <option value="mismatch">divergentes</option>
    </select></div>`;
  h += `<p class="note">Clique no número de <b>cópias</b> para ver cada uma e
    escolher qual fica.</p>`;
  h += table(['Arquivo', 'Tam.', 'Cópias', 'Redundante', 'Estado', 'Onde'],
    rows.map((r) => [
      tdp(r.name), tdn(H(r.size)), dupCell(r.copies, r.id), tdn(H(r.wasted)),
      `<td>${VER_TAG[r.verified] || esc(r.verified)}</td>`,
      `<td class="path">${r.paths.slice(0, 4).map(esc).join('<br>')}${
        r.paths.length > 4 ? `<br><span style="color:var(--fg-3)">+${r.paths.length - 4} outro(s)</span>` : ''}</td>`,
    ]));
  el('p-duplicatas').innerHTML = h;
  el('p-duplicatas').querySelectorAll('[data-dup]').forEach(
    (b) => b.addEventListener('click', () => toggleDup(b)));
  const s2 = el('dupfilter');
  if (s2) { s2.value = f; s2.addEventListener('change', panelDuplicatas); }
}

/* A gaveta de duplicatas. Mesmo gesto da aba de árvores, com uma diferença que
   muda tudo: ali são dois lados, aqui são N cópias. Agir uma por vez num grupo
   de cinco é trabalho repetido e a forma fácil de terminar sem nenhuma, então a
   operação é do grupo: uma fica, as outras saem. */
const DUP_CACHE = {};
const DUP_KEEP = {};   // grupo -> caminho absoluto escolhido para ficar

const dupCell = (n, grp) => `<td class="num twigcell">
  <button class="twig" data-dup="${grp}" aria-expanded="false"
    title="ver as cópias e escolher qual fica">${N(n)}×<span class="arrow"
    aria-hidden="true">▸</span></button></td>`;

function dupBody(d) {
  if (d.erro) return `<div class="empty">${esc(d.erro)}</div>`;
  const mes = (m) => (m ? new Date(m * 1000).toISOString().slice(0, 7) : '—');
  // O primeiro da lista já vem ordenado por keep_score e com os ausentes no
  // fim, então o default é o melhor sobrevivente que de fato existe.
  const vivos = d.membros.filter((m) => m.exists);
  const escolhido = DUP_KEEP[d.id] || (vivos[0] && vivos[0].abs);
  const linhas = d.membros.map((m) => {
    const marcado = m.exists && m.abs === escolhido;
    const nota = !m.abs ? ' <span class="tag warn">volume desmontado</span>'
      : !m.exists ? ' <span class="tag crit">não está mais no disco</span>' : '';
    return [
      `<td class="num">${ACTIONS_ON && m.exists
        ? `<input type="radio" name="keep${d.id}" data-keep="${esc(m.abs)}"
             data-grp="${d.id}"${marcado ? ' checked' : ''}>` : ''}</td>`,
      `<td class="path"><span class="hi">${esc(m.full)}</span></td>`,
      tdn(H(m.size)), tdn(mes(m.mtime)),
      `<td class="path">${esc(m.reason)}${nota}</td>`,
    ];
  });
  const velho = d.sumidos ? `<div class="warnbox crit"><b>${N(d.sumidos)} de
    ${N(d.membros.length)} cópias não estão mais no disco.</b> O índice é de uma
    varredura anterior e envelheceu. Rode <b>Indexar um volume</b> e
    <b>Recalcular achados</b> antes de agir neste grupo.</div>` : '';
  const gate = d.verified === 'mismatch'
    ? `<div class="warnbox crit"><b>Grupo divergente.</b> O hash provou que estas
       cópias têm conteúdo diferente — são arquivos distintos com o mesmo nome e
       tamanho. Não há redundância para remover, e descartar aqui perde conteúdo.
       O botão está desligado de propósito.</div>`
    : d.verified === 'full'
      ? `<div class="warnbox ok"><b>sha256 confirmado nas ${N(d.copies)} cópias.</b>
         Descartar as outras libera <b>${H(d.wasted)}</b> sem perder conteúdo.</div>`
      : `<div class="warnbox warn"><b>${d.verified === 'quick'
          ? 'Só o hash parcial foi conferido' : 'Ninguém conferiu o conteúdo'}.</b>
         Nome e tamanho é candidato, não prova. Confira antes de descartar.</div>`;
  return `<div class="twigbox">${velho}${gate}
    ${table([ACTIONS_ON ? 'fica' : '', 'Cópia', 'Tam.', 'quando', 'por quê'], linhas)}
    ${ACTIONS_ON ? `<div class="sheet-foot" style="justify-content:flex-start">
      ${d.verified === 'full' || d.verified === 'mismatch' ? '' :
        `<button class="btn" data-vgrp="${d.id}">Conferir agora (sha256)</button>`}
      <button class="btn danger" data-dgrp="${d.id}"${
        d.verified === 'mismatch' || d.sumidos || vivos.length < 2
          ? ' disabled' : ''}>descartar as outras
        ${N(Math.max(0, vivos.length - 1))}</button></div>` : ''}</div>`;
}

async function toggleDup(b) {
  const tr = b.closest('tr');
  if (b.getAttribute('aria-expanded') === 'true') {
    b.setAttribute('aria-expanded', 'false');
    const d = tr.nextElementSibling;
    if (d && d.classList.contains('det')) d.remove();
    return;
  }
  b.setAttribute('aria-expanded', 'true');
  const grp = b.dataset.dup;
  const det = document.createElement('tr');
  det.className = 'det';
  det.innerHTML = `<td colspan="${tr.children.length}">
    <div class="empty">consultando o índice…</div></td>`;
  tr.after(det);
  try {
    if (!DUP_CACHE[grp]) DUP_CACHE[grp] = await api(`/api/dup-members?grp=${grp}`);
    pintaDup(det, DUP_CACHE[grp]);
  } catch (e) {
    det.querySelector('td').innerHTML =
      `<div class="empty">não deu para consultar: ${esc(e.message || e)}</div>`;
  }
}

function pintaDup(det, d) {
  det.querySelector('td').innerHTML = dupBody(d);
  det.querySelectorAll('[data-keep]').forEach((r) => r.addEventListener('change', () => {
    DUP_KEEP[r.dataset.grp] = r.dataset.keep;
  }));
  const v = det.querySelector('[data-vgrp]');
  if (v) v.addEventListener('click', () => conferirGrupo(d, det));
  const g = det.querySelector('[data-dgrp]');
  if (g) g.addEventListener('click', () => descartarGrupo(d));
}

async function conferirGrupo(d, det) {
  const box = det.querySelector('.sheet-foot');
  box.innerHTML = '<pre class="out">lendo as cópias do disco…</pre>';
  try {
    const r = await post('/api/verify-group', { grp: d.id, stage: 'full' });
    d.verified = r.verified;
    DUP_CACHE[d.id] = d;
    toast(r.output, r.verified === 'mismatch' ? 'err' : 'ok');
    loaded.duplicatas = false;
    pintaDup(det, d);
  } catch (e) {
    box.innerHTML = `<div class="warnbox crit">${esc(e.message)}</div>`;
  }
}

function descartarGrupo(d) {
  const fica = DUP_KEEP[d.id] || (d.membros[0] && d.membros[0].abs);
  const saem = d.membros.filter((m) => m.exists && m.abs !== fica);
  const bytes = saem.reduce((a, m) => a + m.size, 0);
  sheet().innerHTML = `<div class="sheet-in" role="dialog" aria-modal="true">
    <h3>Descartar ${N(saem.length)} cópia(s) · ${esc(d.name)}</h3>
    <div class="warnbox${d.verified === 'full' ? ' ok' : ' warn'}">
      Libera <b>${H(bytes)}</b>. ${d.verified === 'full'
        ? 'As cópias têm sha256 idêntico: nada de conteúdo se perde.'
        : 'O conteúdo <b>não</b> foi confirmado por hash — isto é uma aposta.'}</div>
    <div class="sheet-path"><b>fica</b> ${esc(fica)}</div>
    ${saem.map((m) => `<div class="sheet-path"><b>sai</b> ${esc(m.abs)}</div>`).join('')}
    <div class="warnbox">Tudo vai para <code>.diskmap-quarantine</code> no volume
      de cada uma. Nada é apagado, e o Histórico desfaz uma a uma.</div>
    <div class="sheet-foot">
      <button class="btn" data-close>Cancelar</button>
      <button class="btn danger" data-go>Descartar ${N(saem.length)}</button>
    </div></div>`;
  sheet().hidden = false;
  sheet().querySelector('[data-close]').addEventListener('click', closeSheet);
  sheet().querySelector('[data-go]').addEventListener('click', async () => {
    sheet().innerHTML = `<div class="sheet-in"><h3>Descartando…</h3>
      <pre class="out">movendo para a quarentena…</pre></div>`;
    const foot = '<div class="sheet-foot"><button class="btn" data-close>Fechar</button></div>';
    try {
      const res = await post('/api/discard-group', { grp: d.id, keep: fica });
      sheet().innerHTML = `<div class="sheet-in"><h3>${esc(d.name)}</h3>
        <pre class="out">${esc(res.output)}</pre>
        ${res.falhas.length ? `<div class="warnbox crit">${
          res.falhas.map((f) => esc(f.path + ' — ' + f.erro)).join('<br>')}</div>` : ''}
        <div class="warnbox ok">ficou: <code>${esc(res.kept)}</code></div>
        ${reindexNota()}${foot}</div>`;
      toast(`${res.descartados.length} na quarentena · ${H(res.bytes)}`, 'ok');
      loaded.duplicatas = false; loaded.historico = false; loaded.resumo = false;
      delete DUP_CACHE[d.id];
    } catch (e) {
      sheet().innerHTML = `<div class="sheet-in"><h3>${esc(d.name)}</h3>
        <div class="warnbox crit">${esc(e.message)}</div>${foot}</div>`;
      toast(e.message, 'err');
    }
    sheet().querySelector('[data-close]').addEventListener('click', closeSheet);
  });
}

async function panelPastas() {
  const sel = el('volfilter');
  const v = sel ? sel.value : '';
  const d = el('depthfilter');
  const depth = d ? d.value : '1';
  const rows = await api(`/api/dirs?depth=${depth}&limit=60` + (v ? `&vol=${encodeURIComponent(v)}` : ''));
  let h = `<div class="phead"><p class="eyebrow">onde o peso está</p><h2>Pastas por tamanho</h2>
    <p>Tamanho recursivo real: cada pasta soma tudo abaixo dela.</p></div>
    <div class="row">
      <label for="volfilter">volume</label>
      <select id="volfilter"><option value="">todos</option>${
        OV.volumes.map((x) => `<option value="${esc(x.label)}">${esc(x.label)}</option>`).join('')}</select>
      <label for="depthfilter">profundidade</label>
      <select id="depthfilter">${[1, 2, 3, 4].map((n) =>
        `<option value="${n}">${n}</option>`).join('')}</select>
    </div>`;
  h += table(['Pasta', 'Tamanho', 'Arquivos', ''], rows.map((r) => [
    tdp(`${r.label}/${r.relpath}`), tdn(H(r.bytes)), tdn(N(r.files)),
    actCell(absDir(r))]));
  el('p-pastas').innerHTML = h;
  const s1 = el('volfilter'), s3 = el('depthfilter');
  if (s1) { s1.value = v; s1.addEventListener('change', panelPastas); }
  if (s3) { s3.value = depth; s3.addEventListener('change', panelPastas); }
}

async function panelFrio() {
  const [cold, big] = await Promise.all([api('/api/cold?limit=50'), api('/api/biggest?limit=40')]);
  const when = (m) => m ? new Date(m * 1000).toISOString().slice(0, 7) : '—';
  let h = `<div class="phead"><p class="eyebrow">arquivo morto</p><h2>Dado grande e frio</h2>
    <p>Arquivos acima de 1 GB sem modificação há mais de dois anos. Candidatos naturais
    a sair do disco de trabalho.</p></div>`;
  const cols = ['Arquivo', 'Tamanho', 'Modificado', ''];
  h += table(cols, cold.map((r) => [
    tdp(fullPath(r)), tdn(H(r.size)), tdn(when(r.mtime)), actCell(abs(r))]));
  h += `<h3 style="margin:28px 0 12px">Os maiores arquivos do acervo</h3>`;
  h += table(cols, big.map((r) => [
    tdp(fullPath(r)), tdn(H(r.size)), tdn(when(r.mtime)), actCell(abs(r))]));
  el('p-frio').innerHTML = h;
}

let searchTimer = null;
/* ------------------------------------------------------------- etiquetas
   O único dado deste índice que ninguém consegue recalcular. Duplicata, hash,
   miniatura: tudo isso se refaz com tempo. "isto é música" é um juízo, e some
   para sempre se o lugar errado guardar.

   Na busca elas são balões: entram no filtro com um clique e saem pelo × —
   nada de uma sintaxe escondida que você tenha de lembrar. */
let TAGS = [];                 // o catálogo, recarregado quando muda
const TAGS_ON = new Set();     // as que estão filtrando agora

/* O filtro vive no hash: '#acervo:video&tag=Musica'. Um filtro que não cabe num
   link é um filtro que você não consegue mandar para ninguém nem recuperar
   depois de recarregar — e aqui ele também é o que o botão "ver no acervo"
   carrega de um painel para o outro. */
function lerTagsDoHash() {
  const h = (location.hash || '').replace('#', '');
  const q = h.includes('&') ? h.slice(h.indexOf('&') + 1) : '';
  const vindas = new URLSearchParams(q).getAll('tag');
  if (vindas.length) { TAGS_ON.clear(); vindas.forEach((t) => TAGS_ON.add(t)); }
}

function gravaTagsNoHash() {
  const h = (location.hash || '').replace('#', '');
  const base = h.split('&')[0] || 'busca';
  const q = [...TAGS_ON].map((t) => 'tag=' + encodeURIComponent(t)).join('&');
  try { location.hash = base + (q ? '&' + q : ''); } catch (_) {}
}

async function carregaTags() {
  try { TAGS = (await api('/api/tags')).tags || []; } catch (_) { TAGS = []; }
  return TAGS;
}

const balao = (n, ativa) => (ativa
  ? `<span class="chip on" data-chipnome="${esc(n)}" title="dois cliques para renomear">${esc(n)}
      <button data-chipoff="${esc(n)}" aria-label="tirar o filtro ${esc(n)}">×</button>
    </span>`
  : `<button class="chip" data-chipon="${esc(n)}" data-chipnome="${esc(n)}"
      title="dois cliques para renomear">${esc(n)}</button>`);

/* Renomear no próprio balão, com dois cliques.
   O balão apagado é um <button> que liga o filtro, então os dois cliques
   ligariam o filtro e o redesenho comeria o campo antes de alguém digitar.
   Por isso o clique simples espera: 220 ms, que some ao lado da consulta que
   ele dispara, e o segundo clique cancela a espera. O balão aceso é um <span>
   -- só o × dele clica -- e ali não há conflito nenhum para resolver. */
function editaBalao(el, nome, redesenha) {
  if (el.querySelector('input')) return;
  const antes = el.innerHTML;
  el.innerHTML = `<input class="chipedit" value="${esc(nome)}" maxlength="48"
    aria-label="novo nome da etiqueta ${esc(nome)}">`;
  const inp = el.querySelector('input');
  inp.focus(); inp.select();
  let pronto = false;
  const desiste = () => { if (!pronto) { pronto = true; el.innerHTML = antes; } };
  inp.addEventListener('keydown', async (ev) => {
    ev.stopPropagation();
    if (ev.key === 'Escape') { desiste(); return; }
    if (ev.key !== 'Enter') return;
    const novo = (inp.value || '').trim();
    if (!novo || novo === nome) { desiste(); return; }
    pronto = true;
    inp.disabled = true;
    try {
      await post('/api/tag', { acao: 'rename', name: nome, novo });
      // O filtro guarda NOMES: sem trocar aqui, renomear uma etiqueta ativa
      // deixaria o painel filtrando por um nome que não existe mais, e a
      // listagem viria vazia sem dizer por quê.
      if (TAGS_ON.has(nome)) { TAGS_ON.delete(nome); TAGS_ON.add(novo); }
      gravaTagsNoHash();
      await carregaTags();
      loaded.busca = false; loaded.acervo = false;
      toast(`etiqueta renomeada para ${novo}`, 'ok');
      redesenha();
    } catch (e) {
      toast(e.message, 'err');
      pronto = false; inp.disabled = false; inp.focus();
    }
  });
  inp.addEventListener('blur', desiste);
  inp.addEventListener('click', (ev) => ev.stopPropagation());
}

/* Duas camadas: na primeira, as etiquetas-raiz; na segunda, as filhas das que
   estão ativas. A segunda linha só existe quando há filha para mostrar, e diz
   de quem é cada grupo quando mais de uma mãe está ativa — senão "Extras" e
   "Extras" de mães diferentes ficariam lado a lado sem dizer de onde vêm. */
function chipsHTML() {
  const raizes = TAGS.filter((t) => !t.parent);
  const ativas = [...TAGS_ON];
  const linha1 = [
    ...ativas.filter((n) => raizes.some((t) => t.name === n)).map((n) => balao(n, true)),
    ...raizes.filter((t) => !TAGS_ON.has(t.name) && (t.caminhos || TAGS.some(
      (f) => f.parent === t.id))).map((t) => balao(t.name, false)),
  ];
  const grupos = [];
  ativas.forEach((n) => {
    const mae = TAGS.find((t) => t.name === n);
    if (!mae) return;
    const filhas = TAGS.filter((t) => t.parent === mae.id);
    if (!filhas.length) return;
    grupos.push({ mae: mae.name, filhas });
  });
  // Filha ativa cujo pai saiu do filtro continua visível: tirar o balão de uma
  // coisa que ainda está filtrando é esconder o filtro de quem o ligou.
  const orfas = ativas.filter((n) => {
    const t = TAGS.find((x) => x.name === n);
    return t && t.parent && !ativas.includes(t.pai_nome);
  });
  if (!linha1.length && !grupos.length && !orfas.length) return '';
  const varias = grupos.length > 1;
  return `<div class="chips">${linha1.join('')}
    ${orfas.map((n) => balao(n, true)).join('')}</div>
    ${grupos.map((g) => `<div class="chips sub">
      ${varias ? `<span class="subde">${esc(g.mae)} ›</span>` : ''}
      ${g.filhas.map((f) => balao(f.name, TAGS_ON.has(f.name))).join('')}
    </div>`).join('')}`;
}

/* Duas etiquetas somam exigência, não alternativa: o arquivo precisa ter as
   duas. É o que empilhar filtro significa em qualquer outro lugar da ferramenta
   — mas dá zero sem explicação, e zero sem explicação parece defeito. */
function nadaPorInterseccao(n) {
  if (n || TAGS_ON.size < 2) return '';
  return `<div class="warnbox">Nenhum arquivo tem <b>todas</b> as
    ${N(TAGS_ON.size)} etiquetas ao mesmo tempo. Empilhar etiquetas soma
    exigência; tire uma no × para ver o que cada uma cobre.</div>`;
}

/* ------------------------------------------------- seleção em massa
   Etiquetar um por um não escala, mas etiquetar 262 arquivos também não é a
   melhor resposta quando eles moram em 9 pastas: a etiqueta de pasta já cobre
   o conteúdo, inclusive o que entrar depois. A barra diz isso em vez de deixar
   a pessoa descobrir sozinha depois de gravar 262 linhas. */
const SEL = new Set();

const selCheck = (abs) => (!abs ? '' : `<input type="checkbox" class="selbox"
  data-sel="${esc(abs)}"${SEL.has(abs) ? ' checked' : ''}
  aria-label="selecionar para etiquetar">`);

const soPastas = () => [...SEL].every((p) => !/\.[A-Za-z0-9]{1,6}$/.test(p));

/* As pastas que contêm o que está selecionado, sem repetir.
   Dez arquivos da mesma pasta são UMA etiqueta, não dez: é esse o ganho da
   operação, e sem a deduplicação ela seria só o mesmo trabalho com outro nome.
   O que já é pasta entra como está — subir mais um nível alcançaria as pastas
   irmãs, que ninguém pediu. */
function pastasDaSelecao() {
  const out = new Set();
  SEL.forEach((p) => {
    const ehPasta = !/\.[A-Za-z0-9]{1,6}$/.test(p);
    const corte = p.lastIndexOf('/');
    out.add(ehPasta || corte <= 0 ? p : p.slice(0, corte));
  });
  return [...out];
}

function barraSelecao() {
  if (!SEL.size) return '';
  return `<div class="selbar" role="region" aria-label="seleção">
    <b data-selcount>${N(SEL.size)}</b> selecionado(s)
    <input class="textin" id="selnome" list="taglist" placeholder="etiqueta…"
      maxlength="48">
    <datalist id="taglist">${TAGS.map((t) =>
      `<option value="${esc(t.name)}">`).join('')}</datalist>
    <select id="selpai"><option value="">solta</option>
      ${TAGS.filter((t) => !t.parent).map((t) =>
        `<option value="${esc(t.name)}">dentro de ${esc(t.name)}</option>`).join('')}
    </select>
    <button class="btn primary" data-selgo="add">etiquetar</button>
    <button class="btn" data-selpasta>etiquetar a pasta…</button>
    <button class="btn" data-selgo="remove">tirar</button>
    <button class="btn" data-selclear>limpar</button>
    <span class="selhint"${soPastas() ? ' hidden' : ''}>dica: etiquetar a
      <b>pasta</b> cobre tudo que está dentro, inclusive o que entrar depois</span>
  </div>`;
}

/* Marcar uma caixa não pode custar uma consulta ao índice.
   A primeira versão religava tudo com um redesenho, e redesenhar é refazer a
   busca: a grade piscava "consultando o índice…" a cada clique e a página
   voltava para o topo. Aqui o clique mexe só no que mudou — a classe do
   cartão e o contador da barra — e a barra só é criada ou removida quando
   cruza o zero. O valor digitado no campo de etiqueta sobrevive, porque o nó
   não é recriado. */
function sincronizaBarra(alvo, redesenha) {
  let bar = alvo.querySelector('.selbar');
  if (!SEL.size) {
    if (bar) bar.remove();
    return;
  }
  if (!bar) {
    const ancora = alvo.querySelector('.grade') || alvo.querySelector('.grid')
      || alvo.querySelector('.tw');
    if (!ancora) return;
    const tmp = document.createElement('div');
    tmp.innerHTML = barraSelecao();
    bar = tmp.firstElementChild;
    ancora.parentNode.insertBefore(bar, ancora);
    ligaBotoesDaBarra(alvo, redesenha);
    return;
  }
  bar.querySelector('[data-selcount]').textContent = N(SEL.size);
  const dica = bar.querySelector('.selhint');
  if (dica) dica.hidden = soPastas();
}

function marcaCartao(c) {
  const cartao = c.closest('.cart, .sh, tr');
  if (cartao) cartao.classList.toggle('sel', c.checked);
}

function ligaSelecao(alvo, redesenha) {
  alvo.querySelectorAll('[data-sel]').forEach((c) => c.addEventListener('change', () => {
    if (c.checked) SEL.add(c.dataset.sel); else SEL.delete(c.dataset.sel);
    marcaCartao(c);
    sincronizaBarra(alvo, redesenha);
  }));
  const todos = alvo.querySelector('[data-seltodos]');
  if (todos) todos.addEventListener('click', () => {
    const cx = [...alvo.querySelectorAll('[data-sel]')];
    const ligar = cx.some((c) => !SEL.has(c.dataset.sel));
    cx.forEach((c) => {
      if (ligar) SEL.add(c.dataset.sel); else SEL.delete(c.dataset.sel);
      c.checked = ligar;
      marcaCartao(c);
    });
    sincronizaBarra(alvo, redesenha);
  });
  ligaBotoesDaBarra(alvo, redesenha);
}

function ligaBotoesDaBarra(alvo, redesenha) {
  const limpar = alvo.querySelector('[data-selclear]');
  if (limpar) limpar.addEventListener('click', () => {
    SEL.clear();
    alvo.querySelectorAll('[data-sel]').forEach((c) => {
      c.checked = false; marcaCartao(c);
    });
    sincronizaBarra(alvo, redesenha);
  });
  const bp = alvo.querySelector('[data-selpasta]');
  if (bp) bp.addEventListener('click', () => {
    const nome = (alvo.querySelector('#selnome').value || '').trim();
    if (!nome) { toast('escreva o nome da etiqueta', 'err'); return; }
    folhaPasta(nome, alvo.querySelector('#selpai').value || null, alvo, redesenha);
  });
  alvo.querySelectorAll('[data-selgo]').forEach((b) => b.addEventListener('click', async () => {
    const nome = (alvo.querySelector('#selnome').value || '').trim();
    if (!nome) { toast('escreva o nome da etiqueta', 'err'); return; }
    const pai = alvo.querySelector('#selpai').value || null;
    const acao = b.dataset.selgo;
    b.disabled = true;
    try {
      const r = await post('/api/tag-bulk', { name: nome, paths: [...SEL], pai, acao });
      toast(r.output, r.ok ? 'ok' : 'err');
      if (r.pulados && r.pulados.length) {
        console.warn('caminhos recusados', r.pulados);
      }
      SEL.clear();
      await carregaTags();
      loaded.busca = false; loaded.acervo = false;
    } catch (e) { toast(e.message, 'err'); }
    b.disabled = false;
    redesenha();
  }));
}

/* Etiquetar a pasta alcança o que NÃO está selecionado, e o que ainda nem
   existe. Isso é justamente a utilidade — e justamente por isso é a única
   operação de etiqueta que pede confirmação: o resto do painel só mexe no que
   você marcou, e aqui o alcance passa do que está na tela. */
function folhaPasta(nome, pai, alvo, redesenha) {
  const pastas = pastasDaSelecao();
  const mostra = pastas.slice(0, 12);
  sheet().innerHTML = `<div class="sheet-in" role="dialog" aria-modal="true">
    <h3>Etiquetar ${N(pastas.length)} pasta(s) como <b>${esc(nome)}</b></h3>
    <div class="warnbox">A etiqueta fica na <b>pasta</b>, não nos arquivos: vale
      para tudo que está dentro dela — inclusive o que você não selecionou e o
      que entrar depois. ${pai ? `Fica dentro de <b>${esc(pai)}</b>.` : ''}</div>
    ${mostra.map((p) => `<div class="sheet-path">${esc(p)}</div>`).join('')}
    ${pastas.length > mostra.length
      ? `<div class="sheet-path">… e mais ${N(pastas.length - mostra.length)}</div>` : ''}
    <div class="sheet-foot">
      <button class="btn" data-close>Cancelar</button>
      <button class="btn primary" data-go>Etiquetar ${N(pastas.length)} pasta(s)</button>
    </div></div>`;
  sheet().hidden = false;
  sheet().querySelector('[data-close]').addEventListener('click', closeSheet);
  sheet().querySelector('[data-go]').addEventListener('click', async () => {
    const b = sheet().querySelector('[data-go]');
    b.disabled = true;
    try {
      const r = await post('/api/tag-bulk', { name: nome, paths: pastas, pai, acao: 'add' });
      toast(r.output, r.ok ? 'ok' : 'err');
      if (r.pulados && r.pulados.length) console.warn('caminhos recusados', r.pulados);
      SEL.clear();
      await carregaTags();
      loaded.busca = false; loaded.acervo = false;
      closeSheet();
      redesenha();
    } catch (e) {
      toast(e.message, 'err');
      b.disabled = false;
    }
  });
}

function ligaChips(alvo, redesenha) {
  alvo.querySelectorAll('[data-chipnome]').forEach((el) => {
    el.addEventListener('dblclick', (ev) => {
      ev.preventDefault(); ev.stopPropagation();
      clearTimeout(el._tLiga);
      editaBalao(el, el.dataset.chipnome, redesenha);
    });
  });
  alvo.querySelectorAll('[data-chipon]').forEach((b) => b.addEventListener('click', () => {
    if (b.querySelector('input')) return;
    clearTimeout(b._tLiga);
    b._tLiga = setTimeout(() => {
      TAGS_ON.add(b.dataset.chipon); gravaTagsNoHash(); redesenha();
    }, 220);
  }));
  alvo.querySelectorAll('[data-chipoff]').forEach((b) => b.addEventListener('click', () => {
    TAGS_ON.delete(b.dataset.chipoff); gravaTagsNoHash(); redesenha();
  }));
}

async function panelBusca(term) {
  const t = (term !== undefined ? term : (el('q').value || '')).trim();
  const target = el('p-busca');
  lerTagsDoHash();
  await carregaTags();
  const cabeca = `<div class="phead"><p class="eyebrow">offline</p><h2>Onde está…</h2>
      <p>A busca responde pelo índice, então acha arquivo em disco desconectado
      — e te diz em qual volume plugar. Clique numa etiqueta para filtrar por
      ela.</p></div>${chipsHTML()}`;
  if (t.length < 2 && !TAGS_ON.size) {
    target.innerHTML = cabeca
      + '<div class="empty">digite ao menos dois caracteres, ou escolha uma etiqueta</div>';
    ligaChips(target, () => panelBusca(term));
    return;
  }
  const qs = new URLSearchParams({ limit: '200' });
  if (t.length >= 2) qs.set('q', t);
  [...TAGS_ON].forEach((n) => qs.append('tag', n));
  const res = await api('/api/search?' + qs.toString());
  const mounted = {}; OV.volumes.forEach((v) => { mounted[v.label] = !!v.mount; });
  const titulo = [t.length >= 2 ? `“${esc(t)}”` : null,
    ...[...TAGS_ON].map((n) => `etiqueta ${esc(n)}`)].filter(Boolean).join(' · ');
  let h = `<div class="phead phead-row"><div>
      <p class="eyebrow">offline</p>
      <h2>${titulo}</h2><p>${N(res.files.length)}${res.truncado ? '+' : ''} arquivos
      e ${N(res.dirs.length)} pastas · ${H(res.bytes)}.</p></div>
      ${TAGS_ON.size ? '<button class="btn" data-veracervo>ver no acervo →</button>' : ''}
    </div>${chipsHTML()}`;
  if (res.dirs.length) {
    h += '<h3 style="margin:0 0 12px">Pastas</h3>';
    h += table(['Pasta', 'Tamanho', 'Arquivos'], res.dirs.map((r) => [
      tdp(`${r.label}/${r.relpath}`), tdn(H(r.bytes)), tdn(N(r.files))]));
  }
  h += nadaPorInterseccao(res.files.length);
  if (res.files.length) {
    h += '<h3 style="margin:28px 0 12px">Arquivos</h3>';
    h += barraSelecao() + `<div class="row" style="margin:8px 0">
      <button class="btn" data-seltodos>marcar/desmarcar os desta lista</button></div>`;
    h += table(['', 'Arquivo', 'Tamanho', 'Volume', ''], res.files.map((r) => [
      `<td class="num">${selCheck(abs(r))}</td>`,
      tdp(fullPath(r)), tdn(H(r.size)),
      `<td>${esc(r.label)} ${mounted[r.label] ? '' : '<span class="tag">plugar</span>'}</td>`,
      mounted[r.label] ? actCell(abs(r)) : '<td class="act"></td>']));
  }
  target.innerHTML = h;
  ligaChips(target, () => panelBusca(term));
  ligaSelecao(target, () => panelBusca(term));
  const vb = target.querySelector('[data-veracervo]');
  if (vb) vb.addEventListener('click', () => verNoAcervo());
}

/* Levar o filtro para o Acervo sem cair numa aba vazia: as etiquetas já são
   estado global, então o que falta é escolher a sub-aba que de fato tem
   conteúdo sob elas. Perguntar é mais barato que adivinhar. */
async function verNoAcervo() {
  const q = new URLSearchParams();
  [...TAGS_ON].forEach((n) => q.append('tag', n));
  try {
    const c = (await api('/api/acervo-contagem?' + q)).contagem || {};
    const melhor = ACERVO_ABAS.map(([k]) => k)
      .sort((a, b) => (c[b] || 0) - (c[a] || 0))[0];
    if (c[melhor]) ACV_TIPO = melhor;
  } catch (_) { /* sem contagem, mantém a sub-aba atual */ }
  ACV_PAGE = 0;
  loaded.acervo = false;
  const sufixo = [...TAGS_ON].map((n) => 'tag=' + encodeURIComponent(n)).join('&');
  try { location.hash = 'acervo:' + ACV_TIPO + (sufixo ? '&' + sufixo : ''); }
  catch (_) {}
  show('acervo');
}

/* Atividade: o que está acontecendo no disco neste instante.

   São duas medidas distintas do kernel, e a tela diz isso em voz alta: o
   contador por dispositivo é exato, o contador por processo é exato, mas o
   kernel não guarda em qual disco o byte do processo caiu. A coluna de volume
   vem do lsof e é palpite. Fingir que era cruzamento seria o tipo de número
   bonito que leva alguém a apagar a coisa errada. */
let IO_TIMER = null;
const RT = (b) => H(b) + '/s';

async function panelAtividade() {
  const t = el('p-atividade');
  if (!el('io-disks')) {
    t.innerHTML = `<div class="phead">
      <h2>Atividade agora</h2>
      <p>Quanto cada disco está lendo e gravando neste instante, e quais processos
         seus estão movendo bytes. Atualiza sozinho a cada 2 segundos.</p></div>
      <h3 id="io-head" class="eyebrow">medindo a primeira janela…</h3>
      <div id="io-disks"></div>
      <h3 class="eyebrow" style="margin-top:22px">Processos</h3>
      <div id="io-procs"></div>
      <p class="note" id="io-foot" style="margin-top:14px"></p>`;
  }
  let r;
  try {
    r = await api('/api/io');
  } catch (e) {
    el('io-head').textContent = 'erro: ' + e.message;
    return;
  }
  if (!r.warming) {
    el('io-head').textContent =
      `lendo ${RT(r.total_read)} · gravando ${RT(r.total_write)} `
      + `(janela de ${r.seconds.toFixed(1).replace('.', ',')} s)`;

    const mx = Math.max(1, ...r.disks.map((d) => d.read + d.write));
    el('io-disks').innerHTML = table(
      ['Disco', 'Volume', 'Lendo', 'Gravando', ''],
      r.disks.filter((d) => d.read || d.write || d.volumes.length).map((d) => [
        `<td class="mono">${esc(d.dev)}</td>`,
        `<td>${esc(d.volumes.join(', ')) || '<span class="iomut">não indexado</span>'}</td>`,
        tdn(RT(d.read)), tdn(RT(d.write)),
        `<td style="width:34%"><div class="bar-t"><div class="bar-f" style="width:${
          (100 * (d.read + d.write) / mx).toFixed(1)}%;background:var(--accent)"></div></div></td>`,
      ]));

    el('io-procs').innerHTML = r.procs.length ? table(
      ['PID', 'Processo', 'Lendo', 'Gravando', 'Volumes abertos'],
      r.procs.map((p) => [
        `<td class="mono">${p.pid}</td>`,
        `<td class="path"><span class="hi">${esc(p.comm.split('/').pop())}</span></td>`,
        tdn(RT(p.read)), tdn(RT(p.write)),
        `<td>${esc(p.volumes.join(', ')) || '<span class="iomut">—</span>'}</td>`,
      ])) : '<div class="empty">nenhum processo seu moveu bytes nesta janela</div>';

    el('io-foot').innerHTML =
      `<b>${N(r.denied)} processos de outro usuário</b> ficam de fora: o kernel só deixa
       ler o contador dos seus, e <code>sudo</code> incluiria o resto.
       A coluna <b>volumes abertos</b> vem dos arquivos que o processo tem abertos agora
       — é palpite de onde ele escreve, não prova de onde o byte caiu. Por isso a soma
       dos processos não fecha com a soma dos discos.`;
  }
  clearTimeout(IO_TIMER);
  if (!t.hidden) IO_TIMER = setTimeout(panelAtividade, 2000);
}

/* ---------- shell ---------- */
const PANELS = {
  resumo: panelResumo, atividade: panelAtividade, liberar: panelLiberar,
  arvores: panelArvores,
  duplicatas: panelDuplicatas, pastas: panelPastas, frio: panelFrio,
  busca: panelBusca, acervo: panelAcervo, tarefas: panelTarefas,
  historico: panelHistorico,
};
const loaded = {};

async function show(name) {
  document.querySelectorAll('#tabs button').forEach((b) => {
    b.setAttribute('aria-selected', String(b.dataset.p === name));
  });
  Object.keys(PANELS).forEach((k) => { el('p-' + k).hidden = (k !== name); });
  if (name !== 'tarefas') clearTimeout(JOB_TIMER);
  if (name !== 'atividade') clearTimeout(IO_TIMER);
  if (!loaded[name] || ['busca', 'tarefas', 'imagens', 'atividade'].includes(name)) {
    try {
      await PANELS[name]();
      loaded[name] = true;
    } catch (e) {
      el('p-' + name).innerHTML = `<div class="empty">erro: ${esc(e.message)}</div>`;
    }
  }
  // Preserva o sufixo quando é a mesma aba, senão abrir '#acervo:video'
  // reescreveria para '#acervo' antes de o painel ler a sub-aba.
  try {
    const atual = (location.hash || '').replace('#', '');
    if (atual.split(':')[0].split('&')[0] !== name) location.hash = name;
  } catch (_) {}
}

function tooltips() {
  const tip = el('tip');
  const hide = () => { tip.style.opacity = '0'; };
  document.addEventListener('mousemove', (ev) => {
    const t = ev.target.closest('[data-tip]');
    if (!t) { hide(); return; }
    tip.textContent = t.getAttribute('data-tip');
    tip.style.opacity = '1';
    const r = tip.getBoundingClientRect();
    let x = Math.min(Math.max(8, ev.clientX - r.width / 2), window.innerWidth - r.width - 8);
    let y = ev.clientY - r.height - 12;
    if (y < 8) y = ev.clientY + 16;
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
  });
  document.addEventListener('scroll', hide, { passive: true });
}

async function boot() {
  tooltips();
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[data-act-path]');
    if (b) { openActions(b.dataset.actPath); return; }
    if (e.target === sheet()) closeSheet();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (!el('viewer').hidden) { el('viewer').hidden = true; return; }
    if (!sheet().hidden) closeSheet();
  });
  el('viewer').addEventListener('click', (e) => {
    if (e.target === el('viewer')) el('viewer').hidden = true;
  });
  if (ACTIONS_ON) {
    try { ACTS = (await api('/api/actions')).actions || []; } catch (_) { ACTS = []; }
  }
  el('tabs').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-p]');
    if (b) show(b.dataset.p);
  });
  el('q').addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      show('busca');
    }, 250);
  });
  try {
    OV = await api('/api/overview');
  } catch (e) {
    el('sub').textContent = 'erro ao ler o índice: ' + e.message;
    el('p-resumo').innerHTML = `<div class="empty">${esc(e.message)}<br><br>
      Rode <code>diskmap analyze</code> e recarregue.</div>`;
    return;
  }
  const t = OV.totals;
  const scanned = OV.volumes.map((v) => v.scanned_at).filter(Boolean).sort().pop();
  el('sub').textContent = `${N(t.files)} arquivos · ${H(t.bytes)} · ${OV.volumes.length} volumes`
    + (scanned ? ` · índice de ${new Date(scanned * 1000).toLocaleDateString('pt-BR')}` : '')
    + (ACTIONS_ON ? ` · ações ligadas (${ACTS.length})` : ' · somente leitura');
  // '#acervo:video' é a aba 'acervo' com a sub-aba 'video'; o sufixo é lido
  // pelo painel, aqui só interessa a aba.
  const want = (location.hash || '').replace('#', '').split(':')[0].split('&')[0];
  show(PANELS[want] ? want : 'resumo');
}
boot();
