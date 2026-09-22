// Le filet de la corbeille : après une mise à la corbeille, un bandeau
// « Mis à la corbeille · Annuler · Voir la corbeille » pendant 10 secondes.
//
// Depuis le 2026-09-22 la corbeille n'a plus d'onglet (elle vit sous le compte,
// « Mes réunions · avancé ») : on la découvrirait au moment où l'on panique.
// Le bandeau rend l'erreur réparable là où elle a été faite.
//
//   annoncerCorbeille([{ type: 'file', id }, { type: 'meeting', id }], 'libellé')
//
// `file` → POST /api/file/<id>/restore ; `meeting` (YouTube, MCR) →
// POST /api/meetings/<id>/restore.

const DUREE_MS = 10_000;
let _minuteur = null;

function _styles() {
  if (document.getElementById('mr-annuler-styles')) return;
  const st = document.createElement('style');
  st.id = 'mr-annuler-styles';
  st.textContent = `
    #mr-annuler{position:fixed;left:50%;bottom:1.2rem;transform:translateX(-50%);z-index:10400;
      display:flex;gap:.9rem;align-items:center;background:#161616;color:#fff;border-radius:6px;
      padding:.6rem .9rem .6rem 1.1rem;box-shadow:0 6px 24px rgba(0,0,0,.3);font-size:.9rem;max-width:calc(100vw - 32px);}
    #mr-annuler[hidden]{display:none;}
    #mr-annuler button{background:none;border:0;color:#aeb4ff;font:inherit;font-weight:600;cursor:pointer;padding:.2rem .3rem;text-decoration:underline;}
    #mr-annuler button:focus-visible{outline:2px solid #fff;outline-offset:2px;}
  `;
  document.head.appendChild(st);
}

function _bandeau() {
  let b = document.getElementById('mr-annuler');
  if (b) return b;
  _styles();
  b = document.createElement('div');
  b.id = 'mr-annuler';
  b.setAttribute('role', 'status');
  b.setAttribute('aria-live', 'polite');
  b.hidden = true;
  b.innerHTML = `<span data-texte></span>
    <button type="button" data-annuler>Annuler</button>
    <button type="button" data-voir>Voir la corbeille</button>`;
  document.body.appendChild(b);
  return b;
}

function _masquer() {
  const b = document.getElementById('mr-annuler');
  if (b) b.hidden = true;
  if (_minuteur) { clearTimeout(_minuteur); _minuteur = null; }
}

async function _restaurer(elements) {
  let ok = 0;
  for (const e of elements) {
    const url = e.type === 'meeting'
      ? `/api/meetings/${encodeURIComponent(e.id)}/restore`
      : `/api/file/${encodeURIComponent(e.id)}/restore`;
    try {
      const r = await fetch(url, { method: 'POST', credentials: 'same-origin' });
      if (r.ok) ok++;
    } catch (err) { /* compté comme échec */ }
  }
  const msg = ok === elements.length
    ? (ok > 1 ? `${ok} réunions restaurées.` : 'Réunion restaurée.')
    : `${ok} restaurée(s) sur ${elements.length}. Les autres restent dans la corbeille.`;
  if (window.showToast) window.showToast(msg, ok === elements.length ? 'success' : 'error');
  if (typeof window.loadSessions === 'function') window.loadSessions({ force: true });
  document.dispatchEvent(new CustomEvent('mesreunions:restauration'));
}

export function annoncerCorbeille(elements, libelle) {
  const liste = (elements || []).filter((e) => e && e.id);
  if (!liste.length) return;
  const b = _bandeau();
  b.querySelector('[data-texte]').textContent = libelle
    || (liste.length > 1 ? `${liste.length} réunions mises à la corbeille.` : 'Mise à la corbeille.');
  const annuler = b.querySelector('[data-annuler]');
  const voir = b.querySelector('[data-voir]');
  annuler.onclick = () => { _masquer(); _restaurer(liste); };
  voir.onclick = () => { _masquer(); if (typeof window.ouvrirEcran === 'function') window.ouvrirEcran('trash'); };
  b.hidden = false;
  if (_minuteur) clearTimeout(_minuteur);
  _minuteur = setTimeout(_masquer, DUREE_MS);
}

window.annoncerCorbeille = annoncerCorbeille;
