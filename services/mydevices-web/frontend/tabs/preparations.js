// Onglet "Préparation de réunion" — refonte DSFR (PR-UX-Preparations).
//
// Migration depuis frontend/legacy.js : toutes les fonctions brief/amend
// (loadBriefs, showBriefDetail, renderBriefBody, fillAmendForm,
// buildAmendBriefJson, saveAmendBrief, link/detach audio, série, banner > 90j,
// delete/restore/permanently) sont désormais portées par ce module.
//
// Contrat :
//   - export `mount(container, ctx)` : pose la délégation d'événements
//     (data-action), restaure l'état des sections collapsibles, charge la
//     liste des briefs. Idempotent.
//   - export `unmount(container)` : retire le listener.
//
// Compat legacy :
//   - quelques fonctions sont republiées sur `window.*` (loadBriefs,
//     showBriefDetail, restoreBrief, deleteBriefPermanently) parce que
//     `legacy.js::setupTabs()` et la liste "corbeille" les appellent
//     directement. Tant que la corbeille n'est pas migrée à son tour,
//     on garde ces handles globaux.
//
// API consommée : /api/preparations/* (cf. tests/regression/test_mydevices_web_modules.py).

import { renderBriefDetailSkeleton } from '../lib/skeleton.js';
import * as detailCache from '../lib/detail-cache.js';

const PANEL_ID = 'panel-brief';

// ─── État courant ─────────────────────────────────────────────────────────
let _briefDetailId = null;
let _amendBriefOriginal = {};
let _delegationBound = false;

