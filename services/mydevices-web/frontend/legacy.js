// Code historique de mydevices-web — extrait verbatim de l'inline JS
// de app/templates/index.html (PR4). Les 2 interpolations Jinja ont
// été remplacées par lecture du bootstrap-data via lib/bootstrap.js.
//
// Pourquoi un seul fichier ? Le JS inline original (~3300 lignes)
// est tightly coupled via ~120 onclick="" qui s'attendent à trouver
// des fonctions globales. Le découper en modules ES avec mount/unmount
// nécessite d'abord d'éliminer ces onclick (event delegation) — déféré
// à PR5. Pour PR4, on extrait le bloc complet, on le bundle via Vite,
// et on documente le découpage logique dans docs/refactor-mydevices-pr4-mapping.md.
//
// Les modules tabs/*.js sont des wrappers minces qui ré-exportent les
// fonctions globales correspondantes pour permettre l'introspection IDE
// et préparer la migration PR5.

import './lib/bootstrap.js';  // doit charger avant tout (publie window.ALLOWED_AUDIO_EXTENSIONS etc.)
import { formatDuration, formatDate } from './utils/format.js';

const impactCache = {};
const impactLoading = new Set();
let showAllDevices = false;
// Ordre de tri courant pour la liste à plat des réunions. Persisté côté
// localStorage pour survivre au reload. "desc" = plus récent d'abord (par
// défaut, le plus naturel après ajout d'un upload).
let _sortDir = (() => {
    try { return localStorage.getItem('mydevices.sort.dir') === 'asc' ? 'asc' : 'desc'; }
    catch (e) { return 'desc'; }
})();
// Colonne de tri (chantier UX-Refonte-3). 'date' = date de réunion (override
// utilisateur sinon created_at) — défaut historique. 'title' = original_filename
// alphabétique. 'duration' = audio_duration_seconds (null → tri en fin).
let _sortKey = (() => {
    try {
        const k = localStorage.getItem('mydevices.sort.key');
        return (k === 'title' || k === 'duration') ? k : 'date';
    } catch (e) { return 'date'; }
})();

function toggleSortDir() {
    _sortDir = (_sortDir === 'desc') ? 'asc' : 'desc';
    try { localStorage.setItem('mydevices.sort.dir', _sortDir); } catch (e) {}
    _refreshSortToggleUi();
    // Re-render à partir du snapshot existant sans rappeler l'API.
    loadSessions({ force: true });
}

// Setter sort-key + sort-dir combinés appelé par les <th> de l'en-tête
// fr-table. Cliquer la même colonne toggle la direction ; cliquer une
// autre colonne réinitialise à 'desc' (cas le plus utile au switch).
window.setSortColumn = function setSortColumn(key) {
    if (key !== 'title' && key !== 'date' && key !== 'duration') return;
    if (_sortKey === key) {
        _sortDir = (_sortDir === 'desc') ? 'asc' : 'desc';
    } else {
        _sortKey = key;
        _sortDir = 'desc';
    }
    try {
        localStorage.setItem('mydevices.sort.key', _sortKey);
        localStorage.setItem('mydevices.sort.dir', _sortDir);
    } catch (e) {}
    _refreshSortToggleUi();
    loadSessions({ force: true });
};

function _refreshSortToggleUi() {
    const btn = document.getElementById('sort-toggle-btn');
    if (btn) {
        const label = btn.querySelector('.sort-toggle-label');
        const arrow = btn.querySelector('.sort-toggle-arrow');
        // Le toggle global agit sur la colonne courante (date par défaut).
        // Libellé spécialisé selon la clé courante pour rester explicite.
        const isDesc = (_sortDir === 'desc');
        if (label) {
            if (_sortKey === 'title') {
                label.textContent = isDesc ? 'Z → A (titre)' : 'A → Z (titre)';
            } else if (_sortKey === 'duration') {
                label.textContent = isDesc ? 'Plus longue d\'abord' : 'Plus courte d\'abord';
            } else {
                label.textContent = isDesc ? 'Plus récent d\'abord' : 'Plus ancien d\'abord';
            }
        }
        if (arrow) arrow.textContent = isDesc ? '▼' : '▲';
    }
    // Reflète l'état actif sur l'en-tête fr-table (flèche colonne).
    const headers = document.querySelectorAll('[data-sort-col]');
    headers.forEach((th) => {
        const col = th.getAttribute('data-sort-col');
        const arrowEl = th.querySelector('.sort-arrow');
        if (col === _sortKey) {
            th.setAttribute('aria-sort', _sortDir === 'desc' ? 'descending' : 'ascending');
            th.classList.add('is-active');
            if (arrowEl) arrowEl.textContent = (_sortDir === 'desc') ? '▼' : '▲';
        } else {
            th.removeAttribute('aria-sort');
            th.classList.remove('is-active');
            if (arrowEl) arrowEl.textContent = '↕';
        }
    });
}
// Map qr_token → {device_name, status, retention_expires_at} populée par
// loadDevices. Sert à enrichir l'en-tête de chaque session dans la liste
// des transferts (montre "iPhone CODE (active)" au lieu de juste "CODE").
const _devicesByQrToken = {};
// Publication globale : tabs/devices.js (loadDevices) écrit dans ce même
// objet (par référence) pour que loadSessions ci-dessous lise les libellés
// device sans re-fetch.
window._devicesByQrToken = _devicesByQrToken;
// État vue transferts : null = liste compacte, fileId = vue détail pour
// ce fichier. Switch via showFileDetail / showFilesList.
let _detailFileId = null;
function showFileDetail(fileId) {
    _detailFileId = fileId;
    activateTab('transfers');
    // Active le mode "page détail" : masque le titre + bouton purge +
    // bordure carte pour donner l'illusion d'une vraie page dédiée.
    const pane = document.querySelector('.tab-pane[data-tab="transfers"]');
    if (pane) pane.classList.add('detail-active');
    // Skeleton immédiat dans #sessions-list — masque la latence du
    // loadSessions({force:true}) qui ré-appelle /api/my-sessions et
    // re-render toute la liste avant d'afficher la fiche détail (chantier
    // UX-Refonte-3 #2). Le skeleton sera remplacé dès que rowsHtml est prêt.
    const container = document.getElementById('sessions-list');
    if (container) {
        container.innerHTML = '<div class="skeleton-fade-in" data-skeleton="1" style="padding:0.5rem 0.4rem;">' +
            '<div class="skeleton-line skeleton-line--title"></div>' +
            '<div class="skeleton-line skeleton-line--short"></div>' +
            '<div class="skeleton-line skeleton-line--mid"></div>' +
            '<div class="skeleton-line"></div>' +
            '<div class="skeleton-line skeleton-line--block"></div>' +
            '</div>';
    }
    const header = document.getElementById('sessions-table-header');
    if (header) header.style.display = 'none';
    loadSessions({ force: true });
    requestAnimationFrame(() => window.scrollTo(0, 0));
}
function showFilesList() {
    _detailFileId = null;
    const pane = document.querySelector('.tab-pane[data-tab="transfers"]');
    if (pane) pane.classList.remove('detail-active');
    loadSessions({ force: true });
}
// Toggle l'affichage de la zone résumé sous une ligne compacte. Le
// chevron tourne (CSS) selon la classe is-open.
// ── Info-bulle file d'attente Kevent (vue détail) ───────────────────────
// Poll 10s vers /api/queue-status. Format texte conformément à la spec.
const _TERMINAL_TS = new Set([
    'completed','kevent_completed','failed','kevent_failed',
    'kevent_partially_completed','mcr_pushed','mcr_auth_failed',
    'mcr_rejected','mcr_push_failed','disabled',
]);
let _queueHintTimer = null;
function _fmtEta(s) {
    if (s == null) return '';
    if (s < 15) return 'quasi immédiat';
    if (s < 60) return `${s} s`;
    const m = Math.floor(s / 60);
    const sec = Math.round((s % 60) / 10) * 10;
    return sec === 0 ? `${m} min` : `${m} min ${String(sec).padStart(2,'0')} s`;
}
// Calcule le texte du hint à partir du payload /api/queue-status.
// Renvoie '' si pas d'info exploitable.
function _formatQueueHint(d) {
    if (!d || d.pending_total == null) return '';
    let txt = '';
    if (d.your_position === 1) {
        txt = '⏳ En tête de file';
    } else if (d.your_position && d.pending_total > 0) {
        const eta = d.eta_seconds;
        const part = eta != null && eta < 15
            ? ' — quasi immédiat'
            : (eta != null ? ` — env. ${_fmtEta(eta)} d'attente` : '');
        txt = `⏳ Position ${d.your_position}/${d.pending_total} dans la file${part}`;
    } else if (d.pending_total > 0) {
        txt = `⏳ ${d.pending_total} job${d.pending_total > 1 ? 's' : ''} en attente`;
    } else if (d.processing_total > 0) {
        txt = '⏳ Tour suivant';   // file vide mais quelqu'un est en train de tourner devant
    } else {
        txt = '⏳ Réservation de la file…';  // file complètement vide, transitoire (entre 2 jobs)
    }
    if (txt && d.stale) txt += ' (estimation)';
    return txt;
}

// Poll global : scanne UNIQUEMENT les widgets queue-hint marqués comme
// pollables (data-pollable="1") — c'est-à-dire ceux dont le fichier
// associé est encore dans un statut polling (kevent_queued/transcribing/
// processing). Les widgets sur des fichiers terminaux (kevent_completed,
// kevent_failed, kevent_partially_completed) restent dans le DOM mais
// sans le flag pollable → on ne les met PAS à jour, on les vide même
// si le fichier vient juste de transiter vers un état terminal.
async function _pollQueueHintAll() {
    const widgets = document.querySelectorAll('[data-queue-hint-for][data-pollable="1"]');
    // Vide les widgets qui ne sont plus pollables (transition kevent_*ing →
    // kevent_completed/failed/partially) — sinon le dernier texte du poll
    // précédent reste affiché de façon trompeuse ("Réservation de la file…"
    // sur un fichier terminal).
    document.querySelectorAll('[data-queue-hint-for]:not([data-pollable="1"])').forEach((el) => {
        if (el.textContent) el.textContent = '';
    });
    if (widgets.length === 0) return;

    // Regroupe par job_id (null = générique).
    const byJobId = new Map();
    widgets.forEach((el) => {
        const jid = (el.getAttribute('data-queue-job-id') || '').trim() || null;
        if (!byJobId.has(jid)) byJobId.set(jid, []);
        byJobId.get(jid).push(el);
    });

    await Promise.all(Array.from(byJobId.entries()).map(async ([jid, els]) => {
        const url = jid
            ? `/api/queue-status?service_type=audio&job_id=${encodeURIComponent(jid)}`
            : '/api/queue-status?service_type=audio';
        try {
            const r = await fetch(url, { cache: 'no-store' });
            const d = await r.json();
            const txt = _formatQueueHint(d);
            els.forEach((el) => {
                el.textContent = txt;
                el.classList.toggle('queue-hint-stale', !!(d && d.stale));
            });
        } catch (e) { /* silencieux — on retentera dans 10s */ }
    }));
}

// Activé tant qu'au moins un widget queue-hint est dans le DOM. Démarré
// idempotemment à chaque render (loadSessions) ; auto-stop dans le poll quand
// il n'y a plus de widget (sortie de liste vers vue Mes appareils, etc.).
function ensureQueueHintPolling() {
    if (_queueHintTimer) return;          // déjà actif
    _pollQueueHintAll();                  // 1er appel immédiat
    _queueHintTimer = setInterval(() => {
        // Auto-stop : plus AUCUN widget pollable dans le DOM.
        if (document.querySelectorAll('[data-queue-hint-for][data-pollable="1"]').length === 0) {
            // Un dernier passage pour vider les widgets non-pollables qui
            // auraient encore du texte résiduel.
            _pollQueueHintAll();
            clearInterval(_queueHintTimer);
            _queueHintTimer = null;
            return;
        }
        _pollQueueHintAll();
    }, 10000);
}

// Aliases pour compat ascendante (anciens call-sites avant la généralisation).
function startQueueHintDetail(fileId) { ensureQueueHintPolling(); }
function stopQueueHintDetail() {
    if (_queueHintTimer) clearInterval(_queueHintTimer);
    _queueHintTimer = null;
}

// Affiche un modal avec toutes les infos techniques du fichier :
// statut + engine + langue + étapes IA (✓/✗ avec description) + impact LUFS.
async function openFileInfoModal(fileId) {
    const cached = (window._fileInfoCache || {})[fileId];
    if (!cached) {
        showToast('Données techniques en cours de chargement.', 'error');
        return;
    }
    const STEPS = {
        'transcript':              { label: 'Transcription brute',          desc: 'Texte issu de Whisper (faster-whisper).' },
        'transcript-tagged':       { label: 'Identification des locuteurs', desc: 'Diarisation pyannote — sépare le texte par interlocuteur. Peut échouer sur monolocuteur/audio très court.' },
        'transcript-corrected':    { label: 'Correction des sigles',        desc: 'LLM relit avec un glossaire métier pour corriger les acronymes.' },
        'transcript-cleaned':      { label: 'Nettoyage hors-sujet',         desc: 'LLM retire les passages parasites (faux départs, bruits verbalisés).' },
        'transcript-reformulated': { label: 'Discours indirect',            desc: 'LLM reformule au style indirect pour lecture rapide.' },
        'meeting-cr':              { label: 'Compte-rendu structuré',      desc: 'LLM produit l\'analyse 5 sections : acteurs, thématiques, décisions, gaps, recommandations.' },
    };
    // État par étape :
    //   ok    → output présent (vert ✓)
    //   fail  → output absent ET statut global = failed (rouge ✗ + cause)
    //   run   → output absent ET pipeline en cours, première étape pending
    //   wait  → output absent, pas encore tentée
    const status = cached.status || '';
    const isFail = (window._FAILED_TS && window._FAILED_TS.has(status))
        || ['failed','kevent_failed','mcr_auth_failed','mcr_rejected','mcr_push_failed'].includes(status);
    const isRunning = ['pending','processing','kevent_queued','kevent_transcribing','kevent_processing'].includes(status);
    let runMarked = false;
    const stepsHtml = Object.keys(STEPS).map(k => {
        const ok = !!(cached.outputs || {})[k];
        let icon, color, suffix = '';
        if (ok) { icon = '✓'; color = '#10b981'; }
        else if (isFail) {
            icon = '✗'; color = '#b91c1c';
            suffix = ` <small style="color:#94a3b8">(échec du pipeline — étape non aboutie)</small>`;
        } else if (isRunning && !runMarked) {
            icon = '⏳'; color = '#2563eb'; runMarked = true;
            suffix = ` <small style="color:#94a3b8">(en cours)</small>`;
        } else if (isRunning) {
            icon = '☐'; color = '#94a3b8';
            suffix = ` <small style="color:#94a3b8">(en attente)</small>`;
        } else {
            icon = '☐'; color = '#94a3b8';
        }
        return `<div class="modal-step">
            <span style="color:${color};font-weight:700;font-size:1rem;">${icon}</span>
            <div>
                <div class="modal-step-label">${escapeHtml(STEPS[k].label)}${suffix}</div>
                <div class="modal-step-desc">${escapeHtml(STEPS[k].desc)}</div>
            </div>
        </div>`;
    }).join('');

    // Récupère ou crée le modal
    let modal = document.getElementById('file-info-modal');
    if (!modal) {
        modal = document.createElement('dialog');
        modal.id = 'file-info-modal';
        modal.className = 'file-info-modal';
        document.body.appendChild(modal);
        modal.addEventListener('click', (e) => {
            // Click sur backdrop ferme le modal
            if (e.target === modal) modal.close();
        });
    }
    modal.innerHTML = `
        <div class="modal-header">
            <h3>Détails techniques</h3>
            <button class="modal-close" onclick="document.getElementById('file-info-modal').close()" aria-label="Fermer">✕</button>
        </div>
        <div class="modal-body">
            <div class="modal-section">
                <div class="modal-section-title">Pipeline IA</div>
                <div class="modal-status">
                    <strong>Statut :</strong> ${escapeHtml(cached.label)}
                    <small style="color:#94a3b8;">(${escapeHtml(cached.status)}${cached.engine ? ' · ' + escapeHtml(cached.engine) : ''})</small>
                </div>
                ${cached.language ? `<div><strong>Langue détectée :</strong> ${escapeHtml(cached.language)}</div>` : ''}
            </div>
            <div class="modal-section">
                <div class="modal-section-title">Étapes</div>
                <div class="modal-steps">${stepsHtml}</div>
            </div>
            <div class="modal-section">
                <div class="modal-section-title">Normalisation audio (LUFS)</div>
                <p class="modal-explain">La normalisation aligne le niveau sonore sur la cible −16 LUFS (compatible voix). Le calcul mesure les LUFS / TP / LRA avant et après transcodage.</p>
                <button class="fr-btn fr-btn--sm fr-btn--secondary"
                        onclick="loadNormalizationImpact('${fileId}')">Calculer l'impact</button>
                <div class="modal-impact-result" id="modal-impact-${fileId}">
                    ${(impactCache[fileId] && impactCache[fileId].text) ? escapeHtml(impactCache[fileId].text) : '<small style="color:#94a3b8;">Pas encore calculé.</small>'}
                </div>
            </div>
        </div>
    `;
    if (typeof modal.showModal === 'function') {
        modal.showModal();
    } else {
        modal.setAttribute('open', '');
    }
}

