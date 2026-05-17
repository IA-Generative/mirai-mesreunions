// Code historique de mesreunions-web — extrait verbatim de l'inline JS
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
        'transcript':              { label: 'Transcription brute (Whisper)',                   desc: 'Texte issu de Whisper (faster-whisper, gateway Mirai). Étape obligatoire pour toutes les autres.' },
        'transcript-tagged':       { label: 'Identification des interlocuteurs',               desc: 'Diarisation pyannote — sépare le texte par interlocuteur. Peut échouer sur monolocuteur/audio très court.' },
        'transcript-corrected':    { label: 'Correction des sigles',                            desc: 'LLM relit avec votre glossaire métier pour corriger les acronymes mal transcrits (ex: EHS → EFS).' },
        'transcript-cleaned':      { label: 'Suppression des hésitations et redites',          desc: 'LLM retire les passages parasites du discours oral (faux départs, "euh", redites, bruits verbalisés).' },
        'transcript-reformulated': { label: 'Synthèse narrative',                               desc: 'LLM reformule au style indirect ("X explique que…") pour une lecture rapide.' },
        'meeting-cr':              { label: 'Compte-rendu structuré',                          desc: 'LLM produit l\'analyse 5 sections : acteurs, thématiques, décisions, gaps, recommandations.' },
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
    // Régénération LLM-only : les 4 étapes glossary/cleaned/reformulated/
    // meeting-cr sont rejouées. Whisper + diarisation NE sont PAS rejoués.
    // On marque les 4 étapes "à refaire" en bleu pendant le reprocess
    // pour signaler clairement que les anciens outputs sont obsolètes.
    const isReprocessing = (status === 'kevent_reprocessing');
    const REPROCESSED_KEYS = new Set([
        'transcript-corrected',
        'transcript-cleaned',
        'transcript-reformulated',
        'meeting-cr',
    ]);
    let runMarked = false;
    const stepsHtml = Object.keys(STEPS).map(k => {
        const ok = !!(cached.outputs || {})[k];
        let icon, color, suffix = '';
        if (isReprocessing && REPROCESSED_KEYS.has(k)) {
            icon = '⏳'; color = '#1d4ed8';
            suffix = ` <small style="color:#1d4ed8;font-weight:600;">— en cours de régénération</small>`;
        } else if (ok) {
            icon = '✓'; color = '#10b981';
        } else if (isFail) {
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
        // Dispatch un événement input pour neutraliser le dirty-state →
        // les boutons ✓ et ↺ se redésactivent via le listener global.
        input.dispatchEvent(new Event('input', { bubbles: true }));
        showToast('Titre renommé.', 'success');
        // Force un refresh des sessions pour propager le nouveau titre.
        loadSessions({ force: true });
    } catch (e) {
        btn.disabled = false;
        showToast(`Renommage échoué : ${e.message}`, 'error');
    }
}
// Active les boutons ✓ enregistrer + ↺ annuler de la ligne titre selon
// l'état "dirty" (= valeur courante ≠ valeur originale).
document.addEventListener('input', (ev) => {
    const t = ev.target;
    if (!t || !t.matches('.file-detail-title-input')) return;
    const original = t.dataset.originalTitle || '';
    const current = (t.value || '').trim();
    const dirty = !!current && current !== original;
    const row = t.closest('.file-detail-title-row');
    if (row) {
        const validateBtn = row.querySelector('.file-detail-rename-btn');
        if (validateBtn) validateBtn.disabled = !dirty;
        const revertBtn = row.querySelector('[data-detail-title-revert-for]');
        if (revertBtn) revertBtn.disabled = !dirty;
    }
});

// Active les boutons ✓ enregistrer + ↺ revert/effacer de la ligne date selon
// l'état "dirty". Si dirty, ↺ revient à la valeur saved. Sinon, le ↺ reste
// activé pour effacer l'override (mais seulement si une valeur saved
// existe — sinon nothing to do, disabled).
document.addEventListener('input', (ev) => {
    const t = ev.target;
    if (!t || !t.matches('.file-detail-meeting-input')) return;
    const fileId = t.getAttribute('data-meeting-dt-for') || '';
    const original = t.dataset.meetingDtOriginal || '';
    const current = (t.value || '').trim();
    const dirty = current !== original;
    const row = t.closest('.file-detail-meeting-row');
    if (!row) return;
    const validateBtn = row.querySelector(`[data-meeting-dt-save-for="${fileId}"]`);
    if (validateBtn) validateBtn.disabled = !dirty;
    const revertBtn = row.querySelector(`[data-meeting-dt-reset-for="${fileId}"]`);
    if (revertBtn) {
        // Bouton actif si dirty (= revert local possible) OU si une valeur
        // saved existe (= effacer l'override possible).
        revertBtn.disabled = !dirty && !original;
        revertBtn.title = dirty
            ? 'Annuler la modification non sauvegardée'
            : (original ? 'Effacer la date saisie (retombe sur la date d\'upload)' : 'Aucune modification à annuler');
    }
});

// Revert input du titre vers la valeur originale + désactive les 2 boutons.
function revertDetailTitle(fileId) {
    const input = document.querySelector(`[data-detail-title-for="${fileId}"]`);
    if (!input) return;
    input.value = input.dataset.originalTitle || '';
    input.dispatchEvent(new Event('input', { bubbles: true }));
}

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
    const saveBtn = document.querySelector(`[data-meeting-dt-save-for="${fileId}"]`);
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
        // Met à jour la valeur "original" sur l'input pour neutraliser le
        // dirty-state (les boutons ✓/↺ se désactivent).
        if (inputEl) {
            inputEl.dataset.meetingDtOriginal = inputEl.value || '';
        }
        if (saveBtn) saveBtn.disabled = true;
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

// ↺ Action contextuelle :
// - si la valeur courante diffère de la valeur saved (dirty), revert local
//   sans appel serveur (annule la modif non-confirmée) ;
// - sinon, efface l'override côté serveur (PATCH meeting_datetime=null).
async function resetMeetingDatetime(fileId) {
    const input = document.querySelector(`[data-meeting-dt-for="${fileId}"]`);
    if (!input) return;
    const original = input.dataset.meetingDtOriginal || '';
    const current = (input.value || '').trim();
    if (current !== original) {
        // Revert local — pas d'appel serveur, juste restore.
        input.value = original;
        input.dispatchEvent(new Event('input', { bubbles: true }));
        return;
    }
    // Pas dirty mais l'utilisateur clique ↺ → on efface l'override serveur.
    input.value = '';
    input.dataset.meetingDtOriginal = '';
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
// le CR + audio interne + Transcription nettoyée + Synthèse narrative, le
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
    // Coche de validation pour les boutons "✓ enregistrer" (vert quand actif).
    check:      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5l5 5L19 7"/></svg>',
    // Flèche reverse pour les boutons "↺ annuler / restaurer".
    revert:     '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 12a8 8 0 1 1 2 5.3"/><path d="M4 18v-5h5"/></svg>',
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
const _TRANSCRIPT_REPROCESSING = new Set(['kevent_reprocessing']);
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
    if (_TRANSCRIPT_REPROCESSING.has(status)) {
        // kind=processing pour réutiliser la CSS de pulse existante
        // (filerowStatusTagPulse) → l'icône clignote dans la liste.
        return { label: 'Régénération en cours', icon: 'fr-icon-refresh-line', kind: 'processing' };
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
    // Rendu icône-only (taille fixe ~24 px) : le libellé reste accessible via
    // aria-label + title pour SR et utilisateurs voyants, mais le tag n'occupe
    // plus la largeur du texte. Évite les chevauchements sur la liste compacte
    // quand des libellés longs ("Partiellement prête") cohabitaient avec le
    // titre tronqué et la source.
    return `<span class="${info.icon} file-row-status-tag file-row-status-tag--${info.kind}"
                  data-file-status-tag="${escapeHtml(fileId)}"
                  data-status-kind="${info.kind}"
                  role="status"
                  aria-label="${escapeHtml(aria)}"
                  title="${escapeHtml(info.label)} — ${escapeHtml(tech)}"></span>`;
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

// ─── Feedback widget (Phase F1 — pouce ↑/↓ + régénération) ────────
//
// Inséré dans la fiche détail via <div class="file-detail-feedback-block"
// data-feedback-for="<fileId>"></div>. Un MutationObserver détecte
// chaque insertion et appelle mountFeedbackBlock(), idempotent grâce
// au flag data-feedback-mounted="1".
//
// 2 sections :
//   1) Pouce ↑/↓ "Cette retranscription vous est-elle utile ?"
//      → checklist raisons + free-text → POST /api/file/<id>/feedback
//      → message "Merci !"
//   2) Régénération
//      → bouton "Régénérer les comptes-rendus" (LLM-only, instantané)
//      → bouton "Régénérer transcription + diarisation" (full, queue admin)
//      → chaque bouton ouvre une modale "Pourquoi ?" (raison obligatoire)
//      → POST /api/file/<id>/regenerate

const USEFULNESS_REASONS_DOWN = [
    { id: 'transcription_imprecise',  label: 'Transcription imprécise (mots ou phrases mal compris)' },
    { id: 'speakers_wrong',           label: 'Locuteurs mal identifiés' },
    { id: 'sigles_wrong',             label: 'Sigles / acronymes non corrigés' },
    { id: 'summary_off',              label: 'Résumé / CR à côté du sujet' },
    { id: 'missing_content',          label: 'Du contenu important est manquant' },
    { id: 'too_long',                 label: 'Trop verbeux / illisible' },
    { id: 'other_down',               label: 'Autre' },
];
const USEFULNESS_REASONS_UP = [
    { id: 'transcription_good',       label: 'Transcription fidèle' },
    { id: 'summary_useful',           label: 'Résumé / CR pertinent' },
    { id: 'gained_time',              label: 'M\'a fait gagner du temps' },
    { id: 'shareable',                label: 'Partageable en l\'état' },
    { id: 'other_up',                 label: 'Autre' },
];

function _escapeAttr(s) { return escapeHtml(s); }

function mountFeedbackBlock(container) {
    if (!container || container.dataset.feedbackMounted === '1') return;
    const fileId = container.getAttribute('data-feedback-for') || '';
    if (!fileId) return;
    container.dataset.feedbackMounted = '1';
    // Restaure l'état "corrections en attente" persisté côté localStorage
    // (le badge + le clignotement du bouton 🔄 sont rendus après).
    const pendingCount = getPendingCorrectionsCount(fileId);
    // Le badge est inséré APRÈS innerHTML ci-dessous (sinon écrasé).
    // Ordre : régénération en haut, pouce ↑/↓ en bas de la fiche (le pouce
    // = action de clôture/post-lecture, doit venir après la consultation).
    container.innerHTML = `
      <div class="feedback-section feedback-section--regen">
        <div class="feedback-row">
          <span class="feedback-q">Régénérer&nbsp;:</span>
          <button type="button" class="feedback-regen-btn feedback-regen-btn--llm"
                  data-feedback-regen="llm-only" data-feedback-file="${_escapeAttr(fileId)}"
                  title="Relance les étapes LLM (glossaire → compte-rendu) avec le glossaire actuel">
            🔄 Comptes-rendus (LLM)
          </button>
          <button type="button" class="feedback-regen-btn feedback-regen-btn--full"
                  data-feedback-regen="full" data-feedback-file="${_escapeAttr(fileId)}"
                  title="Relance TOUT le pipeline depuis l'audio (Whisper + diarisation + LLM). Très coûteux en calcul — à demander uniquement si vraiment nécessaire.">
            🔁 Transcription + diarisation
          </button>
        </div>
      </div>
      <div class="feedback-section feedback-section--useful" data-feedback-useful-for="${_escapeAttr(fileId)}">
        <div class="feedback-row">
          <span class="feedback-q">Cette retranscription vous est-elle utile ?</span>
          <button type="button" class="feedback-thumb feedback-thumb-up"
                  data-feedback-thumb="up" data-feedback-file="${_escapeAttr(fileId)}"
                  title="Oui, utile">👍</button>
          <button type="button" class="feedback-thumb feedback-thumb-down"
                  data-feedback-thumb="down" data-feedback-file="${_escapeAttr(fileId)}"
                  title="Non, à améliorer">👎</button>
        </div>
        <div class="feedback-detail" data-feedback-detail-for="${_escapeAttr(fileId)}" hidden></div>
        <div class="feedback-thanks" data-feedback-thanks-for="${_escapeAttr(fileId)}" hidden>
          <span class="feedback-thanks-icon">✓</span>
          Merci pour votre feedback — il nourrit l'amélioration du service.
        </div>
      </div>
    `;
    // Restaure le badge "modifications en attente" si présent en localStorage.
    if (pendingCount > 0) {
        _applyPendingCorrectionsBadge(fileId, pendingCount);
    }
}

// Délégation click sur tous les éléments du widget feedback.
document.addEventListener('click', (ev) => {
    const t = ev.target;
    if (!t || !t.closest) return;

    // Pouce ↑↓ → expand checklist.
    const thumb = t.closest('[data-feedback-thumb]');
    if (thumb) {
        ev.preventDefault();
        const fileId = thumb.getAttribute('data-feedback-file') || '';
        const thumbVal = thumb.getAttribute('data-feedback-thumb') || '';
        _openFeedbackDetail(fileId, thumbVal);
        return;
    }
    // Régénérer → modale raison.
    const regen = t.closest('[data-feedback-regen]');
    if (regen) {
        ev.preventDefault();
        const fileId = regen.getAttribute('data-feedback-file') || '';
        const scope = regen.getAttribute('data-feedback-regen') || '';
        _openRegenerateModal(fileId, scope);
        return;
    }
    // Bouton "Envoyer" du formulaire de feedback détail.
    const send = t.closest('[data-feedback-send]');
    if (send) {
        ev.preventDefault();
        const fileId = send.getAttribute('data-feedback-send') || '';
        _submitUsefulnessFeedback(fileId);
        return;
    }
});

function _openFeedbackDetail(fileId, thumb) {
    const detailEl = document.querySelector(`[data-feedback-detail-for="${fileId}"]`);
    if (!detailEl) return;
    const reasons = (thumb === 'up') ? USEFULNESS_REASONS_UP : USEFULNESS_REASONS_DOWN;
    const reasonsHtml = reasons.map(r =>
        `<label class="feedback-reason">
           <input type="checkbox" name="feedback-reason" value="${_escapeAttr(r.id)}" />
           <span>${escapeHtml(r.label)}</span>
         </label>`
    ).join('');
    detailEl.innerHTML = `
      <input type="hidden" data-feedback-thumb-value="${_escapeAttr(thumb)}" />
      <div class="feedback-detail-reasons">${reasonsHtml}</div>
      <textarea class="feedback-detail-text"
                placeholder="Optionnel — détaillez votre retour, qu'on s'améliore."
                maxlength="2000"></textarea>
      <div class="feedback-detail-actions">
        <button type="button" class="feedback-send-btn"
                data-feedback-send="${_escapeAttr(fileId)}">Envoyer le feedback</button>
        <button type="button" class="feedback-cancel-btn"
                onclick="document.querySelector('[data-feedback-detail-for=\\'${_escapeAttr(fileId)}\\']').hidden=true; document.querySelector('[data-feedback-detail-for=\\'${_escapeAttr(fileId)}\\']').innerHTML='';">
          Annuler
        </button>
      </div>
    `;
    detailEl.hidden = false;
}

// État "modifs en attente de reprocess LLM" — survit au refresh via
// localStorage. Permet d'inviter l'utilisateur à régénérer le CR
// (clignotement bouton "🔄 Comptes-rendus (LLM)") sans relancer le
// LLM à chaque correction (coûteux).
function _pendingCorrectionsKey(fileId) {
    return `mcr_pending_corrections_${fileId}`;
}
function getPendingCorrectionsCount(fileId) {
    try { return parseInt(localStorage.getItem(_pendingCorrectionsKey(fileId)) || '0', 10) || 0; }
    catch (e) { return 0; }
}
function incrementPendingCorrections(fileId) {
    try {
        const n = getPendingCorrectionsCount(fileId) + 1;
        localStorage.setItem(_pendingCorrectionsKey(fileId), String(n));
        _applyPendingCorrectionsBadge(fileId, n);
        return n;
    } catch (e) { return 0; }
}
function clearPendingCorrections(fileId) {
    try {
        localStorage.removeItem(_pendingCorrectionsKey(fileId));
        _applyPendingCorrectionsBadge(fileId, 0);
    } catch (e) { /* ignore */ }
}
// Applique/retire la classe `has-pending-corrections` sur le bloc
// feedback du fichier concerné (cible le bouton "🔄 Comptes-rendus
// (LLM)" via CSS pour le clignotement amber). Ajoute aussi un badge
// compteur "N modifs non répercutées" sur le bloc.
function _applyPendingCorrectionsBadge(fileId, count) {
    const block = document.querySelector(`[data-feedback-for="${fileId}"]`);
    if (!block) return;
    block.classList.toggle('has-pending-corrections', count > 0);
    let badge = block.querySelector('[data-pending-badge]');
    if (count > 0) {
        if (!badge) {
            badge = document.createElement('div');
            badge.setAttribute('data-pending-badge', '');
            badge.className = 'pending-corrections-badge';
            block.insertBefore(badge, block.firstChild);
        }
        badge.innerHTML = `⚠️ <strong>${count}</strong> modification${count > 1 ? 's' : ''} de transcription en attente — cliquez « 🔄 Comptes-rendus (LLM) » ci-dessous pour les répercuter dans le résumé/CR.`;
    } else if (badge) {
        badge.remove();
    }
}

async function _submitUsefulnessFeedback(fileId) {
    const detailEl = document.querySelector(`[data-feedback-detail-for="${fileId}"]`);
    if (!detailEl) return;
    const thumbInput = detailEl.querySelector('[data-feedback-thumb-value]');
    const thumb = thumbInput ? thumbInput.getAttribute('data-feedback-thumb-value') : '';
    const reasons = Array.from(detailEl.querySelectorAll('input[name="feedback-reason"]:checked')).map(c => c.value);
    const free = (detailEl.querySelector('.feedback-detail-text')?.value || '').trim().slice(0, 2000);
    const sendBtn = detailEl.querySelector('[data-feedback-send]');
    if (sendBtn) sendBtn.disabled = true;
    try {
        const resp = await fetch(`/api/file/${encodeURIComponent(fileId)}/feedback`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                type: 'usefulness',
                payload: { thumb, reasons, free_text: free },
            }),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        // Cache le form, affiche le merci.
        detailEl.hidden = true; detailEl.innerHTML = '';
        const thanks = document.querySelector(`[data-feedback-thanks-for="${fileId}"]`);
        if (thanks) thanks.hidden = false;
    } catch (e) {
        if (sendBtn) sendBtn.disabled = false;
        alert(`Envoi du feedback échoué : ${e.message}`);
    }
}

// Modale custom pour la raison de régénération (remplace window.prompt).
// Permet une liste de raisons préremplies cliquables sous le textarea.
// Retourne une Promise<string|null> : string = raison, null = annulé.
function _promptRegenReason(scopeLabel, presets, opts) {
    return new Promise((resolve) => {
        opts = opts || {};
        const initialReason = opts.initialReason || '';
        const initialChip   = opts.initialChip || '';
        const id = 'regen-reason-modal';
        document.querySelectorAll(`#${id}`).forEach((el) => el.remove());
        const wrap = document.createElement('div');
        wrap.id = id;
        wrap.setAttribute('role', 'dialog');
        wrap.setAttribute('aria-modal', 'true');
        wrap.style.cssText = 'position:fixed;inset:0;background:rgba(15,23,42,0.55);' +
            'display:flex;align-items:center;justify-content:center;z-index:10000;';
        const presetsHtml = presets.map((p) => `
          <button type="button" class="regen-preset-chip"
                  data-regen-preset="${escapeHtml(p)}"
                  style="font-size:0.78rem;padding:0.25rem 0.6rem;
                         border:1px solid #cbd5e1;border-radius:999px;
                         background:#fff;cursor:pointer;color:#1e293b;
                         ${p === initialChip ? 'background:#dbeafe;border-color:#1d4ed8;color:#0c4498;font-weight:600;' : ''}">
            ${escapeHtml(p)}
          </button>
        `).join(' ');
        wrap.innerHTML = `
          <div style="background:#fff;border-radius:0.5rem;max-width:560px;width:92%;
                      padding:1.1rem 1.3rem;box-shadow:0 10px 40px rgba(0,0,0,0.25);">
            <h2 style="margin:0 0 0.4rem;font-size:1.05rem;color:#0c4498;">Régénérer ${escapeHtml(scopeLabel)}</h2>
            <p style="margin:0 0 0.6rem;font-size:0.82rem;color:#64748b;">
              Indiquez la raison (pour traçabilité et amélioration). Vous pouvez taper
              librement ou cliquer une suggestion ci-dessous.
            </p>
            <textarea class="regen-reason-input" rows="3" maxlength="500"
                      style="width:100%;padding:0.5rem 0.6rem;font-size:0.88rem;
                             border:1px solid #cbd5e1;border-radius:4px;
                             resize:vertical;font-family:inherit;"
                      placeholder="Pourquoi régénérer ?">${escapeHtml(initialReason)}</textarea>
            ${opts.contextHint ? `<div style="font-size:0.74rem;color:#0c4498;background:#eff6ff;padding:0.35rem 0.55rem;border-radius:3px;margin-top:0.4rem;">💡 ${escapeHtml(opts.contextHint)}</div>` : ''}
            <div style="display:flex;flex-wrap:wrap;gap:0.3rem;margin-top:0.6rem;">
              ${presetsHtml}
            </div>
            <div style="display:flex;justify-content:flex-end;gap:0.5rem;margin-top:0.9rem;">
              <button type="button" class="regen-cancel"
                      style="padding:0.4rem 0.9rem;border:1px solid #cbd5e1;background:#fff;
                             border-radius:3px;cursor:pointer;">Annuler</button>
              <button type="button" class="regen-confirm"
                      style="padding:0.4rem 0.9rem;border:1px solid #1d4ed8;background:#1d4ed8;
                             color:#fff;font-weight:600;border-radius:3px;cursor:pointer;">Régénérer</button>
            </div>
          </div>
        `;
        document.body.appendChild(wrap);
        const ta = wrap.querySelector('.regen-reason-input');
        const close = (val) => { wrap.remove(); resolve(val); };
        if (ta) { ta.focus(); ta.selectionStart = ta.selectionEnd = ta.value.length; }
        // Click sur préset : remplace le contenu du textarea (sauf si déjà
        // identique = toggle vers vide). Les chips ne s'accumulent pas pour
        // garder une raison concise et claire.
        wrap.querySelectorAll('[data-regen-preset]').forEach((btn) => {
            btn.addEventListener('click', (ev) => {
                ev.preventDefault();
                const v = btn.getAttribute('data-regen-preset') || '';
                if ((ta.value || '').trim() === v) {
                    ta.value = '';
                } else {
                    ta.value = v;
                }
                ta.focus();
                // Re-style l'état actif visuel des chips.
                wrap.querySelectorAll('[data-regen-preset]').forEach((b) => {
                    const active = (b.getAttribute('data-regen-preset') || '') === (ta.value || '').trim();
                    b.style.background = active ? '#dbeafe' : '#fff';
                    b.style.borderColor = active ? '#1d4ed8' : '#cbd5e1';
                    b.style.color = active ? '#0c4498' : '#1e293b';
                    b.style.fontWeight = active ? '600' : 'normal';
                });
            });
        });
        wrap.querySelector('.regen-cancel').addEventListener('click', () => close(null));
        wrap.querySelector('.regen-confirm').addEventListener('click', () => close(ta.value || ''));
        // Escape annule, Ctrl/Cmd+Enter confirme.
        wrap.addEventListener('keydown', (ev) => {
            if (ev.key === 'Escape') { ev.preventDefault(); close(null); }
            else if ((ev.metaKey || ev.ctrlKey) && ev.key === 'Enter') {
                ev.preventDefault(); close(ta.value || '');
            }
        });
        // Click backdrop ferme aussi.
        wrap.addEventListener('click', (ev) => { if (ev.target === wrap) close(null); });
    });
}

async function _openRegenerateModal(fileId, scope) {
    // Garde-fou explicite pour la régen full (re-Whisper + re-diarisation) :
    // c'est lourd en compute (GPU L4 + LLM downstream), ne doit être lancé
    // que si l'utilisateur en a vraiment besoin. Demande confirmation
    // explicite avant d'ouvrir la modale de raison.
    if (scope === 'full') {
        const ok = window.confirm(
            "⚠️  Régénération complète : transcription + diarisation\n\n" +
            "Cette opération relance TOUT le pipeline depuis l'audio brut :\n" +
            "• Whisper (transcription, GPU)\n" +
            "• pyannote (diarisation, GPU)\n" +
            "• Toute la chaîne LLM en aval (CR, glossaire, etc.)\n\n" +
            "C'est très coûteux en ressources de calcul.\n" +
            "Ne lancez cette régénération QUE SI VRAIMENT NÉCESSAIRE\n" +
            "(ex: transcription totalement à côté, diarisation cassée).\n\n" +
            "Pour une simple maj du compte-rendu suite à un glossaire modifié,\n" +
            "utilisez plutôt le bouton « 🔄 Comptes-rendus (LLM) ».\n\n" +
            "Confirmer la régénération complète ?"
        );
        if (!ok) return;
    }

    const scopeLabel = scope === 'full'
        ? 'transcription + diarisation (refonte complète du pipeline)'
        : 'comptes-rendus (étapes LLM seulement, instantané)';
    // Raisons préremplies cliquables : remplissent le textarea (un clic).
    // Differencie par scope : les raisons LLM-only concernent le CR/glossaire/
    // mise en forme aval ; les raisons full concernent la qualité audio /
    // diarisation / Whisper.
    const presets = scope === 'full' ? [
        'Transcription totalement à côté',
        'Diarisation cassée (locuteurs mal séparés)',
        'Audio multi-langues mal détecté',
        'Speakers mal identifiés',
        'Trop de mots manquants',
    ] : [
        'Termes corrigés à répercuter dans le CR',
        'Glossaire mis à jour',
        'CR confus ou hors-sujet',
        'Manque de détails sur une décision',
        'Reformulation à améliorer',
        'Locuteurs renommés',
    ];
    // Pré-remplissage automatique : si l'utilisateur a corrigé des termes
    // sur ce fichier (pending corrections > 0), c'est très probablement
    // pour ça qu'il régénère. On pré-sélectionne le preset correspondant
    // et on affiche un hint contextuel "N corrections en attente".
    let initialReason = '', initialChip = '', contextHint = '';
    if (scope === 'llm-only') {
        const pending = (typeof getPendingCorrectionsCount === 'function')
            ? getPendingCorrectionsCount(fileId) : 0;
        if (pending > 0) {
            initialChip = 'Termes corrigés à répercuter dans le CR';
            initialReason = initialChip;
            contextHint = `${pending} correction${pending > 1 ? 's' : ''} de transcription en attente sur ce fichier.`;
        }
    }
    const reason = await _promptRegenReason(scopeLabel, presets, {
        initialReason, initialChip, contextHint,
    });
    if (reason === null) return;  // user clicked Cancel
    const trimmed = (reason || '').trim();
    if (!trimmed) {
        alert('La raison est obligatoire — annulé.');
        return;
    }
    // Désactive le bouton + change son label pendant la requête (la
    // chaîne LLM tourne synchrone et peut prendre 30s-5min).
    const btn = document.querySelector(
        `[data-feedback-regen="${scope}"][data-feedback-file="${fileId}"]`
    );
    const originalLabel = btn ? btn.innerHTML : '';
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '🔄 Régénération en cours…';
        btn.style.opacity = '0.7';
        btn.style.cursor = 'wait';
    }
    // Active immédiatement le pulse sur le bouton (i) "Détails techniques"
    // de la fiche pour signaler le traitement en cours, sans attendre le
    // prochain poll de transcript-status (qui mettrait jusqu'à 15s).
    // Le pulse sera re-confirmé puis retiré naturellement par
    // loadTranscriptStatus selon le polling status réel.
    const infoBtnEl = document.querySelector(`[data-file-info-btn="${CSS.escape(fileId)}"]`);
    if (infoBtnEl) {
        infoBtnEl.classList.add('file-detail-info-btn--pulse');
        infoBtnEl.setAttribute('title', `Régénération en cours — cliquer pour voir les détails`);
    }
    // Force aussi un refresh transcript-status immédiat puis re-poll
    // serré (3s) pendant la phase active pour mettre à jour le rail.
    const persistentCt = document.querySelector(
        `.transcript-section[data-transcript-file-id="${CSS.escape(fileId)}"]`
    );
    if (persistentCt && typeof loadTranscriptStatus === 'function') {
        setTimeout(() => loadTranscriptStatus(fileId, persistentCt), 1500);
    }
    try {
        const resp = await fetch(`/api/file/${encodeURIComponent(fileId)}/regenerate`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ scope, reason: trimmed }),
        });
        const data = await resp.json().catch(() => ({}));
        if (resp.status === 202) {
            alert(data.message || 'Demande enregistrée — traitement admin en attente.');
        } else if (resp.ok) {
            // Pas d'alert intrusif : la fiche se rafraîchit toute seule.
            clearPendingCorrections(fileId);
            if (typeof loadSessions === 'function') loadSessions({ force: true });
            showToast && showToast('✓ Régénération terminée', 'success');
        } else {
            alert(`Échec de la régénération : HTTP ${resp.status} — ${data.error || ''}`);
        }
    } catch (e) {
        alert(`Échec de la régénération : ${e.message}`);
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = originalLabel;
            btn.style.opacity = '';
            btn.style.cursor = '';
        }
        // Re-fetch le statut pour soit retirer le pulse (si terminé) soit
        // le garder (si polling continue côté serveur).
        if (persistentCt && typeof loadTranscriptStatus === 'function') {
            loadTranscriptStatus(fileId, persistentCt);
        }
    }
}

