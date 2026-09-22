// Onglet « Associer mon téléphone » (ex-« Mes appareils ») + formulaire
// d'association (ex-« Enrôler un nouvel appareil »).
//
// Migration PR6 (refonte UX devices DSFR) : les fonctions historiquement
// hébergées dans frontend/legacy.js sont déplacées ici et exposées sur
// `window.*` pour rester appelables par le reste de legacy.js (lequel
// déclenche encore loadDevices() à la fin de son init et au retour de
// certaines opérations cross-tab).
//
// Contrat des panneaux :
//   - #panel-devices : liste des devices enrôlés (loadDevices/renderDevices,
//     fr-card horizontale, actions Renouveler/Révoquer/Supprimer/Renommer
//     via délégation d'événements `data-action`).
//   - #panel-generate : formulaire d'enrôlement (generateCode/resetForm,
//     QR + countdown). DSFR `fr-select`, `fr-input`, `fr-btn`, `fr-alert`,
//     `fr-callout`.
//
// Dépendances cross-modules conservées sur window pour la transition :
//   - window.escapeHtml, window.deviceTokenStateLabel, window.deviceTokenStateColor,
//     window.tokenIdShort, window.tokenValidityDaysLabel, window.formatDateTimeShort
//     → utilitaires d'affichage (legacy.js).
//   - window.pickDefaultTab, window.loadSessions, window._devicesByQrToken
//     → orchestration cross-onglet (legacy.js).
//   - window.DEVICE_RETENTION_DAYS → bootstrap (lib/bootstrap.js).

import { formatDate } from '../utils/format.js';

const _esc = (v) => (window.escapeHtml ? window.escapeHtml(v) : String(v == null ? '' : v));

// ── État local du tab devices ──────────────────────────────────────────
let _showAllDevices = false;
let _pendingDevicesPollTimer = null;
let _userRequestedEnrollmentForm = false;
try {
  _userRequestedEnrollmentForm = sessionStorage.getItem('userRequestedEnrollmentForm') === '1';
} catch (e) {}

// ── Helpers DOM ─────────────────────────────────────────────────────────
function _$(id) { return document.getElementById(id); }

// État d'association affiché sous le titre de l'onglet : compte les
// téléphones non révoqués (même compteur que le message de liste vide).
function _updateAssociationState(nonRevokedCount) {
  const el = _$('devices-association-state');
  if (!el) return;
  const n = Number(nonRevokedCount) || 0;
  el.textContent = n === 0
    ? 'Aucun téléphone associé'
    : `${n} téléphone${n > 1 ? 's' : ''} associé${n > 1 ? 's' : ''}`;
}

function _updateDeviceFilterButton() {
  const btn = _$('device-filter-btn');
  if (!btn) return;
  btn.textContent = _showAllDevices ? 'Masquer les téléphones retirés' : 'Afficher les téléphones retirés';
}

function _schedulePendingDevicesPoll(devices) {
  const hasPending = (devices || []).some((d) => (d && d.status ? String(d.status).toLowerCase() : '') === 'pending');
  if (_pendingDevicesPollTimer) {
    clearTimeout(_pendingDevicesPollTimer);
    _pendingDevicesPollTimer = null;
  }
  if (hasPending) {
    // Aligne le poll sur la cadence heartbeat mobile-upload-pwa (15s) afin
    // que le user voie la transition pending → active rapidement après
    // confirmation côté device.
    _pendingDevicesPollTimer = setTimeout(() => {
      _pendingDevicesPollTimer = null;
      loadDevices();
    }, 15000);
  }
}

// L'assistant « Associer en sécurité votre téléphone » (2026-09-22) se règle
// sur l'état, sans case « ne plus montrer » :
//   - première ouverture → les trois étapes ;
//   - un téléphone actif → le bandeau « déjà associé » (nom, date, validité),
//     la seconde voie devient « Associer un autre téléphone » ;
//   - le formulaire du code reste TOUJOURS visible : c'est le seul chemin.
//   EXCEPTION : si un QR vient d'être généré (#result.active), generateCode()
//   a caché le form pour montrer le QR ; on ne le réécrase pas (bug 2026-05-18).
const CLE_ASSISTANT_VU = 'mesreunions.assistant-telephone.vu';

