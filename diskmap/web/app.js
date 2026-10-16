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
const TOKEN = window.DISKMAP_TOKEN || null;
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

const post = async (path, body) => {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Diskmap-Token': TOKEN || '' },
    body: JSON.stringify(body),
  });
  const j = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (!r.ok || j.error) throw new Error(j.error || `HTTP ${r.status}`);
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
  const blocked = !w.allowed || (w.refusals || []).length;
  const facts = [`${esc(w.fs)}`,
    w.rotational === true ? 'disco mecânico' : w.rotational === false ? 'memória flash' : null,
    w.snapshots ? `${N(w.snapshots)} snapshots` : null,
    w.read_only ? 'montado somente-leitura' : null].filter(Boolean).join(' · ');

  sheet().innerHTML = `<div class="sheet-in" role="dialog" aria-modal="true">
    <h3>${esc(a.name)}</h3>
    <div class="sheet-path">${esc(path)}</div>
    <div class="warnbox ${cls}"><b>${esc(title)}.</b> ${esc(w.why)}</div>
    <p class="note">${esc(w.label)} · ${facts}${
      w.size != null ? ` · ${H(w.size)}` : ''}</p>
    ${(w.refusals || []).length
      ? `<div class="warnbox crit">${w.refusals.map(esc).join('<br>')}</div>` : ''}
    ${blocked ? '' : `<div class="warnbox crit"><b>Isto não tem undo.</b>
      Sobrescreve cada byte com zero, confere a leitura e apaga o arquivo.
      A aba Histórico vai registrar, mas não vai conseguir desfazer.</div>
      <p class="note">Para confirmar, digite <b>ZERAR</b>:</p>
      <input id="wipe-ok" class="searchbox" autocomplete="off" spellcheck="false">`}
    <div class="sheet-foot">
      <button class="btn" data-close>Cancelar</button>
      ${blocked ? '' : '<button class="btn danger" data-go disabled>Zerar e apagar</button>'}
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
let IMG_ROWS = [];        // what the grid currently shows, for the viewer
let IMG_MODE = 'galeria'; // galeria | repetidas

const thumb = (id) => `/thumb/${id}.jpg`;
const imgPath = (r) => (r.relpath ? `${r.label}/${r.relpath}/${r.name}` : `${r.label}/${r.name}`);
const imgAbs = (r) => (r.mount ? joinp(r.mount.replace(/\/$/, ''), r.relpath, r.name) : null);

function shot(r) {
  const px = (r.width && r.height) ? `${r.width}×${r.height}` : '';
  return `<figure class="sh" data-img="${r.id}">
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
  const d = await api('/api/images?' + p);
  IMG_ROWS = d.images;
  el('imgcount').textContent =
    `${N(d.total)} imagens · ${H(d.bytes)}` + (d.total > 120 ? ` · página ${IMG_PAGE + 1}` : '');
  const g = el('grid');
  g.innerHTML = d.images.length ? d.images.map(shot).join('')
    : '<div class="empty">nenhuma imagem com esse filtro</div>';
  el('imgprev').hidden = IMG_PAGE === 0;
  el('imgnext').hidden = (IMG_PAGE + 1) * 120 >= d.total;
}

