// Onglet « Mes réunions (IA) » — module ES avec cycle de vie mount/unmount
// orchestré par lib/tab-manager.js.
//
// Ce module encapsule la logique d'interaction propre à l'onglet
// `panel-transfers` (alias historique du tab `data-tab="transfers"`,
// libellé public « Mes réunions (IA) »). Il s'appuie encore sur les
// implémentations historiques publiées par `frontend/legacy.js` sur
// `window` — ces fonctions restent partagées avec d'autres onglets
// (devices, corbeille, briefs) à travers les onclick="" du template
// et un état global (`_detailFileId`, `_devicesByQrToken`, polling
// queue-hint). Les sortir intégralement de legacy.js nécessiterait
// une refonte de l'état global qui dépasse le périmètre de cet onglet
// (cf. docs/refactor-mydevices-report-ux-meetings.md).
//
// Ce que ce module fait :
//   1. ré-exporte (comme avant) les fonctions globales legacy pour
//      permettre l'introspection IDE et préparer une future migration ;
//   2. expose `mount(container, ctx)` idempotent — appelé au 1er
//      affichage du panneau via tab-manager. Il :
//        – branche une délégation d'événements `[data-action]` locale
//          au panel pour les nouveaux boutons DSFR (sort-toggle,
//          purge, retour-liste, info-modal, etc.) afin de réduire la
//          surface d'onclick="" inline ;
//        – déclenche un `loadSessions({ force: true })` au 1er montage
//          puis sur demande (data-action="refresh-meetings") ;
//        – s'assure que la délégation d'inputs (file-detail-title-input,
//          .file-detail-meeting-datetime) reste documentée.
//   3. expose `unmount(container)` — stoppe les polls actifs propres
//      au tab (queue-hint) pour éviter les network calls inutiles
//      quand l'utilisateur quitte l'onglet.
//
// Sélecteurs DOM consommés (depuis index.html / panel-transfers) :
//   - #sessions-list          (cible du re-render loadSessions)
//   - #file-count             (compteur global)
//   - #sort-toggle-btn        (bouton tri date)
//   - #local-upload-progress  (barre upload local)
//   - #transfer-live          (live-tracker uploads in-flight)
//   - .recent-activities-panel.detail-active (mode page-détail)
//
// IMPORTANT : `legacy.js` doit être importé AVANT ce module (shell.js
// s'en charge dans le bon ordre). Sans ça, les `window.<fn>` ne sont
// pas encore publiés et les ré-exports valent undefined.

// ── Ré-exports historiques (compat onclick=""). ──────────────────────
// Surface publique consommée par les autres modules (preparations.js,
// useful-data.js) et par les onclick="" du template. À NE PAS retirer
// avant d'avoir déplacé tous les call-sites vers `data-action`.
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


// ── Cycle de vie ──────────────────────────────────────────────────────

// Délégation panel-locale pour les nouveaux boutons DSFR équipés
// d'un `data-action="..."`. Le handler est posé une seule fois au
// 1er mount() puis ré-utilisé tel quel (idempotent). On garde un
// flag séparé pour `mounted` afin de ne pas re-déclencher loadSessions
// après chaque switch de tab si rien n'a changé.
let _mounted = false;
let _delegationBound = false;
let _firstLoadDone = false;

function _resolveFn(name) {
  // Indirection paresseuse : si window.<fn> n'était pas encore défini
  // au moment de l'évaluation ES des `export const ... = window.<fn>`,
  // on tente une 2e lecture au moment du call.
  const fn = window[name];
  return typeof fn === 'function' ? fn : null;
}

function _onPanelAction(ev) {
  const el = ev.target && ev.target.closest && ev.target.closest('[data-action]');
  if (!el) return;
  // Ne réagit qu'aux actions du panel meetings (data-action préfixé
  // "meetings:" pour éviter les collisions avec d'autres onglets).
  const action = el.getAttribute('data-action') || '';
  if (!action.startsWith('meetings:')) return;
  const verb = action.slice('meetings:'.length);

  switch (verb) {
    case 'toggle-sort': {
      ev.preventDefault();
      const fn = _resolveFn('toggleSortDir');
      if (fn) fn();
      break;
    }
    case 'purge-all': {
      ev.preventDefault();
      const fn = _resolveFn('purgeSessions');
      if (fn) fn();
      break;
    }
    case 'back-to-list': {
      ev.preventDefault();
      const fn = _resolveFn('showFilesList');
      if (fn) fn();
      break;
    }
    case 'refresh': {
      ev.preventDefault();
      const fn = _resolveFn('loadSessions');
      if (fn) fn({ force: true });
      break;
    }
    case 'pick-files': {
      ev.preventDefault();
      const input = document.getElementById('local-upload-files-input');
      if (input) input.click();
      break;
    }
    case 'pick-folder': {
      ev.preventDefault();
      const input = document.getElementById('local-upload-folder-input');
      if (input) input.click();
      break;
    }
    case 'show-upload-help': {
      ev.preventDefault();
      const fn = _resolveFn('showUploadHelp');
      if (fn) fn();
      break;
    }
    default:
      // Action meetings:* inconnue — silencieux (un autre listener
      // pourrait l'attraper).
      break;
  }
}

export function mount(container /*, ctx */) {
  // container = #panel-transfers. Idempotent : appelable plusieurs fois
  // sans dupliquer les listeners ni rebloomer la liste.
  const panel = container || document.getElementById('panel-transfers');
  if (!panel) return;

  if (!_delegationBound) {
    panel.addEventListener('click', _onPanelAction);
    _delegationBound = true;
  }

  // Premier chargement de la liste : confié à legacy.js qui le fait
  // déjà au boot (`loadDevices().then(loadSessions)` en fin de fichier).
  // On force un refresh seulement si on remonte l'onglet après un
  // unmount (cas hypothétique d'un futur destroy/recreate).
  if (_mounted && _firstLoadDone) {
    const fn = _resolveFn('loadSessions');
    if (fn) fn({ force: true });
  }
  _mounted = true;
  _firstLoadDone = true;
}

export function unmount(/* container */) {
  // Stoppe le poll queue-hint quand on quitte le panel — il sera
  // relancé automatiquement par loadSessions au prochain rendu.
  const stop = _resolveFn('stopQueueHintDetail');
  if (stop) {
    try { stop(); } catch (e) { /* silencieux */ }
  }
  _mounted = false;
}

// Exposé pour debug + tests e2e.
if (typeof window !== 'undefined') {
  window.__meetingsTab = { mount, unmount };
}