function _applyEnrollmentFormVisibility(devices) {
  const form = _$('generate-form');
  const result = _$('result');
  const collapsed = _$('enrollment-collapsed');
  const etapes = _$('assistant-etapes');
  const actifs = (devices || []).filter((d) => {
    const st = (d.status || '').toLowerCase();
    return st !== 'revoked' && st !== 'pending';
  });
  let vu = false;
  try { vu = localStorage.getItem(CLE_ASSISTANT_VU) === '1'; } catch (e) { /* navigation privée */ }
  if (etapes) etapes.style.display = vu ? 'none' : '';
  if (collapsed) {
    if (actifs.length) {
      const d = actifs[0];
      const titre = _$('enrollment-collapsed-titre');
      const detail = _$('enrollment-collapsed-detail');
      const nom = d.device_name || 'téléphone sans nom';
      if (titre) titre.textContent = actifs.length > 1
        ? `${actifs.length} téléphones sont déjà associés (dont ${nom})`
        : `Un téléphone est déjà associé : ${nom}`;
      const validite = window.tokenValidityDaysLabel ? window.tokenValidityDaysLabel(d.retention_expires_at) : '';
      if (detail) detail.textContent = `${validite ? 'Envois acceptés ' + validite + '. ' : ''}Vous pouvez enregistrer dès maintenant.`;
      collapsed.style.display = '';
    } else {
      collapsed.style.display = 'none';
    }
  }
  const eyebrow = _$('voie-code-eyebrow');
  const titreVoie = _$('voie-code-titre');
  const btn = _$('btn-generate');
  if (eyebrow) eyebrow.textContent = actifs.length ? 'Un autre appareil' : "Disponible aujourd'hui";
  if (titreVoie) titreVoie.textContent = actifs.length ? 'Associer un autre téléphone' : 'Utiliser le dictaphone du téléphone';
  if (btn) btn.textContent = actifs.length ? 'Associer cet autre téléphone' : 'Associer ce téléphone';
  if (form && !(result && result.classList.contains('active'))) {
    form.style.display = '';
  }
}

// Marque l'explication comme vue dès que l'assistant a été affiché une fois.
export function marquerAssistantVu() {
  try { localStorage.setItem(CLE_ASSISTANT_VU, '1'); } catch (e) { /* rien */ }
}

export function showEnrollmentForm() {
  _userRequestedEnrollmentForm = true;
  try { sessionStorage.setItem('userRequestedEnrollmentForm', '1'); } catch (e) {}
  const form = _$('generate-form');
  const collapsed = _$('enrollment-collapsed');
  if (form) form.style.display = '';
  if (collapsed) collapsed.style.display = 'none';
}

export function toggleDeviceScope() {
  _showAllDevices = !_showAllDevices;
  _updateDeviceFilterButton();
  loadDevices();
}

export function updateDeviceFilterButton() {
  _updateDeviceFilterButton();
}

