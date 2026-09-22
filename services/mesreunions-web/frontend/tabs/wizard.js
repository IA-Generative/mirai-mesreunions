// Wizard "Nouvelle préparation" — modale fullscreen DSFR avec stepper
// multi-étapes (chantier UX-Refonte-3 #3).
//
// Remplace la route HTML standalone /meeting-prep/new (supprimée — désormais
// redirect vers /?tab=brief&action=new). La modale est rendue inline dans
// l'app (menu visible derrière) ; le wizard reprend les mêmes champs que
// l'ancien _wizard_template.py mais distribués en 5 steps :
//   1. Identité  : type + sujet (+ durée prévue, déplacée ici pour cohérence)
//   2. Contexte  : rôle dans la réunion + attendu du brief
//   3. Sources   : dossiers/fichiers Drive, réunions passées, textes collés
//   4. Focus     : checkboxes "Sur quoi concentrer l'analyse ?"
//   5. Récap     : récapitulatif + bouton Générer
//
// API : POST /api/preparations (même payload que le wizard historique).
// Deep-link compat : /meeting-prep/new redirige vers /?tab=brief&action=new
//   → preparations.js détecte ?action=new au boot et appelle openWizard().

import {
  createParticipantRow,
  serializeParticipantsContainer,
  listInvalidEmails,
} from '../lib/participants.js';
import {
  buildRuleFromForm,
  refreshFreqVisibility,
  summarizeRule,
} from '../lib/rrule-builder.js';
import {
  mountThemesChips,
  serializeThemesChips,
  loadThemesSuggestions,
} from '../lib/themes-chips.js';
import {
  mountSourceBasket,
  getSourceBasketEntries,
  setSourceBasketEntries,
  addSourceBasketEntries,
  toApiSources,
  snapshotEntries,
} from '../lib/source-basket.js';
import { openSourcePicker } from '../lib/source-pickers.js';
import { isStackedModalOpen } from '../lib/stacked-modal.js';
import { showToast } from '../lib/toast.js';

// Le wizard reste à CINQ étapes. En ajouter une aurait deux conséquences
// qu'aucun gain d'ergonomie ne compense : `_COACH_BY_STEP` est indexé par
// numéro d'étape (les suggestions atterriraient sur le mauvais écran), et
// surtout `_applySnapshot` restaure `snap.step` comme un entier brut, sans
// champ de version — tous les brouillons déjà en localStorage rouvriraient
// donc sur un écran décalé. L'étape 3 change seulement de nature : de
// « Documents » (un champ Drive) à « Sources » (quatre pickers + panier).
const STEP_IDS = ['identite', 'contexte', 'documents', 'focus', 'recap'];
const STEP_LABELS = [
  'Identité', 'Contexte', 'Sources', 'Focus', 'Récap',
];

let _currentStep = 0;
let _wizardOpenedOnce = false;
// Identifiant opaque fourni par l'application tierce qui a ouvert le lien.
// Transmis au brief pour que l'appelant puisse retrouver ce qu'il a déclenché.
let _externalRef = '';

// Vrai dès qu'on a déjà prévenu l'utilisateur pour l'ouverture en cours —
// le save est débouncé à 400 ms, sans ce drapeau le toast se répéterait à
// chaque frappe.
let _draftQuotaWarned = false;

function _qs(sel, root) { return (root || document).querySelector(sel); }
function _qsa(sel, root) { return Array.from((root || document).querySelectorAll(sel)); }

function _esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function _statusBox() { return _qs('#wizard-status'); }

// ── Panier de sources (étape 3) ─────────────────────────────────────────
function _basketEl() { return _qs('#wizard-sources-basket'); }

/**
 * Recopie dans le champ historique `#wizard-drive-folder` (devenu un hidden)
 * le premier dossier sans instance explicite, et lui seul.
 *
 * Pourquoi « sans instance » : le serveur replace `drive_folder` en tête de
 * `sources[]` puis déduplique sur le triplet (drive, host, id). Le champ
 * historique ne porte pas d'instance ; une entrée qui en porte une ne se
 * dédupliquerait donc pas contre lui, et le dossier serait ingéré deux fois.
 * Le picker Drive ne stampille une instance que lorsqu'il y a un choix à
 * faire — dans le cas courant (un seul Drive), un dossier choisi en naviguant
 * alimente donc bien ce champ, exactement comme un dossier collé.
 */
function _syncDriveHiddenInput(entries) {
  const el = _qs('#wizard-drive-folder');
  if (!el) return;
  const legacy = (entries || []).find(
    (e) => e && e.type === 'drive_folder' && !e.drive && !e.host,
  );
  el.value = legacy ? legacy.id : '';
}

function _onBasketChange(entries) {
  _syncDriveHiddenInput(entries);
  // Les pickers vivent dans `document.body`, donc hors du nœud sur lequel
  // `_bindAutosave` délègue (`#wizard-modal-backdrop`) : sans ce rappel
  // explicite, rien de ce que l'utilisateur choisit ne survivrait à une
  // fermeture du wizard.
  _scheduleSave();
}

function _sourceEntries() {
  return getSourceBasketEntries(_basketEl());
}

function _setStatus(msg, kind) {
  const el = _statusBox();
  if (!el) return;
  el.textContent = msg || '';
  el.className = 'wizard-status ' + (kind || 'info');
}
function _clearStatus() {
  const el = _statusBox();
  if (!el) return;
  el.className = 'wizard-status';
  el.textContent = '';
}

// ── Stepper ─────────────────────────────────────────────────────────────
function _renderStepper() {
  const root = _qs('#wizard-stepper');
  if (!root) return;
  root.innerHTML = STEP_LABELS.map((label, idx) => {
    let cls = 'wizard-stepper-item';
    if (idx === _currentStep) cls += ' is-current';
    else if (idx < _currentStep) cls += ' is-done';
    return `<span class="${cls}">${idx + 1}. ${_esc(label)}</span>`;
  }).join('');
}

function _showStep(idx) {
  if (idx < 0 || idx >= STEP_IDS.length) return;
  _currentStep = idx;
  STEP_IDS.forEach((sid, i) => {
    const el = _qs(`#wizard-step-${sid}`);
    if (el) el.classList.toggle('is-active', i === idx);
  });
  _renderStepper();
  // Refresh recap on last step entry.
  if (idx === STEP_IDS.length - 1) _renderRecap();
  _renderCoach();
  // Boutons précédent / suivant / valider
  const prevBtn = _qs('#wizard-prev-btn');
  const nextBtn = _qs('#wizard-next-btn');
  const submitBtn = _qs('#wizard-submit-btn');
  if (prevBtn) prevBtn.style.visibility = (idx === 0) ? 'hidden' : '';
  if (nextBtn) nextBtn.style.display = (idx === STEP_IDS.length - 1) ? 'none' : '';
  if (submitBtn) submitBtn.style.display = (idx === STEP_IDS.length - 1) ? '' : 'none';
}

