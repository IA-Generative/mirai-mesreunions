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
  return { meetingType, subject, role, expectation, drive, duration, focus };
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
    </dl>`;
}

// ── Submit POST /api/preparations ───────────────────────────────────────
async function _submit(ev) {
  if (ev) ev.preventDefault();
  _clearStatus();
  // Validation finale : on rejoue les checks des steps 0+1 (les autres ne
  // sont pas obligatoires).
  if (!_validateStepIdx(0) || !_validateStepIdx(1)) return;
  const v = _collectValues();

  // series_parent_id éventuel — lu depuis la query string ou ?series_parent_id
  // posé en hidden input par openWizard().
  const seriesParent = (_qs('#wizard-series-parent') || {}).value || '';

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

  const submitBtn = _qs('#wizard-submit-btn');
  if (submitBtn) submitBtn.disabled = true;
  _setStatus(v.drive
    ? 'Lecture du Drive et génération du brief en cours…'
    : 'Génération du brief en cours…', 'info');

  try {
    const r = await fetch('/api/preparations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      _setStatus(d.error || ('Erreur ' + r.status), 'err');
      return;
    }
    _setStatus('Brief généré.', 'info');
    closeWizard();
    // Refresh la liste briefs et ouvre la fiche du brief créé si l'API
    // renvoie son id (le service le fait — cf modules/preparations).
    try {
      if (typeof window.loadBriefs === 'function') window.loadBriefs();
      const newId = (d && (d.id || (d.preparation && d.preparation.id))) || null;
      if (newId && typeof window.showBriefDetail === 'function') {
        setTimeout(() => window.showBriefDetail(newId), 100);
      }
    } catch (e) { /* non-fatal */ }
  } catch (err) {
    _setStatus('Erreur réseau : ' + (err && err.message ? err.message : err), 'err');
  } finally {
    if (submitBtn) submitBtn.disabled = false;
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