// ── Rendu liste devices (fr-card horizontale par appareil) ─────────────
function _renderDeviceCard(d) {
  const status = (d.status || '').toLowerCase();
  const isRevoked = status === 'revoked';
  const recentUploads24h = Number(d.recent_uploads_24h || 0);
  const remainingUploads = Number(d.remaining_uploads || 0);
  const sessionMaxUploads = Number(d.session_max_uploads || 0);
  const renewNeedsAttention = !isRevoked && (!!d.session_expiring_soon || remainingUploads < 2);
  const stateLabel = window.deviceTokenStateLabel ? window.deviceTokenStateLabel(d) : status || 'active';
  const tokenShort = (d.session_simple_code || '').trim() || (window.tokenIdShort ? window.tokenIdShort(d.qr_token) : (d.qr_token || ''));
  const validity = window.tokenValidityDaysLabel ? window.tokenValidityDaysLabel(d.retention_expires_at) : '-';
  const lastSeen = window.formatDateTimeShort ? window.formatDateTimeShort(d.last_seen_at) : '-';
  const remainingFragment = (sessionMaxUploads > 0 && remainingUploads < 10)
    ? ` | restants: ${remainingUploads}/${sessionMaxUploads}` : '';

  // Badge DSFR pour l'état du token. Mapping → variants fr-badge--*.
  let badgeClass = 'fr-badge--success';
  if (stateLabel === 'retiré') badgeClass = 'fr-badge--error';
  else if (stateLabel === 'expiré') badgeClass = 'fr-badge--warning';
  else if (stateLabel === 'en attente du scan') badgeClass = 'fr-badge--info';

  const devId = _esc(d.device_id);
  const qr = _esc(d.qr_token || '');
  const name = _esc(d.device_name || 'Appareil sans nom');

  return `
    <div class="fr-card fr-card--horizontal fr-card--sm device-card ${isRevoked ? 'device-row-revoked' : ''}"
         data-device-row="${devId}" style="margin-bottom:0.55rem;">
      <div class="fr-card__body">
        <div class="fr-card__content" style="padding:0.6rem 0.8rem;">
          <h3 class="fr-card__title" style="font-size:0.92rem;margin-bottom:0.3rem;">
            ${name}
            <span class="device-token-code fr-text--xs" style="margin-left:0.35rem;color:#475569;font-weight:500;"
                  title="${qr}">${_esc(tokenShort)}</span>
            <span data-device-status="${devId}"
                  class="fr-badge fr-badge--sm ${badgeClass}"
                  style="margin-left:0.4rem;vertical-align:middle;">${_esc(stateLabel)}</span>
          </h3>
          <p class="fr-card__desc device-meta fr-text--xs" data-device-meta="${devId}"
             style="margin:0 0 0.4rem 0;color:#64748b;">
            validité token: ${_esc(validity)}${remainingFragment} | récents 24h: ${recentUploads24h} | vu: ${_esc(lastSeen)}
          </p>
          <div class="fr-grid-row fr-grid-row--gutters" style="align-items:center;gap:0.35rem;margin:0;">
            <input type="text" class="fr-input fr-input--sm" id="dev-name-${devId}"
                   data-device-name-input="${devId}"
                   placeholder="Renommer l'appareil" value="${_esc(d.device_name || '')}"
                   style="flex:1;min-width:0;padding:0.3rem 0.5rem;font-size:0.8rem;">
            <button type="button" class="fr-btn fr-btn--sm fr-btn--secondary"
                    data-action="rename-device" data-device-id="${devId}">Renommer</button>
          </div>
        </div>
        <div class="fr-card__footer" style="padding:0.4rem 0.8rem;border-top:1px solid #e2e8f0;">
          <ul class="fr-btns-group fr-btns-group--sm fr-btns-group--inline fr-btns-group--right"
              style="margin:0;">
            <li>
              <button type="button"
                      class="fr-btn fr-btn--sm fr-btn--secondary ${renewNeedsAttention ? 'btn-renew-alert' : ''}"
                      data-action="renew-device" data-qr-token="${qr}">
                Prolonger
              </button>
            </li>
            <li>
              <button type="button" class="fr-btn fr-btn--sm fr-btn--tertiary-no-outline mr-retirer-discret"
                      data-action="revoke-device" data-device-id="${devId}"
                      data-device-revoke="${devId}" ${isRevoked ? 'disabled' : ''}
                      title="Téléphone perdu ou volé ? Retirer refuse ses envois immédiatement">
                Retirer
              </button>
            </li>
            <li>
              <button type="button" class="fr-btn fr-btn--sm fr-btn--tertiary-no-outline btn-danger-mini"
                      data-action="delete-device" data-device-id="${devId}"
                      data-device-name="${name}" data-device-delete="${devId}"
                      title="Suppression irréversible (audit perdu)">
                Supprimer
              </button>
            </li>
          </ul>
        </div>
      </div>
    </div>
  `;
}