// Renomme le titre suggéré (suggested_filename) du fichier en vue détail.
// Persiste via POST /api/file/<id>/rename → device-token-authority interne.
async function renameDetailTitle(fileId, btn) {
    const input = document.querySelector(`[data-detail-title-for="${fileId}"]`);
    if (!input) return;
    const newTitle = (input.value || '').trim();
    if (!newTitle) {
        showToast('Le titre ne peut pas être vide.', 'error');
        return;
    }
    btn.disabled = true;
    try {
        const resp = await fetch(`/api/file/${fileId}/rename`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ title: newTitle }),
        });
        const data = await resp.json();
        if (!resp.ok || !data.ok) throw new Error(data.error || 'rename_failed');
        input.dataset.originalTitle = newTitle;
        showToast('Titre renommé.', 'success');
        // Force un refresh des sessions pour propager le nouveau titre.
        loadSessions({ force: true });
    } catch (e) {
        btn.disabled = false;
        showToast(`Renommage échoué : ${e.message}`, 'error');
    }
}
// Active le bouton "Renommer" quand le titre est modifié (vs valeur initiale).
document.addEventListener('input', (ev) => {
    const t = ev.target;
    if (!t || !t.matches('.file-detail-title-input')) return;
    const original = t.dataset.originalTitle || '';
    const current = (t.value || '').trim();
    const btn = t.parentElement && t.parentElement.querySelector('.file-detail-rename-btn');
    if (btn) btn.disabled = !current || current === original;
});

function toggleRowExpand(btn) {
    const wrapper = btn.closest('.file-row-compact-wrapper');
    if (!wrapper) return;
    const exp = wrapper.querySelector('.file-row-expanded');
    if (!exp) return;
    const open = exp.style.display !== 'none';
    exp.style.display = open ? 'none' : '';
    btn.classList.toggle('is-open', !open);
    const lbl = btn.querySelector('.file-row-expand-label');
    if (lbl) lbl.textContent = open ? 'détails' : 'replier';
    btn.setAttribute('aria-label', open ? 'Voir le résumé' : 'Masquer le résumé');
}
// Format helpers pour la vue liste compacte — délègue aux utils centralisés
// (TKT-103) pour produire un rendu français lisible cohérent dans toute l'UI.
function _formatDateCompact(iso) {
    return formatDate(iso, { withTime: true });
}
function _formatDuration(seconds) {
    return formatDuration(seconds);
}

// Convertit une ISO 8601 ("2026-05-14T13:42:00+02:00" ou avec Z) au format
// attendu par <input type="datetime-local"> ("YYYY-MM-DDTHH:MM" en heure
// locale). Renvoie '' si l'entrée est invalide.
function _isoToDatetimeLocal(iso) {
    if (!iso) return '';
    try {
        const d = new Date(iso);
        if (Number.isNaN(d.getTime())) return '';
        // toLocaleString en sv-SE renvoie "YYYY-MM-DD HH:MM:SS" — on remplace
        // l'espace par T et on tronque les secondes.
        const s = d.toLocaleString('sv-SE');
        return s.slice(0, 16).replace(' ', 'T');
    } catch (e) { return ''; }
}

// Convertit la valeur d'un <input type="datetime-local"> ("YYYY-MM-DDTHH:MM")
// en ISO 8601 avec offset local (envoyée au serveur pour stockage TZ-aware).
function _datetimeLocalToIso(value) {
    if (!value) return null;
    // Construire un Date à partir de la chaîne locale. new Date(str sans TZ)
    // interprète l'heure comme locale ; toISOString convertit en UTC.
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return null;
    return d.toISOString();
}

let _meetingDtSaveTimers = new Map();

async function saveMeetingDatetime(fileId, inputEl) {
    if (!fileId || !inputEl) return;
    const raw = (inputEl.value || '').trim();
    const iso = raw ? _datetimeLocalToIso(raw) : null;
    // Si l'utilisateur a vidé le champ : équivalent à un reset (NULL côté serveur).
    // Debounce léger pour éviter de spammer le PATCH si change+blur tirent
    // tous les deux dans la même ms.
    const prevTimer = _meetingDtSaveTimers.get(fileId);
    if (prevTimer) clearTimeout(prevTimer);
    const timer = setTimeout(() => _doSaveMeetingDatetime(fileId, iso, inputEl), 80);
    _meetingDtSaveTimers.set(fileId, timer);
}

async function _doSaveMeetingDatetime(fileId, iso, inputEl) {
    const statusEl = document.querySelector(`[data-meeting-dt-status-for="${fileId}"]`);
    const resetBtn = document.querySelector(`[data-meeting-dt-reset-for="${fileId}"]`);
    if (statusEl) { statusEl.textContent = 'Enregistrement…'; statusEl.className = 'file-detail-meeting-status'; }
    try {
        const resp = await fetch(`/api/file/${fileId}/meeting-datetime`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ meeting_datetime: iso }),
        });
        const data = await resp.json();
        if (!resp.ok || !data.ok) throw new Error(data.error || 'save_failed');
        if (statusEl) {
            statusEl.textContent = '✓ Enregistré';
            statusEl.className = 'file-detail-meeting-status saved';
            setTimeout(() => { if (statusEl.textContent === '✓ Enregistré') statusEl.textContent = ''; }, 2500);
        }
        if (resetBtn) resetBtn.disabled = !data.meeting_datetime_overridden;
        // Re-charge la liste pour refléter le nouveau tri + l'italique mis à
        // jour. Force=true contourne le diff sur snapshot.
        loadSessions({ force: true });
    } catch (e) {
        if (statusEl) {
            statusEl.textContent = '✗ Échec sauvegarde';
            statusEl.className = 'file-detail-meeting-status error';
        }
    }
}

async function resetMeetingDatetime(fileId) {
    const input = document.querySelector(`[data-meeting-dt-for="${fileId}"]`);
    if (input) input.value = '';
    await _doSaveMeetingDatetime(fileId, null, input);
}

// Extensions audio acceptées (recopie côté client de ALLOWED_AUDIO_EXTENSIONS).
// Sert au filtre dossier (le picker dossier ne filtre pas par extension).
const _ALLOWED_AUDIO_EXT_SET = new Set(
    Array.from(window.ALLOWED_AUDIO_EXTENSIONS || [])
);
function _isAudioFileForUpload(file) {
    if (!file || !file.name || file.size === 0) return false;
    const idx = file.name.lastIndexOf('.');
    if (idx <= 0) return false;
    const ext = file.name.slice(idx + 1).toLowerCase();
    return _ALLOWED_AUDIO_EXT_SET.has(ext);
}

let _localUploadInFlight = false;

function handleLocalUploadInput(inputEl) {
    if (!inputEl || !inputEl.files || inputEl.files.length === 0) return;
    const files = Array.from(inputEl.files);
    uploadLocalFiles(files);
    // Réinitialise pour autoriser un re-pick du même fichier ensuite.
    inputEl.value = '';
}

async function uploadLocalFiles(files) {
    if (_localUploadInFlight) return;
    if (!files || files.length === 0) return;
    const audioFiles = files.filter(_isAudioFileForUpload);
    const filteredOut = files.length - audioFiles.length;
    if (audioFiles.length === 0) {
        alert(`Aucun fichier audio valide trouvé. Extensions acceptées : ${Array.from(_ALLOWED_AUDIO_EXT_SET).join(', ')}`);
        return;
    }

    _localUploadInFlight = true;
    const progress = document.getElementById('local-upload-progress');
    const label = document.getElementById('local-upload-progress-label');
    const count = document.getElementById('local-upload-progress-count');
    const fill = document.getElementById('local-upload-progress-fill');
    const errors = document.getElementById('local-upload-progress-errors');
    const filesBtn = document.getElementById('local-upload-files-btn');
    const folderBtn = document.getElementById('local-upload-folder-btn');
    if (progress) progress.classList.add('is-active');
    if (errors) errors.textContent = '';
    if (filesBtn) filesBtn.disabled = true;
    if (folderBtn) folderBtn.disabled = true;

    const total = audioFiles.length;
    let okCount = 0;
    const failed = [];
    for (let i = 0; i < total; i++) {
        const file = audioFiles[i];
        if (label) label.textContent = `Upload de "${file.name}"…`;
        if (count) count.textContent = ` (${i + 1}/${total})`;
        try {
            await _uploadOneLocal(file);
            okCount += 1;
        } catch (e) {
            failed.push(`${file.name}: ${e.message || 'échec'}`);
        }
        if (fill) fill.style.width = `${Math.round(((i + 1) / total) * 100)}%`;
    }

    if (label) label.textContent = failed.length
        ? `${okCount}/${total} fichier(s) uploadé(s)`
        : `✓ ${okCount} fichier(s) uploadé(s)`;
    if (count) count.textContent = filteredOut > 0 ? ` (${filteredOut} non-audio ignoré(s))` : '';
    if (errors && failed.length) errors.textContent = failed.join('\n');
    if (filesBtn) filesBtn.disabled = false;
    if (folderBtn) folderBtn.disabled = false;
    _localUploadInFlight = false;

    // Refresh de la liste pour faire apparaître les nouveaux fichiers.
    loadSessions({ force: true });
    // Cache la barre après quelques secondes si tout est OK.
    if (!failed.length) {
        setTimeout(() => {
            if (progress) progress.classList.remove('is-active');
            if (fill) fill.style.width = '0%';
        }, 3000);
    }
}

function _uploadOneLocal(file) {
    return new Promise((resolve, reject) => {
        const fd = new FormData();
        fd.append('file', file);
        const xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/my-upload');
        xhr.timeout = 120000; // 2 min/file pour les gros audios
        xhr.upload.onprogress = (e) => {
            if (e.lengthComputable) {
                const label = document.getElementById('local-upload-progress-label');
                const pct = Math.round((e.loaded / e.total) * 100);
                if (label) label.textContent = `Upload de "${file.name}" (${pct}%)…`;
            }
        };
        xhr.onload = () => {
            let data;
            try { data = JSON.parse(xhr.responseText || '{}'); } catch (e) { data = {}; }
            if (xhr.status >= 200 && xhr.status < 300) return resolve(data);
            reject(new Error(data.error || `HTTP ${xhr.status}`));
        };
        xhr.onerror = () => reject(new Error('erreur réseau'));
        xhr.ontimeout = () => reject(new Error('timeout (>2min)'));
        xhr.send(fd);
    });
}

// Drag & drop : capture sur le header pour rester découvrable. Ignore le
// drop si on tombe sur un input/button — laisse le comportement natif.
function _initLocalUploadDnD() {
    const host = document.querySelector('.recent-activities-panel');
    if (!host) return;
    let dragDepth = 0;
    host.addEventListener('dragenter', (e) => {
        if (!e.dataTransfer || !Array.from(e.dataTransfer.types || []).includes('Files')) return;
        e.preventDefault();
        dragDepth += 1;
        host.classList.add('is-dragover');
    });
    host.addEventListener('dragleave', () => {
        dragDepth = Math.max(0, dragDepth - 1);
        if (dragDepth === 0) host.classList.remove('is-dragover');
    });
    host.addEventListener('dragover', (e) => {
        if (e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files')) {
            e.preventDefault();
        }
    });
    host.addEventListener('drop', (e) => {
        dragDepth = 0;
        host.classList.remove('is-dragover');
        if (!e.dataTransfer || !e.dataTransfer.files || e.dataTransfer.files.length === 0) return;
        e.preventDefault();
        uploadLocalFiles(Array.from(e.dataTransfer.files));
    });
}
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _initLocalUploadDnD);
} else {
    _initLocalUploadDnD();
}
// Durée de rétention device en jours (lue depuis DEVICE_TOKEN_RETENTION_HOURS
// côté serveur — 15j en prod-bêta, 7j par défaut). Sert aux messages de
// confirmation pour refléter la vraie durée que Renouveler applique.
const deviceRetentionDays = window.DEVICE_RETENTION_DAYS;

// Mode "Mode avancé" pour les téléchargements : OFF (défaut) montre
// le CR + audio interne + Transcription nettoyée + Discours indirect, le
// reste va dans le menu "Autres". ON affiche tout à plat (pas de menu).
// Persistant en sessionStorage. Astuce power-user non documentée : Alt
// active un peek temporaire (sans changer l'état persistant) — pratique
// pour jeter un œil sans toggler.
let _dlAdvancedMode = false;
try { _dlAdvancedMode = sessionStorage.getItem('mydevices-dl-mode') === 'advanced'; } catch(e){}
let _altPeek = false;

function effectiveAdvancedDl() { return _dlAdvancedMode || _altPeek; }

function updateAdvancedToggleUi() {
    const btn = document.getElementById('advanced-toggle');
    if (btn) {
        btn.classList.toggle('is-on', _dlAdvancedMode);
        btn.classList.toggle('is-peek', _altPeek);
        btn.setAttribute('aria-pressed', _dlAdvancedMode ? 'true' : 'false');
    }
    // body.adv-mode pilote la visibilité de .advanced-only (camion poubelle, etc.).
    document.body.classList.toggle('adv-mode', effectiveAdvancedDl());
}

function toggleAdvancedDl() {
    _dlAdvancedMode = !_dlAdvancedMode;
    try { sessionStorage.setItem('mydevices-dl-mode', _dlAdvancedMode ? 'advanced' : 'simple'); } catch(e){}
    updateAdvancedToggleUi();
    refreshDownloadsBlocks();
}

function refreshDownloadsBlocks() {
    document.querySelectorAll('.transcript-section[data-persistent-summary="1"]').forEach((container) => {
        const fileId = container.getAttribute('data-transcript-file-id');
        if (fileId) loadTranscriptStatus(fileId, container);
    });
}

