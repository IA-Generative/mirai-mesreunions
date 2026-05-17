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
// mydevices-web pour l'instant — ce module rend une vue informative
// + des cards "à venir" pour les intégrations futures (mail, agenda,
// Resana, mescollections, drives institutionnels).

import { CURRENT_USER } from '../lib/bootstrap.js';

export const COMING_SOON = [
  { id: 'mail', label: 'Boîte mail', icon: 'fr-icon-mail-line' },
  { id: 'agenda', label: 'Agenda', icon: 'fr-icon-calendar-line' },
  { id: 'drive-perso', label: 'Drive personnel (Google)', icon: 'fr-icon-cloud-line' },
  { id: 'drive-dinum', label: 'Drive DINUM / DTNUM', icon: 'fr-icon-folder-2-line' },
  { id: 'resana', label: 'Resana', icon: 'fr-icon-team-line' },
  { id: 'mescollections', label: 'Mes collections', icon: 'fr-icon-bookmark-line' },
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
          <p>
            Votre <strong>glossaire utilisateur</strong> agrège automatiquement
            les termes métier extraits de tous vos briefs de préparation
            (noms propres, sigles, expressions). Il est utilisé en amont de
            chaque transcription pour améliorer la reconnaissance vocale
            (initial-prompt Whisper) puis en aval pour la correction
            terminologique LLM.
          </p>
          <p style="margin-top:0.4rem;">
            Le fichier est synchronisé en temps réel dans votre Drive
            (<em>Préparations de réunion/glossaire-utilisateur.txt</em>).
            Limites en vigueur : 50 termes prioritaires par brief,
            200 termes secondaires, 300 termes au total dans le glossaire global.
          </p>
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
}

export function unmount(/* container */) {
  // Le rendu est statique — rien à nettoyer.
}