function _validateCurrentStep() {
  _clearStatus();
  if (_currentStep === 0) {
    const subject = (_qs('#wizard-subject') || {}).value || '';
    const duration = _parseDurationMinutes((_qs('#wizard-duration') || {}).value);
    if (!subject.trim()) { _setStatus('Le sujet est obligatoire.', 'err'); return false; }
    if (!duration) { _setStatus('Durée non reconnue (ex : « 1h », « 45 minutes »).', 'err'); return false; }
  } else if (_currentStep === 1) {
    const role = (_qs('#wizard-role') || {}).value || '';
    const exp = (_qs('#wizard-expectation') || {}).value || '';
    if (!role.trim()) { _setStatus('Votre rôle est obligatoire.', 'err'); return false; }
    if (!exp.trim()) { _setStatus("L'attendu du brief est obligatoire.", 'err'); return false; }
  }
  return true;
}

function _next() {
  if (!_validateCurrentStep()) return;
  _showStep(Math.min(_currentStep + 1, STEP_IDS.length - 1));
}
function _prev() {
  _showStep(Math.max(_currentStep - 1, 0));
}

// ── Parsing durée (repris de _wizard_template.py) ───────────────────────
function _parseDurationMinutes(raw) {
  if (!raw) return null;
  const s = String(raw).trim().toLowerCase();
  const inp = _qs('#wizard-duration');
  const explicit = inp && inp.dataset.minutes;
  if (explicit) {
    const n = parseInt(explicit, 10);
    if (!isNaN(n) && n > 0) return n;
  }
  if (s === 'demi-journée') return 240;
  if (s === 'journée complète' || s === 'journée') return 480;
  let m;
  m = s.match(/^(\d+)\s*h\s*(\d+)?$/);
  if (m) return parseInt(m[1], 10) * 60 + (m[2] ? parseInt(m[2], 10) : 0);
  m = s.match(/^(\d+)\s*(heure|heures|h)$/);
  if (m) return parseInt(m[1], 10) * 60;
  m = s.match(/^(\d+)\s*(minute|minutes|min|m)?$/);
  if (m) return parseInt(m[1], 10);
  return null;
}

// ── Coach réunion — suggestions humbles, ancrées dans la recherche ─────
// Une seule suggestion à la fois, jamais bloquante, toujours masquable.
// Les sources sont citées en petit : c'est la recherche qui parle, pas nous.
const _COACH_TIPS = [
  {
    id: 'outcome_empty',
    when: (s) => !s.outcomes.length && !s.successCriteria,
    text: 'Une pratique qui aide souvent : décrire ce qui devra être vrai à la fin '
      + '(« le budget est arbitré », « chacun connaît sa prochaine action »). '
      + 'Et si aucune décision ni question à trancher n\'émerge, un échange écrit peut parfois suffire.',
    source: 'S. Rogelberg, The Surprising Science of Meetings',
  },
  {
    id: 'agenda_questions',
    when: (s) => s.outcomes.includes('decision') || s.outcomes.includes('actions'),
    text: 'Si c\'est utile : un ordre du jour formulé en questions à trancher, plutôt qu\'en thèmes, '
      + 'clarifie qui doit vraiment être présent. Le brief généré proposera ces questions.',
    source: 'S. Rogelberg — un agenda n\'aide que si sa formulation engage',
  },
  {
    id: 'many_participants',
    when: (s) => s.participantsCount > 8,
    text: 'Au-delà de 8 participants, la participation de chacun chute nettement (mesuré à grande '
      + 'échelle). Peut-être inviter au strict nécessaire — le compte-rendu informera les autres ?',
    source: 'Étude Microsoft 2021 (efficacité & inclusion des réunions)',
  },
  {
    id: 'long_break',
    when: (s) => s.duration >= 90,
    text: 'Au-delà d\'une heure, une pause de 5-10 minutes améliore réellement l\'attention de tous '
      + '(mesuré par EEG). Peut-être la prévoir dans l\'ordre du jour ?',
    source: 'Microsoft Human Factors Lab, 2021',
  },
  {
    id: 'send_before',
    when: () => true,
    text: 'Le geste le plus rentable après la génération : envoyer ce brief aux participants '
      + '24-48h avant la réunion. La lecture préalable est l\'un des facteurs d\'efficacité '
      + 'les mieux documentés.',
    source: 'Cambridge Handbook of Meeting Science (pré-communication)',
  },
];
// Ordre d'affichage par écran : contexte = intention d'abord ; récap =
// signaux de risque d'abord, sinon le conseil « envoyer avant ».
const _COACH_BY_STEP = {
  1: { container: 'wizard-coach-tip', tips: ['outcome_empty', 'agenda_questions', 'many_participants', 'long_break'] },
  4: { container: 'wizard-coach-tip-recap', tips: ['many_participants', 'long_break', 'send_before'] },
};
let _coachDismissed = new Set();

function _coachState() {
  const list = _qs('#wizard-participants-list');
  return {
    outcomes: _qsa('#wizard-outcome-chips .wizard-chip.is-on').map((c) => c.dataset.outcome),
    successCriteria: ((_qs('#wizard-success-criteria') || {}).value || '').trim(),
    duration: _parseDurationMinutes((_qs('#wizard-duration') || {}).value) || 0,
    participantsCount: list ? list.children.length : 0,
  };
}

function _renderCoach() {
  Object.values(_COACH_BY_STEP).forEach((cfg) => {
    const el = document.getElementById(cfg.container);
    if (el) { el.classList.remove('is-visible'); el.innerHTML = ''; }
  });
  const cfg = _COACH_BY_STEP[_currentStep];
  if (!cfg) return;
  const el = document.getElementById(cfg.container);
  if (!el) return;
  const state = _coachState();
  const tip = cfg.tips
    .map((id) => _COACH_TIPS.find((t) => t.id === id))
    .find((t) => t && !_coachDismissed.has(t.id) && t.when(state));
  if (!tip) return;
  el.innerHTML = `
    <span aria-hidden="true">💡</span>
    <span class="coach-text">Suggestion — ${_esc(tip.text)}
      <span class="coach-source">${_esc(tip.source)}</span></span>
    <button type="button" class="coach-dismiss" data-coach-dismiss="${_esc(tip.id)}">Masquer</button>`;
  el.classList.add('is-visible');
  const btn = el.querySelector('[data-coach-dismiss]');
  if (btn) {
    btn.addEventListener('click', () => {
      _coachDismissed.add(tip.id);
      _renderCoach();
    });
  }
}