// MutationObserver : mount tout nouveau bloc feedback inséré dans le DOM.
(function _setupFeedbackObserver() {
    if (typeof MutationObserver === 'undefined') return;
    const obs = new MutationObserver((mutations) => {
        for (const m of mutations) {
            for (const node of m.addedNodes) {
                if (node.nodeType !== 1) continue;
                if (node.matches && node.matches('.file-detail-feedback-block')) {
                    mountFeedbackBlock(node);
                }
                if (node.matches && node.matches('.file-detail-corrector-block')) {
                    mountTranscriptCorrector(node);
                }
                if (node.querySelectorAll) {
                    node.querySelectorAll('.file-detail-feedback-block').forEach(mountFeedbackBlock);
                    node.querySelectorAll('.file-detail-corrector-block').forEach(mountTranscriptCorrector);
                }
            }
        }
    });
    obs.observe(document.body, { childList: true, subtree: true });
})();

// ─── Transcript corrector (Phase B + B3) ──────────────────────────
//
// Parse speaker_tagged_text en blocs {speaker, start, end, text}, affiche
// chaque bloc avec un bouton ▶ qui joue l'audio à ce timecode. L'user
// sélectionne du texte → popup "Corriger" avec input + 3 cases + bouton
// 🔊 (rejoue le bloc). POST /api/file/<id>/correct-term.

