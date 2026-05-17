// Onglet « Mes réunions (IA) » — module ES qui prend l'ownership complet
// du rendu de la liste, du header et des interactions (expand, bulk delete
// via Alt, polling status). Remplace l'ancien rendu inline dans
// frontend/legacy.js (qui devient un fallback déclenché si ce module
// n'a pas réussi à se brancher).
//
// Design (cf. spec utilisateur 2026-05-17) :
//   • Header séparé de la liste : titre + compteur + tri date + 2 boutons
//     d'import (fichiers / dossier). La purge n'apparaît qu'en mode bulk
//     (touche Alt enfoncée + au moins 1 fichier sélectionné).
//   • Liste : 1 ligne par fichier, grille à colonnes fixes pour empêcher
//     tout chevauchement. Pastille status SVG inline (animée si pipeline
//     en cours), titre cliquable, date à HH:mm, durée, chevron ▾ qui
//     déplie un résumé enrichi.
//   • Status : SVG inline (PAS le font DSFR fr-icon-* qui rend mal sur
//     les fonds colorés ou quand notre CSS surcharge `::before`). Anneau
//     de progression + glyphe au centre, couleur par kind.
//   • Source : SVG inline également (smartphone / upload local), tooltip
//     pour le détail (nom device + code).
//   • Alt-key bulk delete : maintien Alt → checkboxes apparaissent +
//     barre d'actions en bas. Reposer Alt désactive le mode si rien
//     n'est sélectionné.

import { formatDuration, formatDate } from '../utils/format.js';

// ── Constantes ────────────────────────────────────────────────────────

// Mapping statut pipeline upload → kind UI + % approximatif.
// L'utilisateur n'a pas besoin du % exact des sous-étapes Kevent (chaque
// LLM step est court devant le whisper). On affiche un anneau plein quand
// l'étape s'achève, animé pendant.
const UPLOAD_PIPELINE = {
  pending:              { kind: 'queued',     pct:   0, label: 'En file d\'attente' },
  scanning:             { kind: 'processing', pct:  15, label: 'Antivirus' },
  scan_clean:           { kind: 'processing', pct:  30, label: 'Antivirus OK' },
  transcoding:          { kind: 'processing', pct:  45, label: 'Conversion audio' },
  transcoded:           { kind: 'processing', pct:  60, label: 'Audio normalisé' },
  ready_for_transfer:   { kind: 'processing', pct:  65, label: 'Prêt pour transfert' },
  transferring:         { kind: 'processing', pct:  75, label: 'Transfert interne' },
  transferred:          { kind: 'processing', pct:  80, label: 'Transcription IA' },
  scan_infected:        { kind: 'error',      pct: 100, label: 'Virus détecté' },
  quarantined:          { kind: 'error',      pct: 100, label: 'Mis en quarantaine' },
  transcode_failed:     { kind: 'error',      pct: 100, label: 'Échec conversion audio' },
  error:                { kind: 'error',      pct: 100, label: 'Erreur pipeline' },
};

// Override par transcription_status (post-transfert, polling séparé).
const TRANSCRIPT_PIPELINE = {
  kevent_queued:                  { kind: 'processing', pct:  80, label: 'En file Kevent' },
  kevent_processing:              { kind: 'processing', pct:  88, label: 'Transcription Whisper' },
  kevent_transcribing:            { kind: 'processing', pct:  90, label: 'Transcription Whisper' },
  kevent_completed:               { kind: 'success',    pct: 100, label: 'Réunion prête' },
  kevent_partially_completed:     { kind: 'partial',    pct: 100, label: 'Partiellement prête' },
  kevent_failed:                  { kind: 'error',      pct: 100, label: 'Échec transcription' },
  mcr_pushed:                     { kind: 'success',    pct: 100, label: 'Poussée vers MCR' },
  mcr_auth_failed:                { kind: 'error',      pct: 100, label: 'Auth MCR refusée' },
  mcr_rejected:                   { kind: 'error',      pct: 100, label: 'MCR a refusé' },
  mcr_push_failed:                { kind: 'error',      pct: 100, label: 'Échec push MCR' },
  completed:                      { kind: 'success',    pct: 100, label: 'Réunion prête' },
  failed:                         { kind: 'error',      pct: 100, label: 'Échec' },
  processing:                     { kind: 'processing', pct:  88, label: 'En cours' },
};

