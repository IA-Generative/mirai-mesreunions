// Onglet "Mes données utiles" — vitrine des sources de connaissance
// personnelle exploitées (ou bientôt exploitées) par les briefs de
// préparation et les comptes-rendus.
//
// Cycle actuel (cf. memoire reference_meeting_prep_cycle.md) :
//   - glossaire personnel agrégé via les briefs (table user_glossary_terms,
//     reflété dans Drive : Préparations de réunion/glossaire-utilisateur.txt)
//   - sync Drive automatique des 4 documents par brief
//   - corbeille soft-delete 30j (onglet "Corbeille" dédié)
//
// Pas d'endpoint backend dédié au glossaire utilisateur exposé côté
// mesreunions-web pour l'instant — ce module rend une vue informative
// + des cards "à venir" pour les intégrations futures (mail, agenda,
// Resana, mescollections, drives institutionnels).

import { CURRENT_USER } from '../lib/bootstrap.js';

export const COMING_SOON = [
  { id: 'mail', label: 'Boîte mail', icon: 'fr-icon-mail-line' },
  { id: 'agenda', label: 'Agenda', icon: 'fr-icon-calendar-line' },
  { id: 'drive-perso', label: 'Drive personnel (NextCloud)', icon: 'fr-icon-cloud-line' },
  { id: 'drive-dinum', label: 'Drive DINUM / DTNUM', icon: 'fr-icon-folder-2-line' },
  { id: 'resana', label: 'Resana', icon: 'fr-icon-team-line' },
  { id: 'mescollections', label: 'Mes collections', icon: 'fr-icon-bookmark-line' },
  { id: 'data-sources', label: 'Sources de données', icon: 'fr-icon-database-line' },
  { id: 'agents', label: 'Agents', icon: 'fr-icon-robot-line' },
];

function _esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

function _comingSoonCard(icon, title, desc) {
  return `
    <div class="fr-col-12 fr-col-md-6 fr-col-lg-4">
      <div class="fr-card fr-card--shadow" style="opacity:0.72;">
        <div class="fr-card__body">
          <div class="fr-card__content">
            <h3 class="fr-card__title">
              <span class="${icon}" aria-hidden="true" style="margin-right:0.3rem;"></span>
              ${_esc(title)}
              <span class="fr-badge fr-badge--info fr-badge--sm fr-ml-1w">À venir</span>
            </h3>
            <p class="fr-card__desc">${_esc(desc)}</p>
          </div>
        </div>
      </div>
    </div>
  `;
}

const COMING_SOON_DESCRIPTIONS = {
  mail: "Recherche dans vos derniers échanges pour préparer une réunion à partir d'un fil de discussion.",
  agenda: 'Détection automatique des prochaines réunions et préparation suggérée.',
  'drive-perso': 'Indexation de vos documents personnels pour citation contextuelle dans les briefs.',
  'drive-dinum': 'Accès lecture seule aux espaces partagés de votre direction.',
  resana: 'Liaison avec votre espace collaboratif Resana (notes, espaces projets).',
  mescollections: 'Bibliothèque personnelle de documents importés (PDF, notes, références).',
  'data-sources': 'Connecteurs vers vos bases de données métier et entrepôts (lecture seule, périmètre cadré).',
  agents: 'Agents spécialisés (synthèse, relecture, recherche) que vous pouvez invoquer depuis vos réunions.',
};