async function renderClusters() {
  const d = await api('/api/img-groups?limit=25');
  const box = el('clusters');
  el('imgcount').textContent =
    `${N(d.count)} grupos parecidos · ${H(d.wasted)} se mantiver só a maior de cada`;
  if (!d.groups.length) {
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

async function panelImagens() {
  const target = el('p-imagens');
  if (!IMG_OV) {
    try { IMG_OV = await api('/api/img-overview'); } catch (_) { IMG_OV = null; }
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

  h += `<div id="grid" class="grid"${IMG_MODE !== 'galeria' ? ' hidden' : ''}></div>
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
    b.addEventListener('click', () => { IMG_MODE = b.dataset.mode; panelImagens(); });
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
      São reconstruíveis — contam como espaço, não como acervo.</p>`;
  }
  el('p-resumo').innerHTML = h;
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

async function panelArvores() {
  const rows = await api('/api/overlap?limit=40');
  let h = `<div class="phead"><p class="eyebrow">causa raiz</p><h2>Árvores clonadas</h2>
    <p>Pares de diretórios que guardam o mesmo conteúdo com nomes diferentes.
    A coluna <em>igual</em> é quanto os dois têm de arquivo idêntico; <em>sobrep.</em>
    é essa fatia sobre a menor das duas árvores. Nenhum buscador de duplicata
    reporta isso como uma coisa só.</p></div>`;
  h += table(['Árvore A', 'Árvore B', 'Igual', 'Sobrep.', 'Arq.'],
    rows.map((r) => [tdp(r.a), tdp(r.b), tdn(H(r.shared)),
      tdn(`${Number(r.pct).toFixed(0)}%`), tdn(N(r.files))]));
  const arch = await api('/api/archives?limit=30');
  h += `<h3 style="margin:28px 0 12px">Compactado com a pasta extraída ao lado</h3>`;
  h += table(['Arquivo', 'Volume', 'Tamanho', ''],
    arch.map((r) => [tdp(r.relpath), `<td>${esc(r.label)}</td>`, tdn(H(r.bytes)),
      actCell(absRel(r.label, r.relpath))]));
  el('p-arvores').innerHTML = h;
}

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
  h += table(['Arquivo', 'Tam.', 'Cópias', 'Redundante', 'Estado', 'Onde'],
    rows.map((r) => [
      tdp(r.name), tdn(H(r.size)), tdn(r.copies + '×'), tdn(H(r.wasted)),
      `<td>${VER_TAG[r.verified] || esc(r.verified)}</td>`,
      `<td class="path">${r.paths.slice(0, 4).map(esc).join('<br>')}${
        r.paths.length > 4 ? `<br><span style="color:var(--fg-3)">+${r.paths.length - 4} outro(s)</span>` : ''}</td>`,
    ]));
  el('p-duplicatas').innerHTML = h;
  const s2 = el('dupfilter');
  if (s2) { s2.value = f; s2.addEventListener('change', panelDuplicatas); }
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
async function panelBusca(term) {
  const t = (term !== undefined ? term : (el('q').value || '')).trim();
  const target = el('p-busca');
  if (t.length < 2) {
    target.innerHTML = `<div class="phead"><p class="eyebrow">offline</p><h2>Onde está…</h2>
      <p>Digite ao menos dois caracteres. A busca responde pelo índice, então acha
      arquivo em disco desconectado — e te diz em qual volume plugar.</p></div>`;
    return;
  }
  const res = await api('/api/search?q=' + encodeURIComponent(t) + '&limit=200');
  const mounted = {}; OV.volumes.forEach((v) => { mounted[v.label] = !!v.mount; });
  let h = `<div class="phead"><p class="eyebrow">offline</p>
    <h2>“${esc(t)}”</h2><p>${N(res.files.length)} arquivos e ${N(res.dirs.length)} pastas.</p></div>`;
  if (res.dirs.length) {
    h += '<h3 style="margin:0 0 12px">Pastas</h3>';
    h += table(['Pasta', 'Tamanho', 'Arquivos'], res.dirs.map((r) => [
      tdp(`${r.label}/${r.relpath}`), tdn(H(r.bytes)), tdn(N(r.files))]));
  }
  if (res.files.length) {
    h += '<h3 style="margin:28px 0 12px">Arquivos</h3>';
    h += table(['Arquivo', 'Tamanho', 'Volume', ''], res.files.map((r) => [
      tdp(fullPath(r)), tdn(H(r.size)),
      `<td>${esc(r.label)} ${mounted[r.label] ? '' : '<span class="tag">plugar</span>'}</td>`,
      mounted[r.label] ? actCell(abs(r)) : '<td class="act"></td>']));
  }
  target.innerHTML = h;
}

/* ---------- shell ---------- */
const PANELS = {
  resumo: panelResumo, liberar: panelLiberar, arvores: panelArvores,
  duplicatas: panelDuplicatas, pastas: panelPastas, frio: panelFrio,
  busca: panelBusca, imagens: panelImagens, tarefas: panelTarefas,
  historico: panelHistorico,
};
const loaded = {};

async function show(name) {
  document.querySelectorAll('#tabs button').forEach((b) => {
    b.setAttribute('aria-selected', String(b.dataset.p === name));
  });
  Object.keys(PANELS).forEach((k) => { el('p-' + k).hidden = (k !== name); });
  if (name !== 'tarefas') clearTimeout(JOB_TIMER);
  if (!loaded[name] || name === 'busca' || name === 'tarefas' || name === 'imagens') {
    try {
      await PANELS[name]();
      loaded[name] = true;
    } catch (e) {
      el('p-' + name).innerHTML = `<div class="empty">erro: ${esc(e.message)}</div>`;
    }
  }
  try { location.hash = name; } catch (_) {}
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
  const want = (location.hash || '').replace('#', '');
  show(PANELS[want] ? want : 'resumo');
}
boot();