// Peek temporaire via Alt enfoncé. Modifier-only keydown ne se répète
// pas (autorepeat ignore Alt sur la plupart des navigateurs), donc on
// fire bien une seule fois à l'appui.
document.addEventListener('keydown', (e) => {
    if (e.key === 'Alt' && !_altPeek) {
        _altPeek = true;
        updateAdvancedToggleUi();
        refreshDownloadsBlocks();
        e.preventDefault();
    }
});
document.addEventListener('keyup', (e) => {
    if (e.key === 'Alt' && _altPeek) {
        _altPeek = false;
        updateAdvancedToggleUi();
        refreshDownloadsBlocks();
    }
});
// Si la fenêtre perd le focus pendant un peek (Cmd+Tab…), on annule
// pour ne pas rester coincé en mode peek.
window.addEventListener('blur', () => {
    if (_altPeek) { _altPeek = false; updateAdvancedToggleUi(); refreshDownloadsBlocks(); }
});

// Icônes SVG inline (Heroicons-like, simplifiés). Centralisées ici pour
// que tous les boutons-icône partagent le même rendu et qu'on puisse les
// faire évoluer en un seul endroit.
const ICONS = {
    // Poubelle simple — suppression d'un élément
    trash: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>',
    // Camion poubelle — purge massive (suppression de toute la liste)
    truck: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 17h2V7a1 1 0 0 1 1-1h9v11h2"/><path d="M14 10h4l3 4v3h-2"/><circle cx="7" cy="18" r="2"/><circle cx="17" cy="18" r="2"/><path d="M7 10v3M9 10v3M11 10v3"/></svg>',
    // Télécharger un audio : icône "fichier audio" type Finder — document
    // avec coin replié + note de musique à l'intérieur.
    fmt_audio:  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" fill="#eff6ff"/><path d="M14 2v6h6" fill="#dbeafe"/><path d="M11 12v5.5" stroke="#1d4ed8"/><path d="M11 12l4-1v5.5" stroke="#1d4ed8"/><ellipse cx="10" cy="17.5" rx="1.4" ry="1.1" fill="#1d4ed8" stroke="#1d4ed8"/><ellipse cx="14" cy="16.5" rx="1.4" ry="1.1" fill="#1d4ed8" stroke="#1d4ed8"/></svg>',
    // Écouter : triangle play dans un cercle (style bouton lecteur).
    fmt_play:   '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="12" cy="12" r="10" fill="currentColor" opacity="0.12"/><circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="1.8" fill="none"/><path d="M10 8.5v7l6-3.5z" fill="currentColor"/></svg>',
    fmt_txt:    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M8 13h8M8 17h8M8 9h2"/></svg>',
    fmt_md:     '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M7 15V9l2.5 3L12 9v6"/><path d="M16 9v6m0 0l-1.5-1.5M16 15l1.5-1.5"/></svg>',
    fmt_docx:   '<svg viewBox="0 0 24 24" fill="none" stroke="#2563eb" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" fill="#dbeafe"/><path d="M14 2v6h6" fill="#bfdbfe"/><text x="12" y="18" font-size="6" font-weight="700" fill="#1e40af" text-anchor="middle" font-family="Arial,sans-serif">W</text></svg>',
    fmt_odt:    '<svg viewBox="0 0 24 24" fill="none" stroke="#16a34a" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" fill="#dcfce7"/><path d="M14 2v6h6" fill="#bbf7d0"/><text x="12" y="18" font-size="5" font-weight="700" fill="#166534" text-anchor="middle" font-family="Arial,sans-serif">ODT</text></svg>',
    fmt_json:   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 4c-2 0-3 1-3 3v3c0 2-2 2-2 2s2 0 2 2v3c0 2 1 3 3 3"/><path d="M16 4c2 0 3 1 3 3v3c0 2 2 2 2 2s-2 0-2 2v3c0 2-1 3-3 3"/></svg>',
};
function escapeHtml(v) {
    return (v || '').toString().replace(/[&<>"']/g, (s) => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;',
    })[s]);
}

function statusLabel(status) {
    const labels = {
        pending: 'En attente',
        scanning: 'Analyse antivirus',
        scan_clean: 'Scan OK',
        scan_infected: 'Infecté',
        transcoding: 'Transcodage',
        transcoded: 'Transcodé',
        ready_for_transfer: 'Prêt transfert',
        transferring: 'Transfert',
        transferred: 'Transféré',
        quarantined: 'Quarantaine',
        transcode_failed: 'Transcodage échoué',
        error: 'Erreur',
    };
    return labels[status] || status;
}

// Phases d'upload où le pipeline tourne encore (avant que la transcription
// ne prenne le relais). Utilisé pour animer le tag dès le départ.
const UPLOAD_IN_PROGRESS_STATES = new Set([
    'pending', 'scanning', 'scan_clean',
    'transcoding', 'ready_for_transfer', 'transferring',
]);
function _uploadStateLabel(status) {
    if (status === 'transferred') return 'Fichier reçu. Transcription en attente.';
    if (UPLOAD_IN_PROGRESS_STATES.has(status)) {
        return `Étape en cours : ${statusLabel(status)}`;
    }
    return statusLabel(status);
}

// ── TKT-101 : tag de statut DSFR (remplace l'ancienne pastille ●) ─────────
// Renvoie { label, icon, kind, srLabel } pour rendu en `fr-tag fr-tag--sm`
// + classe sémantique `file-row-status-tag--<kind>` (success/processing/
// queued/partial/error/neutral). On accepte indifféremment les statuts du
// pipeline d'upload (file.status) et de transcription (Kevent/MCR).
const _TRANSCRIPT_SUCCESS = new Set(['completed', 'kevent_completed', 'mcr_pushed']);
const _TRANSCRIPT_PROCESSING = new Set(['processing', 'kevent_transcribing', 'kevent_processing']);
const _TRANSCRIPT_QUEUED = new Set(['pending', 'kevent_queued']);
const _TRANSCRIPT_PARTIAL = new Set(['kevent_partially_completed']);
const _TRANSCRIPT_FAILED = new Set([
    'failed', 'kevent_failed',
    'mcr_auth_failed', 'mcr_rejected', 'mcr_push_failed',
]);
const _UPLOAD_FAILED = new Set(['transcode_failed', 'error']);

function _statusTagInfo(status, opts) {
    const o = opts || {};
    if (o.virus) {
        return { label: 'Quarantaine', icon: 'fr-icon-error-warning-line', kind: 'error' };
    }
    if (_TRANSCRIPT_SUCCESS.has(status)) {
        return { label: 'Prête', icon: 'fr-icon-success-line', kind: 'success' };
    }
    if (_TRANSCRIPT_PARTIAL.has(status)) {
        return { label: 'Partiellement prête', icon: 'fr-icon-warning-line', kind: 'partial' };
    }
    if (_TRANSCRIPT_FAILED.has(status) || _UPLOAD_FAILED.has(status)) {
        return { label: 'Erreur', icon: 'fr-icon-error-warning-line', kind: 'error' };
    }
    if (_TRANSCRIPT_PROCESSING.has(status) || UPLOAD_IN_PROGRESS_STATES.has(status)) {
        return { label: 'En traitement', icon: 'fr-icon-time-line', kind: 'processing' };
    }
    if (_TRANSCRIPT_QUEUED.has(status) || status === 'transferred') {
        return { label: 'En file d\'attente', icon: 'fr-icon-time-line', kind: 'queued' };
    }
    if (status === 'disabled') {
        return { label: 'Désactivée', icon: 'fr-icon-information-line', kind: 'neutral' };
    }
    return { label: 'En attente', icon: 'fr-icon-time-line', kind: 'queued' };
}

function _renderStatusTag(status, opts) {
    const o = opts || {};
    const info = _statusTagInfo(status, o);
    // aria-label détaillé : libellé court + statut technique (utile au screen
    // reader pour distinguer "kevent_partially_completed" d'une vraie erreur).
    const tech = o.virus ? `Virus détecté — ${statusLabel(status)}` : (o.tooltip || _uploadStateLabel(status));
    const aria = `Statut : ${info.label} — ${tech}`;
    const fileId = o.fileId || '';
    return `<span class="fr-tag fr-tag--sm fr-tag--icon-left ${info.icon} file-row-status-tag file-row-status-tag--${info.kind}"
                  data-file-status-tag="${escapeHtml(fileId)}"
                  data-status-kind="${info.kind}"
                  role="status"
                  aria-label="${escapeHtml(aria)}"
                  title="${escapeHtml(tech)}">${escapeHtml(info.label)}</span>`;
}

function tokenValidityDaysLabel(retentionExpiresAt) {
    if (!retentionExpiresAt) return '-';
    const endMs = new Date(retentionExpiresAt).getTime();
    if (!Number.isFinite(endMs)) return '-';
    const diffMs = endMs - Date.now();
    if (diffMs <= 0) return 'expiré';
    const days = diffMs / (24 * 60 * 60 * 1000);
    if (days < 1) return '< 1 jour';
    return `${Math.ceil(days)} jour(s)`;
}

function formatDateTimeShort(isoValue) {
    return formatDate(isoValue, { withTime: true }) || '-';
}

function tokenIdShort(tokenValue) {
    const raw = (tokenValue || '').toString().trim();
    if (!raw) return '-';
    if (raw.length <= 12) return raw;
    return `${raw.slice(0, 6)}...${raw.slice(-4)}`;
}

function deviceTokenStateLabel(device) {
    const rawStatus = (device && device.status ? String(device.status) : '').toLowerCase();
    if (rawStatus === 'revoked') return 'révoqué';
    if (rawStatus === 'pending') return 'initialisation…';
    const endMs = new Date((device && (device.retention_expires_at || device.session_expires_at)) || '').getTime();
    if (Number.isFinite(endMs) && endMs <= Date.now()) return 'expiré';
    return 'active';
}

function deviceTokenStateColor(stateLabel) {
    if (stateLabel === 'révoqué') return '#b91c1c';
    if (stateLabel === 'expiré') return '#b45309';
    if (stateLabel === 'initialisation…') return '#64748b';
    return '#166534';
}
// Publication globale des helpers partagés — consommés par tabs/devices.js
// (rendu fr-card horizontale par appareil) et tabs/* futurs.
window.escapeHtml = escapeHtml;
window.tokenValidityDaysLabel = tokenValidityDaysLabel;
window.formatDateTimeShort = formatDateTimeShort;
window.tokenIdShort = tokenIdShort;
window.deviceTokenStateLabel = deviceTokenStateLabel;
window.deviceTokenStateColor = deviceTokenStateColor;


function transferProgressFromMessage(status, msg) {
    if (status === 'transferred') return 100;
    if (status === 'ready_for_transfer') return 10;
    if (status !== 'transferring') return 0;
    const text = (msg || '').toLowerCase();
    const m = text.match(/(\d{1,3})\s*%/);
    if (m) {
        const v = Math.max(0, Math.min(100, parseInt(m[1], 10)));
        return Number.isFinite(v) ? v : 50;
    }
    if (text.includes('notification')) return 20;
    if (text.includes('téléchargement') || text.includes('telechargement')) return 45;
    if (text.includes('copie')) return 70;
    if (text.includes('finalisation')) return 90;
    return 50;
}

function pipelineProgress(status, statusMessage) {
    const p = { scan: 0, transcode: 0, transfer: 0, error: false, blocked: false, active: 'analyse' };
    switch (status) {
        case 'pending':
            break;
        case 'scanning':
            p.scan = 50;
            p.active = 'analyse';
            break;
        case 'scan_clean':
            p.scan = 100;
            p.active = 'transcodage';
            break;
        case 'scan_infected':
        case 'quarantined':
            p.scan = 100;
            p.blocked = true;
            p.active = 'analyse';
            break;
        case 'transcoding':
            p.scan = 100;
            p.transcode = 50;
            p.active = 'transcodage';
            break;
        case 'transcoded':
            p.scan = 100;
            p.transcode = 100;
            p.active = 'transfert';
            break;
        case 'ready_for_transfer':
            p.scan = 100;
            p.transcode = 100;
            p.transfer = transferProgressFromMessage(status, statusMessage);
            p.active = 'transfert';
            break;
        case 'transferring':
            p.scan = 100;
            p.transcode = 100;
            p.transfer = transferProgressFromMessage(status, statusMessage);
            p.active = 'transfert';
            break;
        case 'transferred':
            p.scan = 100;
            p.transcode = 100;
            p.transfer = 100;
            p.active = 'transfert';
            break;
        case 'transcode_failed':
            p.scan = 100;
            p.transcode = 60;
            p.error = true;
            p.active = 'transcodage';
            break;
        default:
            p.error = true;
            break;
    }
    p.total = Math.round((p.scan + p.transcode + p.transfer) / 3);
    return p;
}

// ── PR6 : extraction devices → frontend/tabs/devices.js ────────────────
// Les fonctions ci-dessous vivent désormais dans tabs/devices.js et sont
// republiées sur window.* pour rester appelables depuis le reste de
// legacy.js (qui les invoque encore via `loadDevices()`, etc.) :
//   - generateCode, resetForm
//   - updateDeviceFilterButton, toggleDeviceScope, showEnrollmentForm
//   - loadDevices, renameDevice, revokeDevice, deleteDevicePermanently,
//     revokeAllDevices, renewTokenByQr
//
// On garde des trampolines locaux pour ne casser aucun call-site interne
// à legacy.js — les modules ES bundlés par Vite ne hoist pas les top-level
// dans la portée globale, donc une référence directe `loadDevices()`
// résoudrait sur l'ancienne définition (absente). Les trampolines
// délèguent à window.* qui pointe sur les exports tabs/devices.js.
function generateCode() { return window.generateCode && window.generateCode.apply(null, arguments); }
function resetForm() { return window.resetForm && window.resetForm.apply(null, arguments); }
function updateDeviceFilterButton() { return window.updateDeviceFilterButton && window.updateDeviceFilterButton.apply(null, arguments); }
function toggleDeviceScope() { return window.toggleDeviceScope && window.toggleDeviceScope.apply(null, arguments); }
function showEnrollmentForm() { return window.showEnrollmentForm && window.showEnrollmentForm.apply(null, arguments); }
function loadDevices() {
    if (window.loadDevices) return window.loadDevices.apply(null, arguments);
    return Promise.resolve();
}
function renameDevice() { return window.renameDevice && window.renameDevice.apply(null, arguments); }
function revokeDevice() { return window.revokeDevice && window.revokeDevice.apply(null, arguments); }
function deleteDevicePermanently() { return window.deleteDevicePermanently && window.deleteDevicePermanently.apply(null, arguments); }
function revokeAllDevices() { return window.revokeAllDevices && window.revokeAllDevices.apply(null, arguments); }
function renewTokenByQr() { return window.renewTokenByQr && window.renewTokenByQr.apply(null, arguments); }

// (le toggle Voir/Masquer activités a été retiré — la liste est toujours
//  affichée dans l'onglet "Mes transferts et analyses".)

async function purgeSessions() {
    const ok = confirm('Mettre TOUTES vos sessions et leurs fichiers à la corbeille ?\n\n' +
                       'Les éléments seront définitivement supprimés au bout de 30 jours.');
    if (!ok) return;
    try {
        const resp = await fetch('/api/purge-my-sessions', { method: 'POST' });
        const data = await resp.json();
        // TKT-211 : libellés user-facing débarrassés du terme "purge".
        if (!resp.ok) throw new Error(data.error || 'Erreur lors de la mise à la corbeille');
        alert(`Mis à la corbeille: ${data.deleted_sessions || 0} session(s), ${data.deleted_files || 0} fichier(s).\n` +
              `Suppression définitive automatique au bout de 30 jours.`);
        loadSessions();
        loadDevices();
    } catch (e) {
        alert('Erreur: ' + e.message);
    }
}