function _fmtTimecode(sec) {
    sec = Math.max(0, Math.round(sec));
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return `${m}:${String(s).padStart(2, '0')}`;
}

function _parseSpeakerTagged(text) {
    if (!text || typeof text !== 'string') return [];
    const lines = text.split('\n');
    const blocks = [];
    let current = null;
    const headRe = /^\*\*([^*]+)\*\*\s*_\((\d+):(\d+(?:\.\d+)?)\s*→\s*(\d+):(\d+(?:\.\d+)?)\)_/;
    for (const raw of lines) {
        const line = raw.trimEnd();
        const m = line.match(headRe);
        if (m) {
            if (current) blocks.push(current);
            current = {
                speaker: m[1].trim(),
                start: parseInt(m[2], 10) * 60 + parseFloat(m[3]),
                end:   parseInt(m[4], 10) * 60 + parseFloat(m[5]),
                text: '',
            };
        } else if (current && line.startsWith('>')) {
            const t = line.replace(/^>\s?/, '').trim();
            current.text = current.text ? current.text + ' ' + t : t;
        }
        // Lignes vides : on les ignore, on garde le bloc courant ouvert.
    }
    if (current) blocks.push(current);
    return blocks;
}

async function mountTranscriptCorrector(container) {
    if (!container || container.dataset.correctorMounted === '1') return;
    const fileId = container.getAttribute('data-corrector-for') || '';
    if (!fileId) return;
    container.dataset.correctorMounted = '1';
    const audioUrl = container.getAttribute('data-audio-url') || '';
    const audioPurged = container.getAttribute('data-audio-purged') === '1';
    container.innerHTML = `<p style="color:#94a3b8;font-size:0.85rem;">Chargement de la transcription…</p>`;

    let data;
    try {
        const resp = await fetch(`/api/file/transcript-status/${encodeURIComponent(fileId)}`);
        if (!resp.ok) {
            container.innerHTML = `<p style="color:#94a3b8;font-size:0.85rem;">Transcription indisponible (HTTP ${resp.status}).</p>`;
            return;
        }
        data = await resp.json();
    } catch (e) {
        container.innerHTML = `<p style="color:#b91c1c;font-size:0.85rem;">Erreur chargement : ${escapeHtml(e.message)}</p>`;
        return;
    }
    if (!data || !data.available) {
        container.innerHTML = '';   // pas encore prêt — on attend que le pipeline finisse
        return;
    }
    const blocks = _parseSpeakerTagged(data.speaker_tagged_text || '');
    if (!blocks.length) {
        // Fallback : pas de speaker-tagged (échec diarisation) — afficher juste
        // la transcription brute non-éditable.
        container.innerHTML = `
          <details class="transcript-corrector-fallback">
            <summary>📜 Transcription complète (sans blocs interlocuteur)</summary>
            <pre class="transcript-corrector-raw">${escapeHtml(data.transcription_text || data.speaker_tagged_text || '(vide)')}</pre>
          </details>
        `;
        return;
    }
    // Player audio sticky en haut : visible dès qu'on déplie le <details>.
    // Source : transferred (interne, persistant) ou rien si purgé.
    const playerHtml = audioPurged || !audioUrl
        ? `<div class="tc-audio-purged" title="L'audio a été purgé du stockage interne (rétention dépassée).">
             ⚠️ Audio purgé — ré-écoute indisponible. Les corrections par texte restent possibles.
           </div>`
        : `<audio class="transcript-corrector-audio" controls preload="metadata"
                  src="${escapeHtml(audioUrl)}"></audio>`;
    // Notice d'utilisation (visible quand le details est ouvert).
    const noticeHtml = `
      <div class="tc-notice">
        <div class="tc-notice-text">
          <strong>Mode d'emploi.</strong>
          Cliquez <span class="tc-notice-play">▶</span> pour écouter un passage.
          <strong>Sélectionnez un mot</strong> mal transcrit pour le corriger
          (avec ré-écoute du contexte 🔊).
          Cliquez sur <span class="tc-notice-pencil">✏️</span> à côté d'un
          interlocuteur (« SPEAKER_03 », etc.) pour le renommer.
          ${audioPurged ? '' : 'Cliquez sur une ligne pour positionner le lecteur audio.'}
        </div>
        <div class="tc-find" data-tc-find>
          <input type="search" class="tc-find-input" placeholder="Chercher…"
                 autocomplete="off" spellcheck="false" />
          <button type="button" class="tc-find-prev" title="Précédent (Maj+Entrée)">▲</button>
          <button type="button" class="tc-find-next" title="Suivant (Entrée)">▼</button>
          <span class="tc-find-status" data-tc-find-status></span>
        </div>
      </div>
    `;
    container.innerHTML = `
      <details class="transcript-corrector">
        <summary class="transcript-corrector-summary">
          📜 Transcription de la réunion
          <span style="font-weight:400;font-size:0.78rem;color:#64748b;">
            (${blocks.length} bloc${blocks.length > 1 ? 's' : ''})
          </span>
        </summary>
        <div class="transcript-corrector-sticky">
          ${playerHtml}
        </div>
        ${noticeHtml}
        <div class="transcript-corrector-blocks">
          ${blocks.map((b, i) => `
            <div class="tc-block" data-tc-idx="${i}" data-tc-start="${b.start}" data-tc-end="${b.end}">
              <button type="button" class="tc-play" data-tc-play="${b.start}"
                      title="${audioPurged ? 'Audio purgé' : 'Écouter ce passage (' + _fmtTimecode(b.start) + ')'}"
                      ${audioPurged ? 'disabled' : ''}>▶</button>
              <span class="tc-speaker" data-tc-speaker-idx="${i}">${escapeHtml(b.speaker)}</span>
              <button type="button" class="tc-speaker-rename"
                      data-tc-speaker-rename="${escapeHtml(b.speaker)}"
                      data-tc-file="${escapeHtml(fileId)}"
                      title="Renommer cet interlocuteur partout">✏️</button>
              <span class="tc-time">${_fmtTimecode(b.start)} → ${_fmtTimecode(b.end)}</span>
              <span class="tc-text" data-tc-text="${i}">${escapeHtml(b.text)}</span>
            </div>
          `).join('')}
        </div>
        <div class="transcript-corrector-footer"
             data-tc-corrector-footer="${escapeHtml(fileId)}"
             hidden></div>
      </details>
    `;

    const audio = container.querySelector('.transcript-corrector-audio');
    const blocksEls = Array.from(container.querySelectorAll('.tc-block'));

    // Click ▶ → seek + play. Click sur texte d'un bloc → seek (sans play
    // forcé pour pas démarrer si l'user voulait juste sélectionner).
    container.addEventListener('click', (ev) => {
        const playBtn = ev.target.closest && ev.target.closest('[data-tc-play]');
        if (playBtn && !playBtn.disabled) {
            ev.preventDefault();
            const t = parseFloat(playBtn.getAttribute('data-tc-play')) || 0;
            if (audio) {
                // pause() avant play() : sinon, si l'audio jouait déjà,
                // chrome/firefox cumulent (rare mais reproduit par user)
                // et on entend la bande son 2× désynchronisée.
                try { audio.pause(); audio.currentTime = Math.max(0, t); audio.play(); } catch (e) { /* ignore */ }
            }
            return;
        }
        // Click sur le texte d'un bloc (mais pas pendant une sélection !) :
        // seek audio sans play. On détecte "click sans sélection" via
        // window.getSelection().isCollapsed après un petit délai.
        const textEl = ev.target.closest && ev.target.closest('.tc-text');
        if (textEl && audio) {
            setTimeout(() => {
                const sel = window.getSelection();
                if (!sel || sel.isCollapsed) {
                    const blockEl = textEl.closest('.tc-block');
                    const start = blockEl ? parseFloat(blockEl.getAttribute('data-tc-start')) : 0;
                    try { audio.currentTime = Math.max(0, start); } catch (e) { /* ignore */ }
                }
            }, 50);
        }
    });

    // Sync audio → text : pendant la lecture, highlight le bloc courant
    // + scroll dans le viewport du container blocks si hors-vue.
    if (audio) {
        audio.addEventListener('timeupdate', () => {
            const t = audio.currentTime || 0;
            let activeIdx = -1;
            for (let i = 0; i < blocks.length; i++) {
                if (blocks[i].start <= t && t < blocks[i].end) {
                    activeIdx = i;
                    break;
                }
            }
            // Pas trouvé dans une plage exacte (silence entre 2 blocs) →
            // garde le précédent en cours pour pas perdre le highlight.
            if (activeIdx < 0) {
                for (let i = blocks.length - 1; i >= 0; i--) {
                    if (blocks[i].start <= t) { activeIdx = i; break; }
                }
            }
            blocksEls.forEach((el, i) => {
                el.classList.toggle('is-playing', i === activeIdx);
            });
            // Auto-scroll dans le container blocks si l'élément actif sort
            // du viewport visible.
            if (activeIdx >= 0) {
                const el = blocksEls[activeIdx];
                const containerEl = el.closest('.transcript-corrector-blocks');
                if (containerEl && el) {
                    const cRect = containerEl.getBoundingClientRect();
                    const eRect = el.getBoundingClientRect();
                    if (eRect.top < cRect.top || eRect.bottom > cRect.bottom) {
                        el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
                    }
                }
            }
        });
    }

    // Sélection texte → afficher le footer correction.
    container.addEventListener('mouseup', () => _onTranscriptSelection(container, fileId, audio, blocks));
    container.addEventListener('touchend', () => _onTranscriptSelection(container, fileId, audio, blocks));

    // Renommage interlocuteur (déléguée au container pour éviter de
    // rebrancher sur chaque ✏️).
    container.addEventListener('click', async (ev) => {
        const renameBtn = ev.target.closest && ev.target.closest('[data-tc-speaker-rename]');
        if (!renameBtn) return;
        ev.preventDefault();
        const oldName = renameBtn.getAttribute('data-tc-speaker-rename') || '';
        const newName = window.prompt(
            `Renommer l'interlocuteur « ${oldName} » :\n\n` +
            "Cela remplacera ce nom dans toutes les transcriptions de cette réunion " +
            "(brute, par-interlocuteur, glossaire, nettoyée, reformulation).",
            oldName
        );
        if (newName === null) return;
        const trimmed = (newName || '').trim();
        if (!trimmed || trimmed === oldName) return;
        // Le marker dans les colonnes texte est `**<NOM>**` (markdown bold).
        // On remplace en bloc.
        const oldMarker = `**${oldName}**`;
        const newMarker = `**${trimmed}**`;
        try {
            const resp = await fetch(`/api/file/${encodeURIComponent(fileId)}/correct-term`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    old: oldMarker, new: newMarker,
                    add_to_glossary: false,
                    patch_text: true,
                    reprocess_llm: false,
                }),
            });
            if (!resp.ok) {
                const data = await resp.json().catch(() => ({}));
                alert(`Échec renommage : ${data.error || resp.status}`);
                return;
            }
            // Patch in-place les occurrences visibles du nom (label .tc-speaker
            // + bouton ✏️ + textes `**NOM**`) pour éviter un loadSessions qui
            // collapserait la vue détail. La propagation backend (brute,
            // par-interlocuteur, glossaire, nettoyée, reformulation) a déjà
            // été faite par le POST ci-dessus ; un refresh manuel ou le
            // prochain polling re-synchronisera.
            container.querySelectorAll(`.tc-speaker`).forEach((el) => {
                if ((el.textContent || '').trim() === oldName) el.textContent = trimmed;
            });
            container.querySelectorAll(`[data-tc-speaker-rename="${CSS.escape(oldName)}"]`).forEach((el) => {
                el.setAttribute('data-tc-speaker-rename', trimmed);
                el.setAttribute('title', `Renommer cet interlocuteur partout`);
            });
            // Remplace `**oldName**` → `**trimmed**` dans tous les .tc-text rendus.
            const oldMd = `**${oldName}**`;
            const newMd = `**${trimmed}**`;
            container.querySelectorAll('.tc-text').forEach((el) => {
                if (el.innerHTML && el.innerHTML.includes(escapeHtml(oldName))) {
                    el.innerHTML = el.innerHTML.split(escapeHtml(oldName)).join(escapeHtml(trimmed));
                }
                if (el.textContent && el.textContent.includes(oldMd)) {
                    el.textContent = el.textContent.split(oldMd).join(newMd);
                }
            });
        } catch (e) {
            alert(`Erreur réseau : ${e.message}`);
        }
    });

    _attachLocalSearch(container);
}

