// Widget « Interroger mes réunions » — agent conversationnel RAG (OpenRAG).
//
// Bulle flottante bas-droite qui ouvre un panneau de chat en overlay,
// persistant et indépendant des onglets. Interroge la partition perso de
// l'utilisateur côté serveur (POST /api/rag/query) ; bouton « Indexer mes
// réunions » (POST /api/rag/ingest). Ne se révèle que si le RAG est
// configuré (GET /api/rag/status → configured), sinon reste inerte.

let _open = false;
let _busy = false;
const _history = [];        // [{role:'user'|'assistant', content}]
let _els = null;

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function renderMarkdown(text) {
  // marked.js est déjà chargé par l'app (cf legacy CR). Fallback <pre>.
  if (window.marked && typeof window.marked.parse === 'function') {
    try { return window.marked.parse(String(text || '')); } catch (e) { /* fallthrough */ }
  }
  return `<p>${esc(text).replace(/\n/g, '<br>')}</p>`;
}

// Icônes de source (cohérentes avec la liste « Mes réunions »).
const _IC = {
  youtube: '<svg width="13" height="13" viewBox="0 0 24 24" fill="#000091" aria-hidden="true" style="vertical-align:-2px"><path d="M23 7.5c-.3-1.5-1.4-2.6-2.9-2.9C17.3 4 12 4 12 4s-5.3 0-8.1.6C2.4 4.9 1.3 6 1 7.5.4 10.3.4 13.7 1 16.5c.3 1.5 1.4 2.6 2.9 2.9C6.7 20 12 20 12 20s5.3 0 8.1-.6c1.5-.3 2.6-1.4 2.9-2.9.6-2.8.6-6.2 0-9zM10 16V8l5.5 4L10 16z"/></svg>',
  mcr: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" aria-hidden="true" style="vertical-align:-2px"><rect x="3" y="3.5" width="18" height="13" rx="2" stroke="#000091" stroke-width="1.6"/><path d="M6 7.6h6M6 10.1h4.4" stroke="#000091" stroke-width="1.5" stroke-linecap="round"/><circle cx="17.6" cy="7" r="1.5" fill="#e1000f"/><circle cx="12" cy="15.1" r="2.3" fill="#000091"/><path d="M7.8 21.6c0-2.4 1.9-4 4.2-4s4.2 1.6 4.2 4z" fill="#000091"/></svg>',
  lasuite: '<svg width="13" height="13" viewBox="0 0 24 24" aria-hidden="true" style="vertical-align:-2px"><rect width="24" height="24" rx="6" fill="#000091"/><circle cx="9" cy="9.5" r="3" fill="#fff"/><circle cx="15.5" cy="15" r="3" fill="#e1000f"/></svg>',
  meeting: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#000091" stroke-width="1.7" stroke-linecap="round" aria-hidden="true" style="vertical-align:-2px"><circle cx="9" cy="8" r="2.4"/><path d="M3.5 19c0-3 2.4-5 5.5-5s5.5 2 5.5 5"/><circle cx="17" cy="9" r="1.8"/><path d="M15 19c0-2.4 1.6-4.2 4-4.2"/></svg>',
};
function _srcIcon(meta) {
  const st = ((meta && meta.source_type) || '').toLowerCase();
  const origin = ((meta && meta.origin) || '').toLowerCase();
  if (st.indexOf('youtube') >= 0) return _IC.youtube;
  if (origin === 'mcr_import') return _IC.mcr;
  if (st === 'lasuite_visio' || st === 'lasuite_transcript') return _IC.lasuite;
  return _IC.meeting;
}
function _stripSources(t) {
  // OpenRAG ajoute un bloc « **Sources :** [titre](url) » avec des liens
  // externes (statique OpenRAG) inopérants dans l'app → on le retire et on
  // affiche nos propres puces cliquables vers la fiche.
  if (!t) return t;
  return String(t)
    .replace(/\n?-{2,}\s*\n+\*\*\s*sources?\s*:?\s*\*\*[\s\S]*$/i, '')
    .replace(/\n+\*\*\s*sources?\s*:?\s*\*\*[\s\S]*$/i, '')
    .trim();
}