export async function loadDevices() {
  const container = _$('devices-list');
  if (!container) return;
  try {
    const resp = await fetch('/api/my-devices');
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || 'Erreur chargement devices');
    const devices = Array.isArray(data) ? data : [];
    _schedulePendingDevicesPoll(devices);
    _applyEnrollmentFormVisibility(devices);
    const nowMs = Date.now();
    const oneDayMs = 24 * 60 * 60 * 1000;

    const nonRevokedCount = devices.filter((d) => (d.status || '').toLowerCase() !== 'revoked').length;
    _updateAssociationState(nonRevokedCount);
    // Pour la carte « Enregistrez depuis votre téléphone » de la liste des réunions
    // (tabs/meetings.js) et le compte de l'entrée « Mes téléphones » du menu commun.
    window.__mesreunionsTelephones = nonRevokedCount;
    try { document.dispatchEvent(new CustomEvent('mesreunions:telephones', { detail: { n: nonRevokedCount } })); } catch (e) { /* rien */ }
    // Sélection onglet par défaut au 1er chargement (idempotent).
    if (typeof window.pickDefaultTab === 'function') {
      try { window.pickDefaultTab(nonRevokedCount > 0); } catch (e) {}
    }

    // Map qr_token → {name,status} partagée avec loadSessions (legacy.js).
    const byQr = window._devicesByQrToken || {};
    Object.keys(byQr).forEach((k) => delete byQr[k]);
    devices.forEach((d) => {
      const qr = (d.qr_token || '').trim();
      if (qr) {
        byQr[qr] = {
          name: d.device_name || 'Appareil',
          status: (d.status || '').toLowerCase(),
        };
      }
    });

    const visibleDevices = devices.filter((d) => {
      const status = (d.status || '').toLowerCase();
      if (status !== 'revoked') return true;
      const revokedAtRaw = d.revoked_at || d.updated_at || d.created_at;
      if (!revokedAtRaw) return _showAllDevices;
      const revokedAtMs = new Date(revokedAtRaw).getTime();
      if (!Number.isFinite(revokedAtMs)) return _showAllDevices;
      const age = nowMs - revokedAtMs;
      if (age >= oneDayMs) return false; // hide after 24h in UI, keep in DB
      return _showAllDevices;
    });

    if (!visibleDevices.length) {
      const msg = _showAllDevices
        ? `Aucun téléphone affichable. Téléphones associés non révoqués : <strong>${nonRevokedCount}</strong>.`
        : `Aucun téléphone associé. « Associer un téléphone » explique comment faire.`;
      container.innerHTML = `<div class="fr-callout fr-callout--blue-ecume" style="padding:0.6rem 0.8rem;">
        <p class="fr-callout__text" style="font-size:0.85rem;margin:0;">${msg}</p>
      </div>`;
      return;
    }
    container.innerHTML = visibleDevices.map(_renderDeviceCard).join('');
  } catch (e) {
    container.innerHTML = `<div class="fr-alert fr-alert--error fr-alert--sm">
      <p>Erreur chargement appareils.</p>
    </div>`;
  }
}

// ── Actions device (rename / revoke / delete / revoke-all / renew) ─────
export async function renameDevice(deviceId) {
  const input = document.querySelector(`[data-device-name-input="${deviceId}"]`) || _$(`dev-name-${deviceId}`);
  if (!input) return;
  const name = (input.value || '').trim();
  if (!name) return;
  try {
    const resp = await fetch(`/api/my-devices/${deviceId}/rename`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ device_name: name }),
    });
    const data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || 'rename_failed');
    loadDevices();
  } catch (e) {
    alert('Echec renommage appareil.');
  }
}

export async function revokeDevice(deviceId) {
  if (!confirm('Retirer ce téléphone ? Ses envois seront refusés immédiatement.')) return;
  const revokeBtn = document.querySelector(`[data-device-revoke="${deviceId}"]`);
  if (revokeBtn) revokeBtn.disabled = true;
  try {
    const resp = await fetch(`/api/my-devices/${deviceId}/revoke`, { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || 'revoke_failed');
    const statusEl = document.querySelector(`[data-device-status="${deviceId}"]`);
    if (statusEl) {
      statusEl.textContent = 'retiré';
      statusEl.className = 'fr-badge fr-badge--sm fr-badge--error';
    }
    setTimeout(loadDevices, 250);
  } catch (e) {
    if (revokeBtn) revokeBtn.disabled = false;
    alert('Echec révocation appareil.');
  }
}

export async function deleteDevicePermanently(deviceId, deviceName) {
  // Double-confirm — irreversible, aucune ligne d'audit conservée.
  const label = (deviceName || 'sans nom').slice(0, 60);
  if (!confirm(`Supprimer DÉFINITIVEMENT l'appareil « ${label} » ?

Cette action est irréversible : la ligne sera retirée de la base de données (aucun audit conservé). Pour une suppression réversible, utilisez « Retirer ».`)) {
    return;
  }
  if (!confirm(`Confirmer la suppression définitive de « ${label} » ?`)) return;
  const btn = document.querySelector(`[data-device-delete="${deviceId}"]`);
  if (btn) btn.disabled = true;
  try {
    const resp = await fetch(`/api/my-devices/${deviceId}`, { method: 'DELETE' });
    const data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || 'delete_failed');
    const row = document.querySelector(`[data-device-row="${deviceId}"]`);
    if (row) row.remove();
    setTimeout(loadDevices, 250);
  } catch (e) {
    if (btn) btn.disabled = false;
    alert('Echec suppression définitive de l\'appareil.');
  }
}

