// La visite guidée — cinq pas à la première arrivée, rejouable depuis le menu
// commun (« Revoir la visite guidée », lib/menu-hote.js).
//
// Un voile, un cadre qui se déplace d'un élément à l'autre, une bulle qui dit
// ce que l'on FAIT à cet endroit (pas comment c'est construit). « Passer » à
// chaque pas ; elle ne rejoue jamais seule ; le souvenir est côté navigateur
// (localStorage) — rejouée sur un autre poste, ce qui est acceptable.
// Un pas dont l'élément n'existe pas (bouton flottant pas encore monté,
// menu commun absent) est sauté, pas planté.

const CLE_VUE = 'mesreunions.visite.vue';
const Z = 2600;   // au-dessus de la barre du menu commun (2000), sous les modales (10000)

const PAS = [
  { cible: '#tab-btn-transfers', titre: 'Vos réunions', pos: 'bas',
    texte: 'Ici, vous retrouvez chaque réunion enregistrée, avec sa transcription et son compte-rendu rédigé par l’IA.' },
  { cible: '#tab-btn-brief', titre: 'Préparer une réunion', pos: 'bas',
    texte: 'Avant une réunion, l’IA vous prépare un brief : ordre du jour, participants, documents utiles, points de vigilance.' },
  { cible: '.meetings-tab-header [data-action="meetings-new:toggle-menu"][data-menu="add"]', titre: 'Importer une réunion', pos: 'bas',
    texte: 'Ajoutez un fichier audio, une vidéo… ou enregistrez directement avec votre téléphone : tout arrive ici.' },
  { cible: '#mm-p-cpt', avant: _ouvrirCompte, apres: _fermerCompte, titre: 'Le menu en haut à droite', pos: 'gauche',
    texte: 'Sous votre compte : vos téléphones, vos données utiles, la corbeille — et cette visite, si vous voulez la revoir.' },
  { cible: '#rag-fab', titre: 'Posez vos questions', pos: 'haut-droite',
    texte: '« Qu’a-t-on décidé sur le budget ? » L’assistant répond à partir du contenu de vos réunions.' },
];

let _i = -1;
let _els = null;

function _lire() { try { return localStorage.getItem(CLE_VUE) === '1'; } catch (e) { return true; } }
function _ecrire() { try { localStorage.setItem(CLE_VUE, '1'); } catch (e) { /* rien */ } }

function _ouvrirCompte() {
  const b = document.getElementById('mm-b-cpt');
  const p = document.getElementById('mm-p-cpt');
  if (b && p && p.hidden) b.click();
}
function _fermerCompte() {
  const p = document.getElementById('mm-p-cpt');
  const b = document.getElementById('mm-b-cpt');
  if (p && !p.hidden && b) b.click();
}

function _styles() {
  if (document.getElementById('mr-visite-styles')) return;
  const st = document.createElement('style');
  st.id = 'mr-visite-styles';
  st.textContent = `
    #mr-visite{position:fixed;inset:0;z-index:${Z};pointer-events:none;}
    #mr-visite .mr-visite-trou{position:absolute;border-radius:6px;box-shadow:0 0 0 9999px rgba(0,0,145,.55);transition:all .35s ease;}
    #mr-visite .mr-visite-bulle{position:absolute;width:300px;max-width:calc(100vw - 24px);background:#fff;color:#161616;
      border-radius:8px;box-shadow:0 6px 24px rgba(0,0,0,.25);padding:14px 16px;pointer-events:auto;
      border-top:3px solid #000091;transition:all .35s ease;font-size:14px;line-height:1.45;}
    #mr-visite .mr-visite-etape{font-size:11px;color:#666;letter-spacing:.06em;text-transform:uppercase;font-weight:700;}
    #mr-visite h3{font-size:15px;margin:4px 0;color:#161616;}
    #mr-visite p{margin:0 0 10px;color:#3a3a3a;}
    #mr-visite .mr-visite-pied{display:flex;gap:8px;align-items:center;}
    #mr-visite .mr-visite-passer{margin-right:auto;background:none;border:0;color:#000091;text-decoration:underline;cursor:pointer;font:inherit;font-size:13px;padding:0;}
    @media (prefers-reduced-motion:reduce){#mr-visite .mr-visite-trou,#mr-visite .mr-visite-bulle{transition:none;}}
  `;
  document.head.appendChild(st);
}

