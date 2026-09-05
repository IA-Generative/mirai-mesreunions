// Unit tests pour services/mesreunions-web/frontend/lib/advanced-toggle-anchor.js
//
// Exécutable directement :  node --test tests/unit/test_advanced_toggle_anchor.js
// Aussi déclenché par      :  pytest tests/unit/test_advanced_toggle_anchor.py
//
// ── Pourquoi ce test existe ────────────────────────────────────────────────────
// Le toggle « Mode avancé » et la barre flottante du menu commun se disputaient la
// même bande de l'en-tête. Le menu monte à z-index 2000 : là où ils se recouvraient,
// le bouton était VISIBLE et INERTE. C'est le mode de panne qu'une capture d'écran
// ne montre pas — d'où des assertions qui portent sur la GÉOMÉTRIE (le bouton
// est-il sous la barre ? à droite du bon repère ?), jamais sur l'apparence.
//
// ── Pourquoi un faux DOM à la main, et pas jsdom ───────────────────────────────
// Le module ne lit que trois choses : `getBoundingClientRect()`, `offsetHeight` et
// `window.innerWidth`. jsdom ne fait pas de mise en page — il rendrait 0 partout, et
// le test passerait au vert sans rien mesurer. Un modèle explicite de la géométrie
// est à la fois plus honnête et sans dépendance nouvelle, comme le reste de la suite.
//
// Pour vérifier que ce fichier peut ÉCHOUER : commenter `btn.classList.add(...)`
// dans `placer()` — la moitié des cas doit tomber.

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const MODULE_PATH = path.resolve(
  __dirname, '..', '..',
  'services', 'mesreunions-web', 'frontend', 'lib', 'advanced-toggle-anchor.js',
);

// Le module garde un état au niveau du fichier (`hautOrigine`, `resizeBranche`).
// Un import par scénario, sinon le second hérite du décalage mesuré par le premier.
let compteurImport = 0;
async function chargerModuleNeuf() {
  compteurImport += 1;
  return await import(`${pathToFileURL(MODULE_PATH).href}?n=${compteurImport}`);
}

// ── Un DOM minuscule, mais qui a des COORDONNÉES ──────────────────────────────
// Chaque noeud connaît son rectangle en viewport. Le seul calcul du faux DOM est
// celui que le vrai navigateur ferait pour nous : `top`/`right` posés en absolu dans
// `.fr-header` déplacent réellement le bouton.
function faireNoeud(id, rect, classes = []) {
  return {
    id,
    _rect: { ...rect },
    style: {
      _vars: {},
      setProperty(nom, valeur) { this._vars[nom] = valeur; },
      getPropertyValue(nom) { return this._vars[nom]; },
    },
    classList: {
      _c: new Set(classes),
      add(c) { this._c.add(c); },
      contains(c) { return this._c.has(c); },
    },
    getBoundingClientRect() { return { ...this._rect }; },
    get offsetHeight() { return this._rect.bottom - this._rect.top; },
    closest() { return null },
  };
}

/**
 * L'en-tête de Mes réunions tel qu'il est réellement : `.fr-header` du DSFR sur
 * toute la largeur, la barre du menu commun flottant à droite, l'avatar du compte
 * en dernier élément de cette barre, et le toggle « Mode avancé » qui, AVANT
 * l'ancrage, vit dans le flux du même en-tête et empiète sur la barre.
 */
function poserLaScene({ largeur = 1280 } = {}) {
  const entete = faireNoeud('entete', { top: 0, bottom: 100, left: 0, right: largeur });
  const barre = faireNoeud('mm-barre', { top: 20, bottom: 50, left: largeur - 300, right: largeur - 16 });
  const avatar = faireNoeud('mm-b-cpt', { top: 26, bottom: 44, left: largeur - 60, right: largeur - 24 });
  // Le toggle empiète sur la barre : c'est l'état AVANT ancrage, celui qui rendait le
  // bouton visible et inerte. Le premier test vérifie que la scène le reproduit bien.
  const btn = faireNoeud('advanced-toggle', { top: 24, bottom: 44, left: largeur - 330, right: largeur - 220 });
  btn.closest = (sel) => (sel === '.fr-header' ? entete : null);

  const noeuds = { 'mm-barre': barre, 'advanced-toggle': btn, 'mm-b-cpt': avatar };
  const ecouteurs = [];

  global.window = {
    innerWidth: largeur,
    addEventListener: (type, fn) => ecouteurs.push([type, fn]),
    // `.unref()` : le module pose un minuteur de 15 s pour débrancher son observateur.
    // Sans cela, `node --test` attendrait ces quinze secondes avant de rendre la main.
    setTimeout: (fn, ms) => { const t = setTimeout(fn, ms); if (t.unref) t.unref(); return t; },
    clearTimeout: (t) => clearTimeout(t),
  };
  global.document = {
    body: entete,
    getElementById: (id) => noeuds[id] || null,
  };
  global.MutationObserver = class {
    constructor(fn) { this.fn = fn; }
    observe() {}
    disconnect() { this.deconnecte = true; }
  };
  global.setTimeout = global.setTimeout || setTimeout;

  return { entete, barre, avatar, btn, ecouteurs, noeuds };
}

