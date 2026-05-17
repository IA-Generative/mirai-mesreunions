// Onglet "Administration" — accessible uniquement si le user a le role
// 'admin' dans ses claims OIDC.
//
// Trois entrées :
//   - revealNavIfAdmin() : exécuté au boot (shell.js). Rend visible le
//     <li id="tab-btn-admin-li"> de la nav `fr-tabs` si user admin.
//     Sinon l'onglet reste invisible dans la nav.
//   - mount(container, ctx) : exécuté au 1er click sur l'onglet (via
//     tab-manager.js lazy-load). Pose une `fr-alert`/`fr-callout` DSFR
//     selon le rôle.
//   - gotoAdmin() : conservé pour compatibilité avec d'éventuels
//     callsites legacy (window.gotoAdmin).

import { isAdmin } from '../lib/auth.js';

export function isAdminAvailable() {
  return isAdmin();
}

export function gotoAdmin() {
  if (!isAdminAvailable()) return false;
  window.location.href = '/admin/';
  return true;
}

export function revealNavIfAdmin() {
  if (!isAdmin()) return;
  const li = document.getElementById('tab-btn-admin-li');
  if (li) li.style.display = '';
}

function _esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

export function mount(container /*, ctx */) {
  const root = container.querySelector('[data-admin-root]') || container;
  if (!isAdmin()) {
    root.innerHTML = `
      <div class="fr-alert fr-alert--info">
        <h3 class="fr-alert__title">Accès admin non disponible</h3>
        <p>Votre compte n'a pas les droits d'administration sur ce service.
           Si vous pensez qu'il s'agit d'une erreur, contactez l'équipe MIrAI.</p>
      </div>
    `;
    return;
  }

  root.innerHTML = `
    <div class="fr-callout fr-icon-information-line" style="margin-bottom:1rem;">
      <h3 class="fr-callout__title">Console d'administration</h3>
      <p class="fr-callout__text">
        Vous disposez du rôle <strong>admin</strong>.
      </p>
      <button type="button" class="fr-btn fr-btn--secondary fr-btn--icon-right fr-icon-arrow-right-line"
              data-action="goto-admin">
        Ouvrir la console admin externe
      </button>
    </div>

    <section style="margin-top:1.5rem;">
      <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:0.5rem;margin-bottom:0.5rem;">
        <h2 style="font-size:1rem;margin:0;">Feedbacks utilisateurs</h2>
        <div style="display:flex;gap:0.4rem;flex-wrap:wrap;">
          <select data-admin-feedback-filter style="font-size:0.85rem;padding:0.3rem 0.5rem;border:1px solid #cbd5e1;border-radius:4px;">
            <option value="">Tous</option>
            <option value="new">Nouveaux (à traiter)</option>
            <option value="processed">Pris en compte</option>
            <option value="dismissed">Écartés</option>
          </select>
          <button type="button" data-action="refresh-admin-feedback" class="fr-btn fr-btn--sm fr-btn--secondary">Actualiser</button>
          <a href="/api/admin/feedback.csv" download class="fr-btn fr-btn--sm fr-btn--secondary fr-btn--icon-right fr-icon-download-line">
            Export CSV
          </a>
        </div>
      </div>
      <div data-admin-feedback-list>
        <p style="color:#94a3b8;font-size:0.85rem;">Chargement…</p>
      </div>
    </section>
  `;

  const gotoBtn = root.querySelector('[data-action="goto-admin"]');
  if (gotoBtn) gotoBtn.addEventListener('click', gotoAdmin);

  const refreshBtn = root.querySelector('[data-action="refresh-admin-feedback"]');
  const filterSel = root.querySelector('[data-admin-feedback-filter]');
  const reload = () => _loadAdminFeedback(root, filterSel ? filterSel.value : '');
  if (refreshBtn) refreshBtn.addEventListener('click', reload);
  if (filterSel) filterSel.addEventListener('change', reload);

  // Délégation pour les actions par row.
  root.addEventListener('click', (ev) => {
    const action = ev.target.closest && ev.target.closest('[data-feedback-row-action]');
    if (!action) return;
    ev.preventDefault();
    const id = action.getAttribute('data-feedback-id');
    const newStatus = action.getAttribute('data-feedback-row-action');
    _patchFeedback(id, newStatus, root, filterSel ? filterSel.value : '');
  });

  reload();
}

async function _loadAdminFeedback(root, status) {
  const list = root.querySelector('[data-admin-feedback-list]');
  if (!list) return;
  const url = '/api/admin/feedback' + (status ? `?status=${encodeURIComponent(status)}` : '');
  try {
    const resp = await fetch(url);
    if (!resp.ok) {
      list.innerHTML = `<p style="color:#b91c1c;font-size:0.85rem;">Erreur de chargement (HTTP ${resp.status}).</p>`;
      return;
    }
    const data = await resp.json();
    const items = data.items || [];
    if (!items.length) {
      list.innerHTML = `<p style="color:#94a3b8;font-size:0.85rem;">Aucun feedback ${status ? '(' + status + ')' : ''}.</p>`;
      return;
    }
    list.innerHTML = `
      <p style="color:#64748b;font-size:0.78rem;margin-bottom:0.4rem;">${items.length} feedback(s) — sur ${data.total} au total.</p>
      <div style="display:flex;flex-direction:column;gap:0.4rem;">
        ${items.map(_renderAdminFeedbackRow).join('')}
      </div>
    `;
  } catch (e) {
    list.innerHTML = `<p style="color:#b91c1c;font-size:0.85rem;">Erreur réseau : ${_esc(e.message)}</p>`;
  }
}