export function mount(container /*, ctx */) {
  if (!container) return;
  const root = container.querySelector('[data-useful-data-root]') || container;
  const email = _esc(CURRENT_USER.email || '');
  const cards = COMING_SOON.map((s) =>
    _comingSoonCard(s.icon, s.label, COMING_SOON_DESCRIPTIONS[s.id] || '')
  ).join('');
  root.innerHTML = `
    <h1 style="font-size:1.1rem;margin-bottom:0.2rem;">Mes données utiles</h1>
    <p class="subtitle" style="margin-top:0;margin-bottom:1rem;">
      Sources de connaissance personnelle exploitées par MIrAI pour enrichir
      vos préparations de réunion et vos comptes-rendus.
    </p>

    <div class="fr-accordions-group">

      <section class="fr-accordion">
        <h3 class="fr-accordion__title">
          <button type="button" class="fr-accordion__btn"
                  aria-expanded="true" aria-controls="ud-acc-glossary">
            Glossaire personnel
          </button>
        </h3>
        <div class="fr-collapse" id="ud-acc-glossary">
          <p class="subtitle" style="margin:0 0 0.6rem 0;">
            Agrégé depuis vos briefs <strong>et</strong> éditable
            directement ci-dessous. Les termes ⭐ favoris sont passés en
            priorité à Whisper. Les termes 🚫 ignorés ne sont plus suggérés.
            <em>Tapez Entrée pour ajouter un terme, click sur les
            icônes pour basculer leur statut.</em>
          </p>
          <div class="glossary-add-row">
            <input type="text" class="glossary-add-input"
                   placeholder="Ajouter un terme (ex: « EFS », « Fernand Naudin »…) puis Entrée"
                   maxlength="200" />
          </div>
          <p style="color:#64748b;font-size:0.72rem;margin:0 0 0.3rem 0;">
            Astuce : maintenez <kbd>Alt</kbd> (ou <kbd>Option</kbd> ⌥) pour faire
            apparaître des cases à cocher et supprimer plusieurs termes d'un coup.
          </p>
          <div class="glossary-bulk-bar" data-glossary-bulk-bar hidden
               style="display:flex;align-items:center;gap:0.6rem;padding:0.4rem 0.6rem;
                      background:#f0f6ff;border:1px solid #c5d8ff;border-radius:0.25rem;
                      margin-bottom:0.4rem;">
            <span data-glossary-bulk-count style="font-size:0.85rem;color:#0c4498;font-weight:600;">
              Aucun terme sélectionné
            </span>
            <button type="button" class="fr-btn fr-btn--tertiary fr-btn--sm"
                    data-glossary-bulk-action="select-all"
                    style="margin-left:auto;">Tout cocher (page)</button>
            <button type="button" class="fr-btn fr-btn--tertiary fr-btn--sm"
                    data-glossary-bulk-action="clear">Décocher</button>
            <button type="button" class="fr-btn fr-btn--sm"
                    data-glossary-bulk-action="delete" disabled
                    style="background:#b91c1c;color:#fff;border-color:#b91c1c;">
              Supprimer la sélection
            </button>
          </div>
          <style>
            /* Par défaut la checkbox est retirée du flow (display:none
               ⇒ le grid ignore l'item et garde ses 5 colonnes intactes).
               En mode bulk on la réintègre EN PREMIÈRE COLONNE d'une
               grille à 6 colonnes pour ne pas casser l'alignement. */
            .glossary-bulk-cb {
              display: none;
              margin: 0;
              width: 1rem;
              height: 1rem;
              cursor: pointer;
            }
            .glossary-bulk-active .glossary-bulk-cb { display: inline-block; }
            .glossary-bulk-active .glossary-row {
              grid-template-columns: 1.2rem 1fr auto 1.4rem 1.4rem 1.4rem;
            }
          </style>
          <div data-my-glossary-list>
            <p style="color:#94a3b8;font-size:0.85rem;">Chargement…</p>
          </div>
        </div>
      </section>

      <section class="fr-accordion">
        <h3 class="fr-accordion__title">
          <button type="button" class="fr-accordion__btn"
                  aria-expanded="false" aria-controls="ud-acc-drive">
            Synchronisation Drive
          </button>
        </h3>
        <div class="fr-collapse" id="ud-acc-drive">
          <div class="fr-callout fr-callout--blue-ecume">
            <p class="fr-callout__text">
              Chaque brief produit 4 documents dans votre Drive :
              <code>brief.md</code>, <code>glossaire.txt</code>,
              <code>documents-source.md</code> et
              <code>prompt-utilise.txt</code>. La synchronisation est
              best-effort asynchrone (overwrite à chaque modification).
              Une suppression dans MIrAI déplace le dossier vers la
              corbeille Drive.
            </p>
          </div>
        </div>
      </section>

    </div>

    <!-- Mes feedbacks — vue lecture des retours laissés par l'utilisateur
         (pouce ↑/↓ + demandes de regénération) avec tag "pris en
         compte" quand un admin a traité. Peuplé via fetch GET
         /api/my-feedback au mount. -->
    <section class="fr-accordion" style="margin-top:0.8rem;">
      <h3 class="fr-accordion__title">
        <button type="button" class="fr-accordion__btn"
                aria-expanded="false" aria-controls="ud-acc-my-feedback">
          Mes feedbacks
        </button>
      </h3>
      <div class="fr-collapse" id="ud-acc-my-feedback">
        <p class="subtitle" style="margin-top:0;margin-bottom:0.6rem;">
          Tous les retours que vous avez laissés sur vos réunions
          (pouce ↑/↓ ou demandes de regénération). Les éléments
          tagués <em>pris en compte</em> ont été traités par un
          administrateur.
        </p>
        <div data-my-feedback-list>
          <p style="color:#94a3b8;font-size:0.85rem;">Chargement…</p>
        </div>
      </div>
    </section>

    <!-- TKT-115 : l'accordéon "Corbeille" doublonnait l'onglet de premier
         niveau du même nom. Remplacé par un callout DSFR simple qui
         pointe vers l'onglet, pour garder la mention dans la rubrique
         "Mes données utiles" sans dupliquer le contenu. -->
    <div class="fr-callout fr-mt-3w" style="margin-top:1rem;">
      <h3 class="fr-callout__title fr-h6">Corbeille</h3>
      <p class="fr-callout__text">
        Les fichiers et briefs supprimés sont conservés
        <strong>30 jours</strong> dans la corbeille avant suppression
        définitive (soft-delete). Restauration possible à tout moment.
      </p>
      <button type="button"
              class="fr-btn fr-btn--secondary fr-btn--icon-left fr-icon-delete-line"
              data-action="open-trash">
        Aller à la corbeille
      </button>
    </div>

    <h2 style="font-size:1rem;margin-top:1.6rem;margin-bottom:0.6rem;">
      Bientôt
    </h2>
    <p class="subtitle" style="margin-top:0;margin-bottom:0.8rem;">
      Sources supplémentaires en cours d'intégration pour enrichir vos briefs.
    </p>
    <div class="fr-grid-row fr-grid-row--gutters">
      ${cards}
    </div>

    ${email ? `<p style="margin-top:1.2rem;font-size:0.78rem;color:#64748b;">
      Connecté en tant que <strong>${email}</strong>.
    </p>` : ''}
  `;

  // Délégation locale : bouton "Aller à la corbeille".
  root.addEventListener('click', (ev) => {
    const btn = ev.target.closest && ev.target.closest('[data-action="open-trash"]');
    if (!btn) return;
    const trashBtn = document.getElementById('tab-btn-trash');
    if (trashBtn) trashBtn.click();
  });

  // Charge les feedbacks utilisateur (asynchrone, ne bloque pas le rendu).
  _loadMyFeedback(root);
  // Charge le glossaire utilisateur + ajout inline via Enter.
  _loadMyGlossary(root);
  _bindGlossaryAddInput(root);
  _bindGlossaryBulk(root);
}

