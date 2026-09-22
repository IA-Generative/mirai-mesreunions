// Ancrage du toggle « Mode avancé » sous la barre flottante du menu commun.
//
// Le menu commun de la bêta (/_beta/menu.js, dépôt mirai-apps-menu) pose une
// barre `#mm-barre` en `position: fixed`, verticalement CENTRÉE sur l'en-tête
// qu'il mesure au démarrage. Notre toggle « Mode avancé » vit dans ce même
// en-tête, aligné à droite : les deux se disputaient la même bande et le
// toggle passait SOUS la barre (masqué, et inerte là où ils se recouvraient —
// le menu monte à z-index 2000).
//
// On range donc les deux en DEUX ÉTAGES : la barre remonte de la moitié de la
// place que prend le toggle (l'ensemble reste centré sur l'en-tête), et le
// toggle vient se poser juste dessous, bord droit aligné sur celui de l'avatar
// du compte — la pastille « (ET) », dernier élément de la barre.
//
// Le toggle passe en `position: absolute` DANS l'en-tête (le DSFR pose déjà
// `.fr-header` en `position: relative`) : il défile avec lui, comme avant. La
// barre du menu, elle, reste `fixed` — ce n'est pas la nôtre.
//
// Tout est MESURÉ, rien n'est supposé : ni la hauteur de l'en-tête, ni la
// position de la barre, ni la largeur de l'avatar. Si le menu commun ne charge
// pas (son `onerror` est vide, c'est un cas normal), `#mm-barre` n'apparaît
// jamais et le toggle reste simplement à sa place d'origine dans le flux.
//
// ⚠ CONTRAT AVEC UN AUTRE DÉPÔT — `--mm-haut` NE SE RÈGLE QUE D'UN SEUL ENDROIT.
//   Cette variable et `#mm-barre` appartiennent à `mirai-apps-menu` (src/menu.js).
//   Depuis sa version 1.8.0, il sait la régler lui-même : `placerSurEnTete()` la
//   réécrit en boucle (rAF + minuteries + ResizeObserver + observateur de
//   mutations) pour les hôtes qui déclarent une clé `ancrage` dans sa table APPS.
//   Aujourd'hui `mesfichiers` est le seul, donc aucun conflit — mais le jour où
//   `mesreunions` en reçoit une, les deux écriront la même variable sur le même
//   nœud à la fréquence d'une trame, et le bouton sautera sans que rien ne
//   l'explique. La règle : qui reçoit un `ancrage` perd ce module, dans le même
//   commit. Le même avertissement est posé en face, dans `placerSurEnTete()`.
//
// Éprouvé par `tests/unit/test_advanced_toggle_anchor.js` (8 cas, sans jsdom :
// jsdom ne fait pas de mise en page et rendrait 0 partout, ce qui verdirait le
// test sans rien mesurer). Vu rouge : neutraliser `classList.add('is-anchored')`
// ci-dessous en fait tomber trois.

const ECART = 6;          // respiration entre la barre et le toggle, en px
const HAUT_MINI = 5;      // le filet tricolore du menu occupe les 3 premiers px
const REPLI_TEL = 900;    // seuil du repli mobile du menu commun (media query)
const ATTENTE_MAX = 15000; // le menu se construit après un appel réseau

let hautOrigine = null;   // top de la barre AVANT notre décalage, mémorisé une fois

function placer() {
  const barre = document.getElementById('mm-barre');
  const btn = document.getElementById('advanced-toggle');
  if (!barre || !btn) return false;

  // En dessous du repli, le menu commun impose son propre `top` (--mm-haut-tel) :
  // notre remontée y serait sans effet, on se contente d'empiler le toggle.
  const large = window.innerWidth > REPLI_TEL;
  if (large && hautOrigine === null) {
    hautOrigine = barre.getBoundingClientRect().top;
  }
  if (large && hautOrigine !== null) {
    const place = (btn.offsetHeight || 20) + ECART;
    const haut = Math.max(HAUT_MINI, Math.round(hautOrigine - place / 2));
    barre.style.setProperty('--mm-haut', haut + 'px');
  }

  // L'avatar du compte est le dernier élément de la barre ; s'il manque
  // (menu sans avatar), le bord droit de la barre fait aussi bien l'affaire.
  const ancre = document.getElementById('mm-b-cpt') || barre;
  const entete = btn.closest('.fr-header') || document.body;
  const rb = barre.getBoundingClientRect();
  const ra = ancre.getBoundingClientRect();
  const re = entete.getBoundingClientRect();

  // Coordonnées relatives au repère `.fr-header`, pas au viewport : la barre
  // est mesurée en viewport, on retranche l'origine de l'en-tête.
  btn.classList.add('is-anchored');
  btn.style.top = Math.round(rb.bottom + ECART - re.top) + 'px';
  btn.style.right = Math.max(4, Math.round(re.right - ra.right)) + 'px';
  return true;
}

export function initAdvancedToggleAnchor() {
  if (placer()) return brancherResize();

  // La barre naît après `demanderCapacites()` — donc après un aller-retour
  // réseau, à un instant que rien ne nous annonce. On observe, borné dans le
  // temps pour ne pas laisser un observer vivre si le menu n'arrive jamais.
  const obs = new MutationObserver(() => {
    if (placer()) { obs.disconnect(); brancherResize(); }
  });
  obs.observe(document.body, { childList: true });
  window.setTimeout(() => obs.disconnect(), ATTENTE_MAX);
}

let resizeBranche = false;
function brancherResize() {
  if (resizeBranche) return;
  resizeBranche = true;
  let t = 0;
  window.addEventListener('resize', () => {
    window.clearTimeout(t);
    t = window.setTimeout(placer, 120);
  });
}