function _ensureStyles() {
  if (document.getElementById('rag-widget-style')) return;
  const st = document.createElement('style');
  st.id = 'rag-widget-style';
  st.textContent = `
    #rag-fab{position:fixed;right:1.1rem;bottom:1.1rem;z-index:9998;display:inline-flex;
      align-items:center;gap:.45rem;background:#000091;color:#fff;border:none;border-radius:999px;
      padding:.6rem .9rem;font:600 .9rem/1 Marianne,system-ui,sans-serif;cursor:pointer;
      box-shadow:0 4px 14px rgba(0,0,0,.25);}
    #rag-fab:hover{background:#1212a0;}
    #rag-fab svg{width:18px;height:18px;}
    #rag-panel{position:fixed;right:1.1rem;bottom:1.1rem;z-index:9999;width:min(420px,94vw);
      height:min(620px,82vh);background:#fff;border:1px solid #ddd;border-radius:12px;
      box-shadow:0 12px 40px rgba(0,0,0,.28);display:none;flex-direction:column;overflow:hidden;
      font-family:Marianne,system-ui,sans-serif;}
    #rag-panel.is-open{display:flex;}
    .rag-head{display:flex;align-items:center;gap:.5rem;padding:.6rem .8rem;background:#000091;color:#fff;}
    .rag-head .rag-title{font-weight:700;font-size:.95rem;flex:1;}
    .rag-head button{background:transparent;border:0;color:#fff;cursor:pointer;font-size:1.1rem;line-height:1;}
    .rag-sub{font-size:.72rem;color:#e9e9ff;opacity:.9;}
    .rag-body{flex:1;overflow-y:auto;padding:.7rem .8rem;background:#f7f7fb;}
    .rag-msg{margin:0 0 .55rem;display:flex;}
    .rag-msg.user{justify-content:flex-end;}
    .rag-bubble{max-width:85%;padding:.5rem .7rem;border-radius:10px;font-size:.86rem;line-height:1.4;}
    .rag-msg.user .rag-bubble{background:#000091;color:#fff;border-bottom-right-radius:3px;}
    .rag-msg.bot .rag-bubble{background:#fff;color:#1b1b35;border:1px solid #e6e6ef;border-bottom-left-radius:3px;}
    .rag-bubble pre{white-space:pre-wrap;font-family:inherit;margin:.3rem 0;}
    .rag-bubble p{margin:.25rem 0;} .rag-bubble ul{margin:.25rem 0 .25rem 1rem;padding:0;}
    .rag-bubble a{color:#000091;}
    .rag-sources{margin-top:.35rem;display:flex;flex-wrap:wrap;gap:.3rem;}
    .rag-src{font-size:.72rem;background:#eef;border:1px solid #ccd;border-radius:999px;padding:.05rem .5rem;cursor:pointer;color:#000091;}
    .rag-foot{border-top:1px solid #eee;padding:.55rem .7rem;background:#fff;}
    .rag-inputrow{display:flex;gap:.4rem;}
    .rag-inputrow textarea{flex:1;resize:none;border:1px solid #ccc;border-radius:8px;padding:.45rem .6rem;
      font:inherit;font-size:.86rem;max-height:90px;}
    .rag-send{background:#000091;color:#fff;border:0;border-radius:8px;padding:0 .8rem;cursor:pointer;font-weight:600;}
    .rag-send:disabled{opacity:.5;cursor:wait;}
    .rag-toolbar{display:flex;align-items:center;gap:.5rem;margin-bottom:.45rem;font-size:.74rem;color:#555;}
    .rag-ingest{background:#fff;border:1px solid #000091;color:#000091;border-radius:999px;padding:.15rem .6rem;cursor:pointer;font-size:.74rem;}
    .rag-ingest:disabled{opacity:.5;cursor:wait;}
    .rag-empty{color:#777;font-size:.82rem;text-align:center;padding:1rem .5rem;}
  `;
  document.head.appendChild(st);
}