export async function revokeAllDevices() {
  if (!confirm('Retirer tous vos téléphones ? Leurs envois seront refusés immédiatement.')) return;
  try {
    const resp = await fetch('/api/my-devices/revoke-all', { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || 'revoke_all_failed');
    alert(`Téléphones retirés : ${data.revoked || 0}`);
    loadDevices();
  } catch (e) {
    alert('Echec révocation globale.');
  }
}

export async function renewTokenByQr(qrToken) {
  if (!qrToken) {
    alert('Token introuvable pour cet appareil.');
    return;
  }
  const retentionDays = Number(window.DEVICE_RETENTION_DAYS || 15);
  if (!confirm(`Prolonger l'association de ${retentionDays} jours ?`)) return;
  try {
    // Pas de ttl_minutes : le serveur applique DEVICE_TOKEN_RETENTION_HOURS
    // (15j en prod-bêta) pour rester aligné avec la rétention device. Bug
    // historique : on envoyait la valeur du select #ttl (5 min) → l'access
    // expirait 5 min après le renew.
    const resp = await fetch('/api/my-token/renew-7d', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ qr_token: qrToken }),
    });
    const data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || 'renew_failed');
    if (typeof window.loadSessions === 'function') {
      try { window.loadSessions(); } catch (e) {}
    }
    loadDevices();
  } catch (e) {
    alert('Echec renouvellement token.');
  }
}

// ── Onglet "Enrôler un nouvel appareil" : generateCode / resetForm ─────
export async function generateCode() {
  const btn = _$('btn-generate');
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = 'Génération...';
  try {
    const resp = await fetch('/api/generate-code', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ttl_minutes: _$('ttl') ? _$('ttl').value : '5',
        max_uploads: parseInt((_$('max-uploads') || { value: 299 }).value, 10) || 299,
        auto_transcribe: !!(_$('auto-transcribe') && _$('auto-transcribe').checked),
      }),
    });
    if (!resp.ok) {
      const err = await resp.json();
      throw new Error(err.error || 'Erreur serveur');
    }
    const data = await resp.json();
    if (_$('display-code')) _$('display-code').textContent = data.simple_code;
    if (_$('qr-img')) _$('qr-img').src = '/api/qr-image/' + data.qr_token;
    if (_$('display-expires')) {
      _$('display-expires').textContent =
        'Valide jusqu\'au ' + formatDate(data.expires_at, { withTime: true });
    }
    if (_$('display-remaining')) {
      _$('display-remaining').textContent = `Téléchargements restants : ${data.max_uploads}`;
    }
    if (_$('generate-form')) _$('generate-form').style.display = 'none';
    if (_$('result')) _$('result').classList.add('active');

    if (typeof window.loadSessions === 'function') {
      try { window.loadSessions(); } catch (e) {}
    }
    loadDevices();
  } catch (e) {
    alert('Erreur: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Générer un code';
  }
}

export function resetForm() {
  if (_$('generate-form')) _$('generate-form').style.display = 'block';
  if (_$('result')) _$('result').classList.remove('active');
}

// ── Délégation d'événements (mount/unmount) ────────────────────────────
function _onDevicesClick(ev) {
  const target = ev.target.closest && ev.target.closest('[data-action]');
  if (!target) return;
  const action = target.getAttribute('data-action');
  if (action === 'toggle-device-scope') {
    toggleDeviceScope();
  } else if (action === 'revoke-all-devices') {
    revokeAllDevices();
  } else if (action === 'rename-device') {
    renameDevice(target.getAttribute('data-device-id'));
  } else if (action === 'revoke-device') {
    revokeDevice(target.getAttribute('data-device-id'));
  } else if (action === 'delete-device') {
    deleteDevicePermanently(
      target.getAttribute('data-device-id'),
      target.getAttribute('data-device-name')
    );
  } else if (action === 'renew-device') {
    renewTokenByQr(target.getAttribute('data-qr-token'));
  }
}