// Recherche locale dans la transcription : highlight + navigation ▲/▼,
// Entrée = suivant, Maj+Entrée = précédent, statut "n/N", "début", "fin".
// On stocke le texte original par .tc-text (data-tc-original) au 1er appel
// pour pouvoir reconstruire sans accumuler les <mark>.
function _attachLocalSearch(container) {
    const find = container.querySelector('[data-tc-find]');
    if (!find) return;
    const inp = find.querySelector('.tc-find-input');
    const btnPrev = find.querySelector('.tc-find-prev');
    const btnNext = find.querySelector('.tc-find-next');
    const status = find.querySelector('[data-tc-find-status]');
    const textsEls = Array.from(container.querySelectorAll('.tc-text'));
    textsEls.forEach((el) => { el.setAttribute('data-tc-original', el.textContent || ''); });

    let hits = [];      // [{el, idxInText, length}]
    let cursor = -1;    // index courant dans hits

    const renderHighlights = (q) => {
        const norm = (q || '').toLowerCase();
        hits = [];
        textsEls.forEach((el) => {
            const orig = el.getAttribute('data-tc-original') || '';
            if (!norm) { el.textContent = orig; return; }
            const lower = orig.toLowerCase();
            // Build innerHTML avec <mark> autour de chaque occurrence.
            let out = '';
            let i = 0;
            while (i < orig.length) {
                const j = lower.indexOf(norm, i);
                if (j < 0) { out += escapeHtml(orig.slice(i)); break; }
                if (j > i) out += escapeHtml(orig.slice(i, j));
                const hitText = orig.slice(j, j + norm.length);
                const hitId = `tc-hit-${hits.length}`;
                out += `<mark class="tc-find-hit" id="${hitId}">${escapeHtml(hitText)}</mark>`;
                hits.push({ elId: hitId });
                i = j + norm.length;
            }
            el.innerHTML = out;
        });
    };

    const updateStatus = () => {
        if (!inp.value) { status.textContent = ''; return; }
        if (hits.length === 0) { status.textContent = '0 résultat'; return; }
        const pos = cursor < 0 ? 0 : cursor + 1;
        let suffix = '';
        if (cursor >= 0) {
            if (cursor === 0 && hits.length > 1) suffix = ' — début';
            else if (cursor === hits.length - 1 && hits.length > 1) suffix = ' — fin';
            else if (hits.length === 1) suffix = ' — unique';
        }
        status.textContent = `${pos}/${hits.length}${suffix}`;
    };

    const setCurrent = (idx) => {
        find.querySelectorAll('.tc-find-current').forEach((el) => el.classList.remove('tc-find-current'));
        if (idx < 0 || idx >= hits.length) return;
        const el = document.getElementById(hits[idx].elId);
        if (el) {
            el.classList.add('tc-find-current');
            el.scrollIntoView({ block: 'center', behavior: 'smooth' });
        }
    };

    const jump = (delta) => {
        if (hits.length === 0) { updateStatus(); return; }
        if (cursor < 0) cursor = delta > 0 ? 0 : hits.length - 1;
        else cursor = (cursor + delta + hits.length) % hits.length;
        setCurrent(cursor);
        updateStatus();
    };

    let debounce;
    inp.addEventListener('input', () => {
        clearTimeout(debounce);
        debounce = setTimeout(() => {
            cursor = -1;
            renderHighlights(inp.value);
            if (hits.length > 0) { cursor = 0; setCurrent(0); }
            updateStatus();
        }, 120);
    });
    inp.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter') {
            ev.preventDefault();
            jump(ev.shiftKey ? -1 : 1);
        } else if (ev.key === 'Escape') {
            inp.value = ''; cursor = -1; renderHighlights(''); updateStatus();
        }
    });
    btnNext.addEventListener('click', (ev) => { ev.preventDefault(); inp.focus(); jump(1); });
    btnPrev.addEventListener('click', (ev) => { ev.preventDefault(); inp.focus(); jump(-1); });
}

// ─── Édition du CR (modale + drawer source) ────────────────────────────
//
// Architecture :
//   1. _openCrEditorModal(fileId, data) → modale plein-écran avec 4
//      onglets (compte-rendu, reformulation, nettoyée, pour-les-absents),
//      recherche locale, sélection-pour-corriger (réutilise pipe
//      correct-term), pastilles 🔍 sur termes corrigés, bouton 🔍 par
//      ligne (→ drawer query fuzzy).
//   2. _openSourceDrawer(fileId, termOrQuery, opts) → drawer slide-in
//      droite avec les sources brutes. Deux modes :
//        - opts.mode === 'term' (défaut) : substring exact + propagation.
//        - opts.mode === 'query' : recherche fuzzy multi-mots (back-link
//          CR ligne → segments brute), pas de propagation.
//   La section "Transcription de la réunion" (corrector audio standalone)
//   reste séparée et n'apparaît pas dans la modale CR.

// 4 onglets de la modale d'édition du CR. La section "Transcription de
// la réunion" (corrector audio par interlocuteur + ▶ par segment + sync
// audio) reste séparée et inchangée — c'est la surface audio dédiée.
const CR_TABS = [
    { key: 'meeting_cr',    label: '📋 Compte-rendu',     source: (d) => _formatMeetingAnalysisAsMarkdown(d.meeting_analysis_json) },
    { key: 'reformulated',  label: '✍️ Reformulation',    source: (d) => d.reformulated_text || '' },
    { key: 'cleaned',       label: '🧹 Nettoyée',         source: (d) => d.cleaned_text || '' },
    { key: 'absentee',      label: '🪧 Pour les absents', source: (d) => d.absentee_summary || '' },
];

function _formatMeetingAnalysisAsMarkdown(jsonText) {
    // Le meeting_analysis_json est un objet 5-sections produit par LLM.
    // Format observé : objets typés par section avec champs variés selon
    // les versions du prompt. On extrait les champs connus, on garde le
    // reste (champs additionnels comme `context`, `details`, `priority`,
    // etc) en suffixe italique pour ne pas perdre d'info utile, mais on
    // évite d'afficher du JSON brut.
    if (!jsonText) return '';
    let obj;
    try { obj = JSON.parse(jsonText); }
    catch (e) { return jsonText; }
    if (!obj || typeof obj !== 'object') return String(jsonText);

    // Helpers : extrait le premier champ non-vide parmi une liste, et
    // formate les champs restants en "clé: valeur · ..." pour suffix.
    const pick = (o, keys) => {
        for (const k of keys) {
            if (o && o[k] != null && String(o[k]).trim()) return String(o[k]).trim();
        }
        return '';
    };
    const SKIP_KEYS = new Set([
        // Champs déjà extraits par section (vide = on les enlève des extras).
    ]);
    const fmtExtras = (o, consumedKeys) => {
        if (!o || typeof o !== 'object') return '';
        const consumed = new Set([...(consumedKeys || []), ...SKIP_KEYS]);
        const extras = Object.entries(o)
            .filter(([k, v]) => !consumed.has(k) && v != null && String(v).trim())
            .map(([k, v]) => {
                if (Array.isArray(v)) v = v.join(', ');
                else if (typeof v === 'object') v = JSON.stringify(v);
                return `${k}: ${v}`;
            });
        if (extras.length === 0) return '';
        return ` _(${extras.join(' · ')})_`;
    };

    const fmtActor = (a) => {
        if (typeof a === 'string') return `- ${a}`;
        const name = pick(a, ['name', 'speaker', 'label']);
        const role = pick(a, ['role', 'title']);
        const consumed = ['name','speaker','label','role','title'];
        const main = role ? `**${name}** — ${role}` : `**${name}**`;
        return `- ${main}${fmtExtras(a, consumed)}`;
    };
    const fmtTheme = (t) => {
        if (typeof t === 'string') return `- ${t}`;
        const title = pick(t, ['title', 'label', 'theme']);
        const summary = pick(t, ['summary', 'description']);
        const consumed = ['title','label','theme','summary','description'];
        const main = summary ? `**${title}** : ${summary}` : `**${title}**`;
        return `- ${main}${fmtExtras(t, consumed)}`;
    };
    const fmtDecision = (d) => {
        if (typeof d === 'string') return `- ${d}`;
        const text = pick(d, ['decision', 'text', 'title', 'statement']);
        const owner = pick(d, ['owner', 'assignee', 'responsible']);
        const deadline = pick(d, ['deadline', 'due', 'date', 'when']);
        const consumed = ['decision','text','title','statement','owner','assignee','responsible','deadline','due','date','when'];
        const meta = [owner && `porteur : ${owner}`, deadline && `échéance : ${deadline}`].filter(Boolean).join(' · ');
        const main = meta ? `${text} _(${meta})_` : text;
        return `- ${main}${fmtExtras(d, consumed)}`;
    };
    const fmtGap = (g) => {
        if (typeof g === 'string') return `- ${g}`;
        const text = pick(g, ['question', 'gap', 'text', 'title', 'issue']);
        const raised = pick(g, ['raised_by', 'asked_by', 'speaker']);
        const consumed = ['question','gap','text','title','issue','raised_by','asked_by','speaker'];
        const main = raised ? `${text} _(soulevé par ${raised})_` : text;
        return `- ${main}${fmtExtras(g, consumed)}`;
    };
    const fmtReco = (r) => {
        if (typeof r === 'string') return `- ${r}`;
        const text = pick(r, ['recommendation', 'text', 'title', 'action']);
        const why = pick(r, ['why', 'reason', 'rationale']);
        const consumed = ['recommendation','text','title','action','why','reason','rationale'];
        const main = why ? `${text} _— ${why}_` : text;
        return `- ${main}${fmtExtras(r, consumed)}`;
    };
    const sections = [
        { key: 'actors',          title: 'Acteurs',           fmt: fmtActor },
        { key: 'themes',          title: 'Thèmes',            fmt: fmtTheme },
        { key: 'decisions',       title: 'Décisions',         fmt: fmtDecision },
        { key: 'gaps',            title: 'Points en suspens', fmt: fmtGap },
        { key: 'recommendations', title: 'Recommandations',   fmt: fmtReco },
    ];
    const parts = [];
    for (const s of sections) {
        const v = obj[s.key];
        if (!v || (Array.isArray(v) && v.length === 0)) continue;
        parts.push(`## ${s.title}`);
        if (Array.isArray(v)) {
            parts.push(v.map(s.fmt).join('\n'));
        } else if (typeof v === 'string') {
            parts.push(v);
        }
        parts.push('');
    }
    return parts.join('\n') || jsonText;
}

