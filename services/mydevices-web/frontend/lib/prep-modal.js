// Modale générique réutilisable pour la fiche brief — pilote l'élément
// #prep-modal-backdrop défini dans index.html (Lot 3).
//
// API :
//   openPrepModal({title, bodyHtml|bodyEl, footerEl, onClose?}) → resolve promise
//   closePrepModal()
//   bindPrepModal() → pose les listeners (idempotent, à appeler au boot)

let _openCb = null;

function _qs(sel) { return document.querySelector(sel); }

export function openPrepModal({ title, bodyHtml, bodyEl, footerEl, onClose } = {}) {
  const backdrop = _qs('#prep-modal-backdrop');
  const titleEl = _qs('#prep-modal-title');
  const bodyMount = _qs('#prep-modal-body');
  const footMount = _qs('#prep-modal-footer');
  if (!backdrop || !titleEl || !bodyMount || !footMount) return;
  titleEl.textContent = title || '';
  bodyMount.innerHTML = '';
  if (bodyEl instanceof Element) {
    bodyMount.appendChild(bodyEl);
  } else if (bodyHtml) {
    bodyMount.innerHTML = bodyHtml;
  }
  footMount.innerHTML = '';
  if (footerEl instanceof Element) footMount.appendChild(footerEl);
  backdrop.style.display = 'flex';
  document.body.style.overflow = 'hidden';
  _openCb = typeof onClose === 'function' ? onClose : null;
}

export function closePrepModal() {
  const backdrop = _qs('#prep-modal-backdrop');
  if (!backdrop) return;
  backdrop.style.display = 'none';
  document.body.style.overflow = '';
  const cb = _openCb; _openCb = null;
  if (cb) { try { cb(); } catch (e) {} }
}

export function bindPrepModal() {
  const backdrop = _qs('#prep-modal-backdrop');
  if (!backdrop || backdrop.__bound) return;
  backdrop.__bound = true;
  backdrop.addEventListener('click', (ev) => {
    if (ev.target === backdrop) closePrepModal();
    const a = ev.target && ev.target.closest && ev.target.closest('[data-action="close-prep-modal"]');
    if (a) closePrepModal();
  });
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && backdrop.style.display === 'flex') {
      ev.preventDefault();
      closePrepModal();
    }
  });
}