/** Le rectangle du bouton APRÈS ancrage, ramené en coordonnées viewport. */
function rectangleAncre(btn, entete) {
  const haut = entete._rect.top + parseInt(btn.style.top, 10);
  const droite = entete._rect.right - parseInt(btn.style.right, 10);
  const hauteur = btn._rect.bottom - btn._rect.top;
  const largeur = btn._rect.right - btn._rect.left;
  return { top: haut, bottom: haut + hauteur, right: droite, left: droite - largeur };
}

function seRecouvrent(a, b) {
  return a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
}

// ══════════════════════════════════════════════════════════════════════════════

test('sans ancrage, le toggle et la barre se recouvrent — le défaut qu on répare', () => {
  const { barre, btn } = poserLaScene();
  assert.ok(seRecouvrent(btn._rect, barre._rect),
    'la scène doit reproduire le défaut, sinon le test ne prouve rien');
});

test('le toggle passe SOUS la barre, sans la toucher', async () => {
  const { entete, barre, btn } = poserLaScene();
  const { initAdvancedToggleAnchor } = await chargerModuleNeuf();
  initAdvancedToggleAnchor();

  assert.ok(btn.classList.contains('is-anchored'), 'le toggle doit passer en absolu');
  const pose = rectangleAncre(btn, entete);
  // La barre a elle-même bougé : le module la remonte de la moitié de la place prise.
  const hautBarre = parseInt(barre.style.getPropertyValue('--mm-haut'), 10);
  const barreApres = { ...barre._rect, top: hautBarre, bottom: hautBarre + 30 };
  assert.ok(!seRecouvrent(pose, barreApres),
    `le toggle (${pose.top}→${pose.bottom}) chevauche encore la barre (${barreApres.top}→${barreApres.bottom})`);
  assert.ok(pose.top >= barreApres.bottom, 'le toggle doit être DESSOUS, pas au-dessus');
});

test('l ensemble reste centré : la barre remonte de la moitié de la place prise', async () => {
  const { barre, btn } = poserLaScene();
  const avant = barre._rect.top;                       // 20
  const { initAdvancedToggleAnchor } = await chargerModuleNeuf();
  initAdvancedToggleAnchor();

  const apres = parseInt(barre.style.getPropertyValue('--mm-haut'), 10);
  const place = (btn._rect.bottom - btn._rect.top) + 6;   // hauteur du toggle + ECART
  assert.equal(apres, Math.round(avant - place / 2));
});

test('le bord droit du toggle s aligne sur celui de l avatar du compte', async () => {
  const { entete, avatar, btn } = poserLaScene();
  const { initAdvancedToggleAnchor } = await chargerModuleNeuf();
  initAdvancedToggleAnchor();

  const pose = rectangleAncre(btn, entete);
  assert.equal(Math.round(pose.right), Math.round(avatar._rect.right),
    'aligner sur l avatar est le seul repère stable : c est le dernier élément de la barre');
});

test('sans avatar, le bord droit de la barre fait l affaire', async () => {
  const { entete, barre, btn, noeuds } = poserLaScene();
  delete noeuds['mm-b-cpt'];                            // menu sans panneau compte
  const { initAdvancedToggleAnchor } = await chargerModuleNeuf();
  initAdvancedToggleAnchor();

  const pose = rectangleAncre(btn, entete);
  assert.equal(Math.round(pose.right), Math.round(barre._rect.right));
});

test('sous le repli mobile, la barre n est PAS remontée', async () => {
  // En dessous de 900 px le menu commun impose son propre `top` (--mm-haut-tel) :
  // écrire par-dessus serait sans effet ici, et faux le jour où il changera.
  const { barre, btn } = poserLaScene({ largeur: 390 });
  const { initAdvancedToggleAnchor } = await chargerModuleNeuf();
  initAdvancedToggleAnchor();

  assert.equal(barre.style.getPropertyValue('--mm-haut'), undefined,
    'aucun --mm-haut ne doit être écrit sous le repli');
  assert.ok(btn.classList.contains('is-anchored'), 'le toggle, lui, s empile quand même');
});

test('menu commun absent : le toggle reste dans le flux, et rien ne lève', async () => {
  // `onerror` du script du menu est vide : ne pas charger est un cas NORMAL.
  const { btn, noeuds } = poserLaScene();
  delete noeuds['mm-barre'];
  const { initAdvancedToggleAnchor } = await chargerModuleNeuf();
  initAdvancedToggleAnchor();                            // ne doit pas lever

  assert.ok(!btn.classList.contains('is-anchored'),
    'sans barre, le toggle garde sa place d origine — on ne force rien');
});

test('l observateur est branché quand la barre arrive en retard, et se débranche après', async () => {
  const { btn, noeuds } = poserLaScene();
  const barreTardive = noeuds['mm-barre'];
  delete noeuds['mm-barre'];

  let observateur = null;
  global.MutationObserver = class {
    constructor(fn) { this.fn = fn; observateur = this; }
    observe() {}
    disconnect() { this.deconnecte = true; }
  };
  const { initAdvancedToggleAnchor } = await chargerModuleNeuf();
  initAdvancedToggleAnchor();

  assert.ok(observateur, 'la barre naît après un aller-retour réseau : il faut observer');
  noeuds['mm-barre'] = barreTardive;                     // le menu finit par arriver
  observateur.fn();
  assert.ok(btn.classList.contains('is-anchored'), 'le placement doit se faire au retard');
  assert.ok(observateur.deconnecte, 'et l observateur doit se débrancher, pas vivre pour rien');
});