function _openCrEditorModal(fileId, data) {
    // Modale plein-écran d'édition du CR. 4 onglets (Compte-rendu,
    // Reformulation, Nettoyée, Pour les absents) avec recherche locale,
    // sélection→corriger (réutilise pipe correct-term), pastilles 🔍 sur
    // les termes corrigés (→ drawer sources), et bouton 🔍 par ligne
    // (→ drawer query fuzzy). La section "Transcription de la réunion"
    // reste séparée et n'apparaît pas dans cette modale.
    const tabsWithContent = CR_TABS.filter((t) => (t.source(data) || '').trim().length > 0);
    if (tabsWithContent.length === 0) {
        alert('Aucun contenu à afficher (le compte-rendu n\'a pas encore été généré).');
        return;
    }
    document.querySelectorAll('.cr-editor-modal').forEach((el) => el.remove());

    const wrap = document.createElement('div');
    wrap.className = 'cr-editor-modal';
    wrap.setAttribute('role', 'dialog');
    wrap.setAttribute('aria-modal', 'true');
    wrap.style.cssText = 'position:fixed;inset:0;background:rgba(15,23,42,0.55);'
        + 'display:flex;align-items:center;justify-content:center;z-index:10000;'
        + 'padding:2rem;';
    const tabsHtml = tabsWithContent.map((t, i) => `
      <button type="button" class="cr-inline-tab ${i === 0 ? 'cr-inline-tab--active' : ''}"
              data-cr-tab="${t.key}">${t.label}</button>
    `).join('');
    wrap.innerHTML = `
      <div class="cr-editor-inner cr-inline" data-cr-inline-for="${escapeHtml(fileId)}"
           style="background:#fff;border-radius:0.5rem;max-width:980px;width:100%;
                  max-height:90vh;display:flex;flex-direction:column;
                  box-shadow:0 10px 40px rgba(0,0,0,0.25);">
        <div style="display:flex;justify-content:space-between;align-items:center;
                    padding:0.7rem 1rem;border-bottom:1px solid #e2e8f0;background:#f0f6ff;">
          <div style="font-weight:600;color:#0c4498;font-size:1rem;">📑 Modifier le compte-rendu</div>
          <button type="button" class="cr-editor-close" aria-label="Fermer"
                  style="background:transparent;border:0;font-size:1.3rem;cursor:pointer;color:#64748b;">×</button>
        </div>
        <div class="cr-inline-tabs">
          ${tabsHtml}
          <div class="cr-inline-find">
            <input type="search" placeholder="Chercher…" autocomplete="off" spellcheck="false" />
            <button type="button" data-cr-find="prev" title="Précédent (Maj+Entrée)">▲</button>
            <button type="button" data-cr-find="next" title="Suivant (Entrée)">▼</button>
            <span data-cr-find-status></span>
          </div>
        </div>
        <div class="cr-inline-body" data-cr-body
             style="flex:1 1 auto;overflow-y:auto;max-height:none;"></div>
        <div class="cr-inline-foot" style="padding:0.4rem 0.7rem;border-top:1px solid #f1f5f9;
             font-size:0.72rem;color:#64748b;text-align:center;">
          Astuce : sélectionnez un mot pour le corriger ·
          🔍 par ligne pour retrouver la source brute (audio + texte).
        </div>
      </div>
    `;
    document.body.appendChild(wrap);
    const close = () => wrap.remove();
    wrap.querySelector('.cr-editor-close').addEventListener('click', close);
    wrap.addEventListener('click', (ev) => { if (ev.target === wrap) close(); });
    wrap.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') close(); });

    // Bind l'intérieur — équivalent à l'ancien _attachCrInline mais
    // s'attache à `wrap` (le container modale).
    const root = wrap.querySelector('.cr-editor-inner');
    const body = root.querySelector('[data-cr-body]');
    const tabs = Array.from(root.querySelectorAll('[data-cr-tab]'));
    const findInp = root.querySelector('.cr-inline-find input');
    const findPrev = root.querySelector('[data-cr-find="prev"]');
    const findNext = root.querySelector('[data-cr-find="next"]');
    const findStatus = root.querySelector('[data-cr-find-status]');

    const correctedTerms = _getAppliedTermsForFile(fileId);
    let activeKey = tabs[0]?.getAttribute('data-cr-tab') || 'meeting_cr';

    const renderBody = (key) => {
        const tab = CR_TABS.find((t) => t.key === key);
        if (!tab) {
            body.innerHTML = `<div class="cr-inline-empty">Onglet inconnu.</div>`;
            return;
        }
        const text = tab.source(data) || '';
        if (!text.trim()) {
            body.innerHTML = `<div class="cr-inline-empty">Pas de contenu pour cet onglet.</div>`;
            return;
        }
        // Heuristique : si le texte contient des marqueurs markdown
        // structurels (titres ##, listes -, gras **), on le passe par
        // marked.js. Sinon (typiquement la reformulation = texte continu
        // avec retours à la ligne), on le rend en <pre.cr-plain> pour
        // préserver les sauts de ligne sans transformation.
        const looksLikeMarkdown = /^(\s*#{1,6}\s|\s*[-*]\s|\s*\d+\.\s)/m.test(text)
            || /\*\*[^*]+\*\*/.test(text);
        let html;
        if (looksLikeMarkdown && window.marked) {
            try { html = window.marked.parse(text); }
            catch (e) { html = `<pre class="cr-plain">${escapeHtml(text)}</pre>`; }
        } else {
            html = `<pre class="cr-plain">${escapeHtml(text)}</pre>`;
        }
        body.innerHTML = html;
        if (correctedTerms && correctedTerms.length > 0) {
            _markCorrectedTermsInBody(body, correctedTerms);
        }
        _decorateCrLinesWithFindButtons(body, fileId);
    };

    tabs.forEach((t) => {
        t.addEventListener('click', (ev) => {
            ev.preventDefault();
            tabs.forEach((x) => x.classList.remove('cr-inline-tab--active'));
            t.classList.add('cr-inline-tab--active');
            activeKey = t.getAttribute('data-cr-tab');
            renderBody(activeKey);
            // reset search
            if (findInp) { findInp.value = ''; if (findStatus) findStatus.textContent = ''; }
        });
    });

    // Search locale dans le body actif.
    let hits = [], cursor = -1;
    const renderHl = (q) => {
        if (!body) return;
        const original = body.getAttribute('data-cr-html-original') || body.innerHTML;
        if (!body.getAttribute('data-cr-html-original')) body.setAttribute('data-cr-html-original', original);
        hits = [];
        if (!q) { body.innerHTML = original; return; }
        // Recherche substring case-insensitive sur le textContent, avec
        // re-wrapping via innerHTML. Approche simple : DOM walker.
        body.innerHTML = original;
        const ql = q.toLowerCase();
        const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT, null);
        const nodes = [];
        let n; while ((n = walker.nextNode())) nodes.push(n);
        nodes.forEach((tn) => {
            const txt = tn.nodeValue || '';
            const lower = txt.toLowerCase();
            if (lower.indexOf(ql) < 0) return;
            const frag = document.createDocumentFragment();
            let i = 0;
            while (i < txt.length) {
                const j = lower.indexOf(ql, i);
                if (j < 0) { frag.appendChild(document.createTextNode(txt.slice(i))); break; }
                if (j > i) frag.appendChild(document.createTextNode(txt.slice(i, j)));
                const mk = document.createElement('mark');
                mk.className = 'tc-find-hit';
                mk.id = `cr-hit-${hits.length}`;
                mk.textContent = txt.slice(j, j + ql.length);
                frag.appendChild(mk);
                hits.push({ elId: mk.id });
                i = j + ql.length;
            }
            tn.parentNode.replaceChild(frag, tn);
        });
    };
    const setCurrent = (idx) => {
        body.querySelectorAll('.tc-find-current').forEach((el) => el.classList.remove('tc-find-current'));
        if (idx < 0 || idx >= hits.length) return;
        const el = document.getElementById(hits[idx].elId);
        if (el) { el.classList.add('tc-find-current'); el.scrollIntoView({ block: 'center', behavior: 'smooth' }); }
    };
    const updateStat = () => {
        if (!findInp || !findStatus) return;
        if (!findInp.value) { findStatus.textContent = ''; return; }
        if (hits.length === 0) { findStatus.textContent = '0 résultat'; return; }
        const pos = cursor < 0 ? 0 : cursor + 1;
        let suf = '';
        if (cursor === 0 && hits.length > 1) suf = ' — début';
        else if (cursor === hits.length - 1 && hits.length > 1) suf = ' — fin';
        else if (hits.length === 1) suf = ' — unique';
        findStatus.textContent = `${pos}/${hits.length}${suf}`;
    };
    const jump = (delta) => {
        if (hits.length === 0) { updateStat(); return; }
        if (cursor < 0) cursor = delta > 0 ? 0 : hits.length - 1;
        else cursor = (cursor + delta + hits.length) % hits.length;
        setCurrent(cursor);
        updateStat();
    };
    if (findInp) {
        let debounce;
        findInp.addEventListener('input', () => {
            clearTimeout(debounce);
            debounce = setTimeout(() => {
                cursor = -1; renderHl(findInp.value);
                if (hits.length > 0) { cursor = 0; setCurrent(0); }
                updateStat();
            }, 120);
        });
        findInp.addEventListener('keydown', (ev) => {
            if (ev.key === 'Enter') { ev.preventDefault(); jump(ev.shiftKey ? -1 : 1); }
            else if (ev.key === 'Escape') { findInp.value = ''; cursor = -1; renderHl(''); updateStat(); }
        });
        findNext.addEventListener('click', (ev) => { ev.preventDefault(); findInp.focus(); jump(1); });
        findPrev.addEventListener('click', (ev) => { ev.preventDefault(); findInp.focus(); jump(-1); });
    }

    // Sélection texte pour corriger : footer attaché au root de la modale.
    body.addEventListener('mouseup', () => _onCrInlineSelection(body, fileId, root));
    body.addEventListener('touchend', () => _onCrInlineSelection(body, fileId, root));

    // Click sur pastille 🔍 (terme corrigé) → drawer sources brutes mode term.
    body.addEventListener('click', (ev) => {
        const span = ev.target.closest && ev.target.closest('.cr-corrected-term');
        if (!span) return;
        ev.preventDefault();
        const term = span.getAttribute('data-cr-term') || '';
        if (term) _openSourceDrawer(fileId, term);
    });

    renderBody(activeKey);
}

// Délégation click globale : bouton ✏️ "Modifier" sur la ligne CR ouvre
// la modale d'édition. On lit `data` depuis le container persistent qui
// l'a stocké (cf loadTranscriptStatus). Fallback : refetch depuis l'API.
document.addEventListener('click', async (ev) => {
    const btn = ev.target.closest && ev.target.closest('[data-cr-edit]');
    if (!btn) return;
    ev.preventDefault();
    const fileId = btn.getAttribute('data-cr-edit') || '';
    if (!fileId) return;
    // Cherche le container persistent qui stocke `data`.
    const persistentCt = document.querySelector(`.transcript-section[data-transcript-file-id="${CSS.escape(fileId)}"]`);
    let data = persistentCt && persistentCt._mesreunionsCrData;
    if (!data) {
        try {
            const resp = await fetch(`/api/file/transcript-status/${encodeURIComponent(fileId)}`);
            data = await resp.json();
        } catch (e) {
            alert(`Chargement impossible : ${e.message}`);
            return;
        }
    }
    if (!data || !data.available) {
        alert('Le compte-rendu n\'est pas encore disponible.');
        return;
    }
    _openCrEditorModal(fileId, data);
});