function _bindGlossaryBulk(root) {
  _glossaryRootRef = root;
  _selectedTerms.clear();
  // Alt-key globalement écouté (pas seulement quand le focus est dans
  // le panneau) pour matcher l'UX de l'onglet « Réunions ».
  document.removeEventListener('keydown', _onAltDownGlossary);
  document.removeEventListener('keyup', _onAltUpGlossary);
  document.addEventListener('keydown', _onAltDownGlossary);
  document.addEventListener('keyup', _onAltUpGlossary);

  root.addEventListener('change', (ev) => {
    const cb = ev.target.closest && ev.target.closest('[data-glossary-bulk-cb]');
    if (!cb) return;
    const term = cb.getAttribute('data-term');
    if (!term) return;
    if (cb.checked) _selectedTerms.add(term);
    else _selectedTerms.delete(term);
    _refreshBulkVisuals(root);
  });

  root.addEventListener('click', (ev) => {
    const btn = ev.target.closest && ev.target.closest('[data-glossary-bulk-action]');
    if (!btn) return;
    const action = btn.getAttribute('data-glossary-bulk-action');
    if (action === 'select-all') {
      root.querySelectorAll('[data-glossary-bulk-cb]').forEach((cb) => {
        cb.checked = true;
        const t = cb.getAttribute('data-term');
        if (t) _selectedTerms.add(t);
      });
      _refreshBulkVisuals(root);
    } else if (action === 'clear') {
      _selectedTerms.clear();
      root.querySelectorAll('[data-glossary-bulk-cb]').forEach((cb) => { cb.checked = false; });
      _refreshBulkVisuals(root);
    } else if (action === 'delete') {
      _bulkDeleteSelected(root);
    }
  });
}