function _scrollBottom() { if (_els) _els.body.scrollTop = _els.body.scrollHeight; }

function _addMsg(role, html, sources) {
  const wrap = document.createElement('div');
  wrap.className = 'rag-msg ' + (role === 'user' ? 'user' : 'bot');
  let inner = `<div class="rag-bubble">${html}`;
  if (sources && sources.length) {
    const seen = new Set();
    const chips = [];
    for (const s of sources) {
      const meta = (s && s.metadata) || {};
      // L'uaf_id (= user_audio_files.id, UUID à tirets) est l'id passé à
      // showFileDetail. On le cherche dans le metadata enrichi, puis sur la
      // source elle-même (file_id = id d'indexation), puis dans l'URL. On
      // normalise les underscores → tirets (sanitization OpenRAG du path).
      let fid = meta.uaf_id || meta.file_id || s.file_id || s.uaf_id || '';
      const url = s.file_url || s.chunk_url || meta.link || '';
      if (!fid && url) {
        const m = String(url).match(/\/file\/([^/?#]+)/);
        if (m) fid = m[1];
      }
      fid = fid ? String(fid).replace(/_/g, '-') : '';
      const key = fid || (meta.meeting_title || '') + url;
      if (seen.has(key)) continue;
      seen.add(key);
      const title = esc(meta.meeting_title || s.title || meta.original_title || 'réunion');
      chips.push(`<span class="rag-src" data-rag-open="${esc(fid)}" title="Ouvrir : ${title}">${_srcIcon(meta)} ${title.slice(0, 34)}</span>`);
      if (chips.length >= 6) break;
    }
    if (chips.length) inner += `<div class="rag-sources">${chips.join('')}</div>`;
  }
  inner += '</div>';
  wrap.innerHTML = inner;
  _els.body.appendChild(wrap);
  _scrollBottom();
  return wrap;
}

async function _refreshStatus() {
  try {
    const r = await fetch('/api/rag/status', { credentials: 'same-origin' });
    const d = await r.json().catch(() => ({}));
    if (_els && _els.count) {
      const n = d.indexed_count || 0;
      _els.count.textContent = n > 0 ? `${n} réunion(s) indexée(s)` : 'aucune réunion indexée';
    }
    return d;
  } catch (e) { return {}; }
}

async function _send() {
  if (_busy) return;
  const q = (_els.input.value || '').trim();
  if (!q) return;
  _els.input.value = '';
  _addMsg('user', esc(q));
  _history.push({ role: 'user', content: q });
  _busy = true; _els.send.disabled = true;
  const thinking = _addMsg('bot', '<em>…</em>');
  try {
    const r = await fetch('/api/rag/query', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: q, history: _history.slice(-8) }),
    });
    const d = await r.json().catch(() => ({}));
    thinking.remove();
    if (!r.ok || d.error) {
      const msg = d.error === 'rag_not_configured'
        ? "Le service RAG n'est pas configuré."
        : "Désolé, je n'ai pas pu répondre (service indisponible).";
      _addMsg('bot', `<em>${esc(msg)}</em>`);
    } else {
      const answer = d.answer || '(réponse vide)';
      _addMsg('bot', renderMarkdown(_stripSources(answer)), d.sources);
      _history.push({ role: 'assistant', content: answer });
    }
  } catch (e) {
    thinking.remove();
    _addMsg('bot', `<em>Erreur réseau.</em>`);
  } finally {
    _busy = false; _els.send.disabled = false; _els.input.focus();
  }
}