// ─── Helpers ──────────────────────────────────────────────────────────────
function _esc(v) {
  return (v || '').toString().replace(/[&<>"']/g, (s) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[s]);
}

function _toast(msg, kind) {
  try {
    if (typeof window.showToast === 'function') {
      window.showToast(msg, kind);
    } else {
      // eslint-disable-next-line no-console
      console.log('[toast]', kind || 'info', msg);
    }
  } catch (e) { /* no-op */ }
}

// ─── Liste briefs ─────────────────────────────────────────────────────────
async function loadBriefs() {
  const container = document.getElementById('brief-list');
  if (!container) return;
  try {
    const resp = await fetch('/api/preparations?with_counts=true');
    const data = await resp.json();
    const briefs = (data && (data.preparations || data.briefs)) || [];
    try { renderOlderThan90dBanner(data && data.older_than_90d_unlinked_count); }
    catch (e) { /* non-fatal */ }
    if (briefs.length === 0) {
      container.innerHTML = `<p style="color:#94a3b8;">
        Aucun brief pour le moment.
        <a href="/meeting-prep/new" class="fr-link">Préparation de réunion ?</a>
      </p>`;
      return;
    }
    // DSFR fr-table compact + actions inline par ligne (data-action delegation).
    const rows = briefs.map(b => {
      const date = (b.created_at || '').slice(0, 16).replace('T', ' ');
      const title = b.title || b.subject || '(sans titre)';
      const tEsc = _esc(title);
      return `
        <tr data-brief-id="${_esc(b.id)}" data-brief-title="${tEsc}">
          <td>
            <a href="#" class="fr-link" data-action="open-brief" data-brief-id="${_esc(b.id)}">${tEsc}</a>
          </td>
          <td style="color:#94a3b8;font-size:0.78rem;white-space:nowrap;">${_esc(date)}</td>
          <td style="text-align:right;white-space:nowrap;">
            <button type="button"
                    class="fr-btn fr-btn--sm fr-btn--secondary"
                    data-action="open-brief" data-brief-id="${_esc(b.id)}">
              Ouvrir
            </button>
            <button type="button"
                    class="fr-btn fr-btn--sm fr-btn--tertiary-no-outline"
                    data-action="delete-brief" data-brief-id="${_esc(b.id)}" data-brief-title="${tEsc}"
                    aria-label="Supprimer">
              Supprimer
            </button>
          </td>
        </tr>`;
    }).join('');
    container.innerHTML = `
      <div class="fr-table fr-table--bordered fr-table--no-caption" data-preparations-table>
        <table>
          <caption class="fr-sr-only">Liste de vos préparations de réunion</caption>
          <thead>
            <tr>
              <th scope="col">Titre</th>
              <th scope="col">Créé le</th>
              <th scope="col" style="text-align:right;">Actions</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    `;
  } catch (e) {
    container.innerHTML = `<p style="color:#b91c1c;">Erreur chargement briefs.</p>`;
  }
}

function showBriefList() {
  _briefDetailId = null;
  const list = document.getElementById('brief-list-view');
  const detail = document.getElementById('brief-detail-view');
  if (list) list.style.display = '';
  if (detail) detail.style.display = 'none';
  loadBriefs();
}

// ─── Rendu détail (contenu structuré) ─────────────────────────────────────
function renderBriefBody(brief_json) {
  const bj = brief_json || {};
  const esc = _esc;
  const parts = [];
  const objective = (bj.objective_reformulated || '').trim();
  const context = (bj.context_recap || '').trim();
  if (objective || context) {
    let html = '<section class="brief-section"><h3 class="brief-section-title"><span class="brief-section-icon">🎯</span>Objectif &amp; contexte</h3>';
    if (objective) html += `<p class="brief-objective">${esc(objective)}</p>`;
    if (context) html += `<p class="brief-context" style="margin-top:0.5rem;">${esc(context)}</p>`;
    html += '</section>';
    parts.push(html);
  }
  const agenda = Array.isArray(bj.agenda) ? bj.agenda : [];
  if (agenda.length) {
    let html = '<section class="brief-section"><h3 class="brief-section-title"><span class="brief-section-icon">📋</span>Ordre du jour</h3><ol class="brief-agenda">';
    agenda.forEach((it) => {
      if (!it || typeof it !== 'object') return;
      const title = esc(it.title || '(sans titre)');
      const dur = it.duration_minutes ? `<span class="brief-agenda-duration">${parseInt(it.duration_minutes, 10) || 0} min</span>` : '';
      const objv = (it.objective || '').trim();
      const kqs = Array.isArray(it.key_questions) ? it.key_questions.filter((q) => (q || '').trim()) : [];
      let inner = `<div class="brief-agenda-title">${title}${dur}</div>`;
      if (objv) inner += `<div class="brief-agenda-objective">${esc(objv)}</div>`;
      if (kqs.length) {
        inner += '<ul class="brief-agenda-questions">';
        kqs.forEach((q) => { inner += `<li>${esc(q)}</li>`; });
        inner += '</ul>';
      }
      html += `<li class="brief-agenda-item">${inner}</li>`;
    });
    html += '</ol></section>';
    parts.push(html);
  }
  const participants = Array.isArray(bj.participants_notes) ? bj.participants_notes : [];
  if (participants.length) {
    let html = '<section class="brief-section"><h3 class="brief-section-title"><span class="brief-section-icon">👥</span>Participants</h3><div class="brief-participants">';
    participants.forEach((p) => {
      if (!p || typeof p !== 'object') return;
      const name = esc(p.name || '');
      const note = esc(p.note || '');
      if (!name && !note) return;
      html += `<div class="brief-participant"><div class="brief-participant-name">${name || '—'}</div><div class="brief-participant-note">${note}</div></div>`;
    });
    html += '</div></section>';
    parts.push(html);
  }
  const threads = Array.isArray(bj.open_threads) ? bj.open_threads : [];
  if (threads.length) {
    let html = '<section class="brief-section"><h3 class="brief-section-title"><span class="brief-section-icon">🧵</span>Points en suspens</h3><ul class="brief-list brief-list-threads">';
    threads.forEach((t) => {
      if (!t || typeof t !== 'object') return;
      const item = (t.item || '').trim();
      if (!item) return;
      const src = (t.source || '').trim();
      html += `<li>${esc(item)}${src ? `<span class="brief-thread-source">↳ ${esc(src)}</span>` : ''}</li>`;
    });
    html += '</ul></section>';
    parts.push(html);
  }
  const openingQs = Array.isArray(bj.opening_questions) ? bj.opening_questions.filter((q) => (q || '').trim()) : [];
  if (openingQs.length) {
    let html = '<section class="brief-section"><h3 class="brief-section-title"><span class="brief-section-icon">💬</span>Questions d\'ouverture</h3><ul class="brief-list brief-list-questions">';
    openingQs.forEach((q) => { html += `<li>${esc(q)}</li>`; });
    html += '</ul></section>';
    parts.push(html);
  }
  const risks = Array.isArray(bj.risk_points) ? bj.risk_points.filter((q) => (q || '').trim()) : [];
  if (risks.length) {
    let html = '<section class="brief-section"><h3 class="brief-section-title"><span class="brief-section-icon">⚠️</span>Points de vigilance</h3><ul class="brief-list brief-list-risks">';
    risks.forEach((q) => { html += `<li>${esc(q)}</li>`; });
    html += '</ul></section>';
    parts.push(html);
  }
  const checklist = Array.isArray(bj.preparation_checklist) ? bj.preparation_checklist.filter((q) => (q || '').trim()) : [];
  if (checklist.length) {
    let html = '<section class="brief-section"><h3 class="brief-section-title"><span class="brief-section-icon">✅</span>À faire avant la réunion</h3><ul class="brief-list brief-list-checklist">';
    checklist.forEach((q) => { html += `<li>${esc(q)}</li>`; });
    html += '</ul></section>';
    parts.push(html);
  }
  if (!parts.length) return '<p class="brief-empty">Aucun contenu structuré dans ce brief.</p>';
  return parts.join('');
}

// Applique payload brief sur le DOM. Factored out pour pouvoir l'appeler
// depuis le cache (synchrone) ET depuis le refetch silencieux (async).
function _applyBriefPayload(briefId, d) {
  const titleEl = document.getElementById('brief-detail-title');
  const metaEl = document.getElementById('brief-detail-meta');
  const bodyEl = document.getElementById('brief-detail-body');
  const b = d.preparation || d.brief || {};
  if (titleEl) titleEl.textContent = b.title || b.subject || '(sans titre)';
  const created = (b.created_at || '').slice(0, 16).replace('T', ' ');
  if (metaEl) metaEl.textContent = `Créé le ${created} · rôle: ${b.role || '—'} · durée: ${b.duration_minutes || '—'} min`;
  const content = b.content || b.brief_json || {};
  if (bodyEl) bodyEl.innerHTML = renderBriefBody(content);
  fillAmendForm(content);
  try {
    const btn = document.getElementById('brief-detail-prepare-next');
    if (btn) {
      btn.href = `/meeting-prep/new?series_parent_id=${encodeURIComponent(briefId)}`;
      btn.style.display = '';
    }
    const linkBtn = document.getElementById('brief-detail-link-audio-btn');
    if (linkBtn) linkBtn.style.display = '';
  } catch (e) {}
}

async function showBriefDetail(briefId) {
  _briefDetailId = briefId;
  const listView = document.getElementById('brief-list-view');
  const detailView = document.getElementById('brief-detail-view');
  if (listView) listView.style.display = 'none';
  if (detailView) detailView.style.display = '';
  const titleEl = document.getElementById('brief-detail-title');
  const metaEl = document.getElementById('brief-detail-meta');
  const bodyEl = document.getElementById('brief-detail-body');
  const amendPane = document.getElementById('brief-amend-pane');
  if (amendPane) amendPane.style.display = 'none';

  // Cache hit ? On affiche tout de suite + refetch silencieux derrière.
  const cached = detailCache.get('brief', briefId);
  if (cached) {
    _applyBriefPayload(briefId, cached);
    try { loadBriefAudioFiles(briefId); } catch (e) {}
    try { loadBriefSeries(briefId); } catch (e) {}
    // Refetch silencieux — diff JSON pour éviter re-render si identique.
    (async () => {
      try {
        const r = await fetch(`/api/preparations/${briefId}`);
        if (!r.ok) return;
        const d = await r.json();
        const prev = JSON.stringify(cached || {});
        const next = JSON.stringify(d || {});
        if (prev !== next) {
          detailCache.put('brief', briefId, d);
          // Ne réécrit que si l'utilisateur regarde encore CE brief (n'a pas
          // navigué ailleurs entre-temps).
          if (_briefDetailId === briefId) _applyBriefPayload(briefId, d);
        }
      } catch (e) { /* silencieux */ }
    })();
    return;
  }

  // Cache miss → skeleton immédiat + fetch.
  if (titleEl) titleEl.textContent = ' ';
  if (metaEl) metaEl.textContent = '';
  if (bodyEl) bodyEl.innerHTML = renderBriefDetailSkeleton();
  try {
    const r = await fetch(`/api/preparations/${briefId}`);
    if (!r.ok) throw new Error('fetch failed');
    const d = await r.json();
    detailCache.put('brief', briefId, d);
    // Garde-fou : si l'utilisateur a déjà cliqué ailleurs entre-temps, on
    // n'écrase pas la nouvelle vue avec une ancienne réponse en vol.
    if (_briefDetailId !== briefId) return;
    _applyBriefPayload(briefId, d);
    try { loadBriefAudioFiles(briefId); } catch (e) {}
    try { loadBriefSeries(briefId); } catch (e) {}
  } catch (e) {
    if (titleEl) titleEl.textContent = 'Erreur';
    if (bodyEl) bodyEl.innerHTML = '<p style="color:#b91c1c;">Erreur de chargement du brief.</p>';
  }
}

// ─── Audio liés / chaîne série ────────────────────────────────────────────
async function loadBriefAudioFiles(briefId) {
  const box = document.getElementById('brief-detail-linked-audios');
  if (!box) return;
  box.innerHTML = '<em style="color:#94a3b8;">Chargement des fichiers audio…</em>';
  try {
    const r = await fetch(`/api/preparations/${briefId}/audio-files`);
    if (!r.ok) throw new Error('fetch_failed');
    const d = await r.json();
    const items = (d && d.audio_files) || [];
    if (!items.length) {
      box.innerHTML = '<p style="color:#94a3b8;">Aucun audio lié.</p>';
      return;
    }
    box.innerHTML = '<strong>Fichier(s) audio lié(s) :</strong><ul style="margin:0.3rem 0 0 1.2rem;">' +
      items.map(a => {
        const label = _esc(a.suggested_filename || a.original_filename || a.id);
        const meta = (a.created_at || '').slice(0, 16).replace('T', ' ');
        return `<li><a href="#" class="fr-link" data-action="goto-transfers">${label}</a>` +
          ` <span style="color:#94a3b8;">${_esc(meta)}</span>` +
          ` <button class="fr-btn fr-btn--sm fr-btn--tertiary-no-outline"` +
          ` data-action="detach-audio" data-audio-id="${_esc(a.id)}">Détacher</button></li>`;
      }).join('') + '</ul>';
  } catch (e) {
    box.innerHTML = '<p style="color:#b91c1c;">Erreur chargement audios.</p>';
  }
}

async function loadBriefSeries(briefId) {
  const box = document.getElementById('brief-detail-series-chain');
  if (!box) return;
  box.innerHTML = '';
  try {
    const r = await fetch(`/api/preparations/${briefId}/series`);
    if (!r.ok) return;
    const d = await r.json();
    const chain = (d && d.series) || [];
    if (chain.length < 2) return;
    box.innerHTML = '<strong>Cette série :</strong><ol style="margin:0.3rem 0 0 1.2rem;">' +
      chain.map(b => {
        const t = _esc(b.title || b.subject || '(sans titre)');
        const isCurrent = String(b.id) === String(briefId);
        if (isCurrent) return `<li><strong>${t}</strong> (brief courant)</li>`;
        return `<li><a href="#" class="fr-link" data-action="open-brief" data-brief-id="${_esc(b.id)}">${t}</a></li>`;
      }).join('') + '</ol>';
  } catch (e) { /* silencieux */ }
}

async function detachAudioFromBrief(audioId) {
  if (!confirm('Détacher ce fichier du brief ?')) return;
  try {
    const r = await fetch(`/api/preparations/unlink-audio`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file_id: audioId }),
    });
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || 'detach_failed');
    if (_briefDetailId) detailCache.invalidate('brief', _briefDetailId);
    _toast('Audio détaché.', 'success');
    if (_briefDetailId) loadBriefAudioFiles(_briefDetailId);
  } catch (e) {
    _toast('Détachement échoué.', 'error');
  }
}