function _monter() {
  if (_els) return;
  _styles();
  const voile = document.createElement('div');
  voile.id = 'mr-visite';
  voile.innerHTML = `
    <div class="mr-visite-trou"></div>
    <div class="mr-visite-bulle" role="dialog" aria-live="polite" aria-labelledby="mr-visite-titre">
      <div class="mr-visite-etape" data-etape></div>
      <h3 id="mr-visite-titre" data-titre></h3>
      <p data-texte></p>
      <div class="mr-visite-pied">
        <button type="button" class="mr-visite-passer" data-passer>Passer</button>
        <button type="button" class="fr-btn fr-btn--sm fr-btn--secondary" data-prec>Précédent</button>
        <button type="button" class="fr-btn fr-btn--sm" data-suiv>Suivant</button>
      </div>
    </div>`;
  document.body.appendChild(voile);
  _els = {
    voile, trou: voile.querySelector('.mr-visite-trou'), bulle: voile.querySelector('.mr-visite-bulle'),
    etape: voile.querySelector('[data-etape]'), titre: voile.querySelector('[data-titre]'), texte: voile.querySelector('[data-texte]'),
    passer: voile.querySelector('[data-passer]'), prec: voile.querySelector('[data-prec]'), suiv: voile.querySelector('[data-suiv]'),
  };
  // Un clic dans la bulle ne doit pas fermer le panneau du menu commun (son
  // « clic ailleurs » écoute le document).
  _els.bulle.addEventListener('click', (ev) => ev.stopPropagation());
  _els.passer.addEventListener('click', finirVisite);
  _els.prec.addEventListener('click', () => _aller(_i - 1, -1));
  _els.suiv.addEventListener('click', () => _aller(_i + 1, +1));
  document.addEventListener('keydown', _onKey);
  window.addEventListener('resize', _placer);
}

function _onKey(e) {
  if (_i < 0) return;
  if (e.key === 'Escape') { e.preventDefault(); finirVisite(); }
  else if (e.key === 'ArrowRight') { e.preventDefault(); _aller(_i + 1, +1); }
  else if (e.key === 'ArrowLeft') { e.preventDefault(); _aller(_i - 1, -1); }
}

function _cible(pas) {
  try { return document.querySelector(pas.cible); } catch (e) { return null; }
}

// Va au pas demandé ; saute ceux dont l'élément manque, dans le sens donné.
function _aller(i, sens) {
  const courant = PAS[_i];
  if (courant && courant.apres) { try { courant.apres(); } catch (e) { /* rien */ } }
  while (i >= 0 && i < PAS.length) {
    const pas = PAS[i];
    if (pas.avant) { try { pas.avant(); } catch (e) { /* rien */ } }
    if (_cible(pas)) break;
    if (pas.apres) { try { pas.apres(); } catch (e) { /* rien */ } }
    i += sens;
  }
  if (i < 0) i = 0;
  if (i >= PAS.length) { finirVisite(); return; }
  _i = i;
  _placer();
}

function _placer() {
  if (_i < 0 || !_els) return;
  const pas = PAS[_i];
  const el = _cible(pas);
  if (!el) { _aller(_i + 1, +1); return; }
  try { el.scrollIntoView({ block: 'nearest', inline: 'nearest' }); } catch (e) { /* rien */ }
  const r = el.getBoundingClientRect();
  const m = 6;
  const x = r.left - m, y = r.top - m, w = r.width + 2 * m, h = r.height + 2 * m;
  Object.assign(_els.trou.style, { left: x + 'px', top: y + 'px', width: w + 'px', height: h + 'px' });
  _els.etape.textContent = `Étape ${_i + 1} sur ${PAS.length}`;
  _els.titre.textContent = pas.titre;
  _els.texte.textContent = pas.texte;
  _els.prec.hidden = _i === 0;
  _els.suiv.textContent = _i === PAS.length - 1 ? 'Terminer' : 'Suivant';
  const vw = window.innerWidth, vh = window.innerHeight;
  const bw = Math.min(300, vw - 24);
  const bh = _els.bulle.offsetHeight || 160;
  let bx, by;
  if (pas.pos === 'bas') { bx = x; by = y + h + 12; }
  else if (pas.pos === 'gauche') { bx = x - bw - 12; by = y + 8; if (bx < 12) { bx = 12; by = y + h + 12; } }
  else { bx = x + w - bw; by = y - 12 - bh; if (by < 12) by = y + h + 12; }
  bx = Math.max(12, Math.min(bx, vw - bw - 12));
  by = Math.max(12, Math.min(by, vh - bh - 12));
  Object.assign(_els.bulle.style, { left: bx + 'px', top: by + 'px', width: bw + 'px' });
  _els.suiv.focus({ preventScroll: true });
}

export function demarrerVisite(options) {
  const forcer = !!(options && options.forcer);
  if (!forcer && _lire()) return false;
  _monter();
  _els.voile.hidden = false;
  _i = -1;
  _aller(0, +1);
  return true;
}

export function finirVisite() {
  const courant = PAS[_i];
  if (courant && courant.apres) { try { courant.apres(); } catch (e) { /* rien */ } }
  _i = -1;
  if (_els) _els.voile.hidden = true;
  _ecrire();
}

// À la première arrivée : quand la liste des réunions a eu le temps de se
// rendre (l'en-tête « Importer une réunion » est un pas de la visite).
export function initVisiteGuidee() {
  if (_lire()) return;
  let essais = 0;
  const tenter = () => {
    essais++;
    const pret = document.querySelector('.meetings-tab-header [data-menu="add"]');
    if (pret || essais > 20) { demarrerVisite(); return; }
    setTimeout(tenter, 400);
  };
  setTimeout(tenter, 600);
}

window.__visiteGuidee = { demarrer: demarrerVisite, finir: finirVisite };
