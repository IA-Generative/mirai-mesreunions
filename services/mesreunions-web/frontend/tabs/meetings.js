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
// 5 kinds visuels (spec utilisateur 2026-05-17) :
//   queued     : nouveau / en attente (gris, pas d'animation)
//   processing : en cours réel (bleu, anneau qui tourne)
//   success    : fini OK (vert plein, pas d'animation)
//   partial    : partiellement prête (orange, pas d'animation)
//   error      : échec (rouge, pas d'animation)
//
// Pour `transferred` (= pipeline pré-transcription terminé, transcription
// Kevent à venir/en cours/déjà finie), on N'A PAS l'info — on l'affiche
// en kind=queued ("Prête à transcrire") tant qu'un fetch transcript-status
// n'a pas confirmé le vrai état. Le pré-fetch parallèle au render
// (cf. _prefetchTranscriptStatus) corrige rapidement vers success ou
// processing selon le retour Kevent.
const UPLOAD_PIPELINE = {
  pending:              { kind: 'queued',     pct:   0, label: 'En attente' },
  scanning:             { kind: 'processing', pct:  20, label: 'Antivirus' },
  scan_clean:           { kind: 'processing', pct:  35, label: 'Antivirus OK' },
  transcoding:          { kind: 'processing', pct:  50, label: 'Conversion audio' },
  transcoded:           { kind: 'processing', pct:  65, label: 'Audio normalisé' },
  ready_for_transfer:   { kind: 'processing', pct:  70, label: 'Prêt pour transfert' },
  transferring:         { kind: 'processing', pct:  80, label: 'Transfert interne' },
  // transferred = pré-transcription OK. Sans transcript-status, on assume
  // "prête à transcrire" en kind=queued (gris). Le pré-fetch corrige vers
  // success/partial/error/processing une fois la réponse Kevent connue.
  transferred:          { kind: 'queued',     pct:  85, label: 'Prête à transcrire' },
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
let _transcriptCache = new Map(); // fileId → { status, engine, kp, suggested, outputs, fetchedAt }
let _altPressed = false;
let _lastSessions = [];
let _delegationBound = false;
// IDs des rows fraîchement relancées (par "Relancer les bloqués"). Affiche
// un badge persistant "🔄 Relancé à HH:MM" sur chaque row tant que le
// statut transcription n'a pas bougé (signal le watchdog a engagé).
// Map fileId → { at: Date, lastSeenStatus: string }
let _recentlyRelaunched = new Map();

// Stats temporelles du pipeline (medianes RTF + durées par bucket).
// Fetch au mount + refresh toutes les 15min. Utilisé par les tooltips
// (étape "Démarré à / Écoulé / Estimé restant").
let _pipelineStats = null;
let _pipelineStatsLastFetch = 0;
const _PIPELINE_STATS_TTL_MS = 15 * 60 * 1000;

// ── SVG helpers ───────────────────────────────────────────────────────

function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// Pastille status — anneau STATIQUE qui reflète le % d'avancement réel
// (stroke-dashoffset proportionnel à pct), + glyphe central qui pulse
// subtilement quand kind=processing (au lieu d'une rotation continue).
// Cette approche porte 2 informations à la fois :
//   • % d'avancement = arc rempli (visible d'un coup d'œil)
//   • activité en cours = pulse du glyphe (signal vivant subtil)
function statusIcon(kind, pct, animated) {
  const col = KIND_COLORS[kind] || KIND_COLORS.queued;
  const r = 10;
  const circumference = 2 * Math.PI * r;     // ~62.83
  const offset = circumference * (1 - (Math.max(0, Math.min(100, pct)) / 100));
  // Glyphe central par kind. Le pulse est porté par la classe is-active
  // sur le <g> qui contient le glyphe (CSS animation opacity).
  const glyphs = {
    success:    '<path d="M8 12.5l2.5 2.5L16 9" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" fill="none"/>',
    processing: '<circle cx="12" cy="12" r="2.5" fill="currentColor"/>',
    queued:     '<circle cx="12" cy="12" r="1.6" fill="currentColor"/>',
    partial:    '<path d="M12 7v6M12 16v.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
    error:      '<path d="M8 8l8 8M16 8l-8 8" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  };
  const glyphClass = animated ? 'meeting-status-glyph is-pulsing' : 'meeting-status-glyph';
  return `<svg class="meeting-status-svg" viewBox="0 0 24 24" style="--ring-bg:${col.bg}; --ring-fg:${col.ring}; color:${col.fg};" aria-hidden="true">
    <circle cx="12" cy="12" r="${r}" fill="var(--ring-bg)" stroke="none"/>
    <circle class="meeting-status-ring" cx="12" cy="12" r="${r}" fill="none" stroke="var(--ring-fg)" stroke-width="2" stroke-linecap="round"
      stroke-dasharray="${circumference.toFixed(2)}" stroke-dashoffset="${offset.toFixed(2)}"
      transform="rotate(-90 12 12)" />
    <g class="${glyphClass}">${glyphs[kind] || glyphs.queued}</g>
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
        <button type="button" class="meetings-tab-btn meetings-tab-btn--secondary"
                data-action="meetings-new:import-from-mcr"
                title="Importer une ou plusieurs réunions depuis compte-rendu.mirai">
          📥 Depuis MCR
        </button>
        <button type="button" class="meetings-tab-btn meetings-tab-btn--ghost"
                data-action="meetings-new:toggle-sort"
                title="Inverser l'ordre de tri (date de réunion)">
          <span data-sort-label>Plus récent d'abord</span>
        </button>
        <button type="button" class="meetings-tab-btn meetings-tab-btn--ghost"
                data-action="meetings-new:resume-stuck"
                title="Relancer les transcriptions bloquées (>5min sans activité) OU en échec (kevent_failed)">
          🔄 Relancer les bloqués
        </button>
      </div>
    </div>
    <div class="meetings-tab-header-hint">
      Maintenez la touche <kbd>Alt</kbd> pour activer la sélection multiple et supprimer en lot.
    </div>
    ${hasSelection ? `<div class="meetings-tab-bulkbar">
      <span class="meetings-tab-bulkbar-count"><strong>${_selectedIds.size}</strong> réunion(s) sélectionnée(s)</span>
      <div class="bulk-dl-wrap" data-bulk-dl-wrap style="position:relative;display:inline-block;">
        <button type="button" class="meetings-tab-btn meetings-tab-btn--secondary"
                data-action="meetings-new:bulk-download-menu"
                aria-haspopup="menu" aria-expanded="false"
                style="display:inline-flex;align-items:center;gap:0.3rem;">
          ⬇ Télécharger <span aria-hidden="true">▾</span>
        </button>
        <div class="bulk-dl-menu" data-bulk-dl-menu hidden role="menu"
             style="position:absolute;top:100%;left:0;margin-top:0.25rem;
                    min-width:300px;background:#fff;border:1px solid #cbd5e1;
                    border-radius:0.3rem;box-shadow:0 10px 30px rgba(0,0,0,0.15);
                    z-index:50;padding:0.3rem 0;font-size:0.85rem;">
          <!-- Rempli dynamiquement par _renderBulkDownloadMenu lors de l'ouverture -->
        </div>
      </div>
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

// Format "HH:MM" → "il y a X min" lisible.
function _fmtElapsed(ms) {
  if (!ms || ms < 0) return '';
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const sec = s % 60;
  if (m < 60) return sec ? `${m}min ${sec}s` : `${m}min`;
  const h = Math.floor(m / 60);
  return `${h}h${m % 60}min`;
}

// Estime le temps restant pour un job en cours, sur base des stats
// médianes (median_rtf × durée audio) - temps écoulé. Retourne ''
// quand on n'a pas assez d'info (stats absentes, durée inconnue, etc).
function _estimateRemaining(file, elapsedMs) {
  if (!_pipelineStats || !_pipelineStats.median_rtf || !file.audio_duration_seconds) return '';
  const expectedTotalS = file.audio_duration_seconds * _pipelineStats.median_rtf;
  const elapsedS = elapsedMs / 1000;
  const remainingS = expectedTotalS - elapsedS;
  if (remainingS < 5) return '< 1min';
  return _fmtElapsed(remainingS * 1000);
}

// Compte la position dans la file d'attente pour un job en queue.
// Approximation : count des jobs non-terminaux antérieurs (last_activity
// ou created plus ancien) — précision suffisante pour un tooltip.
function _queuePosition(file) {
  let pos = 1;
  const myTs = new Date(file.created_at).getTime();
  for (const s of _lastSessions || []) {
    for (const f of (s.uploads || [])) {
      if (f.id === file.id) continue;
      const cached = _transcriptCache.get(f.id);
      const st = (cached && cached.status) || '';
      if (!['kevent_queued','kevent_transcribing','kevent_processing','pending','transcoding']
            .includes(st) && f.status !== 'pending' && f.status !== 'transcoding') continue;
      const ts = new Date(f.created_at).getTime();
      if (ts < myTs) pos++;
    }
  }
  return pos;
}

// Tooltip détaillé du pipeline pour rollover sur la pastille status.
// Donne l'étape courante + status_message backend + engine + timing
// (Démarré / Écoulé / Estimé restant) + position file quand pertinent.
function _buildStatusTooltip(file, status) {
  const lines = [status.label];
  if (file.status_message) lines.push('— ' + file.status_message);
  const cached = _transcriptCache.get(file.id);
  if (cached && cached.engine) lines.push('Moteur : ' + cached.engine);
  if (cached && cached.status) lines.push('État transcription : ' + cached.status);

  // Lignes de timing pour les jobs en cours.
  const inProgress = status.kind === 'processing';
  const cachedStatus = (cached && cached.status) || '';
  // Distinction wait (queued) vs processing (transcribing/processing) :
  // - en queue : on affiche "Attente estimée" = position × médiane durée job
  // - en traitement actif : on affiche "Écoulé" + "Estimé restant"
  const isQueued = cachedStatus === 'kevent_queued' || cachedStatus === 'pending';
  const isActive = cachedStatus === 'kevent_processing' ||
                   cachedStatus === 'kevent_transcribing' ||
                   cachedStatus === 'transcoding';

  if (inProgress) {
    const startedAt = file.transcription_started_at || (cached && cached.startedAt);
    const pos = _queuePosition(file);

    if (isQueued && _pipelineStats && _pipelineStats.median_total_s) {
      // Attente estimée = position × médiane totale (worst case
      // séquentiel). Sur-estimation acceptable car plusieurs pods peuvent
      // traiter en parallèle, mais ça borne supérieurement.
      lines.push('');
      const waitS = pos * _pipelineStats.median_total_s;
      lines.push(`⏳ En file d'attente — ~${_fmtElapsed(waitS * 1000)} avant traitement`);
      if (pos > 1) lines.push(`📊 ${pos}ᵉ sur ${_pipelineStats.queue_depth || pos} jobs en file`);
      if (startedAt) {
        const enqueuedAt = new Date(startedAt);
        lines.push(`⏱ En file depuis ${enqueuedAt.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' })}`);
      }
    } else if (startedAt) {
      const startDate = new Date(startedAt);
      const elapsedMs = Date.now() - startDate.getTime();
      lines.push('');
      lines.push(`⏱ Démarré à ${startDate.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' })}`);
      lines.push(`⏱ Écoulé : ${_fmtElapsed(elapsedMs)}`);
      const eta = _estimateRemaining(file, elapsedMs);
      if (eta) lines.push(`⏱ Estimé restant : ${eta}`);
      if (pos > 1) lines.push(`📊 ${pos}ᵉ dans la file`);
    } else if (pos > 1) {
      lines.push('');
      lines.push(`📊 ${pos}ᵉ dans la file`);
    }

    // Note sur la fraîcheur des stats (au cas où l'utilisateur s'étonne
    // de la valeur). 15 min de TTL.
    if (_pipelineStats && _pipelineStats.sample_size) {
      lines.push(`   (stats sur ${_pipelineStats.sample_size} transcriptions, MAJ /15min)`);
    }
  }

  // Badge "relancé" persistant tant que le statut n'a pas bougé.
  const relaunch = _recentlyRelaunched.get(file.id);
  if (relaunch) {
    lines.push('');
    lines.push(`🔄 Relancé à ${relaunch.at.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' })} (en attente d'activité)`);
  }

  lines.push('');
  lines.push('Clic = ouvrir la fiche complète');
  lines.push('▾ = afficher le résumé inline');
  return lines.join('\n');
}

async function _fetchPipelineStats() {
  if (_pipelineStats && (Date.now() - _pipelineStatsLastFetch) < _PIPELINE_STATS_TTL_MS) {
    return _pipelineStats;
  }
  try {
    const resp = await fetch('/api/pipeline/timing-stats');
    if (!resp.ok) return null;
    _pipelineStats = await resp.json();
    _pipelineStatsLastFetch = Date.now();
    return _pipelineStats;
  } catch (e) {
    return null;
  }
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
  const statusTooltip = _buildStatusTooltip(file, status);
  const sourceLabel = session.device_label || (isLocal ? 'Upload local' : 'Appareil enrôlé');
  const sourceTooltip = `${sourceLabel}${session.simple_code ? ' — code ' + session.simple_code : ''}`;

  return `<div class="meeting-row${isExpanded ? ' is-expanded' : ''}${isSelected ? ' is-selected' : ''}" data-file-id="${escapeHtml(file.id)}">
    <div class="meeting-row-main">
      <label class="meeting-row-check" title="Sélectionner (Alt)">
        <input type="checkbox" data-meeting-check="${escapeHtml(file.id)}" ${isSelected ? 'checked' : ''} />
      </label>
      <button type="button" class="meeting-row-status"
              data-action="meetings-new:open-detail"
              data-file-id="${escapeHtml(file.id)}"
              data-status-host="${escapeHtml(file.id)}"
              aria-label="Statut : ${escapeHtml(status.label)} — clic pour ouvrir la fiche"
              title="${escapeHtml(statusTooltip)}">
        ${statusIcon(status.kind, status.pct, animated)}
      </button>
      <div class="meeting-row-title-wrap">
        <button type="button" class="meeting-row-title-btn"
                data-action="meetings-new:open-detail"
                data-file-id="${escapeHtml(file.id)}"
                title="${escapeHtml(title)} — clic pour ouvrir la fiche complète">
          <span class="meeting-row-title-text" data-title-for="${escapeHtml(file.id)}">${escapeHtml(title)}</span>
        </button>
        <button type="button" class="meeting-row-chevron"
                data-action="meetings-new:toggle-expand"
                data-file-id="${escapeHtml(file.id)}"
                aria-expanded="${isExpanded}"
                aria-label="${isExpanded ? 'Masquer le résumé inline' : 'Afficher le résumé inline'}"
                title="${isExpanded ? 'Masquer le résumé' : 'Afficher le résumé inline'}">
          ${chevronIcon()}
        </button>
      </div>
      <span class="meeting-row-date" title="Date de la réunion">${escapeHtml(dateLabel)}</span>
      <span class="meeting-row-dur" title="Durée du fichier audio">${escapeHtml(durLabel || '—')}</span>
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

  // Pré-fetch transcript-status pour TOUS les fichiers `transferred` sans
  // cache. Permet d'afficher rapidement le bon kind visuel (success/partial/
  // error/processing) au lieu de laisser kind=queued par défaut. Le call
  // updateRowStatus() update juste la pastille + tooltip de la row
  // concernée, pas tout le DOM.
  for (const { f } of entries) {
    if (f.status === 'transferred' && !_transcriptCache.has(f.id)) {
      _prefetchTranscriptStatus(f.id);
    }
  }

  // Pour chaque row dépliée, fetch+render le résumé asynchrone.
  for (const id of _expandedIds) {
    _fetchAndRenderSummary(id);
  }
}

// Fetch transcript-status d'un fichier `transferred`, met en cache,
// et update SEULEMENT la pastille status + le titre de la row concernée.
// N'altère pas le reste de la row (pas de re-render complet).
async function _prefetchTranscriptStatus(fileId) {
  try {
    // ?summary=1 → on n'a besoin que de status/engine/suggested/key_points,
    // pas des blobs texte (speaker_tagged/cleaned/reformulated/absentee).
    // Évite de tirer ~plusieurs Mo inutiles à chaque tick de polling.
    const resp = await fetch(`/api/file/transcript-status/${encodeURIComponent(fileId)}?summary=1`);
    if (!resp.ok) return;
    const data = await resp.json();
    if (!data || !data.available) return;
    const newStatus = (data.transcription_status || '').toLowerCase();
    _transcriptCache.set(fileId, {
      status: newStatus,
      engine: data.transcription_engine || '',
      suggested: data.suggested_filename || '',
      kp: data.key_points_summary || '',
      outputs: data.outputs || {},
      // transcription_started_at exposé par transcript-status (fiche
      // détaillée) — utilisé pour calculer Écoulé / Estimé restant.
      startedAt: data.transcription_started_at || null,
      fetchedAt: Date.now(),
    });
    // Auto-clear du badge "Relancé" : si le statut a bougé depuis la
    // relance, le pipeline a effectivement repris.
    const relaunch = _recentlyRelaunched.get(fileId);
    if (relaunch && newStatus && newStatus !== relaunch.lastSeenStatus) {
      _recentlyRelaunched.delete(fileId);
    }
    updateRowStatus(fileId);
  } catch (e) {
    // Silencieux — un échec de pre-fetch n'est pas critique (la pastille
    // restera en kind=queued au lieu de success, mais reste utilisable).
  }
}

// Met à jour SEULEMENT la pastille status + tooltip + titre d'une row
// donnée, sans replaceWith() qui casse les listeners. Appelé après un
// pre-fetch de transcript-status.
function updateRowStatus(fileId) {
  const file = _findFile(fileId);
  if (!file) return;
  const status = resolveStatus(file);
  const animated = status.kind === 'processing';
  // Remplace le contenu SVG de la pastille (innerHTML léger).
  const statusBtn = document.querySelector(`[data-status-host="${cssEscape(fileId)}"]`);
  if (statusBtn) {
    statusBtn.innerHTML = statusIcon(status.kind, status.pct, animated);
    statusBtn.setAttribute('title', _buildStatusTooltip(file, status));
    statusBtn.setAttribute('aria-label', `Statut : ${status.label} — clic pour ouvrir la fiche`);
  }
  // Update le titre si suggested_filename est devenu disponible.
  const titleEl = document.querySelector(`[data-title-for="${cssEscape(fileId)}"]`);
  if (titleEl) {
    titleEl.textContent = resolveTitle(file);
  }
}

// ── Polling résumé enrichi (depuis /api/file/transcript-status) ───────

async function _fetchAndRenderSummary(fileId) {
  const summaryEl = document.querySelector(`[data-summary-for="${cssEscape(fileId)}"]`);
  if (!summaryEl) return;
  try {
    // Liste : on n'utilise que key_points_summary + suggested_filename →
    // mode summary.
    const resp = await fetch(`/api/file/transcript-status/${encodeURIComponent(fileId)}?summary=1`);
    if (!resp.ok) {
      summaryEl.innerHTML = `<em class="meeting-row-summary-empty">Résumé indisponible (HTTP ${resp.status}).</em>`;
      return;
    }
    const data = await resp.json();
    if (!data || !data.available) {
      summaryEl.innerHTML = `<em class="meeting-row-summary-empty">La transcription n'est pas encore disponible (le pipeline Kevent peut prendre quelques minutes).</em>`;
      return;
    }
    // Mémorise pour le titre + status (utilisé par resolveTitle/resolveStatus
    // sur prochain render via loadSessions polling).
    const newStatus2 = (data.transcription_status || '').toLowerCase();
    _transcriptCache.set(fileId, {
      status: newStatus2,
      engine: data.transcription_engine || '',
      suggested: data.suggested_filename || '',
      kp: data.key_points_summary || '',
      outputs: data.outputs || {},
      startedAt: data.transcription_started_at || null,
      fetchedAt: Date.now(),
    });
    const relaunch2 = _recentlyRelaunched.get(fileId);
    if (relaunch2 && newStatus2 && newStatus2 !== relaunch2.lastSeenStatus) {
      _recentlyRelaunched.delete(fileId);
    }
    const kp = (data.key_points_summary || '').trim();
    summaryEl.innerHTML = kp
      ? `<pre class="meeting-row-summary-kp">${escapeHtml(kp)}</pre>`
      : `<em class="meeting-row-summary-empty">Pas de résumé clé disponible.</em>`;
    // Update juste la pastille + titre de la row (in-place, sans casser
    // les listeners ni la sélection bulk).
    updateRowStatus(fileId);
  } catch (e) {
    summaryEl.innerHTML = `<em class="meeting-row-summary-empty">Erreur de chargement du résumé : ${escapeHtml(e.message || e)}.</em>`;
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
    case 'bulk-download-menu': {
      _toggleBulkDownloadMenu(ev);
      break;
    }
    case 'bulk-download-audio':
    case 'bulk-download-cr':
    case 'bulk-download-reformulated':
    case 'bulk-download-cleaned': {
      const kind = action.replace('bulk-download-', '');
      _runBulkDownload(kind);
      break;
    }
    case 'toggle-sort': {
      const fn = _resolveLegacyFn('toggleSortDir');
      if (fn) fn();
      break;
    }
    case 'resume-stuck': {
      _resumeStuckJobs(ev);
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
    case 'import-from-mcr': {
      _openMcrImportModal();
      break;
    }
    default:
      break;
  }
}

// ── Bulk download dropdown ────────────────────────────────────────────
//
// Le menu liste les 4 formats (audio / cr / reformulated / cleaned) avec
// un compteur "X / N prêts" calculé depuis _transcriptCache (rempli par
// le polling ?summary=1 des rows transferred). Items à 0/N sont grisés
// + ⏳, les autres déclenchent un POST /api/files/bulk-download/<kind>
// qui retourne un ZIP streamé.

const _BULK_KIND_META = {
  audio:        { icon: '🎵', label: 'Audio (interne)',  flagKey: 'transferred' },
  cr:           { icon: '📋', label: 'Compte-rendu',     flagKey: 'meeting-cr'  },
  reformulated: { icon: '✍️', label: 'Reformulation',     flagKey: 'transcript-reformulated' },
  cleaned:      { icon: '🧹', label: 'Nettoyée',         flagKey: 'transcript-cleaned' },
};

// Pour chaque kind, retourne {available: N, total: N, missing: [filename...]}
// en lisant les caches _transcriptCache + les flags transferred_available
// portés par chaque file row côté window.sessions.
function _computeBulkAvailability(fileIds) {
  const out = {};
  for (const kind of Object.keys(_BULK_KIND_META)) {
    out[kind] = { available: 0, total: fileIds.length, missing: [] };
  }
  for (const fid of fileIds) {
    const file = _findFile(fid);
    const cached = _transcriptCache.get(fid);
    const title = (file && (file.suggested_filename || file.original_filename)) || fid.slice(0, 8);
    // Audio interne = transferred_available porté par le row.
    if (file && file.transferred_available) {
      out.audio.available += 1;
    } else {
      out.audio.missing.push(title);
    }
    // CR / reformulated / cleaned : depuis transcript-status cache.
    const outputs = (cached && cached.outputs) || {};
    for (const kind of ['cr', 'reformulated', 'cleaned']) {
      const flag = _BULK_KIND_META[kind].flagKey;
      if (outputs[flag]) out[kind].available += 1;
      else out[kind].missing.push(title);
    }
  }
  return out;
}

function _renderBulkDownloadMenu(menuEl, fileIds) {
  const avail = _computeBulkAvailability(fileIds);
  const inProgressCount = fileIds.filter((fid) => {
    const cached = _transcriptCache.get(fid);
    const st = (cached && cached.status) || '';
    // statuses non-terminaux = pipeline encore en route
    return !['kevent_completed', 'kevent_partially_completed', 'kevent_failed',
             'completed', 'failed'].includes(st);
  }).length;

  const itemsHtml = Object.keys(_BULK_KIND_META).map((kind) => {
    const meta = _BULK_KIND_META[kind];
    const { available, total, missing } = avail[kind];
    const disabled = available === 0;
    const partial = available > 0 && available < total;
    const hourglass = (disabled || partial) ? ' ⏳' : '';
    const tooltip = disabled
      ? `Aucun fichier prêt pour ce format — réessayer plus tard (${total} en pipeline).`
      : (partial
          ? `${total - available} fichier(s) encore en pipeline et ignoré(s) :\n· ${missing.slice(0, 10).join('\n· ')}${missing.length > 10 ? `\n… et ${missing.length - 10} autre(s)` : ''}`
          : `${available} fichier(s) prêt(s) → ZIP`);
    return `
      <button type="button" role="menuitem"
              class="bulk-dl-item"
              ${disabled ? 'disabled' : `data-action="meetings-new:bulk-download-${kind}"`}
              title="${escapeHtml(tooltip)}"
              style="display:flex;width:100%;align-items:center;gap:0.5rem;
                     padding:0.4rem 0.7rem;border:0;background:transparent;
                     text-align:left;cursor:${disabled ? 'not-allowed' : 'pointer'};
                     color:${disabled ? '#94a3b8' : '#0f172a'};">
        <span style="font-size:1rem;">${meta.icon}</span>
        <span style="flex:1;">${meta.label}${hourglass}</span>
        <span style="color:${disabled ? '#cbd5e1' : '#64748b'};font-variant-numeric:tabular-nums;">
          ${available} / ${total}
        </span>
      </button>
    `;
  }).join('');

  menuEl.innerHTML = `
    <div style="padding:0.4rem 0.7rem;color:#64748b;font-size:0.72rem;border-bottom:1px solid #f1f5f9;">
      ⬇ Télécharger en masse pour ${fileIds.length} réunion(s)
    </div>
    ${itemsHtml}
    ${inProgressCount > 0 ? `
    <div style="padding:0.4rem 0.7rem;color:#0c4498;font-size:0.72rem;
                border-top:1px solid #f1f5f9;background:#f0f6ff;">
      ⏳ ${inProgressCount} réunion(s) encore en pipeline. Le ZIP n'inclura que les prêts.
    </div>` : ''}
  `;
}

function _toggleBulkDownloadMenu(ev) {
  const wrap = ev.target.closest && ev.target.closest('[data-bulk-dl-wrap]');
  if (!wrap) return;
  const menu = wrap.querySelector('[data-bulk-dl-menu]');
  const btn = wrap.querySelector('[data-action="meetings-new:bulk-download-menu"]');
  if (!menu || !btn) return;
  const willOpen = menu.hidden;
  // Fermer les autres menus éventuels (si on multiplie un jour).
  document.querySelectorAll('[data-bulk-dl-menu]').forEach((m) => { m.hidden = true; });
  if (willOpen) {
    _renderBulkDownloadMenu(menu, Array.from(_selectedIds));
    menu.hidden = false;
    btn.setAttribute('aria-expanded', 'true');
    // Fermer au clic extérieur (1 frame plus tard pour ne pas se fermer
    // soi-même).
    setTimeout(() => {
      const onClickOutside = (e) => {
        if (!wrap.contains(e.target)) {
          menu.hidden = true;
          btn.setAttribute('aria-expanded', 'false');
          document.removeEventListener('click', onClickOutside);
        }
      };
      document.addEventListener('click', onClickOutside);
    }, 0);
  } else {
    btn.setAttribute('aria-expanded', 'false');
  }
}

async function _runBulkDownload(kind) {
  const ids = Array.from(_selectedIds);
  if (ids.length === 0) return;
  // Ferme le menu immédiatement.
  document.querySelectorAll('[data-bulk-dl-menu]').forEach((m) => { m.hidden = true; });
  // Indicateur de progression léger sur le bouton.
  const btn = document.querySelector('[data-action="meetings-new:bulk-download-menu"]');
  const originalLabel = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.textContent = 'Préparation du ZIP…'; }
  try {
    const resp = await fetch(`/api/files/bulk-download/${encodeURIComponent(kind)}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file_ids: ids }),
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      window.alert(`Téléchargement impossible : ${data.error || ('HTTP ' + resp.status)}`);
      return;
    }
    const skipped = parseInt(resp.headers.get('X-Skipped-Files') || '0', 10);
    const included = parseInt(resp.headers.get('X-Included-Files') || '0', 10);
    // Trigger download du blob via lien temporaire.
    const blob = await resp.blob();
    // Récupère le filename suggéré par le serveur via Content-Disposition.
    const cd = resp.headers.get('Content-Disposition') || '';
    const m = cd.match(/filename\*?=(?:UTF-8'')?["']?([^"';\n]+)/);
    const filename = m ? decodeURIComponent(m[1]) : `${kind}_export.zip`;
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = filename;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
    // Toast informatif si certains fichiers ont été skippés.
    if (skipped > 0 && window.showToast) {
      window.showToast(`✓ ${included} fichier(s) — ${skipped} ignoré(s) (pas encore prêts)`, 'success');
    } else if (window.showToast) {
      window.showToast(`✓ ${included} fichier(s) téléchargé(s)`, 'success');
    }
  } catch (e) {
    window.alert(`Erreur réseau : ${e.message}`);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = originalLabel; }
  }
}

async function _resumeStuckJobs(ev) {
  const btn = ev && ev.target && ev.target.closest('[data-action="meetings-new:resume-stuck"]');
  const originalLabel = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Recherche…'; }
  // Toast persistant pendant l'analyse — sinon le user ne sait pas si l'app
  // a entendu le clic. Disparait remplacé par le résultat final.
  if (window.showToast) {
    window.showToast('🔍 Recherche des transcriptions à relancer…', 'info', 8000);
  }
  try {
    const resp = await fetch('/api/files/resume-stuck-jobs', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      // Bouton manuel : on inclut aussi les kevent_failed (action user
      // explicite, opt-in). Le tick automatique reste sur les
      // non-terminaux uniquement pour éviter les boucles de retry.
      body: JSON.stringify({ limit: 20, include_failed: true }),
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      window.alert(`Échec : ${data.error || ('HTTP ' + resp.status)}`);
      return;
    }
    const data = await resp.json();
    const claimed = data.claimed || 0;
    const resumedList = Array.isArray(data.resumed) ? data.resumed : [];
    if (claimed === 0) {
      if (window.showToast) window.showToast('Aucune transcription bloquée à relancer.', 'info');
      else window.alert('Aucune transcription bloquée à relancer.');
      return;
    }
    // Construit la liste lisible des titres relancés (depuis le cache
    // local) + déclenche un highlight visuel sur les rows correspondantes.
    const titles = [];
    const ids = [];
    for (const r of resumedList) {
      const fid = r.audio_id;
      if (!fid) continue;
      ids.push(fid);
      const f = _findFile(fid);
      const title = (f && (f.suggested_filename || f.original_filename)) || fid.slice(0, 8);
      titles.push(title);
      // Marque comme relancé : un badge persistant "🔄 Relancé à HH:MM"
      // s'affiche jusqu'à ce que le statut transcription bouge (cf
      // _checkRelaunchClear appelé au polling).
      _recentlyRelaunched.set(fid, {
        at: new Date(),
        lastSeenStatus: (_transcriptCache.get(fid) || {}).status || '',
      });
      // Invalide le cache transcript pour ce fileId — le polling va
      // refetch le nouveau statut depuis transcript-status.
      _transcriptCache.delete(fid);
    }
    // Detail explicite dans le toast (et fallback alert).
    const sample = titles.slice(0, 5).join(', ');
    const more = titles.length > 5 ? ` (+${titles.length - 5})` : '';
    const msg = `🔄 ${claimed} transcription(s) relancée(s) : ${sample}${more}`;
    if (window.showToast) window.showToast(msg, 'success');
    else window.alert(msg);

    // Refresh la liste pour montrer les nouveaux statuts ("kevent_queued").
    const reload = _resolveLegacyFn('loadSessions');
    if (reload) await reload({ force: true });

    // Highlight visuel temporaire (3.5s) sur les rows relancées pour que
    // l'utilisateur voie EXACTEMENT lesquelles ont été reprises. CSS
    // animation injectée à la volée (évite de toucher un fichier .css
    // partagé pour 1 utilisation ponctuelle).
    _ensureRelaunchHighlightStyle();
    setTimeout(() => {
      for (const fid of ids) {
        const row = document.querySelector(`.meeting-row[data-file-id="${cssEscape(fid)}"]`);
        if (row) {
          row.classList.add('meeting-row--just-relaunched');
          // Scroll au 1er en vue (en mode "nearest" pour ne pas brusquer).
          if (fid === ids[0]) row.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
          setTimeout(() => row.classList.remove('meeting-row--just-relaunched'), 3500);
        }
      }
    }, 50);  // 50ms : laisse le DOM finir le re-render
  } catch (e) {
    window.alert(`Erreur réseau : ${e.message}`);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = originalLabel; }
  }
}

// Injecte le CSS du highlight relance UNE FOIS (idempotent).
function _ensureRelaunchHighlightStyle() {
  if (document.getElementById('meeting-row-relaunch-style')) return;
  const st = document.createElement('style');
  st.id = 'meeting-row-relaunch-style';
  st.textContent = `
    @keyframes meetingRowRelaunchPulse {
      0%   { background-color: #fef3c7; box-shadow: inset 3px 0 0 #ca8a04; }
      40%  { background-color: #fef9c3; box-shadow: inset 3px 0 0 #ca8a04; }
      100% { background-color: transparent; box-shadow: inset 3px 0 0 transparent; }
    }
    .meeting-row--just-relaunched {
      animation: meetingRowRelaunchPulse 3.5s ease-out;
    }
  `;
  document.head.appendChild(st);
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
  // Pré-charge les stats temporelles (cache 15min) pour que le calcul
  // d'ETA dans les tooltips soit dispo dès le 1er rollover.
  _fetchPipelineStats();
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


// ── MCR import modal ──────────────────────────────────────────────────
//
// Liste les réunions de compte-rendu.mirai pour l'utilisateur connecté,
// permet d'en cocher plusieurs, et déclenche un import asynchrone côté
// backend. Cf services/mesreunions-web/app/modules/mcr_import/routes.py.

let _mcrModalEl = null;

function _openMcrImportModal() {
  _closeMcrImportModal();
  const overlay = document.createElement('div');
  overlay.className = 'mcr-modal-overlay';
  overlay.style.cssText = (
    'position:fixed;inset:0;background:rgba(0,0,0,0.4);z-index:1000;' +
    'display:flex;align-items:center;justify-content:center;'
  );
  overlay.innerHTML = `
    <div class="mcr-modal" role="dialog" aria-modal="true"
         style="background:#fff;border-radius:0.5rem;width:min(900px,90vw);
                max-height:85vh;display:flex;flex-direction:column;
                box-shadow:0 20px 60px rgba(0,0,0,0.3);">
      <div style="padding:1rem 1.25rem;border-bottom:1px solid #e5e7eb;
                  display:flex;align-items:center;justify-content:space-between;">
        <h3 style="margin:0;font-size:1.1rem;">📥 Importer depuis MCR</h3>
        <button type="button" data-mcr-action="close"
                style="background:none;border:0;font-size:1.5rem;cursor:pointer;
                       line-height:1;color:#64748b;">×</button>
      </div>
      <div style="padding:0.75rem 1.25rem;display:flex;gap:0.5rem;align-items:center;
                  border-bottom:1px solid #f1f5f9;">
        <input type="text" data-mcr-search placeholder="Rechercher…"
               style="flex:1;padding:0.4rem 0.6rem;border:1px solid #cbd5e1;
                      border-radius:0.3rem;font-size:0.9rem;">
        <label style="font-size:0.85rem;color:#475569;display:inline-flex;
                      align-items:center;gap:0.3rem;">
          <input type="checkbox" data-mcr-fallback checked>
          Importer la transcription si pas d'audio
        </label>
      </div>
      <div data-mcr-body style="flex:1;overflow:auto;padding:0.5rem 1.25rem;
                                 font-size:0.9rem;">
        <p style="color:#64748b;padding:1rem 0;">Chargement…</p>
      </div>
      <div style="padding:0.75rem 1.25rem;border-top:1px solid #e5e7eb;
                  display:flex;justify-content:space-between;align-items:center;
                  gap:0.5rem;">
        <span data-mcr-status style="font-size:0.85rem;color:#475569;"></span>
        <div style="display:flex;gap:0.5rem;">
          <button type="button" data-mcr-action="export"
                  class="meetings-tab-btn meetings-tab-btn--ghost"
                  title="Télécharge un CSV de toutes tes réunions MCR (toutes pages)">
            📥 Exporter CSV
          </button>
          <button type="button" data-mcr-action="close"
                  class="meetings-tab-btn meetings-tab-btn--ghost">Annuler</button>
          <button type="button" data-mcr-action="submit"
                  class="meetings-tab-btn meetings-tab-btn--primary"
                  disabled>Importer la sélection</button>
        </div>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);
  _mcrModalEl = overlay;

  overlay.addEventListener('click', (ev) => {
    if (ev.target === overlay) {
      _closeMcrImportModal();
    }
    const btn = ev.target.closest('[data-mcr-action]');
    if (!btn) return;
    const action = btn.getAttribute('data-mcr-action');
    if (action === 'close') _closeMcrImportModal();
    if (action === 'submit') _submitMcrImport();
    if (action === 'export') _exportMcrCsv();
  });

  const search = overlay.querySelector('[data-mcr-search]');
  let searchTimer = null;
  search.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => _loadMcrMeetings(1, search.value.trim()), 300);
  });

  _loadMcrMeetings(1, '');
}

function _closeMcrImportModal() {
  if (_mcrModalEl) {
    _mcrModalEl.remove();
    _mcrModalEl = null;
  }
}

async function _loadMcrMeetings(page, search) {
  if (!_mcrModalEl) return;
  const body = _mcrModalEl.querySelector('[data-mcr-body]');
  body.innerHTML = '<p style="color:#64748b;padding:1rem 0;">Chargement…</p>';
  try {
    const params = new URLSearchParams({ page: String(page), page_size: '20' });
    if (search) params.set('search', search);
    const resp = await fetch(`/api/mcr/meetings?${params.toString()}`, {
      credentials: 'same-origin',
    });
    if (resp.status === 401) {
      body.innerHTML = (
        '<p style="color:#b91c1c;">Reconnecte-toi pour activer l\'import depuis MCR ' +
        '(refresh_token absent ou expiré).</p>'
      );
      return;
    }
    if (resp.status === 403) {
      body.innerHTML = (
        '<p style="color:#b91c1c;">Ton compte n\'a pas accès à ' +
        'compte-rendu.mirai.</p>'
      );
      return;
    }
    if (!resp.ok) {
      body.innerHTML = `<p style="color:#b91c1c;">Erreur MCR (HTTP ${resp.status}).</p>`;
      return;
    }
    const data = await resp.json();
    const items = data.data || [];
    if (items.length === 0) {
      body.innerHTML = (
        '<p style="color:#64748b;padding:1rem 0;">Aucune réunion trouvée sur ' +
        'compte-rendu.mirai pour ton compte.</p>'
      );
      return;
    }
    const rows = items.map((m) => {
      const esc = (s) => String(s || '').replace(/[<>&"']/g, (c) => ({
        '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&#39;',
      })[c]);
      // Row "pourrie" côté MCR (validator pydantic) — exposée via _broken=true
      // par le backend pour que l'utilisateur voie EXACTEMENT la position en
      // erreur, sans la cocher (id=null → non importable).
      if (m._broken) {
        const tooltip = esc((m._mcr_error || '').slice(0, 500));
        // Extrait l'indice technique du body MCR : platform_id partiel
        // (ex "...apq-smlr-zlv") et la plateforme (VISIO/WEBCONF/…).
        const platformMatch = /not supported for platform (\w+)/.exec(m._mcr_error || '');
        const platform = platformMatch ? platformMatch[1] : '?';
        const idMatch = /platform_id['\"]?:\s*['\"]([^'\"]+)['\"]/.exec(m._mcr_error || '');
        const partialId = idMatch ? idMatch[1] : '?';
        return `<tr style="background:#fef2f2;color:#991b1b;">
          <td><input type="checkbox" disabled></td>
          <td style="padding:0.4rem 0.5rem;" colspan="3" title="${tooltip}">
            <strong>⚠️ Réunion #${m._slot ?? '?'} non récupérable</strong>
            <div style="font-size:0.75rem;color:#7f1d1d;opacity:0.85;margin-top:2px;">
              plateforme=<code>${esc(platform)}</code>
              · meeting_platform_id=<code>${esc(partialId)}</code>
              — bug serveur MCR (validator pydantic). Survol pour le détail.
            </div>
          </td>
        </tr>`;
      }
      const d = m.start_date || m.creation_date || '';
      const dateStr = d ? new Date(d).toLocaleString('fr-FR') : '';
      return `<tr>
        <td><input type="checkbox" data-mcr-pick value="${m.id}"></td>
        <td style="padding:0.4rem 0.5rem;">${esc(m.name)}</td>
        <td style="padding:0.4rem 0.5rem;color:#64748b;font-size:0.85rem;white-space:nowrap;">${dateStr}</td>
        <td style="padding:0.4rem 0.5rem;color:#64748b;font-size:0.85rem;white-space:nowrap;">${esc(m.status)}</td>
      </tr>`;
    }).join('');
    const fallbackNotice = data._fallback_used ? `
      <div style="background:#fef3c7;border-left:3px solid #f59e0b;
                  padding:0.5rem 0.75rem;margin-bottom:0.5rem;font-size:0.85rem;color:#78350f;">
        ⚠️ ${data._broken_count || 0} réunion(s) sur cette page sont en erreur côté MCR
        (validator pydantic). Les autres sont importables normalement.
      </div>` : '';
    const pager = `
      <div style="display:flex;justify-content:space-between;align-items:center;
                  margin-top:0.75rem;color:#64748b;font-size:0.85rem;">
        <span>${data.total_items || 0} réunion(s) — page ${data.page || page} / ${data.total_pages || 1}</span>
        <div style="display:flex;gap:0.3rem;">
          ${page > 1 ? `<button type="button" data-mcr-page="${page-1}" class="meetings-tab-btn meetings-tab-btn--ghost">‹ Précédent</button>` : ''}
          ${page < (data.total_pages || 1) ? `<button type="button" data-mcr-page="${page+1}" class="meetings-tab-btn meetings-tab-btn--ghost">Suivant ›</button>` : ''}
        </div>
      </div>
    `;
    body.innerHTML = `
      ${fallbackNotice}
      <table style="width:100%;border-collapse:collapse;">
        <thead><tr style="background:#f8fafc;text-align:left;">
          <th style="padding:0.4rem 0.5rem;"></th>
          <th style="padding:0.4rem 0.5rem;">Nom</th>
          <th style="padding:0.4rem 0.5rem;">Date</th>
          <th style="padding:0.4rem 0.5rem;">Statut</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
      ${pager}
    `;
    body.querySelectorAll('[data-mcr-page]').forEach((b) => {
      b.addEventListener('click', () => {
        const p = parseInt(b.getAttribute('data-mcr-page'), 10) || 1;
        _loadMcrMeetings(p, search);
      });
    });
    body.querySelectorAll('[data-mcr-pick]').forEach((cb) => {
      cb.addEventListener('change', _updateMcrSubmitState);
    });
    _updateMcrSubmitState();
  } catch (err) {
    console.error('mcr list error', err);
    body.innerHTML = '<p style="color:#b91c1c;">Erreur réseau.</p>';
  }
}

function _updateMcrSubmitState() {
  if (!_mcrModalEl) return;
  const picks = _mcrModalEl.querySelectorAll('[data-mcr-pick]:checked');
  const submit = _mcrModalEl.querySelector('[data-mcr-action="submit"]');
  const status = _mcrModalEl.querySelector('[data-mcr-status]');
  submit.disabled = picks.length === 0;
  status.textContent = picks.length === 0
    ? ''
    : `${picks.length} réunion(s) sélectionnée(s)`;
}

async function _submitMcrImport() {
  if (!_mcrModalEl) return;
  const picks = Array.from(_mcrModalEl.querySelectorAll('[data-mcr-pick]:checked'))
    .map((cb) => cb.value);
  if (picks.length === 0) return;
  const fallback = _mcrModalEl.querySelector('[data-mcr-fallback]').checked;
  const submit = _mcrModalEl.querySelector('[data-mcr-action="submit"]');
  submit.disabled = true;
  submit.textContent = 'Import en cours…';
  try {
    const resp = await fetch('/api/mcr/import', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ meeting_ids: picks, fallback_transcript: fallback }),
    });
    if (!resp.ok) {
      const err = await resp.text();
      alert(`Échec de l'import : HTTP ${resp.status}\n${err.slice(0, 300)}`);
      submit.disabled = false;
      submit.textContent = 'Importer la sélection';
      return;
    }
    const data = await resp.json().catch(() => ({}));
    const published = data.published ?? picks.length;
    _closeMcrImportModal();
    const msg = `✓ ${published} import(s) MCR lancé(s). Les réunions vont apparaître dans la liste — la transcription et le compte-rendu prennent ~1 min.`;
    if (window.showToast) window.showToast(msg, 'success');
    else alert(msg);
    // Le worker côté ingester met ~2-5s à créer les rows en DB. On fait
    // 3 reloads échelonnés pour rafraîchir l'UI au fil de l'apparition :
    //   - immédiat : peut déjà voir le 1er insert si rapide
    //   - 3s : tous les inserts faits
    //   - 30s : les statuts ont bougé (mcr_transcript_only / kevent_completed)
    const reload = _resolveLegacyFn('loadSessions');
    if (reload) {
      reload();
      setTimeout(() => reload({ force: true }), 3000);
      setTimeout(() => reload({ force: true }), 30000);
    }
  } catch (err) {
    console.error('mcr import error', err);
    alert('Erreur réseau pendant l\'import.');
    submit.disabled = false;
    submit.textContent = 'Importer la sélection';
  }
}