async function _ingest() {
  if (_busy) return;
  _busy = true; _els.ingest.disabled = true;
  const prev = _els.ingest.textContent;
  _els.ingest.textContent = 'Indexation…';
  try {
    const r = await fetch('/api/rag/ingest', { method: 'POST', credentials: 'same-origin' });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.error) {
      _addMsg('bot', `<em>Indexation impossible (${esc(d.error || r.status)}).</em>`);
    } else {
      _addMsg('bot', `<em>Indexation lancée : ${d.queued || 0} réunion(s) envoyée(s)`
        + `${d.skipped ? `, ${d.skipped} déjà indexée(s)/sans texte` : ''}`
        + `${d.failed ? `, ${d.failed} échec(s)` : ''}. Le traitement se termine en arrière-plan.</em>`);
    }
    setTimeout(_refreshStatus, 1500);
  } catch (e) {
    _addMsg('bot', `<em>Erreur réseau pendant l'indexation.</em>`);
  } finally {
    _busy = false; _els.ingest.disabled = false; _els.ingest.textContent = prev;
  }
}

function _toggle(forceOpen) {
  _open = forceOpen != null ? forceOpen : !_open;
  _els.panel.classList.toggle('is-open', _open);
  _els.fab.style.display = _open ? 'none' : 'inline-flex';
  if (_open) {
    _refreshStatus();
    if (!_els.body.querySelector('.rag-msg') && !_els.body.querySelector('.rag-empty')) {
      const greet = document.createElement('div');
      greet.className = 'rag-empty';
      greet.innerHTML = "Posez une question sur vos réunions.<br>Ex. « Quelles décisions ont été prises ? »<br><small>Si aucune réunion n'est indexée, cliquez « Indexer mes réunions ».</small>";
      _els.body.appendChild(greet);
    }
    setTimeout(() => _els.input && _els.input.focus(), 50);
  }
}

function _build() {
  _ensureStyles();
  const fab = document.createElement('button');
  fab.id = 'rag-fab';
  fab.type = 'button';
  fab.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg> Interroger mes réunions`;

  const panel = document.createElement('div');
  panel.id = 'rag-panel';
  panel.innerHTML = `
    <div class="rag-head">
      <span class="rag-title">Interroger mes réunions</span>
      <button type="button" data-rag-close aria-label="Fermer">×</button>
    </div>
    <div class="rag-body" data-rag-body></div>
    <div class="rag-foot">
      <div class="rag-toolbar">
        <span data-rag-count class="rag-sub" style="color:#555;flex:1;">…</span>
        <button type="button" class="rag-ingest" data-rag-ingest title="Indexer vos réunions dans le moteur de recherche IA">⟳ Indexer mes réunions</button>
      </div>
      <div class="rag-inputrow">
        <textarea data-rag-input rows="1" placeholder="Votre question…"></textarea>
        <button type="button" class="rag-send" data-rag-send>Envoyer</button>
      </div>
    </div>`;
  document.body.appendChild(fab);
  document.body.appendChild(panel);

  _els = {
    fab, panel,
    body: panel.querySelector('[data-rag-body]'),
    input: panel.querySelector('[data-rag-input]'),
    send: panel.querySelector('[data-rag-send]'),
    ingest: panel.querySelector('[data-rag-ingest]'),
    count: panel.querySelector('[data-rag-count]'),
  };

  fab.addEventListener('click', () => _toggle(true));
  panel.querySelector('[data-rag-close]').addEventListener('click', () => _toggle(false));
  _els.send.addEventListener('click', _send);
  _els.ingest.addEventListener('click', _ingest);
  _els.input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); _send(); }
  });
  // Clic sur une source → ouvre la fiche réunion (best-effort).
  _els.body.addEventListener('click', (e) => {
    const c = e.target.closest && e.target.closest('[data-rag-open]');
    if (!c) return;
    const fid = c.getAttribute('data-rag-open');
    if (fid && typeof window.showFileDetail === 'function') {
      _toggle(false);
      try { window.showFileDetail(fid); } catch (_) {}
    }
  });
}

export async function initChatWidget() {
  // Ne révèle le widget que si le RAG est configuré côté serveur.
  let status = {};
  try {
    const r = await fetch('/api/rag/status', { credentials: 'same-origin' });
    status = await r.json().catch(() => ({}));
  } catch (e) { return; }
  if (!status || status.configured !== true) return;  // inerte si non configuré
  if (document.getElementById('rag-fab')) return;       // déjà monté
  _build();
}