function _renderAdminFeedbackRow(fb) {
  const created = fb.created_at
    ? new Date(fb.created_at).toLocaleDateString('fr-FR', { day: 'numeric', month: 'short', year: 'numeric' })
      + ' à ' + new Date(fb.created_at).toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' })
    : '';
  const p = fb.payload || {};
  let summary = '';
  if (fb.type === 'usefulness') {
    const thumb = p.thumb === 'up' ? '👍' : '👎';
    summary = `${thumb} <strong>Utilité</strong>` +
      (p.reasons && p.reasons.length ? ` · ${_esc(p.reasons.join(', '))}` : '') +
      (p.free_text ? `<div style="color:#475569;margin-top:0.2rem;">« ${_esc(p.free_text)} »</div>` : '');
  } else if (fb.type === 'regenerate') {
    summary = `🔄 <strong>Régénération</strong> (${_esc(p.scope || '?')}) · « ${_esc(p.reason || '')} »`;
  } else if (fb.type === 'correction') {
    const applied = Array.isArray(p.applied) ? p.applied.join(', ') : '';
    summary = `✏️ <strong>Correction</strong> · « <span style="color:#b91c1c;">${_esc(p.old || '')}</span> » → <strong style="color:#15803d;">${_esc(p.new || '')}</strong>` +
              (applied ? `<div style="font-size:0.72rem;color:#64748b;margin-top:0.2rem;">Actions : ${_esc(applied)}</div>` : '');
  }
  const status = (fb.status || 'new').toLowerCase();
  const statusTag = status === 'processed'
    ? '<span class="my-feedback-tag my-feedback-tag--processed">✓ Pris en compte</span>'
    : (status === 'dismissed'
        ? '<span class="my-feedback-tag my-feedback-tag--dismissed">Écarté</span>'
        : '<span class="my-feedback-tag my-feedback-tag--new">Nouveau</span>');
  const fileLink = fb.file_id
    ? `<a href="#" onclick="event.preventDefault();window.showFileDetail && window.showFileDetail('${_esc(fb.file_id)}');" style="font-size:0.75rem;color:#1d4ed8;">Voir la fiche</a>` : '';
  const aiSugg = fb.ai_suggestion
    ? `<div style="margin-top:0.3rem;padding:0.3rem 0.5rem;background:#eff6ff;border-left:3px solid #1d4ed8;font-size:0.78rem;color:#1e40af;">
         💡 IA : ${_esc(fb.ai_suggestion)}
       </div>` : '';
  const adminCmt = fb.admin_comment
    ? `<div style="margin-top:0.3rem;font-size:0.78rem;color:#475569;">Commentaire admin : « ${_esc(fb.admin_comment)} »</div>` : '';
  // Actions selon état.
  let actions = '';
  if (status === 'new') {
    actions = `
      <button type="button" class="fr-btn fr-btn--sm" data-feedback-row-action="processed" data-feedback-id="${_esc(fb.id)}">Marquer pris en compte</button>
      <button type="button" class="fr-btn fr-btn--sm fr-btn--secondary" data-feedback-row-action="dismissed" data-feedback-id="${_esc(fb.id)}">Écarter</button>
    `;
  } else {
    actions = `
      <button type="button" class="fr-btn fr-btn--sm fr-btn--secondary" data-feedback-row-action="new" data-feedback-id="${_esc(fb.id)}">Rouvrir</button>
    `;
  }
  return `<div style="padding:0.6rem 0.7rem;background:#fff;border:1px solid #e2e8f0;border-radius:5px;">
    <div style="display:flex;align-items:center;gap:0.5rem;margin-bottom:0.3rem;flex-wrap:wrap;">
      <span style="font-size:0.74rem;color:#94a3b8;">${_esc(created)}</span>
      <span style="font-size:0.74rem;color:#64748b;">user=${_esc((fb.user_sub || '').slice(0, 12))}…</span>
      ${statusTag}
      ${fileLink}
    </div>
    <div style="font-size:0.85rem;color:#0f172a;">${summary}</div>
    ${aiSugg}
    ${adminCmt}
    <div style="display:flex;gap:0.4rem;margin-top:0.5rem;flex-wrap:wrap;">
      ${actions}
    </div>
  </div>`;
}

async function _patchFeedback(id, newStatus, root, filterValue) {
  const comment = newStatus === 'processed'
    ? window.prompt('Commentaire (optionnel) pour cet enregistrement de "pris en compte" :', '')
    : '';
  try {
    const body = { status: newStatus };
    if (comment != null && comment !== '') body.admin_comment = comment.slice(0, 2000);
    const resp = await fetch(`/api/admin/feedback/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      alert(`Échec mise à jour : HTTP ${resp.status}`);
      return;
    }
    _loadAdminFeedback(root, filterValue);
  } catch (e) {
    alert(`Erreur réseau : ${e.message}`);
  }
}

export function unmount(/* container */) {
  // Rien à nettoyer pour ce module.
}

window.gotoAdmin = gotoAdmin;