async function linkAudioToBriefPrompt() {
  if (!_briefDetailId) return;
  const audioId = prompt(
    'ID du fichier audio à lier au brief courant ' +
    "(visible dans l'onglet « Mes fichiers » → détail audio) :",
    ''
  );
  if (!audioId) return;
  try {
    const r = await fetch(`/api/preparations/${encodeURIComponent(_briefDetailId)}/link-audio`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file_id: audioId.trim() }),
    });
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || 'link_failed');
    detailCache.invalidate('brief', _briefDetailId);
    _toast('Audio lié au brief.', 'success');
    loadBriefAudioFiles(_briefDetailId);
  } catch (e) {
    _toast('Lien échoué : ' + (e.message || e), 'error');
  }
}

// ─── Banner > 90j ─────────────────────────────────────────────────────────
function renderOlderThan90dBanner(count) {
  const banner = document.getElementById('older-than-90d-banner');
  const msg = document.getElementById('older-than-90d-msg');
  if (!banner) return;
  let dismissed = false;
  try { dismissed = sessionStorage.getItem('older-than-90d-dismissed') === '1'; } catch (e) {}
  const n = Number(count || 0);
  if (n > 0 && !dismissed) {
    if (msg) msg.textContent = `Vous avez ${n} brief(s) ancien(s) (> 90 jours) sans audio lié.`;
    banner.style.display = '';
  } else {
    banner.style.display = 'none';
  }
}