function _onCrInlineSelection(body, fileId, parentContainer) {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed) return;
    const selected = (sel.toString() || '').trim();
    if (selected.length < 1 || selected.length > 200) return;
    const anchor = sel.anchorNode;
    if (!anchor) return;
    const node = (anchor.nodeType === 1 ? anchor : anchor.parentElement);
    if (!body.contains(node)) return;
    // On rend le footer correction dans le container parent (réutilise
    // l'infrastructure existante).
    let footer = parentContainer.querySelector(`[data-tc-corrector-footer="${CSS.escape(fileId)}"]`);
    if (!footer) {
        footer = document.createElement('div');
        footer.className = 'transcript-corrector-footer';
        footer.setAttribute('data-tc-corrector-footer', fileId);
        footer.hidden = true;
        parentContainer.appendChild(footer);
    }
    _showCorrectionFooter(parentContainer, fileId, selected, null, null, []);
}

function _getAppliedTermsForFile(fileId) {
    // Récupère les termes déjà corrigés sur ce file via localStorage.
    // Format : Map { fileId -> [{old, new}, ...] }.
    try {
        const raw = localStorage.getItem('mesreunions.corrections.applied');
        if (!raw) return [];
        const all = JSON.parse(raw);
        const arr = all && all[fileId];
        return Array.isArray(arr) ? arr : [];
    } catch (e) { return []; }
}

function _saveAppliedTermForFile(fileId, oldTerm, newTerm) {
    try {
        const raw = localStorage.getItem('mesreunions.corrections.applied');
        const all = raw ? JSON.parse(raw) : {};
        const arr = all[fileId] || [];
        // Dédoublonne sur (old, new).
        if (!arr.some((x) => x.old === oldTerm && x.new === newTerm)) {
            arr.push({ old: oldTerm, new: newTerm, ts: Date.now() });
        }
        all[fileId] = arr.slice(-50); // cap
        localStorage.setItem('mesreunions.corrections.applied', JSON.stringify(all));
    } catch (e) {}
}

function _markCorrectedTermsInBody(body, correctedTerms) {
    // Pour chaque terme "new", wrap les occurrences dans body avec
    // <span class="cr-corrected-term" data-cr-term="new">…</span>.
    correctedTerms.forEach((c) => {
        const newTerm = (c.new || '').trim();
        if (!newTerm || newTerm.length < 2) return;
        const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT, null);
        const nodes = [];
        let n; while ((n = walker.nextNode())) nodes.push(n);
        nodes.forEach((tn) => {
            // Skip si déjà dans un span.cr-corrected-term ou .tc-find-hit.
            if (tn.parentElement && (
                tn.parentElement.classList.contains('cr-corrected-term') ||
                tn.parentElement.classList.contains('tc-find-hit')
            )) return;
            const txt = tn.nodeValue || '';
            const lower = txt.toLowerCase();
            const ql = newTerm.toLowerCase();
            const j = lower.indexOf(ql);
            if (j < 0) return;
            const before = txt.slice(0, j);
            const hit = txt.slice(j, j + newTerm.length);
            const after = txt.slice(j + newTerm.length);
            const frag = document.createDocumentFragment();
            if (before) frag.appendChild(document.createTextNode(before));
            const sp = document.createElement('span');
            sp.className = 'cr-corrected-term';
            sp.setAttribute('data-cr-term', newTerm);
            sp.setAttribute('title', `Terme corrigé — cliquez pour voir les sources brutes`);
            sp.textContent = hit;
            frag.appendChild(sp);
            if (after) frag.appendChild(document.createTextNode(after));
            tn.parentNode.replaceChild(frag, tn);
        });
    });
}

function _decorateCrLinesWithFindButtons(body, fileId) {
    // Cible : tout li / p / h2 / h3 qui contient assez de texte (> 12 chars)
    // pour valoir une recherche fuzzy en source. On évite les titres mono-mot.
    const selectors = ['li', 'p', 'h2', 'h3'];
    body.querySelectorAll(selectors.join(',')).forEach((el) => {
        const txt = (el.textContent || '').trim();
        if (txt.length < 12) return;
        if (el.querySelector('.cr-find-source-btn')) return; // déjà décoré
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'cr-find-source-btn';
        btn.textContent = '🔍';
        btn.setAttribute('title', 'Retrouver la source brute (audio + texte)');
        btn.style.cssText =
            'opacity:0;margin-left:0.4rem;font-size:0.7rem;line-height:1;'
            + 'padding:0.1rem 0.3rem;border:1px solid #cbd5e1;background:#fff;'
            + 'border-radius:3px;cursor:pointer;transition:opacity 120ms;'
            + 'vertical-align:middle;';
        el.appendChild(document.createTextNode(' '));
        el.appendChild(btn);
        el.addEventListener('mouseenter', () => { btn.style.opacity = '0.75'; });
        el.addEventListener('mouseleave', () => { btn.style.opacity = '0'; });
        btn.addEventListener('mouseenter', () => { btn.style.opacity = '1'; });
        btn.addEventListener('click', (ev) => {
            ev.preventDefault();
            ev.stopPropagation();
            // On extrait le texte sans le bouton lui-même pour la requête.
            const query = (el.textContent || '').replace('🔍', '').trim();
            _openSourceDrawer(fileId, query, { mode: 'query' });
        });
    });
}

function _openSourceDrawer(fileId, termOrQuery, opts) {
    opts = opts || {};
    const mode = opts.mode === 'query' ? 'query' : 'term';
    const isQuery = (mode === 'query');
    // Tronque l'affichage si requête longue (lignes CR peuvent faire 200+ chars).
    const headerLabel = isQuery
        ? (termOrQuery.length > 80 ? termOrQuery.slice(0, 77) + '…' : termOrQuery)
        : termOrQuery;
    const headerTitle = isQuery
        ? `🔍 Source brute de la ligne CR`
        : `🔍 Sources brutes de « ${escapeHtml(termOrQuery)} »`;
    // Supprime drawer existant si présent.
    document.querySelectorAll('.cr-drawer-backdrop, .cr-drawer').forEach((el) => el.remove());
    const backdrop = document.createElement('div');
    backdrop.className = 'cr-drawer-backdrop';
    const drawer = document.createElement('div');
    drawer.className = 'cr-drawer';
    // En mode query, on ne propose pas la propagation (l'user explore une
    // source, il ne corrige pas un terme). Footer caché par défaut.
    drawer.innerHTML = `
      <div class="cr-drawer-head">
        <div class="cr-drawer-title">${headerTitle}</div>
        <button type="button" class="cr-drawer-close" aria-label="Fermer">×</button>
      </div>
      <div class="cr-drawer-body">
        ${isQuery ? `<div style="font-size:0.75rem;color:#64748b;padding:0 0 0.5rem;font-style:italic;">« ${escapeHtml(headerLabel)} »</div>` : ''}
        <p style="color:#94a3b8;font-size:0.85rem;text-align:center;padding:1rem;">Chargement…</p>
      </div>
      ${isQuery ? '' : `
      <div class="cr-drawer-foot" style="display:none;">
        <label><input type="checkbox" data-cr-target="raw" checked /> Propager dans la transcription brute</label>
        <label><input type="checkbox" data-cr-target="clean" /> Propager dans la nettoyée</label>
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <span class="status" data-cr-drawer-status></span>
          <button type="button" class="primary" data-cr-drawer-apply>Appliquer aux segments cochés</button>
        </div>
      </div>`}
    `;
    document.body.appendChild(backdrop);
    document.body.appendChild(drawer);
    requestAnimationFrame(() => { backdrop.classList.add('is-open'); drawer.classList.add('is-open'); });
    const close = () => {
        backdrop.classList.remove('is-open');
        drawer.classList.remove('is-open');
        setTimeout(() => { backdrop.remove(); drawer.remove(); }, 240);
    };
    backdrop.addEventListener('click', close);
    drawer.querySelector('.cr-drawer-close').addEventListener('click', close);

    // Fetch sources (paramètre term ou query selon le mode).
    const qParam = isQuery ? `query=${encodeURIComponent(termOrQuery)}` : `term=${encodeURIComponent(termOrQuery)}`;
    fetch(`/api/file/${encodeURIComponent(fileId)}/term-sources?${qParam}`)
        .then((r) => r.json().then((d) => ({ ok: r.ok, data: d })))
        .then(({ ok, data }) => {
            const bodyEl = drawer.querySelector('.cr-drawer-body');
            const footEl = drawer.querySelector('.cr-drawer-foot');
            if (!ok) {
                bodyEl.innerHTML = `<p style="color:#b91c1c;font-size:0.85rem;">Erreur : ${escapeHtml(data.error || 'inconnu')}</p>`;
                return;
            }
            const sources = data.sources || [];
            if (sources.length === 0) {
                const msg = isQuery
                    ? `Aucune source brute n'a un recouvrement suffisant avec cette ligne (le LLM a beaucoup reformulé).`
                    : `Aucune source brute trouvée pour « ${escapeHtml(termOrQuery)} » (le LLM a peut-être reformulé).`;
                bodyEl.innerHTML = `<p style="color:#94a3b8;font-size:0.85rem;padding:1rem;text-align:center;">${msg}</p>`;
                return;
            }
            const audio = document.querySelector(`.transcript-corrector-audio`);
            const items = sources.map((s) => {
                const scoreLabel = (s.score && isQuery) ? ` <span style="font-size:0.65rem;color:#94a3b8;">(${s.score} mot${s.score > 1 ? 's' : ''})</span>` : '';
                return `
              <div class="cr-drawer-source" data-cr-src-idx="${s.idx}">
                ${isQuery ? '' : '<input type="checkbox" class="cr-drawer-source-check" checked />'}
                ${s.start != null
                    ? `<button type="button" class="cr-drawer-source-play" data-cr-play="${s.start}"
                               title="Écouter ce passage">▶ ${_fmtTimecode(s.start)}</button>`
                    : `<span style="font-size:0.7rem;color:#94a3b8;">—</span>`}
                <div class="cr-drawer-source-content">
                  <div class="cr-drawer-source-meta">${escapeHtml(s.speaker || '')}${scoreLabel}</div>
                  <div class="cr-drawer-source-snippet">${escapeHtml(s.snippet || '')}</div>
                </div>
              </div>`;
            }).join('');
            const header = isQuery
                ? `${sources.length} segment${sources.length > 1 ? 's' : ''} probable${sources.length > 1 ? 's' : ''} (triés par recouvrement de mots).`
                : `${sources.length} occurrence${sources.length > 1 ? 's' : ''} trouvée${sources.length > 1 ? 's' : ''} dans la brute.`;
            const queryPreview = isQuery
                ? `<div style="font-size:0.75rem;color:#64748b;padding:0 0 0.5rem;font-style:italic;">« ${escapeHtml(headerLabel)} »</div>`
                : '';
            bodyEl.innerHTML = `${queryPreview}<div style="font-size:0.78rem;color:#64748b;padding:0 0 0.5rem;">${header}</div>${items}`;
            if (footEl) footEl.style.display = 'flex';

            // Click ▶ : seek audio (utilise le lecteur sticky du corrector
            // si présent dans la page).
            bodyEl.querySelectorAll('[data-cr-play]').forEach((btn) => {
                btn.addEventListener('click', (ev) => {
                    ev.preventDefault();
                    const t = parseFloat(btn.getAttribute('data-cr-play')) || 0;
                    if (audio) {
                        try { audio.pause(); audio.currentTime = Math.max(0, t); audio.play(); } catch (e) {}
                    }
                });
            });
            // En mode query, pas de propagation : l'user explore seulement.
            if (isQuery) return;

            // Apply propagation (mode term seulement).
            const applyBtn = drawer.querySelector('[data-cr-drawer-apply]');
            const statusEl = drawer.querySelector('[data-cr-drawer-status]');
            applyBtn.addEventListener('click', async (ev) => {
                ev.preventDefault();
                applyBtn.disabled = true;
                statusEl.textContent = 'Application…';
                statusEl.style.color = '#64748b';
                // Pour MVP : on propage via correct-term (qui patch tous
                // les textes contenant l'ancien terme). L'ancien terme est
                // dérivé du snippet (entre « »).
                const oldMatch = sources.map((s) => {
                    const m = (s.snippet || '').match(/«([^»]+)»/);
                    return m ? m[1] : null;
                }).filter(Boolean);
                if (oldMatch.length === 0) {
                    statusEl.textContent = 'Échec : impossible de déterminer le terme à remplacer.';
                    statusEl.style.color = '#b91c1c';
                    applyBtn.disabled = false;
                    return;
                }
                const oldTerm = oldMatch[0]; // tous identiques (case du hit)
                try {
                    const r = await fetch(`/api/file/${encodeURIComponent(fileId)}/correct-term`, {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            old: oldTerm, new: termOrQuery,
                            add_to_glossary: false,
                            patch_text: true,
                            reprocess_llm: false,
                        }),
                    });
                    const j = await r.json().catch(() => ({}));
                    if (!r.ok) {
                        statusEl.textContent = `Échec : ${j.error || r.status}`;
                        statusEl.style.color = '#b91c1c';
                        applyBtn.disabled = false;
                        return;
                    }
                    statusEl.textContent = `✓ Propagé dans la brute & nettoyée`;
                    statusEl.style.color = '#16a34a';
                    setTimeout(close, 1400);
                } catch (e) {
                    statusEl.textContent = `Erreur réseau : ${e.message}`;
                    statusEl.style.color = '#b91c1c';
                    applyBtn.disabled = false;
                }
            });
        })
        .catch((e) => {
            const bodyEl = drawer.querySelector('.cr-drawer-body');
            bodyEl.innerHTML = `<p style="color:#b91c1c;font-size:0.85rem;">Erreur réseau : ${escapeHtml(e.message)}</p>`;
        });
}