function _bindOutcomeChips() {
  const group = _qs('#wizard-outcome-chips');
  if (!group) return;
  _qsa('.wizard-chip', group).forEach((chip) => {
    chip.addEventListener('click', () => {
      chip.classList.toggle('is-on');
      _renderCoach();
    });
  });
}

// ── Récap ───────────────────────────────────────────────────────────────
function _collectValues() {
  const meetingType = (_qs('#wizard-meeting-type') || {}).value || 'general';
  const subject = ((_qs('#wizard-subject') || {}).value || '').trim();
  const role = ((_qs('#wizard-role') || {}).value || '').trim();
  const expectation = ((_qs('#wizard-expectation') || {}).value || '').trim();
  const drive = ((_qs('#wizard-drive-folder') || {}).value || '').trim();
  const duration = _parseDurationMinutes((_qs('#wizard-duration') || {}).value);
  const focus = _qsa('input[name="wizard-focus"]:checked').map(el => el.value);
  const participants = serializeParticipantsContainer(_qs('#wizard-participants-list'));
  // Lot 6 — récurrence (optionnelle). Le toggle pilote la prise en compte.
  const recurringToggle = _qs('#wizard-recurring-toggle');
  const isRecurring = !!(recurringToggle && recurringToggle.checked);
  const ruleContainer = _qs('#wizard-recurrence-form');
  const recurrenceRule = (isRecurring && ruleContainer) ? buildRuleFromForm(ruleContainer) : null;
  // Lot 9 — thématiques additionnelles (chips libres) + Lot 8 — toggle CR.
  const themesContainer = _qs('#wizard-themes-container');
  const themes = themesContainer ? serializeThemesChips(themesContainer) : [];
  const sendCrEl = _qs('#wizard-send-cr-email');
  const sendCrEmail = !!(sendCrEl && sendCrEl.checked);
  // Coaching — intention de fin de réunion (chips + texte libre).
  const expectedOutcomes = _qsa('#wizard-outcome-chips .wizard-chip.is-on')
    .map((c) => c.dataset.outcome).filter(Boolean);
  const successCriteria = ((_qs('#wizard-success-criteria') || {}).value || '').trim();
  // Étape 3 — panier de sources. `drive` reste renseigné en parallèle pour
  // le chemin historique (cf. _syncDriveHiddenInput).
  const sources = _sourceEntries();
  return {
    meetingType, subject, role, expectation, drive, duration, focus, participants,
    sources,
    isRecurring: isRecurring && !!recurrenceRule,
    recurrenceRule: isRecurring ? recurrenceRule : null,
    themes,
    sendCrEmail,
    expectedOutcomes,
    successCriteria,
  };
}

const _OUTCOME_RECAP_LABELS = {
  decision: 'une décision prise',
  actions: 'un plan d\'action daté',
  alignement: 'un alignement partagé',
  idees: 'des idées nouvelles',
  information: 'une information transmise',
};

function _renderRecap() {
  const root = _qs('#wizard-recap-content');
  if (!root) return;
  const v = _collectValues();
  root.innerHTML = `
    <dl>
      <dt>Type :</dt><dd>${_esc(v.meetingType)}</dd>
      <dt>Sujet :</dt><dd>${_esc(v.subject) || '<em>—</em>'}</dd>
      <dt>Durée :</dt><dd>${v.duration ? v.duration + ' min' : '<em>—</em>'}</dd>
      <dt>Votre rôle :</dt><dd>${_esc(v.role) || '<em>—</em>'}</dd>
      <dt>Attendu :</dt><dd>${_esc(v.expectation) || '<em>—</em>'}</dd>
      <dt>Résultat espéré :</dt><dd>${
        (v.expectedOutcomes.length || v.successCriteria)
          ? _esc([
              ...v.expectedOutcomes.map((o) => _OUTCOME_RECAP_LABELS[o] || o),
              v.successCriteria,
            ].filter(Boolean).join(' ; '))
          : '<em>(non précisé)</em>'
      }</dd>
      <dt>Sources :</dt><dd>${
        v.sources.length
          ? '<ul style="margin:0;padding-left:1.1rem;">'
            + v.sources.map((s) => (
              `<li>${_esc(s.label)} <span style="color:#94a3b8;font-size:0.8em;">`
              + `${_esc(s.meta || '')}</span></li>`
            )).join('')
            + '</ul>'
          : '<em>(aucune)</em>'
      }</dd>
      <dt>Dossier Drive :</dt><dd>${_esc(v.drive) || '<em>(aucun)</em>'}</dd>
      <dt>Focus :</dt><dd>${v.focus.length ? v.focus.map(_esc).join(', ') : '<em>(aucun)</em>'}</dd>
      <dt>Participants :</dt><dd>${
        v.participants.length
          ? v.participants.map(p => _esc(p.name || p.email)).join(', ')
          : '<em>(aucun)</em>'
      }</dd>
      <dt>Récurrence :</dt><dd>${
        v.isRecurring && v.recurrenceRule
          ? _esc(summarizeRule(v.recurrenceRule))
          : '<em>(ponctuelle)</em>'
      }</dd>
      <dt>Thématiques :</dt><dd>${
        v.themes && v.themes.length
          ? v.themes.map(_esc).join(', ')
          : '<em>(aucune)</em>'
      }</dd>
      <dt>Envoi CR auto :</dt><dd>${
        v.sendCrEmail ? 'Oui (aux participants avec email)' : '<em>Non</em>'
      }</dd>
    </dl>`;
}

// ── Stepper animé pendant la génération (Lot 2) ──────────────────────────
// Phases techniques mappées vers libellés UX. L'ordre détermine l'animation
// (chaque phase passée s'affiche ✓, la phase courante ⏳, les suivantes ○).
const _GEN_PHASES = [
  { key: 'test_drive',          label: 'Connexion au Drive' },
  { key: 'listing_docs',        label: 'Listing des documents' },
  { key: 'reading_doc',         label: 'Lecture des documents' },
  { key: 'generating_llm',      label: 'Génération du brief par l\'IA' },
  { key: 'persisting',          label: 'Sauvegarde du brief' },
  { key: 'extracting_glossary', label: 'Extraction du glossaire' },
  { key: 'done',                label: 'Terminé' },
];
const _PHASES_NO_DRIVE = new Set(['test_drive', 'listing_docs', 'reading_doc']);