function dismissOlderThan90dBanner() {
  try { sessionStorage.setItem('older-than-90d-dismissed', '1'); } catch (e) {}
  const banner = document.getElementById('older-than-90d-banner');
  if (banner) banner.style.display = 'none';
}

async function trashAllOlderThan90d() {
  // Endpoint serveur pas encore disponible — feedback explicite.
  _toast('Action non encore disponible — endpoint serveur manquant.', 'error');
}

// ─── Renommer ─────────────────────────────────────────────────────────────
async function renameBriefPrompt() {
  if (!_briefDetailId) return;
  const titleEl = document.getElementById('brief-detail-title');
  const current = titleEl ? (titleEl.textContent || '') : '';
  const next = prompt('Nouveau titre du brief (max 120 caractères) :', current);
  if (next == null) return;
  const trimmed = next.trim();
  if (!trimmed) return;
  try {
    const r = await fetch(`/api/preparations/${_briefDetailId}/rename`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: trimmed }),
    });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || 'rename_failed');
    if (titleEl) titleEl.textContent = d.title || trimmed;
    detailCache.invalidate('brief', _briefDetailId);
    _toast('Brief renommé.', 'success');
  } catch (e) { _toast('Renommage échoué.', 'error'); }
}

// ─── Édition manuelle (amend) ─────────────────────────────────────────────
function toggleAmendBrief() {
  const pane = document.getElementById('brief-amend-pane');
  if (!pane) return;
  const opening = pane.style.display === 'none';
  pane.style.display = opening ? '' : 'none';
  if (opening) restoreAmendSectionsState();
}

function _amendInputRow(value, placeholder) {
  const row = document.createElement('div');
  row.className = 'amend-input-row';
  row.style.cssText = 'display:flex;gap:0.3rem;align-items:center;';
  const inp = document.createElement('input');
  inp.type = 'text';
  inp.value = value || '';
  inp.placeholder = placeholder || '';
  inp.style.cssText = 'flex:1;border:1px solid #cbd5e1;border-radius:0.3rem;padding:0.3rem 0.4rem;font-size:0.85rem;';
  const rm = document.createElement('button');
  rm.type = 'button';
  rm.className = 'fr-btn fr-btn--sm fr-btn--tertiary-no-outline';
  rm.textContent = '×';
  rm.setAttribute('aria-label', 'Supprimer');
  rm.onclick = () => row.remove();
  row.appendChild(inp);
  row.appendChild(rm);
  return row;
}

function _amendCard() {
  const card = document.createElement('div');
  card.className = 'amend-card';
  card.style.cssText = 'border:1px solid #e2e8f0;border-radius:0.4rem;padding:0.5rem 0.6rem;background:#fafbfc;';
  return card;
}

function _amendCardHeader(label, onRemove) {
  const hd = document.createElement('div');
  hd.style.cssText = 'display:flex;justify-content:space-between;align-items:center;margin-bottom:0.3rem;';
  const left = document.createElement('span');
  left.style.cssText = 'font-size:0.78rem;color:#64748b;';
  left.innerHTML = '<span aria-hidden="true" style="cursor:grab;">⋮⋮</span> ' + label;
  const rm = document.createElement('button');
  rm.type = 'button';
  rm.className = 'fr-btn fr-btn--sm fr-btn--tertiary-no-outline';
  rm.textContent = '✕';
  rm.setAttribute('aria-label', 'Retirer');
  rm.onclick = onRemove;
  hd.appendChild(left);
  hd.appendChild(rm);
  return hd;
}