function _onTranscriptSelection(container, fileId, audio, blocks) {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed) return;
    const selected = (sel.toString() || '').trim();
    if (selected.length < 1 || selected.length > 200) return;
    // Vérifie que la sélection est bien à l'intérieur d'un .tc-text de NOTRE container.
    const anchor = sel.anchorNode;
    if (!anchor) return;
    const parentTc = (anchor.nodeType === 1 ? anchor : anchor.parentElement).closest('.tc-text');
    if (!parentTc || !container.contains(parentTc)) return;
    const blockEl = parentTc.closest('.tc-block');
    const blockIdx = blockEl ? parseInt(blockEl.getAttribute('data-tc-idx'), 10) : null;
    _showCorrectionFooter(container, fileId, selected, blockIdx, audio, blocks);
}

function _showCorrectionFooter(container, fileId, selectedText, blockIdx, audio, blocks) {
    const footer = container.querySelector(`[data-tc-corrector-footer="${fileId}"]`);
    if (!footer) return;
    const block = (blockIdx != null && blocks[blockIdx]) ? blocks[blockIdx] : null;
    const playStart = block ? block.start : 0;
    footer.innerHTML = `
      <div class="tc-correct-form">
        <div class="tc-correct-head">
          <span class="tc-correct-label">Corriger&nbsp;:</span>
          <code class="tc-correct-old">${escapeHtml(selectedText)}</code>
          ${block ? `<button type="button" class="tc-correct-listen" data-tc-play="${playStart}"
                              title="Réécouter ce passage">🔊 ${_fmtTimecode(playStart)}</button>` : ''}
          <button type="button" class="tc-correct-close"
                  onclick="document.querySelector('[data-tc-corrector-footer=\\'${fileId}\\']').hidden=true; document.querySelector('[data-tc-corrector-footer=\\'${fileId}\\']').innerHTML='';">×</button>
        </div>
        <div class="tc-correct-row">
          <input type="text" class="tc-correct-new"
                 placeholder="Remplacer par…"
                 maxlength="200" />
        </div>
        <div class="tc-correct-opts">
          <label><input type="checkbox" class="tc-opt-glossary" checked /> Ajouter au glossaire personnel</label>
          <label><input type="checkbox" class="tc-opt-patch" checked /> Remplacer dans cette transcription</label>
          <!-- Case "Relancer les étapes LLM" retirée : le user déclenche
               manuellement la régénération CR via le bouton dédié en bas
               (signalé par un clignotement quand des modifs sont
               appliquées sans reprocess). Réduit le nb de runs LLM
               coûteux par session de correction. -->
        </div>
        <div class="tc-correct-actions">
          <button type="button" class="tc-correct-apply"
                  data-tc-apply="${escapeHtml(fileId)}"
                  data-tc-old="${escapeHtml(selectedText)}">Appliquer</button>
          <span class="tc-correct-status" data-tc-status></span>
        </div>
      </div>
    `;
    footer.hidden = false;
    const inp = footer.querySelector('.tc-correct-new');
    if (inp) inp.focus();
}

// Délégation click APPLIQUE/Listen au niveau document (le footer est rendu
// dans des containers existants, on évite de rebrancher à chaque rendu).
document.addEventListener('click', async (ev) => {
    const applyBtn = ev.target.closest && ev.target.closest('[data-tc-apply]');
    if (applyBtn) {
        ev.preventDefault();
        const fileId = applyBtn.getAttribute('data-tc-apply');
        const old = applyBtn.getAttribute('data-tc-old') || '';
        const footer = applyBtn.closest('.transcript-corrector-footer');
        if (!footer) return;
        const newText = (footer.querySelector('.tc-correct-new')?.value || '').trim();
        if (!newText) {
            alert('Indiquez le terme de remplacement.');
            return;
        }
        const body = {
            old, new: newText,
            add_to_glossary: footer.querySelector('.tc-opt-glossary')?.checked,
            patch_text:     footer.querySelector('.tc-opt-patch')?.checked,
            reprocess_llm:  footer.querySelector('.tc-opt-reprocess')?.checked,
        };
        const status = footer.querySelector('[data-tc-status]');
        applyBtn.disabled = true;
        if (status) status.textContent = 'Application…';
        try {
            const resp = await fetch(`/api/file/${encodeURIComponent(fileId)}/correct-term`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) {
                if (status) status.textContent = `Échec : ${data.error || resp.status}`;
                applyBtn.disabled = false;
                return;
            }
            const applied = (data.applied || []).join(', ') || 'rien';
            if (status) status.textContent = `✓ ${applied}`;
            // Incrémente le compteur "corrections en attente de reprocess
            // LLM" pour ce fichier (persisté en localStorage) → le badge
            // apparaît sur le bloc feedback et le bouton "🔄 Comptes-rendus"
            // se met à clignoter pour inviter l'utilisateur à régénérer.
            // Sauf si l'user a inclus reprocess_llm (= les CR sont déjà
            // refaits avec la nouvelle correction).
            if (!body.reprocess_llm) {
                incrementPendingCorrections(fileId);
            }
            // Persiste le terme corrigé pour faire apparaître la pastille
            // 🔍 dans le CR inline (option C drawer source).
            try { _saveAppliedTermForFile(fileId, old, newText); } catch (e) {}
            // Patch in-place de la transcription visible (sans reload :
            // un loadSessions ici refermerait la vue détail et le footer
            // de correction, ce qui interrompt le flow de l'utilisateur
            // qui veut enchaîner plusieurs corrections). Le serveur a
            // déjà appliqué le str.replace sur toutes les colonnes texte,
            // donc le prochain refresh manuel sera cohérent.
            if (body.patch_text || body.reprocess_llm) {
                _patchVisibleTranscriptOccurrences(fileId, old, newText);
            }
            // Garde le footer ouvert pour enchaîner d'autres corrections sans
            // re-sélectionner. On vide juste le champ "remplacer par" et on
            // réactive le bouton ; le statut ✓ reste visible.
            const inp = footer.querySelector('.tc-correct-new');
            if (inp) { inp.value = ''; inp.focus(); }
            applyBtn.disabled = false;
        } catch (e) {
            if (status) status.textContent = `Erreur : ${e.message}`;
            applyBtn.disabled = false;
        }
    }
});