function _onGenerateClick(ev) {
  const target = ev.target.closest && ev.target.closest('[data-action]');
  if (!target) return;
  const action = target.getAttribute('data-action');
  if (action === 'generate-code') {
    generateCode();
  } else if (action === 'reset-form') {
    resetForm();
  } else if (action === 'show-enrollment-form') {
    showEnrollmentForm();
  } else if (action === 'store-bientot') {
    // Simulation assumée : aucune application n'est publiée (voir le template).
    const nom = target.getAttribute('data-store') || 'le magasin';
    if (window.showToast) window.showToast(`L'application Transcript n'est pas encore publiée sur ${nom}. « M'avertir à la sortie » vous préviendra.`, 'info');
  } else if (action === 'store-avertir') {
    // Une idée déposée par « Mon avis » du menu commun : comptée, instruite.
    if (window.MirAI && typeof window.MirAI.ouvrirAvis === 'function') {
      window.MirAI.ouvrirAvis({ motif: 'je n’ai pas su faire', texte: "Prévenez-moi à la sortie de l'application Transcript (enregistrer avec mon téléphone)." });
    } else if (window.showToast) {
      window.showToast("Nous vous préviendrons à la sortie de l'application Transcript.", 'success');
    }
    target.textContent = '✓ Vous serez averti';
    target.disabled = true;
  }
}

// Map container → listener pour pouvoir détacher en unmount.
const _devicesListeners = new WeakMap();
const _generateListeners = new WeakMap();

export function mount(container, ctx) {
  if (!container) return;
  const id = container.id || '';
  const skipLoad = !!(ctx && ctx.skipLoad);
  if (id === 'panel-devices' || container.querySelector('#devices-list')) {
    _updateDeviceFilterButton();
    if (!_devicesListeners.has(container)) {
      container.addEventListener('click', _onDevicesClick);
      _devicesListeners.set(container, _onDevicesClick);
    }
    if (!skipLoad) loadDevices();
  }
  if (id === 'panel-generate' || container.querySelector('#generate-form')) {
    if (!_generateListeners.has(container)) {
      container.addEventListener('click', _onGenerateClick);
      _generateListeners.set(container, _onGenerateClick);
    }
  }
}

export function unmount(container) {
  if (!container) return;
  const dl = _devicesListeners.get(container);
  if (dl) {
    container.removeEventListener('click', dl);
    _devicesListeners.delete(container);
  }
  const gl = _generateListeners.get(container);
  if (gl) {
    container.removeEventListener('click', gl);
    _generateListeners.delete(container);
  }
  if (_pendingDevicesPollTimer) {
    clearTimeout(_pendingDevicesPollTimer);
    _pendingDevicesPollTimer = null;
  }
}

// ── Init au boot : on bind les listeners sans déclencher loadDevices()
// (legacy.js lance la chaîne `loadDevices().then(loadSessions)` en fin de
// fichier, juste après l'import devices.js qui aura publié window.loadDevices).
function _autoMount() {
  const pd = _$('panel-devices');
  if (pd) mount(pd, { skipLoad: true });
  const pg = _$('panel-generate');
  if (pg) mount(pg);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _autoMount);
} else {
  _autoMount();
}

// ── Exposition sur window pour le reste de legacy.js qui appelle encore
// loadDevices(), generateCode(), etc. sans passer par un import ES ──────
window.loadDevices = loadDevices;
window.renameDevice = renameDevice;
window.revokeDevice = revokeDevice;
window.deleteDevicePermanently = deleteDevicePermanently;
window.revokeAllDevices = revokeAllDevices;
window.renewTokenByQr = renewTokenByQr;
window.toggleDeviceScope = toggleDeviceScope;
window.updateDeviceFilterButton = updateDeviceFilterButton;
window.showEnrollmentForm = showEnrollmentForm;
window.marquerAssistantVu = marquerAssistantVu;
window.generateCode = generateCode;
window.resetForm = resetForm;