function addAmendAgendaItem(data) {
  const list = document.getElementById('amend-agenda-list');
  if (!list) return;
  const item = data || { title: '', duration_minutes: 5, objective: '', key_questions: [] };
  const card = _amendCard();
  card.classList.add('amend-agenda-card');
  card.appendChild(_amendCardHeader("Point d'agenda", () => card.remove()));

  const grid = document.createElement('div');
  grid.style.cssText = 'display:grid;grid-template-columns:2fr 1fr;gap:0.4rem;';
  const titleWrap = document.createElement('div');
  titleWrap.innerHTML = '<label class="fr-label" style="font-size:0.75rem;">Titre</label>';
  const titleInp = document.createElement('input');
  titleInp.type = 'text';
  titleInp.className = 'amend-agenda-title';
  titleInp.value = item.title || '';
  titleInp.style.cssText = 'width:100%;border:1px solid #cbd5e1;border-radius:0.3rem;padding:0.3rem;font-size:0.85rem;';
  titleWrap.appendChild(titleInp);
  const durWrap = document.createElement('div');
  durWrap.innerHTML = '<label class="fr-label" style="font-size:0.75rem;">Durée (min)</label>';
  const durInp = document.createElement('input');
  durInp.type = 'number';
  durInp.min = '1';
  durInp.className = 'amend-agenda-duration';
  durInp.value = Number(item.duration_minutes) || 5;
  durInp.style.cssText = 'width:100%;border:1px solid #cbd5e1;border-radius:0.3rem;padding:0.3rem;font-size:0.85rem;';
  durWrap.appendChild(durInp);
  grid.appendChild(titleWrap);
  grid.appendChild(durWrap);
  card.appendChild(grid);

  const objLbl = document.createElement('label');
  objLbl.className = 'fr-label';
  objLbl.style.cssText = 'font-size:0.75rem;margin-top:0.3rem;display:block;';
  objLbl.textContent = 'Objectif';
  card.appendChild(objLbl);
  const objTxt = document.createElement('textarea');
  objTxt.rows = 2;
  objTxt.className = 'amend-agenda-objective';
  objTxt.value = item.objective || '';
  objTxt.style.cssText = 'width:100%;border:1px solid #cbd5e1;border-radius:0.3rem;padding:0.3rem;font-size:0.85rem;';
  card.appendChild(objTxt);

  const qLbl = document.createElement('label');
  qLbl.className = 'fr-label';
  qLbl.style.cssText = 'font-size:0.75rem;margin-top:0.3rem;display:block;';
  qLbl.textContent = 'Questions clés';
  card.appendChild(qLbl);
  const qList = document.createElement('div');
  qList.className = 'amend-agenda-questions';
  qList.style.cssText = 'display:flex;flex-direction:column;gap:0.25rem;';
  (item.key_questions || []).forEach(q => qList.appendChild(_amendInputRow(q, 'Question clé')));
  card.appendChild(qList);
  const addQ = document.createElement('button');
  addQ.type = 'button';
  addQ.className = 'fr-btn fr-btn--sm fr-btn--tertiary';
  addQ.textContent = '+ Question';
  addQ.style.marginTop = '0.25rem';
  addQ.onclick = () => qList.appendChild(_amendInputRow('', 'Question clé'));
  card.appendChild(addQ);

  list.appendChild(card);
}

function addAmendParticipant(data) {
  const list = document.getElementById('amend-participants-list');
  if (!list) return;
  const item = data || { name: '', note: '' };
  const card = _amendCard();
  card.classList.add('amend-participant-card');
  card.appendChild(_amendCardHeader('Participant', () => card.remove()));
  const nameLbl = document.createElement('label');
  nameLbl.className = 'fr-label';
  nameLbl.style.cssText = 'font-size:0.75rem;display:block;';
  nameLbl.textContent = 'Nom';
  card.appendChild(nameLbl);
  const nameInp = document.createElement('input');
  nameInp.type = 'text';
  nameInp.className = 'amend-participant-name';
  nameInp.value = item.name || '';
  nameInp.style.cssText = 'width:100%;border:1px solid #cbd5e1;border-radius:0.3rem;padding:0.3rem;font-size:0.85rem;';
  card.appendChild(nameInp);
  const noteLbl = document.createElement('label');
  noteLbl.className = 'fr-label';
  noteLbl.style.cssText = 'font-size:0.75rem;display:block;margin-top:0.3rem;';
  noteLbl.textContent = 'Note';
  card.appendChild(noteLbl);
  const noteTxt = document.createElement('textarea');
  noteTxt.rows = 2;
  noteTxt.className = 'amend-participant-note';
  noteTxt.value = item.note || '';
  noteTxt.style.cssText = 'width:100%;border:1px solid #cbd5e1;border-radius:0.3rem;padding:0.3rem;font-size:0.85rem;';
  card.appendChild(noteTxt);
  list.appendChild(card);
}

function addAmendThread(data) {
  const list = document.getElementById('amend-threads-list');
  if (!list) return;
  const item = data || { item: '', source: '' };
  const card = _amendCard();
  card.classList.add('amend-thread-card');
  card.appendChild(_amendCardHeader('Point en suspens', () => card.remove()));
  const itemLbl = document.createElement('label');
  itemLbl.className = 'fr-label';
  itemLbl.style.cssText = 'font-size:0.75rem;display:block;';
  itemLbl.textContent = 'Item';
  card.appendChild(itemLbl);
  const itemTxt = document.createElement('textarea');
  itemTxt.rows = 2;
  itemTxt.className = 'amend-thread-item';
  itemTxt.value = item.item || '';
  itemTxt.style.cssText = 'width:100%;border:1px solid #cbd5e1;border-radius:0.3rem;padding:0.3rem;font-size:0.85rem;';
  card.appendChild(itemTxt);
  const srcLbl = document.createElement('label');
  srcLbl.className = 'fr-label';
  srcLbl.style.cssText = 'font-size:0.75rem;display:block;margin-top:0.3rem;';
  srcLbl.textContent = 'Source (optionnel)';
  card.appendChild(srcLbl);
  const srcInp = document.createElement('input');
  srcInp.type = 'text';
  srcInp.className = 'amend-thread-source';
  srcInp.value = item.source || '';
  srcInp.style.cssText = 'width:100%;border:1px solid #cbd5e1;border-radius:0.3rem;padding:0.3rem;font-size:0.85rem;';
  card.appendChild(srcInp);
  list.appendChild(card);
}