const KIND_COLORS = {
  success:    { ring: '#15803d', bg: '#dcfce7', fg: '#15803d' },
  processing: { ring: '#1d4ed8', bg: '#dbeafe', fg: '#1d4ed8' },
  queued:     { ring: '#94a3b8', bg: '#f1f5f9', fg: '#475569' },
  partial:    { ring: '#d97706', bg: '#fef3c7', fg: '#92400e' },
  error:      { ring: '#b91c1c', bg: '#fee2e2', fg: '#b91c1c' },
};

// État local du tab (clear sur unmount).
let _selectedIds = new Set();   // file IDs cochés en mode bulk
let _expandedIds = new Set();   // file IDs avec le chevron déployé
let _transcriptCache = new Map(); // fileId → { status, engine, kp, suggested, fetchedAt }
let _altPressed = false;
let _lastSessions = [];
let _delegationBound = false;

// ── SVG helpers ───────────────────────────────────────────────────────

function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// Pastille status — anneau de progression (stroke-dasharray) + glyphe au
// centre. Animée via classe `is-spinning` quand kind=processing.
function statusIcon(kind, pct, animated) {
  const col = KIND_COLORS[kind] || KIND_COLORS.queued;
  const r = 10;
  const circumference = 2 * Math.PI * r;     // ~62.83
  const offset = circumference * (1 - (Math.max(0, Math.min(100, pct)) / 100));
  const ringClass = animated ? 'meeting-status-ring is-spinning' : 'meeting-status-ring';
  // Glyphe central par kind.
  const glyphs = {
    success:    '<path d="M8 12.5l2.5 2.5L16 9" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" fill="none"/>',
    processing: '<circle cx="12" cy="12" r="2.5" fill="currentColor"/>',
    queued:     '<path d="M9 8h6M9 16h6M10 8v2c0 1.5 1 2.5 2 3 1-.5 2-1.5 2-3V8M10 16v-2c0-1.5 1-2.5 2-3 1 .5 2 1.5 2 3v2" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" fill="none"/>',
    partial:    '<path d="M12 7v6M12 16v.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
    error:      '<path d="M8 8l8 8M16 8l-8 8" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  };
  return `<svg class="meeting-status-svg" viewBox="0 0 24 24" style="--ring-bg:${col.bg}; --ring-fg:${col.ring}; color:${col.fg};" aria-hidden="true">
    <circle cx="12" cy="12" r="${r}" fill="var(--ring-bg)" stroke="none"/>
    <circle class="${ringClass}" cx="12" cy="12" r="${r}" fill="none" stroke="var(--ring-fg)" stroke-width="2" stroke-linecap="round"
      stroke-dasharray="${circumference.toFixed(2)}" stroke-dashoffset="${offset.toFixed(2)}"
      transform="rotate(-90 12 12)" />
    ${glyphs[kind] || glyphs.queued}
  </svg>`;
}

// Source — smartphone (device enrôlé) ou upload (fichier local).
function sourceIcon(isLocal) {
  if (isLocal) {
    // Flèche montante (upload local).
    return `<svg class="meeting-source-svg" viewBox="0 0 24 24" aria-hidden="true">
      <path d="M12 4v12M6 10l6-6 6 6M5 20h14" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
    </svg>`;
  }
  // Smartphone (device PWA / mobile).
  return `<svg class="meeting-source-svg" viewBox="0 0 24 24" aria-hidden="true">
    <rect x="6" y="3" width="12" height="18" rx="2.5" stroke="currentColor" stroke-width="1.8" fill="none"/>
    <line x1="10.5" y1="18" x2="13.5" y2="18" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
  </svg>`;
}

// Chevron ▾ rotatif quand la row est dépliée.
function chevronIcon() {
  return `<svg class="meeting-chevron-svg" viewBox="0 0 24 24" aria-hidden="true">
    <path d="M7 10l5 5 5-5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
  </svg>`;
}

// ── Status mapping ────────────────────────────────────────────────────

function resolveStatus(file) {
  // Si on a déjà le transcript-status en cache, on l'utilise (plus précis).
  const cached = _transcriptCache.get(file.id);
  if (cached && cached.status && TRANSCRIPT_PIPELINE[cached.status]) {
    return TRANSCRIPT_PIPELINE[cached.status];
  }
  // Sinon on retombe sur le pipeline upload (pré-transfert) ou un état
  // par défaut "Transcription IA" si déjà transferred.
  return UPLOAD_PIPELINE[file.status] || { kind: 'queued', pct: 0, label: file.status || 'Inconnu' };
}

// Titre à afficher : suggested_filename (cache) si dispo, sinon
// original_filename. Tronqué côté CSS via text-overflow.
function resolveTitle(file) {
  const cached = _transcriptCache.get(file.id);
  if (cached && cached.suggested) return cached.suggested;
  return file.original_filename || '(sans nom)';
}

// ── Rendu HTML ────────────────────────────────────────────────────────

function renderHeader(fileCount, hasSelection) {
  return `<div class="meetings-tab-header">
    <div class="meetings-tab-header-top">
      <h2 class="meetings-tab-title">Mes réunions <span class="meetings-tab-count">${fileCount}</span></h2>
      <div class="meetings-tab-actions">
        <button type="button" class="meetings-tab-btn meetings-tab-btn--primary"
                data-action="meetings-new:pick-files"
                title="Importer un ou plusieurs fichiers audio">
          + Importer
        </button>
        <button type="button" class="meetings-tab-btn meetings-tab-btn--secondary"
                data-action="meetings-new:pick-folder"
                title="Importer un dossier entier (tous les audios à l'intérieur)">
          + Dossier
        </button>
        <button type="button" class="meetings-tab-btn meetings-tab-btn--ghost"
                data-action="meetings-new:toggle-sort"
                title="Inverser l'ordre de tri (date de réunion)">
          <span data-sort-label>Plus récent d'abord</span>
        </button>
      </div>
    </div>
    <div class="meetings-tab-header-hint">
      Maintenez la touche <kbd>Alt</kbd> pour activer la sélection multiple et supprimer en lot.
    </div>
    ${hasSelection ? `<div class="meetings-tab-bulkbar">
      <span class="meetings-tab-bulkbar-count"><strong>${_selectedIds.size}</strong> réunion(s) sélectionnée(s)</span>
      <button type="button" class="meetings-tab-btn meetings-tab-btn--danger"
              data-action="meetings-new:bulk-delete">
        Mettre à la corbeille
      </button>
      <button type="button" class="meetings-tab-btn meetings-tab-btn--ghost"
              data-action="meetings-new:bulk-clear">
        Annuler
      </button>
    </div>` : ''}
  </div>`;
}

function renderRow(file, session) {
  const status = resolveStatus(file);
  const animated = status.kind === 'processing';
  const title = resolveTitle(file);
  const dateLabel = formatDate(file.meeting_datetime || file.created_at, { withTime: true });
  const durLabel = formatDuration(file.audio_duration_seconds);
  const isExpanded = _expandedIds.has(file.id);
  const isSelected = _selectedIds.has(file.id);
  const isLocal = !!session.is_local_upload;
  // Tooltip status détaillé (rollover sur la pastille).
  const statusTooltip = `${status.label}${file.status_message ? ' — ' + file.status_message : ''}`;
  // Source tooltip : nom device ou "Upload local" + simple_code.
  const sourceLabel = session.device_label || (isLocal ? 'Upload local' : 'Appareil enrôlé');
  const sourceTooltip = `${sourceLabel}${session.simple_code ? ' — code ' + session.simple_code : ''}`;

  return `<div class="meeting-row${isExpanded ? ' is-expanded' : ''}${isSelected ? ' is-selected' : ''}" data-file-id="${escapeHtml(file.id)}">
    <div class="meeting-row-main">
      <label class="meeting-row-check" title="Sélectionner (Alt)">
        <input type="checkbox" data-meeting-check="${escapeHtml(file.id)}" ${isSelected ? 'checked' : ''} />
      </label>
      <div class="meeting-row-status"
           role="status"
           aria-label="Statut : ${escapeHtml(status.label)}"
           title="${escapeHtml(statusTooltip)}">
        ${statusIcon(status.kind, status.pct, animated)}
      </div>
      <button type="button" class="meeting-row-title-btn"
              data-action="meetings-new:toggle-expand"
              data-file-id="${escapeHtml(file.id)}"
              title="${escapeHtml(title)}">
        <span class="meeting-row-title-text">${escapeHtml(title)}</span>
      </button>
      <span class="meeting-row-date" title="Date de la réunion">${escapeHtml(dateLabel)}</span>
      <span class="meeting-row-dur" title="Durée du fichier audio">${escapeHtml(durLabel || '—')}</span>
      <button type="button" class="meeting-row-chevron"
              data-action="meetings-new:toggle-expand"
              data-file-id="${escapeHtml(file.id)}"
              aria-expanded="${isExpanded}"
              aria-label="${isExpanded ? 'Masquer le détail' : 'Voir le détail'}">
        ${chevronIcon()}
      </button>
    </div>
    ${isExpanded ? `<div class="meeting-row-expanded" data-expanded-for="${escapeHtml(file.id)}">
      <div class="meeting-row-expanded-row">
        <span class="meeting-row-source" title="${escapeHtml(sourceTooltip)}">
          ${sourceIcon(isLocal)}
          <span class="meeting-row-source-label">${escapeHtml(sourceLabel)}</span>
        </span>
        <span class="meeting-row-created">
          Importée le ${escapeHtml(formatDate(file.created_at, { withTime: true }))}
        </span>
      </div>
      <div class="meeting-row-summary" data-summary-for="${escapeHtml(file.id)}">
        <em class="meeting-row-summary-loading">Chargement du résumé…</em>
      </div>
      <div class="meeting-row-prep" data-prep-for="${escapeHtml(file.id)}" hidden></div>
      <div class="meeting-row-expanded-actions">
        <button type="button" class="meeting-row-action-btn"
                data-action="meetings-new:open-detail"
                data-file-id="${escapeHtml(file.id)}">
          Ouvrir la fiche complète
        </button>
        <button type="button" class="meeting-row-action-btn meeting-row-action-btn--danger"
                data-action="meetings-new:delete-one"
                data-file-id="${escapeHtml(file.id)}">
          Mettre à la corbeille
        </button>
      </div>
    </div>` : ''}
  </div>`;
}

function renderEmpty() {
  return `<div class="meetings-tab-empty">
    <p><strong>Vous n'avez pas encore importé de réunion.</strong></p>
    <p>Cliquez sur <em>+ Importer</em> ci-dessus pour démarrer, ou enrôlez un téléphone depuis l'onglet <em>Mes appareils</em>.</p>
  </div>`;
}

// ── Render principal ──────────────────────────────────────────────────

export function renderList(sessions) {
  _lastSessions = sessions || [];
  const container = document.getElementById('sessions-list');
  if (!container) return;

  // Aplatit + trie par date desc (date de réunion override sinon upload).
  const entries = [];
  for (const s of (sessions || [])) {
    for (const f of (s.uploads || [])) {
      entries.push({ f, s });
    }
  }
  entries.sort((a, b) => {
    const da = new Date(a.f.meeting_datetime || a.f.created_at).getTime();
    const db = new Date(b.f.meeting_datetime || b.f.created_at).getTime();
    return db - da;
  });

  const headerHtml = renderHeader(entries.length, _selectedIds.size > 0);
  const listHtml = entries.length
    ? entries.map(({ f, s }) => renderRow(f, s)).join('')
    : renderEmpty();
  container.innerHTML = `${headerHtml}<div class="meetings-tab-list">${listHtml}</div>`;

  // Toggle classe body pour CSS bulk-mode (révèle checkboxes).
  document.body.classList.toggle('meetings-bulk-active', _selectedIds.size > 0 || _altPressed);

  // Pour chaque row dépliée, fetch+render le résumé asynchrone.
  for (const id of _expandedIds) {
    _fetchAndRenderSummary(id);
  }
}

// ── Polling résumé enrichi (depuis /api/file/transcript-status) ───────

async function _fetchAndRenderSummary(fileId) {
  const summaryEl = document.querySelector(`[data-summary-for="${cssEscape(fileId)}"]`);
  if (!summaryEl) return;
  try {
    const resp = await fetch(`/api/file/transcript-status/${encodeURIComponent(fileId)}`);
    if (!resp.ok) {
      summaryEl.innerHTML = `<em class="meeting-row-summary-empty">Résumé indisponible (HTTP ${resp.status}).</em>`;
      return;
    }
    const data = await resp.json();
    if (!data || !data.available) {
      summaryEl.innerHTML = `<em class="meeting-row-summary-empty">La transcription n'est pas encore disponible.</em>`;
      return;
    }
    // Mémorise pour le titre + status (utilisé par resolveTitle/resolveStatus
    // lors du prochain render).
    _transcriptCache.set(fileId, {
      status: (data.transcription_status || '').toLowerCase(),
      engine: data.transcription_engine || '',
      suggested: data.suggested_filename || '',
      kp: data.key_points_summary || '',
      fetchedAt: Date.now(),
    });
    const kp = (data.key_points_summary || '').trim();
    summaryEl.innerHTML = kp
      ? `<pre class="meeting-row-summary-kp">${escapeHtml(kp)}</pre>`
      : `<em class="meeting-row-summary-empty">Pas de résumé clé disponible.</em>`;
    // Re-rend le titre + status de la row si les valeurs ont changé (le
    // resolveTitle/resolveStatus relit le cache).
    const row = document.querySelector(`.meeting-row[data-file-id="${cssEscape(fileId)}"]`);
    if (row) {
      const file = _findFile(fileId);
      const session = _findSession(fileId);
      if (file && session) {
        const newHtml = renderRow(file, session);
        const tmp = document.createElement('div');
        tmp.innerHTML = newHtml;
        const fresh = tmp.firstElementChild;
        if (fresh) row.replaceWith(fresh);
      }
    }
  } catch (e) {
    summaryEl.innerHTML = `<em class="meeting-row-summary-empty">Erreur de chargement du résumé.</em>`;
  }
}

function _findFile(fileId) {
  for (const s of _lastSessions) {
    for (const f of (s.uploads || [])) {
      if (f.id === fileId) return f;
    }
  }
  return null;
}
function _findSession(fileId) {
  for (const s of _lastSessions) {
    for (const f of (s.uploads || [])) {
      if (f.id === fileId) return s;
    }
  }
  return null;
}

// CSS.escape polyfill minimal (les UUIDs ne contiennent jamais de chars
// magiques, donc on peut juste retourner tel quel).
function cssEscape(s) { return String(s); }

// ── Délégation d'actions ──────────────────────────────────────────────

function _resolveLegacyFn(name) {
  const fn = window[name];
  return typeof fn === 'function' ? fn : null;
}

function _onClick(ev) {
  // 1) Checkbox de sélection bulk
  const cb = ev.target.closest && ev.target.closest('[data-meeting-check]');
  if (cb && cb.tagName === 'INPUT') {
    const id = cb.getAttribute('data-meeting-check');
    if (cb.checked) _selectedIds.add(id); else _selectedIds.delete(id);
    renderList(_lastSessions);
    return;
  }
  // 2) Actions data-action préfixées meetings-new:
  const el = ev.target && ev.target.closest && ev.target.closest('[data-action]');
  if (!el) return;
  const action = el.getAttribute('data-action') || '';
  if (!action.startsWith('meetings-new:')) return;
  const verb = action.slice('meetings-new:'.length);
  const fileId = el.getAttribute('data-file-id') || '';
  ev.preventDefault();

  switch (verb) {
    case 'toggle-expand': {
      if (_expandedIds.has(fileId)) _expandedIds.delete(fileId);
      else _expandedIds.add(fileId);
      renderList(_lastSessions);
      break;
    }
    case 'open-detail': {
      const fn = _resolveLegacyFn('showFileDetail');
      if (fn) fn(fileId);
      break;
    }
    case 'delete-one': {
      const file = _findFile(fileId);
      if (!file) return;
      const fn = _resolveLegacyFn('deleteFile');
      if (fn) fn(fileId, file.original_filename || '');
      break;
    }
    case 'bulk-delete': {
      _confirmBulkDelete();
      break;
    }
    case 'bulk-clear': {
      _selectedIds.clear();
      renderList(_lastSessions);
      break;
    }
    case 'toggle-sort': {
      const fn = _resolveLegacyFn('toggleSortDir');
      if (fn) fn();
      break;
    }
    case 'pick-files': {
      const input = document.getElementById('local-upload-files-input');
      if (input) input.click();
      break;
    }
    case 'pick-folder': {
      const input = document.getElementById('local-upload-folder-input');
      if (input) input.click();
      break;
    }
    default:
      break;
  }
}

async function _confirmBulkDelete() {
  const ids = Array.from(_selectedIds);
  if (ids.length === 0) return;
  const ok = window.confirm(`Mettre ${ids.length} réunion(s) à la corbeille ?\n\n(Suppression définitive automatique sous 30 jours, restaurable d'ici là.)`);
  if (!ok) return;
  let okCount = 0, failed = 0;
  for (const id of ids) {
    try {
      const resp = await fetch(`/api/file/${encodeURIComponent(id)}`, { method: 'DELETE' });
      if (resp.ok) okCount++;
      else failed++;
    } catch (e) {
      failed++;
    }
  }
  _selectedIds.clear();
  if (failed > 0) {
    window.alert(`${okCount} supprimé(s), ${failed} échec(s). Rechargez la page pour vérifier l'état.`);
  }
  const fn = _resolveLegacyFn('loadSessions');
  if (fn) fn({ force: true });
}

// ── Touche Alt → mode bulk visible ────────────────────────────────────

function _onKeyDown(e) {
  if (e.key === 'Alt' && !_altPressed) {
    _altPressed = true;
    document.body.classList.add('meetings-bulk-active');
  }
}
function _onKeyUp(e) {
  if (e.key === 'Alt' && _altPressed) {
    _altPressed = false;
    // Si rien n'est sélectionné, on retire la classe (sinon on garde la
    // barre d'actions visible tant qu'il y a une sélection).
    if (_selectedIds.size === 0) {
      document.body.classList.remove('meetings-bulk-active');
    }
  }
}

// ── Cycle de vie ──────────────────────────────────────────────────────

let _firstLoadDone = false;

export function mount(container /*, ctx */) {
  const panel = container || document.getElementById('panel-transfers');
  if (!panel) return;
  // Marque le panel pour que le CSS cache le header legacy
  // (.dsfr-inline-actions / #sessions-table-header) — notre module
  // rend son propre header dans #sessions-list.
  panel.classList.add('meetings-new-active');
  if (!_delegationBound) {
    panel.addEventListener('click', _onClick);
    document.addEventListener('keydown', _onKeyDown);
    document.addEventListener('keyup', _onKeyUp);
    _delegationBound = true;
  }
  if (_firstLoadDone) {
    const fn = _resolveLegacyFn('loadSessions');
    if (fn) fn({ force: true });
  }
  _firstLoadDone = true;
}

export function unmount(/* container */) {
  // Retire la classe body si on quitte le tab.
  document.body.classList.remove('meetings-bulk-active');
  const stop = _resolveLegacyFn('stopQueueHintDetail');
  if (stop) { try { stop(); } catch (e) { /* silencieux */ } }
}

// ── Ré-exports historiques (compat onclick="" du template legacy) ─────
// Les autres tabs (preparations, useful-data) référencent encore ces
// fonctions globales via les onclick inline. On les ré-expose tels quels
// jusqu'à ce qu'ils soient eux aussi modularisés.
export const loadSessions = window.loadSessions;
export const purgeSessions = window.purgeSessions;
export const deleteSession = window.deleteSession;
export const renewSession = window.renewSession;
export const deleteFile = window.deleteFile;
export const restoreFile = window.restoreFile;
export const restoreSession = window.restoreSession;
export const deleteFilePermanently = window.deleteFilePermanently;
export const showFileDetail = window.showFileDetail;
export const showFilesList = window.showFilesList;
export const renameDetailTitle = window.renameDetailTitle;
export const toggleRowExpand = window.toggleRowExpand;
export const openFileInfoModal = window.openFileInfoModal;
export const loadNormalizationImpact = window.loadNormalizationImpact;
export const saveMeetingDatetime = window.saveMeetingDatetime;
export const resetMeetingDatetime = window.resetMeetingDatetime;
export const toggleSortDir = window.toggleSortDir;
export const handleLocalUploadInput = window.handleLocalUploadInput;
export const uploadLocalFiles = window.uploadLocalFiles;
export const loadTrash = window.loadTrash;
export const toggleAdvancedDl = window.toggleAdvancedDl;
export const loadTranscriptStatus = window.loadTranscriptStatus;
export const updateOtherDownload = window.updateOtherDownload;
export const updateDownloadButtons = window.updateDownloadButtons;

// ── Hook global exposé à legacy.js ────────────────────────────────────
// legacy.js loadSessions() teste si window.__meetingsTab.renderList existe
// AVANT son rendu legacy ; si oui, délègue et return. Cf. legacy.js
// (bloc « rendu sessions-list »).
if (typeof window !== 'undefined') {
  window.__meetingsTab = { mount, unmount, renderList };
}