function _phaseIndex(phase) {
  for (let i = 0; i < _GEN_PHASES.length; i++) {
    if (_GEN_PHASES[i].key === phase) return i;
  }
  // 'init' / 'queued' = avant la première phase visible
  if (phase === 'init' || phase === 'queued') return -1;
  return -1;
}

function _renderGenerationStepper(state, opts) {
  const root = _qs('#wizard-generation-progress');
  if (!root) return;
  root.style.display = '';
  const hasDrive = !!(opts && opts.hasDrive);
  const phase = (state && state.phase) || 'queued';
  const idx = _phaseIndex(phase);
  const isFailed = phase === 'failed';
  const phases = _GEN_PHASES.filter(p => hasDrive || !_PHASES_NO_DRIVE.has(p.key));

  let html = '<ol class="wizard-gen-stepper" style="list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:0.4rem;">';
  phases.forEach((p) => {
    const phaseGlobalIdx = _phaseIndex(p.key);
    let icon = '○';
    let cls = 'todo';
    let style = 'color:#94a3b8;';
    if (isFailed && phaseGlobalIdx === idx) {
      icon = '✕'; cls = 'failed'; style = 'color:#b91c1c;font-weight:600;';
    } else if (phaseGlobalIdx < idx) {
      icon = '✓'; cls = 'done'; style = 'color:#10b981;';
    } else if (phaseGlobalIdx === idx) {
      icon = '⏳'; cls = 'current'; style = 'color:#0066cc;font-weight:600;';
    }
    let extra = '';
    if (p.key === 'reading_doc' && phaseGlobalIdx === idx) {
      const proc = state.docs_processed || 0;
      const tot = state.docs_total || 0;
      const cur = state.current_doc ? ' — ' + _esc(state.current_doc) : '';
      extra = tot
        ? ` <span style="color:#64748b;font-size:0.85em;">(${proc}/${tot}${_esc(cur)})</span>`
        : '';
    }
    html += `<li class="wizard-gen-step is-${cls}" style="${style}">${icon} ${_esc(p.label)}${extra}</li>`;
  });
  html += '</ol>';
  if (isFailed && state.error) {
    html += `<div class="fr-alert fr-alert--error fr-alert--sm" style="margin-top:0.5rem;">
      <p>${_esc(state.error)}</p></div>`;
  }
  root.innerHTML = html;
}

function _hideGenerationStepper() {
  const root = _qs('#wizard-generation-progress');
  if (root) { root.style.display = 'none'; root.innerHTML = ''; }
}

let _pollTimer = null;
let _pollStartedAt = 0;
// Borne dure du polling : au-delà, le job est perdu (le serveur requalifie
// les jobs orphelins à 30 min — on lui laisse une marge). Sans borne, un
// pod redémarré en pleine génération laissait l'animation tourner sans fin.
const _POLL_MAX_MS = 35 * 60 * 1000;

function _stopPolling() {
  if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }
}

function _failGeneration(message) {
  // Échec : on garde le brouillon (les réponses de l'utilisateur), on
  // réactive le bouton, on affiche la cause.
  _stopPolling();
  _setStatus(message || 'La génération a échoué.', 'err');
  const submitBtn = _qs('#wizard-submit-btn');
  if (submitBtn) submitBtn.disabled = false;
}

async function _pollJob(jobId, opts) {
  if (_pollStartedAt && (Date.now() - _pollStartedAt) > _POLL_MAX_MS) {
    _failGeneration('La génération a été interrompue (délai dépassé). '
      + 'Vos réponses sont conservées — relancez la génération.');
    return;
  }
  try {
    const r = await fetch(`/api/preparations/jobs/${encodeURIComponent(jobId)}`);
    if (r.status === 404) {
      _failGeneration('Job de génération introuvable (expiré ?). Relancez la génération.');
      return;
    }
    if (r.redirected || ((r.headers.get('content-type') || '').indexOf('json') === -1)) {
      // Session OIDC expirée : @require_auth renvoie une redirection HTML
      // vers /login — sans ce garde, r.json() échouait et on repartait en
      // boucle silencieuse pour toujours.
      _failGeneration('Votre session a expiré. Reconnectez-vous puis relancez la génération '
        + '(vos réponses sont conservées dans le brouillon).');
      return;
    }
    const d = await r.json().catch(() => ({}));
    _renderGenerationStepper(d, opts);
    if (d.phase === 'done') {
      const newId = d.preparation_id;
      if (!newId) {
        // Défense en profondeur : un done sans preparation_id est un échec
        // de sauvegarde — ne surtout pas jeter le brouillon.
        _renderGenerationStepper({ phase: 'failed', error: 'Sauvegarde du brief incomplète.' }, opts);
        _failGeneration('Le brief n\'a pas pu être sauvegardé. '
          + 'Vos réponses sont conservées — relancez la génération.');
        return;
      }
      _stopPolling();
      _setStatus('Brief généré.', 'info');
      // Brouillon validé -> retire de la liste localStorage.
      if (_currentDraftId) { try { deleteDraft(_currentDraftId); } catch (e) {} }
      closeWizard();
      try {
        if (typeof window.loadBriefs === 'function') window.loadBriefs();
        if (typeof window.showBriefDetail === 'function') {
          setTimeout(() => window.showBriefDetail(newId), 100);
        }
      } catch (e) { /* non-fatal */ }
      return;
    }
    if (d.phase === 'failed') {
      _failGeneration(d.error || 'La génération a échoué.');
      return;
    }
    _pollTimer = setTimeout(() => _pollJob(jobId, opts), 1500);
  } catch (err) {
    // Erreur réseau ponctuelle — on retente plus tard, sans interrompre.
    _pollTimer = setTimeout(() => _pollJob(jobId, opts), 3000);
  }
}