async function deleteFile(fileId, filenameRaw) {
    const filename = (filenameRaw || '').replace(/&#39;/g, "'");
    if (!confirm(`Mettre le fichier « ${filename} » à la corbeille ?

` +
                 `Le fichier (audio + transcription + CR) est masqué de la liste ` +
                 `et sera définitivement supprimé au bout de 30 jours.`)) return;
    // Optimistic UI : on retire la row immédiatement du DOM pour que le user
    // ait un feedback instantané. Si l'API DELETE échoue, on ré-insert la row
    // à sa position d'origine et on alert.
    const row = document.querySelector(`[data-file-row="${fileId}"]`);
    let revertSnapshot = null;
    if (row) {
        revertSnapshot = { el: row, parent: row.parentNode, next: row.nextSibling };
        row.remove();
    }
    // Si on était en vue détail de ce fichier, revenir à la liste.
    if (_detailFileId === fileId) {
        try { showFilesList(); } catch (e) {}
    }
    try {
        const resp = await fetch(`/api/file/${fileId}`, { method: 'DELETE' });
        const data = await resp.json();
        if (!resp.ok || !data.ok) throw new Error(data.error || 'delete_failed');
        // OK : force un loadSessions en arrière-plan pour resync compteurs / autres rows.
        loadSessions({ force: true });
    } catch (e) {
        // Échec API : restaurer la row à sa position d'origine et alerter.
        if (revertSnapshot && revertSnapshot.parent) {
            try {
                if (revertSnapshot.next && revertSnapshot.next.parentNode === revertSnapshot.parent) {
                    revertSnapshot.parent.insertBefore(revertSnapshot.el, revertSnapshot.next);
                } else {
                    revertSnapshot.parent.appendChild(revertSnapshot.el);
                }
            } catch (_) { /* dernière sécurité : loadSessions re-render tout */ }
        }
        alert('Echec suppression du fichier.');
        loadSessions({ force: true });
    }
}

async function deleteSession(simpleCode, allowSilent) {
    // For "expired_unused" / "pending_enrollment" with 0 upload, no confirm
    // (rien à perdre). For "enrolled" / "expired_consumed" with files,
    // double confirm warning that linked files will be removed too.
    if (!allowSilent) {
        if (!confirm(`Mettre la session ${simpleCode} à la corbeille ?

` +
                     `Les fichiers uploadés via ce code seront aussi masqués ` +
                     `et définitivement supprimés au bout de 30 jours.`)) return;
    }
    const btn = document.querySelector(`[data-session-delete="${simpleCode}"]`);
    if (btn) btn.disabled = true;
    try {
        const resp = await fetch(`/api/my-sessions/${simpleCode}`, { method: 'DELETE' });
        const data = await resp.json();
        if (!resp.ok || !data.ok) throw new Error(data.error || 'delete_failed');
        const row = document.querySelector(`[data-session-row="${simpleCode}"]`);
        if (row) row.remove();
        setTimeout(() => { loadSessions(); loadDevices(); }, 250);
    } catch (e) {
        if (btn) btn.disabled = false;
        alert('Echec suppression de la session.');
    }
}

async function renewSession(sessionId) {
    if (!confirm('Renouveler cette session de 7 jours ?')) return;
    try {
        const ttlValue = document.getElementById('ttl') ? document.getElementById('ttl').value : '';
        const addUploadsValue = document.getElementById('max-uploads')
            ? parseInt(document.getElementById('max-uploads').value || '0', 10)
            : 0;
        const resp = await fetch(`/api/my-sessions/${sessionId}/renew-7d`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                ttl_minutes: ttlValue,
                add_uploads: addUploadsValue,
            }),
        });
        const data = await resp.json();
        if (!resp.ok || !data.ok) throw new Error(data.error || 'renew_failed');
        loadSessions();
    } catch (e) {
        alert('Echec renouvellement de la session.');
    }
}

// Détection d'interaction utilisateur dans la liste sessions : si focus
// sur un <select>/<input>/<button> dans #sessions-list ou #transfer-live,
// on saute le refresh pour ne pas casser la sélection en cours. Reprise
// au focusout + au prochain tick.
let _userInteractingTs = 0;
document.addEventListener('focusin', (ev) => {
    const t = ev.target;
    if (!t) return;
    if (t.closest('#sessions-list') || t.closest('#transfer-live')) {
        _userInteractingTs = Date.now();
    }
});
document.addEventListener('focusout', () => {
    // Délai pour absorber un clic qui bascule focus rapidement.
    setTimeout(() => { _userInteractingTs = 0; }, 800);
});
function _userIsInteracting() {
    // Considère l'utilisateur actif si focus posé < 2s
    return _userInteractingTs && (Date.now() - _userInteractingTs) < 2000;
}

// Snapshot du dernier rendu (JSON sérialisé) — sert au diff-based refresh
// pour ne pas re-render si rien n'a changé entre 2 polls (ce qui faisait
// flicker l'UI toutes les 15s).
let _lastSessionsSnapshot = '';

// Exposé pour tabs/devices.js (generateCode / renewTokenByQr déclenchent
// un refresh de la liste réunions après une opération device).
// Empty-state DSFR (chantier UX-Refonte-3) — affiché quand 0 fichier audio
// uploadé. Format fr-callout avec CTA "Comment téléverser ?" qui ouvre un
// petit modal d'aide. Pas de fr-modal full pour rester léger.
function _renderMeetingsEmptyState() {
    return `
        <div class="fr-callout fr-callout--blue-cumulus" style="margin-top:0.6rem;">
            <h3 class="fr-callout__title" style="font-size:1rem;">Aucun fichier audio téléversé pour le moment</h3>
            <p class="fr-callout__text" style="font-size:0.88rem;">
                Vos enregistrements audio apparaîtront ici dès que vous en aurez
                téléversé un, depuis cette page (boutons « Fichiers » / « Dossier »)
                ou depuis l'appli mobile MIrAI (PWA).
            </p>
            <button type="button" class="fr-btn fr-btn--sm"
                    data-action="meetings:show-upload-help">
                Comment téléverser ?
            </button>
        </div>`;
}

// Petit modal d'aide "Comment téléverser ?" — branché via délégation
// data-action="meetings:show-upload-help" dans tabs/meetings.js.
window.showUploadHelp = function showUploadHelp() {
    const existing = document.getElementById('upload-help-modal');
    if (existing) { existing.remove(); }
    const modal = document.createElement('div');
    modal.id = 'upload-help-modal';
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    modal.style.cssText = 'position:fixed;inset:0;background:rgba(15,23,42,0.55);' +
        'display:flex;align-items:center;justify-content:center;z-index:10000;';
    modal.innerHTML = `
        <div style="background:#fff;border-radius:0.5rem;max-width:520px;width:90%;
                    padding:1.2rem 1.4rem;box-shadow:0 10px 40px rgba(0,0,0,0.25);">
            <h2 style="margin:0 0 0.6rem;font-size:1.1rem;">Comment téléverser un audio ?</h2>
            <p style="font-size:0.88rem;color:#1e293b;">
                Deux options :
            </p>
            <ul style="font-size:0.88rem;color:#1e293b;padding-left:1.1rem;">
                <li><strong>Depuis ce poste</strong> : bouton « Fichiers » (un ou
                    plusieurs fichiers) ou « Dossier » (un dossier complet) en haut
                    à droite de cette page.</li>
                <li><strong>Depuis l'appli mobile MIrAI (PWA)</strong> : enrôlez
                    votre téléphone via un QR depuis l'onglet « Appareils »,
                    puis enregistrez ou choisissez un audio dans l'appli.</li>
            </ul>
            <div style="text-align:right;margin-top:0.8rem;">
                <button type="button" class="fr-btn fr-btn--sm"
                        onclick="document.getElementById('upload-help-modal').remove()">
                    Fermer
                </button>
            </div>
        </div>`;
    modal.addEventListener('click', (ev) => {
        if (ev.target === modal) modal.remove();
    });
    document.body.appendChild(modal);
};