function addAmendOpeningQuestion(value) {
  const list = document.getElementById('amend-opening-list');
  if (!list) return;
  list.appendChild(_amendInputRow(value || '', "Question d'ouverture"));
}

function addAmendRisk(value) {
  const list = document.getElementById('amend-risks-list');
  if (!list) return;
  list.appendChild(_amendInputRow(value || '', 'Risque'));
}

function addAmendChecklistItem(value) {
  const list = document.getElementById('amend-checklist-list');
  if (!list) return;
  list.appendChild(_amendInputRow(value || '', 'Élément de checklist'));
}

function fillAmendForm(briefJson) {
  _amendBriefOriginal = (briefJson && typeof briefJson === 'object' && !Array.isArray(briefJson)) ? briefJson : {};
  const objEl = document.getElementById('amend-objective');
  if (objEl) {
    objEl.value = _amendBriefOriginal.objective_reformulated || '';
    _updateAmendObjectiveCount();
  }
  const ctxEl = document.getElementById('amend-context');
  const ctxNullEl = document.getElementById('amend-context-null');
  if (ctxEl && ctxNullEl) {
    const ctx = _amendBriefOriginal.context_recap;
    if (ctx === null || ctx === undefined) {
      ctxNullEl.checked = true;
      ctxEl.value = '';
      ctxEl.disabled = true;
    } else {
      ctxNullEl.checked = false;
      ctxEl.value = ctx;
      ctxEl.disabled = false;
    }
  }
  ['amend-agenda-list', 'amend-participants-list', 'amend-threads-list',
   'amend-opening-list', 'amend-risks-list', 'amend-checklist-list'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.innerHTML = '';
  });
  (_amendBriefOriginal.agenda || []).forEach(it => addAmendAgendaItem(it));
  (_amendBriefOriginal.participants_notes || []).forEach(it => addAmendParticipant(it));
  (_amendBriefOriginal.open_threads || []).forEach(it => addAmendThread(it));
  (_amendBriefOriginal.opening_questions || []).forEach(v => addAmendOpeningQuestion(v));
  (_amendBriefOriginal.risk_points || []).forEach(v => addAmendRisk(v));
  (_amendBriefOriginal.preparation_checklist || []).forEach(v => addAmendChecklistItem(v));
}

function _updateAmendObjectiveCount() {
  const inp = document.getElementById('amend-objective');
  const out = document.getElementById('amend-objective-count');
  if (inp && out) out.textContent = `${inp.value.length} caractères`;
}

function buildAmendBriefJson() {
  const out = JSON.parse(JSON.stringify(_amendBriefOriginal || {}));
  const objEl = document.getElementById('amend-objective');
  out.objective_reformulated = (objEl && objEl.value || '').trim();
  const ctxNullEl = document.getElementById('amend-context-null');
  const ctxEl = document.getElementById('amend-context');
  if (ctxNullEl && ctxNullEl.checked) {
    out.context_recap = null;
  } else {
    out.context_recap = (ctxEl && ctxEl.value || '').trim();
  }
  const agenda = [];
  document.querySelectorAll('#amend-agenda-list .amend-agenda-card').forEach(card => {
    const title = (card.querySelector('.amend-agenda-title') || {}).value || '';
    const dur = parseInt((card.querySelector('.amend-agenda-duration') || {}).value || '0', 10) || 0;
    const obj = (card.querySelector('.amend-agenda-objective') || {}).value || '';
    const qs = [];
    card.querySelectorAll('.amend-agenda-questions input[type="text"]').forEach(inp => {
      const v = (inp.value || '').trim();
      if (v) qs.push(v);
    });
    agenda.push({ title: title.trim(), duration_minutes: dur, objective: obj.trim(), key_questions: qs });
  });
  out.agenda = agenda;
  const participants = [];
  document.querySelectorAll('#amend-participants-list .amend-participant-card').forEach(card => {
    const name = ((card.querySelector('.amend-participant-name') || {}).value || '').trim();
    const note = ((card.querySelector('.amend-participant-note') || {}).value || '').trim();
    participants.push({ name, note });
  });
  out.participants_notes = participants;
  const threads = [];
  document.querySelectorAll('#amend-threads-list .amend-thread-card').forEach(card => {
    const itm = ((card.querySelector('.amend-thread-item') || {}).value || '').trim();
    const src = ((card.querySelector('.amend-thread-source') || {}).value || '').trim();
    threads.push({ item: itm, source: src || null });
  });
  out.open_threads = threads;
  const _collect = (sel) => {
    const arr = [];
    document.querySelectorAll(sel).forEach(inp => {
      const v = (inp.value || '').trim();
      if (v) arr.push(v);
    });
    return arr;
  };
  out.opening_questions = _collect('#amend-opening-list input[type="text"]');
  out.risk_points = _collect('#amend-risks-list input[type="text"]');
  out.preparation_checklist = _collect('#amend-checklist-list input[type="text"]');
  return out;
}

function _validateAmendForm() {
  const errs = [];
  const obj = (document.getElementById('amend-objective') || {}).value || '';
  if (!obj.trim()) errs.push("L'objectif reformulé est obligatoire.");
  let agendaIdx = 0;
  document.querySelectorAll('#amend-agenda-list .amend-agenda-card').forEach(card => {
    agendaIdx += 1;
    const title = ((card.querySelector('.amend-agenda-title') || {}).value || '').trim();
    const dur = parseInt((card.querySelector('.amend-agenda-duration') || {}).value || '0', 10) || 0;
    if (!title) errs.push(`Agenda #${agendaIdx} : titre manquant.`);
    if (dur <= 0) errs.push(`Agenda #${agendaIdx} : durée doit être > 0.`);
  });
  return errs;
}