// ── Submit POST /api/preparations ───────────────────────────────────────
async function _submit(ev) {
  if (ev) ev.preventDefault();
  // Le wizard est un <form> qui contient un bouton submit : taper Entrée dans
  // n'importe quel champ texte déclenchait la soumission implicite HTML, donc
  // une génération depuis l'étape 1, validée sur les seules étapes 0 et 1.
  if (_currentStep !== STEP_IDS.length - 1) return;
  _clearStatus();
  _hideGenerationStepper();
  // Validation finale : on rejoue les checks des steps 0+1 (les autres ne
  // sont pas obligatoires).
  if (!_validateStepIdx(0) || !_validateStepIdx(1)) return;
  const v = _collectValues();

  // series_parent_id éventuel — lu depuis la query string ou ?series_parent_id
  // posé en hidden input par openWizard().
  const seriesParent = (_qs('#wizard-series-parent') || {}).value || '';
  const targetDate = (_qs('#wizard-target-date') || {}).value || '';

  // Validation emails participants (Lot 5).
  const partsContainer = _qs('#wizard-participants-list');
  const invalidEmails = listInvalidEmails(partsContainer);
  if (invalidEmails.length) {
    _setStatus('Email participant invalide : ' + invalidEmails[0], 'err');
    return;
  }

  const body = {
    subject: v.subject,
    drive_folder: v.drive,
    role: v.role,
    expectation: v.expectation,
    duration_minutes: v.duration,
    focus: v.focus,
    meeting_type: v.meetingType,
  };
  if (seriesParent) body.series_parent_id = seriesParent;
  if (targetDate) body.target_meeting_date = targetDate;
  if (v.participants && v.participants.length) body.participants = v.participants;
  // Lot 6 — récurrence (n'envoie que si toggle on + règle valide).
  if (v.isRecurring && v.recurrenceRule) {
    body.is_recurring = true;
    body.recurrence_rule = v.recurrenceRule;
  }
  // Lot 9 — thématiques + Lot 8 — toggle CR auto.
  if (v.themes && v.themes.length) body.themes = v.themes;
  if (v.sendCrEmail) body.send_cr_email = true;
  // Coaching — intention de fin de réunion.
  if (v.expectedOutcomes.length) body.expected_outcomes = v.expectedOutcomes;
  if (v.successCriteria) body.success_criteria = v.successCriteria;
  if (_externalRef) body.external_ref = _externalRef;
  // Étape 3 — sources combinées. `toApiSources` regroupe les fichiers Drive
  // d'une même instance et retire toutes les clés d'affichage : le serveur ne
  // reçoit que le contrat de `modules/preparations/sources.py`.
  const apiSources = toApiSources(v.sources);
  if (apiSources.length) body.sources = apiSources;

  // Le stepper de génération masque les phases Drive quand aucune source ne
  // les déclenche — un « Connexion au Drive » qui ne viendra jamais fait
  // croire à un blocage.
  const hasDrive = !!v.drive || apiSources.some(
    (s) => s.type === 'drive_folder' || s.type === 'drive_files',
  );

  const submitBtn = _qs('#wizard-submit-btn');
  if (submitBtn) submitBtn.disabled = true;
  _setStatus(hasDrive
    ? 'Lecture du Drive et génération du brief en cours…'
    : 'Génération du brief en cours…', 'info');
  // Affiche tout de suite le stepper en état initial pour feedback immédiat.
  _renderGenerationStepper({ phase: 'init' }, { hasDrive });

  try {
    const r = await fetch('/api/preparations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      _setStatus(d.error || ('Erreur ' + r.status), 'err');
      _hideGenerationStepper();
      if (submitBtn) submitBtn.disabled = false;
      return;
    }
    // Mode async (Lot 2) : back renvoie {job_id} en 202.
    if (d.job_id) {
      _pollStartedAt = Date.now();
      _pollJob(d.job_id, { hasDrive });
      return;
    }
    // Fallback : ancienne réponse sync (compat).
    _setStatus('Brief généré.', 'info');
    closeWizard();
    try {
      if (typeof window.loadBriefs === 'function') window.loadBriefs();
      const newId = (d && (d.id || d.preparation_id || (d.preparation && d.preparation.id))) || null;
      if (newId && typeof window.showBriefDetail === 'function') {
        setTimeout(() => window.showBriefDetail(newId), 100);
      }
    } catch (e) { /* non-fatal */ }
    if (submitBtn) submitBtn.disabled = false;
  } catch (err) {
    _setStatus('Erreur réseau : ' + (err && err.message ? err.message : err), 'err');
    _hideGenerationStepper();
    if (submitBtn) submitBtn.disabled = false;
  }
}

// Le test d'accès Drive (ancien `_testDriveAccess`) a déménagé dans
// `lib/source-pickers.js` avec le markup qu'il pilotait : le champ d'URL
// vit désormais dans la modale « Coller un lien ». Il y est paramétré par
// ses éléments (`testDriveAccess({input, result, spinner, btn})`) — le
// wizard ne peut pas l'importer depuis les pickers ET l'inverse sans
// créer un cycle d'import.

// Validation step idx (sans afficher / déplacer l'utilisateur).
function _validateStepIdx(idx) {
  const prev = _currentStep;
  _currentStep = idx;
  const ok = _validateCurrentStep();
  _currentStep = prev;
  // Si invalid, on remet l'utilisateur sur le step fautif.
  if (!ok) _showStep(idx);
  return ok;
}

// ── Chips (suggestions cliquables qui remplissent l'input) ──────────────
function _bindChips(root) {
  _qsa('.wizard-chips', root).forEach((group) => {
    const targetId = group.getAttribute('data-target');
    const target = document.getElementById(targetId);
    if (!target) return;
    _qsa('.wizard-chip', group).forEach((chip) => {
      chip.addEventListener('click', () => {
        target.value = (chip.textContent || '').trim();
        if (chip.dataset.minutes) {
          target.dataset.minutes = chip.dataset.minutes;
        } else {
          delete target.dataset.minutes;
        }
        target.focus();
      });
    });
  });
}