window.loadSessions = function(opts) { return loadSessions(opts); };
async function loadSessions(opts) {
    opts = opts || {};
    // Skip le refresh si l'utilisateur est en train de sélectionner un
    // download ou cliquer un bouton — sinon innerHTML remplace le DOM
    // et la sélection est perdue. Le polling 15s tentera de nouveau.
    if (_userIsInteracting() && !opts.force) return;
    // Préserve la position de scroll pendant le refresh des sessions
    // (sinon innerHTML reset le scroll en haut, particulièrement gênant
    // sur les pages longues avec plusieurs sessions actives).
    const savedScrollY = window.scrollY;
    // Sauvegarde aussi l'ID + value du select de download en focus, si y'en
    // a un (l'utilisateur a peut-être hover/sélectionné mais pas encore
    // cliqué un bouton — restaure pour pas perdre le fil).
    window._savedDlSelections = new Map();
    document.querySelectorAll('.downloads-select').forEach((sel) => {
        const sec = sel.closest('.transcript-section');
        const fid = sec && sec.getAttribute('data-transcript-file-id');
        if (fid) window._savedDlSelections.set(fid, sel.selectedIndex);
    });
    try {
        const resp = await fetch('/api/my-sessions');
        const sessions = await resp.json();
        if (!resp.ok) {
            throw new Error((sessions && sessions.error) ? sessions.error : 'Erreur API sessions');
        }
        if (!Array.isArray(sessions)) {
            throw new Error('Format API invalide');
        }
        // Diff : si la response est identique à la dernière (et qu'aucune
        // vue détail/forced n'est demandée), on skip le re-render pour
        // éviter le flicker visuel toutes les 15s sur une page stable.
        const snap = JSON.stringify(sessions);
        if (!opts.force && snap === _lastSessionsSnapshot) return;
        _lastSessionsSnapshot = snap;
        const container = document.getElementById('sessions-list');
        const transferBox = document.getElementById('transfer-live');
        const activityMiniText = document.getElementById('activity-mini-text');
        const activityRail = document.getElementById('activity-rail');
        const activitySpinner = document.getElementById('activity-spinner');

        const activityStats = { analyse: 0, transcodage: 0, transfert: 0, done: 0, blocked: 0, total: 0 };
        for (const s of sessions) {
            for (const f of (s.uploads || [])) {
                activityStats.total += 1;
                switch (f.status) {
                    case 'pending':
                    case 'scanning':
                    case 'scan_clean':
                        activityStats.analyse += 1;
                        break;
                    case 'transcoding':
                    case 'transcoded':
                        activityStats.transcodage += 1;
                        break;
                    case 'ready_for_transfer':
                    case 'transferring':
                        activityStats.transfert += 1;
                        break;
                    case 'transferred':
                        activityStats.done += 1;
                        break;
                    case 'scan_infected':
                    case 'quarantined':
                    case 'transcode_failed':
                    case 'error':
                        activityStats.blocked += 1;
                        break;
                    default:
                        activityStats.analyse += 1;
                        break;
                }
            }
        }
        if (activityMiniText) {
            if (activityStats.total === 0) {
                activityMiniText.textContent = 'Activités: aucune.';
            } else {
                const parts = [
                    `A ${activityStats.analyse}`,
                    `T ${activityStats.transcodage}`,
                    `X ${activityStats.transfert}`,
                ];
                if (activityStats.blocked > 0) parts.push(`Q ${activityStats.blocked}`);
                if (activityStats.done > 0) parts.push(`OK ${activityStats.done}`);
                activityMiniText.textContent = `Activités: ${parts.join(' | ')}`;
            }
        }
        if (activitySpinner) {
            const active = (activityStats.analyse + activityStats.transcodage + activityStats.transfert) > 0;
            activitySpinner.classList.toggle('active', active);
            activitySpinner.title = active ? 'Activité en cours' : 'Aucune activité en cours';
        }
        if (activityRail) {
            const analyseOn = activityStats.analyse > 0;
            const transcodeOn = activityStats.transcodage > 0;
            const transferOn = activityStats.transfert > 0;
            activityRail.innerHTML = `
                <span class="activity-dot ${analyseOn ? 'active' : ''}" title="Analyse: ${activityStats.analyse}">1</span>
                <span class="activity-link ${(analyseOn || transcodeOn) ? 'active' : ''}"></span>
                <span class="activity-dot ${transcodeOn ? 'active' : ''}" title="Transcodage: ${activityStats.transcodage}">2</span>
                <span class="activity-link ${(transcodeOn || transferOn) ? 'active' : ''}"></span>
                <span class="activity-dot ${transferOn ? 'active' : ''}" title="Transfert: ${activityStats.transfert}">3</span>
            `;
        }

        // Files "in progress" = tout ce qui n'est pas encore "transferred"
        // (pré-transfert + transfer en cours). Pour chaque on affiche :
        //   ligne 1 : nom + status_message (ex: "Transcription Kevent — file
        //             d'attente Mirai")
        //   ligne 2 : mini chemin de fer 4 étapes (analyse / transcodage /
        //             transfert / transcription)
        // Disparaît dès que le fichier passe en transferred (la suite est
        // visible dans la liste sessions plus bas).
        const transfersInProgress = sessions.flatMap(s =>
            ((s.uploads || []).map(f => ({
                fileId: f.id,
                sessionCode: s.simple_code,
                name: f.original_filename,
                status: f.status,
                message: f.status_message || '',
                updatedAt: f.updated_at || f.created_at || null,
            })))
        ).filter(f => f.status !== 'transferred'
                   && f.status !== 'error'
                   && f.status !== 'scan_infected'
                   && f.status !== 'quarantined'
                   && f.status !== 'transcode_failed');

        if (transferBox) {
            if (transfersInProgress.length === 0) {
                transferBox.style.display = 'none';
                transferBox.innerHTML = '';
            } else {
                transferBox.style.display = '';
                const now = Date.now();
                const rows = transfersInProgress.map(t => {
                    const progress = pipelineProgress(t.status, t.message);
                    const analyseClass = (progress.scan === 100 && !progress.blocked) ? 'done'
                        : (progress.active === 'analyse' ? (progress.blocked ? 'blocked' : 'active') : '');
                    const transcodeClass = (progress.transcode === 100) ? 'done'
                        : (progress.active === 'transcodage' ? (progress.error ? 'blocked' : 'active') : '');
                    const transferClass = (progress.transfer === 100) ? 'done'
                        : (progress.active === 'transfert' ? 'active' : '');
                    const stale = t.updatedAt && (now - new Date(t.updatedAt).getTime()) > 180000;
                    return `
                        <div class="transfer-live-row" title="${escapeHtml(t.name)}">
                            <div class="transfer-live-line1">
                                <span class="transfer-live-code">${escapeHtml(t.sessionCode)}</span>
                                <span class="transfer-live-name">${escapeHtml(t.name)}</span>
                                <span class="transfer-live-msg" style="color:${stale ? '#b91c1c' : '#64748b'};">
                                    ${escapeHtml(t.message || 'En cours...')}
                                </span>
                            </div>
                            <div class="railroad railroad-mini">
                                <div class="rail-segment ${analyseClass}">
                                    <span class="rail-node">1</span><span class="rail-line"></span>
                                </div>
                                <div class="rail-segment ${transcodeClass}">
                                    <span class="rail-node">2</span><span class="rail-line"></span>
                                </div>
                                <div class="rail-segment ${transferClass}">
                                    <span class="rail-node">3</span><span class="rail-line"></span>
                                </div>
                                <div class="rail-segment" data-transcribe-segment="${t.fileId}">
                                    <span class="rail-node">4</span><span class="rail-line rail-line-tail"></span>
                                </div>
                            </div>
                        </div>`;
                }).join('');
                transferBox.innerHTML = `
                    <div class="transfer-live-title">Transferts en cours (${transfersInProgress.length})</div>
                    <div class="transfer-live-list">${rows}</div>
                `;
            }
        }

        if (sessions.length === 0) {
            container.innerHTML = _renderMeetingsEmptyState();
            const purgeBtn = document.getElementById('purge-btn');
            if (purgeBtn) purgeBtn.disabled = true;
            const countLabel = document.getElementById('file-count');
            if (countLabel) countLabel.textContent = '';
            const header = document.getElementById('sessions-table-header');
            if (header) header.style.display = 'none';
            return;
        }

        // En mode vue détail, on garde uniquement la session qui contient
        // le fichier ciblé, et on filtre tout le reste (autres sessions,
        // wrappers buckets) pour vraiment afficher une page focus fichier.
        const sessionsToRender = _detailFileId
            ? sessions.filter(s => (s.uploads || []).some(u => u.id === _detailFileId))
            : sessions;

        // Aplatir tous les fichiers de toutes les sessions, puis trier par
        // date de réunion (override utilisateur) ou à défaut date d'upload.
        // L'ancien wrapping par session a disparu — la session n'est plus
        // qu'une donnée portée par chaque entrée (pour la chip "device").
        const allFileEntries = [];
        for (const s of sessionsToRender) {
            for (const f of (s.uploads || [])) {
                if (_detailFileId && _detailFileId !== f.id) continue;
                allFileEntries.push({ f, s });
            }
        }
        allFileEntries.sort((a, b) => {
            let ka, kb, cmp;
            if (_sortKey === 'title') {
                ka = (a.f.original_filename || '').toLowerCase();
                kb = (b.f.original_filename || '').toLowerCase();
                cmp = ka < kb ? -1 : (ka > kb ? 1 : 0);
            } else if (_sortKey === 'duration') {
                // null/undefined → traités comme -Infinity en asc (fin en desc)
                ka = (a.f.audio_duration_seconds == null) ? -1 : Number(a.f.audio_duration_seconds);
                kb = (b.f.audio_duration_seconds == null) ? -1 : Number(b.f.audio_duration_seconds);
                cmp = ka - kb;
            } else {
                ka = (a.f.meeting_datetime || a.f.created_at || '');
                kb = (b.f.meeting_datetime || b.f.created_at || '');
                cmp = ka < kb ? -1 : (ka > kb ? 1 : 0);
            }
            return cmp * (_sortDir === 'desc' ? -1 : 1);
        });
        _refreshSortToggleUi();

        const rowsHtml = allFileEntries.map(({ f, s }) => {
                // Si une vue détail est active et ce fichier n'est pas le
                // détail demandé, on le saute (un seul fichier visible).
                if (_detailFileId && _detailFileId !== f.id) return '';
                const isDetailView = (_detailFileId === f.id);
                const quality = (f.audio_quality_score !== null && f.audio_quality_score !== undefined)
                    ? ` <span class="quality-help" title="Indice de qualité audio (1 à 5). Calculé automatiquement par le worker de transcodage selon le niveau RMS, la proportion de silence, la durée et la fréquence d'échantillonnage.">i</span> ${f.audio_quality_score.toFixed(1)}/5`
                    : '';
                const progress = pipelineProgress(f.status, f.status_message);
                const fileStatusClass = `file-badge-${f.status || 'pending'}`;
                // Date affichée : on prend la date *de réunion* surchargée par
                // l'utilisateur si disponible, sinon la date d'upload. La
                // classe is-default/is-overridden pilote l'italique (italique
                // = pas modifié par l'utilisateur).
                const dateSource = f.meeting_datetime || f.created_at;
                const fileDateLabel = _formatDateCompact(dateSource);
                const dateClass = f.meeting_datetime_overridden ? 'is-overridden' : 'is-default';
                const fileDurLabel = _formatDuration(f.audio_duration_seconds);
                // Label "device" affiché en chip inline. Priorité :
                // 1) device_label fourni par le serveur (sessions L-XXX :
                //    "Upload local") ; 2) nom du device enrôlé associé au
                //    qr_token via _devicesByQrToken ; 3) simple_code en
                //    dernier recours.
                const _devForRow = _devicesByQrToken[s.qr_token || ''];
                const deviceLabelForRow = s.device_label
                    || (_devForRow && _devForRow.name)
                    || s.simple_code
                    || '';
                const analyseClass = (progress.scan === 100 && !progress.blocked) ? 'done'
                    : (progress.active === 'analyse' ? (progress.blocked ? 'blocked' : 'active') : '');
                const transcodeClass = (progress.transcode === 100) ? 'done'
                    : (progress.active === 'transcodage' ? (progress.error ? 'blocked' : 'active') : '');
                const transferClass = (progress.transfer === 100) ? 'done'
                    : (progress.active === 'transfert' ? 'active' : '');
                const canComputeImpact = (f.status === 'transcoded' || f.status === 'transferring' || f.status === 'transferred');
                const cache = impactCache[f.id];
                const loading = impactLoading.has(f.id);
                const impactTooltip = loading
                    ? 'Analyse en cours...'
                    : (cache
                        ? `${cache.text} (Maj: ${cache.at})`
                        : 'Impact non calculé. Cliquez sur cette icône pour calculer et afficher l\'impact de la normalisation.');
                const impactIcon = canComputeImpact
                    ? `<button class="impact-icon-btn ${loading ? 'loading' : (cache ? 'computed' : '')}"
                           onclick="loadNormalizationImpact('${f.id}')"
                           title="${escapeHtml(impactTooltip)}"
                           ${loading ? 'disabled' : ''}>i</button>`
                    : '';
                // Liste des audios téléchargeables pour ce fichier — sera
                // mergée avec les transcripts par loadTranscriptStatus pour
                // produire un seul dropdown au lieu de 3 blocs Source/
                // Transcodé/Transféré qui prenaient toute la hauteur.
                const audioDownloadsList = [];
                if (f.source_available) audioDownloadsList.push({
                    label: 'Audio source', dl: f.source_download_url, stream: f.source_stream_url,
                });
                if (f.transcoded_available) audioDownloadsList.push({
                    label: 'Audio transcodé', dl: f.transcoded_download_url, stream: f.transcoded_stream_url,
                });
                if (f.transferred_available) audioDownloadsList.push({
                    label: 'Audio transféré (interne)', dl: f.transferred_download_url, stream: f.transferred_stream_url,
                });
                const audioDownloadsAttr = encodeURIComponent(JSON.stringify(audioDownloadsList));
                // Cache le chemin de fer une fois le pipeline pré-transcription
                // abouti (fichier transféré côté interne). La 4e étape
                // (transcription IA) est affichée séparément par le bandeau
                // de loadTranscriptStatus, donc plus besoin du rail visuel
                // qui prend de la place. On laisse le rail visible pendant
                // l'analyse / transcodage / transfert pour montrer où on en
                // est en temps réel.
                const pipelineDone = (f.status === 'transferred');
                const railroadBlock = pipelineDone ? '' : `
                    <div class="pipeline-box" title="Progression du pipeline en chemin de fer: analyse, transcodage, transfert, transcription">
                        <div class="railroad">
                            <div class="rail-segment ${analyseClass}">
                                <span class="rail-node">1</span><span class="rail-line"></span>
                            </div>
                            <div class="rail-segment ${transcodeClass}">
                                <span class="rail-node">2</span><span class="rail-line"></span>
                            </div>
                            <div class="rail-segment ${transferClass}">
                                <span class="rail-node">3</span><span class="rail-line"></span>
                            </div>
                            <div class="rail-segment" data-transcribe-segment="${f.id}">
                                <span class="rail-node">4</span><span class="rail-line rail-line-tail"></span>
                            </div>
                        </div>
                        <div class="rail-labels">
                            <span>Analyse ${progress.scan}%</span>
                            <span>Transcodage ${progress.transcode}%</span>
                            <span>Transfert ${progress.transfer}%</span>
                            <span data-transcribe-label="${f.id}">Transcription</span>
                        </div>
                    </div>`;
                // Bandeau d'alerte rouge inline si l'antivirus a bloqué le fichier
                // (scan_infected / quarantined). On l'insère AVANT le contenu
                // normal de la row pour que ce soit la première chose lue par
                // l'utilisateur. Le dot rouge fixe + tooltip clair complètent.
                const VIRUS_STATES = new Set(['scan_infected', 'quarantined']);
                const isVirusBlocked = VIRUS_STATES.has(f.status);
                const virusBanner = isVirusBlocked
                    ? `<div class="file-row-virus-banner" role="alert">
                         <span class="file-row-virus-icon" aria-hidden="true">⚠</span>
                         <span class="file-row-virus-msg">
                           <strong>Virus détecté</strong> — fichier
                           ${f.status === 'quarantined' ? 'mis en quarantaine' : 'bloqué par l\'antivirus'}.
                           Aucun téléchargement possible. Si vous pensez à un
                           faux positif, contactez un administrateur.
                         </span>
                       </div>`
                    : '';
                if (!isDetailView) {
                    // ── Vue LISTE COMPACTE ─────────────────────────────
                    // Une seule ligne + chevron expandable pour le résumé :
                    //   • point statut transcription (rollover = label complet)
                    //   • titre cliquable (= suggested_filename si dispo, sinon
                    //     filename technique) — ouvre vue détail
                    //   • date+durée
                    //   • chevron ▶ : déplie inline le résumé sans quitter la liste
                    //   • bouton Supprimer
                    return `<div class="file-row-compact-wrapper${isVirusBlocked ? ' file-row-virus' : ''}" data-file-row="${f.id}" data-row-click-target="${f.id}">
                        ${virusBanner}
                        <div class="file-row-compact">
                            <!-- transcript-section caché : sert juste à
                                 déclencher loadTranscriptStatus qui mettra
                                 à jour la couleur du dot inline via JS. -->
                            <div class="transcript-section transcript-section--inline"
                                 data-transcript-file-id="${f.id}"
                                 data-audio-downloads="${audioDownloadsAttr}"
                                 data-compact="1"
                                 style="display:none;"></div>
                            <!-- TKT-101 : tag DSFR de statut (remplace l'ancienne
                                 pastille ● 6px par un libellé textuel + icône
                                 fr-icon-*). Le rendu initial s'appuie sur
                                 file.status ; loadTranscriptStatus remplacera
                                 ensuite le tag par le statut transcription dès
                                 que celui-ci est disponible. -->
                            ${_renderStatusTag(f.status, { fileId: f.id, virus: isVirusBlocked })}
                            <a href="#" class="file-row-title" data-file-id="${f.id}"
                               onclick="event.preventDefault();showFileDetail('${f.id}');"
                               title="${escapeHtml(f.original_filename)}">
                                ${escapeHtml(f.original_filename)}
                            </a>
                            <!-- Chip "device" inline : remplace le wrapping par
                                 session qu'on avait avant le passage en liste
                                 à plat. Affiche le nom du device enrôlé (ou
                                 'Upload local' pour les sessions L-XXXXXXXX). -->
                            <span class="file-row-device ${s.is_local_upload ? 'is-local' : ''}"
                                  title="${escapeHtml(s.simple_code || '')}">
                                ${escapeHtml(deviceLabelForRow)}
                            </span>
                            <!-- Hint file d'attente Kevent (visible uniquement
                                 quand le pipeline est en cours — peuplé par
                                 _pollQueueHintAll via /api/queue-status,
                                 vide sinon). -->
                            <span class="file-row-queue-hint"
                                  data-queue-hint-for="${f.id}"></span>
                            <button class="file-row-expand" type="button"
                                    onclick="toggleRowExpand(this)"
                                    aria-label="Voir le résumé">
                                <span class="file-row-expand-icon">▶</span>
                                <span class="file-row-expand-label">détails</span>
                            </button>
                            <span class="file-row-meta">
                                <span class="file-row-meta-date ${dateClass}"
                                      title="${f.meeting_datetime_overridden ? 'Date de réunion saisie par l\'utilisateur' : 'Date d\'upload (cliquez le fichier pour saisir la vraie date de réunion)'}">${escapeHtml(fileDateLabel)}</span>
                                ${fileDurLabel ? `<span class="file-row-meta-dur">${escapeHtml(fileDurLabel)}</span>` : ''}
                            </span>
                            <button type="button" class="icon-btn file-row-delete"
                                    onclick="deleteFile('${f.id}', '${escapeHtml(f.original_filename).replace(/'/g, '&#39;')}')"
                                    title="Mettre à la corbeille (supprimée automatiquement après 30 jours)"
                                    aria-label="Mettre à la corbeille">
                                ${ICONS.trash}
                            </button>
                        </div>
                        <!-- Zone résumé révélée par le chevron, sans le statut
                             technique (qui reste accessible via tooltip de la
                             pastille). -->
                        <div class="file-row-expanded" data-expanded-file-id="${f.id}" style="display:none;">
                            <div class="file-row-expanded-summary"
                                 data-expanded-summary-for="${f.id}"></div>
                        </div>
                    </div>`;
                }
                // ── Vue DÉTAIL ──────────────────────────────────────────
                // Mode "page" : on cache tout le reste (header de la session
                // + autres fichiers) via la classe parent `.detail-active`
                // pour ne montrer QUE le détail demandé.
                // Titre éditable (suggested_filename, persisté via
                // /api/file/<id>/rename) + nom technique en petit dessous.
                // Statut technique uniquement via tooltip sur la pastille.
                // Résumé déployé persistant (pas de <details>).
                return `<div class="file-detail${isVirusBlocked ? ' file-detail-virus' : ''}" data-detail-file-id="${f.id}">
                    ${virusBanner}
                    <div class="file-detail-header">
                        <button type="button" class="file-detail-back"
                                onclick="showFilesList()"
                                title="Retour à la liste des réunions"
                                aria-label="Retour à la liste">← Liste</button>
                        <button type="button" class="icon-btn"
                                onclick="deleteFile('${f.id}', '${escapeHtml(f.original_filename).replace(/'/g, '&#39;')}')"
                                title="Mettre à la corbeille (supprimée automatiquement après 30 jours)"
                                aria-label="Mettre à la corbeille">
                            ${ICONS.trash}
                        </button>
                    </div>
                    <div class="file-detail-title-row">
                        <input class="file-detail-title-input" type="text"
                               value="${escapeHtml(f.original_filename)}"
                               data-original-title="${escapeHtml(f.original_filename)}"
                               data-detail-title-for="${f.id}"
                               placeholder="Titre de la réunion" />
                        <button class="file-detail-rename-btn fr-btn fr-btn--sm fr-btn--secondary"
                                onclick="renameDetailTitle('${f.id}', this)" disabled>
                            Renommer
                        </button>
                        <span class="file-detail-upload-info"
                              title="Date d'upload du fichier (immuable, technique)">
                            Uploadé le ${escapeHtml(_formatDateCompact(f.created_at))}
                        </span>
                    </div>
                    <!-- Date *réelle* de la réunion, surchargée par
                         l'utilisateur. NULL côté serveur = pas d'override,
                         l'UI retombe sur created_at pour l'affichage et
                         le tri. Le bouton ↺ remet à NULL (clear). -->
                    <div class="file-detail-meeting-row">
                        <span class="file-detail-meeting-label">Date de la réunion :</span>
                        <input type="datetime-local"
                               class="file-detail-meeting-input"
                               data-meeting-dt-for="${f.id}"
                               value="${_isoToDatetimeLocal(f.meeting_datetime) || ''}"
                               placeholder="${_isoToDatetimeLocal(f.created_at) || ''}"
                               onchange="saveMeetingDatetime('${f.id}', this)"
                               onblur="saveMeetingDatetime('${f.id}', this)" />
                        <button class="file-detail-meeting-reset"
                                data-meeting-dt-reset-for="${f.id}"
                                title="Effacer la date de réunion (retombe sur la date d'upload)"
                                aria-label="Effacer la date de réunion"
                                onclick="resetMeetingDatetime('${f.id}')"
                                ${f.meeting_datetime ? '' : 'disabled'}>↺</button>
                        <span class="file-detail-meeting-status"
                              data-meeting-dt-status-for="${f.id}"></span>
                    </div>
                    <!-- Ligne sous le titre : juste date+durée à gauche +
                         bouton (i) coloré à droite. Les infos techniques
                         (qualité, statut, étapes, normalisation) sont
                         derrière le bouton (i) qui ouvre un modal. -->
                    <div class="file-detail-techline">
                        <span class="file-row-meta">
                            <span class="file-row-meta-date ${dateClass}">${escapeHtml(fileDateLabel)}</span>
                            ${fileDurLabel ? `<span class="file-row-meta-dur">${escapeHtml(fileDurLabel)}</span>` : ''}
                        </span>
                        <span class="file-detail-source-filename"
                              title="Nom d'origine du fichier audio">
                            ${escapeHtml(f.original_filename)}
                        </span>
                        <button class="file-detail-info-btn"
                                type="button"
                                data-file-info-btn="${f.id}"
                                onclick="openFileInfoModal('${f.id}')"
                                title="Détails techniques (statut, qualité, étapes IA, normalisation)"
                                aria-label="Voir les détails techniques">i</button>
                    </div>
                    <!-- transcript-section caché pour déclencher
                         loadTranscriptStatus qui met à jour la couleur du
                         bouton (i) selon le status. -->
                    <div class="transcript-section transcript-section--inline"
                         data-transcript-file-id="${f.id}"
                         data-audio-downloads="${audioDownloadsAttr}"
                         data-compact="1"
                         data-info-btn-target="${f.id}"
                         style="display:none;"></div>
                    <!-- Hint file d'attente Kevent (idem PWA) — poll 10s tant
                         que la transcription n'est pas terminée. -->
                    <p class="fr-text--sm fr-text-mention--grey queue-hint"
                       data-queue-hint-for="${f.id}"
                       style="margin:.4rem 0 0 0;min-height:1.1em;"></p>
                    ${railroadBlock}
                    <!-- Section résumé toujours visible (pas de <details>
                         repliable en vue détail). Le contenu (key_points
                         + dropdown downloads) est injecté par
                         loadTranscriptStatus en mode non-compact. -->
                    <div class="file-detail-fullinfo transcript-section"
                         data-transcript-file-id="${f.id}"
                         data-audio-downloads="${audioDownloadsAttr}"
                         data-persistent-summary="1"></div>
                </div>`;
            }).join('');

        container.innerHTML = rowsHtml || _renderMeetingsEmptyState();
        // Affiche / masque l'en-tête fr-table (visible uniquement en mode liste
        // — pas en vue détail, et pas en empty state).
        const header = document.getElementById('sessions-table-header');
        if (header) {
            const showHeader = !!rowsHtml && !_detailFileId;
            header.style.display = showHeader ? '' : 'none';
        }

        const fileCount = allFileEntries.length;
        const countLabel = document.getElementById('file-count');
        if (countLabel) {
            countLabel.textContent = fileCount
                ? `${fileCount} réunion${fileCount > 1 ? 's' : ''}`
                : '';
        }
        const purgeBtn = document.getElementById('purge-btn');
        if (purgeBtn) purgeBtn.disabled = fileCount === 0;

        // Lazy fetch transcript metadata for each file row to populate the
        // download section + key_points subtitle. Throttled by browser parallel
        // limit; fired and forgotten — failures leave the section empty.
        document.querySelectorAll('[data-transcript-file-id]').forEach((el) => {
            const fid = el.dataset.transcriptFileId;
            if (fid && !el.dataset.loaded) {
                el.dataset.loaded = '1';
                loadTranscriptStatus(fid, el);
            }
        });
    } catch (e) {
        console.error('Failed to load sessions', e);
        const container = document.getElementById('sessions-list');
        const activityMiniText = document.getElementById('activity-mini-text');
        const activitySpinner = document.getElementById('activity-spinner');
        if (container) {
            container.innerHTML = '<p style="color:#b91c1c;font-size:0.85rem;">Erreur chargement sessions. Rechargez la page.</p>';
        }
        if (activityMiniText) {
            activityMiniText.textContent = 'Activités: indisponibles.';
        }
        if (activitySpinner) {
            activitySpinner.classList.remove('active');
            activitySpinner.title = 'Activités indisponibles';
        }
    } finally {
        // Restaure la position de scroll après l'innerHTML, sinon la
        // page remonte en haut à chaque polling 15s.
        if (Number.isFinite(savedScrollY)) {
            requestAnimationFrame(() => window.scrollTo(0, savedScrollY));
        }
    }
}