async function saveAmendBrief() {
  if (!_briefDetailId) return;
  const errs = _validateAmendForm();
  if (errs.length) { _toast(errs[0], 'error'); return; }
  const briefJson = buildAmendBriefJson();
  const btn = document.getElementById('amend-save-btn');
  const spin = document.getElementById('amend-save-spinner');
  if (btn) btn.disabled = true;
  if (spin) spin.style.display = '';
  try {
    const r = await fetch(`/api/preparations/${_briefDetailId}/amend`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: briefJson }),
    });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || 'amend_failed');
    detailCache.invalidate('brief', _briefDetailId);
    _toast('Brief amendé.', 'success');
    const pane = document.getElementById('brief-amend-pane');
    if (pane) pane.style.display = 'none';
    showBriefDetail(_briefDetailId);
  } catch (e) {
    _toast('Amendement échoué.', 'error');
  } finally {
    if (btn) btn.disabled = false;
    if (spin) spin.style.display = 'none';
  }
}

// ─── Sections collapsibles ────────────────────────────────────────────────
function _amendSectionKey(idx) { return `brief-amend-section-${idx}-collapsed`; }

function _setAmendSectionCollapsed(header, collapsed) {
  const fs = header.closest('.brief-amend-section');
  if (!fs) return;
  const body = fs.querySelector('.brief-amend-section-body');
  const chev = header.querySelector('.brief-amend-chevron');
  if (body) body.style.display = collapsed ? 'none' : '';
  if (chev) chev.textContent = collapsed ? '▸' : '▾';
}

function restoreAmendSectionsState() {
  document.querySelectorAll('.brief-amend-section-header[data-section-toggle]').forEach(hd => {
    const idx = hd.getAttribute('data-section-toggle');
    let collapsed = false;
    try { collapsed = sessionStorage.getItem(_amendSectionKey(idx)) === '1'; } catch (e) {}
    _setAmendSectionCollapsed(hd, collapsed);
  });
}

// ─── Suppression / restauration / définitif ──────────────────────────────
async function deleteBrief(briefId, titleRaw) {
  const title = (titleRaw || '').replace(/&#39;/g, "'");
  if (!confirm(`Mettre « ${title} » à la corbeille ?`)) return;
  try {
    const r = await fetch(`/api/preparations/${briefId}`, { method: 'DELETE' });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || 'delete_failed');
    detailCache.invalidate('brief', briefId);
    _toast('Brief envoyé à la corbeille.', 'success');
    loadBriefs();
  } catch (e) { _toast('Suppression échouée.', 'error'); }
}

async function restoreBrief(briefId) {
  try {
    const r = await fetch(`/api/preparations/${briefId}/restore`, { method: 'POST' });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || 'restore_failed');
    _toast('Brief restauré.', 'success');
    if (typeof window.loadTrash === 'function') window.loadTrash();
  } catch (e) { _toast('Restauration échouée.', 'error'); }
}

async function deleteBriefPermanently(briefId, titleRaw) {
  const title = (titleRaw || '').replace(/&#39;/g, "'");
  if (!confirm(`Supprimer définitivement « ${title} » ? Cette action est irréversible.`)) return;
  try {
    const r = await fetch(`/api/preparations/${briefId}/permanently`, { method: 'DELETE' });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || 'delete_failed');
    _toast('Brief supprimé définitivement.', 'success');
    if (typeof window.loadTrash === 'function') window.loadTrash();
  } catch (e) { _toast('Suppression définitive échouée.', 'error'); }
}

// ─── Délégation d'événements DSFR + click handler unique ─────────────────
function _onPanelClick(ev) {
  const target = ev.target;
  if (!target) return;
  const actionEl = target.closest && target.closest('[data-action]');
  if (!actionEl) return;
  if (!_panelEl().contains(actionEl)) return;
  const action = actionEl.getAttribute('data-action');
  switch (action) {
    case 'open-brief': {
      ev.preventDefault();
      const id = actionEl.getAttribute('data-brief-id');
      if (id) showBriefDetail(id);
      return;
    }
    case 'delete-brief': {
      ev.preventDefault();
      const id = actionEl.getAttribute('data-brief-id');
      const title = actionEl.getAttribute('data-brief-title') || '';
      if (id) deleteBrief(id, title);
      return;
    }
    case 'show-list': {
      ev.preventDefault();
      showBriefList();
      return;
    }
    case 'rename-brief':
      ev.preventDefault(); renameBriefPrompt(); return;
    case 'toggle-amend':
      ev.preventDefault(); toggleAmendBrief(); return;
    case 'link-audio':
      ev.preventDefault(); linkAudioToBriefPrompt(); return;
    case 'detach-audio': {
      ev.preventDefault();
      const id = actionEl.getAttribute('data-audio-id');
      if (id) detachAudioFromBrief(id);
      return;
    }
    case 'goto-transfers': {
      ev.preventDefault();
      try {
        const tabBtn = document.getElementById('tab-btn-transfers');
        if (tabBtn) tabBtn.click();
      } catch (e) {}
      return;
    }
    case 'trash-older-90d':
      ev.preventDefault(); trashAllOlderThan90d(); return;
    case 'dismiss-older-90d':
      ev.preventDefault(); dismissOlderThan90dBanner(); return;
    case 'add-agenda-item':
      ev.preventDefault(); addAmendAgendaItem(); return;
    case 'add-participant':
      ev.preventDefault(); addAmendParticipant(); return;
    case 'add-thread':
      ev.preventDefault(); addAmendThread(); return;
    case 'add-opening-question':
      ev.preventDefault(); addAmendOpeningQuestion(); return;
    case 'add-risk':
      ev.preventDefault(); addAmendRisk(); return;
    case 'add-checklist-item':
      ev.preventDefault(); addAmendChecklistItem(); return;
    case 'toggle-amend-section': {
      // Géré par _onPanelClickSection ci-dessous (passe par data-section-toggle).
      return;
    }
    default:
      return;
  }
}