// ── Ouvre / ferme la modale ────────────────────────────────────────────
export function openWizard(opts) {
  opts = opts || {};
  const backdrop = _qs('#wizard-modal-backdrop');
  if (!backdrop) return;
  backdrop.classList.add('is-open');
  _wizardOpenedOnce = true;
  // Reset state
  _currentStep = 0;
  _clearStatus();
  // Draft tracking : reprise d'un brouillon (depuis la liste) ou nouveau.
  // Stocké tôt pour que _scheduleSave fire dès le 1er keystroke.
  _currentDraftId = opts.draftId || _newDraftId();
  // Optionnel : series_parent_id depuis l'URL ou opts
  const seriesEl = _qs('#wizard-series-parent');
  const params = new URLSearchParams(window.location.search || '');
  const sp = opts.seriesParentId || params.get('series_parent_id') || '';
  if (seriesEl) seriesEl.value = sp;
  // Lot 3 — target_meeting_date pré-rempli depuis la modale "Préparer la prochaine".
  const targetEl = _qs('#wizard-target-date');
  if (targetEl) targetEl.value = opts.targetMeetingDate || '';
  // Lot 5 — reset liste participants à chaque ouverture.
  const partsList = _qs('#wizard-participants-list');
  if (partsList) partsList.innerHTML = '';
  // Lot 6 — reset récurrence (toggle off + form replié).
  const recToggle = _qs('#wizard-recurring-toggle');
  if (recToggle) recToggle.checked = false;
  const recForm = _qs('#wizard-recurrence-form');
  if (recForm) recForm.style.display = 'none';
  // Lot 9 — monte le composant chips thématiques (avec suggestions async).
  const themesContainer = _qs('#wizard-themes-container');
  if (themesContainer) {
    mountThemesChips(themesContainer, { initial: [], suggestions: [] });
    loadThemesSuggestions().then((items) => {
      if (themesContainer._themesChipsSetSuggestions) {
        themesContainer._themesChipsSetSuggestions(items);
      }
    }).catch(() => {});
  }
  // Étape 3 — panier de sources. Monté AVANT `_applySnapshot` : la
  // restauration d'un brouillon repose sur `_sourceBasketSet`, posé par le
  // montage. Monté après, elle échouerait en silence et le panier serait vide.
  const basket = _basketEl();
  if (basket) {
    mountSourceBasket(basket, { onChange: _onBasketChange });
    _syncDriveHiddenInput([]);
  }
  _draftQuotaWarned = false;
  // Lot 8 — reset toggle CR.
  const sendCr = _qs('#wizard-send-cr-email');
  if (sendCr) sendCr.checked = false;
  // Coaching — reset chips d'intention + suggestions masquées.
  _qsa('#wizard-outcome-chips .wizard-chip.is-on').forEach((c) => c.classList.remove('is-on'));
  _coachDismissed = new Set();
  const banner = _qs('#wizard-series-banner');
  if (banner) banner.style.display = sp ? '' : 'none';
  if (sp) {
    // Fetch parent title best-effort
    fetch(`/api/preparations/${encodeURIComponent(sp)}`)
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        if (!d) return;
        const b = (d.preparation || d.brief) || {};
        const t = _qs('#wizard-series-banner-title');
        if (t) t.textContent = b.title || b.subject || '(brief parent)';
        // Pré-remplissage
        ['subject', 'role', 'expectation'].forEach(k => {
          const inp = _qs('#wizard-' + (k === 'subject' ? 'subject' : k));
          if (inp && !inp.value && b[k]) inp.value = b[k];
        });
        const mt = _qs('#wizard-meeting-type');
        if (mt && b.meeting_type) {
          for (const o of mt.options) {
            if (o.value === b.meeting_type) { mt.value = b.meeting_type; break; }
          }
        }
      })
      .catch(() => { /* non-fatal */ });
  }
  _showStep(0);
  // Restaure le snapshot si on reprend un brouillon (après _showStep
  // pour que les conditionnels d'affichage step soient déjà appliqués).
  if (opts.restoreSnapshot) {
    try { _applySnapshot(opts.restoreSnapshot); } catch (e) {}
  }
  // Focus le 1er input
  setTimeout(() => {
    const first = _qs('#wizard-subject');
    if (first) first.focus();
  }, 50);
  // Verrouille le scroll body
  document.body.style.overflow = 'hidden';
}

export function closeWizard() {
  const backdrop = _qs('#wizard-modal-backdrop');
  if (!backdrop) return;
  backdrop.classList.remove('is-open');
  document.body.style.overflow = '';
  _stopPolling();
  _hideGenerationStepper();
  // Flush save final + clear pointer (le draft reste en localStorage
  // si non terminé — on le retrouvera dans la liste).
  if (_saveTimer) { clearTimeout(_saveTimer); _saveTimer = null; }
  if (_currentDraftId) {
    try { _saveDraft(_currentDraftId, _collectSnapshot()); } catch (e) {}
  }
  _currentDraftId = null;
  // Le panier est vidé APRÈS la sauvegarde ci-dessus, sinon on persisterait
  // un brouillon amputé de ses sources.
  const basket = _basketEl();
  if (basket && basket._sourceBasketState) {
    basket._sourceBasketState.entries = [];
    if (basket._sourceBasketRender) basket._sourceBasketRender();
  }
  _syncDriveHiddenInput([]);
  // Nettoie ?action=new de l'URL pour éviter de réouvrir si reload.
  try {
    const url = new URL(window.location.href);
    if (url.searchParams.get('action') === 'new') {
      url.searchParams.delete('action');
      window.history.replaceState({}, '', url.toString());
    }
  } catch (e) { /* tolérant */ }
}

// ── Init ────────────────────────────────────────────────────────────────
function _bindEvents() {
  const backdrop = _qs('#wizard-modal-backdrop');
  if (!backdrop) return;
  const closeBtn = _qs('#wizard-close-btn');
  const cancelBtn = _qs('#wizard-cancel-btn');
  const prevBtn = _qs('#wizard-prev-btn');
  const nextBtn = _qs('#wizard-next-btn');
  const submitBtn = _qs('#wizard-submit-btn');
  const form = _qs('#wizard-form');
  if (closeBtn) closeBtn.addEventListener('click', closeWizard);
  if (cancelBtn) cancelBtn.addEventListener('click', closeWizard);
  if (prevBtn) prevBtn.addEventListener('click', _prev);
  if (nextBtn) nextBtn.addEventListener('click', _next);
  if (form) form.addEventListener('submit', _submit);
  if (submitBtn) submitBtn.addEventListener('click', _submit);
  // Étape 3 — ouverture des pickers par délégation sur `data-source-picker`.
  // Ni `onclick=` (une fonction d'un module ES n'existe pas dans le scope
  // global, le clic échouerait en ReferenceError), ni `data-action` (déjà
  // possédé par `preparations.js::_onPanelClick`).
  const sourceCards = _qs('#wizard-source-cards');
  if (sourceCards) {
    sourceCards.addEventListener('click', (ev) => {
      const btn = ev.target && ev.target.closest
        ? ev.target.closest('[data-source-picker]') : null;
      if (!btn) return;
      ev.preventDefault();
      openSourcePicker(btn.getAttribute('data-source-picker'), {
        trigger: btn,
        addEntries: (entries) => addSourceBasketEntries(_basketEl(), entries),
      });
    });
  }
  // Coaching — chips d'intention + rafraîchissement des suggestions quand
  // les entrées qui les conditionnent changent (durée, participants, texte).
  _bindOutcomeChips();
  backdrop.addEventListener('input', () => _renderCoach());
  backdrop.addEventListener('change', () => _renderCoach());
  backdrop.addEventListener('click', () => _renderCoach());
  // Lot 5 — ajout participant dans le wizard step 2.
  const addPartBtn = _qs('#wizard-add-participant-btn');
  if (addPartBtn) {
    addPartBtn.addEventListener('click', () => {
      const list = _qs('#wizard-participants-list');
      if (list) list.appendChild(createParticipantRow({}));
    });
  }
  // Lot 6 — récurrence : toggle ouvre/masque le sous-formulaire, et le
  // select fréquence raffraichit la visibilité des sous-champs (jours).
  const recurringToggle = _qs('#wizard-recurring-toggle');
  const recurrenceForm = _qs('#wizard-recurrence-form');
  if (recurringToggle && recurrenceForm) {
    const _toggle = () => {
      recurrenceForm.style.display = recurringToggle.checked ? '' : 'none';
      if (recurringToggle.checked) refreshFreqVisibility(recurrenceForm);
    };
    recurringToggle.addEventListener('change', _toggle);
    _toggle();
  }
  if (recurrenceForm) {
    const freqEl = recurrenceForm.querySelector('[data-rrule-freq]');
    if (freqEl) freqEl.addEventListener('change', () => refreshFreqVisibility(recurrenceForm));
  }
  // Click backdrop (hors carte) → ferme.
  backdrop.addEventListener('click', (ev) => {
    if (ev.target === backdrop) closeWizard();
  });
  // ESC ferme.
  document.addEventListener('keydown', (ev) => {
    if (!_wizardOpenedOnce) return;
    if (!backdrop.classList.contains('is-open')) return;
    // Une modale empilée (picker de sources) est au-dessus : ESC lui
    // appartient. Sans cette garde, échapper d'un picker fermerait le wizard
    // entier — et le travail en cours partirait avec lui.
    if (isStackedModalOpen()) return;
    if (ev.key === 'Escape') { ev.preventDefault(); closeWizard(); }
  });
  _bindChips(backdrop);
}

