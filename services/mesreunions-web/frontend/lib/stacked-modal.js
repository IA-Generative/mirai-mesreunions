// Modale empilable — une modale qui s'ouvre PAR-DESSUS une autre modale.
//
// Pourquoi un module de plus alors que `lib/prep-modal.js` existe ? Parce que
// `prep-modal.js` est structurellement inutilisable au-dessus du wizard, pour
// trois raisons qui se cumulent :
//
//   1. son ancre `#prep-modal-backdrop` est en `z-index:1000`, sous le voile
//      du wizard (`z-index:9000`) — elle s'ouvre *derrière* ;
//   2. cette ancre est imbriquée dans `#panel-brief`, qui passe en
//      `display:none` dès qu'on change d'onglet — la modale disparaît avec
//      son panneau ;
//   3. son `close()` remet `document.body.style.overflow = ''`, ce qui
//      déverrouille le scroll de la page derrière un wizard resté ouvert.
//
// On généralise donc le pattern déjà éprouvé de la modale CR-editor
// (`frontend/legacy.js`, `_openCrEditorModal`) : un wrap créé dynamiquement
// dans `document.body`. Créer dans `body` a un second bénéfice, moins visible
// mais décisif ici : la modale est **hors du `<form id="wizard-form">`**, donc
// aucun bouton de picker ne peut déclencher la soumission implicite du wizard.
//
// Invariants tenus par ce module :
//   - `z-index` à partir de 10200, +20 par niveau d'empilement ;
//   - listeners de fermeture posés **sur le wrap**, jamais sur `document` :
//     un listener global survivrait au retrait du wrap et fermerait la modale
//     suivante (bug classique de la pile) ;
//   - `role="dialog"` + `aria-modal="true"` + focus trap + restauration du
//     focus sur l'élément qui a ouvert la modale ;
//   - **on ne touche pas à `document.body.style.overflow`** : le wizard en est
//     propriétaire, le lui reprendre casserait son verrou de scroll.
//
// API :
//   openStackedModal({ title, bodyHtml|bodyEl, footerEl, width, onClose })
//     → { el, body, footer, close() }
//   closeTopStackedModal()
//   isStackedModalOpen()   ← utilisé par la garde ESC du wizard

const BASE_Z_INDEX = 10200;
const Z_STEP = 20;

// Pile des modales ouvertes (la dernière est celle du dessus).
const _stack = [];