// ─── Transcript / CR downloads (Feature 3) ─────────────────────────────────
// For each file row we lazy-load the available outputs (transcript, corrected,
// CR) and render a download row + key_points subtitle. One HTTP per file —
// acceptable since we display ~10-20 files and the endpoint is internal-only.
//
// TODO follow-up: button to send the rendered document to the user's Drive
// folder. Out of scope for this PR — needs OAuth scope + Drive provider config.

const TRANSCRIPT_KIND_LABELS = {
    'transcript':              'Transcription brute',
    'transcript-tagged':       'Transcription par locuteur',
    'transcript-corrected':    'Transcription (sigles corrigés)',
    'transcript-cleaned':      'Transcription nettoyée',
    'transcript-reformulated': 'Discours indirect',
};

const TRANSCRIPT_KIND_FORMATS = {
    'transcript':              ['txt', 'md', 'docx', 'odt'],
    'transcript-tagged':       ['md', 'docx', 'odt'],
    'transcript-corrected':    ['md', 'docx', 'odt'],
    'transcript-cleaned':      ['txt', 'md', 'docx', 'odt'],
    'transcript-reformulated': ['md', 'docx', 'odt'],
};

const CR_FORMATS = ['md', 'docx', 'odt', 'json'];

// Map globale { selectId → [iconsHtml par index d'option] } pour éviter
// les pièges d'escape HTML quand on stocke du HTML dans un attribut.
const _otherDlIcons = new Map();

// Met à jour la rangée d'icônes à droite du select "Autres téléchargements".
// On résout le target via le DOM voisin (sel.closest(.downloads-other-row))
// et non via document.querySelector — plusieurs containers transcript-section
// peuvent partager le même selectId (vue compacte + vue détail) et un
// querySelector global retourne le PREMIER (souvent l'élément caché).
function updateOtherDownload(sel) {
    const row = sel.closest('.downloads-other-row');
    const target = row && row.querySelector('.downloads-other-icons');
    if (!target) return;
    const list = _otherDlIcons.get(sel.id) || [];
    const idx = sel.selectedIndex;
    target.innerHTML = (idx >= 0 && list[idx]) || '';
}

// Met à jour les boutons Télécharger/Écouter selon l'option sélectionnée
// dans le dropdown unifié (audios + transcripts + meeting-cr).
function updateDownloadButtons(select) {
    const opt = select.options[select.selectedIndex];
    if (!opt) return;
    const dl = opt.getAttribute('data-dl') || '#';
    const stream = opt.getAttribute('data-stream') || '';
    const isAudio = !!opt.getAttribute('data-audio');
    const block = select.closest('.downloads-block');
    if (!block) return;
    const dlBtn = block.querySelector('.downloads-btn-dl');
    const streamBtn = block.querySelector('.downloads-btn-stream');
    if (dlBtn) dlBtn.setAttribute('href', dl);
    if (streamBtn) {
        if (isAudio && stream) {
            streamBtn.setAttribute('href', stream);
            streamBtn.style.display = '';
        } else {
            streamBtn.style.display = 'none';
        }
    }
}

function renderDownloadRow(label, fileId, kind, formats, isCR) {
    const buttons = formats.map((ext) => {
        const url = isCR
            ? `/api/file/meeting-cr/${ext}/${fileId}`
            : `/api/file/transcript/${kind}/${ext}/${fileId}`;
        return `<a class="transcript-fmt-btn" href="${url}" target="_blank" rel="noopener" download>.${ext}</a>`;
    }).join('');
    return `<div class="transcript-download-row">
        <span class="transcript-download-label">${escapeHtml(label)}</span>
        <span class="transcript-download-buttons">${buttons}</span>
    </div>`;
}

// Map des statuts transcription DB → label UI + indique si on doit re-poll.
// Met à jour la 4e étape du chemin de fer (rail-segment + label associé)
// selon le backend de transcription en cours. Appelé depuis
// loadTranscriptStatus() pour rester cohérent avec ce que voit
// l'utilisateur dans le bandeau statut.
function updateTranscribeRail(fileId, engine, status) {
    const segment = document.querySelector(`[data-transcribe-segment="${fileId}"]`);
    const label = document.querySelector(`[data-transcribe-label="${fileId}"]`);
    if (!segment || !label) return;
    const e = (engine || '').toLowerCase();
    const s = (status || '').toLowerCase();
    // Nom utilisateur du backend
    const engineNames = {
        stub: 'Transcription (test)',
        mcr:  'Transcription MCR',
        kevent: 'Transcription IA',
    };
    const baseName = engineNames[e] || 'Transcription';
    let cls = '';
    let label_text = baseName;
    if (s === 'completed' || s === 'kevent_completed' || s === 'mcr_pushed') {
        cls = 'done';
        label_text = `${baseName} terminée`;
    } else if (s === 'kevent_partially_completed') {
        cls = 'done';
        label_text = `${baseName} (partielle)`;
    } else if (s === 'failed' || s === 'kevent_failed' || s === 'mcr_auth_failed' || s === 'mcr_rejected' || s === 'mcr_push_failed') {
        cls = 'blocked';
        label_text = `${baseName} échouée`;
    } else if (s === 'disabled') {
        cls = '';
        label_text = `${baseName} désactivée`;
    } else if (s) {
        cls = 'active';
        label_text = `${baseName} en cours`;
    }
    segment.className = `rail-segment ${cls}`;
    label.textContent = label_text;
}

const TRANSCRIPT_STATUS_LABELS = {
    'pending':                     { label: 'Transcription en attente', polling: true },
    'processing':                  { label: 'Transcription en cours (stub)', polling: true },
    'completed':                   { label: 'Transcription disponible', polling: false },
    'failed':                      { label: 'Transcription échouée', polling: false },
    'kevent_queued':               { label: 'Transcription Kevent — file d\'attente Mirai', polling: true },
    'kevent_transcribing':         { label: 'Transcription Kevent — Whisper en cours', polling: true },
    'kevent_processing':           { label: 'Transcription Kevent — traitement', polling: true },
    'kevent_completed':            { label: 'Pipeline Kevent terminé', polling: false },
    'kevent_partially_completed':  { label: 'Pipeline Kevent partiel — certaines étapes ont échoué', polling: false },
    'kevent_failed':               { label: 'Accès au backend IA refusé ou indisponible', polling: false },
    'mcr_pushed':                  { label: 'Poussé vers MCR', polling: false },
    'mcr_auth_failed':             { label: 'MCR : échec auth', polling: false },
    'mcr_rejected':                { label: 'MCR : rejeté', polling: false },
    'mcr_push_failed':             { label: 'MCR : échec push', polling: false },
    'disabled':                    { label: 'Transcription désactivée', polling: false },
};

// Étapes du pipeline IA — ordre d'exécution. Réutilisé pour construire la
// checklist de progression dans le tooltip du (i) et le modal Détails.
const PIPELINE_STEPS = [
    { key: 'transcript',              label: 'Transcription brute' },
    { key: 'transcript-tagged',       label: 'Identification des locuteurs' },
    { key: 'transcript-corrected',    label: 'Correction des sigles' },
    { key: 'transcript-cleaned',      label: 'Nettoyage hors-sujet' },
    { key: 'transcript-reformulated', label: 'Discours indirect' },
    { key: 'meeting-cr',              label: 'Compte-rendu structuré' },
];

const _FAILED_TRANSCRIPT_STATUSES = new Set([
    'failed', 'kevent_failed',
    'mcr_auth_failed', 'mcr_rejected', 'mcr_push_failed',
]);

// Construit le tooltip multi-ligne du bouton (i). Chaque étape porte un
// glyphe :
//   ✓  étape réussie (output présent)
//   ✗  étape échouée explicitement (statut failed + output absent)
//   ⏳  étape en cours (pipeline qui tourne + output absent)
//   ☐  étape en attente (pas encore tentée)
// Les sauts de ligne \n sont rendus par les tooltips natifs (vu sur
// Firefox/Chrome desktop).
function _buildInfoTooltip(status, engine, outputs, meta) {
    const head = (meta && meta.label) || status || 'Statut inconnu';
    const isFail = _FAILED_TRANSCRIPT_STATUSES.has(status);
    const isRunning = !!(meta && meta.polling);
    const lines = [
        `Pipeline IA — ${head}${engine ? ' (' + engine + ')' : ''}`,
        '─────────────',
    ];
    // Première étape "en cours" qu'on rencontre = la prochaine attendue.
    let firstPendingMarked = false;
    for (const step of PIPELINE_STEPS) {
        const done = !!(outputs || {})[step.key];
        let glyph;
        let suffix = '';
        if (done) {
            glyph = '✓';
        } else if (isFail) {
            glyph = '✗';
            suffix = ' (échec)';
        } else if (isRunning && !firstPendingMarked) {
            glyph = '⏳';
            suffix = ' (en cours)';
            firstPendingMarked = true;
        } else if (isRunning) {
            glyph = '☐';
            suffix = ' (en attente)';
        } else {
            glyph = '☐';
        }
        lines.push(`${glyph} ${step.label}${suffix}`);
    }
    lines.push('─────────────');
    lines.push('Cliquer pour voir le détail complet.');
    return lines.join('\n');
}