// Champs pré-remplissables depuis un lien entrant (route /preparer). Le
// serveur a déjà borné et assaini ces valeurs ; ici on ne fait que les poser.
const _PREFILL_FIELDS = {
  subject: 'wizard-subject',
  duration: 'wizard-duration',
  role: 'wizard-role',
  expectation: 'wizard-expectation',
  meeting_type: 'wizard-meeting-type',
};

function _applyPrefillFromQuery(params) {
  Object.entries(_PREFILL_FIELDS).forEach(([param, id]) => {
    const value = params.get(param);
    if (!value) return;
    const el = document.getElementById(id);
    if (!el || el.value) return;
    if (el.tagName === 'SELECT') {
      // Un type de réunion inconnu est ignoré plutôt que posé en dur :
      // la valeur vient d'une application tierce.
      if (Array.from(el.options).some((o) => o.value === value)) el.value = value;
    } else {
      el.value = value;
    }
  });
  const ref = params.get('external_ref');
  if (ref) _externalRef = ref;
}

function _autoOpenFromQuery() {
  try {
    const params = new URLSearchParams(window.location.search || '');
    if (params.get('action') === 'new') {
      // Active aussi le bon onglet pour cohérence.
      try {
        const tabBtn = document.getElementById('tab-btn-brief');
        if (tabBtn) tabBtn.click();
      } catch (e) {}
      openWizard({
        seriesParentId: params.get('series_parent_id') || '',
        targetMeetingDate: params.get('target_date') || '',
      });
      // Après openWizard, qui réinitialise les champs.
      _applyPrefillFromQuery(params);
    }
  } catch (e) { /* non-fatal */ }
}

function _boot() {
  _bindEvents();
  _bindAutosave();
  _autoOpenFromQuery();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _boot);
} else {
  _boot();
}

// ─── Drafts (autosave localStorage) ──────────────────────────────────
//
// Persiste l'état du wizard à chaque keystroke (debounced 400 ms) sous
// `mesreunions.prep-wizard.drafts`. Permet à l'utilisateur de fermer le
// wizard sans rien perdre, et à preparations.js d'afficher les brouillons
// non générés dans la liste à côté des briefs DB (A2). Suppression au
// `phase=done` (générer = valider le brouillon).
//
// Format : { [draftId]: { step, fields, participants, recurring, themes,
//                         updatedAt, title } }

const _DRAFTS_KEY = 'mesreunions.prep-wizard.drafts';
let _currentDraftId = null;
let _saveTimer = null;

function _newDraftId() {
  return 'draft_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2, 6);
}

function _readAllDrafts() {
  try { return JSON.parse(localStorage.getItem(_DRAFTS_KEY) || '{}') || {}; }
  catch (e) { return {}; }
}

function _writeAllDrafts(obj) {
  // Cette fonction avalait toute erreur en silence. Or le quota localStorage
  // (~5 Mo, partagé par TOUS les brouillons) est réellement atteignable
  // depuis que les sources `inline` portent jusqu'à 20 000 caractères : le
  // brouillon entier était alors perdu sans le moindre signal.
  try {
    localStorage.setItem(_DRAFTS_KEY, JSON.stringify(obj));
    return true;
  } catch (e) {
    const isQuota = e && (e.name === 'QuotaExceededError'
      || e.name === 'NS_ERROR_DOM_QUOTA_REACHED' || e.code === 22);
    if (isQuota && !_draftQuotaWarned) {
      _draftQuotaWarned = true;
      try {
        showToast('Brouillon non sauvegardé : l\'espace local est saturé. '
          + 'Supprimez d\'anciens brouillons, ou générez ce brief maintenant.', 'error');
      } catch (e2) { /* non-fatal */ }
    }
    return false;
  }
}

// Exposé pour preparations.js (rendu liste).
export function listDrafts() {
  const all = _readAllDrafts();
  // L'id est la clé du dict — on doit l'injecter dans chaque snapshot
  // sinon le rendu UI sort `data-draft-id=""` vide et le click handler
  // `reopen-draft` reçoit '' → reopenPrepDraft('') → return false silencieux.
  return Object.entries(all)
    .map(([id, snap]) => Object.assign({}, snap, { id: id }))
    .sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
}
export function deleteDraft(id) {
  if (!id) return;
  const all = _readAllDrafts();
  delete all[id];
  _writeAllDrafts(all);
}
function _saveDraft(id, snap) {
  if (!id || !snap) return;
  const all = _readAllDrafts();
  all[id] = snap;
  _writeAllDrafts(all);
}