// ─── Glossaire personnel — vue éditable inline ────────────────────

async function _loadMyGlossary(root) {
  const list = root.querySelector('[data-my-glossary-list]');
  if (!list) return;
  try {
    const resp = await fetch('/api/my-glossary?limit=500');
    if (!resp.ok) {
      list.innerHTML = `<p style="color:#94a3b8;font-size:0.85rem;">Erreur de chargement (HTTP ${resp.status}).</p>`;
      return;
    }
    const data = await resp.json();
    const items = data.items || [];
    if (!items.length) {
      list.innerHTML = `<p style="color:#94a3b8;font-size:0.85rem;">Aucun terme dans votre glossaire. Ajoutez-en un via le champ ci-dessus, ou ils s'ajouteront automatiquement quand vous créerez des préparations.</p>`;
      return;
    }
    // Purge des termes sélectionnés qui n'existent plus côté backend
    // (suppression individuelle ou par un autre onglet) — évite que
    // la barre bulk affiche un compteur fantôme.
    const existing = new Set(items.map((g) => g.term));
    Array.from(_selectedTerms).forEach((t) => { if (!existing.has(t)) _selectedTerms.delete(t); });

    list.innerHTML = `
      <p style="color:#64748b;font-size:0.75rem;margin:0 0 0.3rem 0;">${items.length} terme(s)</p>
      <div class="glossary-grid">
        ${items.map(_renderGlossaryRow).join('')}
      </div>
    `;
    _refreshBulkVisuals(root);
  } catch (e) {
    list.innerHTML = `<p style="color:#b91c1c;font-size:0.85rem;">Erreur réseau : ${_esc(e.message)}</p>`;
  }
}

function _renderGlossaryRow(g) {
  const curated = !!g.curated_by_user;
  const blocked = !!g.blacklisted;
  const occ = g.occurrence_count || 0;
  const checked = _selectedTerms.has(g.term);
  return `
    <div class="glossary-row${blocked ? ' is-blocked' : ''}${curated ? ' is-curated' : ''}" data-term="${_esc(g.term)}">
      <input type="checkbox" class="glossary-bulk-cb"
             data-glossary-bulk-cb data-term="${_esc(g.term)}"
             aria-label="Sélectionner ${_esc(g.term)}"
             ${checked ? 'checked' : ''} />
      <span class="glossary-term">${_esc(g.term)}</span>
      <span class="glossary-meta">${occ}×</span>
      <button type="button" class="glossary-toggle glossary-toggle--star ${curated ? 'is-on' : ''}"
              data-glossary-action="toggle-curated" data-term="${_esc(g.term)}"
              title="${curated ? 'Retirer des favoris (passe en auto)' : 'Marquer favori (priorité Whisper)'}">
        ${curated ? '⭐' : '☆'}
      </button>
      <button type="button" class="glossary-toggle glossary-toggle--block ${blocked ? 'is-on' : ''}"
              data-glossary-action="toggle-blacklisted" data-term="${_esc(g.term)}"
              title="${blocked ? 'Réactiver le terme' : "Ignorer ce terme (ne plus l'utiliser)"}">
        ${blocked ? '🚫' : '○'}
      </button>
      <button type="button" class="glossary-toggle glossary-toggle--del"
              data-glossary-action="delete" data-term="${_esc(g.term)}"
              title="Supprimer">×</button>
    </div>
  `;
}

// ── Bulk-select / bulk-delete (Alt-key) ───────────────────────────────
//
// Pattern repris de l'onglet « Réunions » : maintenir Alt révèle les
// checkboxes, sélectionner ≥1 ligne fait apparaître une barre d'action
// « Supprimer N termes » qui survit au relâchement de Alt tant qu'il
// reste une sélection. Permet la suppression en masse sans confirmation
// par terme (1 seule confirmation pour le batch).
const _selectedTerms = new Set();
let _altPressedGlossary = false;
let _glossaryRootRef = null;

function _glossaryBulkActive() {
  return _altPressedGlossary || _selectedTerms.size > 0;
}