async function loadTranscriptStatus(fileId, container) {
    try {
        const resp = await fetch(`/api/file/transcript-status/${fileId}`);
        if (!resp.ok) {
            container.innerHTML = '';
            return;
        }
        const data = await resp.json();
        if (!data.available) {
            container.innerHTML = '';
            // re-test dans 30s : la row apparaîtra dès que internal-ingester a intégré
            setTimeout(() => loadTranscriptStatus(fileId, container), 30000);
            return;
        }
        const status = (data.transcription_status || '').toLowerCase();
        const engine = data.transcription_engine || '';
        const meta = TRANSCRIPT_STATUS_LABELS[status] || { label: status || 'Statut inconnu', polling: false };
        const isInProgress = meta.polling;
        // Met aussi à jour la 4e étape du chemin de fer dans la session.
        updateTranscribeRail(fileId, engine, status);

        const outputs = data.outputs || {};
        const kp = data.key_points_summary || '';
        const title = data.suggested_filename || '';
        // Key points : collapse par défaut (résumé peut faire 1000+ chars).
        // On garde le titre toujours visible, key_points derrière un
        // <details> repliable — SAUF en vue détail (data-persistent-summary=1)
        // où le résumé est toujours visible.
        const persistent = container.getAttribute('data-persistent-summary') === '1';
        const subtitle = (title || kp)
            ? `<div class="transcript-meta">
                ${title && !persistent ? `<div class="transcript-meta-title">${escapeHtml(title)}</div>` : ''}
                ${kp
                    ? (persistent
                        ? `<div class="transcript-meta-persistent-title">Résumé</div><pre class="transcript-meta-keypoints">${escapeHtml(kp)}</pre>`
                        : `<details class="transcript-meta-details"><summary>Résumé</summary><pre class="transcript-meta-keypoints">${escapeHtml(kp)}</pre></details>`)
                    : ''}
              </div>`
            : '';

        // Classification visuelle du bandeau : erreur (rouge ⚠), succès
        // (vert ✓), en cours (bleu pulse) ou neutre (gris).
        const failedStatuses = new Set([
            'failed', 'kevent_failed',
            'mcr_auth_failed', 'mcr_rejected', 'mcr_push_failed',
        ]);
        const successStatuses = new Set([
            'completed', 'kevent_completed', 'kevent_partially_completed',
            'mcr_pushed',
        ]);
        let bannerClass = '';
        let dotClass = 'off';
        let leadIcon = '';
        if (failedStatuses.has(status)) {
            bannerClass = 'transcript-status-error';
            dotClass = 'err';
            leadIcon = '<span class="transcript-status-icon" aria-hidden="true">⚠</span>';
        } else if (successStatuses.has(status)) {
            bannerClass = 'transcript-status-ok';
            dotClass = 'ok';
            leadIcon = '<span class="transcript-status-icon" aria-hidden="true">✓</span>';
        } else if (isInProgress) {
            dotClass = 'on';
        }
        // Diagnostic per-step : on déduit les étapes manquantes des
        // outputs absents (visible uniquement quand la transcription
        // est terminée, partiellement ou non, ou échouée).
        const STEP_INFO = {
            'transcript':              { label: 'Transcription brute',          desc: 'Texte issu du Whisper (faster-whisper). Étape obligatoire pour toutes les autres.' },
            'transcript-tagged':       { label: 'Identification des locuteurs', desc: 'Diarisation pyannote — sépare le texte par interlocuteur. Peut échouer sur les enregistrements très courts ou monolocuteurs.' },
            'transcript-corrected':    { label: 'Correction des sigles',        desc: 'LLM relit le texte avec un glossaire pour corriger les acronymes mal entendus (ex: "EFS" → "EHS" repassé en "EFS").' },
            'transcript-cleaned':      { label: 'Nettoyage hors-sujet',         desc: 'LLM retire les passages parasites (faux départs, bruits ambiants verbalisés).' },
            'transcript-reformulated': { label: 'Discours indirect',            desc: 'LLM reformule au style indirect ("X a dit que...") pour une lecture rapide.' },
            'meeting-cr':              { label: 'Compte-rendu structuré',      desc: 'LLM produit l\'analyse 5 sections : acteurs, thématiques, décisions, gaps, recommandations.' },
        };
        let stepsDetails = '';
        // On n'affiche le diagnostic que lorsque la transcription est terminée
        // (en cours = pas encore d'outputs) — sinon ça ferait du bruit.
        const showSteps = (status === 'kevent_completed'
                          || status === 'kevent_partially_completed'
                          || status === 'kevent_failed'
                          || status === 'completed' || status === 'failed');
        if (showSteps) {
            const rows = Object.keys(STEP_INFO).map(k => {
                const info = STEP_INFO[k];
                const ok = !!outputs[k];
                const icon = ok ? '✓' : '✗';
                const color = ok ? '#10b981' : '#b91c1c';
                const note = (!ok && k === 'transcript-tagged')
                    ? ' <small style="color:#94a3b8">(pyannote a peut-être eu un problème avec ce signal — voir logs côté admin)</small>'
                    : '';
                return `<div class="status-step">
                    <span style="color:${color};font-weight:700;">${icon}</span>
                    <span class="status-step-label">${escapeHtml(info.label)}</span>
                    <span class="status-step-desc">${escapeHtml(info.desc)}${note}</span>
                </div>`;
            }).join('');
            stepsDetails = `<details class="status-details">
                <summary>Voir le détail des étapes</summary>
                <div class="status-step-list">${rows}</div>
            </details>`;
        }
        const statusBadge = `<div class="transcript-status-line ${bannerClass}">
            ${leadIcon}
            <span class="transcript-status-spinner ${dotClass}"></span>
            <span class="transcript-status-label">${escapeHtml(meta.label)}${engine ? ` <small style="color:#94a3b8">(${escapeHtml(engine)})</small>` : ''}</span>
        </div>${stepsDetails}`;

        // Nouvelle UX downloads : on liste les TYPES de document (pas les
        // formats × types), avec à droite une rangée de boutons-icône, un
        // par format disponible. Pour un néophyte : "Ah je veux le
        // compte-rendu — je clique l'icône Word." Plus de jargon dans le
        // libellé, plus de dropdown 12 lignes.
        let audioOptions = [];
        try {
            const raw = container.getAttribute('data-audio-downloads') || '';
            audioOptions = raw ? JSON.parse(decodeURIComponent(raw)) : [];
        } catch (e) { audioOptions = []; }

        const FMT_ICON = {
            txt:  { svg: ICONS.fmt_txt,  title: 'Texte simple (.txt)' },
            md:   { svg: ICONS.fmt_md,   title: 'Markdown (.md)' },
            docx: { svg: ICONS.fmt_docx, title: 'Word (.docx)' },
            odt:  { svg: ICONS.fmt_odt,  title: 'LibreOffice (.odt)' },
            json: { svg: ICONS.fmt_json, title: 'JSON (.json) — données brutes' },
        };
        const fmtIconHtml = (ext, url) => {
            const fi = FMT_ICON[ext] || { svg: ICONS.fmt_txt, title: `.${ext}` };
            return `<a class="downloads-icon-btn" href="${escapeHtml(url)}"
                       download target="_blank" rel="noopener"
                       title="${escapeHtml(fi.title)}"
                       aria-label="${escapeHtml(fi.title)}">${fi.svg}</a>`;
        };

        // Layout : on met en HAUT les 2 actions courantes (transcription
        // nettoyée + écouter audio interne), puis une section "Autres
        // téléchargements" avec un menu déroulant qui révèle les icônes
        // de format pour le type sélectionné. Plus de bruit visuel.
        const defaultRows = [];
        const otherRows = [];

        const audioDlIcon = (url) =>
            `<a class="downloads-icon-btn" href="${escapeHtml(url)}"
               download target="_blank" rel="noopener"
               title="Télécharger l'audio" aria-label="Télécharger l'audio">${ICONS.fmt_audio}</a>`;
        const audioPlayIcon = (url) =>
            `<a class="downloads-icon-btn downloads-icon-btn-play"
               href="${escapeHtml(url)}" target="_blank" rel="noopener"
               title="Écouter dans le navigateur" aria-label="Écouter">${ICONS.fmt_play}</a>`;

        // Direct (haut de section) = uniquement CR + audio (interne).
        // Le reste passe dans la dropdown "Autres" :
        //   - simple mode (default) : nettoyée + discours indirect seulement
        //   - avancé (toggle ou Alt) : toutes les transcriptions + audios non-interne
        const SIMPLE_OTHER_KINDS = new Set([
            'transcript-cleaned',
            'transcript-reformulated',
        ]);
        const advanced = effectiveAdvancedDl();
        for (const kind of Object.keys(TRANSCRIPT_KIND_LABELS)) {
            if (!outputs[kind]) continue;
            if (!advanced && !SIMPLE_OTHER_KINDS.has(kind)) continue;
            const formats = TRANSCRIPT_KIND_FORMATS[kind] || ['txt'];
            const icons = formats.map((ext) =>
                fmtIconHtml(ext, `/api/file/transcript/${kind}/${ext}/${fileId}`)
            ).join('');
            otherRows.push({ label: TRANSCRIPT_KIND_LABELS[kind], iconsHtml: icons });
        }

        // Audios : interne → accès direct (toujours visible) ; les variantes
        // source/transcodé ne sont accessibles QU'EN MODE AVANCÉ (dropdown).
        for (const a of audioOptions) {
            const isInternal = (a.label || '').toLowerCase().includes('interne');
            if (isInternal && (a.stream || a.dl)) {
                const icons = [];
                if (a.stream) icons.push(audioPlayIcon(a.stream));
                if (a.dl)     icons.push(audioDlIcon(a.dl));
                defaultRows.push({
                    label: "Écouter / Télécharger l'audio (interne)",
                    iconsHtml: icons.join(''),
                });
            } else if (advanced) {
                const icons = [];
                if (a.dl) icons.push(audioDlIcon(a.dl));
                if (a.stream) icons.push(audioPlayIcon(a.stream));
                otherRows.push({ label: a.label, iconsHtml: icons.join('') });
            }
        }

        // Compte-rendu structuré : accès direct (en TÊTE des défauts).
        if (outputs['meeting-cr']) {
            const icons = CR_FORMATS.map((ext) =>
                fmtIconHtml(ext, `/api/file/meeting-cr/${ext}/${fileId}`)
            ).join('');
            defaultRows.unshift({ label: 'Compte-rendu structuré', iconsHtml: icons });
        }

        let dropdownBlock = '';
        if (defaultRows.length > 0 || otherRows.length > 0) {
            const defaultHtml = defaultRows.map(r => `
                <div class="downloads-row">
                    <span class="downloads-row-label">${escapeHtml(r.label)}</span>
                    <span class="downloads-row-icons">${r.iconsHtml}</span>
                </div>`).join('');

            let otherSection = '';
            let pendingOtherSelectId = '';
            if (otherRows.length > 0) {
                // Pas de placeholder : on pré-sélectionne la première entrée.
                // Le HTML des icônes est stocké dans _otherDlIcons (Map JS)
                // pour ne pas dépendre de l'escape HTML d'attribut.
                const optsHtml = otherRows.map((r, i) =>
                    `<option value="${i}"${i === 0 ? ' selected' : ''}>${escapeHtml(r.label)}</option>`
                ).join('');
                const selectId = `other-dl-${fileId}`;
                pendingOtherSelectId = selectId;
                _otherDlIcons.set(selectId, otherRows.map(r => r.iconsHtml));
                otherSection = `
                    <div class="downloads-other-row">
                        <span class="downloads-other-label">Autres :</span>
                        <select class="downloads-other-select fr-select"
                                id="${selectId}"
                                onchange="updateOtherDownload(this)">
                            ${optsHtml}
                        </select>
                        <span class="downloads-row-icons downloads-other-icons"
                              data-other-icons-for="${selectId}"></span>
                    </div>`;
            }
            dropdownBlock = `<div class="downloads-block">${defaultHtml}${otherSection}</div>`;
        }

        container.innerHTML = `${statusBadge}${subtitle}${dropdownBlock}`;
        // Peuple immédiatement les icônes du select "Autres téléchargements"
        // pour la première entrée sélectionnée (sinon la zone reste vide
        // jusqu'au premier change).
        container.querySelectorAll('.downloads-other-select').forEach((sel) => {
            updateOtherDownload(sel);
        });
        // Restaure la sélection du dropdown si l'utilisateur avait avancé
        // (mémorisée par loadSessions dans window._savedDlSelections).
        try {
            const saved = window._savedDlSelections && window._savedDlSelections.get(fileId);
            if (Number.isInteger(saved)) {
                const sel = container.querySelector('.downloads-select');
                if (sel && saved >= 0 && saved < sel.options.length) {
                    sel.selectedIndex = saved;
                    updateDownloadButtons(sel);
                }
            }
        } catch (e) {}
        // Met à jour le titre cliquable de la ligne compacte avec le
        // suggested_filename (généré par l'IA) si disponible — plus
        // parlant que le filename technique poemes013_xxx.mp3. Le
        // rollover affiche le filename d'origine pour traçabilité.
        // Met aussi le tooltip de la pastille statut compacte avec le
        // label complet (ex: "Pipeline Kevent partiel...").
        try {
            if (title) {
                const link = document.querySelector(
                    `.file-row-title[data-file-id="${fileId}"]`
                );
                if (link) {
                    link.textContent = title;
                }
                // En vue détail, pré-remplit l'input éditable du titre
                // une seule fois (puis on n'overwrite plus, l'utilisateur
                // peut être en train de saisir une nouvelle valeur).
                const titleInput = document.querySelector(
                    `[data-detail-title-for="${fileId}"]`
                );
                if (titleInput && !titleInput.dataset.prefilled) {
                    titleInput.value = title;
                    titleInput.dataset.prefilled = '1';
                    titleInput.dataset.originalTitle = title;
                }
            }
            if (container.getAttribute('data-compact') === '1') {
                // TKT-101 : remplace le tag de statut (rendu initial basé sur
                // file.status) par celui qui reflète le statut transcription
                // dès qu'il est connu. L'animation pulse est portée par la
                // classe file-row-status-tag--processing (cf. CSS).
                const tag = document.querySelector(`[data-file-status-tag="${fileId}"]`);
                if (tag) {
                    const friendly = (TRANSCRIPT_STATUS_LABELS[status] || {}).label || status;
                    const tooltip = `Étape en cours : ${friendly}${engine ? ' (' + engine + ')' : ''}`;
                    const info = _statusTagInfo(status);
                    tag.className = `fr-tag fr-tag--sm fr-tag--icon-left ${info.icon} file-row-status-tag file-row-status-tag--${info.kind}`;
                    tag.setAttribute('data-status-kind', info.kind);
                    tag.setAttribute('aria-label', `Statut : ${info.label} — ${tooltip}`);
                    tag.title = tooltip;
                    tag.textContent = info.label;
                }
                // Bouton (i) : tooltip multi-ligne avec checklist par étape
                // (☐/✓/✗). Pulse + bordure bleue si le pipeline tourne.
                const infoBtn = document.querySelector(`[data-file-info-btn="${fileId}"]`);
                if (infoBtn) {
                    infoBtn.title = _buildInfoTooltip(status, engine, outputs, meta);
                    infoBtn.classList.toggle('is-in-progress', isInProgress);
                }
                // Mémorise les infos pour le modal (status raw, engine,
                // outputs map). On ne re-fetch pas quand l'utilisateur
                // clique sur (i), on lit ce cache.
                window._fileInfoCache = window._fileInfoCache || {};
                window._fileInfoCache[fileId] = {
                    status, engine, label: meta.label,
                    outputs: outputs, title, kp,
                    language: data.transcription_language,
                };
                // Hint file d'attente : assure que le poll global tourne
                // tant qu'il existe au moins un fichier non-terminal (liste
                // ou détail). Le poll global ensureQueueHintPolling est
                // idempotent + auto-stop quand plus aucun widget dispo.
                // Pollabilité : on marque le widget queue-hint comme pollable
                // tant que le statut est non-terminal. _pollQueueHintAll ne
                // touchera plus les widgets non-pollables et videra leur texte
                // — ça évite "⏳ Réservation de la file…" qui restait sur les
                // fichiers passés à kevent_failed/_completed/_partially.
                const isPollable = !_TERMINAL_TS.has(status);
                document.querySelectorAll(
                    `[data-queue-hint-for="${fileId}"]`
                ).forEach((el) => {
                    if (isPollable) {
                        el.setAttribute('data-pollable', '1');
                        if (data.kevent_job_id) {
                            el.setAttribute('data-queue-job-id', data.kevent_job_id);
                        }
                    } else {
                        el.removeAttribute('data-pollable');
                        el.removeAttribute('data-queue-job-id');
                        el.textContent = '';
                    }
                });
                if (isPollable) ensureQueueHintPolling();
                // Zone résumé : on n'affiche que les key_points (pas le
                // label statut "Pipeline Kevent partiel..." qui est déjà
                // sur la pastille via tooltip).
                const expandedSummary = document.querySelector(
                    `[data-expanded-summary-for="${fileId}"]`
                );
                if (expandedSummary) {
                    expandedSummary.innerHTML = kp
                        ? `<pre class="transcript-meta-keypoints">${escapeHtml(kp)}</pre>`
                        : `<div class="file-row-expanded-empty">Pas de résumé disponible.</div>`;
                }
            }
        } catch (e) {}

        // Re-poll automatique tant qu'on est en cours, pour ne pas obliger
        // l'utilisateur à recharger la page pour voir la transcription apparaître.
        if (isInProgress) {
            setTimeout(() => loadTranscriptStatus(fileId, container), 15000);
        }
    } catch (e) {
        container.innerHTML = '';
    }
}