function _collectSnapshot() {
  // Inputs natifs du wizard
  const fields = {};
  const NATIVE_IDS = [
    'wizard-subject', 'wizard-role', 'wizard-expectation',
    'wizard-meeting-type', 'wizard-duration', 'wizard-drive-folder',
    'wizard-target-date', 'wizard-series-parent',
    'wizard-recurring-toggle', 'wizard-rrule-freq', 'wizard-rrule-interval',
    'wizard-rrule-time', 'wizard-rrule-until', 'wizard-send-cr-email',
    'wizard-success-criteria',
  ];
  NATIVE_IDS.forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    if (el.type === 'checkbox') fields[id] = !!el.checked;
    else fields[id] = el.value || '';
  });
  // Participants (composant lib/participants.js)
  let participants = [];
  try {
    const c = document.getElementById('wizard-participants-list');
    if (c) participants = serializeParticipantsContainer(c) || [];
  } catch (e) {}
  // Thèmes (composant mountThemesChips)
  let themes = [];
  try {
    const tc = document.getElementById('wizard-themes-container');
    // serializeThemesChips lit le state du composant. L'ancien appel visait
    // _themesChipsGetValues(), qui n'a jamais existé : gardé par un `if`, il
    // échouait en silence et aucune thématique n'était sauvegardée.
    if (tc) themes = serializeThemesChips(tc) || [];
  } catch (e) {}
  // Coaching — chips d'intention (toggles multi-sélection).
  let outcomes = [];
  try {
    outcomes = Array.from(
      document.querySelectorAll('#wizard-outcome-chips .wizard-chip.is-on'),
    ).map((c) => c.dataset.outcome).filter(Boolean);
  } catch (e) {}
  // Sources (composant lib/source-basket.js). `snapshotEntries` ampute les
  // textes collés au-delà d'un budget volontairement bas : un brouillon
  // tronqué et signalé vaut mieux qu'un brouillon perdu par dépassement de
  // quota (cf. _writeAllDrafts).
  let sources = [];
  try {
    const snap = snapshotEntries(_sourceEntries());
    sources = snap.entries;
    if (snap.degraded && !_draftQuotaWarned) {
      _draftQuotaWarned = true;
      showToast('Les textes collés sont trop volumineux pour le brouillon : '
        + 'ils y sont abrégés. Générez le brief sans fermer le wizard pour '
        + 'les conserver entiers.', 'error');
    }
  } catch (e) {}
  return {
    step: _currentStep || 0,
    fields, participants, themes, outcomes, sources,
    title: fields['wizard-subject'] || '(brouillon sans titre)',
    updatedAt: Date.now(),
  };
}

function _applySnapshot(snap) {
  if (!snap || !snap.fields) return;
  Object.entries(snap.fields).forEach(([id, val]) => {
    const el = document.getElementById(id);
    if (!el) return;
    if (el.type === 'checkbox') el.checked = !!val;
    else el.value = (val == null ? '' : val);
    // Trigger éventuels listeners (ex: toggle récurrence -> reveal form)
    try { el.dispatchEvent(new Event('change', { bubbles: true })); } catch (e) {}
  });
  // Participants
  try {
    const list = document.getElementById('wizard-participants-list');
    if (list && Array.isArray(snap.participants) && snap.participants.length > 0) {
      list.innerHTML = '';
      const onChange = () => _scheduleSave();
      snap.participants.forEach((p) => {
        list.appendChild(createParticipantRow(p || {}, onChange));
      });
    }
  } catch (e) {}
  // Thèmes
  try {
    const tc = document.getElementById('wizard-themes-container');
    // Idem au retour : la méthode exposée est _themesChipsSetThemes.
    if (tc && tc._themesChipsSetThemes && Array.isArray(snap.themes)) {
      tc._themesChipsSetThemes(snap.themes);
    }
  } catch (e) {}
  // Sources — panier de l'étape 3.
  try {
    const basket = _basketEl();
    if (basket) {
      if (Array.isArray(snap.sources) && snap.sources.length) {
        setSourceBasketEntries(basket, snap.sources);
      } else if (snap.fields['wizard-drive-folder']) {
        // Brouillon antérieur au panier : son dossier collé ne vivait que
        // dans le champ texte. On le remonte en entrée pour qu'il reste
        // visible et supprimable, au lieu d'un champ devenu invisible.
        setSourceBasketEntries(basket, [{
          type: 'drive_folder',
          id: snap.fields['wizard-drive-folder'],
          label: snap.fields['wizard-drive-folder'],
          origin: 'link',
          meta: 'Dossier Drive collé',
        }]);
      }
    }
  } catch (e) {}
  // Coaching — restaure les chips d'intention.
  try {
    if (Array.isArray(snap.outcomes)) {
      document.querySelectorAll('#wizard-outcome-chips .wizard-chip').forEach((c) => {
        c.classList.toggle('is-on', snap.outcomes.indexOf(c.dataset.outcome) !== -1);
      });
    }
  } catch (e) {}
  // Step
  if (typeof snap.step === 'number') _showStep(snap.step);
}

function _scheduleSave() {
  if (!_currentDraftId) return;
  if (_saveTimer) clearTimeout(_saveTimer);
  _saveTimer = setTimeout(() => {
    _saveTimer = null;
    try { _saveDraft(_currentDraftId, _collectSnapshot()); } catch (e) {}
  }, 400);
}

function _bindAutosave() {
  const backdrop = document.getElementById('wizard-modal-backdrop');
  if (!backdrop) return;
  // Délégation : tout input/change/click(bouton+/-/checkbox) déclenche un save.
  ['input', 'change', 'click'].forEach((evt) => {
    backdrop.addEventListener(evt, (ev) => {
      // ignore les clics sur les boutons de navigation (prev/next/submit/cancel/close)
      const t = ev.target;
      if (!t) return;
      if (t.id === 'wizard-next-btn' || t.id === 'wizard-prev-btn'
          || t.id === 'wizard-submit-btn' || t.id === 'wizard-cancel-btn'
          || t.id === 'wizard-close-btn') return;
      if (!_currentDraftId) return; // wizard non ouvert
      _scheduleSave();
    });
  });
}

// Publié pour preparations.js (réouverture d'un brouillon depuis la liste).
export function reopenDraft(draftId) {
  const all = _readAllDrafts();
  const snap = all[draftId];
  if (!snap) return false;
  openWizard({ draftId, restoreSnapshot: snap });
  return true;
}

// Publié sur window pour data-action="open-wizard" + tests.
if (typeof window !== 'undefined') {
  window.openWizard = openWizard;
  window.closeWizard = closeWizard;
  window.listPrepDrafts = listDrafts;
  window.deletePrepDraft = deleteDraft;
  window.reopenPrepDraft = reopenDraft;
}