function _onAmendSectionHeaderClick(ev) {
  const hd = ev.target && ev.target.closest && ev.target.closest('.brief-amend-section-header[data-section-toggle]');
  if (!hd) return;
  if (!_panelEl().contains(hd)) return;
  const fs = hd.closest('.brief-amend-section');
  if (!fs) return;
  const body = fs.querySelector('.brief-amend-section-body');
  const collapsed = !(body && body.style.display === 'none') ? true : false;
  _setAmendSectionCollapsed(hd, collapsed);
  const idx = hd.getAttribute('data-section-toggle');
  try { sessionStorage.setItem(_amendSectionKey(idx), collapsed ? '1' : '0'); } catch (e) {}
}

function _onAmendObjectiveInput() {
  _updateAmendObjectiveCount();
}

function _onAmendContextNullChange() {
  const ctxNullEl = document.getElementById('amend-context-null');
  const ctxEl = document.getElementById('amend-context');
  if (!ctxEl || !ctxNullEl) return;
  ctxEl.disabled = ctxNullEl.checked;
  if (ctxNullEl.checked) ctxEl.value = '';
}

function _onAmendFormSubmit(ev) {
  ev.preventDefault();
  saveAmendBrief();
  return false;
}

function _panelEl() {
  return document.getElementById(PANEL_ID) || document.body;
}

// ─── mount / unmount ──────────────────────────────────────────────────────
export function mount(container /*, ctx */) {
  const panel = container || _panelEl();
  if (!panel) return;
  if (_delegationBound) {
    // Idempotent : on recharge la liste à chaque mount (visite onglet).
    try { loadBriefs(); } catch (e) {}
    return;
  }
  panel.addEventListener('click', _onPanelClick);
  panel.addEventListener('click', _onAmendSectionHeaderClick);

  const objEl = document.getElementById('amend-objective');
  if (objEl) objEl.addEventListener('input', _onAmendObjectiveInput);
  const ctxNullEl = document.getElementById('amend-context-null');
  if (ctxNullEl) ctxNullEl.addEventListener('change', _onAmendContextNullChange);
  const form = document.getElementById('brief-amend-form');
  if (form) form.addEventListener('submit', _onAmendFormSubmit);

  _delegationBound = true;
  try { loadBriefs(); } catch (e) {}
}

export function unmount(container) {
  const panel = container || _panelEl();
  if (!panel || !_delegationBound) return;
  panel.removeEventListener('click', _onPanelClick);
  panel.removeEventListener('click', _onAmendSectionHeaderClick);
  const objEl = document.getElementById('amend-objective');
  if (objEl) objEl.removeEventListener('input', _onAmendObjectiveInput);
  const ctxNullEl = document.getElementById('amend-context-null');
  if (ctxNullEl) ctxNullEl.removeEventListener('change', _onAmendContextNullChange);
  const form = document.getElementById('brief-amend-form');
  if (form) form.removeEventListener('submit', _onAmendFormSubmit);
  _delegationBound = false;
}

// ─── Exports nommés (introspection IDE + tests) ───────────────────────────
export {
  loadBriefs,
  showBriefList,
  showBriefDetail,
  renderBriefBody,
  loadBriefAudioFiles,
  loadBriefSeries,
  detachAudioFromBrief,
  linkAudioToBriefPrompt,
  renameBriefPrompt,
  toggleAmendBrief,
  fillAmendForm,
  buildAmendBriefJson,
  saveAmendBrief,
  addAmendAgendaItem,
  addAmendParticipant,
  addAmendThread,
  addAmendOpeningQuestion,
  addAmendRisk,
  addAmendChecklistItem,
  deleteBrief,
  restoreBrief,
  deleteBriefPermanently,
  renderOlderThan90dBanner,
  dismissOlderThan90dBanner,
  trashAllOlderThan90d,
  restoreAmendSectionsState,
};

// ─── Compat legacy (referencés depuis legacy.js::setupTabs et corbeille) ──
window.loadBriefs = loadBriefs;
window.showBriefDetail = showBriefDetail;
window.showBriefList = showBriefList;
window.restoreBrief = restoreBrief;
window.deleteBriefPermanently = deleteBriefPermanently;
// Le setup global (sections collapsibles initial) est repris au 1er mount().
// On expose aussi restoreAmendSectionsState pour les tests éventuels.
window.restoreAmendSectionsState = restoreAmendSectionsState;

// Boot : monte automatiquement au DOMContentLoaded (l'onglet brief est
// présent dans la nav dès le chargement, et legacy.js::setupTabs s'attend
// à pouvoir appeler loadBriefs() au switch d'onglet). On monte les
// listeners DOM côté form/sections dès que possible — la liste, elle,
// est chargée à chaque visite de l'onglet via le hook legacy.
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => mount());
} else {
  // Module évalué après DOMContentLoaded (cas du bundle Vite type=module).
  try { mount(); } catch (e) { /* tolérant — re-mount au switch d'onglet */ }
}
