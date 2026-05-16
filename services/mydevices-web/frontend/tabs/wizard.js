// Wizard "Nouvelle préparation" — modale fullscreen DSFR avec stepper
// multi-étapes (chantier UX-Refonte-3 #3).
//
// Remplace la route HTML standalone /meeting-prep/new (supprimée — désormais
// redirect vers /?tab=brief&action=new). La modale est rendue inline dans
// l'app (menu visible derrière) ; le wizard reprend les mêmes champs que
// l'ancien _wizard_template.py mais distribués en 5 steps :
//   1. Identité  : type + sujet (+ durée prévue, déplacée ici pour cohérence)
//   2. Contexte  : rôle dans la réunion + attendu du brief
//   3. Documents : dossier Drive optionnel
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

const STEP_IDS = ['identite', 'contexte', 'documents', 'focus', 'recap'];
const STEP_LABELS = [
  'Identité', 'Contexte', 'Documents', 'Focus', 'Récap',
];

let _currentStep = 0;
let _wizardOpenedOnce = false;

function _qs(sel, root) { return (root || document).querySelector(sel); }
function _qsa(sel, root) { return Array.from((root || document).querySelectorAll(sel)); }

function _esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function _statusBox() { return _qs('#wizard-status'); }

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
  return {
    meetingType, subject, role, expectation, drive, duration, focus, participants,
    isRecurring: isRecurring && !!recurrenceRule,
    recurrenceRule: isRecurring ? recurrenceRule : null,
  };
}

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
function _stopPolling() {
  if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }
}

async function _pollJob(jobId, opts) {
  try {
    const r = await fetch(`/api/preparations/jobs/${encodeURIComponent(jobId)}`);
    if (r.status === 404) {
      _setStatus('Job de génération introuvable (expiré ?).', 'err');
      _stopPolling();
      return;
    }
    const d = await r.json().catch(() => ({}));
    _renderGenerationStepper(d, opts);
    if (d.phase === 'done') {
      _stopPolling();
      _setStatus('Brief généré.', 'info');
      const newId = d.preparation_id;
      closeWizard();
      try {
        if (typeof window.loadBriefs === 'function') window.loadBriefs();
        if (newId && typeof window.showBriefDetail === 'function') {
          setTimeout(() => window.showBriefDetail(newId), 100);
        }
      } catch (e) { /* non-fatal */ }
      return;
    }
    if (d.phase === 'failed') {
      _stopPolling();
      _setStatus(d.error || 'La génération a échoué.', 'err');
      const submitBtn = _qs('#wizard-submit-btn');
      if (submitBtn) submitBtn.disabled = false;
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

  const submitBtn = _qs('#wizard-submit-btn');
  if (submitBtn) submitBtn.disabled = true;
  _setStatus(v.drive
    ? 'Lecture du Drive et génération du brief en cours…'
    : 'Génération du brief en cours…', 'info');
  // Affiche tout de suite le stepper en état initial pour feedback immédiat.
  _renderGenerationStepper({ phase: 'init' }, { hasDrive: !!v.drive });

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
      _pollJob(d.job_id, { hasDrive: !!v.drive });
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

// ── Test d'accès Drive (Lot 1) ───────────────────────────────────────────
async function _testDriveAccess() {
  const input = _qs('#wizard-drive-folder');
  const result = _qs('#wizard-drive-test-result');
  const spinner = _qs('#wizard-drive-test-spinner');
  const btn = _qs('#wizard-drive-test-btn');
  if (!input || !result) return;
  const raw = (input.value || '').trim();
  if (!raw) {
    result.innerHTML = '<div class="fr-alert fr-alert--info fr-alert--sm"><p>Aucun dossier renseigné — le test est inutile.</p></div>';
    return;
  }
  result.innerHTML = '';
  if (spinner) spinner.style.display = '';
  if (btn) btn.disabled = true;
  try {
    const url = '/api/preparations/test-drive?folder_id=' + encodeURIComponent(raw);
    const r = await fetch(url);
    const d = await r.json().catch(() => ({}));
    if (d && d.ok) {
      const n = d.docs_count || 0;
      const docs = (d.docs || []).filter(x => !x.is_folder).slice(0, 10);
      let list = '';
      if (docs.length) {
        list = '<ul style="margin:0.3rem 0 0 1.2rem;font-size:0.85rem;">'
          + docs.map(x => `<li>${_esc(x.name)}</li>`).join('')
          + (n > docs.length ? `<li><em>…et ${n - docs.length} autre(s)</em></li>` : '')
          + '</ul>';
      }
      result.innerHTML = `<div class="fr-alert fr-alert--success fr-alert--sm">
        <p><strong>${n} document(s) trouvé(s)</strong> dans ce dossier.</p>${list}</div>`;
    } else {
      const msg = (d && d.error) ? d.error : 'Accès au Drive impossible.';
      result.innerHTML = `<div class="fr-alert fr-alert--error fr-alert--sm"><p>${_esc(msg)}</p></div>`;
    }
  } catch (err) {
    result.innerHTML = `<div class="fr-alert fr-alert--error fr-alert--sm"><p>Erreur réseau : ${_esc(err && err.message ? err.message : err)}</p></div>`;
  } finally {
    if (spinner) spinner.style.display = 'none';
    if (btn) btn.disabled = false;
  }
}

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
  const driveTestResult = _qs('#wizard-drive-test-result');
  if (driveTestResult) driveTestResult.innerHTML = '';
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
  const driveTestBtn = _qs('#wizard-drive-test-btn');
  if (driveTestBtn) driveTestBtn.addEventListener('click', _testDriveAccess);
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
    if (ev.key === 'Escape') { ev.preventDefault(); closeWizard(); }
  });
  _bindChips(backdrop);
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
      openWizard();
    }
  } catch (e) { /* non-fatal */ }
}

function _boot() {
  _bindEvents();
  _autoOpenFromQuery();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _boot);
} else {
  _boot();
}

// Publié sur window pour data-action="open-wizard" + tests.
if (typeof window !== 'undefined') {
  window.openWizard = openWizard;
  window.closeWizard = closeWizard;
}