function _refreshBulkVisuals(root) {
  if (!root) return;
  root.classList.toggle('glossary-bulk-active', _glossaryBulkActive());
  const bar = root.querySelector('[data-glossary-bulk-bar]');
  if (!bar) return;
  const n = _selectedTerms.size;
  bar.hidden = !_glossaryBulkActive();
  const count = bar.querySelector('[data-glossary-bulk-count]');
  if (count) count.textContent = n
    ? `${n} terme${n > 1 ? 's' : ''} sélectionné${n > 1 ? 's' : ''}`
    : 'Aucun terme sélectionné';
  const delBtn = bar.querySelector('[data-glossary-bulk-action="delete"]');
  if (delBtn) delBtn.disabled = n === 0;
}

function _onAltDownGlossary(e) {
  if (e.key !== 'Alt' || _altPressedGlossary) return;
  _altPressedGlossary = true;
  _refreshBulkVisuals(_glossaryRootRef);
}

function _onAltUpGlossary(e) {
  if (e.key !== 'Alt' || !_altPressedGlossary) return;
  _altPressedGlossary = false;
  _refreshBulkVisuals(_glossaryRootRef);
}

async function _bulkDeleteSelected(root) {
  const terms = Array.from(_selectedTerms);
  if (terms.length === 0) return;
  const ok = window.confirm(
    `Supprimer ${terms.length} terme${terms.length > 1 ? 's' : ''} du glossaire ?\n\n` +
    terms.slice(0, 12).map((t) => `· ${t}`).join('\n') +
    (terms.length > 12 ? `\n… et ${terms.length - 12} autre(s)` : '')
  );
  if (!ok) return;
  const bar = root.querySelector('[data-glossary-bulk-bar]');
  const delBtn = bar && bar.querySelector('[data-glossary-bulk-action="delete"]');
  if (delBtn) { delBtn.disabled = true; delBtn.textContent = `Suppression de ${terms.length}…`; }
  // Suppressions en parallèle (1 DELETE par terme — pas d'endpoint
  // batch côté backend). On capture les échecs pour rapport, mais on
  // ne stoppe pas la suite : un terme déjà absent (404) ne doit pas
  // bloquer les autres.
  const results = await Promise.allSettled(terms.map((term) =>
    fetch('/api/my-glossary', {
      method: 'DELETE', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ term }),
    }).then((r) => ({ term, ok: r.ok, status: r.status }))
  ));
  const failed = results
    .map((r) => r.status === 'fulfilled' ? r.value : { term: '?', ok: false })
    .filter((r) => !r.ok);
  if (failed.length) {
    alert(`${terms.length - failed.length}/${terms.length} suppressions OK. ${failed.length} échec(s).`);
  }
  _selectedTerms.clear();
  _refreshBulkVisuals(root);
  _loadMyGlossary(root);
}

function _bindGlossaryAddInput(root) {
  const input = root.querySelector('.glossary-add-input');
  if (!input) return;
  input.addEventListener('keydown', async (ev) => {
    if (ev.key !== 'Enter') return;
    ev.preventDefault();
    const term = (input.value || '').trim();
    if (!term) return;
    input.disabled = true;
    try {
      const resp = await fetch('/api/my-glossary', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ term }),
      });
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        alert(`Ajout impossible : ${data.error || ('HTTP ' + resp.status)}`);
      } else {
        input.value = '';
        _loadMyGlossary(root);
      }
    } catch (e) {
      alert(`Erreur réseau : ${e.message}`);
    } finally {
      input.disabled = false;
      input.focus();
    }
  });

  // Délégation click pour toggle / delete sur chaque row.
  root.addEventListener('click', async (ev) => {
    const btn = ev.target.closest && ev.target.closest('[data-glossary-action]');
    if (!btn) return;
    const action = btn.getAttribute('data-glossary-action');
    const term = btn.getAttribute('data-term');
    if (!term) return;
    try {
      if (action === 'delete') {
        const ok = window.confirm(`Supprimer « ${term} » de votre glossaire ?`);
        if (!ok) return;
        await fetch('/api/my-glossary', {
          method: 'DELETE', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ term }),
        });
      } else if (action === 'toggle-curated' || action === 'toggle-blacklisted') {
        const row = btn.closest('.glossary-row');
        const isOn = btn.classList.contains('is-on');
        const field = action === 'toggle-curated' ? 'curated_by_user' : 'blacklisted';
        await fetch('/api/my-glossary', {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ term, [field]: !isOn }),
        });
      }
      _loadMyGlossary(root);
    } catch (e) {
      alert(`Erreur réseau : ${e.message}`);
    }
  });
}