// Remplace en place les occurrences de `oldTerm` par `newTerm` dans
// tous les .tc-text de la fiche détail correspondant au fileId (vue
// "Transcription de la réunion"). Evite un loadSessions() qui
// refermerait la vue. Le serveur a déjà appliqué le patch côté DB,
// c'est juste pour synchroniser l'affichage immédiat.
function _patchVisibleTranscriptOccurrences(fileId, oldTerm, newTerm) {
    if (!oldTerm || !newTerm || oldTerm === newTerm) return;
    const corrector = document.querySelector(
        `.file-detail-corrector-block[data-corrector-for="${CSS.escape(fileId)}"]`
    );
    if (!corrector) return;
    const oldLower = oldTerm.toLowerCase();
    corrector.querySelectorAll('.tc-text').forEach((el) => {
        const txt = el.textContent || '';
        if (txt.toLowerCase().indexOf(oldLower) < 0) return;
        // Case-preserving substitution simple : on remplace les occurrences
        // exactes (case-sensitive d'abord, puis case-insensitive).
        let updated = txt.split(oldTerm).join(newTerm);
        if (updated === txt) {
            // Fallback case-insensitive : substring lookup, splice manuel.
            const re = new RegExp(oldTerm.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
            updated = txt.replace(re, newTerm);
        }
        el.textContent = updated;
    });
}

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

        // Hook nouveau module tabs/meetings.js (rebuild UI 2026-05-17).
        // Si le module a publié son renderer, on lui délègue intégralement
        // le rendu de #sessions-list (y compris l'empty state). Le reste
        // de loadSessions (transferBox, activityRail, file count global)
        // est conservé pour compat — il pourra être déplacé dans le
        // module en suite.
        // EXCEPTION : en mode "fiche détail" (showFileDetail a posé la
        // classe .detail-active sur le pane), on laisse passer le rendu
        // legacy qui sait afficher la vue page-détail (lines ~1577+).
        const _inDetailView = !!(container && container.closest('.tab-pane.detail-active'));
        if (!_inDetailView && window.__meetingsTab && typeof window.__meetingsTab.renderList === 'function') {
            try {
                window.__meetingsTab.renderList(sessions);
                // L'en-tête de table legacy est obsolète avec le nouveau
                // module (qui dessine son propre header).
                const legacyHeader = document.getElementById('sessions-table-header');
                if (legacyHeader) legacyHeader.style.display = 'none';
                // Le compteur global du header legacy est aussi pris en
                // charge par le module (file-count dans l'ancien layout).
                const legacyCount = document.getElementById('file-count');
                if (legacyCount) legacyCount.textContent = '';
                return;
            } catch (e) {
                console.error('[meetings tab] renderList failed, falling back to legacy render', e);
                // On enchaîne sur le rendu legacy en cas d'erreur.
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
                            <!-- Source : icône (smartphone pour device enrôlé,
                                 upload pour fichier local) avec tooltip donnant
                                 le détail (nom device + simple_code). Remplace
                                 l'ancien chip texte qui chevauchait le titre
                                 et la zone détails sur les libellés longs. -->
                            <span class="${s.is_local_upload ? 'fr-icon-upload-line' : 'fr-icon-smartphone-line'} file-row-source file-row-source--${s.is_local_upload ? 'local' : 'device'}"
                                  role="img"
                                  aria-label="Source : ${escapeHtml(deviceLabelForRow || (s.is_local_upload ? 'Upload local' : 'Appareil enrôlé'))}"
                                  title="${escapeHtml(deviceLabelForRow || (s.is_local_upload ? 'Upload local' : 'Appareil enrôlé'))}${s.simple_code ? ' — code ' + escapeHtml(s.simple_code) : ''}"></span>
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
                    <!-- Titre éditable : input flex + 2 boutons d'action
                         (✓ valider activé si modifié, ↺ annuler activé si
                         modifié = restore valeur originale) | bouton (i)
                         info pipeline à l'extrême droite (cale à droite via
                         margin-left:auto sur le (i)). La date d'upload est
                         dans la techline ci-dessous (à droite du nom du
                         fichier source). -->
                    <div class="file-detail-title-row">
                        <input class="file-detail-title-input file-detail-edit-input" type="text"
                               value="${escapeHtml(f.original_filename)}"
                               data-original-title="${escapeHtml(f.original_filename)}"
                               data-detail-title-for="${f.id}"
                               placeholder="Titre de la réunion" />
                        <button class="file-detail-action-btn file-detail-action-btn--validate file-detail-rename-btn"
                                onclick="renameDetailTitle('${f.id}', this)"
                                title="Enregistrer le nouveau titre"
                                aria-label="Enregistrer le nouveau titre"
                                disabled>${ICONS.check}</button>
                        <button class="file-detail-action-btn file-detail-action-btn--revert"
                                data-detail-title-revert-for="${f.id}"
                                onclick="revertDetailTitle('${f.id}')"
                                title="Annuler les modifications"
                                aria-label="Annuler les modifications du titre"
                                disabled>${ICONS.revert}</button>
                        <button class="file-detail-info-btn file-detail-info-btn--inline"
                                type="button"
                                data-file-info-btn="${f.id}"
                                onclick="openFileInfoModal('${f.id}')"
                                title="Détails techniques (statut, qualité, étapes IA, normalisation)"
                                aria-label="Voir les détails techniques">i</button>
                    </div>
                    <!-- Date *réelle* de la réunion, surchargée par
                         l'utilisateur. NULL côté serveur = pas d'override,
                         l'UI retombe sur created_at pour l'affichage et
                         le tri. 2 boutons : ✓ valider (activé si la valeur
                         courante diffère de la valeur initiale) et ↺
                         annuler (revert à la valeur sauvegardée ; quand
                         la valeur est inchangée, le ↺ devient "effacer
                         l'override" pour retomber sur created_at). -->
                    <div class="file-detail-meeting-row">
                        <span class="file-detail-meeting-label">Date de la réunion :</span>
                        <input type="datetime-local"
                               class="file-detail-meeting-input file-detail-edit-input"
                               data-meeting-dt-for="${f.id}"
                               data-meeting-dt-original="${escapeHtml(_isoToDatetimeLocal(f.meeting_datetime) || '')}"
                               value="${_isoToDatetimeLocal(f.meeting_datetime) || ''}"
                               placeholder="${_isoToDatetimeLocal(f.created_at) || ''}" />
                        <button class="file-detail-action-btn file-detail-action-btn--validate"
                                data-meeting-dt-save-for="${f.id}"
                                onclick="saveMeetingDatetime('${f.id}', document.querySelector('[data-meeting-dt-for=\\'${f.id}\\']'))"
                                title="Enregistrer la date de réunion"
                                aria-label="Enregistrer la date de réunion"
                                disabled>${ICONS.check}</button>
                        <button class="file-detail-action-btn file-detail-action-btn--revert"
                                data-meeting-dt-reset-for="${f.id}"
                                onclick="resetMeetingDatetime('${f.id}')"
                                title="${f.meeting_datetime ? "Effacer la date saisie (retombe sur la date d'upload)" : 'Aucune modification à annuler'}"
                                aria-label="Annuler les modifications de la date"
                                ${f.meeting_datetime ? '' : 'disabled'}>${ICONS.revert}</button>
                        <span class="file-detail-meeting-status"
                              data-meeting-dt-status-for="${f.id}"></span>
                    </div>
                    <!-- Ligne sous le titre : date+durée à gauche, nom du
                         fichier source au milieu, "Uploadé le ..." à droite
                         (info immuable). Le bouton (i) info pipeline a
                         migré sur la title row (à droite du nom de la
                         réunion). -->
                    <div class="file-detail-techline">
                        <span class="file-row-meta">
                            <span class="file-row-meta-date ${dateClass}">${escapeHtml(fileDateLabel)}</span>
                            ${fileDurLabel ? `<span class="file-row-meta-dur">${escapeHtml(fileDurLabel)}</span>` : ''}
                        </span>
                        <span class="file-detail-source-filename"
                              title="Nom d'origine du fichier audio">
                            ${escapeHtml(f.original_filename)}
                        </span>
                        <span class="file-detail-upload-info"
                              title="Date d'upload du fichier (immuable)">
                            Uploadé le ${escapeHtml(_formatDateCompact(f.created_at))}
                        </span>
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
                    <!-- Bloc correction inline (Phase B+B3) : parse le
                         speaker_tagged_text en blocs {speaker, start, end,
                         text}, affiche chaque bloc avec un bouton ▶ qui
                         joue l'audio à ce timecode, et permet de
                         sélectionner un mot/expression pour le corriger
                         (audit dans user_feedback type='correction'). Mount
                         délégué à mountTranscriptCorrector().
                         data-audio-url : URL audio prioritaire (transferred
                         interne survit le plus longtemps, puis transcoded
                         DMZ purgé ~7j) ; vide si tout est purgé → boutons
                         ▶ grisés + bandeau. -->
                    <div class="file-detail-corrector-block"
                         data-corrector-for="${f.id}"
                         data-audio-url="${escapeHtml(f.transferred_stream_url || f.transcoded_stream_url || f.source_stream_url || '')}"
                         data-audio-duration="${f.audio_duration_seconds || ''}"
                         data-audio-purged="${(!f.transferred_available && !f.transcoded_available && !f.source_available) ? '1' : '0'}"></div>
                    <!-- Bloc feedback (en bas de fiche, après la lecture du
                         contenu) : Régénérer + pouce ↑/↓ "utile?". Voir
                         services/mesreunions-web/app/modules/feedback/routes.py
                         pour le backend. Le rendu interne du widget est
                         délégué à mountFeedbackBlock() côté JS (déclenché
                         par MutationObserver après insertion DOM). -->
                    <div class="file-detail-feedback-block" data-feedback-for="${f.id}"></div>
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

// Libellés du dropdown "Autres téléchargements" (vue détail).
// 3 macro-libellés exposés par défaut (cf SIMPLE_OTHER_KINDS) :
// Transcription nettoyée + Synthèse narrative ; + le CR via son
// propre dropdown. Les 3 intermédiaires (raw/tagged/corrected) sont
// visibles UNIQUEMENT en mode avancé pour debug — d'où l'étiquette
// "étape intermédiaire" qui dissipe la confusion utilisateur.
const TRANSCRIPT_KIND_LABELS = {
    'transcript':              'Transcription brute (étape intermédiaire)',
    'transcript-tagged':       'Transcription de la réunion (par interlocuteur — étape intermédiaire)',
    'transcript-corrected':    'Transcription avec sigles corrigés (étape intermédiaire)',
    'transcript-cleaned':      'Transcription nettoyée',
    'transcript-reformulated': 'Synthèse narrative',
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
    } else if (s === 'kevent_reprocessing') {
        cls = 'active';
        label_text = `Régénération en cours…`;
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
    'kevent_reprocessing':         { label: 'Régénération en cours — chaîne LLM (glossaire → CR → synthèses)', polling: true },
    'mcr_pushed':                  { label: 'Poussé vers MCR', polling: false },
    'mcr_auth_failed':             { label: 'MCR : échec auth', polling: false },
    'mcr_rejected':                { label: 'MCR : rejeté', polling: false },
    'mcr_push_failed':             { label: 'MCR : échec push', polling: false },
    'disabled':                    { label: 'Transcription désactivée', polling: false },
};

// Étapes du pipeline IA — ordre d'exécution. Réutilisé pour construire la
// checklist de progression dans le tooltip du (i) et le modal Détails.
const PIPELINE_STEPS = [
    { key: 'transcript',              label: 'Transcription brute (Whisper)' },
    { key: 'transcript-tagged',       label: 'Identification des interlocuteurs' },
    { key: 'transcript-corrected',    label: 'Correction des sigles' },
    { key: 'transcript-cleaned',      label: 'Suppression des hésitations et redites' },
    { key: 'transcript-reformulated', label: 'Synthèse narrative' },
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
            'transcript':              { label: 'Transcription brute (Whisper)',                desc: 'Texte issu du Whisper (faster-whisper, gateway Mirai). Étape obligatoire pour toutes les autres.' },
            'transcript-tagged':       { label: 'Identification des interlocuteurs',            desc: 'Diarisation pyannote — sépare le texte par interlocuteur. Peut échouer sur les enregistrements très courts ou monolocuteurs.' },
            'transcript-corrected':    { label: 'Correction des sigles',                         desc: 'LLM relit le texte avec votre glossaire pour corriger les acronymes mal entendus (ex: "EHS" repassé en "EFS").' },
            'transcript-cleaned':      { label: 'Suppression des hésitations et redites',       desc: 'LLM retire les passages parasites du discours oral (faux départs, "euh", redites, bruits ambiants verbalisés).' },
            'transcript-reformulated': { label: 'Synthèse narrative',                            desc: 'LLM reformule au style indirect ("X explique que…") pour une lecture rapide.' },
            'meeting-cr':              { label: 'Compte-rendu structuré',                       desc: 'LLM produit l\'analyse 5 sections : acteurs, thématiques, décisions, gaps, recommandations.' },
        };
        let stepsDetails = '';
        // Diagnostic visible quand la transcription est terminée OU pendant
        // une régénération (on voit la chaîne LLM se rejouer étape par
        // étape sur les outputs existants — ils restent visibles pendant
        // le reprocess et sont écrasés à la fin).
        const showSteps = (status === 'kevent_completed'
                          || status === 'kevent_partially_completed'
                          || status === 'kevent_failed'
                          || status === 'kevent_reprocessing'
                          || status === 'completed' || status === 'failed');
        const isReprocessing = (status === 'kevent_reprocessing');
        if (showSteps) {
            const rows = Object.keys(STEP_INFO).map(k => {
                const info = STEP_INFO[k];
                const ok = !!outputs[k];
                let icon, color, labelSuffix = '';
                if (isReprocessing && k !== 'transcript' && k !== 'transcript-tagged') {
                    // En reprocess LLM : les 4 étapes glossary/cleaned/
                    // reformulated/meeting-cr sont rejouées. On les affiche
                    // explicitement "à refaire" plutôt que ✓ (les anciens
                    // outputs sont stales et vont être écrasés).
                    icon = '⏳';
                    color = '#1d4ed8';
                    labelSuffix = ' <small style="color:#1d4ed8;font-weight:600;">— en cours de régénération</small>';
                } else {
                    icon = ok ? '✓' : '✗';
                    color = ok ? '#10b981' : '#b91c1c';
                }
                const note = (!ok && k === 'transcript-tagged')
                    ? ' <small style="color:#94a3b8">(pyannote a peut-être eu un problème avec ce signal — voir logs côté admin)</small>'
                    : '';
                return `<div class="status-step">
                    <span style="color:${color};font-weight:700;">${icon}</span>
                    <span class="status-step-label">${escapeHtml(info.label)}${labelSuffix}</span>
                    <span class="status-step-desc">${escapeHtml(info.desc)}${note}</span>
                </div>`;
            }).join('');
            const summaryLabel = isReprocessing
                ? 'Voir la régénération en cours, étape par étape'
                : 'Voir le détail des étapes';
            stepsDetails = `<details class="status-details" ${isReprocessing ? 'open' : ''}>
                <summary>${summaryLabel}</summary>
                <div class="status-step-list">${rows}</div>
            </details>`;
        }
        // Badge regen count : si reprocess_version > 0, on affiche
        // "(version N · dernière régen 14:32)" en petit après le label.
        // Permet à l'utilisateur de voir d'un coup d'œil combien de fois
        // les CR ont été régénérés sur ce fichier (statistique utile).
        const rpv = data.reprocess_version || 0;
        let regenBadge = '';
        if (rpv > 0) {
            let when = '';
            if (data.last_reprocessed_at) {
                try {
                    const d = new Date(data.last_reprocessed_at);
                    when = ` · dernière ${d.toLocaleString('fr-FR', { day:'2-digit', month:'short', hour:'2-digit', minute:'2-digit' })}`;
                } catch (e) {}
            }
            regenBadge = ` <small style="color:#0c4498;background:#dbeafe;padding:0.1rem 0.4rem;border-radius:8px;font-weight:600;" title="Régénérations LLM effectuées sur ce fichier">↻ ${rpv}${when}</small>`;
        }
        const statusBadge = `<div class="transcript-status-line ${bannerClass}">
            ${leadIcon}
            <span class="transcript-status-spinner ${dotClass}"></span>
            <span class="transcript-status-label">${escapeHtml(meta.label)}${engine ? ` <small style="color:#94a3b8">(${escapeHtml(engine)})</small>` : ''}${regenBadge}</span>
        </div>${stepsDetails}`;

        // Fait pulser le bouton (i) "Détails techniques" tant qu'un
        // traitement est en cours (polling). Permet de signaler que la
        // fiche est en train de bouger sans rien lire d'autre.
        const isInProgressNow = !!(meta && meta.polling);
        const infoBtnEl = document.querySelector(`[data-file-info-btn="${CSS.escape(fileId)}"]`);
        if (infoBtnEl) {
            infoBtnEl.classList.toggle('file-detail-info-btn--pulse', isInProgressNow);
            infoBtnEl.setAttribute('title', isInProgressNow
                ? `Traitement en cours (${meta.label}) — cliquer pour voir les détails`
                : 'Détails techniques (statut, qualité, étapes IA, normalisation)');
        }

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
        // Ajoute un bouton ✏️ Modifier en bout de ligne qui ouvre la
        // modale d'édition du CR (markdown + corrector + drawer source).
        if (outputs['meeting-cr']) {
            const icons = CR_FORMATS.map((ext) =>
                fmtIconHtml(ext, `/api/file/meeting-cr/${ext}/${fileId}`)
            ).join('') + `<button type="button" class="downloads-cr-edit-btn"
                                   data-cr-edit="${escapeHtml(fileId)}"
                                   title="Modifier le compte-rendu (sélection→corriger, sources brutes)">✏️</button>`;
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

        // Le CR éditable est désormais exposé via une modale ouverte par
        // le bouton ✏️ "Modifier" sur la ligne "Compte-rendu structuré"
        // (cf dropdownBlock plus haut). On stocke `data` sur le container
        // pour que le click-handler global puisse y accéder sans re-fetch.
        container.innerHTML = `${statusBadge}${subtitle}${dropdownBlock}`;
        if (persistent && data) {
            container._mesreunionsCrData = data;
        }
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
                    // Icône-only (cf. _renderStatusTag) : la classe DSFR fr-icon-*
                    // dessine le picto, les couleurs viennent de --<kind>.
                    tag.className = `${info.icon} file-row-status-tag file-row-status-tag--${info.kind}`;
                    tag.setAttribute('data-status-kind', info.kind);
                    tag.setAttribute('aria-label', `Statut : ${info.label} — ${tooltip}`);
                    tag.title = `${info.label} — ${tooltip}`;
                    tag.textContent = '';
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
