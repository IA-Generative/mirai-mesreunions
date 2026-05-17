// Lot 7 — Time machine : rendu de la timeline horizontale d'une série
// de préparations, navigation entre occurrences, mode readonly preview.
//
// Le module est volontairement sans dépendance Vite externe : il
// reçoit une fonction de navigation (callback) et expose simplement
// le HTML + des hooks "data-action" délégués au panel preparations.js.
//
// Format attendu pour `payload` (réponse de
// `GET /api/preparations/{id}/series` enrichie côté Lot 7) :
//   {
//     series: [
//       { id, title, target_meeting_date, status, created_at,
//         is_current, has_meeting, meeting_id },
//       ...
//     ],
//     root_id: '<uuid>',
//     series_parent_id: '<uuid>'|null,
//     count: N
//   }

import { formatDate } from '../utils/format.js';

function _esc(v) {
  return (v == null ? '' : String(v)).replace(/[&<>"']/g, (s) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[s]);
}

// Format court (jour + mois abrégé) propre à la timeline — pas factorisable
// avec formatDate qui produit toujours l'année complète.
function _shortDate(iso) {
  if (!iso) return 'sans date';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return 'sans date';
  return d.toLocaleDateString('fr-FR', { day: 'numeric', month: 'short' });
}

function _fullDate(iso) {
  return formatDate(iso, { withTime: true }) || 'date non renseignée';
}

// Retourne l'id de l'occurrence "active" de la série :
// la plus récente avec target_meeting_date >= now, sinon la dernière.
export function pickActiveOccurrenceId(series) {
  const list = Array.isArray(series) ? series : [];
  if (!list.length) return null;
  const now = Date.now();
  const upcoming = list.filter((o) => {
    if (!o || !o.target_meeting_date) return false;
    const t = Date.parse(o.target_meeting_date);
    return !Number.isNaN(t) && t >= now;
  });
  if (upcoming.length) {
    upcoming.sort((a, b) =>
      Date.parse(a.target_meeting_date) - Date.parse(b.target_meeting_date));
    return upcoming[0].id;
  }
  // Sinon : dernière occurrence (par target_meeting_date desc, fallback created_at)
  const sorted = list.slice().sort((a, b) => {
    const ta = Date.parse(a.target_meeting_date || a.created_at || 0) || 0;
    const tb = Date.parse(b.target_meeting_date || b.created_at || 0) || 0;
    return tb - ta;
  });
  return sorted[0] && sorted[0].id || null;
}

// Indique si on doit afficher la timeline (au moins 2 noeuds OU prep avec parent).
export function shouldShowTimeline(payload, currentId) {
  if (!payload) return false;
  const series = Array.isArray(payload.series) ? payload.series : [];
  if (series.length >= 2) return true;
  if (payload.series_parent_id) return true;
  // Cas dégénéré : 1 seule prep dans la série → pas de timeline (sauf parent).
  return false;
}

// Rend le HTML de la timeline.
// `currentId` : id de la prep affichée. `readonly` : si vrai, on n'affiche
// pas le noeud "Programmer la suivante".
export function renderTimeline(payload, currentId, opts) {
  opts = opts || {};
  const readonly = !!opts.readonly;
  const series = Array.isArray(payload && payload.series) ? payload.series : [];
  if (!shouldShowTimeline(payload, currentId)) return '';

  const nodes = series.map((occ, idx) => {
    const isCurrent = String(occ.id) === String(currentId);
    const isDone = !!occ.has_meeting;
    let icon;
    let cls;
    if (isCurrent) { icon = '●'; cls = 'mp-tl-node mp-tl-node--current'; }
    else if (isDone) { icon = '✓'; cls = 'mp-tl-node mp-tl-node--done'; }
    else { icon = '○'; cls = 'mp-tl-node mp-tl-node--pending'; }

    const label = _shortDate(occ.target_meeting_date);
    const title = (occ.title || '(sans titre)') + ' — ' +
                  _fullDate(occ.target_meeting_date);
    const tag = isCurrent ? 'span' : 'a';
    const attrs = isCurrent
      ? `class="${cls}" aria-current="step" title="${_esc(title)}"`
      : `class="${cls}" href="#" data-action="open-series-occurrence" `
        + `data-occurrence-id="${_esc(occ.id)}" title="${_esc(title)}"`;
    return `<li class="mp-tl-item">`
      + `<${tag} ${attrs}>`
      +   `<span class="mp-tl-icon" aria-hidden="true">${icon}</span>`
      +   `<span class="mp-tl-label">${_esc(label)}</span>`
      +   (isCurrent ? `<span class="mp-tl-here fr-sr-only">vous êtes ici</span>` : '')
      + `</${tag}>`
      + `</li>`;
  });

  // Noeud "Programmer la suivante" — masqué en readonly preview.
  if (!readonly) {
    nodes.push(
      `<li class="mp-tl-item mp-tl-item--add">`
      + `<a href="#" class="mp-tl-node mp-tl-node--add" `
      +   `data-action="open-prepare-next-modal" `
      +   `title="Programmer la prochaine occurrence">`
      +   `<span class="mp-tl-icon" aria-hidden="true">○</span>`
      +   `<span class="mp-tl-label">Programmer la suivante</span>`
      + `</a>`
      + `</li>`
    );
  }

  return `<section class="mp-series-timeline" aria-label="Historique de la série">`
    + `<h3 class="mp-series-timeline-title">Historique de la série`
    +   ` <span class="fr-badge fr-badge--info fr-badge--sm">`
    +     `${series.length} occurrence${series.length > 1 ? 's' : ''}`
    +   `</span></h3>`
    + `<ol class="mp-tl-list">${nodes.join('')}</ol>`
    + `</section>`;
}

// Bandeau readonly preview (DSFR fr-notice). `activeId` : id de l'occurrence
// "active" de la série vers laquelle pointe le bouton de retour.
export function renderReadonlyBanner(activeId) {
  if (!activeId) {
    return `<div class="fr-notice fr-notice--info mp-readonly-banner" role="status">`
      + `<div class="fr-container"><div class="fr-notice__body">`
      + `<p class="fr-notice__title">Vous consultez l'historique de la série.</p>`
      + `</div></div></div>`;
  }
  return `<div class="fr-notice fr-notice--info mp-readonly-banner" role="status">`
    + `<div class="fr-container"><div class="fr-notice__body">`
    + `<p class="fr-notice__title">Vous consultez l'historique de la série.</p>`
    + `<p style="margin:0.3rem 0 0 0;">`
    +   `<a class="fr-link" href="#" data-action="open-series-occurrence" `
    +     `data-occurrence-id="${_esc(activeId)}" data-leave-readonly="1">`
    +     `Revenir à la préparation active de la série`
    +   `</a>`
    + `</p>`
    + `</div></div></div>`;
}

// Mode "readonly preview" : utilitaires de lecture/écriture du flag,
// portés par l'URL (`?readonly=1`) ET par un état mémoire (fallback si
// l'app ne touche pas à pushState, ex: navigation interne sans URL).
let _memReadonly = false;

export function isReadonlyPreview() {
  if (_memReadonly) return true;
  try {
    const u = new URL(window.location.href);
    if ((u.searchParams.get('readonly') || '') === '1') return true;
  } catch (_e) { /* ignore */ }
  return false;
}

export function setReadonlyPreview(on) {
  _memReadonly = !!on;
  try {
    const u = new URL(window.location.href);
    if (on) u.searchParams.set('readonly', '1');
    else u.searchParams.delete('readonly');
    window.history.replaceState({}, '', u.toString());
  } catch (_e) { /* ignore */ }
}

// Désactive les contrôles d'édition dans la fiche brief (rename inline,
// glossaire édit, link audio, amend, save participants, recurrence save).
// Garde visible : export + bouton "Retour à la prep active" du banner.
const READONLY_DISABLE_SELECTORS = [
  '[data-action="start-rename-inline"]',
  '[data-action="open-link-audio-modal"]',
  '[data-action="open-glossary-modal"]',
  '[data-action="open-prepare-next-modal"]',
  '[data-action="toggle-amend"]',
  '[data-action="rename-brief"]',
  '[data-action="save-detail-participants"]',
  '[data-action="add-detail-participant"]',
  '[data-action="save-detail-recurrence"]',
  '[data-action="detach-audio"]',
  '#brief-detail-recurring-toggle',
];

export function applyReadonlyToDetail(rootEl) {
  const root = rootEl || document.getElementById('brief-detail-view');
  if (!root) return;
  READONLY_DISABLE_SELECTORS.forEach((sel) => {
    root.querySelectorAll(sel).forEach((el) => {
      try {
        el.setAttribute('disabled', 'disabled');
        el.setAttribute('aria-disabled', 'true');
        el.classList.add('mp-readonly-disabled');
        el.style.opacity = '0.5';
        el.style.pointerEvents = 'none';
        el.title = 'Lecture seule (historique de série)';
      } catch (_e) { /* ignore */ }
    });
  });
  // Le titre éditable inline : on coupe l'écouteur en désarmant data-action.
  const titleEl = root.querySelector('#brief-detail-title[data-action="start-rename-inline"]');
  if (titleEl) {
    titleEl.removeAttribute('data-action');
    titleEl.style.cursor = 'default';
  }
}

export function clearReadonlyFromDetail(rootEl) {
  const root = rootEl || document.getElementById('brief-detail-view');
  if (!root) return;
  READONLY_DISABLE_SELECTORS.forEach((sel) => {
    root.querySelectorAll(sel + '.mp-readonly-disabled').forEach((el) => {
      try {
        el.removeAttribute('disabled');
        el.removeAttribute('aria-disabled');
        el.classList.remove('mp-readonly-disabled');
        el.style.opacity = '';
        el.style.pointerEvents = '';
        el.removeAttribute('title');
      } catch (_e) { /* ignore */ }
    });
  });
}