async function _loadMyFeedback(root) {
  const list = root.querySelector('[data-my-feedback-list]');
  if (!list) return;
  try {
    const resp = await fetch('/api/my-feedback?limit=50');
    if (!resp.ok) {
      list.innerHTML = `<p style="color:#94a3b8;font-size:0.85rem;">Erreur de chargement (HTTP ${resp.status}).</p>`;
      return;
    }
    const data = await resp.json();
    const items = data.items || [];
    if (!items.length) {
      list.innerHTML = `<p style="color:#94a3b8;font-size:0.85rem;">Aucun feedback laissé pour l'instant.</p>`;
      return;
    }
    list.innerHTML = items.map(_renderMyFeedbackRow).join('');
  } catch (e) {
    list.innerHTML = `<p style="color:#b91c1c;font-size:0.85rem;">Erreur réseau : ${_esc(e.message)}</p>`;
  }
}

function _renderMyFeedbackRow(fb) {
  const created = fb.created_at
    ? new Date(fb.created_at).toLocaleDateString('fr-FR', { day: 'numeric', month: 'short', year: 'numeric' })
        + ' à ' + new Date(fb.created_at).toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' })
    : '';
  const status = (fb.status || 'new').toLowerCase();
  const statusTag = status === 'processed'
    ? '<span class="my-feedback-tag my-feedback-tag--processed">✓ Pris en compte</span>'
    : (status === 'dismissed'
        ? '<span class="my-feedback-tag my-feedback-tag--dismissed">Écarté</span>'
        : '<span class="my-feedback-tag my-feedback-tag--new">En attente</span>');
  const p = fb.payload || {};
  let summary = '';
  if (fb.type === 'usefulness') {
    const thumb = p.thumb === 'up' ? '👍' : '👎';
    const reasons = Array.isArray(p.reasons) && p.reasons.length
      ? `<div style="font-size:0.78rem;color:#64748b;">${_esc(p.reasons.join(', '))}</div>` : '';
    const free = p.free_text
      ? `<div style="font-size:0.82rem;color:#334155;margin-top:0.2rem;">« ${_esc(p.free_text)} »</div>` : '';
    summary = `<div><strong>${thumb} Utilité de la transcription</strong></div>${reasons}${free}`;
  } else if (fb.type === 'regenerate') {
    const scopeLabel = p.scope === 'full' ? 'Transcription + diarisation' : 'Comptes-rendus (LLM)';
    summary = `<div><strong>🔄 Demande de régénération</strong> — ${_esc(scopeLabel)}</div>
               <div style="font-size:0.82rem;color:#334155;margin-top:0.2rem;">« ${_esc(p.reason || '')} »</div>`;
  } else if (fb.type === 'correction') {
    const applied = Array.isArray(p.applied) && p.applied.length
      ? `<div style="font-size:0.72rem;color:#64748b;margin-top:0.2rem;">Actions appliquées : ${_esc(p.applied.join(', '))}</div>` : '';
    summary = `<div><strong>✏️ Correction de terme</strong></div>
               <div style="font-size:0.82rem;color:#334155;margin-top:0.2rem;">
                 « <span style="color:#b91c1c;">${_esc(p.old || '')}</span> »
                 → <strong style="color:#15803d;">${_esc(p.new || '')}</strong>
               </div>${applied}`;
  } else {
    summary = `<div><em>${_esc(fb.type)}</em></div>`;
  }
  const adminCmt = fb.admin_comment
    ? `<div style="font-size:0.78rem;color:#1d4ed8;margin-top:0.3rem;border-left:2px solid #1d4ed8;padding-left:0.5rem;">
         Réponse admin : ${_esc(fb.admin_comment)}
       </div>` : '';
  return `<div class="my-feedback-item" style="border-bottom:1px solid #f1f5f9;padding:0.6rem 0;">
    <div style="display:flex;align-items:center;gap:0.5rem;margin-bottom:0.2rem;">
      <span style="font-size:0.75rem;color:#94a3b8;">${_esc(created)}</span>
      ${statusTag}
    </div>
    ${summary}
    ${adminCmt}
  </div>`;
}

export function unmount(/* container */) {
  document.removeEventListener('keydown', _onAltDownGlossary);
  document.removeEventListener('keyup', _onAltUpGlossary);
  _altPressedGlossary = false;
  _selectedTerms.clear();
  _glossaryRootRef = null;
}