const _SAFE_RE = /[&<>"']/g;
function _esc(s) {
  return String(s == null ? '' : s).replace(_SAFE_RE, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

const _FOCUSABLE = [
  'a[href]', 'button:not([disabled])', 'input:not([disabled])',
  'select:not([disabled])', 'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

function _focusables(root) {
  return Array.from(root.querySelectorAll(_FOCUSABLE))
    // `offsetParent === null` écarte tout ce qui est masqué (onglets repliés,
    // états de chargement remplacés) — piéger le focus sur un élément invisible
    // donne un Tab qui « ne fait rien » et un utilisateur bloqué.
    .filter((el) => el.offsetParent !== null || el === document.activeElement);
}

/** Y a-t-il au moins une modale empilée ouverte ? */
export function isStackedModalOpen() {
  return _stack.length > 0;
}

/** Ferme la modale du dessus (no-op si la pile est vide). */
export function closeTopStackedModal() {
  const top = _stack[_stack.length - 1];
  if (top) top.close();
}

export function openStackedModal(opts) {
  opts = opts || {};
  const level = _stack.length;
  // L'élément à re-focaliser à la fermeture : celui qui avait le focus au
  // moment de l'ouverture (typiquement la carte cliquée). Sans ça, le focus
  // repart au `<body>` et la navigation clavier redémarre du haut de la page.
  const previousFocus = (opts.trigger instanceof Element)
    ? opts.trigger
    : (document.activeElement instanceof Element ? document.activeElement : null);

  const wrap = document.createElement('div');
  wrap.className = 'stacked-modal';
  wrap.setAttribute('role', 'dialog');
  wrap.setAttribute('aria-modal', 'true');
  wrap.setAttribute('aria-label', String(opts.title || 'Fenêtre'));
  wrap.style.cssText = 'position:fixed;inset:0;background:rgba(15,23,42,0.55);'
    + 'display:flex;align-items:flex-start;justify-content:center;'
    + 'padding:3vh 1rem 2rem;overflow-y:auto;'
    + 'z-index:' + (BASE_Z_INDEX + level * Z_STEP) + ';';

  const width = opts.width || '760px';
  wrap.innerHTML = `
    <div class="stacked-modal-inner" role="document"
         style="background:#fff;border-radius:0.5rem;width:100%;max-width:${_esc(width)};
                display:flex;flex-direction:column;max-height:92vh;
                box-shadow:0 12px 48px rgba(0,0,0,0.30);">
      <div class="stacked-modal-head"
           style="display:flex;justify-content:space-between;align-items:center;gap:0.6rem;
                  padding:0.7rem 1rem;border-bottom:1px solid #e2e8f0;background:#f0f6ff;">
        <div style="font-weight:600;color:#0c4498;font-size:1rem;">${_esc(opts.title || '')}</div>
        <button type="button" class="stacked-modal-close" aria-label="Fermer"
                style="background:transparent;border:0;font-size:1.3rem;cursor:pointer;color:#64748b;">×</button>
      </div>
      <div class="stacked-modal-body"
           style="flex:1 1 auto;overflow-y:auto;padding:0.9rem 1rem;"></div>
      <div class="stacked-modal-foot"
           style="display:flex;justify-content:flex-end;gap:0.5rem;
                  padding:0.6rem 1rem;border-top:1px solid #f1f5f9;"></div>
    </div>`;

  const body = wrap.querySelector('.stacked-modal-body');
  const footer = wrap.querySelector('.stacked-modal-foot');
  if (opts.bodyEl instanceof Element) body.appendChild(opts.bodyEl);
  else if (opts.bodyHtml) body.innerHTML = opts.bodyHtml;
  if (opts.footerEl instanceof Element) footer.appendChild(opts.footerEl);
  else if (opts.footerHtml) footer.innerHTML = opts.footerHtml;
  else footer.style.display = 'none';

  let closed = false;
  const handle = {
    el: wrap,
    body,
    footer,
    close() {
      if (closed) return;
      closed = true;
      const idx = _stack.indexOf(handle);
      if (idx !== -1) _stack.splice(idx, 1);
      wrap.remove();
      // Restauration du focus : uniquement si l'élément est encore dans le
      // document (un picker peut avoir re-rendu la carte déclencheuse).
      try {
        if (previousFocus && previousFocus.isConnected) previousFocus.focus();
      } catch (e) { /* non-fatal */ }
      if (typeof opts.onClose === 'function') {
        try { opts.onClose(); } catch (e) { /* non-fatal */ }
      }
    },
  };

  wrap.querySelector('.stacked-modal-close').addEventListener('click', handle.close);
  // Clic sur le voile (et pas sur la carte) = fermeture.
  wrap.addEventListener('click', (ev) => { if (ev.target === wrap) handle.close(); });
  wrap.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') {
      // `stopPropagation` en plus de la garde `isStackedModalOpen()` posée
      // dans le wizard : ceinture et bretelles, l'événement ne doit pas
      // atteindre le handler ESC global qui fermerait le wizard entier.
      ev.preventDefault();
      ev.stopPropagation();
      handle.close();
      return;
    }
    if (ev.key !== 'Tab') return;
    // Focus trap : Tab en boucle à l'intérieur du wrap.
    const items = _focusables(wrap);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (ev.shiftKey && document.activeElement === first) {
      ev.preventDefault();
      last.focus();
    } else if (!ev.shiftKey && document.activeElement === last) {
      ev.preventDefault();
      first.focus();
    }
  });

  document.body.appendChild(wrap);
  _stack.push(handle);

  // Focus initial : le premier élément focalisable du corps, sinon le bouton
  // de fermeture — pour qu'ESC/Tab fonctionnent immédiatement.
  setTimeout(() => {
    const items = _focusables(body);
    const target = items[0] || wrap.querySelector('.stacked-modal-close');
    if (target) { try { target.focus(); } catch (e) {} }
  }, 30);

  return handle;
}
