// Helper UI — composant "chips de thématiques" (Lot 9 PR-6).
//
// API minimale, sans framework :
//   mountThemesChips(rootEl, { initial=[], suggestions=[], cap=50, onChange })
//   serializeThemesChips(rootEl) → string[]
//   loadThemesSuggestions() → Promise<{label, count}[]>
//
// Le composant injecte :
//   - une liste de chips (button × pour retirer)
//   - un input + bouton "+ Ajouter"
//   - un <datalist> alimenté par les suggestions pour auto-complétion
//
// Persistance : géré par l'appelant (wizard collect ou amend inline).

const SAFE_RE = /[&<>"']/g;
function _esc(s) {
  return String(s || '').replace(SAFE_RE, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function _normalize(label, existing) {
  const s = String(label || '').trim();
  if (!s) return null;
  if (s.length > 80) return null;
  const lc = s.toLowerCase();
  if (existing.some((x) => x.toLowerCase() === lc)) return null;
  return s;
}

export function mountThemesChips(rootEl, opts) {
  if (!rootEl) return;
  opts = opts || {};
  const cap = opts.cap || 50;
  const initial = Array.isArray(opts.initial) ? opts.initial.slice(0, cap) : [];
  const suggestions = Array.isArray(opts.suggestions) ? opts.suggestions : [];
  const onChange = typeof opts.onChange === 'function' ? opts.onChange : null;

  rootEl.classList.add('themes-chips-root');
  rootEl.innerHTML = `
    <div class="themes-chips-list" style="display:flex;flex-wrap:wrap;gap:0.4rem;margin-bottom:0.5rem;"></div>
    <div class="themes-chips-input-row" style="display:flex;gap:0.4rem;align-items:flex-end;">
      <div class="fr-input-group" style="flex:1 1 auto;margin:0;">
        <input class="fr-input fr-input--sm themes-chips-input" type="text"
               list="themes-chips-suggest-${Math.random().toString(36).slice(2, 8)}"
               placeholder="Ajouter une thématique…" maxlength="80">
      </div>
      <button type="button" class="fr-btn fr-btn--sm fr-btn--secondary themes-chips-add">+ Ajouter</button>
    </div>
    <datalist class="themes-chips-datalist"></datalist>
    <div class="themes-chips-counter" style="font-size:0.8rem;color:#64748b;margin-top:0.3rem;"></div>
  `;

  const listEl = rootEl.querySelector('.themes-chips-list');
  const inputEl = rootEl.querySelector('.themes-chips-input');
  const addBtn = rootEl.querySelector('.themes-chips-add');
  const datalistEl = rootEl.querySelector('.themes-chips-datalist');
  const counterEl = rootEl.querySelector('.themes-chips-counter');

  const datalistId = 'themes-chips-suggest-' + Math.random().toString(36).slice(2, 8);
  inputEl.setAttribute('list', datalistId);
  datalistEl.id = datalistId;

  const state = { themes: initial.slice() };

  function _renderChips() {
    listEl.innerHTML = state.themes.map((t, i) => (
      `<span class="fr-tag fr-tag--sm themes-chip" data-idx="${i}"
             style="display:inline-flex;align-items:center;gap:0.3rem;">
         ${_esc(t)}
         <button type="button" class="themes-chip-remove" aria-label="Retirer"
                 data-idx="${i}"
                 style="background:none;border:0;cursor:pointer;color:#b91c1c;font-weight:700;padding:0 0.1rem;">×</button>
       </span>`
    )).join('');
    counterEl.textContent = state.themes.length + ' / ' + cap + ' thématiques';
    listEl.querySelectorAll('.themes-chip-remove').forEach((btn) => {
      btn.addEventListener('click', () => {
        const idx = parseInt(btn.dataset.idx, 10);
        if (!Number.isNaN(idx)) {
          state.themes.splice(idx, 1);
          _renderChips();
          if (onChange) onChange(state.themes.slice());
        }
      });
    });
  }

  function _renderSuggestions(items) {
    datalistEl.innerHTML = (items || [])
      .map((s) => `<option value="${_esc(s.label || s)}">`).join('');
  }

  function _add(label) {
    if (state.themes.length >= cap) return;
    const v = _normalize(label, state.themes);
    if (!v) return;
    state.themes.push(v);
    inputEl.value = '';
    _renderChips();
    if (onChange) onChange(state.themes.slice());
  }

  addBtn.addEventListener('click', () => _add(inputEl.value));
  inputEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); _add(inputEl.value); }
  });

  _renderChips();
  _renderSuggestions(suggestions);

  // API publique sur le DOM root pour serialize/replace.
  rootEl._themesChipsState = state;
  rootEl._themesChipsSetSuggestions = _renderSuggestions;
  rootEl._themesChipsSetThemes = (newThemes) => {
    state.themes = (Array.isArray(newThemes) ? newThemes : []).slice(0, cap);
    _renderChips();
  };
}

export function serializeThemesChips(rootEl) {
  if (!rootEl || !rootEl._themesChipsState) return [];
  return (rootEl._themesChipsState.themes || []).slice();
}

export async function loadThemesSuggestions() {
  try {
    const r = await fetch('/api/preparations/themes-suggestions');
    if (!r.ok) return [];
    const d = await r.json().catch(() => ({}));
    return Array.isArray(d.themes) ? d.themes : [];
  } catch (e) {
    return [];
  }
}