// ── Export CSV de toute la liste MCR ─────────────────────────────────
//
// Boucle GET /api/mcr/meetings sur toutes les pages (page_size=50) jusqu'à
// total_pages, agrège, génère un CSV téléchargeable. Inclut les rows _broken
// (avec marqueur visible) pour que le user ait l'inventaire complet.

async function _exportMcrCsv() {
  if (!_mcrModalEl) return;
  const exportBtn = _mcrModalEl.querySelector('[data-mcr-action="export"]');
  const status = _mcrModalEl.querySelector('[data-mcr-status]');
  const orig = exportBtn.textContent;
  exportBtn.disabled = true;
  exportBtn.textContent = '⏳ Export en cours…';

  const allRows = [];
  let page = 1;
  let totalPages = 1;
  const pageSize = 50;
  try {
    do {
      status.textContent = `Téléchargement page ${page}…`;
      const resp = await fetch(`/api/mcr/meetings?page=${page}&page_size=${pageSize}`, {
        credentials: 'same-origin',
      });
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }
      const data = await resp.json();
      totalPages = data.total_pages || 1;
      const items = data.data || [];
      for (const m of items) allRows.push(m);
      page += 1;
      if (page > 200) break;  // safety stop
    } while (page <= totalPages);

    status.textContent = `Génération CSV (${allRows.length} lignes)…`;
    const csv = _buildMcrCsv(allRows);
    const blob = new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8' });
    const dlUrl = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = dlUrl;
    a.download = `mcr-meetings-${new Date().toISOString().slice(0, 10)}.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(dlUrl);
    status.textContent = `✅ Export terminé (${allRows.length} réunions)`;
  } catch (err) {
    console.error('mcr export error', err);
    alert('Erreur pendant l\'export CSV : ' + err.message);
    status.textContent = '❌ Export échoué';
  } finally {
    exportBtn.disabled = false;
    exportBtn.textContent = orig;
  }
}

function _buildMcrCsv(rows) {
  const cols = ['id', 'name', 'name_platform', 'status', 'creation_date',
                'start_date', 'end_date', 'meeting_platform_id', 'url', 'notes',
                'broken', 'broken_reason'];
  const head = cols.join(';');
  const esc = (v) => {
    if (v === null || v === undefined) return '';
    let s = String(v);
    if (/[";\r\n]/.test(s)) s = '"' + s.replace(/"/g, '""') + '"';
    return s;
  };
  const lines = rows.map((m) => {
    if (m._broken) {
      return [
        '', m.name || '', 'BROKEN', 'BROKEN', '', '', '', '', '', '',
        'true', (m._mcr_error || '').slice(0, 300),
      ].map(esc).join(';');
    }
    return [
      m.id ?? '', m.name ?? '', m.name_platform ?? '', m.status ?? '',
      m.creation_date ?? '', m.start_date ?? '', m.end_date ?? '',
      m.meeting_platform_id ?? '', m.url ?? '', m.notes ?? '',
      'false', '',
    ].map(esc).join(';');
  });
  return [head, ...lines].join('\r\n');
}
