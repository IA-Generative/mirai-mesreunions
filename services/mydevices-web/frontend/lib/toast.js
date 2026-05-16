// Toast léger en bas-droite : disparaît après 4s.
// Extrait de index.html (PR4).
export function showToast(message, kind) {
  const t = document.createElement('div');
  t.textContent = message;
  t.className = `toast toast-${kind || 'info'}`;
  document.body.appendChild(t);
  requestAnimationFrame(() => t.classList.add('toast-show'));
  setTimeout(() => {
    t.classList.remove('toast-show');
    setTimeout(() => t.remove(), 250);
  }, 4000);
}

window.showToast = showToast;