// Toast léger en bas-droite : disparaît après 4s.
function showToast(message, kind) {
    const t = document.createElement('div');
    t.textContent = message;
    t.className = `toast toast-${kind || 'info'}`;
    document.body.appendChild(t);
    requestAnimationFrame(() => t.classList.add('toast-show'));
    setTimeout(() => {
        t.classList.remove('toast-show');
        setTimeout(() => t.remove(), 250);
    }, 4000);
}

async function loadNormalizationImpact(fileId) {
    if (impactLoading.has(fileId)) return;
    impactLoading.add(fileId);
    loadSessions();
    try {
        const resp = await fetch(`/api/file/normalization-impact/${fileId}`);
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || 'Erreur analyse');
        const msg =
            `Avant LUFS ${data.source.i}, Après ${data.normalized.i}, ` +
            `ΔLUFS ${data.delta.i}. TP: ${data.source.tp} -> ${data.normalized.tp}. ` +
            `LRA: ${data.source.lra} -> ${data.normalized.lra}. ` +
            `Amélioration cible -16 LUFS: ${data.improvement_to_target_lufs}.`;
        impactCache[fileId] = {
            text: msg,
            at: formatDate(new Date().toISOString(), { withTime: true }),
        };
        showToast('Impact de la normalisation calculé.', 'success');
        // Si le modal est ouvert, met aussi à jour son contenu impact
        const modalImpact = document.getElementById(`modal-impact-${fileId}`);
        if (modalImpact) modalImpact.textContent = msg;
    } catch (e) {
        const msg = `Erreur: ${e.message}`;
        impactCache[fileId] = {
            text: msg,
            at: formatDate(new Date().toISOString(), { withTime: true }),
        };
        showToast(`Impact normalisation : ${e.message}`, 'error');
    } finally {
        impactLoading.delete(fileId);
        loadSessions();
    }
}

// Tabs : navigation entre Mes appareils / Mes transferts / Nouveau code.
// L'onglet par défaut est choisi par le 1er loadDevices selon la présence
// d'un device actif. Persiste le choix dans sessionStorage pour ne pas
// switcher au refresh.
let _tabsInitialised = false;
const TAB_HEADER_LABELS = {
    transfers: 'Mes réunions IA',
    brief: 'Préparation de réunion',
    devices: 'Mes appareils',
    generate: 'Enrôlement d\'appareil',
    trash: 'Corbeille',
};
function activateTab(tabName) {
    document.querySelectorAll('.tab-btn').forEach((b) => {
        const on = b.getAttribute('data-tab') === tabName;
        b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    document.querySelectorAll('.tab-pane').forEach((p) => {
        p.classList.toggle('is-active', p.getAttribute('data-tab') === tabName);
    });
    const headerLabel = document.getElementById('header-tab-label');
    if (headerLabel) {
        const txt = TAB_HEADER_LABELS[tabName] || '';
        headerLabel.textContent = txt ? ' — ' + txt + ' ' : '';
    }
    try { sessionStorage.setItem('mydevices-active-tab', tabName); } catch (e) {}
}
function setupTabs() {
    document.querySelectorAll('.tab-btn').forEach((b) => {
        b.addEventListener('click', () => {
            const target = b.getAttribute('data-tab');
            // Clic sur "Mes réunions IA" = retour à la vue liste (même si
            // on est déjà sur l'onglet transfers en vue détail).
            if (target === 'transfers') {
                showFilesList();
            }
            if (target === 'trash') {
                loadTrash();
            }
            if (target === 'brief') {
                // Migré vers tabs/preparations.js — `window.loadBriefs` est publié
                // par le module au boot. Fallback noop si pas encore chargé.
                if (typeof window.loadBriefs === 'function') window.loadBriefs();
            }
            activateTab(target);
        });
    });
}

async function loadTrash() {
    const container = document.getElementById('trash-list');
    if (!container) return;
    try {
        const resp = await fetch('/api/my-trash');
        const data = await resp.json();
        const files = data.files || [];
        const sessions = data.sessions || [];
        const briefs = data.briefs || [];
        if (files.length === 0 && sessions.length === 0 && briefs.length === 0) {
            container.innerHTML = `<p style="color:#64748b;font-size:0.85rem;">
                La corbeille est vide. Les éléments supprimés y restent ${data.retention_days || 30} jours avant suppression définitive.
            </p>`;
            return;
        }
        // TKT-211 : bannir le terme "purge" côté user-facing au profit de
        // "Sera supprimé automatiquement dans N jour(s)" (libellé explicite).
        const autoDeleteLabel = (daysLeft) => {
            if (daysLeft == null) return 'Sera supprimé automatiquement prochainement';
            const n = Number(daysLeft);
            if (!Number.isFinite(n) || n <= 0) return 'Sera supprimé automatiquement aujourd\'hui';
            return `Sera supprimé automatiquement dans ${n} ${n > 1 ? 'jours' : 'jour'}`;
        };
        const sessionsHtml = sessions.map(s => `
            <div class="trash-item">
                <span class="trash-item-type">Session</span>
                <span class="trash-item-name"><strong>${escapeHtml(s.simple_code)}</strong> · ${s.files_count} fichier(s)</span>
                <span class="trash-item-meta">${escapeHtml(autoDeleteLabel(s.days_left))}</span>
                <button class="btn-primary fr-btn fr-btn--sm fr-btn--secondary"
                        onclick="restoreSession('${s.simple_code}')">Restaurer</button>
            </div>
        `).join('');
        const filesHtml = files.map(f => `
            <div class="trash-item">
                <span class="trash-item-type">Fichier</span>
                <span class="trash-item-name">${escapeHtml(f.original_filename)} <small style="color:#94a3b8;">(${escapeHtml(f.simple_code || '?')})</small></span>
                <span class="trash-item-meta">${escapeHtml(autoDeleteLabel(f.days_left))}</span>
                <button class="btn-primary fr-btn fr-btn--sm fr-btn--secondary"
                        onclick="restoreFile('${f.id}')">Restaurer</button>
                <button class="btn-primary btn-danger-mini fr-btn fr-btn--sm fr-btn--tertiary-no-outline"
                        onclick="deleteFilePermanently('${f.id}', '${escapeHtml(f.original_filename).replace(/'/g, '&#39;')}')">
                    Supprimer définitivement
                </button>
            </div>
        `).join('');
        const briefsHtml = briefs.map(b => `
            <div class="trash-item" data-trash-kind="brief">
                <span class="trash-item-type">[Brief]</span>
                <span class="trash-item-name">${escapeHtml(b.title || '(sans titre)')}</span>
                <span class="trash-item-meta">${escapeHtml(autoDeleteLabel(b.days_left))}</span>
                <button class="btn-primary fr-btn fr-btn--sm fr-btn--secondary"
                        onclick="restoreBrief('${b.id}')">Restaurer</button>
                <button class="btn-primary btn-danger-mini fr-btn fr-btn--sm fr-btn--tertiary-no-outline"
                        onclick="deleteBriefPermanently('${b.id}', '${escapeHtml(b.title || '').replace(/'/g, '&#39;')}')">
                    Supprimer définitivement
                </button>
            </div>
        `).join('');
        container.innerHTML = sessionsHtml + filesHtml + briefsHtml;
    } catch (e) {
        container.innerHTML = `<p style="color:#b91c1c;font-size:0.85rem;">Erreur chargement corbeille.</p>`;
    }
}

async function restoreFile(fileId) {
    try {
        const r = await fetch(`/api/file/${fileId}/restore`, { method: 'POST' });
        const d = await r.json();
        if (!r.ok || !d.ok) throw new Error(d.error || 'restore_failed');
        showToast('Fichier restauré.', 'success');
        loadTrash();
        loadSessions({ force: true });
    } catch (e) { showToast('Restauration échouée.', 'error'); }
}

async function restoreSession(simpleCode) {
    try {
        const r = await fetch(`/api/my-sessions/${simpleCode}/restore`, { method: 'POST' });
        const d = await r.json();
        if (!r.ok || !d.ok) throw new Error(d.error || 'restore_failed');
        showToast('Session restaurée.', 'success');
        loadTrash();
        loadSessions({ force: true });
    } catch (e) { showToast('Restauration échouée.', 'error'); }
}

// ─── Brief de réunion — extrait vers tabs/preparations.js (PR-UX-Preparations) ─
// Toutes les fonctions loadBriefs/showBriefDetail/renderBriefBody/fillAmendForm/
// buildAmendBriefJson/saveAmendBrief/link/detach audio/série/banner > 90j/
// delete/restore/permanently/restoreAmendSectionsState vivent désormais dans
// frontend/tabs/preparations.js (module ES avec mount()/unmount() + délégation
// data-action). Les fonctions clés sont republiées sur window.* pour rester
// appelables depuis setupTabs/pickDefaultTab et depuis les boutons générés
// par la liste corbeille.

async function deleteFilePermanently(fileId, filenameRaw) {
    const filename = (filenameRaw || '').replace(/&#39;/g, "'");
    if (!confirm(`Supprimer définitivement « ${filename} » ?\n\nLe fichier sera retiré de S3 et de la base. Cette action est irréversible.`)) return;
    try {
        const r = await fetch(`/api/file/${fileId}/permanently`, { method: 'DELETE' });
        const d = await r.json();
        if (!r.ok || !d.ok) throw new Error(d.error || 'delete_failed');
        showToast('Fichier supprimé définitivement.', 'success');
        loadTrash();
    } catch (e) { showToast('Suppression définitive échouée.', 'error'); }
}
// Exposé pour tabs/devices.js (loadDevices y appelle window.pickDefaultTab
// au 1er chargement pour choisir entre "transfers" et "generate" selon
// la présence d'un device enrôlé).
window.pickDefaultTab = function(hasActiveDevice) { return pickDefaultTab(hasActiveDevice); };
function pickDefaultTab(hasActiveDevice) {
    if (_tabsInitialised) return;
    _tabsInitialised = true;
    let target = null;
    // Deep-link ?tab=<id> (utilisé par la redirection /meeting-prep -> /?tab=brief).
    try {
        const params = new URLSearchParams(window.location.search);
        const queryTab = params.get('tab');
        if (queryTab) { target = queryTab; }
    } catch (e) {}
    if (!target) {
        try { target = sessionStorage.getItem('mydevices-active-tab'); } catch (e) {}
    }
    if (!target) {
        // Sans device : on guide direct vers le formulaire d'enrôlement.
        // Avec device : vue principale = transferts/analyses.
        target = hasActiveDevice ? 'transfers' : 'generate';
    }
    activateTab(target);
    if (target === 'brief') {
        // Migré vers tabs/preparations.js — `window.loadBriefs` publié au boot du module.
        try { if (typeof window.loadBriefs === 'function') window.loadBriefs(); } catch (e) {}
    }
    if (target === 'trash') { try { loadTrash(); } catch (e) {} }
}

// ─── Publication globale des handlers legacy ──────────────────────────────
// legacy.js est chargé via shell.js en `<script type="module">` (Vite). Les
// fonctions déclarées ici vivent dans le scope du module et ne sont PAS
// accessibles depuis les attributs `onclick="..."` du template (qui sont
// évalués dans le scope global). Sans publication explicite, chaque clic
// sur un bouton « Mode avancé », un titre de liste, le chevron « détails »,
// le bouton corbeille, etc. déclenche `ReferenceError: <fn> is not defined`
// et le navigateur ignore silencieusement l'action — d'où les 4 bugs UX
// rapportés (titre non cliquable, mode avancé inopérant, chevron inerte,
// purge inaccessible). On republie ici, en bloc, toutes les fonctions
// référencées par un attribut inline dans index.html OU dans le HTML
// généré par innerHTML (file row, file detail, trash item).
// Note : `tabs/meetings.js` lit ces mêmes globals via `window.<fn>` au
// moment de son import — l'ordre dans shell.js fait que legacy.js est
// évalué AVANT meetings.js, donc ces affectations sont en place quand
// les ré-exports `export const ... = window.<fn>` sont résolus.
const _WINDOW_EXPORTS = {
    // Mode avancé (header)
    toggleAdvancedDl,
    // Liste / détail / chevron / corbeille (sessions + fichiers)
    showFileDetail,
    showFilesList,
    toggleRowExpand,
    deleteFile,
    deleteSession,
    deleteFilePermanently,
    restoreFile,
    restoreSession,
    purgeSessions,
    renewSession,
    renameDetailTitle,
    // Modale infos techniques + impact normalisation
    openFileInfoModal,
    loadNormalizationImpact,
    // Tri date + upload local
    toggleSortDir,
    handleLocalUploadInput,
    uploadLocalFiles,
    // Date de réunion éditable (vue détail)
    saveMeetingDatetime,
    resetMeetingDatetime,
    // Corbeille
    loadTrash,
    // Téléchargements (status + dropdowns "Autres")
    loadTranscriptStatus,
    updateOtherDownload,
    updateDownloadButtons,
    // Nav + cycle de vie (consommés par d'autres modules ES)
    activateTab,
    stopQueueHintDetail,
    loadDevices,
};
for (const [name, fn] of Object.entries(_WINDOW_EXPORTS)) {
    if (typeof fn === 'function' && typeof window[name] === 'undefined') {
        window[name] = fn;
    }
}

setupTabs();
updateDeviceFilterButton();
updateAdvancedToggleUi();
// Feedback visuel sur clic d'une icône de téléchargement : flash + scale.
// Délégation globale — fonctionne pour les boutons re-rendus par
// loadTranscriptStatus sans re-bind à chaque refresh.
document.addEventListener('click', (ev) => {
    const btn = ev.target.closest && ev.target.closest('.downloads-icon-btn');
    if (!btn) return;
    btn.classList.add('is-clicked');
    setTimeout(() => btn.classList.remove('is-clicked'), 450);
});
// Charge initial : devices puis sessions. Pas d'auto-refresh setInterval —
// le user peut Rafraîchir manuellement via le bouton dédié dans le header
// de l'onglet, ou la transcription qui poll elle-même (loadTranscriptStatus
// re-fire dans 15-30s tant qu'isInProgress).
loadDevices().then(() => loadSessions({ force: true })).catch(() => loadSessions({ force: true }));
